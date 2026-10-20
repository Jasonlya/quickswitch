"""输入源自动识别测试：候选值探测、交互标注、回读校验、配置写回。"""
from __future__ import annotations

import json
import sys
import tempfile
import typing
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ddc import DDCSwitcher
from main import cmd_detect_inputs, parse_candidates

# 用真实存在的可执行文件占位：FakeMonitor 不会真的执行它，只需通过「工具是否存在」检查
FAKE_TOOL = sys.executable


class FakeMonitor:
    """模拟一台显示器：支持部分输入源、可设置、可按需读回。"""

    def __init__(
        self,
        supported: typing.Iterable[int] = (15, 17),
        initial: int = 17,
        readable: bool = True,
        always_report: Optional[int] = None,
    ) -> None:
        self.state = initial
        self.supported = set(supported)
        self.readable = readable
        self.always_report = always_report
        self.calls: List[List[str]] = []

    def __call__(self, cmd: List[str], timeout: float) -> Tuple[int, str, str]:
        self.calls.append(list(cmd))
        if "/GetValue" in cmd or "getvcp" in cmd or "get" in cmd:
            if not self.readable:
                return (1, "", "")  # 显示器不支持读取 VCP 0x60
            value = self.state if self.always_report is None else self.always_report
            if "/GetValue" in cmd:
                return (0, f"Current Value : {value}", "")
            return (0, f"{value}\n", "")
        value = int(cmd[-1])
        if value not in self.supported:
            return (1, "", "unsupported input")
        self.state = value
        return (0, "", "")


def make_cfg(**ddc_overrides: Any) -> Dict[str, Any]:
    ddc: Dict[str, Any] = {
        "controlmyMonitor_path": r"C:\Tools\ControlMyMonitor.exe",
        "monitor_id": r"\\.\DISPLAY1\Monitor0",
        "m1ddc_path": "/opt/homebrew/bin/m1ddc",
        "display_index": 1,
        "vcp_code": "0x60",
        "mac_input": 15,
        "win_input": 17,
        "command_template": None,
        "read_template": None,
        "read_regex": None,
        "settle": 0,
        "timeout": 5.0,
        "retries": 0,
        "retry_delay": 0,
    }
    ddc.update(ddc_overrides)
    return {"role": "server", "ddc": ddc}


def scripted(*answers: str):
    remaining = list(answers)

    def prompt(question: str) -> str:
        return remaining.pop(0) if remaining else ""

    return prompt


class ProbeTests(unittest.TestCase):
    def test_probe_finds_supported_values_and_restores_input(self) -> None:
        monitor = FakeMonitor(supported=[15, 17], initial=17)
        switcher = DDCSwitcher(make_cfg(), system="Windows", runner=monitor)
        seen: List[Tuple[int, Optional[int]]] = []
        available = switcher.probe_inputs([1, 15, 17, 27], progress=lambda v, r: seen.append((v, r)))
        self.assertEqual(available, [15, 17])
        self.assertEqual(monitor.state, 17)  # 结束后恢复原输入源
        self.assertEqual([value for value, _ in seen], [1, 15, 17, 27])
        self.assertEqual(dict(seen)[15], 15)
        self.assertIsNone(dict(seen)[27])

    def test_probe_returns_empty_when_not_readable(self) -> None:
        monitor = FakeMonitor(readable=False)
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=monitor)
        self.assertEqual(switcher.probe_inputs([15, 17]), [])

    def test_probe_uses_default_candidates(self) -> None:
        monitor = FakeMonitor(supported=[27], initial=27)
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=monitor)
        self.assertEqual(switcher.probe_inputs(), [27])


class WizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.json"
        self.path.write_text(json.dumps(make_cfg()), encoding="utf-8")

    def read_config(self) -> Dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_interactive_flow_labels_and_writes_config(self) -> None:
        cfg = make_cfg(m1ddc_path=FAKE_TOOL)
        monitor = FakeMonitor(supported=[15, 17], initial=17)
        switcher = DDCSwitcher(cfg, system="Darwin", runner=monitor)
        # 先看到输入源 15 是 Mac 桌面，再看到 17 是 Windows 桌面
        code = cmd_detect_inputs(
            cfg, self.path, ddc=switcher, prompt=scripted("2", "1"), write=True
        )
        self.assertEqual(code, 0)
        saved = self.read_config()
        self.assertEqual(saved["ddc"]["mac_input"], 15)
        self.assertEqual(saved["ddc"]["win_input"], 17)
        self.assertEqual(monitor.state, 17)

    def test_explicit_values_skip_questions(self) -> None:
        cfg = make_cfg(m1ddc_path=FAKE_TOOL)
        monitor = FakeMonitor(supported=[15, 17], initial=17)
        switcher = DDCSwitcher(cfg, system="Darwin", runner=monitor)

        def fail(question: str) -> str:
            raise AssertionError(f"不应提问：{question}")

        code = cmd_detect_inputs(
            cfg, self.path, mac_input=15, win_input=17, ddc=switcher, prompt=fail, write=True
        )
        self.assertEqual(code, 0)
        saved = self.read_config()
        self.assertEqual(saved["ddc"]["mac_input"], 15)
        self.assertEqual(saved["ddc"]["win_input"], 17)

    def test_unreadable_monitor_without_values_fails_with_hint(self) -> None:
        cfg = make_cfg(m1ddc_path=FAKE_TOOL)
        monitor = FakeMonitor(readable=False)
        switcher = DDCSwitcher(cfg, system="Darwin", runner=monitor)
        code = cmd_detect_inputs(cfg, self.path, ddc=switcher, prompt=scripted(), write=True)
        self.assertEqual(code, 1)
        saved = self.read_config()
        self.assertEqual(saved["ddc"]["mac_input"], 15)  # 未写入新值
        self.assertEqual(saved["ddc"]["win_input"], 17)

    def test_no_write_keeps_config(self) -> None:
        cfg = make_cfg(m1ddc_path=FAKE_TOOL)
        monitor = FakeMonitor()
        switcher = DDCSwitcher(cfg, system="Darwin", runner=monitor)
        code = cmd_detect_inputs(
            cfg, self.path, mac_input=15, win_input=17, ddc=switcher,
            prompt=scripted(), write=False,
        )
        self.assertEqual(code, 0)
        saved = self.read_config()
        self.assertEqual(saved["ddc"]["mac_input"], 15)
        self.assertEqual(saved["ddc"]["win_input"], 17)

    def test_mismatched_readback_returns_error_but_saves(self) -> None:
        # 显示器只支持 15，但用户坚持把 17 当作 Windows 输入源
        cfg = make_cfg(m1ddc_path=FAKE_TOOL)
        monitor = FakeMonitor(supported=[15], initial=15, always_report=15)
        switcher = DDCSwitcher(cfg, system="Darwin", runner=monitor)
        code = cmd_detect_inputs(
            cfg, self.path, mac_input=15, win_input=17, ddc=switcher, prompt=scripted(), write=True
        )
        self.assertEqual(code, 1)
        saved = self.read_config()
        self.assertEqual(saved["ddc"]["win_input"], 17)

    def test_same_value_for_both_targets_is_rejected(self) -> None:
        cfg = make_cfg(m1ddc_path=FAKE_TOOL)
        monitor = FakeMonitor(supported=[15], initial=15)
        switcher = DDCSwitcher(cfg, system="Darwin", runner=monitor)
        code = cmd_detect_inputs(
            cfg, self.path, mac_input=15, win_input=15, ddc=switcher, prompt=scripted(), write=True
        )
        self.assertEqual(code, 1)
        self.assertEqual(self.read_config()["ddc"]["mac_input"], 15)

    def test_unavailable_tool_exits_without_values(self) -> None:
        cfg = make_cfg(m1ddc_path="/nonexistent/m1ddc")
        switcher = DDCSwitcher(cfg, system="Darwin", runner=FakeMonitor())
        code = cmd_detect_inputs(cfg, self.path, ddc=switcher, prompt=scripted(), write=True)
        self.assertEqual(code, 1)

    def test_candidates_argument_is_used(self) -> None:
        cfg = make_cfg(m1ddc_path=FAKE_TOOL)
        monitor = FakeMonitor(supported=[208], initial=208)
        switcher = DDCSwitcher(cfg, system="Darwin", runner=monitor)
        code = cmd_detect_inputs(
            cfg, self.path, candidates=[208], mac_input=208, win_input=15,
            ddc=switcher, prompt=scripted(), write=True,
        )
        self.assertEqual(code, 1)  # 15 不在候选中且显示器不接受 → 回读失败
        probed = {call[-1] for call in monitor.calls if call[-1].isdigit()}
        self.assertIn("208", probed)


class CandidateParsingTests(unittest.TestCase):
    def test_parses_comma_and_space_separated(self) -> None:
        self.assertEqual(parse_candidates("15,17"), [15, 17])
        self.assertEqual(parse_candidates("15 17 27"), [15, 17, 27])
        self.assertEqual(parse_candidates("0x0f,0x11"), [15, 17])
        self.assertIsNone(parse_candidates(""))
        self.assertIsNone(parse_candidates(None))

    def test_rejects_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_candidates("999")


if __name__ == "__main__":
    unittest.main()
