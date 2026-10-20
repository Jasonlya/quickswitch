"""基于 asyncio 的 JSON Lines 双向 TCP 通信。

设计要点：

- 同时监听本地端口并主动连接对端：任一台机器在线即可建立链路，
  主动连接断开时自动重连（指数退避）。
- 发送优先走「本机主动建立」的连接；若主动连接尚未建立，则复用对端连进来的
  连接，避免单向可达时无法回消息（例如对端在内网、本机在 NAT 后）。
- 心跳 ``ping`` / ``pong``；超过 3 个心跳周期收不到任何消息视为半开连接，主动断开重连。
- 消息为 JSON Lines，一行一条，便于 tcpdump / 日志排查。
- 配置了 ``auth.secret`` 时，连接建立后立即做一次 HMAC 质询-响应（见 §9 协议）：
  每条连接各发一个随机 nonce，对端必须用同一个预共享密钥对「质询串 + 自己的 nonce」
  算出校验码。**没有通过校验的对端发来的 ``switch`` / ``switch_ack`` 一律丢弃**，
  因此局域网里任何人都无法靠伪造报文切换显示器。
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("net")

Message = Dict[str, Any]
MessageHandler = Callable[[Message], Awaitable[None]]
EventHook = Callable[..., None]

AUTH_ALG = "hmac-sha256"
AUTH_PROTOCOL = 1

# 通过校验前会被丢弃的消息类型（握手与心跳始终放行，否则无法完成握手/探活）
GUARDED_TYPES = ("switch", "switch_ack")


def _fmt_addr(addr: Any) -> str:
    if isinstance(addr, (tuple, list)) and len(addr) >= 2:
        return f"{addr[0]}:{addr[1]}"
    return str(addr)


def challenge_mac(secret: str, challenged: str, own: str) -> str:
    """对端的质询串 ``challenged`` 与本端 nonce ``own`` 一起做 HMAC-SHA256。"""
    payload = f"quickswitch-auth-{AUTH_PROTOCOL}|{challenged}|{own}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_mac(secret: str, challenged: str, own: str, mac: str) -> bool:
    return hmac.compare_digest(challenge_mac(secret, challenged, own), str(mac))


class _Auth:
    """单条连接的校验状态。

    每次连接都重新生成 nonce，所以旧连接上抓到的校验码无法重放到新连接
    （质询串对不上）；改一个 nonce 又算不出新校验码——除非真的知道密钥。
    """

    def __init__(self, secret: Optional[str], *, on_verified: Optional[Callable[[], None]] = None) -> None:
        self.secret = (secret or "").strip() or None
        self.nonce: Optional[str] = secrets.token_hex(16) if self.secret else None
        self.peer_nonce: Optional[str] = None
        self.peer_alg: Optional[str] = None
        self.peer_declared = False
        self.ok = False
        self.rejected = 0
        self._on_verified = on_verified

    # ---- 本端要发的东西
    def hello_auth(self) -> Optional[str]:
        return AUTH_ALG if self.secret else None

    def challenge(self) -> Optional[Dict[str, Any]]:
        if not self.secret:
            return None
        return {"type": "auth_challenge", "nonce": self.nonce, "alg": AUTH_ALG}

    def response_for(self, challenged: str) -> Dict[str, Any]:
        return {
            "type": "auth_response",
            "nonce": challenged,
            "mine": self.nonce,
            "alg": AUTH_ALG,
            "mac": challenge_mac(self.secret or "", challenged, self.nonce or ""),
        }

    # ---- 收到的东西
    async def absorb(self, msg: Message, send: Callable[[Message], Awaitable[bool]]) -> bool:
        """处理握手消息；返回 True 表示该消息可以交给上层。"""
        mtype = msg.get("type")
        if mtype == "hello":
            declared = msg.get("auth")
            self.peer_declared = bool(declared)
            self.peer_alg = str(declared) if declared else None
            if declared and declared != AUTH_ALG:
                log.warning("对端声明了不支持的校验方式 %r，该连接无法通过校验", declared)
            return True
        if mtype == "auth_challenge":
            if not self.secret:
                return False  # 没配密钥就不需要校验：握手报文不上交
            nonce = str(msg.get("nonce") or "")
            if not nonce:
                self._reject("质询串为空")
                return False
            self.peer_nonce = nonce
            if not self.ok:
                await send(self.response_for(nonce))
            return False
        if mtype == "auth_response":
            if not self.secret:
                return False
            if not self._accept_response(msg):
                return False
            return False
        if mtype in GUARDED_TYPES and self.secret and not self.ok:
            self._reject(f"{mtype} 来自未通过校验的对端，已丢弃")
            return False
        return True

    def _accept_response(self, msg: Message) -> bool:
        challenged = str(msg.get("nonce") or "")
        own = str(msg.get("mine") or "")
        mac = str(msg.get("mac") or "")
        if not challenged or not own or not mac:
            self._reject("校验回复字段不完整")
            return False
        if challenged != self.nonce:
            self._reject("质询串与本连接不符（可能是重放）")
            return False
        if self.peer_nonce and own != self.peer_nonce:
            self._reject("nonce 与本连接不符（可能是重放）")
            return False
        if not verify_mac(self.secret or "", challenged, own, mac):
            self._reject("校验码不匹配（两端 auth.secret 可能不一致）")
            return False
        if not self.ok:
            self.ok = True
            log.info("对端校验通过（%s）", AUTH_ALG)
            if self._on_verified:
                with contextlib.suppress(Exception):
                    self._on_verified()
        return True

    def _reject(self, reason: str) -> None:
        self.rejected += 1
        if self.rejected == 1:
            log.warning("丢弃未经校验的报文：%s", reason)
        else:
            log.debug("继续丢弃未经校验的报文（第 %d 条）：%s", self.rejected, reason)


class PeerConnection:
    """与对端保持一条（或一组）JSON Lines 连接，提供可靠的 ``send``。"""

    def __init__(
        self,
        host: str,
        port: int,
        listen_port: int,
        on_message: MessageHandler,
        *,
        name: str = "quickswitch",
        identity: Optional[Dict[str, Any]] = None,
        ping_interval: float = 10.0,
        reconnect_interval: float = 3.0,
        reconnect_max_interval: float = 30.0,
        on_event: Optional[EventHook] = None,
        secret: Optional[str] = None,
    ) -> None:
        self.host = (host or "").strip()
        self.port = int(port)
        self.listen_port = int(listen_port)
        self.on_message = on_message
        self.name = name
        self.identity = dict(identity or {})
        self.ping_interval = float(ping_interval)
        self.reconnect_interval = float(reconnect_interval)
        self.reconnect_max_interval = float(reconnect_max_interval)
        self.on_event = on_event
        self.secret = (secret or "").strip() or None

        self._server: Optional[asyncio.AbstractServer] = None
        self._writer: Optional[asyncio.StreamWriter] = None      # 本机主动建立的连接
        self._inbound: Optional[asyncio.StreamWriter] = None     # 对端连进来的连接
        self._write_lock = asyncio.Lock()
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self._last_rx = 0.0
        self._peer_info: Dict[str, Any] = {}
        self._peer_auth: Optional[str] = None
        self._authenticated = False

    # ---------------------------------------------------------------- 状态
    @property
    def active_writer(self) -> Optional[asyncio.StreamWriter]:
        for writer in (self._writer, self._inbound):
            if writer is not None and not writer.is_closing():
                return writer
        return None

    @property
    def connected(self) -> bool:
        return self.active_writer is not None

    @property
    def outbound_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    @property
    def peer_info(self) -> Dict[str, Any]:
        return dict(self._peer_info)

    @property
    def auth_enabled(self) -> bool:
        """本机是否启用了预共享密钥校验。"""
        return self.secret is not None

    @property
    def peer_auth(self) -> Optional[str]:
        """对端 hello 里声明的校验方式；None 表示对端没有声明（旧版本或未配置）。"""
        return self._peer_auth

    @property
    def authenticated(self) -> bool:
        """对端是否已证明它持有同一个密钥；未配置密钥时恒为 False（因为没有校验）。"""
        return bool(self._authenticated)

    @property
    def running(self) -> bool:
        return self._running

    def _emit(self, event: str, **fields: Any) -> None:
        if not self.on_event:
            return
        try:
            self.on_event(event, **fields)
        except Exception as exc:  # 事件回调不影响主流程
            log.debug("事件回调异常：%s", exc)

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> "PeerConnection":
        """启动监听与连接循环；端口被占用时抛出 OSError（也用于单实例检测）。"""
        if self._running:
            return self
        self._running = True
        try:
            self._server = await asyncio.start_server(self._handle_inbound, "0.0.0.0", self.listen_port)
        except OSError:
            self._running = False
            raise
        log.info("监听 0.0.0.0:%s", self.listen_port)
        self._tasks.append(asyncio.create_task(self._connect_loop(), name=f"{self.name}-connect"))
        self._tasks.append(asyncio.create_task(self._ping_loop(), name=f"{self.name}-ping"))
        return self

    async def stop(self) -> None:
        self._running = False
        self._authenticated = False
        self._drop_writer(self._writer)
        self._drop_writer(self._inbound)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        log.debug("网络已停止")

    async def wait_connected(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected:
                return True
            await asyncio.sleep(0.05)
        return self.connected

    def set_host(self, host: str, port: Optional[int] = None) -> None:
        """更新对端地址（自动发现到新 IP 时调用）；地址变化会触发重连。"""
        if port:
            self.port = int(port)
        host = (host or "").strip()
        if not host or host == self.host:
            return
        log.info("对端地址更新为 %s:%s", host, self.port)
        self.host = host
        if self.connected:
            asyncio.ensure_future(self._reconnect_now())

    async def _reconnect_now(self) -> None:
        self._drop_writer(self._writer)
        self._drop_writer(self._inbound)

    # ---------------------------------------------------------------- 发送
    async def send(self, msg: Message) -> bool:
        writer = self.active_writer
        if writer is None:
            return False
        if await self._send_line(writer, msg):
            return True
        self._drop_writer(writer)
        return False

    async def _send_line(self, writer: Optional[asyncio.StreamWriter], msg: Message) -> bool:
        """写到指定的那条连接。

        握手报文必须按连接定向：两端互连时 ``active_writer`` 是本机主动拨出的连接，
        而对方连进来的那条连接有自己的 nonce，发错连接会导致校验串对不上。
        """
        if writer is None or writer.is_closing():
            return False
        async with self._write_lock:
            try:
                writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
                return True
            except Exception as exc:
                log.debug("发送失败：%s", exc)
                return False

    # ---------------------------------------------------------------- 连接循环
    async def _connect_loop(self) -> None:
        backoff = self.reconnect_interval
        while self._running:
            host = self.host
            if not host or host.lower() == "auto":
                await asyncio.sleep(1.0)
                continue
            try:
                reader, writer = await asyncio.open_connection(host, self.port)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("连接 %s:%s 失败：%s，%.1fs 后重试", host, self.port, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max_interval)
                continue

            backoff = self.reconnect_interval
            self._writer = writer
            self._last_rx = time.monotonic()
            log.info("已连接对端 %s:%s", host, self.port)
            self._emit("connected", host=host, port=self.port)
            auth = self._new_auth()
            await self._send_greeting(writer, auth)
            try:
                await self._read_lines(reader, writer, auth)
            except asyncio.CancelledError:
                raise
            finally:
                if self._writer is writer:
                    self._writer = None
                with contextlib.suppress(Exception):
                    writer.close()
                log.info("与 %s:%s 的连接已断开，%.1fs 后重连", host, self.port, self.reconnect_interval)
                self._emit("disconnected", host=host, port=self.port)
            if self._running:
                await asyncio.sleep(self.reconnect_interval)

    async def _ping_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.ping_interval)
            writer = self.active_writer
            if writer is None:
                continue
            stale_after = self.ping_interval * 3
            if self._last_rx and (time.monotonic() - self._last_rx) > stale_after:
                log.warning("对端超过 %.0fs 无任何消息，判定为半开连接，强制重连", stale_after)
                self._drop_writer(writer)
                continue
            await self.send({"type": "ping", "ts": time.time()})

    # ---------------------------------------------------------------- 接收
    async def _handle_inbound(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = _fmt_addr(writer.get_extra_info("peername"))
        log.info("收到来自 %s 的连接", peer)
        self._emit("inbound", peer=peer)
        previous = self._inbound
        self._inbound = writer
        if previous is not None and previous is not writer:
            self._drop_writer(previous)
        self._last_rx = time.monotonic()
        auth = self._new_auth()
        # 被连的一方也要打招呼：单向可达（对端在 NAT 后）时只有这条连接可用，
        # 不主动发质询就没法校验对方。
        await self._send_greeting(writer, auth)
        try:
            await self._read_lines(reader, writer, auth)
        finally:
            if self._inbound is writer:
                self._inbound = None
            with contextlib.suppress(Exception):
                writer.close()
            log.debug("来自 %s 的连接已关闭", peer)

    async def _read_lines(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, auth: _Auth
    ) -> None:
        while self._running:
            try:
                line = await reader.readline()
            except asyncio.CancelledError:
                raise
            except (ConnectionError, asyncio.IncompleteReadError) as exc:
                log.debug("读取中断：%s", exc)
                return
            except ValueError as exc:  # 单行超长等
                log.warning("读取异常（可能是消息过长）：%s", exc)
                return
            if not line:
                return
            self._last_rx = time.monotonic()
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                log.warning("忽略无法解析的消息：%s", text[:200])
                continue
            if not isinstance(msg, dict):
                log.warning("忽略非对象消息：%s", text[:200])
                continue
            await self._dispatch(msg, writer, auth)

    async def _dispatch(self, msg: Message, writer: asyncio.StreamWriter, auth: _Auth) -> None:
        if msg.get("type") == "hello":
            self._peer_info = dict(msg)
            self._peer_auth = str(msg["auth"]) if msg.get("auth") else None
        send = functools.partial(self._send_line, writer)
        if not await auth.absorb(msg, send):
            return
        self._emit("message", msg=msg)
        try:
            await self.on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("处理消息失败：%s", msg)

    # ---------------------------------------------------------------- 内部工具
    def _new_auth(self) -> _Auth:
        return _Auth(self.secret, on_verified=self._mark_authenticated)

    def _mark_authenticated(self) -> None:
        if self._authenticated:
            return
        self._authenticated = True
        self._emit("authenticated", peer=self._peer_info.get("name"), alg=AUTH_ALG)

    async def _send_greeting(self, writer: asyncio.StreamWriter, auth: _Auth) -> None:
        """连接建立后的第一轮：hello（身份 + 是否启用校验）与质询串。"""
        if self.identity:
            hello: Dict[str, Any] = {"type": "hello", **self.identity, "auth": auth.hello_auth()}
            await self._send_line(writer, hello)
        challenge = auth.challenge()
        if challenge is not None:
            await self._send_line(writer, challenge)

    def _drop_writer(self, writer: Optional[asyncio.StreamWriter]) -> None:
        if writer is None:
            return
        if writer is self._writer:
            self._writer = None
        if writer is self._inbound:
            self._inbound = None
        if self._writer is None and self._inbound is None:
            # 两条连接都没了，校验状态跟着失效（重连会重新质询）
            self._authenticated = False
        with contextlib.suppress(Exception):
            writer.close()
