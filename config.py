"""QuickSwitch 配置模块：默认配置生成、JSON 加载、深度合并与校验。

配置文件默认位于 ``~/.quickswitch/config.json``；可用环境变量
``QUICKSWITCH_CONFIG`` 覆盖路径，或在命令行用 ``--config`` 指定。
加载时会与内置默认值做深度合并，因此用户只需在文件里写需要改动的字段。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("config")

DEFAULT_CONFIG_DIR = Path.home() / ".quickswitch"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"

CONFIG_ENV_VAR = "QUICKSWITCH_CONFIG"
LOG_ENV_VAR = "QUICKSWITCH_LOG"


def log_file() -> Path:
    """日志文件路径，可用 ``QUICKSWITCH_LOG`` 覆盖（自检脚本/多实例调试用）。"""
    override = os.environ.get(LOG_ENV_VAR)
    return Path(override).expanduser() if override else DEFAULT_CONFIG_DIR / "quickswitch.log"

VALID_ROLES = ("server", "client")
LABEL = {"mac": "Mac", "win": "Windows"}

DEFAULTS: Dict[str, Any] = {
    "role": "server",                 # server = Windows（物理键鼠所在机）, client = macOS
    "peer_host": "192.168.1.100",     # 对端 IP / Tailscale 主机名 / "auto"（启用局域网自动发现）
    "peer_port": 8765,
    "listen_port": 8765,
    "tray": True,
    "log_level": "INFO",
    "debounce": 0.6,                  # 同一目标的重复触发间隔（秒），防按键连发/重复执行
    "ddc": {
        "controlmyMonitor_path": r"C:\Tools\ControlMyMonitor.exe",
        "monitor_id": r"\\.\DISPLAY1\Monitor0",
        "m1ddc_path": "/opt/homebrew/bin/m1ddc",
        "ddcutil_path": "ddcutil",
        "display_index": 1,
        "vcp_code": "0x60",           # 输入源 VCP 代码，一般无需修改
        "mac_input": 15,              # DP1=15 DP2=16 HDMI1=17 HDMI2=18 USB-C=27
        "win_input": 17,
        "command_template": None,     # 自定义命令模板（字符串或字符串列表），留空用内置模板
        "read_template": None,        # 自定义「读取当前输入源」命令模板（如 BetterDisplay CLI）
        "read_regex": None,           # 从读取命令输出中提取数值的正则（默认按平台约定解析）
        "settle": 1.2,                # 切换后等待显示器稳定的时间（自动识别时使用）
        "timeout": 10.0,
        "retries": 2,                 # DDC 首次失败后的重试次数
        "retry_delay": 0.4,
    },
    "hotkeys": {
        "switch_to_mac": "ctrl+alt+1",
        "switch_to_win": "ctrl+alt+2",
    },
    "network": {
        "ping_interval": 10.0,        # 心跳间隔
        "ack_timeout": 4.0,           # 等待对端切换确认的超时
        "reconnect_interval": 3.0,
        "reconnect_max_interval": 30.0,
        "connect_wait": 4.0,          # --once 模式等待对端连接的时间
    },
    "discovery": {
        "enabled": False,             # peer_host 为 "auto" 时自动启用
        "port": 8766,
        "interval": 5.0,
        "ttl": 15.0,
        "broadcast": "255.255.255.255",  # 也可写成数组，如 ["192.168.1.255"]；留空则只用网段推导
    },
    "web": {
        "enabled": True,              # 本地 Web 控制台（只监听回环地址，每次启动生成随机令牌）
        "host": "127.0.0.1",
        "port": 8767,
    },
    "auth": {
        "secret": "",                 # 预共享密钥：两端必须一致；留空 = 不校验（同网段任何人都能发切换请求）
    },
}


class ConfigError(Exception):
    """配置存在问题且无法继续运行。"""


def config_path(explicit: Optional[str] = None) -> Path:
    """解析配置文件路径：命令行 > 环境变量 > 默认路径。"""
    if explicit:
        return Path(explicit).expanduser()
    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        return Path(env_value).expanduser()
    return DEFAULT_CONFIG_FILE


def default_config() -> Dict[str, Any]:
    """返回一份默认配置的深拷贝。"""
    return copy.deepcopy(DEFAULTS)


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 合并到 base 的副本上：只覆盖出现的字段，缺失字段保留默认值。"""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        current = result.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path: Optional[str] = None, *, create: bool = True) -> Dict[str, Any]:
    """加载配置；文件不存在时按需生成默认配置。"""
    target = config_path(path)
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"配置文件不是合法 JSON：{target}（{exc}）") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"配置文件顶层必须是 JSON 对象：{target}")
        cfg = deep_merge(default_config(), raw)
    else:
        cfg = default_config()
        if create:
            save_config(cfg, target)
            log.info("已生成默认配置：%s", target)

    _apply_env_overrides(cfg)
    for warning in validate(cfg):
        log.warning("配置提示：%s", warning)
    return cfg


def save_config(cfg: Dict[str, Any], path: Optional[str] = None) -> Path:
    target = config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def _apply_env_overrides(cfg: Dict[str, Any]) -> None:
    """容器/临时调试用的轻量环境变量覆盖。"""
    overrides = {
        "QUICKSWITCH_ROLE": ("role", str),
        "QUICKSWITCH_PEER_HOST": ("peer_host", str),
        "QUICKSWITCH_PEER_PORT": ("peer_port", int),
        "QUICKSWITCH_LISTEN_PORT": ("listen_port", int),
    }
    for env_key, (field, cast) in overrides.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            cfg[field] = cast(raw)
        except ValueError:
            log.warning("忽略无效的环境变量 %s=%r", env_key, raw)


def validate(cfg: Dict[str, Any]) -> List[str]:
    """校验配置；致命问题抛 ConfigError，可容忍的问题以列表返回告警。"""
    warnings: List[str] = []

    role = cfg.get("role")
    if role not in VALID_ROLES:
        raise ConfigError(f"role 必须是 {VALID_ROLES} 之一，当前为 {role!r}")

    for field in ("peer_port", "listen_port"):
        value = cfg.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
            raise ConfigError(f"{field} 必须是 1-65535 之间的整数，当前为 {value!r}")

    ddc = cfg.get("ddc")
    if not isinstance(ddc, dict):
        raise ConfigError("ddc 必须是 JSON 对象")
    for field in ("mac_input", "win_input"):
        value = ddc.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
            raise ConfigError(
                f"ddc.{field} 必须是 0-255 的整数（VCP 0x60 输入源值），当前为 {value!r}"
            )
    if ddc.get("mac_input") == ddc.get("win_input"):
        warnings.append("ddc.mac_input 与 ddc.win_input 相同，切换后画面不会变化")
    if not isinstance(ddc.get("display_index"), int) or isinstance(ddc.get("display_index"), bool):
        raise ConfigError(f"ddc.display_index 必须是整数，当前为 {ddc.get('display_index')!r}")
    if not isinstance(ddc.get("timeout"), (int, float)) or ddc.get("timeout") <= 0:
        raise ConfigError(f"ddc.timeout 必须是正数，当前为 {ddc.get('timeout')!r}")

    template = ddc.get("command_template")
    if template is not None and not isinstance(template, (str, list)):
        raise ConfigError("ddc.command_template 必须是字符串或字符串列表")
    read_template = ddc.get("read_template")
    if read_template is not None and not isinstance(read_template, (str, list)):
        raise ConfigError("ddc.read_template 必须是字符串或字符串列表")
    read_regex = ddc.get("read_regex")
    if read_regex is not None:
        if not isinstance(read_regex, str):
            raise ConfigError("ddc.read_regex 必须是字符串")
        try:
            re.compile(read_regex)
        except re.error as exc:
            raise ConfigError(f"ddc.read_regex 不是合法正则：{exc}") from exc

    hotkeys = cfg.get("hotkeys")
    if not isinstance(hotkeys, dict):
        raise ConfigError("hotkeys 必须是 JSON 对象")
    if role == "server":
        if hotkeys.get("switch_to_mac") and hotkeys.get("switch_to_mac") == hotkeys.get("switch_to_win"):
            raise ConfigError("hotkeys.switch_to_mac 与 hotkeys.switch_to_win 不能相同")
        for field in ("switch_to_mac", "switch_to_win"):
            value = hotkeys.get(field)
            if not isinstance(value, str) or not value.strip():
                warnings.append(f"hotkeys.{field} 为空，该热键不会被注册")

    peer_host = str(cfg.get("peer_host") or "").strip()
    discovery_enabled = bool((cfg.get("discovery") or {}).get("enabled"))
    broadcast = (cfg.get("discovery") or {}).get("broadcast")
    if broadcast is not None and not isinstance(broadcast, (str, list, tuple)):
        raise ConfigError("discovery.broadcast 必须是字符串或字符串数组")
    if peer_host.lower() in ("", "auto") and not discovery_enabled:
        warnings.append(
            'peer_host 为 "auto" 但 discovery.enabled 为 false，'
            "本机只会被动接受对端连接，无法主动通知对端"
        )

    web = cfg.get("web") or {}
    if not isinstance(web, dict):
        raise ConfigError("web 必须是 JSON 对象")
    enabled = web.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"web.enabled 必须是布尔值，当前为 {enabled!r}")
    web_port = web.get("port", 8767)
    if not isinstance(web_port, int) or isinstance(web_port, bool) or not 1 <= web_port <= 65535:
        raise ConfigError(f"web.port 必须是 1-65535 之间的整数，当前为 {web_port!r}")
    web_host = web.get("host", "127.0.0.1")
    if not isinstance(web_host, str) or not web_host.strip():
        raise ConfigError(f"web.host 必须是非空字符串，当前为 {web_host!r}")
    if enabled and web_host.strip() not in ("127.0.0.1", "localhost", "::1"):
        warnings.append(
            f"web.host 为 {web_host!r}：控制台将监听非回环地址，"
            "局域网内拿到 URL 的人即可切换显示器，建议保持 127.0.0.1"
        )

    auth = cfg.get("auth") or {}
    if not isinstance(auth, dict):
        raise ConfigError("auth 必须是 JSON 对象")
    secret = auth.get("secret", "")
    if not isinstance(secret, str):
        raise ConfigError(f"auth.secret 必须是字符串，当前为 {secret!r}")
    if not secret.strip():
        warnings.append(
            "auth.secret 为空：监听端口上任何能连进来的程序都能发切换请求，"
            "建议两端配置同一个随机密钥（用 python main.py --gen-secret 生成）"
        )
    elif len(secret.strip()) < 16:
        warnings.append(
            f"auth.secret 只有 {len(secret.strip())} 个字符，太短；"
            "建议用 python main.py --gen-secret 生成一个 32 字节的随机密钥"
        )
    return warnings
