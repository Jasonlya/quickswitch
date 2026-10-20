"""设备发现测试：报文编解码、设备表维护、真实 UDP 收包。"""
from __future__ import annotations

import asyncio
import json
import socket
import time
import unittest
from typing import Any, Callable, Dict, List
from unittest import mock

import discovery as discovery_module
from discovery import MAGIC, PeerDiscovery


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def make_discovery(**kwargs: Any) -> PeerDiscovery:
    params: Dict[str, Any] = {"name": "node-a", "listen_port": 8765, "port": 8766}
    params.update(kwargs)
    return PeerDiscovery(**params)


class PacketTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        node = make_discovery()
        payload = PeerDiscovery.parse_packet(node.build_packet())
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["magic"], MAGIC)
        self.assertEqual(payload["name"], "node-a")
        self.assertEqual(payload["port"], 8765)

    def test_rejects_other_magic(self) -> None:
        data = json.dumps({"magic": "something-else", "name": "x", "port": 1}).encode()
        self.assertIsNone(PeerDiscovery.parse_packet(data))

    def test_rejects_malformed(self) -> None:
        self.assertIsNone(PeerDiscovery.parse_packet(b"not json"))
        self.assertIsNone(PeerDiscovery.parse_packet(json.dumps([1, 2]).encode()))
        self.assertIsNone(PeerDiscovery.parse_packet(json.dumps({"magic": MAGIC}).encode()))
        self.assertIsNone(
            PeerDiscovery.parse_packet(json.dumps({"magic": MAGIC, "name": "", "port": 1}).encode())
        )
        self.assertIsNone(
            PeerDiscovery.parse_packet(
                json.dumps({"magic": MAGIC, "name": "peer", "port": "8765"}).encode()
            )
        )


class BroadcastTargetTests(unittest.TestCase):
    def test_configured_broadcast_is_used(self) -> None:
        node = make_discovery(broadcast="192.168.1.255", port=9000)
        with mock.patch("discovery.local_ipv4_addresses", return_value=[]):
            self.assertEqual(node.broadcast_targets(), [("192.168.1.255", 9000)])

    def test_list_broadcast_and_subnet_inference(self) -> None:
        node = make_discovery(broadcast=["255.255.255.255", "10.0.0.255"], port=9000)
        with mock.patch("discovery.local_ipv4_addresses", return_value=["192.168.1.20", "10.0.0.5"]):
            targets = node.broadcast_targets()
        self.assertIn(("255.255.255.255", 9000), targets)
        self.assertIn(("10.0.0.255", 9000), targets)
        self.assertIn(("192.168.1.255", 9000), targets)
        self.assertEqual(len(targets), len(set(targets)))  # 无重复

    def test_inference_can_be_disabled(self) -> None:
        node = make_discovery(broadcast="", infer_subnet_broadcast=False)
        self.assertEqual(node.broadcast_targets(), [])

    def test_local_ipv4_addresses_excludes_loopback(self) -> None:
        self.assertFalse(any(addr.startswith("127.") for addr in discovery_module.local_ipv4_addresses()))


class PeerTableTests(unittest.TestCase):
    def test_observe_records_new_peer(self) -> None:
        found: List[Dict[str, Any]] = []
        node = make_discovery(on_peer=found.append)
        sender = make_discovery(name="node-b", listen_port=9999)

        result = node.observe(sender.build_packet(), ("192.168.1.20", 50000))
        self.assertIsNotNone(result)
        self.assertEqual(len(found), 1)
        self.assertEqual(node.peers()["node-b"]["host"], "192.168.1.20")
        self.assertEqual(node.best_peer()["port"], 9999)

    def test_observe_ignores_self(self) -> None:
        found: List[Dict[str, Any]] = []
        node = make_discovery(on_peer=found.append)
        self.assertIsNone(node.observe(node.build_packet(), ("127.0.0.1", 50000)))
        self.assertEqual(node.peers(), {})
        self.assertEqual(found, [])

    def test_observe_detects_address_change(self) -> None:
        found: List[Dict[str, Any]] = []
        node = make_discovery(on_peer=found.append)
        sender = make_discovery(name="node-b")
        node.observe(sender.build_packet(), ("192.168.1.20", 50000))
        self.assertIsNone(node.observe(sender.build_packet(), ("192.168.1.20", 50000)))
        node.observe(sender.build_packet(), ("10.0.0.7", 50000))
        self.assertEqual(len(found), 2)
        self.assertEqual(node.peers()["node-b"]["host"], "10.0.0.7")

    def test_stale_peer_is_pruned(self) -> None:
        node = make_discovery(ttl=0.1)
        sender = make_discovery(name="node-b")
        node.observe(sender.build_packet(), ("192.168.1.20", 50000))
        self.assertEqual(len(node.peers()), 1)
        time.sleep(0.15)
        self.assertEqual(node.peers(), {})
        self.assertIsNone(node.best_peer())

    def test_best_peer_prefers_most_recent(self) -> None:
        node = make_discovery()
        node.observe(make_discovery(name="node-b").build_packet(), ("192.168.1.20", 1))
        time.sleep(0.01)
        node.observe(make_discovery(name="node-c").build_packet(), ("192.168.1.21", 1))
        self.assertEqual(node.best_peer()["name"], "node-c")
        self.assertEqual(node.best_peer(exclude=["node-c"])["name"], "node-b")


class LiveDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_receives_unicast_announcement(self) -> None:
        found: List[Dict[str, Any]] = []
        port = free_udp_port()
        node = make_discovery(name="listener", port=port, interval=30.0, ttl=30.0, on_peer=found.append)
        await node.start()
        self.addAsyncCleanup(node.stop)

        sender = make_discovery(name="peer-b", listen_port=8765)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(sender.build_packet(), ("127.0.0.1", port))

        self.assertTrue(await wait_for(lambda: bool(found)))
        self.assertEqual(found[0]["name"], "peer-b")
        self.assertEqual(found[0]["host"], "127.0.0.1")
        self.assertEqual(node.peers()["peer-b"]["port"], 8765)

    async def test_best_peer_after_start(self) -> None:
        port = free_udp_port()
        node = make_discovery(name="listener", port=port, interval=30.0)
        await node.start()
        self.addAsyncCleanup(node.stop)
        self.assertIsNone(node.best_peer())


if __name__ == "__main__":
    unittest.main()
