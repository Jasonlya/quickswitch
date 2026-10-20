# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（Windows 单文件 exe / macOS .app）。

    pyinstaller --clean --noconfirm packaging/quickswitch.spec \
        --distpath dist --workpath build

平台差异：

- Windows：``--onefile``，一个 exe 拷过去就能用；``console=False``（托盘程序），
  但程序启动时会 ``AttachConsole`` 到父控制台，所以 ``QuickSwitch.exe --status``
  在 cmd 里仍能看到输出。
- macOS：``--onedir`` + ``.app``。**不用 onefile**：onefile 会在运行时把内容解到
  临时目录，公证（notarization）对「包里再套未签名二进制」很敏感，且启动更慢。
- 签名身份与 entitlements 由环境变量传入，脚本不需要改这个文件：
  ``QS_CODESIGN_IDENTITY``（如 ``Developer ID Application: Your Name (TEAMID)``）、
  ``QS_ENTITLEMENTS``（默认用 packaging/entitlements.plist）。
"""
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(SPECPATH).resolve()
PROJECT_DIR = HERE.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from main import VERSION  # noqa: E402  （用同一份版本号，避免两处漂移）

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

CODESIGN_IDENTITY = os.environ.get("QS_CODESIGN_IDENTITY") or None
ENTITLEMENTS = os.environ.get("QS_ENTITLEMENTS") or (
    str(HERE / "entitlements.plist") if IS_MAC else None
)


def available(module: str):
    """只在构建环境里真的装了该模块时才加 hiddenimport（缺了就优雅降级）。"""
    try:
        return [module] if importlib.util.find_spec(module) else []
    except (ImportError, ValueError):
        return []


hiddenimports = available("pystray") + available("PIL._imaging")
if IS_MAC:
    hiddenimports += available("pystray._darwin")
elif IS_WIN:
    hiddenimports += available("pystray._win32") + available("keyboard")
else:
    hiddenimports += available("pystray._xorg") + available("pystray._appindicator")

analysis = Analysis(
    [str(PROJECT_DIR / "main.py")],
    pathex=[str(PROJECT_DIR)],
    binaries=[],
    datas=[(str(PROJECT_DIR / "ui"), "ui")],   # Web 控制台前端随包分发
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "doctest", "pydoc_data",
              "numpy", "matplotlib", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

if IS_MAC:
    exe = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,          # onedir：二进制交给 COLLECT
        name="QuickSwitch",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,                      # UPX 压过的可执行文件会破坏签名/公证
        console=False,                  # 菜单栏程序，不弹终端窗口
        disable_windowed_traceback=False,
        codesign_identity=CODESIGN_IDENTITY,
        entitlements_file=ENTITLEMENTS,
    )
    collected = COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        name="QuickSwitch",
    )
    app = BUNDLE(
        collected,
        name="QuickSwitch.app",
        icon=str(HERE / "icon.icns"),
        bundle_identifier="io.quickswitch.agent",
        version=VERSION,
        info_plist={
            "CFBundleName": "QuickSwitch",
            "CFBundleDisplayName": "QuickSwitch",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "CFBundleIdentifier": "io.quickswitch.agent",
            "LSMinimumSystemVersion": "11.0",
            # 菜单栏程序：不出现在 Dock、不抢焦点
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
            # macOS 15+ 访问局域网会弹权限提示，缺这条会被系统静默拒绝
            "NSLocalNetworkUsageDescription": (
                "需要访问局域网，才能与另一台电脑互相通知切换显示器输入源。"
            ),
        },
    )
else:
    exe = EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name="QuickSwitch",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=str(HERE / "icon.ico") if IS_WIN else None,
        version=str(HERE / "version_info.txt") if IS_WIN else None,
        codesign_identity=CODESIGN_IDENTITY,
    )
