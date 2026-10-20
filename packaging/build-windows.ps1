# QuickSwitch Windows 打包：PyInstaller → QuickSwitch.exe → Authenticode 签名 → 校验
#
# 用法（在项目根目录）：
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -Check
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -NoSign
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 `
#       -PfxPath C:\certs\quickswitch.pfx -PfxPassword $env:QS_PFX_PASSWORD
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 `
#       -CertSubject "CN=Your Company, O=Your Company, C=CN"      # 用证书存储里的证书
#
# CI 里常用 base64 存 pfx：把内容放进 QS_PFX_BASE64 即可（脚本自己解到临时文件签名后删掉）。
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoSign,
    [switch]$SkipInstall,
    [string]$PfxPath = $env:QS_PFX_PATH,
    [string]$PfxPassword = $env:QS_PFX_PASSWORD,
    [string]$PfxBase64 = $env:QS_PFX_BASE64,
    [string]$CertSubject = $env:QS_CERT_SUBJECT,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [string]$SignTool,
    [string]$Python = "python",
    [string]$Dist = "",
    [string]$Work = "",
    [string]$Venv = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir  = $PSScriptRoot
$ProjectDir = Split-Path -Parent $ScriptDir
if (-not $Dist)  { $Dist  = Join-Path $ProjectDir "dist" }
if (-not $Work)  { $Work  = Join-Path $ProjectDir "build" }
if (-not $Venv)  { $Venv  = Join-Path $ProjectDir ".venv-build" }

$ExePath = Join-Path $Dist "QuickSwitch.exe"

function Write-Step([string]$Text) { Write-Host "`n== $Text" }
function Write-Info([string]$Text) { Write-Host "  $Text" }

function Find-SignTool([string]$Explicit) {
    if ($Explicit -and (Test-Path $Explicit)) { return $Explicit }
    $cmd = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $roots = @()
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vsPath = & $vswhere -latest -products * -property installationPath 2>$null
        if ($vsPath) { $roots += (Join-Path $vsPath "SDK\bin") }
    }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path $kits) { $roots += $kits }
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        $hit = Get-ChildItem -Path $root -Recurse -Filter "signtool.exe" -ErrorAction SilentlyContinue |
               Where-Object { $_.FullName -match "x64" } |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Get-ProjectVersion {
    $text = Get-Content -Raw -LiteralPath (Join-Path $ProjectDir "main.py")
    $match = [regex]::Match($text, 'VERSION\s*=\s*"([^"]+)"')
    if (-not $match.Success) { throw "读不到 main.py 里的 VERSION" }
    return $match.Groups[1].Value
}

function Assert-VersionConsistency {
    $version = Get-ProjectVersion
    $infoPath = Join-Path $ScriptDir "version_info.txt"
    $infoText = Get-Content -Raw -LiteralPath $infoPath
    if ($infoText -notmatch [regex]::Escape("$version.0")) {
        throw "version_info.txt 里没有 $version.0，与 main.py 的 VERSION=$version 不一致：请同步后重试"
    }
    Write-Info "版本一致：$version（main.py 与 version_info.txt）"
}

function Show-Preflight {
    $failures = 0
    Write-Step "环境自检"
    try {
        $pv = & $Python --version 2>&1
        Write-Info "OK  $pv"
    } catch {
        Write-Info "XX  找不到 Python（-Python 指定解释器路径）"
        $failures++
    }
    $tool = Find-SignTool $SignTool
    if ($tool) {
        Write-Info "OK  signtool：$tool"
    } else {
        Write-Info "XX  找不到 signtool.exe（装 Windows SDK 或 Visual Studio 的「Windows SDK」组件）"
        if (-not $NoSign) { $failures++ }
    }
    if ($NoSign) {
        Write-Info "--  -NoSign：跳过签名（产物会触发 SmartScreen 警告）"
    } elseif ($PfxPath) {
        if (Test-Path $PfxPath) { Write-Info "OK  pfx：$PfxPath" }
        else { Write-Info "XX  pfx 不存在：$PfxPath"; $failures++ }
    } elseif ($PfxBase64) {
        Write-Info "OK  使用 QS_PFX_BASE64（临时解出 pfx 后签名）"
    } elseif ($CertSubject) {
        $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
                Where-Object { $_.Subject -like "*$CertSubject*" -and $_.HasPrivateKey } |
                Select-Object -First 1
        if ($cert) { Write-Info "OK  证书存储里找到：$($cert.Subject)" }
        else { Write-Info "XX  证书存储里没找到带私钥的证书：$CertSubject"; $failures++ }
    } else {
        Write-Info "XX  没有可用签名材料：给 -PfxPath / -PfxBase64 / -CertSubject，或加 -NoSign"
        $failures++
    }
    if ($failures -gt 0) {
        Write-Step "自检结果：$failures 项未满足"
        return $false
    }
    Write-Step "自检结果：可以构建"
    return $true
}

if ($Check) {
    if (Show-Preflight) { exit 0 } else { exit 1 }
}
if (-not (Show-Preflight)) {
    if (-not $NoSign) { throw "环境不满足签名构建；加 -NoSign 可只出未签名 exe" }
}

Assert-VersionConsistency

# --------------------------------------------------------------- 构建环境
Write-Step "准备构建环境"
$BuildPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $BuildPy)) {
    Write-Info "创建虚拟环境 $Venv"
    & $Python -m venv $Venv
}
if (-not (Test-Path $BuildPy)) { throw "虚拟环境创建失败：$BuildPy" }
if (-not $SkipInstall) {
    Write-Info "安装依赖（运行时 + 打包）"
    & $BuildPy -m pip install --quiet --upgrade pip
    & $BuildPy -m pip install --quiet -r (Join-Path $ProjectDir "requirements.txt") `
                                        -r (Join-Path $ScriptDir "requirements-build.txt")
} else {
    Write-Info "-SkipInstall：复用 $Venv 里已有的依赖"
}

# --------------------------------------------------------------- 打包
Write-Step "PyInstaller 打包"
& $BuildPy -m PyInstaller --clean --noconfirm `
    --distpath $Dist --workpath $Work (Join-Path $ScriptDir "quickswitch.spec")
if (-not (Test-Path $ExePath)) { throw "打包失败：找不到 $ExePath" }
$size = [math]::Round((Get-Item $ExePath).Length / 1MB, 1)
Write-Info "已生成 QuickSwitch.exe（$size MB）"

# --------------------------------------------------------------- 签名
if (-not $NoSign) {
    Write-Step "Authenticode 签名"
    $SignToolPath = Find-SignTool $SignTool
    if (-not $SignToolPath) { throw "找不到 signtool.exe" }

    $tempPfx = $null
    if ($PfxBase64) {
        $tempPfx = Join-Path ([System.IO.Path]::GetTempPath()) "quickswitch-$([guid]::NewGuid().ToString('N')).pfx"
        [System.IO.File]::WriteAllBytes($tempPfx, [Convert]::FromBase64String($PfxBase64))
        $PfxPath = $tempPfx
        Write-Info "已从 QS_PFX_BASE64 解出临时 pfx"
    }

    $signArgs = @("sign", "/fd", "sha256", "/tr", $TimestampUrl, "/td", "sha256", "/v")
    if ($PfxPath) {
        $signArgs += @("/f", $PfxPath)
        if ($PfxPassword) { $signArgs += @("/p", $PfxPassword) }
    } elseif ($CertSubject) {
        $signArgs += @("/n", $CertSubject)
    } else {
        throw "没有签名材料；用 -PfxPath / -PfxBase64 / -CertSubject，或加 -NoSign"
    }
    $signArgs += $ExePath

    Write-Info "签名中（SHA-256 + 时间戳）"
    & $SignToolPath @signArgs
    if ($LASTEXITCODE -ne 0) { throw "签名失败（退出码 $LASTEXITCODE）" }

    Write-Info "校验签名"
    & $SignToolPath verify /pa /v $ExePath
    if ($LASTEXITCODE -ne 0) { throw "签名校验失败（退出码 $LASTEXITCODE）" }
    Get-AuthenticodeSignature -FilePath $ExePath |
        Select-Object Status, StatusMessage, @{n = "Signer"; e = { $_.SignerCertificate.Subject }} |
        Format-List | Out-String | Write-Info

    if ($tempPfx) { Remove-Item -LiteralPath $tempPfx -Force -ErrorAction SilentlyContinue }
}

# --------------------------------------------------------------- 收尾
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ExePath).Hash
Write-Step "完成"
Write-Info "产物   ：$ExePath"
if ($NoSign) {
    Write-Info "签名   ：无（-NoSign）"
    Write-Info "首次运行会弹 SmartScreen「Windows 已保护你的电脑」→ 详细信息 → 仍要运行"
} else {
    Write-Info "签名   ：Authenticode / SHA-256（时间戳 $TimestampUrl）"
}
Write-Info "SHA256 ：$hash"
Write-Info ""
Write-Info "在目标机器上还需要："
Write-Info "  1) 防火墙放行监听端口（管理员 PowerShell，只需一次）："
Write-Info "       netsh advfirewall firewall add rule name=\"QuickSwitch\" dir=in action=allow protocol=TCP localport=8765"
Write-Info "  2) 注册全局热键需要管理员权限：用 scripts\install-task.ps1 注册成登录自启"
Write-Info "  3) 自检：QuickSwitch.exe --status（会附着到当前 cmd 窗口输出）"
