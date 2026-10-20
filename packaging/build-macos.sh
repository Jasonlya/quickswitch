#!/usr/bin/env bash
# QuickSwitch macOS 打包：PyInstaller → .app → 代码签名 → 公证（notarize）→ 装订（staple）
#
# 用法：
#   packaging/build-macos.sh --check                     # 只检查环境（不发一份证书也能跑）
#   packaging/build-macos.sh --no-sign                   # 只出未签名 .app，本地自测用
#   packaging/build-macos.sh                             # 自动挑选 Developer ID 身份签名并公证
#   packaging/build-macos.sh --identity "Developer ID Application: X (TEAMID)" \
#                            --keychain-profile quickswitch-notary
#
# 公证凭据先存一次（只需一次，存进钥匙串）：
#   xcrun notarytool store-credentials quickswitch-notary \
#     --apple-id "you@example.com" --team-id TEAMID --password "app-specific-password"
#
# 兼容 macOS 自带的 bash 3.2（不要用 bash 4 的语法）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

DIST_DIR="${PROJECT_DIR}/dist"
WORK_DIR="${PROJECT_DIR}/build"
VENV_DIR="${PROJECT_DIR}/.venv-build"
PYTHON_BIN="${PYTHON:-python3}"
IDENTITY="${QS_CODESIGN_IDENTITY:-}"
KEYCHAIN_PROFILE="${QS_KEYCHAIN_PROFILE:-}"
TIMESTAMP_URL="http://timestamp.apple.com/ts01"
DO_SIGN=1
DO_NOTARIZE=1
CHECK_ONLY=0
UPLOAD_ZIP=""

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
info() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

usage() {
    sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

选项：
  --check                 只做环境自检（工具链 / 图标 / 签名身份 / 公证凭据），不构建
  --no-sign               跳过签名与公证，只产出未签名 .app
  --skip-notarize         签名但不公证（本地验证 hardened runtime 用）
  --identity NAME         代码签名身份，默认取 $QS_CODESIGN_IDENTITY，再不然自动挑一个
  --keychain-profile NAME notarytool 凭据名，默认取 $QS_KEYCHAIN_PROFILE
  --dist DIR              产物目录（默认 <项目>/dist）
  --work DIR              构建中间目录（默认 <项目>/build）
  --venv DIR              构建用虚拟环境（默认 <项目>/.venv-build）
  --python BIN            用来创建虚拟环境的解释器（默认 python3）
  --zip PATH              额外导出可分发 zip（公证后装订过的 .app）
  -h, --help              显示本帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --no-sign) DO_SIGN=0; DO_NOTARIZE=0 ;;
        --skip-notarize) DO_NOTARIZE=0 ;;
        --identity) shift; IDENTITY="${1:-}" ;;
        --keychain-profile) shift; KEYCHAIN_PROFILE="${1:-}" ;;
        --dist) shift; DIST_DIR="${1:-}" ;;
        --work) shift; WORK_DIR="${1:-}" ;;
        --venv) shift; VENV_DIR="${1:-}" ;;
        --python) shift; PYTHON_BIN="${1:-}" ;;
        --zip) shift; UPLOAD_ZIP="${1:-}" ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（--help 看用法）" ;;
    esac
    shift
done

APP_PATH="${DIST_DIR}/QuickSwitch.app"
EXECUTABLE="${APP_PATH}/Contents/MacOS/QuickSwitch"

# ----------------------------------------------------------------- 预检
find_identity() {
    if [ -n "$IDENTITY" ]; then
        printf '%s\n' "$IDENTITY"
        return
    fi
    security find-identity -v -p codesigning 2>/dev/null \
        | sed -n 's/.*"\(Developer ID Application: .*\)".*/\1/p' | head -1
}

preflight() {
    local failures=0
    step "环境自检"

    for tool in xcrun codesign spctl security ditto; do
        if command -v "$tool" >/dev/null 2>&1; then
            info "✔ $tool"
        else
            info "✘ 缺少 $tool"; failures=$((failures + 1))
        fi
    done
    if xcrun --find notarytool >/dev/null 2>&1; then
        info "✔ notarytool（公证提交用）"
    else
        info "✘ 找不到 notarytool：安装 Xcode 或完整版 Command Line Tools"; failures=$((failures + 1))
    fi
    if command -v iconutil >/dev/null 2>&1; then
        info "✔ iconutil（.icns 生成用）"
    else
        info "· 无 iconutil：将复用已生成的 packaging/icon.icns"
    fi

    if "${PYTHON_BIN}" "${SCRIPT_DIR}/make-icons.py" --check >/dev/null 2>&1; then
        info "✔ 图标齐全（icon.icns / icon.ico / icon.png）"
    else
        info "· 图标缺失，构建时会先调用 make-icons.py 生成"
    fi

    if [ "$DO_SIGN" = "1" ]; then
        local found
        found="$(find_identity)"
        if [ -n "$found" ]; then
            info "✔ 签名身份：$found"
        else
            info "✘ 钥匙串里没有 Developer ID Application 证书"
            info "  需要 Apple Developer Program（99 美元/年）→ Certificates 里签发"
            info "  「Developer ID Application」证书并装进钥匙串；只想本地试打用 --no-sign"
            failures=$((failures + 1))
        fi
    else
        info "· --no-sign：跳过签名与公证（产物带 quarantine 属性，别人打不开）"
    fi

    if [ "$DO_SIGN" = "1" ] && [ "$DO_NOTARIZE" = "1" ]; then
        if [ -n "$KEYCHAIN_PROFILE" ]; then
            if security find-generic-password -s "com.apple.gke.notary.tool" \
                 -a "$KEYCHAIN_PROFILE" >/dev/null 2>&1; then
                info "✔ notarytool 凭据：$KEYCHAIN_PROFILE"
            else
                info "✘ 找不到 notarytool 凭据档案 $KEYCHAIN_PROFILE，先执行："
                info "  xcrun notarytool store-credentials $KEYCHAIN_PROFILE \\"
                info "    --apple-id <AppleID> --team-id <TEAMID> --password <专用密码>"
                failures=$((failures + 1))
            fi
        else
            info "✘ 未指定 --keychain-profile（公证必须）"
            failures=$((failures + 1))
        fi
    fi

    if [ "$failures" -gt 0 ]; then
        step "自检结果：$failures 项未满足"
        return 1
    fi
    step "自检结果：可以完整构建（签名 + 公证）"
    return 0
}

if [ "$CHECK_ONLY" = "1" ]; then
    if preflight; then exit 0; else exit 1; fi
fi

preflight || {
    if [ "$DO_SIGN" = "1" ]; then
        die "环境不满足完整构建；加 --no-sign 可以只出未签名产物"
    fi
}

# ----------------------------------------------------------------- 依赖
step "准备构建环境"
if [ ! -x "${VENV_DIR}/bin/python" ]; then
    info "创建虚拟环境 ${VENV_DIR}"
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi
BUILD_PY="${VENV_DIR}/bin/python"
info "安装依赖（运行时 + 打包）"
"${BUILD_PY}" -m pip install --quiet --upgrade pip
"${BUILD_PY}" -m pip install --quiet -r "${PROJECT_DIR}/requirements.txt" \
                                       -r "${SCRIPT_DIR}/requirements-build.txt"

if ! "${BUILD_PY}" "${SCRIPT_DIR}/make-icons.py" --check >/dev/null 2>&1; then
    info "生成图标"
    "${BUILD_PY}" "${SCRIPT_DIR}/make-icons.py"
fi

# ----------------------------------------------------------------- 打包
step "PyInstaller 打包"
QS_CODESIGN_IDENTITY="$IDENTITY" \
QS_ENTITLEMENTS="${SCRIPT_DIR}/entitlements.plist" \
"${BUILD_PY}" -m PyInstaller --clean --noconfirm \
    --distpath "${DIST_DIR}" --workpath "${WORK_DIR}" \
    "${SCRIPT_DIR}/quickswitch.spec"

[ -x "$EXECUTABLE" ] || die "打包失败：找不到 ${EXECUTABLE}"
info "已生成 $(basename "$APP_PATH")（$(du -sh "$APP_PATH" | cut -f1)）"

# ----------------------------------------------------------------- 未签名路径
if [ "$DO_SIGN" = "0" ]; then
    step "完成（未签名）"
    cat <<EOF
  产物：${APP_PATH}
  未签名 + 未公证，拷到别的机器会被 Gatekeeper 拦下；本机自测可先去掉隔离属性：
    xattr -dr com.apple.quarantine "${APP_PATH}"
  命令行自检（.app 的可执行文件在包里，直接跑即可）：
    "${EXECUTABLE}" --status
    "${EXECUTABLE}" --print-config
EOF
    exit 0
fi

# ----------------------------------------------------------------- 签名
step "代码签名"
IDENTITY_RESOLVED="$(find_identity)"
[ -n "$IDENTITY_RESOLVED" ] || die "找不到 Developer ID Application 证书"
info "身份：$IDENTITY_RESOLVED"

# PyInstaller 已经用 codesign_identity 签过包内二进制，这里补签 .app 外层并打时间戳。
codesign --force --options runtime --timestamp \
    --entitlements "${SCRIPT_DIR}/entitlements.plist" \
    --sign "$IDENTITY_RESOLVED" "$APP_PATH"

info "校验签名"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
codesign -dv --verbose=4 "$APP_PATH" 2>&1 | sed -n 's/^/    /p'

# ----------------------------------------------------------------- 公证
if [ "$DO_NOTARIZE" = "0" ]; then
    step "完成（已签名，未公证）"
    info "产物：${APP_PATH}"
    exit 0
fi

step "公证（notarytool）"
[ -n "$KEYCHAIN_PROFILE" ] || die "公证需要 --keychain-profile"
ZIP_PATH="${DIST_DIR}/QuickSwitch-notarize.zip"
rm -f "$ZIP_PATH"
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"
info "已提交：$(basename "$ZIP_PATH")"

SUBMIT_JSON="$("${BUILD_PY}" - "$ZIP_PATH" "$KEYCHAIN_PROFILE" <<'PY'
import json, subprocess, sys

zip_path, profile = sys.argv[1], sys.argv[2]
result = subprocess.run(
    ["xcrun", "notarytool", "submit", zip_path, "--keychain-profile", profile,
     "--wait", "--output-format", "json"],
    capture_output=True, text=True,
)
text = result.stdout.strip() or result.stderr.strip()
try:
    payload = json.loads(text)
except ValueError:
    print(json.dumps({"status": "Invalid", "message": text, "id": ""}))
else:
    print(json.dumps({
        "status": payload.get("status", "Unknown"),
        "id": payload.get("id", ""),
        "message": payload.get("message", ""),
    }))
PY
)"
NOTARY_STATUS="$("${BUILD_PY}" -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$SUBMIT_JSON")"
NOTARY_ID="$("${BUILD_PY}" -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$SUBMIT_JSON")"
info "结果：$NOTARY_STATUS（id=$NOTARY_ID）"

if [ "$NOTARY_STATUS" != "Accepted" ]; then
    printf '\n公证未通过，下面是对应的日志：\n' >&2
    xcrun notarytool log "$NOTARY_ID" --keychain-profile "$KEYCHAIN_PROFILE" >&2 || true
    die "公证失败：$NOTARY_STATUS"
fi

step "装订票据（staple）"
xcrun stapler staple -v "$APP_PATH"
xcrun stapler validate "$APP_PATH"

step "Gatekeeper 评估"
if spctl -a -vvv -t exec "$APP_PATH" 2>&1 | sed -n 's/^/    /p'; then
    info "本机 Gatekeeper 通过"
else
    info "spctl 未通过——常见原因是本机缓存或未装证书链，不影响别人下载后的校验"
fi

if [ -n "$UPLOAD_ZIP" ]; then
    mkdir -p "$(dirname "$UPLOAD_ZIP")"
    rm -f "$UPLOAD_ZIP"
    ditto -c -k --keepParent "$APP_PATH" "$UPLOAD_ZIP"
    info "分发 zip：$UPLOAD_ZIP"
fi

step "完成"
cat <<EOF
  产物   ：${APP_PATH}
  签名   ：${IDENTITY_RESOLVED}
  公证   ：Accepted（id=${NOTARY_ID}）
  SHA256 ：$(shasum -a 256 "$APP_PATH/Contents/MacOS/QuickSwitch" | cut -d' ' -f1)
  装到别的机器：拖进「应用程序」，首次启动若提示「无法验证开发者」，
  在「系统设置 → 隐私与安全性」里点「仍要打开」即可（公证通过后通常不会出现）。
  别忘了授权：本机是 client 时无需额外权限；Deskflow 需要「辅助功能」权限。
EOF
