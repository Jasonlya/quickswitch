"""网络模块测试：双向收发、心跳、重连、端口占用与单向可达回退。"""
from __future__ import annotations

import asyncio
import contextlib
import socket
import time
import unittest
from typing import Any, Callable, Dict, List

from network import PeerConnection


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def recorder(sink: List[Dict[str, Any]]):
    async def handler(msg: Dict[str, Any]) -> None:
        sink.append(msg)

    return handler


async def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


class PeerConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.received_a: List[Dict[str, Any]] = []
        self.received_b: List[Dict[str, Any]] = []
        self.port_a = free_port()
        self.port_b = free_port()
        self.events: List[str] = []
        self.a = PeerConnection(
            "127.0.0.1", self.port_b, self.port_a, recorder(self.received_a),
            name="a", identity={"name": "a", "role": "server"},
            ping_interval=0.2, reconnect_interval=0.2, reconnect_max_interval=0.5,
            on_event=lambda event, **fields: self.events.append(event),
        )
        self.b = PeerConnection(
            "127.0.0.1", self.port_a, self.port_b, recorder(self.received_b),
            name="b", identity={"name": "b", "role": "client"},
            ping_interval=0.2, reconnect_interval=0.2, reconnect_max_interval=0.5,
        )
        await self.a.start()
        await self.b.start()
        self.addAsyncCleanup(self.b.stop)
        self.addAsyncCleanup(self.a.stop)
        self.assertTrue(await self.a.wait_connected(3.0))
        self.assertTrue(await self.b.wait_connected(3.0))

    async def test_messages_flow_both_directions(self) -> None:
        self.assertTrue(await self.a.send({"type": "switch", "target": "mac", "id": "m1"}))
        self.assertTrue(
            await wait_for(lambda: any(m.get("type") == "switch" for m in self.received_b))
        )
        self.assertTrue(await self.b.send({"type": "pong", "ts": 1}))
        self.assertTrue(
            await wait_for(lambda: any(m.get("type") == "pong" for m in self.received_a))
        )

    async def test_hello_is_exchanged(self) -> None:
        self.assertTrue(await wait_for(lambda: self.a.peer_info.get("role") == "client"))
        self.assertTrue(await wait_for(lambda: self.b.peer_info.get("role") == "server"))

    async def test_heartbeat_ping_is_sent(self) -> None:
        self.assertTrue(
            await wait_for(lambda: any(m.get("type") == "ping" for m in self.received_b))
        )

    async def test_reconnect_after_outbound_drop(self) -> None:
        await self.a._reconnect_now()
        self.assertFalse(self.a.outbound_connected)
        self.assertTrue(await wait_for(lambda: self.a.outbound_connected, 3.0))
        self.assertTrue(await self.a.send({"type": "switch", "target": "win", "id": "m2"}))
        self.assertTrue(
            await wait_for(
                lambda: any(m.get("id") == "m2" for m in self.received_b), timeout=3.0
            )
        )

    async def test_event_hook_reports_connected(self) -> None:
        # A 的连接循环可能在 B 完成监听之前先失败一次，因此这里等待事件出现
        self.assertTrue(await wait_for(lambda: "connected" in self.events, 3.0))
        self.assertIn("inbound", self.events)

    async def test_peer_info_name(self) -> None:
        self.assertEqual(self.a.peer_info.get("name"), "b")


class PeerConnectionEdgeCases(unittest.IsolatedAsyncioTestCase):
    async def test_send_without_connection_returns_false(self) -> None:
        peer = PeerConnection("127.0.0.1", free_port(), free_port(), recorder([]), name="solo")
        await peer.start()
        self.addAsyncCleanup(peer.stop)
        self.assertFalse(peer.connected)
        self.assertFalse(await peer.send({"type": "ping"}))
        self.assertFalse(await peer.wait_connected(0.3))

    async def test_listen_port_conflict_raises_oserror(self) -> None:
        port = free_port()
        first = PeerConnection("127.0.0.1", free_port(), port, recorder([]), name="first")
        await first.start()
        self.addAsyncCleanup(first.stop)
        second = PeerConnection("127.0.0.1", free_port(), port, recorder([]), name="second")
        with self.assertRaises(OSError):
            await second.start()
        # 失败后不应留下半启动状态
        self.assertFalse(second.running)

    async def test_auto_host_waits_and_can_use_inbound_channel(self) -> None:
        """本机不做主动连接时（peer_host=auto），仍可通过对端连进来的连接回消息。"""
        passive_port = free_port()
        active_received: List[Dict[str, Any]] = []
        passive = PeerConnection("auto", free_port(), passive_port, recorder([]), name="passive")
        active = PeerConnection(
            "127.0.0.1", passive_port, free_port(), recorder(active_received), name="active",
            reconnect_interval=0.2, reconnect_max_interval=0.5,
        )
        await passive.start()
        await active.start()
        self.addAsyncCleanup(active.stop)
        self.addAsyncCleanup(passive.stop)

        self.assertTrue(await wait_for(lambda: passive.connected, 3.0))
        self.assertFalse(passive.outbound_connected)  # 没有主动连接
        self.assertTrue(await passive.send({"type": "switch_ack", "id": "m3", "ok": True}))
        self.assertTrue(
            await wait_for(lambda: any(m.get("id") == "m3" for m in active_received), 3.0)
        )

    async def test_malformed_message_is_ignored(self) -> None:
        received: List[Dict[str, Any]] = []
        port_a, port_b = free_port(), free_port()
        listener = PeerConnection("127.0.0.1", port_b, port_a, recorder(received), name="listener")
        await listener.start()
        self.addAsyncCleanup(listener.stop)

        reader, writer = await asyncio.open_connection("127.0.0.1", port_a)
        self.addAsyncCleanup(self._close_writer, writer)
        writer.write(b"this is not json\n")
        writer.write(b'{"type": "ping", "id": "ok1"}\n')
        await writer.drain()
        self.assertTrue(await wait_for(lambda: any(m.get("id") == "ok1" for m in received), 3.0))
        self.assertEqual(len([m for m in received if m.get("id") == "ok1"]), 1)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
