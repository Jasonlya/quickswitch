"""本地 Web 控制台：只监听回环地址，令牌鉴权，数据全部来自运行中的进程。

安全模型：
- 仅绑定 127.0.0.1（不对外网暴露）；
- 每次启动生成随机令牌，URL 携带 ``?t=`` 打开页面，页面之后用
  ``X-QuickSwitch-Token`` 头调用 API（自定义头会触发 CORS 预检，阻止跨站表单/图片式请求）；
- 校验 ``Host`` 与 ``Origin``，阻断 DNS rebinding（恶意站点把自己的域名解析到 127.0.0.1）。

数据模型：``/api/status`` 只返回真实状态，不做任何推测或伪造；
读不到的字段返回 null，由前端显示「读取不到」。
"""
from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from config import ConfigError
from config import save_config as save_config_file
from config import validate as validate_config
from ddc import DEFAULT_INPUT_CANDIDATES

log = logging.getLogger("webui")

def _ui_dir() -> Path:
    """前端目录：源码运行取同级 ``ui/``，PyInstaller 打包后取 ``sys._MEIPASS/ui``。"""
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "ui"
    if getattr(sys, "_MEIPASS", None) and bundled.is_dir():
        return bundled
    return Path(__file__).resolve().parent / "ui"


UI_DIR = _ui_dir()

DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8767

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}

MAX_BODY = 256 * 1024

# 只允许页面改这些字段（其余配置需手改文件，避免把 Web 控制台变成任意写入口）
EDITABLE: Dict[str, Any] = {
    "role": None,
    "peer_host": None,
    "peer_port": None,
    "listen_port": None,
    "debounce": None,
    "log_level": None,
    "hotkeys": {"switch_to_mac": None, "switch_to_win": None},
    "network": {"ack_timeout": None, "ping_interval": None, "reconnect_interval": None,
                "reconnect_max_interval": None, "connect_wait": None},
    "discovery": {"enabled": None, "port": None, "interval": None, "ttl": None, "broadcast": None},
    "ddc": {
        "monitor_id": None,
        "display_index": None,
        "mac_input": None,
        "win_input": None,
        "settle": None,
        "timeout": None,
        "retries": None,
        "retry_delay": None,
        "vcp_code": None,
        "controlmyMonitor_path": None,
        "m1ddc_path": None,
        "ddcutil_path": None,
        "command_template": None,
        "read_template": None,
        "read_regex": None,
    },
}

# 保存后立即生效的字段（DDCSwitcher 持有同一个 cfg 字典，改了就生效）
IMMEDIATE_PREFIXES = (
    "ddc.",
    "debounce",
    "network.",
    "log_level",
)

# 必须重启进程才生效的字段
RESTART_PREFIXES = (
    "role",
    "peer_host",
    "peer_port",
    "listen_port",
    "hotkeys.",
    "discovery.",
)


def flatten(values: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    flat: Dict[str, Any] = {}
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def filter_editable(values: Dict[str, Any], schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按白名单过滤外来的配置值，未声明的字段一律丢弃。"""
    schema = EDITABLE if schema is None else schema
    result: Dict[str, Any] = {}
    for key, sub_schema in schema.items():
        if key not in values:
            continue
        value = values[key]
        if sub_schema is None:
            result[key] = value
        elif isinstance(value, dict):
            nested = filter_editable(value, sub_schema)
            if nested:
                result[key] = nested
        else:
            raise ValueError(f"{key} 必须是对象")
    return result


def apply_into(target: Dict[str, Any], values: Dict[str, Any]) -> None:
    """原地递归更新，保持嵌套字典对象身份不变（DDCSwitcher 持有其引用）。"""
    for key, value in values.items():
        current = target.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            apply_into(current, value)
        else:
            target[key] = value


class EventBuffer(logging.Handler):
    """把日志留在内存里，作为控制台「事件」的真实来源。"""

    def __init__(self, limit: int = 300) -> None:
        super().__init__(level=logging.INFO)
        self.limit = limit
        self._records: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # 格式化失败不应影响日志本身
            message = "<无法格式化>"
        with self._lock:
            self._seq += 1
            self._records.append(
                {
                    "seq": self._seq,
                    "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
                    "epoch": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                }
            )
            if len(self._records) > self.limit:
                del self._records[: len(self._records) - self.limit]

    def tail(self, since: int = 0, limit: int = 120) -> List[Dict[str, Any]]:
        with self._lock:
            records = [item for item in self._records if item["seq"] > since]
        return records[-limit:]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq


class WebConsole:
    """把 QuickSwitchApp 的状态与操作暴露成一个本地网页。"""

    def __init__(
        self,
        app,
        *,
        host: str = DEFAULT_WEB_HOST,
        port: int = DEFAULT_WEB_PORT,
        events: Optional[EventBuffer] = None,
    ) -> None:
        self.app = app
        self.host = host
        self.port = int(port)
        self.token = secrets.token_urlsafe(24)
        self.events = events if events is not None else EventBuffer()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- 生命周期
    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?t={self.token}"

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> "WebConsole":
        handler = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="quickswitch-web", daemon=True)
        self._thread.start()
        log.info("Web 控制台已启动：%s", self.url)
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = None
        log.debug("Web 控制台已停止")

    # ---------------------------------------------------------------- 安全
    def _host_allowed(self, host_header: Optional[str]) -> bool:
        if not host_header:
            return False
        host = host_header.strip().lower()
        if host.startswith("["):  # IPv6 字面量
            name, _, _ = host.partition("]")
            candidate = name.lstrip("[")
        else:
            candidate = host.rsplit(":", 1)[0]
        if candidate in ("localhost", self.host):
            return True
        try:
            return ipaddress.ip_address(candidate).is_loopback
        except ValueError:
            return False

    def _origin_allowed(self, origin: Optional[str]) -> bool:
        if not origin:
            return True  # 非浏览器客户端（curl / 本地脚本）
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        try:
            return host in ("localhost", self.host) or ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    # ---------------------------------------------------------------- 业务数据
    def status(self) -> Dict[str, Any]:
        app = self.app
        quick = app.quick
        cfg = app.cfg
        ddc = quick.ddc
        tool_ok, tool_detail = ddc.tool_available()

        def command(target: str) -> Optional[List[str]]:
            with contextlib.suppress(Exception):
                return ddc.build_command(ddc.mac_input if target == "mac" else ddc.win_input)
            return None

        read_command: Optional[List[str]] = None
        with contextlib.suppress(Exception):
            read_command = ddc.build_command(0, get=True) or None

        cached = app.read_cache()
        return {
            "version": app.version,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "host": {
                "name": app.hostname,
                "platform": app.platform,
            },
            "role": cfg.get("role"),
            "state": quick.state,
            "dry_run": bool(app.dry_run),
            "config_path": str(app.config_file),
            "log_file": str(app.log_path),
            "listen_port": cfg.get("listen_port"),
            "peer": {
                "host": app.peer.host,
                "port": app.peer.port,
                "connected": bool(app.peer.connected),
                "outbound": bool(app.peer.outbound_connected),
                "info": app.peer.peer_info or None,
            },
            "auth": {
                "enabled": bool(getattr(app.peer, "auth_enabled", False)),
                "peer": getattr(app.peer, "peer_auth", None),
                "authenticated": bool(getattr(app.peer, "authenticated", False)),
            },
            "discovery": {
                "enabled": bool(app.discovery is not None),
                "peers": app.discovery.peers() if app.discovery is not None else {},
            },
            "ddc": {
                "tool_path": ddc.tool_path(),
                "tool_ok": tool_ok,
                "tool_detail": tool_detail,
                "read_command": read_command,
                "cmd_mac": command("mac"),
                "cmd_win": command("win"),
                "mac_input": ddc.mac_input,
                "win_input": ddc.win_input,
                "display_index": ddc.ddc_cfg.get("display_index"),
                "monitor_id": ddc.ddc_cfg.get("monitor_id"),
                "settle": ddc.settle,
            },
            "probe_candidates": list(DEFAULT_INPUT_CANDIDATES),
            "input": {
                "value": cached.get("value"),
                "target": cached.get("target"),
                "read_at": cached.get("at"),
                "error": cached.get("error"),
            },
            "hotkeys": {
                "switch_to_mac": (cfg.get("hotkeys") or {}).get("switch_to_mac"),
                "switch_to_win": (cfg.get("hotkeys") or {}).get("switch_to_win"),
                "registered": bool(app.hotkeys_registered),
                "reason": app.hotkeys_reason,
            },
            "events_seq": self.events.last_seq,
        }

    def do_switch(self, target: str) -> Dict[str, Any]:
        future = self.app.trigger(target, "web")
        timeout = float((self.app.cfg.get("network") or {}).get("ack_timeout", 4.0)) + 8.0
        ok = future.result(timeout=timeout)
        # 切换后异步回读真实输入源，避免在请求里阻塞
        self.app.refresh_input_async()
        return {"ok": bool(ok), "target": target, "state": self.app.quick.state}

    def read_input(self) -> Dict[str, Any]:
        """同步读取当前输入源（阻塞在 DDC 命令上，最长 ddc.timeout）。"""
        value = self.app.read_input()
        return {"value": value, "target": self.app.target_for_input(value)}

    def probe_inputs(self, candidates: Optional[List[Any]] = None) -> Dict[str, Any]:
        values: Optional[List[int]] = None
        if candidates:
            try:
                values = [int(item) for item in candidates]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"candidates 必须是整数数组：{exc}") from exc
            for value in values:
                if not 0 <= value <= 255:
                    raise ValueError(f"候选取值必须在 0-255 之间：{value}")
        found = self.app.probe_inputs(values)
        self.app.refresh_input_async()
        return {
            "available": found,
            "candidates": values or list(DEFAULT_INPUT_CANDIDATES),
            "restored": True,
        }

    def config_view(self) -> Dict[str, Any]:
        cfg = self.app.cfg
        return {
            "config_path": str(self.app.config_file),
            "values": {key: cfg.get(key, {} if isinstance(cfg.get(key), dict) else None)
                       for key in EDITABLE},
            "immediate_prefixes": list(IMMEDIATE_PREFIXES),
            "restart_prefixes": list(RESTART_PREFIXES),
        }

    def save_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload.get("values") if isinstance(payload.get("values"), dict) else payload
        if not isinstance(raw, dict):
            raise ValueError("请求体需要 values 对象")
        values = filter_editable(raw)
        if not values:
            raise ValueError("没有可更新的字段")

        cfg = self.app.cfg
        before = flatten(cfg)
        # 先算出「合并后」的配置并校验，校验失败就不落盘
        candidate = json.loads(json.dumps(cfg, ensure_ascii=False))
        apply_into(candidate, values)
        try:
            validate_config(candidate)
        except ConfigError as exc:
            raise ValueError(str(exc)) from exc

        after = flatten(candidate)
        changed = sorted(key for key, value in after.items() if before.get(key) != value)
        restart_keys = [
            key for key in changed
            if any(key == prefix.rstrip(".") or key.startswith(prefix) for prefix in RESTART_PREFIXES)
        ]

        # 生效：即时字段直接原地更新运行中的配置；其余需重启
        apply_into(cfg, values)
        saved = save_config_file(cfg, str(self.app.config_file))
        log.info("Web 控制台更新了配置：%s", "、".join(changed) or "（无变化）")
        return {
            "ok": True,
            "saved": str(saved),
            "changed": changed,
            "restart_required": bool(restart_keys),
            "restart_keys": restart_keys,
            "values": self.config_view()["values"],
        }

    # ---------------------------------------------------------------- HTTP
    def _make_handler(self):
        console = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "QuickSwitch"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # 不写 access log 到控制台
                log.debug("web %s - %s", self.address_string(), fmt % args)

            # ---- 工具
            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, code: int, payload: Any) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(code, body, "application/json; charset=utf-8")

            def _error(self, code: int, message: str) -> None:
                self._json(code, {"error": message})

            def _authorized(self) -> bool:
                header = self.headers.get("X-QuickSwitch-Token")
                if header:
                    return secrets.compare_digest(header, console.token)
                query = parse_qs(urlparse(self.path).query)
                provided = (query.get("t") or [""])[0]
                return bool(provided) and secrets.compare_digest(provided, console.token)

            def _guard(self) -> bool:
                if not console._host_allowed(self.headers.get("Host")):
                    self._error(403, "Host 不被允许")
                    return False
                if not console._origin_allowed(self.headers.get("Origin")):
                    self._error(403, "Origin 不被允许")
                    return False
                return True

            def _read_json(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                if length > MAX_BODY:
                    raise ValueError("请求体过大")
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                return data

            # ---- 路由
            def do_GET(self) -> None:
                if not self._guard():
                    return
                path = urlparse(self.path).path
                if path.startswith("/api/"):
                    if not self._authorized():
                        self._error(401, "缺少或错误的令牌")
                        return
                    self._api_get(path)
                    return
                self._static(path)

            def do_HEAD(self) -> None:
                self.do_GET()

            def do_POST(self) -> None:
                if not self._guard():
                    return
                path = urlparse(self.path).path
                if not path.startswith("/api/"):
                    self._error(404, "未知路径")
                    return
                if not self._authorized():
                    self._error(401, "缺少或错误的令牌")
                    return
                try:
                    payload = self._read_json()
                except (ValueError, json.JSONDecodeError) as exc:
                    self._error(400, f"请求体解析失败：{exc}")
                    return
                self._api_post(path, payload)

            def do_PUT(self) -> None:
                self.do_POST()

            # ---- API
            def _api_get(self, path: str) -> None:
                if path == "/api/status":
                    self._json(200, console.status())
                elif path == "/api/config":
                    self._json(200, console.config_view())
                elif path == "/api/events":
                    query = parse_qs(urlparse(self.path).query)
                    since = int((query.get("since") or ["0"])[0] or 0)
                    records = console.events.tail(since=since)
                    self._json(200, {"events": records, "last_seq": console.events.last_seq})
                else:
                    self._error(404, "未知 API")

            def _api_post(self, path: str, payload: Dict[str, Any]) -> None:
                try:
                    if path == "/api/switch":
                        target = str(payload.get("target") or "")
                        if target not in ("mac", "win"):
                            self._error(400, "target 必须是 mac 或 win")
                            return
                        self._json(200, console.do_switch(target))
                    elif path == "/api/read-input":
                        self._json(200, console.read_input())
                    elif path == "/api/probe-inputs":
                        candidates = payload.get("candidates")
                        self._json(200, console.probe_inputs(candidates))
                    elif path in ("/api/config", "/api/config/"):
                        self._json(200, console.save_config(payload))
                    else:
                        self._error(404, "未知 API")
                except TimeoutError:
                    self._error(504, "操作超时（对端未确认或 DDC 未响应）")
                except ValueError as exc:
                    self._error(400, str(exc))
                except Exception as exc:  # 不把栈暴露给页面，日志里留痕
                    log.exception("Web API 失败：%s", path)
                    self._error(500, f"内部错误：{exc}")

            # ---- 静态文件
            def _static(self, path: str) -> None:
                relative = unquote(path).lstrip("/") or "index.html"
                candidate = (UI_DIR / relative).resolve()
                if UI_DIR.resolve() not in candidate.parents and candidate != UI_DIR.resolve():
                    self._error(403, "路径不被允许")
                    return
                if not candidate.is_file():
                    self._error(404, "文件不存在")
                    return
                suffix = candidate.suffix.lower()
                self._send(
                    200,
                    candidate.read_bytes(),
                    CONTENT_TYPES.get(suffix, "application/octet-stream"),
                )

        return Handler
