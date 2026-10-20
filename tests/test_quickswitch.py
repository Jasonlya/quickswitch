"""端到端测试：热键触发的完整链路（本地 DDC + TCP 通知 + 对端 ACK）。"""
from __future__ import annotations

import asyncio
import socket
import time
import unittest
from typing import Any, Callable, Dict, List

from main import QuickSwitch, network_kwargs, run_once
from network import PeerConnection


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def base_cfg(**overrides: Any) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "role": "server",
        "peer_host": "127.0.0.1",
        "peer_port": 8765,
        "listen_port": 8765,
        "debounce": 0,
        "network": {"ping_interval": 30.0, "ack_timeout": 2.0, "reconnect_interval": 0.2,
                    "reconnect_max_interval": 0.5},
        "ddc": {"mac_input": 15, "win_input": 17, "timeout": 5.0, "retries": 0, "retry_delay": 0,
                "display_index": 1, "m1ddc_path": "/opt/homebrew/bin/m1ddc",
                "controlmyMonitor_path": "ControlMyMonitor.exe",
                "monitor_id": r"\\.\DISPLAY1\Monitor0", "vcp_code": "0x60"},
    }
    cfg.update(overrides)
    return cfg


class FakeDDC:
    """记录调用并可直接指定成功/失败。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: List[str] = []
        self.dry_run = False

    def switch_to_mac(self) -> bool:
        self.calls.append("mac")
        return self.ok

    def switch_to_win(self) -> bool:
        self.calls.append("win")
        return self.ok

    def tool_available(self):
        return True, "fake"


async def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class TwoNodeTests(unittest.IsolatedAsyncioTestCase):
    """模拟 Windows(server) 与 macOS(client) 两端常驻进程。"""

    async def asyncSetUp(self) -> None:
        self.ddc_server = FakeDDC()
        self.ddc_client = FakeDDC()
        self.server_port = free_port()
        self.client_port = free_port()
        self.cfg_server = base_cfg(
            peer_host="127.0.0.1", peer_port=self.client_port, listen_port=self.server_port
        )
        self.cfg_client = base_cfg(
            role="client", peer_host="127.0.0.1", peer_port=self.server_port,
            listen_port=self.client_port,
        )
        self.server = QuickSwitch(self.cfg_server, ddc=self.ddc_server, name="server-node")
        self.client = QuickSwitch(self.cfg_client, ddc=self.ddc_client, name="client-node")
        self.peer_server = PeerConnection(
            "127.0.0.1", self.client_port, self.server_port, self.server.handle_message,
            name="server-node", **network_kwargs(self.cfg_server),
        )
        self.peer_client = PeerConnection(
            "127.0.0.1", self.server_port, self.client_port, self.client.handle_message,
            name="client-node", **network_kwargs(self.cfg_client),
        )
        self.server.set_peer(self.peer_server)
        self.client.set_peer(self.peer_client)
        await self.peer_server.start()
        await self.peer_client.start()
        self.addAsyncCleanup(self.peer_client.stop)
        self.addAsyncCleanup(self.peer_server.stop)
        self.assertTrue(await wait_for(lambda: self.peer_server.connected and self.peer_client.connected))

    async def test_trigger_switches_local_and_remote(self) -> None:
        ok = await self.server.trigger("mac", source="hotkey")
        self.assertTrue(ok)
        self.assertEqual(self.ddc_server.calls, ["mac"])
        self.assertEqual(self.ddc_client.calls, ["mac"])
        self.assertEqual(self.server.state, "mac")
        self.assertEqual(self.client.state, "mac")

    async def test_trigger_back_to_windows(self) -> None:
        await self.server.trigger("mac")
        await self.server.trigger("win")
        self.assertEqual(self.ddc_server.calls, ["mac", "win"])
        self.assertEqual(self.ddc_client.calls, ["mac", "win"])

    async def test_client_initiated_switch_does_not_bounce(self) -> None:
        self.assertTrue(await self.client.trigger("win", source="hotkey"))
        self.assertEqual(self.ddc_client.calls, ["win"])
        self.assertEqual(self.ddc_server.calls, ["win"])

    async def test_local_failure_but_remote_success_counts_as_ok(self) -> None:
        self.ddc_server.ok = False
        self.assertTrue(await self.server.trigger("mac"))
        self.assertEqual(self.ddc_server.calls, ["mac"])
        self.assertEqual(self.ddc_client.calls, ["mac"])

    async def test_both_sides_failing_returns_false(self) -> None:
        self.ddc_server.ok = False
        self.ddc_client.ok = False
        self.assertFalse(await self.server.trigger("mac"))

    async def test_unknown_target_gets_negative_ack(self) -> None:
        # 由 client 侧发出请求，因此 ACK 会回到 client；把它登记在 client 的待确认表里
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.client._pending["probe-1"] = future
        await self.peer_client.send({"type": "switch", "target": "linux", "id": "probe-1"})
        ack = await asyncio.wait_for(future, 3.0)
        self.assertFalse(ack["ok"])
        self.assertIn("unknown target", ack["detail"])

    async def test_ping_gets_pong(self) -> None:
        pongs: List[Dict[str, Any]] = []
        original = self.client.handle_message

        async def spy(msg: Dict[str, Any]) -> None:
            if msg.get("type") == "pong":
                pongs.append(msg)
            await original(msg)

        self.client.handle_message = spy
        self.peer_client.on_message = spy
        # client 发 ping → server 收到后回 pong → client 侧观察到 pong
        await self.peer_client.send({"type": "ping", "ts": 1})
        self.assertTrue(await wait_for(lambda: bool(pongs)))

    async def test_peer_disconnect_is_reported_not_fatal(self) -> None:
        await self.peer_client.stop()
        # 对端已断开：本地仍应切换成功，只是没有远端确认
        self.assertTrue(await self.server.trigger("mac"))
        self.assertEqual(self.ddc_server.calls, ["mac"])


class TriggerEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_without_peer_uses_local_ddc(self) -> None:
        ddc = FakeDDC()
        quick = QuickSwitch(base_cfg(), ddc=ddc)
        self.assertTrue(await quick.trigger("win"))
        self.assertEqual(ddc.calls, ["win"])
        self.assertEqual(quick.state, "win")

    async def test_trigger_without_peer_reports_failure(self) -> None:
        quick = QuickSwitch(base_cfg(), ddc=FakeDDC(ok=False))
        self.assertFalse(await quick.trigger("mac"))
        self.assertIsNone(quick.state)

    async def test_debounce_ignores_repeat(self) -> None:
        ddc = FakeDDC()
        quick = QuickSwitch(base_cfg(debounce=10.0), ddc=ddc)
        self.assertTrue(await quick.trigger("mac"))
        self.assertTrue(await quick.trigger("mac"))
        self.assertEqual(ddc.calls, ["mac"])
        # 不同目标不受去抖影响
        self.assertTrue(await quick.trigger("win"))
        self.assertEqual(ddc.calls, ["mac", "win"])

    async def test_unknown_target_raises(self) -> None:
        quick = QuickSwitch(base_cfg(), ddc=FakeDDC())
        with self.assertRaises(ValueError):
            await quick.trigger("linux")

    async def test_status_line_contains_role_and_peer(self) -> None:
        quick = QuickSwitch(base_cfg(), ddc=FakeDDC())
        self.assertIn("role=server", quick.status_line())
        self.assertIn("无对端连接", quick.status_line())


class RunOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_once_dry_run_without_peer(self) -> None:
        cfg = base_cfg(peer_host="127.0.0.1", peer_port=free_port(), listen_port=free_port())
        self.assertTrue(await run_once(cfg, "mac", dry_run=True, wait=0.3))

    async def test_run_once_with_live_peer(self) -> None:
        """--once 场景：本机切换 + 通知对端（对端真实收到并执行）。"""
        server_port, client_port = free_port(), free_port()
        cfg_server = base_cfg(peer_host="127.0.0.1", peer_port=client_port, listen_port=server_port)
        cfg_client = base_cfg(role="client", peer_host="127.0.0.1", peer_port=server_port,
                              listen_port=client_port)
        ddc_client = FakeDDC()
        client = QuickSwitch(cfg_client, ddc=ddc_client, name="client-node")
        peer_client = PeerConnection(
            "127.0.0.1", server_port, client_port, client.handle_message,
            name="client-node", **network_kwargs(cfg_client),
        )
        client.set_peer(peer_client)
        await peer_client.start()
        self.addAsyncCleanup(peer_client.stop)

        self.assertTrue(await run_once(cfg_server, "mac", dry_run=True, wait=2.0))
        self.assertEqual(ddc_client.calls, ["mac"])


if __name__ == "__main__":
    unittest.main()
