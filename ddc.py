"""DDC/CI 输入源切换封装。

内置命令模板（VCP 0x60 = Input Source）：

- Windows：``ControlMyMonitor.exe /SetValue <monitor_id> 0x60 <value>``
- macOS  ：``m1ddc display <index> set input <value>``
- Linux  ：``ddcutil --display <index> setvcp 60 <value>``

如显示器/工具需要别的调用方式，可用配置 ``ddc.command_template`` 覆盖模板，
模板中可用占位符：``{tool}`` ``{monitor}`` ``{vcp}`` ``{display_index}`` ``{value}``。
"""
from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("ddc")

VCP_INPUT = "0x60"

# VCP 0x60 常见取值：VGA1=1 DVI1=3 DP1=15 DP2=16 HDMI1=17 HDMI2=18 USB-C=27
DEFAULT_INPUT_CANDIDATES: List[int] = [1, 3, 15, 16, 17, 18, 27]

SET_TEMPLATES: Dict[str, List[str]] = {
    "Windows": ["{tool}", "/SetValue", "{monitor}", "{vcp}", "{value}"],
    "Darwin": ["{tool}", "display", "{display_index}", "set", "input", "{value}"],
    "Linux": ["{tool}", "--display", "{display_index}", "setvcp", "60", "{value}"],
}

GET_TEMPLATES: Dict[str, List[str]] = {
    "Windows": ["{tool}", "/GetValue", "{monitor}", "{vcp}"],
    # m1ddc 官方 README 未列出，但源码 attrCodeFromCommand 支持 input，
    # 且非 set 指令都会先读取当前值，因此 `m1ddc display N get input` 可用；
    # 若显示器不支持读取 VCP 0x60，命令会返回失败 → 视为「读不到」。
    "Darwin": ["{tool}", "display", "{display_index}", "get", "input"],
    "Linux": ["{tool}", "--display", "{display_index}", "getvcp", "60", "--terse"],
}

TOOL_KEYS: Dict[str, str] = {
    "Windows": "controlmyMonitor_path",
    "Darwin": "m1ddc_path",
    "Linux": "ddcutil_path",
}

FALLBACK_TOOLS: Dict[str, str] = {
    "Windows": "ControlMyMonitor.exe",
    "Darwin": "m1ddc",
    "Linux": "ddcutil",
}

SUPPORTED_SYSTEMS = tuple(SET_TEMPLATES)

# runner(cmd, timeout) -> (returncode, stdout, stderr)
Runner = Callable[[List[str], float], Tuple[int, str, str]]

CMM_VALUE_RE = re.compile(r"current\s+value\s*:\s*(0x[0-9a-fA-F]+|\d+)", re.IGNORECASE)
DDCUTIL_VALUE_RE = re.compile(r"0x([0-9a-fA-F]{1,4})")


class DDCError(Exception):
    """DDC 配置/环境问题，例如当前系统不受支持。"""


def run_command(cmd: List[str], timeout: float) -> Tuple[int, str, str]:
    """执行外部命令并返回 (returncode, stdout, stderr)，不抛 CalledProcessError。"""
    completed = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        text=True,
        errors="replace",
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _pretty(cmd: List[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in cmd)


def _to_int(text: str) -> Optional[int]:
    text = (text or "").strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def _as_command_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return value.split()
    return [str(part) for part in value]


def _first_number(output: str) -> Optional[int]:
    match = re.search(r"(0x[0-9a-fA-F]+|\d+)", output or "")
    return _to_int(match.group(1)) if match else None


def parse_control_my_monitor(output: str) -> Optional[int]:
    """解析 ControlMyMonitor ``/GetValue`` 的输出，取 Current Value。"""
    match = CMM_VALUE_RE.search(output or "")
    if match:
        return _to_int(match.group(1))
    numbers = re.findall(r"(0x[0-9a-fA-F]+|\d+)", output or "")
    return _to_int(numbers[-1]) if numbers else None


def parse_ddcutil(output: str) -> Optional[int]:
    """解析 ``ddcutil getvcp 60 --terse`` 形如 ``VCP 60 SNC 0x000f`` 的输出。"""
    match = DDCUTIL_VALUE_RE.search(output or "")
    return int(match.group(1), 16) if match else None


def parse_plain_number(output: str) -> Optional[int]:
    """解析 m1ddc 这类只打印一个整数的输出（``17\\n``）。"""
    match = re.search(r"^\s*(-?\d+)\s*$", output or "")
    if not match:
        return None
    value = int(match.group(1))
    return value if value >= 0 else None


class DDCSwitcher:
    """按当前系统调用对应 DDC/CI 工具切换显示器输入源。"""

    def __init__(
        self,
        cfg: Dict[str, Any],
        *,
        dry_run: bool = False,
        system: Optional[str] = None,
        runner: Optional[Runner] = None,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
    ) -> None:
        self.cfg = cfg
        self.ddc_cfg: Dict[str, Any] = cfg.get("ddc") or {}
        self.dry_run = dry_run
        self.system = system or platform.system()
        self._runner: Runner = runner or run_command
        self._timeout_override = timeout
        self._retries_override = retries
        self._quiet = False

    # ---------------------------------------------------------------- 基本属性
    @property
    def timeout(self) -> float:
        if self._timeout_override is not None:
            return float(self._timeout_override)
        return float(self.ddc_cfg.get("timeout", 10.0))

    @property
    def retries(self) -> int:
        if self._retries_override is not None:
            return max(0, int(self._retries_override))
        return max(0, int(self.ddc_cfg.get("retries", 0) or 0))

    @property
    def retry_delay(self) -> float:
        return float(self.ddc_cfg.get("retry_delay", 0.4) or 0)

    @property
    def settle(self) -> float:
        """切换输入源后等待显示器稳定的时间（秒），自动识别时会用到。"""
        return float(self.ddc_cfg.get("settle", 1.2) or 0)

    @property
    def mac_input(self) -> int:
        return int(self.ddc_cfg.get("mac_input", 15))

    @property
    def win_input(self) -> int:
        return int(self.ddc_cfg.get("win_input", 17))

    def tool_path(self) -> str:
        key = TOOL_KEYS.get(self.system, "")
        configured = self.ddc_cfg.get(key) if key else None
        return str(configured or FALLBACK_TOOLS.get(self.system, ""))

    # ---------------------------------------------------------------- 命令构建
    def placeholders(self, value: int) -> Dict[str, str]:
        return {
            "tool": self.tool_path(),
            "monitor": str(self.ddc_cfg.get("monitor_id") or ""),
            "vcp": str(self.ddc_cfg.get("vcp_code") or VCP_INPUT),
            "display_index": str(self.ddc_cfg.get("display_index", 1)),
            "value": str(int(value)),
        }

    def _template(self, *, get: bool) -> List[str]:
        if get:
            custom_read = self.ddc_cfg.get("read_template")
            if custom_read:
                return _as_command_list(custom_read)
            return list(GET_TEMPLATES.get(self.system, []))
        custom = self.ddc_cfg.get("command_template")
        if custom:
            return _as_command_list(custom)
        return list(SET_TEMPLATES.get(self.system, []))

    def build_command(self, value: int, *, get: bool = False) -> List[str]:
        template = self._template(get=get)
        if not template:
            raise DDCError(
                f"当前系统 {self.system} 没有内置命令模板"
                f"（支持：{'、'.join(SUPPORTED_SYSTEMS)}），请配置 ddc.command_template"
            )
        placeholders = self.placeholders(value)
        try:
            return [part.format(**placeholders) for part in template]
        except KeyError as exc:
            raise DDCError(f"ddc.command_template 使用了未知占位符 {exc}") from exc

    # ---------------------------------------------------------------- 执行
    def _execute(self, cmd: List[str], *, what: str) -> Tuple[bool, str]:
        attempts = self.retries + 1
        detail = ""
        for attempt in range(1, attempts + 1):
            try:
                returncode, stdout, stderr = self._runner(cmd, self.timeout)
            except FileNotFoundError:
                log.error(
                    "找不到 DDC 工具：%s\n"
                    "  请检查配置 ddc.%s 是否指向正确的可执行文件路径",
                    cmd[0],
                    TOOL_KEYS.get(self.system, "tool"),
                )
                return False, "工具不存在"
            except subprocess.TimeoutExpired:
                detail = f"命令超时（>{self.timeout:g}s）"
            except OSError as exc:
                log.error("无法执行 %s：%s", cmd[0], exc)
                return False, str(exc)
            else:
                if returncode == 0:
                    if attempt > 1:
                        log.info("%s 成功（第 %s 次尝试）", what, attempt)
                    return True, (stdout or "").strip()
                detail = ((stderr or "").strip() or (stdout or "").strip() or f"返回码 {returncode}")

            if attempt < attempts:
                log.log(
                    logging.DEBUG if self._quiet else logging.WARNING,
                    "%s 第 %s/%s 次失败：%s，%.1fs 后重试",
                    what,
                    attempt,
                    attempts,
                    detail,
                    self.retry_delay,
                )
                if self.retry_delay:
                    time.sleep(self.retry_delay)

        log.log(
            logging.DEBUG if self._quiet else logging.ERROR,
            "%s 失败：%s\n  执行的命令：%s",
            what,
            detail,
            _pretty(cmd),
        )
        return False, detail

    def set_input(self, value: int) -> bool:
        """把显示器输入源切换到指定的 VCP 0x60 值。"""
        try:
            cmd = self.build_command(value)
        except DDCError as exc:
            log.error("%s", exc)
            return False

        what = f"切换输入源为 {value}"
        if self.dry_run:
            log.info("[dry-run] %s：%s", what, _pretty(cmd))
            return True
        ok, _ = self._execute(cmd, what=what)
        return ok

    def get_input(self) -> Optional[int]:
        """读取当前输入源；工具不支持读取时返回 None。"""
        if self.system not in GET_TEMPLATES and not self.ddc_cfg.get("read_template"):
            log.debug("%s 上的 %s 没有内置读取命令（可配置 ddc.read_template）", self.system, self.tool_path())
            return None
        try:
            cmd = self.build_command(0, get=True)
        except DDCError as exc:
            log.debug("读取命令不可用：%s", exc)
            return None

        if self.dry_run:
            log.info("[dry-run] 读取当前输入源：%s", _pretty(cmd))
            return None

        ok, output = self._execute(cmd, what="读取当前输入源")
        if not ok:
            return None
        return self.parse_input(output)

    def parse_input(self, output: str) -> Optional[int]:
        """解析读取命令的输出：支持配置正则，否则按平台约定解析。"""
        pattern = self.ddc_cfg.get("read_regex")
        if pattern:
            match = re.search(pattern, output or "")
            if not match:
                log.debug("ddc.read_regex 未匹配到内容：%r", (output or "")[:200])
                return None
            text = match.group(1) if match.groups() else match.group(0)
            return _to_int(text)
        if self.ddc_cfg.get("read_template"):
            return parse_plain_number(output) if output.strip().isdigit() else _first_number(output)
        if self.system == "Windows":
            return parse_control_my_monitor(output)
        if self.system == "Darwin":
            # m1ddc 只会输出一个整数（失败时不输出）；不做模糊匹配，避免误判
            return parse_plain_number(output)
        if self.system == "Linux":
            return parse_ddcutil(output)
        return _first_number(output)

    def probe_inputs(
        self,
        candidates: Optional[List[int]] = None,
        *,
        settle: Optional[float] = None,
        progress: Optional[Callable[[int, Optional[int]], None]] = None,
    ) -> List[int]:
        """逐个试探候选输入源，回读确认哪些被显示器接受；结束后恢复原输入源。

        需要「既能写也能读」的工具（Windows/Linux/macOS 均可，取决于显示器是否
        支持读取 VCP 0x60）。返回被接受的值列表。
        """
        values = list(candidates or DEFAULT_INPUT_CANDIDATES)
        delay = self.settle if settle is None else settle
        previous_retries, previous_quiet = self._retries_override, self._quiet
        # 探测会大量试探「显示器不支持的取值」，因此静默失败并关闭重试：
        # 结果由 progress 回调逐项汇报，日志里不再刷 ERROR。
        self._retries_override = 0
        self._quiet = True
        try:
            original = self.get_input()
            available: List[int] = []
            for value in values:
                if not self.set_input(value):
                    if progress:
                        progress(value, None)
                    continue
                if delay:
                    time.sleep(delay)
                read_back = self.get_input()
                if read_back == value:
                    available.append(value)
                if progress:
                    progress(value, read_back)
            if original is not None and original not in available:
                self.set_input(original)
                if delay:
                    time.sleep(delay)
            return available
        finally:
            self._retries_override = previous_retries
            self._quiet = previous_quiet

    def current_target(self) -> Optional[str]:
        """当前输入源对应的目标（'mac' / 'win' / None 表示未知）。"""
        value = self.get_input()
        if value is None:
            return None
        if value == self.mac_input:
            return "mac"
        if value == self.win_input:
            return "win"
        return None

    def switch_to_mac(self) -> bool:
        log.info("调用 DDC 切换到 Mac（输入源 %s）", self.mac_input)
        return self.set_input(self.mac_input)

    def switch_to_win(self) -> bool:
        log.info("调用 DDC 切换到 Windows（输入源 %s）", self.win_input)
        return self.set_input(self.win_input)

    def switch_target(self, target: str) -> bool:
        if target == "mac":
            return self.switch_to_mac()
        if target == "win":
            return self.switch_to_win()
        raise ValueError(f"未知切换目标：{target!r}")

    def tool_available(self) -> Tuple[bool, str]:
        """检查 DDC 工具是否可用，供 --status / 启动自检使用。"""
        tool = self.tool_path()
        if not tool:
            return False, f"未配置 {self.system} 的 DDC 工具路径"
        looks_like_path = Path(tool).is_absolute() or "/" in tool or "\\" in tool
        if looks_like_path:
            return (True, f"已找到 {tool}") if Path(tool).exists() else (False, f"未找到 {tool}")
        found = shutil.which(tool)
        return (True, f"PATH 中的 {found}") if found else (False, f"PATH 中找不到 {tool}")
