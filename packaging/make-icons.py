#!/usr/bin/env python3
"""生成应用图标（macOS .icns / Windows .ico / 通用 .png）。

画面与托盘图标同一套语言：两块屏幕 + 中间的切换箭头；底色用产品强调色
Yves Klein Blue #002FA7，屏幕与箭头为白色。纯几何绘制，不使用外部素材。

用法：

    python3 packaging/make-icons.py          # 生成 packaging/icon.{png,ico,icns}
    python3 packaging/make-icons.py --check  # 只检查三个文件是否存在且格式正确
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

ACCENT = (0, 47, 167, 255)      # #002FA7
WHITE = (255, 255, 255, 255)

SIZE = 1024
BLEED = 88                      # 四周留白，macOS 图标的圆角方块不顶边
CORNER = 0.2237                 # Big Sur 圆角比例


def draw_icon(size: int = SIZE):
    from PIL import Image, ImageDraw

    scale = size / SIZE
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(coords):
        return [round(value * scale) for value in coords]

    draw.rounded_rectangle(
        box([BLEED, BLEED, SIZE - BLEED, SIZE - BLEED]),
        radius=round((SIZE - 2 * BLEED) * CORNER),
        fill=ACCENT,
    )

    # 两块屏幕（左边接 Mac，右边接 Windows，与界面里的「所有权转移条」同构）
    for left in (168, 592):
        screen = [left, 336, left + 264, 636]
        draw.rounded_rectangle(box(screen), radius=round(26 * scale), fill=WHITE)
        center = (screen[0] + screen[2]) / 2
        draw.rectangle(box([center - 20, 636, center + 20, 676]), fill=WHITE)
        draw.rounded_rectangle(box([center - 62, 676, center + 62, 694]),
                               radius=round(9 * scale), fill=WHITE)

    # 中间的切换箭头
    draw.rectangle(box([452, 466, 540, 506]), fill=WHITE)
    draw.polygon(
        [tuple(box([532, 430])), tuple(box([588, 486])), tuple(box([532, 542]))],
        fill=WHITE,
    )
    return image


def write_png(path: Path, size: int = SIZE) -> None:
    draw_icon(size).save(path, format="PNG")
    print(f"  {path}  ({size}×{size})")


def write_ico(path: Path) -> None:
    icon = draw_icon(SIZE)
    icon.save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                         (64, 64), (128, 128), (256, 256)])
    print(f"  {path}  (16…256 多尺寸)")


def write_icns(path: Path, png: Path) -> None:
    """macOS 的 .icns 用 iconutil 从 .iconset 目录生成（仅 macOS 可用）。"""
    iconutil = shutil.which("iconutil")
    if not iconutil:
        raise SystemExit("找不到 iconutil（.icns 只能在 macOS 上生成）")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        source = draw_icon(SIZE)
        for pixels in (16, 32, 64, 128, 256, 512, 1024):
            source.resize((pixels, pixels)).save(iconset / f"icon_{pixels}x{pixels}.png")
            if pixels <= 512:
                source.resize((pixels * 2, pixels * 2)).save(
                    iconset / f"icon_{pixels}x{pixels}@2x.png"
                )
        subprocess.run([iconutil, "-c", "icns", str(iconset), "-o", str(path)], check=True)
    print(f"  {path}  (16…1024 全尺寸)")
    del png  # 仅用于签名一致性，路径由调用方决定


def check() -> int:
    problems = []
    png, ico, icns = HERE / "icon.png", HERE / "icon.ico", HERE / "icon.icns"
    if not png.exists() or png.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        problems.append("icon.png 缺失或不是 PNG")
    if not ico.exists() or ico.read_bytes()[:4] != b"\x00\x00\x01\x00":
        problems.append("icon.ico 缺失或不是 ICO")
    if not icns.exists() or icns.read_bytes()[:4] != b"icns":
        problems.append("icon.icns 缺失或不是 ICNS")
    if problems:
        print("图标检查失败：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("图标检查通过：icon.png / icon.ico / icon.icns")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 QuickSwitch 应用图标")
    parser.add_argument("--check", action="store_true", help="只检查已有图标是否齐全")
    parser.add_argument("--png-size", type=int, default=SIZE, help="导出的 PNG 边长")
    args = parser.parse_args()

    if args.check:
        return check()
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("需要 Pillow：python3 -m pip install pillow", file=sys.stderr)
        return 2

    print("生成图标：")
    png = HERE / "icon.png"
    write_png(png, args.png_size)
    write_ico(HERE / "icon.ico")
    try:
        write_icns(HERE / "icon.icns", png)
    except SystemExit as exc:
        print(f"  {exc}（Windows 上可忽略，构建脚本只用 icon.ico）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
