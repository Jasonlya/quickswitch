"""预共享密钥校验测试：握手、拒绝伪造/重放报文、配置与命令行入口。"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import ConfigError, default_config, validate
from network import AUTH_ALG, PeerConnection, challenge_mac, verify_mac

PROJECT_DIR = Path(__file__).resolve().parent.parent
MAIN = PROJECT_DIR / "main.py"

SECRET = "k7Qm2pXv9LtR4sYb8HnJ3wZc6DgF1aE0-uI5oT2rM8xS"


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


async def close_writer(writer: Any) -> None:
    try:
        writer.close()
        await asyncio.sleep(0)
    except Exception:
        pass


class ChallengeTests(unittest.TestCase):
    """校验码本身：绑定密钥与两个 nonce。"""

    def test_mac_binds_secret_and_both_nonces(self) -> None:
        base = challenge_mac(SECRET, "peer-nonce", "own-nonce")
        self.assertEqual(len(base), 64)
        self.assertNotEqual(base, challenge_mac(SECRET + "x", "peer-nonce", "own-nonce"))
        self.assertNotEqual(base, challenge_mac(SECRET, "other-nonce", "own-nonce"))
        self.assertNotEqual(base, challenge_mac(SECRET, "peer-nonce", "other-nonce"))
        self.assertTrue(verify_mac(SECRET, "peer-nonce", "own-nonce", base))
        self.assertFalse(verify_mac(SECRET, "other-nonce", "own-nonce", base))
        self.assertFalse(verify_mac(SECRET, "peer-nonce", "own-nonce", ""))
        self.assertFalse(verify_mac(SECRET, "peer-nonce", "own-nonce", base.upper()))


class ConfigTests(unittest.TestCase):
    def test_default_config_has_empty_secret(self) -> None:
        self.assertEqual(default_config()["auth"]["secret"], "")

    def test_validate_warns_on_empty_and_short_secret(self) -> None:
        cfg = default_config()
        self.assertTrue(any("auth.secret 为空" in item for item in validate(cfg)))

        cfg["auth"]["secret"] = "short"
        warnings = validate(cfg)
        self.assertTrue(any("太短" in item for item in warnings))
        self.assertFalse(any("为空" in item for item in warnings))

        cfg["auth"]["secret"] = SECRET
        self.assertFalse(any("auth" in item for item in validate(cfg)))

    def test_validate_rejects_non_string_secret(self) -> None:
        cfg = default_config()
        cfg["auth"]["secret"] = 12345
        with self.assertRaises(ConfigError):
            validate(cfg)


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def _pair(self, secret_a: Optional[str], secret_b: Optional[str]) -> None:
        self.received_a: List[Dict[str, Any]] = []
        self.received_b: List[Dict[str, Any]] = []
        port_a, port_b = free_port(), free_port()
        self.a = PeerConnection(
            "127.0.0.1", port_b, port_a, recorder(self.received_a),
            name="a", identity={"name": "a", "role": "server"},
            ping_interval=0.2, reconnect_interval=0.2, reconnect_max_interval=0.5,
            secret=secret_a,
        )
        self.b = PeerConnection(
            "127.0.0.1", port_a, port_b, recorder(self.received_b),
            name="b", identity={"name": "b", "role": "client"},
            ping_interval=0.2, reconnect_interval=0.2, reconnect_max_interval=0.5,
            secret=secret_b,
        )
        await self.a.start()
        await self.b.start()
        self.addAsyncCleanup(self.b.stop)
        self.addAsyncCleanup(self.a.stop)
        self.assertTrue(await self.a.wait_connected(3.0))
        self.assertTrue(await self.b.wait_connected(3.0))

    async def test_same_secret_authenticates_and_delivers_switch(self) -> None:
        await self._pair(SECRET, SECRET)
        self.assertTrue(await wait_for(lambda: self.a.authenticated and self.b.authenticated))
        self.assertEqual(self.a.peer_auth, AUTH_ALG)
        self.assertEqual(self.b.peer_auth, AUTH_ALG)
        self.assertTrue(self.a.auth_enabled and self.b.auth_enabled)

        await self.a.send({"type": "switch", "target": "mac", "id": "ok-1"})
        self.assertTrue(
            await wait_for(lambda: any(m.get("id") == "ok-1" for m in self.received_b))
        )

    async def test_different_secret_drops_switch_and_ack(self) -> None:
        await self._pair("aaaa-secret-aaaaaaaaaaaa", "bbbb-secret-bbbbbbbbbbbbb")
        self.assertFalse(await wait_for(lambda: self.a.authenticated, 0.6))
        self.assertFalse(await wait_for(lambda: self.b.authenticated, 0.6))

        await self.a.send({"type": "switch", "target": "mac", "id": "evil-1"})
        await self.a.send({"type": "switch_ack", "id": "forged", "ok": True})
        await asyncio.sleep(0.4)
        self.assertFalse(any(m.get("id") in ("evil-1", "forged") for m in self.received_b))

        # 连接本身仍然健在：只有「需要校验」的消息类型被丢弃
        await self.a.send({"type": "ping", "ts": 1})
        self.assertTrue(
            await wait_for(lambda: any(m.get("type") == "ping" for m in self.received_b))
        )

    async def test_secret_only_on_one_side_is_rejected_by_that_side(self) -> None:
        await self._pair(None, SECRET)
        self.assertFalse(self.a.auth_enabled)
        self.assertTrue(self.b.auth_enabled)
        self.assertFalse(await wait_for(lambda: self.b.authenticated, 0.6))
        self.assertIsNone(self.b.peer_auth)  # 对端 hello 没声明校验

        await self.a.send({"type": "switch", "target": "win", "id": "plain-1"})
        await asyncio.sleep(0.4)
        self.assertFalse(any(m.get("id") == "plain-1" for m in self.received_b))

    async def test_open_mode_keeps_working_without_secret(self) -> None:
        await self._pair(None, None)
        self.assertFalse(self.a.authenticated or self.b.authenticated)
        await self.a.send({"type": "switch", "target": "mac", "id": "open-1"})
        self.assertTrue(
            await wait_for(lambda: any(m.get("id") == "open-1" for m in self.received_b))
        )
        # 握手报文不会漏给上层业务处理
        self.assertFalse(
            any(str(m.get("type")).startswith("auth_")
                for m in self.received_a + self.received_b)
        )


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    """抓包者拿旧连接的校验码无法在新连接上通过（质询串每次都是新的）。"""

    async def asyncSetUp(self) -> None:
        self.received: List[Dict[str, Any]] = []
        self.port = free_port()
        self.b = PeerConnection(
            "127.0.0.1", free_port(), self.port, recorder(self.received),
            name="b", identity={"name": "b", "role": "server"},
            ping_interval=30.0, reconnect_interval=0.2, reconnect_max_interval=0.5,
            secret=SECRET,
        )
        await self.b.start()
        self.addAsyncCleanup(self.b.stop)

    async def _raw_session(self, *, accept: bool) -> Dict[str, Any]:
        """手写一个客户端：读 hello 与质询，回一个（可能故意错的）校验码。"""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.addAsyncCleanup(close_writer, writer)
        hello = {
            "type": "hello",
            "name": "attacker",
            "role": "client",
            "version": "1.0.0",
            "platform": "Darwin",
            "listen_port": free_port(),
            "auth": AUTH_ALG,
        }
        own = "attacker-nonce-0123456789abcdef"
        writer.write((json.dumps(hello) + "\n").encode("utf-8"))
        await writer.drain()
        challenge: Dict[str, Any] = {}
        for _ in range(3):  # 被连的一方先发 hello，再发质询
            line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            incoming = json.loads(line.decode("utf-8"))
            if incoming.get("type") == "auth_challenge":
                challenge = incoming
                break
        self.assertEqual(challenge.get("type"), "auth_challenge")
        nonce = challenge["nonce"]
        answered = nonce if accept else "stale-nonce-from-old-session"
        mac = challenge_mac(SECRET, answered, own)
        response = {"type": "auth_response", "nonce": nonce, "mine": own,
                    "alg": AUTH_ALG, "mac": mac}
        message_id = f"s-{nonce[:6]}"
        writer.write((json.dumps(response) + "\n").encode("utf-8"))
        writer.write((json.dumps({"type": "switch", "target": "mac", "id": message_id}) + "\n").encode("utf-8"))
        await writer.drain()
        return {"nonce": nonce, "id": message_id}

    async def test_genuine_response_is_accepted(self) -> None:
        session = await self._raw_session(accept=True)
        self.assertTrue(await wait_for(lambda: self.b.authenticated))
        self.assertTrue(any(m.get("id") == session["id"] for m in self.received))

    async def test_replayed_or_stale_mac_is_rejected(self) -> None:
        first = await self._raw_session(accept=True)
        self.assertTrue(await wait_for(lambda: self.b.authenticated))
        self.assertTrue(any(m.get("id") == first["id"] for m in self.received))

        # 第二条连接：质询串换了，旧校验码（连同旧 nonce 一起搬）也没用
        second = await self._raw_session(accept=False)
        self.assertNotEqual(first["nonce"], second["nonce"])
        await asyncio.sleep(0.5)
        self.assertFalse(any(m.get("id") == second["id"] for m in self.received))


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "QUICKSWITCH_LOG": str(Path(self.tmp.name) / "qs.log")}

    def test_gen_secret_prints_pasteable_config(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(MAIN), "--gen-secret"],
            capture_output=True, text=True, timeout=20, env=self.env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        secret = json.loads(proc.stdout)["auth"]["secret"]
        self.assertGreaterEqual(len(secret), 32)
        self.assertFalse(validate({**default_config(), "auth": {"secret": secret}}))

        again = subprocess.run(
            [sys.executable, str(MAIN), "--gen-secret"],
            capture_output=True, text=True, timeout=20, env=self.env,
        )
        self.assertNotEqual(json.loads(again.stdout)["auth"]["secret"], secret)

    def test_status_never_prints_the_secret(self) -> None:
        config = Path(self.tmp.name) / "config.json"
        port = free_port()
        config.write_text(json.dumps({
            "role": "server",
            "peer_host": "127.0.0.1",
            "peer_port": port,
            "listen_port": port,
            "auth": {"secret": SECRET},
            "ddc": {"m1ddc_path": "/nonexistent/m1ddc"},
        }), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(MAIN), "--status", "--config", str(config)],
            capture_output=True, text=True, timeout=60, env=self.env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("密钥校验", proc.stdout)
        self.assertNotIn(SECRET, proc.stdout)


if __name__ == "__main__":
    unittest.main()
