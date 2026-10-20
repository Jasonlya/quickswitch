"""QuickSwitch 入口：托盘 + 全局热键 + DDC 切换 + 对端 TCP 通知。

常用命令：

    python main.py                  # 常驻运行（托盘图标 + 全局热键）
    python main.py --status         # 打印配置、DDC 工具、当前输入源、对端连通性
    python main.py --once mac       # 执行一次切换后退出（验证 DDC 与通知链路）
    python main.py --dry-run        # 只打印将要执行的 DDC 命令，不做真实切换
    python main.py --no-tray        # 无托盘后台运行（适合任务计划/launchd 调试）
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import logging
import logging.handlers
import platform
import re
import secrets
import signal
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import (
    ConfigError,
    LABEL,
    config_path,
    load_config,
    log_file,
    save_config,
    validate,
)
from ddc import DEFAULT_INPUT_CANDIDATES, DDCSwitcher
from discovery import DEFAULT_PORT as DISCOVERY_DEFAULT_PORT
from discovery import PeerDiscovery
from network import AUTH_ALG, PeerConnection, challenge_mac, verify_mac
from webui import EventBuffer, WebConsole
from webui import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT

VERSION = "1.1.0"
TARGETS = ("mac", "win")


def attach_parent_console() -> None:
    """Windows 的 GUI 打包（``--windowed``）没有自己的控制台。

    从 cmd / PowerShell 里跑 ``QuickSwitch.exe --status`` 时附着到父进程的控制台，
    输出才看得见；双击启动时没有父控制台，附着失败则保持无输出（日志文件照常写）。
    """
    if platform.system() != "Windows" or not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None:
        return  # 已经有控制台（例如用控制台版构建）
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except Exception:
        pass  # 无控制台可用时静默降级，界面与日志不受影响


def setup_logging(level: str = "INFO", *, console_level: Optional[str] = None,
                  log_path: Optional[Path] = None, events: Optional[EventBuffer] = None) -> None:
    target = log_path or log_file()
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, str(console_level or level).upper(), logging.INFO))
    handlers: List[logging.Handler] = [console]
    if target is not None:
        with contextlib.suppress(OSError):
            target.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.handlers.RotatingFileHandler(
                    target, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
                )
            )
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    if events is not None:
        # Web 控制台的「事件」直接读运行中的日志，不另造一份状态
        logging.getLogger().addHandler(events)


def network_kwargs(cfg: Dict[str, Any]) -> Dict[str, Any]:
    net = cfg.get("network") or {}
    return {
        "ping_interval": float(net.get("ping_interval", 10.0)),
        "reconnect_interval": float(net.get("reconnect_interval", 3.0)),
        "reconnect_max_interval": float(net.get("reconnect_max_interval", 30.0)),
        "secret": auth_secret(cfg),
    }


def auth_secret(cfg: Dict[str, Any]) -> Optional[str]:
    """预共享密钥；空字符串表示不校验（局域网内不设防）。"""
    secret = str((cfg.get("auth") or {}).get("secret") or "").strip()
    return secret or None


def discovery_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg_disc = cfg.get("discovery") or {}
    return {
        "port": int(cfg_disc.get("port") or DISCOVERY_DEFAULT_PORT),
        "interval": float(cfg_disc.get("interval") or 5.0),
        "ttl": float(cfg_disc.get("ttl") or 15.0),
        "broadcast": cfg_disc.get("broadcast", "255.255.255.255"),
    }


log = logging.getLogger("main")


class QuickSwitch:
    """切换逻辑核心：本地 DDC 切换 + 通过 TCP 通知对端冗余切换。

    与运行时（托盘/热键）解耦，构造函数允许注入 DDC 与 peer，便于测试。
    """

    def __init__(
        self,
        cfg: Dict[str, Any],
        *,
        dry_run: bool = False,
        ddc: Optional[DDCSwitcher] = None,
        peer: Optional[PeerConnection] = None,
        debounce: Optional[float] = None,
        ack_timeout: Optional[float] = None,
        name: Optional[str] = None,
    ) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.ddc = ddc or DDCSwitcher(cfg, dry_run=dry_run)
        self.peer = peer
        self.discovery: Optional[PeerDiscovery] = None
        self.name = name or f"{socket.gethostname()}-{cfg.get('role', 'server')}"
        net = cfg.get("network") or {}
        self.ack_timeout = float(
            ack_timeout if ack_timeout is not None else net.get("ack_timeout", 4.0)
        )
        self.debounce = float(cfg.get("debounce", 0.6) if debounce is None else debounce)
        self.state: Optional[str] = None
        self.lock = asyncio.Lock()
        self.peer_info: Dict[str, Any] = {}
        self._pending: Dict[str, asyncio.Future] = {}
        self._last_trigger = 0.0
        self._last_target: Optional[str] = None

    # ---------------------------------------------------------------- 辅助
    def set_peer(self, peer: Optional[PeerConnection]) -> None:
        self.peer = peer

    def identity(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "role": self.cfg.get("role"),
            "version": VERSION,
            "platform": platform.system(),
            "listen_port": self.cfg.get("listen_port"),
        }

    def status_line(self) -> str:
        if self.peer is None:
            peer_state = "无对端连接"
        else:
            link = "已连接" if self.peer.connected else "未连接"
            peer_state = f"{self.peer.host}:{self.peer.port} {link}"
        return (
            f"role={self.cfg.get('role')} "
            f"当前目标={LABEL.get(self.state or '', '未知')} "
            f"对端={peer_state}"
        )

    # ---------------------------------------------------------------- 核心流程
    async def trigger(self, target: str, source: str = "hotkey") -> bool:
        """触发一次切换：先本地 DDC，再通知对端（冗余保险）。"""
        if target not in TARGETS:
            raise ValueError(f"未知切换目标：{target!r}")
        now = time.monotonic()
        if (
            self.debounce
            and self._last_target == target
            and (now - self._last_trigger) < self.debounce
        ):
            log.debug("忽略 %.2fs 内的重复触发 → %s", now - self._last_trigger, LABEL[target])
            return True
        self._last_trigger = now
        self._last_target = target
        async with self.lock:
            return await self._do_switch(target, source=source, notify=True)

    async def _do_switch(self, target: str, *, source: str, notify: bool) -> bool:
        log.info("切换请求：→ %s（来源：%s）", LABEL[target], source)
        local_ok = await self._local_switch(target)
        if local_ok:
            self.state = target

        if not notify or self.peer is None:
            return local_ok

        remote_ok = await self._notify_peer(target)
        if remote_ok is None:
            log.info(
                "本地 DDC %s；对端未确认（对端程序可能未运行或未连接）",
                "成功" if local_ok else "失败",
            )
        else:
            log.info(
                "本地 DDC %s；对端 DDC %s",
                "成功" if local_ok else "失败",
                "成功" if remote_ok else "失败",
            )
        return local_ok or remote_ok is True

    async def _local_switch(self, target: str) -> bool:
        fn = self.ddc.switch_to_mac if target == "mac" else self.ddc.switch_to_win
        ok = await asyncio.to_thread(fn)
        log.info("本地 DDC → %s：%s", LABEL[target], "成功" if ok else "失败")
        return ok

    async def _notify_peer(self, target: str) -> Optional[bool]:
        """通知对端切换，返回对端确认结果；None 表示未连接或未确认。"""
        if self.peer is None or not getattr(self.peer, "connected", False):
            log.info("对端未连接，跳过 TCP 通知")
            return None
        if getattr(self.peer, "peer_auth", None) and not getattr(self.peer, "authenticated", False):
            log.warning(
                "对端要求密钥校验，但本端未通过校验（两端 auth.secret 可能不一致）；"
                "本次只做本地 DDC 切换，不发送会立即被丢弃的通知"
            )
            return None

        msg_id = uuid.uuid4().hex[:8]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            sent = await self.peer.send(
                {"type": "switch", "target": target, "id": msg_id, "source": self.name}
            )
            if not sent:
                log.info("TCP 通知发送失败（连接刚断开），跳过")
                return None
            ack = await asyncio.wait_for(future, self.ack_timeout)
            return bool(ack.get("ok"))
        except asyncio.TimeoutError:
            log.warning("对端 %.0fs 内未确认切换（id=%s）", self.ack_timeout, msg_id)
            return None
        finally:
            self._pending.pop(msg_id, None)

    # ---------------------------------------------------------------- 消息处理
    async def handle_message(self, msg: Dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "ping":
            if self.peer is not None:
                await self.peer.send({"type": "pong", "ts": msg.get("ts")})
            return
        if mtype == "pong":
            return
        if mtype == "hello":
            self.peer_info = dict(msg)
            log.info(
                "对端信息：%s（role=%s，平台=%s，版本=%s）",
                msg.get("name"),
                msg.get("role"),
                msg.get("platform"),
                msg.get("version"),
            )
            return
        if mtype == "switch":
            target = msg.get("target")
            if target not in TARGETS:
                log.warning("收到未知切换目标：%r", target)
                await self._send_ack(msg, ok=False, detail=f"unknown target {target!r}")
                return
            log.info("收到对端切换请求：→ %s", LABEL[target])
            async with self.lock:
                ok = await self._do_switch(target, source="peer", notify=False)
            await self._send_ack(msg, ok=ok, detail="ddc ok" if ok else "ddc failed")
            return
        if mtype == "switch_ack":
            future = self._pending.get(msg.get("id"))
            if future is not None and not future.done():
                future.set_result(msg)
            return
        log.debug("忽略未知消息类型：%s", mtype)

    async def _send_ack(self, msg: Dict[str, Any], *, ok: bool, detail: str) -> None:
        if self.peer is None or not msg.get("id"):
            return
        await self.peer.send(
            {
                "type": "switch_ack",
                "id": msg.get("id"),
                "ok": ok,
                "detail": detail,
                "name": self.name,
            }
        )


def tray_image(target: Optional[str] = None, size: int = 64):
    """生成托盘图标（两台显示器 + 中间的切换箭头）。"""
    from PIL import Image, ImageDraw

    accent = {"mac": (150, 90, 220), "win": (25, 110, 210)}.get(target or "", (110, 118, 130))
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    scale = size / 64

    def box(coords):
        return [round(value * scale) for value in coords]

    draw.rounded_rectangle(box([4, 18, 26, 40]), radius=3 * scale, fill=(240, 242, 245, 255),
                           outline=accent + (255,), width=max(1, round(2 * scale)))
    draw.rectangle(box([13, 40, 17, 45]), fill=(240, 242, 245, 255))
    draw.rectangle(box([7, 45, 23, 47]), fill=(240, 242, 245, 255))

    draw.rounded_rectangle(box([38, 18, 60, 40]), radius=3 * scale, fill=(240, 242, 245, 255),
                           outline=accent + (255,), width=max(1, round(2 * scale)))
    draw.rectangle(box([47, 40, 51, 45]), fill=(240, 242, 245, 255))
    draw.rectangle(box([41, 45, 57, 47]), fill=(240, 242, 245, 255))

    draw.line(box([26, 29, 38, 29]), fill=accent + (255,), width=max(2, round(3 * scale)))
    draw.polygon(
        [tuple(box([38, 25])), tuple(box([45, 29])), tuple(box([38, 33]))],
        fill=accent + (255,),
    )
    return img


class QuickSwitchApp:
    """常驻运行时：独立线程跑 asyncio 事件循环，主线程跑托盘。"""

    def __init__(
        self,
        cfg: Dict[str, Any],
        *,
        dry_run: bool = False,
        tray: bool = True,
        config_file: Optional[Path] = None,
        events: Optional[EventBuffer] = None,
        web: Optional[bool] = None,
    ) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.use_tray = bool(tray and cfg.get("tray", True))
        self.version = VERSION
        self.hostname = socket.gethostname()
        self.platform = platform.system()
        self.config_file = Path(config_file) if config_file else config_path(None)
        self.log_path = log_file()
        self.events = events
        self.loop = asyncio.new_event_loop()
        # 把事件循环设为当前线程的循环：QuickSwitch 里的 asyncio.Lock 等在 Python 3.9
        # 会绑定「当前循环」，绑定错误会让后续 await 落到别的循环上。
        asyncio.set_event_loop(self.loop)
        self.quick = QuickSwitch(
            cfg,
            dry_run=dry_run,
            name=f"{socket.gethostname()}-{cfg.get('role', 'server')}",
        )
        self.peer = PeerConnection(
            str(cfg.get("peer_host") or ""),
            int(cfg.get("peer_port")),
            int(cfg.get("listen_port")),
            self.quick.handle_message,
            name=self.quick.name,
            identity=self.quick.identity(),
            on_event=self._on_event,
            **network_kwargs(cfg),
        )
        self.quick.set_peer(self.peer)
        self.discovery: Optional[PeerDiscovery] = None
        self.icon = None
        self._keyboard = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self.hotkeys_registered = False
        self.hotkeys_reason = "尚未尝试注册热键"

        web_cfg = cfg.get("web") or {}
        self.web_enabled = bool(web_cfg.get("enabled", True)) if web is None else bool(web)
        self.web: Optional[WebConsole] = None
        self._input_lock = threading.Lock()
        self._input_busy = threading.Semaphore(1)
        self._input_cache: Dict[str, Any] = {
            "value": None, "target": None, "at": None, "error": None,
        }

    # ---------------------------------------------------------------- 事件
    def _on_event(self, event: str, **fields: Any) -> None:
        if event == "connected":
            log.info("对端已连接：%s:%s", fields.get("host"), fields.get("port"))
        elif event == "authenticated":
            log.info("对端密钥校验通过：%s（%s）", fields.get("peer"), fields.get("alg"))
        elif event == "disconnected":
            log.warning(
                "对端连接断开：%s:%s（会自动重连）", fields.get("host"), fields.get("port")
            )
        elif event == "inbound":
            log.info("接受来自 %s 的连接", fields.get("peer"))
        elif event == "message":
            msg = fields.get("msg") or {}
            if msg.get("type") not in ("ping", "pong"):
                log.debug("收到对端消息：%s", msg)

    def _on_peer_found(self, peer: Dict[str, Any]) -> None:
        self.peer.set_host(peer["host"], peer["port"])

    # ---------------------------------------------------------------- 输入源缓存
    def read_cache(self) -> Dict[str, Any]:
        """最近一次「读取当前输入源」的结果；不发起 DDC 调用，可安全用于状态接口。"""
        with self._input_lock:
            return dict(self._input_cache)

    def read_input(self) -> Optional[int]:
        """同步读取当前输入源并刷新缓存（阻塞，最长 ddc.timeout 乘以重试次数）。"""
        with self._input_busy:
            value = self.quick.ddc.get_input()
        self._store_input(value)
        return value

    def target_for_input(self, value: Optional[int]) -> Optional[str]:
        ddc = self.quick.ddc
        if value is None:
            return None
        if value == ddc.mac_input:
            return "mac"
        if value == ddc.win_input:
            return "win"
        return None

    def probe_inputs(self, candidates: Optional[List[int]] = None) -> List[int]:
        """逐个试探候选输入源（同步、耗时较长），结束后恢复原输入源。"""
        found = self.quick.ddc.probe_inputs(candidates)
        self.refresh_input_async()
        return found

    def refresh_input_async(self) -> None:
        """后台回读输入源，避免把 HTTP 请求阻塞在 DDC 命令上。"""
        threading.Thread(
            target=self._refresh_input_once, name="quickswitch-read", daemon=True
        ).start()

    def _refresh_input_once(self) -> None:
        if not self._input_busy.acquire(blocking=False):
            return  # 已有一次回读在跑，避免探测/切换后排队堆积
        try:
            value = self.quick.ddc.get_input()
        except Exception as exc:  # DDC 异常不该让后台线程静默死掉
            log.debug("后台回读输入源失败：%s", exc)
            self._store_input(None, error=str(exc))
            return
        finally:
            self._input_busy.release()
        self._store_input(value)

    def _store_input(self, value: Optional[int], error: Optional[str] = None) -> None:
        with self._input_lock:
            self._input_cache = {
                "value": value,
                "target": self.target_for_input(value),
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "error": error,
            }

    # ---------------------------------------------------------------- 启停
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _startup(self) -> None:
        await self.peer.start()  # 端口被占用会抛 OSError
        # 启动时后台回读一次输入源：Web 控制台一打开就能显示显示器当前归谁
        self.refresh_input_async()
        cfg_disc = self.cfg.get("discovery") or {}
        auto = str(self.cfg.get("peer_host") or "").strip().lower() in ("", "auto")
        if cfg_disc.get("enabled") or auto:
            self.discovery = PeerDiscovery(
                name=self.quick.name,
                listen_port=int(self.cfg.get("listen_port")),
                role=str(self.cfg.get("role")),
                on_peer=self._on_peer_found,
                **discovery_config(self.cfg),
            )
            try:
                await self.discovery.start()
            except OSError as exc:
                log.warning("局域网发现启动失败（udp/%s）：%s", cfg_disc.get("port"), exc)
                self.discovery = None
        else:
            log.info("对端地址固定为 %s:%s", self.peer.host, self.peer.port)

    async def _shutdown_async(self) -> None:
        if self.discovery is not None:
            await self.discovery.stop()
        await self.peer.stop()

    def shutdown(self) -> None:
        """停止一切资源；应在主线程调用（托盘与信号处理都在主线程）。"""
        if self._stopping:
            return
        self._stopping = True
        log.info("正在退出…")
        if self.web is not None:
            with contextlib.suppress(Exception):
                self.web.stop()
            self.web = None
        if self._keyboard is not None:
            with contextlib.suppress(Exception):
                self._keyboard.unhook_all_hotkeys()
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._shutdown_async(), self.loop).result(timeout=5)
        with contextlib.suppress(Exception):
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        log.info("已退出")

    def run(self) -> int:
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="quickswitch-async")
        self._thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._startup(), self.loop).result(timeout=20)
        except OSError as exc:
            log.error(
                "启动失败：监听端口 %s 不可用（%s）。\n"
                "  通常表示已有一个 QuickSwitch 实例在运行，或端口被别的程序占用。",
                self.cfg.get("listen_port"),
                exc,
            )
            self.shutdown()
            return 3
        except Exception as exc:
            log.error("启动失败：%s", exc)
            self.shutdown()
            return 4

        self._install_signal_handlers()
        self._setup_hotkeys()
        if self.web_enabled:
            self._start_web()
        tool_ok, tool_detail = self.quick.ddc.tool_available()
        log.info("DDC 工具：%s", tool_detail if tool_ok else f"不可用 — {tool_detail}")
        log.info("已启动：%s", self.quick.status_line())

        if not self.use_tray:
            log.info("未启用托盘，按 Ctrl+C 退出")
            try:
                while self._thread.is_alive():
                    self._thread.join(timeout=1.0)
            except KeyboardInterrupt:
                log.info("收到 Ctrl+C")
            self.shutdown()
            return 0

        try:
            self._run_tray()
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            log.error("托盘启动失败：%s", exc)
            log.error(
                "已降级为无托盘后台运行（全局热键仍可用）；"
                "修复依赖：pip install pystray Pillow"
            )
            try:
                while self._thread.is_alive():
                    self._thread.join(timeout=1.0)
            except KeyboardInterrupt:
                log.info("收到 Ctrl+C")
        finally:
            self.shutdown()
        return 0

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(Exception):
                signal.signal(sig, self._on_signal)

    def _on_signal(self, signum, frame) -> None:
        log.info("收到信号 %s，准备退出", signum)
        if self.icon is not None:
            with contextlib.suppress(Exception):
                self.icon.stop()
        else:
            threading.Thread(target=self.shutdown, daemon=True).start()

    # ---------------------------------------------------------------- Web 控制台
    def _start_web(self) -> None:
        web_cfg = self.cfg.get("web") or {}
        host = str(web_cfg.get("host") or DEFAULT_WEB_HOST)
        port = int(web_cfg.get("port") or DEFAULT_WEB_PORT)
        console = WebConsole(self, host=host, port=port, events=self.events)
        try:
            console.start()
        except OSError as exc:
            log.error(
                "Web 控制台启动失败（%s:%s）：%s。切换功能不受影响，"
                "可用 --no-web 跳过，或改配置 web.port。",
                host, port, exc,
            )
            self.web = None
            return
        self.web = console
        if not self.use_tray:
            # 无托盘时把带令牌的地址打到控制台，直接点开就能用
            print(f"Web 控制台：{console.url}", file=sys.stderr, flush=True)

    # ---------------------------------------------------------------- 热键
    def _setup_hotkeys(self) -> None:
        if str(self.cfg.get("role")) != "server":
            self.hotkeys_reason = "client 模式不在本机注册热键（热键在 Windows 端注册）"
            log.info("client 模式：不在本机注册热键（热键在 Windows 端注册）")
            return
        try:
            import keyboard  # 仅 Windows server 需要
        except Exception as exc:
            self.hotkeys_reason = f"未安装 keyboard 库：{exc}"
            log.warning(
                "未安装 keyboard 库（%s），跳过全局热键；可先用托盘菜单切换。\n"
                "  Windows 上安装：pip install keyboard，并以管理员身份运行。",
                exc,
            )
            return
        self._keyboard = keyboard
        hotkeys = self.cfg.get("hotkeys") or {}
        registered: List[str] = []
        failures: List[str] = []
        for field, target in (("switch_to_mac", "mac"), ("switch_to_win", "win")):
            combo = str(hotkeys.get(field) or "").strip()
            if not combo:
                continue
            try:
                # suppress=False：Deskflow 也要能收到同一热键来切换键鼠控制权
                keyboard.add_hotkey(
                    combo, functools.partial(self.trigger, target, "hotkey"), suppress=False
                )
                registered.append(f"{combo} → {LABEL[target]}")
                log.info("已注册热键 %s → %s", combo, LABEL[target])
            except Exception as exc:
                failures.append(f"{combo}（{exc}）")
                log.error("注册热键 %s 失败：%s（通常需要管理员权限）", combo, exc)
        self.hotkeys_registered = bool(registered)
        if registered and not failures:
            self.hotkeys_reason = "已注册：" + "、".join(registered)
        elif registered:
            self.hotkeys_reason = (
                "部分注册：" + "、".join(registered) + "；失败：" + "、".join(failures)
            )
        elif failures:
            self.hotkeys_reason = "注册失败：" + "、".join(failures)
        else:
            self.hotkeys_reason = "配置中的热键为空，未注册任何热键"

    # ---------------------------------------------------------------- 托盘
    def trigger(self, target: str, source: str = "manual"):
        """从任意线程触发切换，返回 concurrent.futures.Future。"""
        future = asyncio.run_coroutine_threadsafe(self.quick.trigger(target, source), self.loop)

        def _done(done_future) -> None:
            try:
                ok = done_future.result()
            except Exception as exc:
                log.error("切换任务异常：%s", exc)
                return
            if not ok:
                log.warning("切换到 %s 未完全成功（详见上方日志）", LABEL[target])

        future.add_done_callback(_done)
        return future

    def _tray_switch(self, target: str):
        def handler(icon, item) -> None:
            self.trigger(target, "tray")

        return handler

    def _tray_status(self, icon, item) -> None:
        log.info("状态：%s", self.quick.status_line())
        if self.quick.ddc.dry_run:
            log.info("当前为 dry-run 模式，不会真正切换显示器")

    def _tray_quit(self, icon, item) -> None:
        icon.stop()

    def _run_tray(self) -> None:
        import pystray

        def state_text(item) -> str:
            link = "已连接" if self.peer.connected else "未连接"
            return f"当前：{LABEL.get(self.quick.state or '', '未知')}｜对端{link}"

        menu = pystray.Menu(
            pystray.MenuItem(state_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("切到 Mac", self._tray_switch("mac")),
            pystray.MenuItem("切到 Windows", self._tray_switch("win")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("查看状态（写入日志）", self._tray_status),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        self.icon = pystray.Icon("QuickSwitch", tray_image(), "QuickSwitch", menu)
        log.info("托盘图标已启动（右键菜单可手动切换）")
        self.icon.run()
        self.icon = None


# ---------------------------------------------------------------- 命令行子命令
async def resolve_peer(cfg: Dict[str, Any], *, timeout: float = 5.0) -> Optional[Tuple[str, int]]:
    """解析对端地址；``peer_host`` 为 "auto" 时通过局域网广播发现。"""
    host = str(cfg.get("peer_host") or "").strip()
    if host and host.lower() != "auto":
        return host, int(cfg.get("peer_port"))

    discovery = PeerDiscovery(
        name=f"{socket.gethostname()}-{cfg.get('role')}",
        listen_port=int(cfg.get("listen_port")),
        role=str(cfg.get("role")),
        **discovery_config(cfg),
    )
    try:
        await discovery.start()
    except OSError as exc:
        log.warning("局域网发现启动失败：%s", exc)
        return None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            peer = discovery.best_peer(exclude=[discovery.name])
            if peer:
                return peer["host"], int(peer["port"])
            await asyncio.sleep(0.2)
        return None
    finally:
        await discovery.stop()


async def probe_peer(cfg: Dict[str, Any], target: Optional[Tuple[str, int]]) -> Tuple[bool, str, Dict[str, Any]]:
    """连接对端、交换 hello 并走一次密钥质询，用于 --status。

    返回值里的 ``auth`` 字段说明校验结果：
    ``None`` = 本机未配置密钥所以没校验；``True`` = 已通过；``False`` = 未通过
    （两端 ``auth.secret`` 不一致，或对端版本不支持）。
    """
    if not target:
        return False, f"未解析到对端地址（peer_host={cfg.get('peer_host')!r}）", {}
    host, port = target
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
    except (OSError, asyncio.TimeoutError) as exc:
        return False, f"{host}:{port} 连接失败（{exc.__class__.__name__}: {exc}）", {}
    try:
        secret = auth_secret(cfg)
        nonce = secrets.token_hex(16) if secret else None
        hello = {
            "type": "hello",
            "name": f"{socket.gethostname()}-{cfg.get('role')}",
            "role": cfg.get("role"),
            "version": VERSION,
            "platform": platform.system(),
            "listen_port": cfg.get("listen_port"),
            "auth": AUTH_ALG if secret else None,
        }
        lines = [json.dumps(hello, ensure_ascii=False)]
        if nonce:
            lines.append(json.dumps({"type": "auth_challenge", "nonce": nonce, "alg": AUTH_ALG}))
        writer.write(("\n".join(lines) + "\n").encode("utf-8"))
        await writer.drain()
        info: Dict[str, Any] = {}
        peer_nonce: Optional[str] = None
        verified: Optional[bool] = None
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            with contextlib.suppress(asyncio.TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
                line = await asyncio.wait_for(reader.readline(), timeout=max(0.1, deadline - time.monotonic()))
                if not line:
                    break
                msg = json.loads(line.decode("utf-8", errors="replace"))
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if mtype == "hello":
                    info = msg
                elif mtype == "auth_challenge" and nonce:
                    peer_nonce = str(msg.get("nonce") or "")
                    response = {
                        "type": "auth_response",
                        "nonce": peer_nonce,
                        "mine": nonce,
                        "alg": AUTH_ALG,
                        "mac": challenge_mac(secret or "", peer_nonce, nonce),
                    }
                    writer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                    await writer.drain()
                elif mtype == "auth_response" and nonce:
                    own = str(msg.get("mine") or "")
                    verified = bool(
                        str(msg.get("nonce") or "") == nonce
                        and (not peer_nonce or own == peer_nonce)
                        and verify_mac(secret or "", nonce, own, str(msg.get("mac") or ""))
                    )
                    break
                elif mtype in ("auth_response", "auth_challenge"):
                    continue
            if verified is not None:
                break
        if info:
            info["auth_verified"] = verified
            return True, f"{host}:{port} 已连接（对端 {info.get('name')}，role={info.get('role')}，版本={info.get('version')}）", info
        return True, f"{host}:{port} 已连接（未收到对端 hello，对端可能未运行 QuickSwitch）", {"auth_verified": verified}
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def cmd_status(cfg: Dict[str, Any], path: Path) -> int:
    ddc = DDCSwitcher(cfg, timeout=3.0, retries=0)
    tool_ok, tool_detail = ddc.tool_available()

    value = ddc.get_input() if tool_ok else None
    if value is None:
        current = (
            "无法读取（显示器不支持读 VCP 0x60、工具不可用或未安装；"
            "可用 ddc.read_template / ddc.read_regex 指定读取方式）"
        )
    else:
        if value == ddc.mac_input:
            current = f"{value} → Mac"
        elif value == ddc.win_input:
            current = f"{value} → Windows"
        else:
            current = f"{value} → 未在配置中登记"

    target = await resolve_peer(cfg, timeout=4.0)
    connected, peer_line, info = await probe_peer(cfg, target)
    secret = auth_secret(cfg)
    verified = info.get("auth_verified")
    if not secret:
        auth_line = "未配置 auth.secret —— 不校验，同网段任何程序都能发切换请求"
        if info.get("auth"):
            auth_line += "；对端已启用校验，本端发出的通知会被对端丢弃"
    elif verified is True:
        auth_line = f"已通过（{AUTH_ALG}）"
    elif verified is False:
        auth_line = "未通过 —— 两端 auth.secret 不一致，通知会被对端丢弃"
    else:
        auth_line = "未完成 —— 对端没有回应质询（对端可能是旧版本或未运行）"

    disc = cfg.get("discovery") or {}
    auto = str(cfg.get("peer_host") or "").strip().lower() in ("", "auto")
    disc_line = (
        f"已启用（udp/{disc.get('port') or DISCOVERY_DEFAULT_PORT}）"
        if (disc.get("enabled") or auto)
        else "未启用"
    )
    hotkeys = cfg.get("hotkeys") or {}

    print(f"QuickSwitch {VERSION} 状态")
    print(f"  role         : {cfg.get('role')}")
    print(f"  配置文件     : {path}{'' if path.exists() else '（不存在，使用默认值）'}")
    print(f"  监听端口     : 0.0.0.0:{cfg.get('listen_port')}")
    print(f"  对端         : {peer_line}")
    print(f"  密钥校验     : {auth_line}")
    print(f"  DDC 工具     : {tool_detail}" + ("" if tool_ok else "  ← 请检查配置 ddc 路径"))
    if tool_ok:
        with contextlib.suppress(Exception):
            read_cmd = ddc.build_command(0, get=True)
            if read_cmd:
                print(f"  读取命令     : {' '.join(read_cmd)}")
    print(f"  当前输入源   : {current}")
    if str(cfg.get("role")) == "server":
        print(
            f"  热键         : {hotkeys.get('switch_to_mac')} → Mac ｜ "
            f"{hotkeys.get('switch_to_win')} → Windows"
        )
    print(f"  局域网发现   : {disc_line}")
    print(f"  日志文件     : {log_file()}")
    return 0


async def run_once(
    cfg: Dict[str, Any], target: str, *, dry_run: bool = False, wait: Optional[float] = None
) -> bool:
    """执行一次完整切换流程后退出，用于验证 DDC 与通知链路。"""
    if wait is None:
        wait = float((cfg.get("network") or {}).get("connect_wait", 4.0))
    quick = QuickSwitch(
        cfg,
        dry_run=dry_run,
        name=f"{socket.gethostname()}-{cfg.get('role', 'server')}",
    )
    peer = PeerConnection(
        str(cfg.get("peer_host") or ""),
        int(cfg.get("peer_port")),
        int(cfg.get("listen_port")),
        quick.handle_message,
        name=quick.name,
        identity=quick.identity(),
        **network_kwargs(cfg),
    )
    quick.set_peer(peer)
    started = False
    try:
        await peer.start()
        started = True
    except OSError as exc:
        log.warning(
            "本机监听端口 %s 不可用（%s），本次仅执行本地 DDC 切换",
            cfg.get("listen_port"),
            exc,
        )
        quick.set_peer(None)

    if started:
        address = await resolve_peer(cfg, timeout=wait)
        if address:
            peer.set_host(*address)
            if await peer.wait_connected(min(wait, 5.0)):
                log.info("已连接对端 %s:%s", address[0], address[1])
            else:
                log.info(
                    "对端 %s:%s 未在 %.0fs 内连接，仅执行本地切换",
                    address[0],
                    address[1],
                    wait,
                )
        else:
            log.info("未解析到对端地址（peer_host=%r），仅执行本地切换", cfg.get("peer_host"))

    try:
        return await quick.trigger(target, source="cli")
    finally:
        if started:
            await peer.stop()


def parse_candidates(text: Optional[str]) -> Optional[List[int]]:
    """解析 ``--candidates 15,17``（支持 0x0f 写法）。"""
    if not text:
        return None
    values: List[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        value = int(part, 0)
        if not 0 <= value <= 255:
            raise ValueError(f"输入源值必须在 0-255 之间：{part}")
        values.append(value)
    return values or None


def make_prompt(interactive: bool):
    """交互模式返回 input；非交互模式返回固定答复，避免脚本里卡住。"""
    if interactive:
        def ask(question: str) -> str:
            try:
                return input(question)
            except (EOFError, KeyboardInterrupt):
                print()
                return ""

        return ask

    def skip(question: str) -> str:
        print(f"{question}（非交互模式：跳过）")
        return ""

    return skip


def ask_input_value(prompt, label: str, available: List[int], default: Optional[int]) -> Optional[int]:
    hint = f"，可选：{available}" if available else ""
    suffix = f"（回车用 {default}）" if default is not None else ""
    while True:
        answer = prompt(f"  请输入 {label} 对应的输入源值{hint}{suffix}：").strip()
        if not answer:
            return default
        try:
            value = int(answer, 0)
        except ValueError:
            print("  请输入 0-255 之间的数字，例如 15。")
            continue
        if not 0 <= value <= 255:
            print("  请输入 0-255 之间的数字，例如 15。")
            continue
        if available and value not in available:
            print(f"  提示：{value} 不在探测到的可用取值 {available} 中，稍后回读校验会确认。")
        return value


def cmd_detect_inputs(
    cfg: Dict[str, Any],
    path: Path,
    *,
    mac_input: Optional[int] = None,
    win_input: Optional[int] = None,
    candidates: Optional[List[int]] = None,
    probe: bool = True,
    interactive: bool = True,
    write: bool = True,
    dry_run: bool = False,
    ddc: Optional[DDCSwitcher] = None,
    prompt=None,
) -> int:
    """识别显示器输入源取值并写回配置。

    流程：读取当前值 → 逐一试探候选取值（set 后回读确认）→ 交互确认哪个值对应哪台机器
    → 回读校验 → 写入 config.json，并打印需要同步到另一端的片段。

    需要「既能写也能读」的 DDC 工具；只能写不能读时，请直接用
    ``--mac-input`` / ``--win-input`` 指定取值。
    """
    switcher = ddc or DDCSwitcher(cfg, dry_run=dry_run)
    if interactive and prompt is None:
        prompt = make_prompt(True)
    elif prompt is None:
        prompt = make_prompt(False)
    values = list(candidates or DEFAULT_INPUT_CANDIDATES)

    print("QuickSwitch 输入源识别向导")
    tool_ok, tool_detail = switcher.tool_available()
    print(f"  DDC 工具   : {tool_detail}")
    if not tool_ok:
        if mac_input is None or win_input is None:
            print("  DDC 工具不可用，无法切换/回读；请先按 README §8.1 手动确认取值，")
            print("  再用 --mac-input / --win-input 指定（可只写配置，稍后再校准）。")
            return 1
        print("  警告：DDC 工具不可用，本次只写配置，跳过探测与回读校验。")
        probe = False

    current = switcher.get_input()
    if current is None:
        print("  当前输入源 : 读取失败")
        print("    可用读取方式：ControlMyMonitor /GetValue、m1ddc get input、ddcutil getvcp 60；")
        print("    其他工具可配置 ddc.read_template（+ ddc.read_regex）指定读取命令。")
    else:
        print(f"  当前输入源 : {current}")

    available: List[int] = []
    if dry_run:
        probe = False
        print("  dry-run 模式：跳过探测与回读")
    if probe and current is not None:
        print(f"  正在试探候选输入源 {values}（屏幕会短暂切换，结束后恢复原输入源）…")

        def progress(value: int, read_back: Optional[int]) -> None:
            if read_back is None:
                print(f"    {value:>3} : 设置失败或显示器未接受")
            elif read_back == value:
                print(f"    {value:>3} : 可用（回读 {read_back}）")
            else:
                print(f"    {value:>3} : 忽略（回读 {read_back}）")

        available = switcher.probe_inputs(values, progress=progress)
        if available:
            print(f"  探测结果   : {available}")
        else:
            print("  探测结果   : 未发现可用取值")
    elif not probe:
        print("  已跳过探测（--no-probe / dry-run）")
    else:
        print("  无法探测：需要能读取当前输入源")

    labels: Dict[str, int] = {}
    if mac_input is not None:
        labels["mac"] = mac_input
    if win_input is not None:
        labels["win"] = win_input

    for value in available:
        if value in labels.values():
            continue
        if not switcher.set_input(value):
            continue
        time.sleep(switcher.settle)
        answer = ""
        for _ in range(3):
            answer = prompt(
                f"  切到输入源 {value} 时，显示器上是："
                "[1] Windows 桌面  [2] Mac 桌面  [3] 黑屏/无信号  [4] 跳过："
            ).strip()
            if answer[:1] in ("1", "2", "3", "4"):
                break
            print("  请输入 1-4（1=Windows 桌面，2=Mac 桌面，3=黑屏/无信号，4=跳过）")
        if answer.startswith("1"):
            labels.setdefault("win", value)
        elif answer.startswith("2"):
            labels.setdefault("mac", value)
        if current is not None:
            switcher.set_input(current)
            time.sleep(switcher.settle)

    for target, label in (("win", "Windows"), ("mac", "Mac")):
        if labels.get(target) is None:
            default = next((value for value in available if value not in labels.values()), None)
            answer = ask_input_value(prompt, label, available, default)
            if answer is not None:
                labels[target] = answer

    if labels.get("mac") is None or labels.get("win") is None:
        print("\n未能确定 Mac / Windows 对应的输入源值。")
        print("请手动指定后重试，例如：python main.py --detect-inputs --mac-input 15 --win-input 17")
        return 1
    if labels["mac"] == labels["win"]:
        print(f"\nMac 与 Windows 的输入源值相同（{labels['mac']}），无法切换；请检查后重试。")
        return 1

    print(f"\n  结果       : Mac = {labels['mac']}，Windows = {labels['win']}")
    mismatch = False
    if tool_ok:
        print("\n回读校验：")
        for target, label in (("mac", "Mac"), ("win", "Windows")):
            value = labels[target]
            sent = switcher.set_input(value)
            time.sleep(switcher.settle)
            read_back = switcher.get_input()
            if not sent:
                mismatch = True
                print(f"  {label:<8}: 切换命令执行失败，请检查 DDC 工具、线材与显示器 DDC 开关")
            elif read_back is None:
                print(f"  {label:<8}: 已发送切换（{value}），本环境无法回读确认")
            elif read_back == value:
                print(f"  {label:<8}: 切换成功（回读 {read_back}）")
            else:
                mismatch = True
                print(f"  {label:<8}: 回读 {read_back} ≠ {value}，该取值可能不正确")
    else:
        print("\n跳过回读校验（DDC 工具不可用）")

    if current is not None and current not in (labels["mac"], labels["win"]):
        switcher.set_input(current)
        print(f"  已恢复原始输入源 {current}")

    if write:
        ddc_cfg = cfg.setdefault("ddc", {})
        ddc_cfg["mac_input"] = labels["mac"]
        ddc_cfg["win_input"] = labels["win"]
        saved = save_config(cfg, str(path))
        print(f"\n已更新配置：{saved}")
    else:
        print("\n未写入配置（--no-write）")

    snippet = json.dumps(
        {"ddc": {"mac_input": labels["mac"], "win_input": labels["win"]}}, ensure_ascii=False
    )
    print("把下面这段同步到另一端的 config.json（同一台显示器的输入源取值相同）：")
    print(f"  {snippet}")
    print("\n下一步：")
    print("  1) 确认 Deskflow 与本机热键一致（默认 ctrl+alt+1 → Mac，ctrl+alt+2 → Windows）")
    print("  2) python main.py --status 查看当前输入源与对端连通性")
    print("  3) 按 Ctrl+Alt+1 / Ctrl+Alt+2 做最终验收")
    if mismatch:
        print("\n注意：回读校验存在不一致项，请确认取值后重新运行。")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quickswitch",
        description="一键切换显示器输入源（DDC/CI）并对端冗余通知；Windows server + macOS client。",
    )
    parser.add_argument("--config", help="配置文件路径（默认 ~/.quickswitch/config.json）")
    parser.add_argument("--role", choices=("server", "client"), help="覆盖配置中的 role")
    parser.add_argument("--once", choices=TARGETS, metavar="{mac,win}",
                        help="执行一次切换后退出（验证 DDC 与对端通知）")
    parser.add_argument("--status", action="store_true", help="打印配置、DDC 工具与对端连通性后退出")
    parser.add_argument("--print-config", action="store_true", help="打印合并默认值后的生效配置")
    parser.add_argument("--init-config", action="store_true", help="生成默认配置文件后退出")
    parser.add_argument("--gen-secret", action="store_true",
                        help="生成一个预共享密钥（写进两端配置的 auth.secret）后退出")
    parser.add_argument("--detect-inputs", action="store_true",
                        help="识别显示器输入源取值（Windows/macOS/Linux）并写回配置")
    parser.add_argument("--mac-input", type=int, help="配合 --detect-inputs：直接指定 Mac 的输入源值")
    parser.add_argument("--win-input", type=int, help="配合 --detect-inputs：直接指定 Windows 的输入源值")
    parser.add_argument("--candidates", help="配合 --detect-inputs：候选取值，逗号分隔（默认 1,3,15,16,17,18,27）")
    parser.add_argument("--no-probe", action="store_true", help="配合 --detect-inputs：跳过候选值探测")
    parser.add_argument("--no-write", action="store_true", help="配合 --detect-inputs：只打印结果，不写配置")
    parser.add_argument("--non-interactive", action="store_true",
                        help="配合 --detect-inputs：不提问，只用命令行给出的取值")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的 DDC 命令，不做真实切换")
    parser.add_argument("--no-tray", action="store_true", help="不显示托盘图标（后台运行）")
    parser.add_argument("--no-web", action="store_true", help="不启动本地 Web 控制台（默认 http://127.0.0.1:8767/）")
    parser.add_argument("--web-port", type=int, help="Web 控制台端口（默认取配置 web.port，8767）")
    parser.add_argument("--log-level", help="日志级别：DEBUG/INFO/WARNING/ERROR")
    parser.add_argument("--version", action="version", version=f"QuickSwitch {VERSION}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    attach_parent_console()
    args = build_parser().parse_args(argv)
    if args.gen_secret:
        print(json.dumps({"auth": {"secret": secrets.token_urlsafe(32)}}, ensure_ascii=False))
        print(
            "把上面这一行合并进两端 ~/.quickswitch/config.json，再重启两端进程；"
            "之后用 python main.py --status 确认「密钥校验：已通过」。",
            file=sys.stderr,
        )
        return 0
    path = config_path(args.config)
    try:
        cfg = load_config(args.config)
        if args.role:
            cfg["role"] = args.role
            validate(cfg)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    events = EventBuffer()
    setup_logging(
        args.log_level or cfg.get("log_level") or "INFO",
        console_level="WARNING" if args.status else None,
        events=events,
    )

    if args.web_port is not None:
        if not 1 <= args.web_port <= 65535:
            print("参数错误：--web-port 必须在 1-65535 之间", file=sys.stderr)
            return 2
        cfg.setdefault("web", {})["port"] = args.web_port

    if args.init_config:
        print(f"已写入配置：{save_config(cfg, args.config)}")
        return 0
    if args.print_config:
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
        return 0
    if args.status:
        try:
            return asyncio.run(cmd_status(cfg, path))
        except KeyboardInterrupt:
            return 130
    if args.detect_inputs:
        try:
            candidates = parse_candidates(args.candidates)
        except ValueError as exc:
            print(f"参数错误：{exc}", file=sys.stderr)
            return 2
        try:
            return cmd_detect_inputs(
                cfg,
                path,
                mac_input=args.mac_input,
                win_input=args.win_input,
                candidates=candidates,
                probe=not args.no_probe,
                interactive=sys.stdin.isatty() and not args.non_interactive,
                write=not args.no_write,
                dry_run=args.dry_run,
            )
        except KeyboardInterrupt:
            print("\n已取消")
            return 130
    if args.once:
        try:
            ok = asyncio.run(run_once(cfg, args.once, dry_run=args.dry_run))
        except KeyboardInterrupt:
            return 130
        print(f"切换到 {LABEL[args.once]}：" + ("成功" if ok else "失败（详见上方日志）"))
        return 0 if ok else 1

    app = QuickSwitchApp(
        cfg,
        dry_run=args.dry_run,
        tray=not args.no_tray,
        config_file=path,
        events=events,
        web=False if args.no_web else None,
    )
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
