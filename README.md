# QuickSwitch

按一个快捷键，同时切换**显示器输入源**和**键鼠控制权**。

适用场景（与方案一致）：Windows 主机通过网线接入局域网、键鼠物理连接在 Windows 上；
macOS 主机通过 WiFi 接入同一局域网；两台主机共用一台显示器。

本目录是方案 v1.0 中「QuickSwitch（自研 Python 客户端）」的完整实现，
另外附带了方案里留给「未来扩展」的局域网自动发现。

> **第一次安装？** 先看 [INSTALL.md](INSTALL.md) —— 面向使用者的安装与使用指南：
> 成品包怎么装、首次配置改哪几项、权限怎么给、日常怎么切、开机自启、升级卸载、常见问题。
> 本文件是完整技术参考。

## 1. 组成与数据流

| 组件 | 作用 | 运行位置 | 是否包含本仓库 |
| --- | --- | --- | --- |
| Deskflow | 一套键鼠控制两台电脑（软件 KVM、剪贴板文本共享） | 两端 | 否，需自行安装 |
| ControlMyMonitor / m1ddc / ddcutil | DDC/CI 切换显示器输入源 | 两端 | 否，需自行安装 |
| **QuickSwitch** | 热键/托盘触发，切本地 DDC 并经 TCP 通知对端冗余切换 | 两端 | **是（本目录）** |
| LocalSend | 局域网文件互传 | 两端 | 否 |
| Tailscale（可选） | 固定虚拟 IP，抵御 IP 变化 | 两端 | 否 |

```
物理键盘（插在 Windows）
  |-- Deskflow 捕获      --> 切换键鼠控制权
  |-- QuickSwitch 捕获   --> 切换显示器信号源（ControlMyMonitor / DDC-CI 0x60）
          |-- TCP 通知 Mac --> Mac 端 m1ddc 再切一次（方案 10.1 的冗余保险）
```

Windows 端是 `server`（注册热键），macOS 端是 `client`（只接收 TCP 指令并调用 DDC，不注册热键）。

## 2. 相对方案文档的实现说明

方案里的示例代码能表达意图，但有几处直接照抄会跑不通或不够可靠，实现时做了如下处理：

| # | 方案原文 | 本实现 | 原因 |
| --- | --- | --- | --- |
| 1 | `ControlMyMonitor.exe <monitor> 96 <value>` | `ControlMyMonitor.exe /SetValue <monitor> 0x60 <value>` | NirSoft 工具的命令行需要子命令；同时提供 `ddc.command_template` 兜底，任何工具都能改。请按 §8.1 先在命令行手动验证一次 |
| 2 | 连接循环只发心跳、不读数据、无断线检测 | 独立读循环 + 心跳 + 3 个周期无任何消息即判定半开并重连，指数退避到 30s | 方案原版对端进程被杀后 writer 仍非空，通知会静默丢失 |
| 3 | 通知对端后不校验结果 | 增加 `switch_ack` 确认（默认 4s 超时）并写日志 | 「冗余保险」是否真的生效可被验证，而不是只能靠猜 |
| 4 | 收到对端请求后再通知对端 | 对端发起的切换不再回传（`notify=False`） | 避免两端互相转发形成回环 |
| 5 | 无重复触发保护 | 同一目标 0.6s 内只执行一次（`debounce` 可配） | 热键连发会触发两次切换与两次 DDC |
| 6 | — | 端口被占用时启动即报错退出（退出码 3） | 顺带作为单实例检测，避免两个进程抢键盘热键 |
| 7 | — | `--status` / `--once` / `--dry-run` / `--print-config` / `--init-config` / `--no-tray` | 方案的 §9 验收清单需要可执行的验证手段 |
| 8 | 「未来扩展：mDNS 自动发现」 | 已实现可选的 UDP 广播发现（`peer_host: "auto"`），零第三方依赖 | 直接解决方案 1.2 的「IP 可能变化」痛点，不必强制上 Tailscale |
| 9 | — | 能读出当前输入源（Windows `/GetValue`、macOS `m1ddc get input`、Linux `getvcp 60`），`--status` 直接显示 | 便于确认切换是否真的生效。注意：`m1ddc` 官方 README 的 get 列表没写 input，但源码 `attrCodeFromCommand` 支持，非 set 指令都会先读当前值，因此可用 |
| 9b | — | `--detect-inputs` 输入源识别向导：探测候选取值 → 交互标注哪台机器 → 回读校验 → 写回配置 | 方案要求「不同品牌取值不同，需用工具读取」，向导把这一步自动化，不必手工搬数字 |
| 9c | — | `ddc.read_template` / `ddc.read_regex` | 读取能力交给任意工具（如 BetterDisplay CLI）；显示器不支持读 VCP 0x60 时也能自行指定 |
| 10 | Linux 只在选型表里提到 ddcutil | 已内置 ddcutil 模板 | 顺手支持，成本一次模板 |
| 11 | 日志只写文件 | 轮转日志（1MB × 3）+ 控制台 | 长期常驻不会把日志写爆 |
| 12 | — | 本地 Web 控制台（`webui.py` + `ui/`，默认 http://127.0.0.1:8767/） | 托盘菜单只能切两下；配置、当前输入源、DDC 命令与事件日志需要一个能一眼看全、能改配置的界面，且不引入 Electron 之类的重依赖 |
| 13 | — | 启动时后台回读一次输入源，切换/读取后自动回读 | 界面首屏就能显示「显示器现在归谁」，不必先点一次按钮 |
| 14 | — | Web 控制台只允许改白名单字段，且只监听回环地址 + 随机令牌 + Host/Origin 校验 | 一个能切换显示器、能写配置的本地端口，等价于本机代码执行入口，不能裸奔 |
| 15 | 网络只靠 TCP 通知，没有鉴权 | 可选预共享密钥 + 每连接随机 nonce 的 HMAC 质询-响应；未通过校验前丢弃 `switch` / `switch_ack` | 方案默认「同网段即可信」：局域网里任何机器只要往 8765 发一行 JSON 就能切走显示器。密钥不上网、旧连接的校验码不能重放 |
| 16 | — | `packaging/`：PyInstaller 打包 + Windows Authenticode 签名 / macOS 签名 + 公证 + 装订 | 方案把它当「Python 客户端」部署，但目标机装 Python、装依赖、配权限对普通用户太贵。详见 §7 |

## 3. 目录结构

```
quickswitch/
  main.py            入口：切换逻辑 + 托盘 + 热键 + 命令行
  config.py          配置加载、深度合并、校验、默认值生成
  ddc.py             DDC/CI 封装（Windows/macOS/Linux 命令模板 + 重试 + 读取）
  network.py         asyncio JSON Lines 通信（心跳、重连、双向互连、ACK、预共享密钥校验）
  discovery.py       可选：局域网 UDP 设备发现（peer_host = "auto"）
  webui.py           本地 Web 控制台：回环 HTTP 服务 + 令牌鉴权 + JSON API + 事件缓冲
  ui/                控制台前端（原生 HTML/CSS/JS，无构建步骤、无 CDN）
  requirements.txt   运行时依赖
  requirements-build.txt  打包期依赖（PyInstaller / Pillow，只在构建机安装）
  packaging/
    quickswitch.spec        PyInstaller 配置（Windows 单文件 exe / macOS .app）
    build-macos.sh          macOS：打包 → 签名 → 公证 → 装订 → Gatekeeper 校验
    build-windows.ps1       Windows：打包 → Authenticode 签名 → 签名校验
    entitlements.plist      hardened runtime 权限（最小集）
    version_info.txt        Windows exe 的版本资源（1.1.0.0）
    make-icons.py           用 Pillow 生成 icon.png / icon.ico / icon.icns
    icon.png / icon.ico / icon.icns   已生成好的图标
  scripts/
    quickstart-windows.bat         Windows 端启动（保留控制台日志，便于排错）
    install-task.ps1               注册 Windows 任务计划（登录自启 + 最高权限）
    com.quickswitch.agent.plist    macOS launchd 自启模板
    selftest.py                    本机链路自检（无需第二台机器/显示器）
  tests/              127 个单元/集成测试（unittest，无第三方依赖）
```

## 4. 快速开始

> Python 3.9+（方案写 3.10+，但本实现未使用 3.10 专属语法，已在 3.9.6 上跑通全部测试）。

### 4.1 Windows（server，物理键鼠所在机）

```bat
:: 1) 依赖
pip install -r requirements.txt

:: 2) 首次运行，生成默认配置 %USERPROFILE%\.quickswitch\config.json
python main.py --init-config

:: 3) 按 §8.1 手动确认 DDC 能切显示器，然后填入 mac_input / win_input
:: 4) 以管理员身份运行（keyboard 注册全局热键需要管理员权限）
python main.py
```

另需：安装 Deskflow（选 Server）、ControlMyMonitor 解压到 `C:\Tools\ControlMyMonitor.exe`、
按需安装 LocalSend / Tailscale，见 §4.3 / §4.4。

若 Windows 防火墙挡住了 `listen_port`（默认 8765），用管理员 PowerShell 放行一次即可：

```powershell
netsh advfirewall firewall add rule name="QuickSwitch" dir=in action=allow protocol=TCP localport=8765
```

### 4.2 macOS（client）

```bash
brew tap deskflow/tap && brew install deskflow   # 键鼠共享
brew install m1ddc                               # DDC/CI 切换（仅 Apple Silicon）

cd quickswitch
python3 -m pip install pystray Pillow
python3 main.py --init-config                    # 生成配置后把 role 改成 client
python3 main.py --status                         # 自检：工具 + 对端连通性
python3 main.py
```

授权：系统设置 → 隐私与安全性 → **辅助功能** 勾选 Deskflow 与 deskflow；
macOS Sequoia 还需在「本地网络」中允许 Deskflow。

### 4.3 Deskflow（键鼠共享 + 剪贴板）

1. Windows 端选 **Server**，配置屏幕布局（Mac 在左/右/上/下）。
2. macOS 端选 **Client**，填 Windows 的 IP 或主机名。
3. 两端配置**相同热键**：`Ctrl+Alt+1 → Switch to screen: Mac`，`Ctrl+Alt+2 → Switch to screen: Windows`。
4. 剪贴板文本同步是 Deskflow 自带能力，无需额外配置。

QuickSwitch 注册热键时使用 `suppress=False`，所以 Deskflow 能收到同一个热键——
这正是「一个快捷键同时切显示器 + 切键鼠」的关键。两个热键必须在 Deskflow 与 `config.json` 中保持一致。

### 4.4 网络（三选一）

| 方案 | 配置 | 说明 |
| --- | --- | --- |
| 静态 IP / 路由器 DHCP 绑定 | `"peer_host": "192.168.1.10"` | 最简单，IP 变了要改配置 |
| Tailscale（推荐） | `"peer_host": "100.x.x.x"` 或 MagicDNS 名 | IP 变化无感 |
| 局域网自动发现 | `"peer_host": "auto"` | 两端在同一网段即可；见 §5 的 `discovery.*` |

## 5. 配置说明

配置文件：`~/.quickswitch/config.json`（可用 `QUICKSWITCH_CONFIG` 环境变量或 `--config` 覆盖）。
文件里只写要改的字段即可，其余自动用默认值补齐。

```json
{
  "role": "server",
  "peer_host": "100.101.102.103",
  "peer_port": 8765,
  "listen_port": 8765,
  "tray": true,
  "log_level": "INFO",
  "debounce": 0.6,
  "ddc": {
    "controlmyMonitor_path": "C:\\Tools\\ControlMyMonitor.exe",
    "monitor_id": "\\\\.\\DISPLAY1\\Monitor0",
    "m1ddc_path": "/opt/homebrew/bin/m1ddc",
    "display_index": 1,
    "vcp_code": "0x60",
    "mac_input": 15,
    "win_input": 17,
    "command_template": null,
    "read_template": null,
    "read_regex": null,
    "settle": 1.2,
    "timeout": 10.0,
    "retries": 2,
    "retry_delay": 0.4
  },
  "hotkeys": { "switch_to_mac": "ctrl+alt+1", "switch_to_win": "ctrl+alt+2" },
  "network": {
    "ping_interval": 10.0,
    "ack_timeout": 4.0,
    "reconnect_interval": 3.0,
    "reconnect_max_interval": 30.0,
    "connect_wait": 4.0
  },
  "discovery": {
    "enabled": false,
    "port": 8766,
    "interval": 5.0,
    "ttl": 15.0,
    "broadcast": "255.255.255.255"
  },
  "web": {
    "enabled": true,
    "host": "127.0.0.1",
    "port": 8767
  },
  "auth": {
    "secret": "用 python main.py --gen-secret 生成，两端填同一个值"
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `role` | `server`（Windows，注册热键）或 `client`（macOS，只响应 TCP） |
| `peer_host` | 对端地址；`"auto"` = 启用局域网自动发现 |
| `peer_port` / `listen_port` | 对端端口 / 本机监听端口（两端都用 8765 即可） |
| `tray` | 是否显示托盘图标（也可用 `--no-tray` 临时关闭） |
| `debounce` | 同一目标的重复触发间隔（秒） |
| `ddc.display_index` | macOS 上 m1ddc 的显示器编号（`m1ddc display list` 查看） |
| `ddc.vcp_code` | 输入源 VCP 代码，默认 `0x60`（十进制 96） |
| `ddc.mac_input` / `ddc.win_input` | 显示器接 Mac / Windows 的输入源值，**必须以实际读取为准** |
| `ddc.command_template` | 自定义命令模板，字符串或数组；占位符 `{tool}` `{monitor}` `{vcp}` `{display_index}` `{value}` |
| `ddc.read_template` | 自定义「读取当前输入源」命令模板（同一套占位符）；用于 BetterDisplay CLI 等其它工具 |
| `ddc.read_regex` | 从读取命令输出里抓数值的正则（含捕获组取第 1 组）；不填则按平台约定解析 |
| `ddc.settle` | 切换后等待显示器稳定的秒数（默认 1.2），`--detect-inputs` 探测与校验时使用 |
| `ddc.retries` | DDC 失败重试次数（显示器偶发无响应很常见，默认重试 2 次） |
| `hotkeys.*` | 仅 `server` 生效；必须与 Deskflow 中的热键一致 |
| `network.ack_timeout` | 等待对端切换确认的超时 |
| `discovery.broadcast` | 广播地址，可写数组；留空则只用「按本机网段推导的 x.y.z.255」 |
| `web.enabled` | 是否启动本地 Web 控制台（默认开；也可用 `--no-web` 临时关闭） |
| `web.host` / `web.port` | 控制台监听地址与端口，默认 `127.0.0.1:8767`；改成非回环地址会有告警 |
| `auth.secret` | 预共享密钥（两端一致）；留空 = 不校验，见 §6.3 |

输入源常见值：DP1=15、DP2=16、HDMI1=17、HDMI2=18、USB-C=27（VCP 0x60）。

## 6. 运行方式

```bash
python main.py                 # 常驻运行：托盘 + 全局热键
python main.py --status        # 自检：配置 / DDC 工具 / 当前输入源 / 对端连通性
python main.py --once mac      # 执行一次完整切换后退出（验证 DDC + 通知链路）
python main.py --detect-inputs # 识别显示器输入源取值并写回配置（交互式向导）
python main.py --dry-run       # 只打印将执行的 DDC 命令，不动硬件
python main.py --print-config  # 打印合并默认值后的生效配置
python main.py --no-tray       # 无托盘后台运行
python main.py --web-port 9000 # Web 控制台换端口（默认 8767）
python main.py --no-web        # 关闭 Web 控制台，只留托盘/热键
python main.py --log-level DEBUG
```

| 退出码 | 含义 |
| --- | --- |
| 0 | 正常 |
| 1 | `--once` 切换失败；或 `--detect-inputs` 取值缺失/回读不一致 |
| 2 | 配置错误 |
| 3 | 监听端口被占用（通常已有实例在运行） |
| 4 | 其他启动失败 |

### 6.1 Web 控制台（默认开启）

常驻运行时会额外起一个**只监听 `127.0.0.1`** 的本地网页，启动日志里会打印带令牌的地址：

```
2026-10-16 14:12:37 [webui] INFO: Web 控制台已启动：http://127.0.0.1:8767/?t=OaftWnJt...
```

直接点开即可（`--no-tray` 时该地址也会打到 stderr）。页面上能做的事：

| 区块 | 内容 |
| --- | --- |
| 显示器所有权 | 两台机器分列一条发丝栅格的两端，蓝色块停在当前持有方；切换时蓝块沿栅格滑动，两端数字（`ddc.mac_input` / `ddc.win_input`）用等宽字形跟着换色 |
| 操作 | 切到 Mac / 切到 Windows / 读取当前输入源 / 探测输入源（可自定义候选值，留空用内置候选集） |
| 连接 | 角色、监听端口、最近切换目标、对端地址与连接状态、对端身份、对端密钥校验结果、自动发现到的设备 |
| 显示器 DDC | 工具路径与可用性、按当前配置拼好的三条命令行（可直接复制到终端核对）、Mac/Windows 取值、当前输入源与上次读取时间 |
| 热键 | 组合键、是否注册成功、失败原因（例如未安装 `keyboard`、缺少管理员权限） |
| 配置 | 白名单字段可视化编辑；保存前用 `config.validate` 校验，校验不过不落盘；标记「需重启」的字段会明确告知要重启才生效 |
| 事件 | 运行中进程的日志尾部（内存环形缓冲，不额外落盘），倒序 |

值一律来自 `/api/status`、`/api/config`、`/api/events` 三个接口的**真实数据**：
读不到就显示「读取不到」，未知就显示「—」，页面不编造任何数值。

安全模型（三层，缺一不可）：

1. 只绑定 `127.0.0.1`，不对外网暴露（改 `web.host` 会有告警，因为那等于把切换能力交给局域网）；
2. 每次启动生成随机令牌，写在地址栏的 `?t=` 里，页面之后用 `X-QuickSwitch-Token` 头调用 API —— 自定义头会触发 CORS 预检，跨站的表单/图片式请求打不进来；
3. 校验 `Host` 与 `Origin` 是否为本机，阻断 DNS rebinding（恶意站点把自己的域名解析到 `127.0.0.1`）。

配置里的 `web.enabled` 可关掉整个控制台；临时关闭用 `--no-web`，改端口用 `--web-port`。
若端口被别的程序占用，只会在日志里报错，切换功能不受影响。

设计方向（前端一次性写死，不分主题）：走 **Swiss** 锚点 —— `#F7F7F8` 底 + 纯白面板、单一强调色 Yves Klein Blue `#002FA7`、Helvetica Neue/Inter 单一无衬线族、1px 发丝线与可见 12 栏栅格、左对齐、数字一律 tabular 并当构图元素用。
不同点只有一处，就是「所有权转移条」：两台机器摆在同一条栅格的两端，切换时蓝块在栅格内滑动、数字换色 —— 状态迁移发生在栅格里，而不是弹一个模态框。
没有暖纸底、没有衬线、没有颗粒纹理、没有装饰性图标与 `//` 式伪注释。

### 6.2 开机自启


- Windows：管理员 PowerShell 执行 `powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1 -ProjectDir C:\Tools\quickswitch`
  （注册为「登录时启动 + 最高权限运行」；也可把 `quickstart-windows.bat` 的快捷方式放进 `shell:startup`）
- macOS：`cp scripts/com.quickswitch.agent.plist ~/Library/LaunchAgents/` 后 `launchctl load -w ...`
  （需先把 plist 里的 `/Users/CHANGE_ME/quickswitch` 换成实际路径；用「系统设置 → 通用 → 登录项」更简单）

### 6.3 局域网访问控制（预共享密钥）

**默认不设防**：`auth.secret` 为空时，同一网段里任何机器只要往 `listen_port` 发一行
`{"type":"switch","target":"mac"}`，显示器就会被切走。家用 LAN 通常可以接受，但只要
网络里有访客设备、物联网设备或公共 WiFi，就该打开这一层。

```bash
python3 main.py --gen-secret
# {"auth": {"secret": "ReKQONRgzal_BnZAnRya3bx_FpCbLXQaLe1dDGSB5zk"}}
```

把这一行合并进**两端**的 `~/.quickswitch/config.json`（或者控制台页面「配置」区块里改别的字段、
密钥直接改文件），重启两端后确认：

```
python3 main.py --status
  对端         : 192.168.1.101:8765 已连接（对端 DESKTOP-ABC，role=server，版本=1.1.0）
  密钥校验     : 已通过（hmac-sha256）
```

行为说明：

| 情形 | 结果 |
| --- | --- |
| 两端密钥一致 | 连接建立后自动完成质询-响应，日志出现「对端密钥校验通过」 |
| 两端密钥不一致 | 双方都校验不通过；对端发来的 `switch` / `switch_ack` 被丢弃（日志「丢弃未经校验的报文：校验码不匹配」），本端也不再发送会被丢弃的通知 |
| 一端配置、一端没配 | 没配的那端发出的切换请求会被丢弃；`--status` 会明确提示是哪一边没配 |
| 都没配 | 与旧版本行为一致，直连直切 |

设计取舍：

- 只拦报文、不拦连接：校验失败时连接与心跳照常，便于从日志和 `--status` 看出「两端配置不一致」，
  而不是表现为「连不上」这种更难排查的故障；
- 密钥不上网：网络上只传 HMAC 校验码；每条连接一个随机 nonce，且校验码同时绑定对方的 nonce，
  因此抓包拿到的旧校验码无法重放（换 nonce 就算不出新校验码）；比较用 `hmac.compare_digest` 常量时间；
- 不防「中间人转发」：转发者拿不到密钥，改不了报文、也造不出新报文，只是把两条连接接在一起，
  而在两端本就互相信任的前提下这没有额外收益；
- Web 控制台不暴露 `auth.secret`（不在可编辑白名单里），页面只显示校验状态，密钥只在配置文件里。

## 7. 打包与分发（PyInstaller + 签名 / 公证）

内置脚本把「源码 + 依赖 + 前端」打成一个双击即用的成品，再按平台要求签名：

| 平台 | 产物 | 签名方式 | 脚本 |
| --- | --- | --- | --- |
| Windows | `dist/QuickSwitch.exe`（**单文件**，自带 Python 运行时与 `ui/`） | Authenticode（`signtool`）+ 时间戳 | `packaging/build-windows.ps1` |
| macOS | `dist/QuickSwitch.app`（**onedir 应用包**，自带 `ui/`） | codesign（Developer ID + hardened runtime）→ 公证 → 装订 | `packaging/build-macos.sh` |

两端的目标机都不需要预装 Python 或依赖。版本号只有一处来源：`main.py` 的 `VERSION`（当前 `1.1.0`），
spec 与 Windows 脚本都从它派生 `CFBundleShortVersionString` / `version_info.txt`（`1.1.0.0`），不会两处漂移。
图标由 `packaging/make-icons.py` 用 Pillow 现画（Klein Blue 圆角方块 + 两台显示器 + 箭头），
`icon.png` / `icon.ico` / `icon.icns` 已随仓库提供，构建时若缺失会自动重画。

### 7.1 macOS：打包 → 签名 → 公证

公证（notarization）是 Apple 的强制要求：**只签名不公证**的 .app 从网上下载后仍会被 Gatekeeper 拦下。
所以脚本默认把「签名 → 公证 → 装订」一次做完。

一次性准备（需要 Apple Developer Program，99 美元/年）：

```bash
# 1) 在 developer.apple.com 签发「Developer ID Application」证书并装进钥匙串
security find-identity -v -p codesigning      # 应能看到该证书

# 2) 生成 App 专用密码（appleid.apple.com → 登录与安全），存进钥匙串（只需一次）
xcrun notarytool store-credentials quickswitch-notary \
  --apple-id "you@example.com" --team-id TEAMID --password "app-specific-password"
```

之后构建：

```bash
packaging/build-macos.sh --check                                   # 只做环境自检（没证书也能跑，会把缺项列出来）
packaging/build-macos.sh --no-sign                                 # 只出未签名 .app，本机自测
packaging/build-macos.sh --keychain-profile quickswitch-notary     # 签名 + 公证（自动挑 Developer ID 身份）
packaging/build-macos.sh --identity "Developer ID Application: X (TEAMID)" \
                         --keychain-profile quickswitch-notary \
                         --zip dist/QuickSwitch-1.1.0.zip          # 顺便导出一份可分发 zip
```

脚本按顺序做：预检（工具链 / 图标 / 签名身份 / 公证凭据）→ 建 `.venv-build` 并装依赖 → PyInstaller →
`codesign --options runtime --timestamp --entitlements` → `codesign --verify --deep --strict` →
`ditto` 打 zip → `notarytool submit --wait` → 未通过时自动打印 `notarytool log` → `stapler staple` + `validate` →
`spctl -a -vvv` 做 Gatekeeper 评估 → 打印 SHA256。

macOS 版刻意选 **onedir + .app** 而不是 onefile：onefile 会在运行时把内容解到临时目录，
公证对「包里再套未签名二进制」很敏感，而且启动更慢。`packaging/entitlements.plist` 是最小 hardened runtime 集合——
只打开网络客户端/服务端，不放开 JIT 与库校验。

### 7.2 Windows：打包 → Authenticode 签名

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -Check
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 -NoSign
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 `
    -PfxPath C:\certs\quickswitch.pfx -PfxPassword $env:QS_PFX_PASSWORD
powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1 `
    -CertSubject "CN=Your Company, O=Your Company, C=CN"       # 证书已装进证书存储时
```

签名材料三选一：

| 方式 | 参数 | 说明 |
| --- | --- | --- |
| pfx 文件 | `-PfxPath` + `-PfxPassword` | 最常用；脚本调 `signtool /f /p` |
| pfx 的 base64 | 环境变量 `QS_PFX_BASE64` | 给 CI 用（不必把证书文件写进构建目录）；脚本解到临时文件，签完立刻删除 |
| 证书存储 | `-CertSubject` | 证书已装进 `Cert:\CurrentUser\My` / `Cert:\LocalMachine\My` 时，用 `/n` 按主题选 |

脚本按顺序做：预检（Python / signtool / 签名材料，并核对 `main.py` 的 `VERSION` 与 `version_info.txt` 一致）→
建 `.venv-build` 并装依赖 → PyInstaller → `signtool sign /fd sha256 /tr <时间戳> /td sha256` →
`signtool verify /pa` → `Get-AuthenticodeSignature` 复核 → 打印 SHA256 与后续步骤。

`signtool.exe` 会自动定位：PATH → vswhere（Visual Studio）→ `Windows Kits\10\bin`，装了 Windows SDK 就能直接用。

未签名（`-NoSign`）的 exe 首次运行会弹 SmartScreen「Windows 已保护你的电脑」；普通 OV 证书也只是逐步积累信誉，
**要立刻消除警告需要 EV 证书或走 Microsoft Store 分发**。

### 7.3 打包脚本开关

| 开关 | 平台 | 作用 |
| --- | --- | --- |
| `--check` / `-Check` | 两边 | 只做环境自检（工具链、图标、证书/签名材料、公证凭据），不构建 |
| `--no-sign` / `-NoSign` | 两边 | 只产出未签名产物，本地自测用 |
| `--skip-notarize` | macOS | 签名但不公证（验证 hardened runtime 时可先跑到这一步） |
| `--identity` / `-CertSubject` | 两边 | 指定签名身份；macOS 不指定则自动挑 Developer ID，Windows 必须显式给 pfx 或证书主题 |
| `--keychain-profile` | macOS | notarytool 凭据名（不传就跳过公证） |
| `--dist` / `--work` / `--venv` / `--python` / `-Python` | 两边 | 改产物目录、中间目录、构建虚拟环境与解释器 |
| `--zip` | macOS | 额外导出一份公证后装订过的分发 zip |
| `-SkipInstall` | Windows | 复用已有 `.venv-build`，跳过 pip 安装（CI 缓存用） |

`dist/`、`build/`、`.venv-build/` 都是可丢弃的中间状态，不必提交。

### 7.4 打包后才出现的行为

这几处只在「打包运行」时生效，源码直跑看不到：

| 行为 | 实现 | 说明 |
| --- | --- | --- |
| 前端随包分发 | `webui._ui_dir()` 优先取 `sys._MEIPASS/ui` | 打包后没有源码目录，`ui/` 由 spec 的 `datas=[(…/ui, "ui")]` 装进包里，控制台页面照常 |
| Windows 控制台输出 | `main.attach_parent_console()` | Windows 版是 `console=False`（不弹黑窗），但 `QuickSwitch.exe --status` 在 cmd 里仍看得到输出：启动时 `AttachConsole(-1)` 附着到父控制台 |
| 菜单栏程序 | `LSUIElement = True` | macOS 版不进 Dock、不抢焦点，只在菜单栏出图标 |
| 局域网权限描述 | `NSLocalNetworkUsageDescription` | macOS 15+ 访问局域网会弹权限框，缺这条会被系统静默拒绝 |
| 不压缩 | `upx = False` | UPX 处理过的可执行文件会破坏签名/公证 |
| 退出方式 | 托盘菜单「退出」 | 托盘运行时主线程停在 Cocoa 事件循环，`SIGTERM` 不会被处理（日志里连「收到信号」都不会出现），自动化脚本停进程请用 `kill -9`；`--no-tray` 运行时 SIGTERM 正常退出 |

装到目标机后还需要（与源码部署一致，见 §4）：

- Windows：放行监听端口
  `netsh advfirewall firewall add rule name="QuickSwitch" dir=in action=allow protocol=TCP localport=8765`；
  注册全局热键需要管理员权限（托盘图标本身不需要）。
- macOS：Deskflow 需要「辅助功能」权限；首次被 QuickSwitch 访问局域网时允许弹窗。

### 7.5 本目录的验证状态

- **已验证**：`packaging/build-macos.sh --no-sign` 在本机真实跑通 PyInstaller 打包，产出 `QuickSwitch.app`（约 25 MB）；
  该 .app 的冻结二进制实测 `--help` / `--print-config` / `--status` / `--gen-secret` 全部正常；
  `--no-tray` 启动后 Web 控制台从包内 `ui/` 提供页面（`/` 返回 HTML、`/app.css` 200、`/api/status` 带令牌返回真实状态）；
  `packaging/make-icons.py --check` 通过。
- **未验证**（本机不具备条件）：真实 `codesign` / `notarytool` / `stapler` 全流程——本机只装了 Command Line Tools，
  钥匙串里没有 Developer ID 证书，`build-macos.sh --check` 会明确报出这两项缺失（预期行为）；
  Windows 的 `build-windows.ps1` 全流程——本机没有 Windows / PowerShell。两个脚本的逻辑已逐条核对，
  首次在真实签名环境运行时建议先跑 `--check` / `-Check` 看缺什么。

## 8. 验收清单（对应方案第 9 节）

### 8.1 DDC 双向控制

Windows（显示器当前显示 Windows）：

```bat
C:\Tools\ControlMyMonitor.exe /SetValue "\\.\DISPLAY1\Monitor0" 0x60 15
C:\Tools\ControlMyMonitor.exe /GetValue "\\.\DISPLAY1\Monitor0" 0x60

:: 显示器保持显示 Mac，再从 Windows 后台执行，验证能否反向切回（对应方案 10.1）
C:\Tools\ControlMyMonitor.exe /SetValue "\\.\DISPLAY1\Monitor0" 0x60 17
```

macOS：

```bash
m1ddc display list
m1ddc display 1 get input         # 读取当前输入源（源码支持，README 未列出）
m1ddc display 1 set input 15      # 切到 Mac
m1ddc display 1 set input 17      # 切到 Windows（需显示器支持在非激活输入源上执行 DDC）
```

各平台读取当前输入源的能力（`--status` 与 `--detect-inputs` 依赖它）：

| 平台 | 读取命令 | 说明 |
| --- | --- | --- |
| Windows | `ControlMyMonitor.exe /GetValue <monitor> 0x60` | 读 `Current Value` 行 |
| macOS | `m1ddc display <n> get input` | 只输出一个整数；显示器不支持读 VCP 0x60 时命令失败 → 视为读不到 |
| Linux | `ddcutil --display <n> getvcp 60 --terse` | 输出形如 `VCP 60 SNC 0x000f` |
| 任意 | `ddc.read_template` + `ddc.read_regex` | 用别的工具（如 BetterDisplay CLI）读取时改这两项即可 |

> 若 `monitor_id` 或 `display_index` 与上面不同，先修正配置；
> 若你的 ControlMyMonitor 版本命令行不同，用 `ddc.command_template` 覆盖即可，无需改代码。

### 8.2 自动识别输入源取值

不想手工找 `mac_input` / `win_input` 时，直接跑向导（Windows / macOS / Linux 都可用，
前提是「能读当前输入源」，见 §8.1 的能力表）：

```bash
python main.py --detect-inputs
```

它会：

1. 读取当前输入源，并逐一试探候选取值（默认 `1,3,15,16,17,18,27`），每次 `set` 后**回读确认**；
2. 逐值切换并询问「屏幕上是 Windows 桌面 / Mac 桌面 / 黑屏」（探测与询问之间会恢复原输入源，
   所以回答问题的时候屏幕已经切回来了）；
3. 回读校验两个取值，然后写入 `config.json`，并打印一行可直接复制到另一端的 JSON；
4. 结束后恢复原始输入源。

常用变体：

```bash
python main.py --detect-inputs --mac-input 15 --win-input 17   # 已知取值，跳过提问
python main.py --detect-inputs --candidates 15,17,27           # 自定义候选取值
python main.py --detect-inputs --no-probe                      # 不探测，只校验并写配置
python main.py --detect-inputs --no-write                      # 只看结果，不改配置
python main.py --detect-inputs --non-interactive --win-input 17 --mac-input 15  # 脚本/自启用
```

说明与限制：

- 探测期间屏幕会短暂切换/黑屏，属正常现象；结束会切回原来的输入源。
- 取值是**显示器属性**，同一台显示器在两端一致，所以一端识别成功后，把打印出的 JSON 复制到另一端即可。
- 若显示器不接受「在非激活输入源上执行 DDC」，探测可能只看到当前那一个值；此时用 §8.1 的手动方式确认。
- 退出码 `0` = 识别成功且回读一致；`1` = 取值缺失或回读不一致（配置仍会写入，需人工复核）。

### 8.3 QuickSwitch 链路

```bash
# 两端各自自检
python main.py --status

# Windows 端整体验证（本地 DDC + 通知 Mac）
python main.py --once mac --dry-run     # 先看将要执行的命令
python main.py --once mac               # 真实切换
```

期望日志：

- Windows：`本地 DDC → Mac：成功`、`本地 DDC 成功；对端 DDC 成功`；
- macOS：`收到对端切换请求：→ Mac`、`本地 DDC → Mac：成功`、`接受来自 <Windows IP> 的连接`。

### 8.4 快捷键与其余组件

- 按 `Ctrl+Alt+1`：显示器切到 Mac、键鼠控制权交给 Mac，两端日志各出现一条切换记录；
- 按 `Ctrl+Alt+2`：切回 Windows；
- 杀掉一端进程：另一端应打印「对端连接断开（会自动重连）」，重启后自动恢复；
- 修改对端 IP 后（Tailscale 或 `"auto"` 模式）无需改配置；
- 剪贴板：Windows 复制文本 → Mac 粘贴，再反向验证（Deskflow）；
- 文件传输：两端打开 LocalSend 应能互相发现并发送文件。

### 8.5 预共享密钥（配了 `auth.secret` 才需要跑）

```bash
# 两端都配同一个密钥后重启，各跑一次：
python3 main.py --status          # 应显示「密钥校验：已通过（hmac-sha256）」
```

- 两端日志应出现「对端密钥校验通过」，且**没有**「丢弃未经校验的报文」；
- 故意把一端的密钥改错再重启：那端日志出现「丢弃未经校验的报文：校验码不匹配」，
  `--status` 显示「未通过」，此时按热键本机照常切换、但对端不动（符合预期）；
- 不改任何硬件也能验证这条链路：`python3 scripts/selftest.py` 里包含 3 项对应用例
  （正确密钥通过、错误密钥的连接能建立但切换被拒、错误密钥没能触发切换）。

## 9. 故障排查

| 现象 | 处理 |
| --- | --- |
| 启动报「监听端口 8765 不可用」 | 已有一个 QuickSwitch 在跑（退出码 3）；或改 `listen_port` |
| 按热键无反应 | 需管理员权限（用 `install-task.ps1` 或「以管理员身份运行」）；检查是否与其他软件抢占热键；`--log-level DEBUG` 看是否注册成功 |
| 只切了显示器和键鼠之一 | 检查 Deskflow 与 `config.json` 的热键是否完全一致 |
| 显示器不切换，日志显示 DDC 失败 | 见 §8.1 手动验证；确认 `monitor_id` / `display_index`；显示器需支持并开启 DDC/CI（部分型号需在 OSD 菜单打开）；建议视频线直连、避免转接 |
| `--status` 显示「无法读取」 | 显示器不支持读 VCP 0x60（部分型号如此），或工具/路径不对。此时仍可正常切换，只是无法回读校验：取值请用 §8.1 手动确认，或用 `ddc.read_template` 换一个支持读取的工具 |
| `--detect-inputs` 探测不到可用值 | 先确认 §8.1 的手动 set/get 是否都能成功；显示器若不允许在非激活输入源上 DDC，就用手动方式确认两个值后 `--detect-inputs --mac-input 15 --win-input 17` |
| 识别出的两个值反了 | 直接 `--detect-inputs --mac-input <值> --win-input <值>` 覆盖，或手动改 `config.json`；两个值的配对不影响底层机制 |
| Windows 能切到 Mac，Mac 侧切不回 Windows | 方案 10.1 描述的现象：非激活输入源不支持 DDC。Mac 端常驻 QuickSwitch 后由 TCP 通知切换即可；仍不行则用显示器自动信号源或按钮兜底 |
| 日志出现「对端未确认」 | 对端程序没在跑、防火墙挡了 8765、或 `peer_host` 指错。用 `python main.py --status` 检查连通性 |
| 日志出现「丢弃未经校验的报文」 | 两端 `auth.secret` 不一致（或对端没配）。用 `--status` 看「密钥校验」一行，两端填同一个密钥后重启 |
| 设了密钥后完全切不过去 | 确认两端进程都重启过（密钥在启动时读取），且没有多余空格；`--status` 显示「未完成」通常是对端版本过旧（不支持 `auth_challenge`） |
| macOS 提示应用「已损坏」 | `xattr -c /Applications/Deskflow.app`，并重新授予辅助功能权限 |
| 在同一台机器上测试两端 | 用 `--config` 指定两份配置（不同 `listen_port`），或让 `peer_host` 明确指向 127.0.0.1 |
| IP 经常变化 | 用 Tailscale，或 `"peer_host": "auto"`；若广播被阻断，把 `discovery.broadcast` 改成网段广播地址（如 `192.168.1.255`） |

## 10. 通信协议

JSON Lines（一行一条 JSON）走 TCP，默认端口 8765；两端都会主动连接对方并监听本地端口。

| 类型 | 方向 | 字段 | 说明 |
| --- | --- | --- | --- |
| `hello` | 连接建立后（两端都发） | `name` `role` `version` `platform` `listen_port` `auth` | 身份交换，日志中可看到对端是谁；`auth` 声明是否要求密钥校验 |
| `auth_challenge` | 双向 | `nonce` `alg` | 仅配置了 `auth.secret` 时发送；`nonce` 每条连接随机生成 |
| `auth_response` | 双向 | `nonce` `mine` `alg` `mac` | 对质询的回答：`mac = HMAC-SHA256(secret, "quickswitch-auth-1\|<对端 nonce>\|<本端 nonce>")` |
| `ping` / `pong` | 双向 | `ts` | 心跳，默认 10s；3 个周期无任何消息则重连 |
| `switch` | 双向 | `target` `id` `source` | 请求对端切换输入源 |
| `switch_ack` | 双向 | `id` `ok` `detail` | 对端执行结果，超时默认 4s |

校验流程（未配置 `auth.secret` 时整段跳过）：

1. 连接建立后双方各发一个 `hello` 和一个 `auth_challenge`（被连的一方也发，保证单向可达时同样能校验）；
2. 收到对方的质询就用同一个密钥算一个 `auth_response` 回去，**只发校验码，不发密钥**；
3. 校验本连接收到的回复：nonce 必须与本连接一致（挡重放）、校验码必须匹配（挡伪造与密钥不一致）；
4. 通过之前，该连接上的 `switch` / `switch_ack` 一律丢弃并在日志里留下「丢弃未经校验的报文」。

## 11. 测试

```bash
python3 -m unittest discover -s tests -t . -v
```

覆盖：配置合并/校验、Windows-macOS-Linux 命令构建与模板、重试与超时、输入源解析、
TCP 双向收发/心跳/重连/端口占用/单向可达回退、设备发现报文与设备表、
两端完整切换链路（含 ACK 与负向 ACK、去抖、本地失败但对端成功）、Web 控制台
（令牌、Host/Origin 防护、状态结构、切换、配置白名单与「需重启」判定、静态文件与目录穿越防护）、
预共享密钥（校验码绑定两个 nonce、密钥不一致时丢弃 switch 与伪造的 switch_ack、
一端未配置、重放旧校验码被拒、`--gen-secret` 与 `--status` 输出不泄漏密钥）、
命令行子进程冒烟测试。

想在不动硬件的情况下看界面，可以用模拟 DDC 工具起一个实例（真的会执行「DDC」命令，
只是这个命令是个写状态文件的脚本）：

```bash
# 用一个只接受 15/17 的假 m1ddc 代替真工具
cat > /tmp/mock-m1ddc.sh <<'EOF'
#!/bin/bash
[ -f /tmp/qs-state ] || echo 17 > /tmp/qs-state
if [ "$3" = "get" ]; then cat /tmp/qs-state; exit 0; fi
case "$5" in 15|17) echo "$5" > /tmp/qs-state; exit 0 ;; *) exit 1 ;; esac
EOF
chmod +x /tmp/mock-m1ddc.sh
python3 main.py --no-tray --print-config > /tmp/qs.json   # 再把 ddc.m1ddc_path 改成 /tmp/mock-m1ddc.sh
python3 main.py --no-tray --config /tmp/qs.json
```

在真机上不想动硬件时，可先跑本机链路自检：它会在临时配置下启动一个真实的
QuickSwitch 进程（`--no-tray --dry-run`），再作为「对端」完成 hello / 心跳 /
switch / switch_ack 全流程，并检查守护进程日志：

```bash
python3 scripts/selftest.py
```

## 12. 参考

- Deskflow：https://github.com/deskflow/deskflow
- ControlMyMonitor：https://www.nirsoft.net/utils/control_my_monitor.html
- m1ddc：https://github.com/waydabber/m1ddc
- ddcutil：https://www.ddcutil.com/
- LocalSend：https://github.com/localsend/localsend
- Tailscale：https://tailscale.com/
