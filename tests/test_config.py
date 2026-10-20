"""配置模块测试：默认值生成、深度合并、校验与环境变量覆盖。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config as config_module


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        patcher = mock.patch.dict(os.environ, {"QUICKSWITCH_CONFIG": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_default_config_on_first_run(self) -> None:
        cfg = config_module.load_config()
        self.assertTrue(self.path.exists())
        self.assertEqual(cfg["role"], "server")
        self.assertEqual(cfg["ddc"]["mac_input"], 15)
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written, config_module.DEFAULTS)

    def test_partial_override_keeps_defaults(self) -> None:
        self.path.write_text(
            json.dumps({"role": "client", "ddc": {"mac_input": 27}}), encoding="utf-8"
        )
        cfg = config_module.load_config()
        self.assertEqual(cfg["role"], "client")
        self.assertEqual(cfg["ddc"]["mac_input"], 27)
        self.assertEqual(cfg["ddc"]["win_input"], 17)
        self.assertEqual(cfg["hotkeys"]["switch_to_mac"], "ctrl+alt+1")
        self.assertEqual(cfg["listen_port"], 8765)

    def test_environment_override(self) -> None:
        with mock.patch.dict(os.environ, {"QUICKSWITCH_ROLE": "client"}):
            cfg = config_module.load_config()
        self.assertEqual(cfg["role"], "client")

    def test_invalid_role_raises(self) -> None:
        self.path.write_text(json.dumps({"role": "windows"}), encoding="utf-8")
        with self.assertRaises(config_module.ConfigError):
            config_module.load_config()

    def test_invalid_port_raises(self) -> None:
        self.path.write_text(json.dumps({"peer_port": 70000}), encoding="utf-8")
        with self.assertRaises(config_module.ConfigError):
            config_module.load_config()

    def test_invalid_input_value_raises(self) -> None:
        self.path.write_text(json.dumps({"ddc": {"win_input": "hdmi1"}}), encoding="utf-8")
        with self.assertRaises(config_module.ConfigError):
            config_module.load_config()

    def test_broken_json_raises(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(config_module.ConfigError):
            config_module.load_config()

    def test_identical_inputs_warns(self) -> None:
        self.path.write_text(
            json.dumps({"ddc": {"mac_input": 17, "win_input": 17}}), encoding="utf-8"
        )
        with self.assertLogs("config", level="WARNING") as captured:
            config_module.load_config()
        self.assertTrue(any("相同" in line for line in captured.output))

    def test_auto_peer_without_discovery_warns(self) -> None:
        cfg = config_module.default_config()
        cfg["peer_host"] = "auto"
        warnings = config_module.validate(cfg)
        self.assertTrue(any("discovery" in warning for warning in warnings))

        cfg["discovery"]["enabled"] = True
        warnings = config_module.validate(cfg)
        self.assertEqual([w for w in warnings if "discovery" in w], [])

    def test_same_hotkey_raises_for_server(self) -> None:
        cfg = config_module.default_config()
        cfg["hotkeys"]["switch_to_win"] = cfg["hotkeys"]["switch_to_mac"]
        with self.assertRaises(config_module.ConfigError):
            config_module.validate(cfg)

    def test_client_role_skips_hotkey_validation(self) -> None:
        cfg = config_module.default_config()
        cfg["role"] = "client"
        cfg["auth"]["secret"] = "x" * 43  # 密钥提示另有用例，这里只验证热键不再报错
        cfg["hotkeys"]["switch_to_mac"] = cfg["hotkeys"]["switch_to_win"] = ""
        self.assertEqual(config_module.validate(cfg), [])

    def test_deep_merge_does_not_mutate_inputs(self) -> None:
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": {"b": 9}}
        merged = config_module.deep_merge(base, override)
        self.assertEqual(merged, {"a": {"b": 9, "c": 2}})
        self.assertEqual(base, {"a": {"b": 1, "c": 2}})
        self.assertEqual(override, {"a": {"b": 9}})


if __name__ == "__main__":
    unittest.main()
