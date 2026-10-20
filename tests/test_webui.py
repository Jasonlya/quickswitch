"""Web 控制台测试：令牌、Host/Origin 防护、状态接口、切换、配置白名单与静态文件。"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import default_config
from main import QuickSwitchApp
from webui import EventBuffer, WebConsole


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeDDC:
    """实现 DDCSwitcher 中被 Web 控制台与状态接口用到的全部接口。"""

    def __init__(self, current: int = 15, tool_ok: bool = True) -> None:
        self.current = current
        self.dry_run = True
        self.mac_input = 15
        self.win_input = 17
        self.settle = 0.0
        self.tool_calls = 0
        self.probe_calls: List[List[int]] = []
        self.ddc_cfg: Dict[str, Any] = {
            "mac_input": 15, "win_input": 17, "display_index": 1,
            "monitor_id": r"\\.\DISPLAY1\Monitor0", "settle": 0.0,
        }
        self._tool_ok = tool_ok

    def tool_path(self) -> str:
        return "/fake/m1ddc"

    def tool_available(self) -> Tuple[bool, str]:
        self.tool_calls += 1
        return (self._tool_ok, "已找到 /fake/m1ddc" if self._tool_ok else "未找到 /fake/m1ddc")

    def build_command(self, value: int, *, get: bool = False) -> List[str]:
        if get:
            return ["/fake/m1ddc", "display", "1", "get", "input"]
        return ["/fake/m1ddc", "display", "1", "set", "input", str(value)]

    def get_input(self) -> Optional[int]:
        return self.current

    def set_input(self, value: int) -> bool:
        self.current = value
        return True

    def switch_to_mac(self) -> bool:
        return self.set_input(self.mac_input)

    def switch_to_win(self) -> bool:
        return self.set_input(self.win_input)

    def probe_inputs(self, candidates=None, *, settle=None, progress=None) -> List[int]:
        values = list(candidates or [1, 3, 15, 16, 17, 18, 27])
        self.probe_calls.append(values)
        found = [value for value in values if value in (15, 17)]
        if progress:
            for value in values:
                progress(value, value if value in found else None)
        return found


class WebConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        cfg = default_config()
        cfg.update({
            "role": "server",
            "peer_host": "127.0.0.1",
            "peer_port": free_port(),
            "listen_port": free_port(),
            "debounce": 0.0,
        })
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_file = Path(self.tmp.name) / "config.json"
        self.events = EventBuffer()
        logging.getLogger().addHandler(self.events)
        self.addCleanup(lambda: logging.getLogger().removeHandler(self.events))

        self.app = QuickSwitchApp(
            cfg, dry_run=True, tray=False, config_file=self.config_file, events=self.events
        )
        self.fake = FakeDDC()
        self.app.quick.ddc = self.fake
        self.thread = threading.Thread(target=self.app._run_loop, daemon=True)
        self.thread.start()
        self.app._thread = self.thread  # 让 shutdown() 能 join 到事件循环线程
        self.addCleanup(self._teardown_app)

        self.console = WebConsole(self.app, port=0, events=self.events)
        self.console.start()
        self.addCleanup(self.console.stop)
        self.port = self.console.port
        self.token = self.console.token

    def _teardown_app(self) -> None:
        self.app.shutdown()
        self.app.loop.close()

    # ---------------------------------------------------------------- 工具
    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        token: Optional[str] = None,
        host: Optional[str] = None,
        origin: Optional[str] = None,
    ) -> Tuple[int, Any, Dict[str, str]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            headers: Dict[str, str] = {}
            if token:
                headers["X-QuickSwitch-Token"] = token
            if host:
                headers["Host"] = host
            if origin:
                headers["Origin"] = origin
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            content_type = response.getheader("Content-Type", "")
            try:
                parsed: Any = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = raw
            return response.status, parsed, {"Content-Type": content_type}
        finally:
            conn.close()

    # ---------------------------------------------------------------- 安全
    def test_api_requires_token(self) -> None:
        status, payload, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 401)
        self.assertIn("令牌", payload["error"])

        status, payload, _ = self.request("GET", "/api/status", token="wrong")
        self.assertEqual(status, 401)

        status, payload, _ = self.request("GET", "/api/status?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "server")

    def test_foreign_host_is_rejected(self) -> None:
        status, payload, _ = self.request(
            "GET", "/api/status?t=" + self.token, host="evil.example.com"
        )
        self.assertEqual(status, 403)
        self.assertIn("Host", payload["error"])

    def test_foreign_origin_is_rejected(self) -> None:
        status, _, _ = self.request(
            "POST", "/api/switch", {"target": "mac"},
            token=self.token, origin="https://evil.example.com",
        )
        self.assertEqual(status, 403)

    def test_token_in_query_works_for_whole_api(self) -> None:
        status, payload, _ = self.request("GET", "/api/config?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn("values", payload)

    # ---------------------------------------------------------------- 状态
    def test_status_reports_real_state_only(self) -> None:
        status, payload, _ = self.request("GET", "/api/status", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["host"]["name"], self.app.hostname)
        self.assertEqual(payload["version"], self.app.version)
        self.assertEqual(payload["ddc"]["mac_input"], 15)
        self.assertEqual(payload["ddc"]["win_input"], 17)
        self.assertEqual(payload["ddc"]["tool_path"], "/fake/m1ddc")
        self.assertTrue(payload["ddc"]["tool_ok"])
        self.assertEqual(payload["ddc"]["cmd_mac"][-1], "15")
        self.assertEqual(payload["ddc"]["read_command"][-1], "input")
        self.assertEqual(payload["probe_candidates"], [1, 3, 15, 16, 17, 18, 27])
        self.assertFalse(payload["peer"]["connected"])
        self.assertFalse(payload["discovery"]["enabled"])
        self.assertFalse(payload["hotkeys"]["registered"])
        self.assertIsNone(payload["input"]["value"])

    def test_read_input_updates_cache(self) -> None:
        status, payload, _ = self.request("POST", "/api/read-input", {}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["value"], 15)
        self.assertEqual(payload["target"], "mac")

        _, status_payload, _ = self.request("GET", "/api/status", token=self.token)
        self.assertEqual(status_payload["input"]["value"], 15)
        self.assertEqual(status_payload["input"]["target"], "mac")
        self.assertIsNotNone(status_payload["input"]["read_at"])

    def test_probe_inputs_returns_accepted_values(self) -> None:
        status, payload, _ = self.request(
            "POST", "/api/probe-inputs", {"candidates": [1, 15, 17]}, token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["available"], [15, 17])
        self.assertEqual(self.fake.probe_calls, [[1, 15, 17]])

        status, payload, _ = self.request(
            "POST", "/api/probe-inputs", {"candidates": [1, 300]}, token=self.token
        )
        self.assertEqual(status, 400)
        self.assertIn("0-255", payload["error"])

    # ---------------------------------------------------------------- 切换
    def test_switch_reaches_ddc_and_rejects_unknown_target(self) -> None:
        status, payload, _ = self.request(
            "POST", "/api/switch", {"target": "win"}, token=self.token
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.fake.current, 17)
        self.assertEqual(self.app.quick.state, "win")

        status, payload, _ = self.request(
            "POST", "/api/switch", {"target": "linux"}, token=self.token
        )
        self.assertEqual(status, 400)
        self.assertIn("mac", payload["error"])

    # ---------------------------------------------------------------- 配置
    def test_config_view_lists_whitelisted_fields(self) -> None:
        status, payload, _ = self.request("GET", "/api/config", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["config_path"], str(self.config_file))
        self.assertEqual(set(payload["values"]), {"role", "peer_host", "peer_port",
                                                  "listen_port", "debounce", "log_level",
                                                  "hotkeys", "network", "discovery", "ddc"})
        self.assertIn("role", payload["restart_prefixes"])
        self.assertIn("ddc.", payload["immediate_prefixes"])

    def test_config_save_applies_and_flags_restart(self) -> None:
        body = {"values": {
            "ddc": {"mac_input": 16, "nonsense": 1},
            "role": "client",
            "web": {"port": 1234},
        }}
        status, payload, _ = self.request("POST", "/api/config", body, token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(sorted(payload["changed"]), ["ddc.mac_input", "role"])
        self.assertTrue(payload["restart_required"])
        self.assertEqual(payload["restart_keys"], ["role"])

        # 白名单外的字段既不进内存配置也不落盘
        self.assertEqual(self.app.cfg["ddc"]["mac_input"], 16)
        self.assertNotIn("nonsense", self.app.cfg["ddc"])
        self.assertEqual(self.app.cfg["web"]["port"], 8767)
        self.assertEqual(self.fake.mac_input, 15)  # DDC 对象持有自己的配置副本

        on_disk = json.loads(self.config_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["ddc"]["mac_input"], 16)
        self.assertEqual(on_disk["role"], "client")
        self.assertEqual(on_disk["web"]["port"], 8767)

    def test_config_save_rejects_invalid_values_without_writing(self) -> None:
        status, payload, _ = self.request(
            "POST", "/api/config", {"values": {"ddc": {"mac_input": 999}}}, token=self.token
        )
        self.assertEqual(status, 400)
        self.assertIn("mac_input", payload["error"])
        self.assertEqual(self.app.cfg["ddc"]["mac_input"], 15)
        self.assertFalse(self.config_file.exists())

        status, payload, _ = self.request(
            "POST", "/api/config", {"values": {"web": {"port": 1}}}, token=self.token
        )
        self.assertEqual(status, 400)
        self.assertIn("没有可更新", payload["error"])

    # ---------------------------------------------------------------- 静态文件
    def test_static_files_served_and_traversal_blocked(self) -> None:
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("QuickSwitch", body.decode("utf-8"))

        status, _, headers = self.request("GET", "/app.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["Content-Type"])

        status, payload, _ = self.request("GET", "/../config.py")
        self.assertEqual(status, 403)
        self.assertIn("路径", payload["error"])

        status, payload, _ = self.request("GET", "/%2e%2e/main.py")
        self.assertEqual(status, 403)

        status, _, _ = self.request("GET", "/missing.js")
        self.assertEqual(status, 404)

    # ---------------------------------------------------------------- 事件
    def test_events_tail_follows_seq(self) -> None:
        logging.getLogger("quickswitch-test").warning("测试事件：切换完成")
        status, payload, _ = self.request("GET", "/api/events?since=0", token=self.token)
        self.assertEqual(status, 200)
        messages = [item["message"] for item in payload["events"]]
        self.assertIn("测试事件：切换完成", messages)
        last_seq = payload["last_seq"]
        self.assertGreater(last_seq, 0)

        status, payload, _ = self.request(
            "GET", f"/api/events?since={last_seq}", token=self.token
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["events"], [])


if __name__ == "__main__":
    unittest.main()
