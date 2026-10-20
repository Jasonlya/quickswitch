"""DDC 模块测试：命令构建、重试/超时、读取解析、dry-run。"""
from __future__ import annotations

import subprocess
import unittest
from typing import Any, Dict, List, Tuple

from ddc import DDCSwitcher, DDCError, parse_control_my_monitor, parse_ddcutil


def make_cfg(**ddc_overrides: Any) -> Dict[str, Any]:
    ddc = {
        "controlmyMonitor_path": r"C:\Tools\ControlMyMonitor.exe",
        "monitor_id": r"\\.\DISPLAY1\Monitor0",
        "m1ddc_path": "/opt/homebrew/bin/m1ddc",
        "ddcutil_path": "ddcutil",
        "display_index": 2,
        "vcp_code": "0x60",
        "mac_input": 15,
        "win_input": 17,
        "command_template": None,
        "timeout": 5.0,
        "retries": 0,
        "retry_delay": 0,
    }
    ddc.update(ddc_overrides)
    return {"role": "server", "ddc": ddc}


class FakeRunner:
    """可编程的外部命令执行器，用于替代真实工具。"""

    def __init__(self, results: List[Any]) -> None:
        self.results = list(results)
        self.calls: List[Tuple[List[str], float]] = []

    def __call__(self, cmd: List[str], timeout: float) -> Tuple[int, str, str]:
        self.calls.append((list(cmd), timeout))
        if not self.results:
            raise AssertionError("runner 被调用的次数多于预期")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class CommandBuildTests(unittest.TestCase):
    def test_windows_command_uses_setvalue(self) -> None:
        switcher = DDCSwitcher(make_cfg(), system="Windows", runner=FakeRunner([]))
        self.assertEqual(
            switcher.build_command(15),
            [
                r"C:\Tools\ControlMyMonitor.exe",
                "/SetValue",
                r"\\.\DISPLAY1\Monitor0",
                "0x60",
                "15",
            ],
        )

    def test_macos_command(self) -> None:
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=FakeRunner([]))
        self.assertEqual(
            switcher.build_command(15),
            ["/opt/homebrew/bin/m1ddc", "display", "2", "set", "input", "15"],
        )

    def test_linux_command(self) -> None:
        switcher = DDCSwitcher(make_cfg(), system="Linux", runner=FakeRunner([]))
        self.assertEqual(
            switcher.build_command(17),
            ["ddcutil", "--display", "2", "setvcp", "60", "17"],
        )

    def test_custom_template_as_string(self) -> None:
        cfg = make_cfg(command_template="{tool} --input {value}")
        switcher = DDCSwitcher(cfg, system="Windows", runner=FakeRunner([]))
        self.assertEqual(
            switcher.build_command(17), [r"C:\Tools\ControlMyMonitor.exe", "--input", "17"]
        )

    def test_custom_template_as_list(self) -> None:
        cfg = make_cfg(command_template=["mytool", "set", "0x60", "{value}"])
        switcher = DDCSwitcher(cfg, system="Windows", runner=FakeRunner([]))
        self.assertEqual(switcher.build_command(15), ["mytool", "set", "0x60", "15"])

    def test_unknown_placeholder_raises(self) -> None:
        cfg = make_cfg(command_template=["{tool}", "{nope}"])
        switcher = DDCSwitcher(cfg, system="Windows", runner=FakeRunner([]))
        with self.assertRaises(DDCError):
            switcher.build_command(15)

    def test_unsupported_system_without_template_raises(self) -> None:
        switcher = DDCSwitcher(make_cfg(), system="SunOS", runner=FakeRunner([]))
        with self.assertRaises(DDCError):
            switcher.build_command(15)
        self.assertFalse(switcher.set_input(15))


class ExecutionTests(unittest.TestCase):
    def test_switch_to_mac_runs_expected_command(self) -> None:
        runner = FakeRunner([(0, "", "")])
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=runner)
        self.assertTrue(switcher.switch_to_mac())
        self.assertEqual(runner.calls[0][0][-1], "15")

    def test_switch_to_win_runs_expected_value(self) -> None:
        runner = FakeRunner([(0, "", "")])
        switcher = DDCSwitcher(make_cfg(), system="Windows", runner=runner)
        self.assertTrue(switcher.switch_to_win())
        self.assertEqual(runner.calls[0][0][-1], "17")

    def test_retries_then_succeeds(self) -> None:
        runner = FakeRunner([(1, "", "DDC busy"), (1, "", "DDC busy"), (0, "", "")])
        switcher = DDCSwitcher(make_cfg(retries=2), system="Darwin", runner=runner)
        self.assertTrue(switcher.set_input(15))
        self.assertEqual(len(runner.calls), 3)

    def test_fails_after_all_retries(self) -> None:
        runner = FakeRunner([(1, "", "err"), (1, "", "err")])
        switcher = DDCSwitcher(make_cfg(retries=1), system="Darwin", runner=runner)
        self.assertFalse(switcher.set_input(15))
        self.assertEqual(len(runner.calls), 2)

    def test_missing_tool_is_reported_not_raised(self) -> None:
        runner = FakeRunner([FileNotFoundError("m1ddc")])
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=runner)
        self.assertFalse(switcher.set_input(15))

    def test_timeout_then_success(self) -> None:
        runner = FakeRunner([subprocess.TimeoutExpired("m1ddc", 5), (0, "", "")])
        switcher = DDCSwitcher(make_cfg(retries=1), system="Darwin", runner=runner)
        self.assertTrue(switcher.set_input(15))

    def test_dry_run_does_not_execute(self) -> None:
        runner = FakeRunner([])
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=runner, dry_run=True)
        self.assertTrue(switcher.set_input(15))
        self.assertTrue(switcher.switch_to_win())
        self.assertEqual(runner.calls, [])

    def test_timeout_override_and_retries_override(self) -> None:
        runner = FakeRunner([(1, "", "err")])
        switcher = DDCSwitcher(
            make_cfg(retries=5, timeout=99.0), system="Darwin", runner=runner, retries=0, timeout=1.5
        )
        self.assertFalse(switcher.set_input(15))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][1], 1.5)

    def test_switch_target_dispatch(self) -> None:
        runner = FakeRunner([(0, "", ""), (0, "", "")])
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=runner)
        switcher.switch_target("mac")
        switcher.switch_target("win")
        self.assertEqual([call[0][-1] for call in runner.calls], ["15", "17"])
        with self.assertRaises(ValueError):
            switcher.switch_target("linux")

    def test_tool_available_for_absolute_path(self) -> None:
        switcher = DDCSwitcher(make_cfg(), system="Darwin")
        ok, detail = switcher.tool_available()
        self.assertFalse(ok)
        self.assertIn("/opt/homebrew/bin/m1ddc", detail)

    def test_tool_available_for_missing_command(self) -> None:
        switcher = DDCSwitcher(make_cfg(ddcutil_path="definitely-not-a-command"), system="Linux")
        ok, detail = switcher.tool_available()
        self.assertFalse(ok)
        self.assertIn("PATH", detail)


class InputReadTests(unittest.TestCase):
    CONTROL_MY_MONITOR_OUTPUT = (
        "Monitor Name: \\\\.\\DISPLAY1\\Monitor0\n"
        "VCP Code     : 0x60\n"
        "Current Value : 0x11\n"
        "Maximum Value : 0xFF\n"
    )

    def test_get_input_windows(self) -> None:
        runner = FakeRunner([(0, self.CONTROL_MY_MONITOR_OUTPUT, ""), (0, self.CONTROL_MY_MONITOR_OUTPUT, "")])
        switcher = DDCSwitcher(make_cfg(), system="Windows", runner=runner)
        self.assertEqual(switcher.get_input(), 17)
        self.assertEqual(switcher.current_target(), "win")

    def test_get_input_linux_terse(self) -> None:
        runner = FakeRunner([(0, "VCP 60 SNC 0x000f", ""), (0, "VCP 60 SNC 0x000f", "")])
        switcher = DDCSwitcher(make_cfg(), system="Linux", runner=runner)
        self.assertEqual(switcher.get_input(), 15)
        self.assertEqual(switcher.current_target(), "mac")

    def test_get_input_unknown_value_returns_none_target(self) -> None:
        # 27 = USB-C，未在配置中登记 → 能读到数值但无法判定目标
        runner = FakeRunner([(0, "VCP 60 SNC 0x001b", ""), (0, "VCP 60 SNC 0x001b", "")])
        switcher = DDCSwitcher(make_cfg(), system="Linux", runner=runner)
        self.assertEqual(switcher.get_input(), 27)
        self.assertIsNone(switcher.current_target())

    def test_get_input_macos_uses_m1ddc_get_input(self) -> None:
        runner = FakeRunner([(0, "17\n", "")])
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=runner)
        self.assertEqual(switcher.get_input(), 17)
        self.assertEqual(
            runner.calls[0][0],
            ["/opt/homebrew/bin/m1ddc", "display", "2", "get", "input"],
        )

    def test_get_input_macos_returns_none_when_monitor_cannot_read(self) -> None:
        # 显示器不支持读取 VCP 0x60 时，m1ddc 以非零码退出且没有输出
        runner = FakeRunner([(1, "", ""), (1, "", "")])
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=runner)
        self.assertIsNone(switcher.get_input())
        self.assertIsNone(switcher.current_target())

    def test_get_input_macos_negative_value_is_ignored(self) -> None:
        runner = FakeRunner([(0, "-1\n", "")])
        switcher = DDCSwitcher(make_cfg(), system="Darwin", runner=runner)
        self.assertIsNone(switcher.get_input())

    def test_custom_read_template_and_regex(self) -> None:
        cfg = make_cfg(read_template=["mytool", "read", "{vcp}"], read_regex=r"input=(\d+)")
        runner = FakeRunner([(0, "input=15\n", ""), (0, "input=15\n", "")])
        switcher = DDCSwitcher(cfg, system="Darwin", runner=runner)
        self.assertEqual(switcher.get_input(), 15)
        self.assertEqual(runner.calls[0][0], ["mytool", "read", "0x60"])
        self.assertEqual(switcher.current_target(), "mac")

    def test_read_regex_can_parse_hex(self) -> None:
        cfg = make_cfg(read_regex=r"value:\s*(0x[0-9a-f]+)")
        runner = FakeRunner([(0, "value: 0x11", "")])
        switcher = DDCSwitcher(cfg, system="Linux", runner=runner)
        self.assertEqual(switcher.get_input(), 17)

    def test_read_regex_without_match_returns_none(self) -> None:
        cfg = make_cfg(read_regex=r"input=(\d+)")
        runner = FakeRunner([(0, "unexpected output", "")])
        switcher = DDCSwitcher(cfg, system="Linux", runner=runner)
        self.assertIsNone(switcher.get_input())

    def test_unsupported_system_without_read_template(self) -> None:
        runner = FakeRunner([])
        switcher = DDCSwitcher(make_cfg(), system="SunOS", runner=runner)
        self.assertIsNone(switcher.get_input())
        self.assertEqual(runner.calls, [])

    def test_get_input_failure_returns_none(self) -> None:
        runner = FakeRunner([(1, "", "no display")])
        switcher = DDCSwitcher(make_cfg(), system="Windows", runner=runner)
        self.assertIsNone(switcher.get_input())

    def test_parse_helpers(self) -> None:
        self.assertEqual(parse_control_my_monitor("Current Value : 15"), 15)
        self.assertEqual(parse_control_my_monitor("current value : 0x0f"), 15)
        self.assertEqual(parse_control_my_monitor("some output 42"), 42)
        self.assertIsNone(parse_control_my_monitor("nothing here"))
        self.assertEqual(parse_ddcutil("VCP 60 SNC 0x0011"), 17)
        self.assertIsNone(parse_ddcutil("no value"))


if __name__ == "__main__":
    unittest.main()
