"""命令行冒烟测试：用真实子进程验证 --status / --print-config / --once / --version。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
MAIN = PROJECT_DIR / "main.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "role": "server",
                    "peer_host": "127.0.0.1",
                    "peer_port": free_port(),
                    "listen_port": free_port(),
                    "network": {"connect_wait": 0.2, "ack_timeout": 0.3},
                    "ddc": {"mac_input": 15, "win_input": 17},
                }
            ),
            encoding="utf-8",
        )
        self.env = dict(os.environ, QUICKSWITCH_CONFIG=str(self.config))

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(MAIN), "--config", str(self.config), *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(PROJECT_DIR),
            timeout=90,
        )

    def test_version(self) -> None:
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("QuickSwitch", result.stdout)

    def test_print_config_merges_defaults(self) -> None:
        result = self.run_cli("--print-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        cfg = json.loads(result.stdout)
        self.assertEqual(cfg["role"], "server")
        self.assertEqual(cfg["ddc"]["mac_input"], 15)
        self.assertEqual(cfg["ddc"]["win_input"], 17)
        self.assertEqual(cfg["hotkeys"]["switch_to_mac"], "ctrl+alt+1")  # 默认值补齐

    def test_status_reports_tools_and_peer(self) -> None:
        result = self.run_cli("--status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("QuickSwitch", result.stdout)
        self.assertIn("role         : server", result.stdout)
        self.assertIn("DDC 工具", result.stdout)
        self.assertIn("当前输入源", result.stdout)

    def test_init_config_writes_file(self) -> None:
        target = Path(self.tmp.name) / "nested" / "generated.json"
        result = subprocess.run(
            [sys.executable, str(MAIN), "--config", str(target), "--init-config"],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_DIR),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(target.exists())
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["role"], "server")

    def test_once_dry_run_succeeds(self) -> None:
        result = self.run_cli("--once", "mac", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("切换到 Mac：成功", result.stdout)

    def test_invalid_config_exits_with_code_2(self) -> None:
        self.config.write_text(json.dumps({"role": "windows"}), encoding="utf-8")
        result = self.run_cli("--status")
        self.assertEqual(result.returncode, 2)
        self.assertIn("配置错误", result.stderr)

    def test_broken_json_exits_with_code_2(self) -> None:
        self.config.write_text("{ nope", encoding="utf-8")
        result = self.run_cli("--status")
        self.assertEqual(result.returncode, 2)
        self.assertIn("配置错误", result.stderr)

    def test_role_override_from_cli(self) -> None:
        result = self.run_cli("--role", "client", "--print-config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["role"], "client")


if __name__ == "__main__":
    unittest.main()
