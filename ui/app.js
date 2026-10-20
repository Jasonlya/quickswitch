/* QuickSwitch 控制台 —— 前端只渲染 /api/status、/api/config、/api/events 里真实存在的数据。
   读不到的字段一律显示「读取不到」或「—」，不猜测、不补样例值。 */
"use strict";

const LABEL = { mac: "Mac", win: "Windows" };
const DASH = "—";

const params = new URLSearchParams(location.search);
const token = params.get("t") || sessionStorage.getItem("quickswitch-token") || "";
if (token) sessionStorage.setItem("quickswitch-token", token);

const $ = (id) => document.getElementById(id);

function text(value) {
  if (value === null || value === undefined || value === "") return DASH;
  return String(value);
}

function block(value) {
  if (value === null || value === undefined || value === "") return "读取不到";
  return String(value);
}

function yesNo(value) {
  if (value === null || value === undefined) return DASH;
  return value ? "是" : "否";
}

function authText(auth) {
  if (!auth) return DASH;
  if (!auth.enabled) return "未启用（配置里没设 auth.secret，同网段任何程序都能发切换请求）";
  if (auth.authenticated) return "已通过（hmac-sha256）";
  if (!auth.peer) return "未通过（对端没声明校验：旧版本，或没配 auth.secret）";
  return "未通过（两端 auth.secret 不一致，对端发来的切换请求会被丢弃）";
}

function clock(iso) {
  if (!iso) return DASH;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toTimeString().slice(0, 8);
}

let noticeTimer = null;

function notify(message, tone) {
  const box = $("notice");
  box.textContent = message;
  box.dataset.tone = tone || "info";
  box.hidden = false;
  if (noticeTimer) clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { box.hidden = true; }, 6000);
}

async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers["X-QuickSwitch-Token"] = token;
  if (opts.body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(path, opts);
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    throw new Error("令牌无效：请用启动日志里带 ?t= 的完整地址打开控制台");
  }
  if (!response.ok) {
    throw new Error(payload.error || ("HTTP " + response.status));
  }
  return payload;
}

/* ---------------------------------------------------------------- 状态渲染 */
function rows(target, entries) {
  target.textContent = "";
  for (const [label, value] of entries) {
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (value instanceof Node) dd.appendChild(value);
    else dd.textContent = value;
    row.append(dt, dd);
    target.appendChild(row);
  }
}

function dot(on) {
  const span = document.createElement("span");
  span.className = "dot";
  span.dataset.on = on ? "true" : "false";
  return span;
}

function flag(text, on) {
  const wrap = document.createElement("span");
  wrap.append(dot(on), document.createTextNode(text));
  return wrap;
}

let lastStatus = null;
let pendingTarget = null;

function renderOwnership(status) {
  const local = status.role === "server" ? "win" : "mac";
  const peerInfo = (status.peer && status.peer.info) || null;
  const input = status.input || {};
  const owner = input.target || null;

  const names = {};
  const platforms = {};
  for (const side of ["mac", "win"]) {
    const isLocal = side === local;
    names[side] = isLocal ? status.host.name : (peerInfo && peerInfo.name) || null;
    platforms[side] = isLocal ? status.host.platform : (peerInfo && peerInfo.platform) || null;
  }

  $("ownerMacHost").textContent = block(names.mac) + " · " + text(platforms.mac);
  $("ownerWinHost").textContent = block(names.win) + " · " + text(platforms.win);
  $("ownerMacValue").textContent = text(status.ddc.mac_input);
  $("ownerWinValue").textContent = text(status.ddc.win_input);
  $("ownerMac").dataset.owner = owner === "mac" && !pendingTarget ? "true" : "false";
  $("ownerWin").dataset.owner = owner === "win" && !pendingTarget ? "true" : "false";

  const rail = $("rail");
  const target = pendingTarget || owner || "mac";
  rail.dataset.target = target === "win" ? "win" : "mac";
  rail.dataset.live = pendingTarget || owner ? "true" : "false";
  rail.dataset.pending = pendingTarget ? "true" : "false";

  const state = $("ownershipState");
  if (pendingTarget) {
    state.textContent = "切换中：正在切到 " + LABEL[pendingTarget] + "（等待本机 DDC 与对端确认）";
  } else if (owner) {
    state.textContent =
      "当前持有：" + LABEL[owner] + " ｜ 显示器输入源 " + text(input.value) +
      " ｜ 读取于 " + clock(input.read_at);
  } else if (input.error) {
    state.textContent = "读取不到当前输入源（" + input.error + "）";
  } else if (input.read_at) {
    state.textContent = "读取不到当前输入源 ｜ 上次读取于 " + clock(input.read_at) +
      " ｜ 该工具或显示器不支持回读 VCP 0x60，可配置 ddc.read_template";
  } else {
    state.textContent = "读取不到当前输入源（尚未执行过回读，点「读取当前输入源」或用 --status 核对）";
  }
}

function renderStatus(status) {
  lastStatus = status;
  $("metaHost").textContent = text(status.host.name);
  $("metaPlatform").textContent = text(status.host.platform);
  $("metaVersion").textContent = text(status.version);
  $("metaSampled").textContent = clock(status.generated_at);

  const discovery = status.discovery || {};
  const peers = discovery.peers || {};
  const peerNames = Object.keys(peers);

  rows($("kvLink"), [
    ["角色", text(status.role) + (status.role === "server" ? "（本机为 Windows 侧）" : "（本机为 macOS 侧）")],
    ["监听端口", text(status.listen_port)],
    ["最近切换目标", status.state ? LABEL[status.state] : DASH],
    ["dry-run", yesNo(status.dry_run)],
    ["对端地址", text(status.peer.host) + ":" + text(status.peer.port)],
    ["对端连接", flag(status.peer.connected ? "已连接" : "未连接", status.peer.connected)],
    ["对端出站连接", yesNo(status.peer.outbound)],
    ["对端身份", status.peer.info
      ? text(status.peer.info.name) + " · " + text(status.peer.info.platform) +
        " · v" + text(status.peer.info.version)
      : block(null)],
    ["对端密钥校验", authText(status.auth)],
    ["自动发现", discovery.enabled
      ? "已启用，发现 " + peerNames.length + " 台：" +
        (peerNames.length ? peerNames.join("、") : block(null))
      : "未启用"],
  ]);

  rows($("kvFiles"), [
    ["配置文件", text(status.config_path)],
    ["日志文件", text(status.log_file)],
    ["采样时间", clock(status.generated_at)],
    ["事件序号", text(status.events_seq)],
  ]);

  const ddc = status.ddc;
  rows($("kvDdc"), [
    ["工具", text(ddc.tool_path)],
    ["工具可用", flag(ddc.tool_ok ? "可用" : "不可用", ddc.tool_ok)],
    ["工具说明", text(ddc.tool_detail)],
    ["显示器 id", text(ddc.monitor_id)],
    ["display_index", text(ddc.display_index)],
    ["Mac 输入源", text(ddc.mac_input)],
    ["Windows 输入源", text(ddc.win_input)],
    ["稳定等待", text(ddc.settle) + " 秒"],
    ["当前输入源", status.input.value === null || status.input.value === undefined
      ? block(status.input.error) : text(status.input.value)],
    ["上次读取", clock(status.input.read_at)],
  ]);

  const commands = $("cmds");
  commands.textContent = "";
  const commandRows = [
    ["切换到 Mac", ddc.cmd_mac],
    ["切换到 Windows", ddc.cmd_win],
    ["读取当前输入源", ddc.read_command],
  ];
  for (const [label, value] of commandRows) {
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    if (Array.isArray(value) && value.length) {
      dd.textContent = value.join(" ");
    } else {
      dd.textContent = "该平台无内置命令，可用 ddc.command_template / ddc.read_template 指定";
      dd.className = "empty";
    }
    row.append(dt, dd);
    commands.appendChild(row);
  }

  rows($("kvHotkeys"), [
    ["切到 Mac", text(status.hotkeys.switch_to_mac)],
    ["切到 Windows", text(status.hotkeys.switch_to_win)],
    ["注册状态", flag(status.hotkeys.registered ? "已注册" : "未注册", status.hotkeys.registered)],
    ["说明", text(status.hotkeys.reason)],
  ]);

  $("probeDefault").textContent = (status.probe_candidates || []).join(", ") || DASH;
  renderOwnership(status);
}

/* ---------------------------------------------------------------- 操作 */
function busy(on, message) {
  for (const id of ["btnMac", "btnWin", "btnRead", "btnProbe", "btnSave"]) {
    $(id).disabled = on;
  }
  const state = $("runState");
  state.dataset.busy = on ? "true" : "false";
  if (message !== undefined) state.textContent = message;
}

async function switchTo(target) {
  pendingTarget = target;
  if (lastStatus) renderOwnership(lastStatus);
  busy(true, "切换中：→ " + LABEL[target] + " …");
  try {
    const result = await api("/api/switch", { method: "POST", body: { target } });
    busy(false, result.ok
      ? "已完成：→ " + LABEL[target] + "（本机 DDC 或对端 DDC 至少一侧确认成功）"
      : "未确认：→ " + LABEL[target] + "，详见事件日志");
  } catch (error) {
    busy(false, "切换失败：" + error.message);
    notify(String(error.message), "error");
  } finally {
    pendingTarget = null;
    await refreshStatus();
    refreshEvents();
  }
}

async function readInput() {
  busy(true, "读取中：正在执行读取命令 …");
  try {
    const result = await api("/api/read-input", { method: "POST", body: {} });
    busy(false, result.value === null || result.value === undefined
      ? "读取不到当前输入源（工具不支持回读，或显示器未响应）"
      : "当前输入源：" + result.value + "（" + (LABEL[result.target] || "未匹配 mac_input / win_input") + "）");
  } catch (error) {
    busy(false, "读取失败：" + error.message);
  }
  await refreshStatus();
}

async function probeInputs() {
  const raw = $("probeCandidates").value.trim();
  const body = {};
  if (raw) {
    const values = raw.split(/[,\s]+/).filter(Boolean).map(Number);
    if (values.some((value) => !Number.isInteger(value) || value < 0 || value > 255)) {
      notify("候选值必须是 0-255 的整数，用逗号分隔", "error");
      return;
    }
    body.candidates = values;
  }
  const started = Date.now();
  busy(true, "探测中：逐个试探候选值并回读确认，显示器会闪几次 …");
  const ticker = setInterval(() => {
    $("runState").textContent =
      "探测中：已用 " + Math.round((Date.now() - started) / 1000) + " 秒，请等显示器稳定 …";
  }, 1000);
  try {
    const result = await api("/api/probe-inputs", { method: "POST", body });
    const found = result.available || [];
    busy(false, "探测完成：接受 " + found.length + " 个取值" +
      (found.length ? "（" + found.join(", ") + "）" : "") + "，已恢复原输入源");
  } catch (error) {
    busy(false, "探测失败：" + error.message);
  } finally {
    clearInterval(ticker);
  }
  await refreshStatus();
}

/* ---------------------------------------------------------------- 配置 */
const CONFIG_FIELDS = [
  { group: "基础", fields: [
    { path: "role", type: "select", options: ["server", "client"] },
    { path: "peer_host", type: "text" },
    { path: "peer_port", type: "number", min: 1, max: 65535 },
    { path: "listen_port", type: "number", min: 1, max: 65535 },
    { path: "debounce", type: "number", min: 0, step: 0.1 },
    { path: "log_level", type: "select", options: ["DEBUG", "INFO", "WARNING", "ERROR"] },
  ] },
  { group: "热键", fields: [
    { path: "hotkeys.switch_to_mac", type: "text" },
    { path: "hotkeys.switch_to_win", type: "text" },
  ] },
  { group: "网络", fields: [
    { path: "network.ack_timeout", type: "number", min: 0.1, step: 0.5 },
    { path: "network.ping_interval", type: "number", min: 0.1, step: 0.5 },
    { path: "network.reconnect_interval", type: "number", min: 0.1, step: 0.5 },
    { path: "network.reconnect_max_interval", type: "number", min: 0.1, step: 0.5 },
    { path: "network.connect_wait", type: "number", min: 0.1, step: 0.5 },
  ] },
  { group: "自动发现", fields: [
    { path: "discovery.enabled", type: "checkbox" },
    { path: "discovery.port", type: "number", min: 1, max: 65535 },
    { path: "discovery.interval", type: "number", min: 0.1, step: 0.5 },
    { path: "discovery.ttl", type: "number", min: 1, step: 0.5 },
    { path: "discovery.broadcast", type: "text" },
  ] },
  { group: "显示器 DDC", fields: [
    { path: "ddc.monitor_id", type: "text" },
    { path: "ddc.display_index", type: "number", min: 0 },
    { path: "ddc.mac_input", type: "number", min: 0, max: 255 },
    { path: "ddc.win_input", type: "number", min: 0, max: 255 },
    { path: "ddc.vcp_code", type: "text" },
    { path: "ddc.settle", type: "number", min: 0, step: 0.1 },
    { path: "ddc.timeout", type: "number", min: 0.1, step: 0.5 },
    { path: "ddc.retries", type: "number", min: 0 },
    { path: "ddc.retry_delay", type: "number", min: 0, step: 0.1 },
    { path: "ddc.controlmyMonitor_path", type: "text" },
    { path: "ddc.m1ddc_path", type: "text" },
    { path: "ddc.ddcutil_path", type: "text" },
    { path: "ddc.command_template", type: "textarea" },
    { path: "ddc.read_template", type: "textarea" },
    { path: "ddc.read_regex", type: "text" },
  ] },
];

const INPUTS = new Map();

function getPath(object, path) {
  return path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), object);
}

function setPath(object, path, value) {
  const keys = path.split(".");
  let node = object;
  for (const key of keys.slice(0, -1)) {
    if (typeof node[key] !== "object" || node[key] === null) node[key] = {};
    node = node[key];
  }
  node[keys[keys.length - 1]] = value;
}

function needsRestart(path) {
  return restartPrefixes.some((prefix) =>
    path === prefix.replace(/\.$/, "") || path.startsWith(prefix));
}

function configValue(input) {
  if (input.type === "checkbox") return input.checked;
  if (input.type === "number") {
    if (input.value.trim() === "") return null;
    return Number(input.value);
  }
  return input.value === "" ? null : input.value;
}

let configBase = null;
let restartPrefixes = [];

function renderConfig(view) {
  configBase = view.values;
  restartPrefixes = view.restart_prefixes || [];
  INPUTS.clear();
  const host = $("configGroups");
  host.textContent = "";
  for (const group of CONFIG_FIELDS) {
    const section = document.createElement("div");
    section.className = "config-group";
    const title = document.createElement("h3");
    title.textContent = group.group;
    const fields = document.createElement("div");
    fields.className = "config-fields";
    for (const field of group.fields) {
      const value = getPath(configBase, field.path);
      const wrap = document.createElement("div");
      wrap.className = "config-field";
      wrap.dataset.path = field.path;
      const label = document.createElement("label");
      label.setAttribute("for", "cfg-" + field.path);
      label.textContent = field.path;
      if (needsRestart(field.path)) {
        const mark = document.createElement("span");
        mark.className = "flag";
        mark.textContent = " 需重启";
        label.appendChild(mark);
      }
      let input;
      if (field.type === "select") {
        input = document.createElement("select");
        for (const option of field.options) {
          const el = document.createElement("option");
          el.value = option;
          el.textContent = option;
          input.appendChild(el);
        }
        input.value = value == null ? "" : String(value);
      } else if (field.type === "textarea") {
        input = document.createElement("textarea");
        input.value = value == null ? "" : (Array.isArray(value) ? value.join("\n") : String(value));
      } else {
        input = document.createElement("input");
        input.type = field.type;
        if (field.min !== undefined) input.min = field.min;
        if (field.max !== undefined) input.max = field.max;
        if (field.step !== undefined) input.step = field.step;
        if (field.type === "checkbox") input.checked = Boolean(value);
        else input.value = value == null ? "" : String(value);
      }
      input.id = "cfg-" + field.path;
      input.addEventListener("input", () => markDirty(field.path));
      input.addEventListener("change", () => markDirty(field.path));
      wrap.append(label, input);
      fields.appendChild(wrap);
      INPUTS.set(field.path, input);
    }
    section.append(title, fields);
    host.appendChild(section);
  }
}

function markDirty(path) {
  const wrap = $("configGroups").querySelector('[data-path="' + path + '"]');
  const input = INPUTS.get(path);
  const original = getPath(configBase, path);
  const current = configValue(input);
  const same = typeof original === "number" && typeof current === "number"
    ? original === current
    : String(original == null ? "" : original) === String(current == null ? "" : current);
  if (wrap) wrap.dataset.dirty = same ? "false" : "true";
}

function collectChanges() {
  const patch = {};
  for (const [path, input] of INPUTS) {
    const original = getPath(configBase, path);
    const current = configValue(input);
    const before = original == null ? "" : (Array.isArray(original) ? original.join("\n") : String(original));
    const after = current == null ? "" : (Array.isArray(current) ? current.join("\n") : String(current));
    if (before === after) continue;
    let value = current;
    if (input.tagName === "TEXTAREA" && typeof value === "string") {
      value = value.split("\n").map((line) => line.trim()).filter(Boolean);
      value = value.length === 1 ? value[0] : (value.length === 0 ? null : value);
    }
    setPath(patch, path, value);
  }
  return patch;
}

async function saveConfig() {
  const values = collectChanges();
  if (!Object.keys(values).length) {
    $("configState").textContent = "没有改动，未提交。";
    return;
  }
  $("configState").textContent = "保存中 …";
  try {
    const result = await api("/api/config", { method: "POST", body: { values } });
    let message = "已保存 " + result.changed.length + " 项：" + result.changed.join("、");
    if (result.restart_required) {
      message += " ｜ 需重启生效：" + result.restart_keys.join("、");
    }
    $("configState").textContent = message;
    renderConfig({ values: result.values, restart_prefixes: restartPrefixes });
    await refreshStatus();
  } catch (error) {
    $("configState").textContent = "保存失败：" + error.message;
    notify("配置未保存：" + error.message, "error");
  }
}

/* ---------------------------------------------------------------- 事件 */
let lastSeq = 0;

function renderEvents(records) {
  const body = $("eventRows");
  if (!records.length) {
    if (!body.rows.length) {
      const row = body.insertRow();
      const cell = row.insertCell();
      cell.colSpan = 4;
      cell.className = "empty";
      cell.textContent = "还没有事件；切换、读取与连接都会写到这里。";
    }
    return;
  }
  const placeholder = body.querySelector(".empty");
  if (placeholder) placeholder.parentNode.remove();
  for (const record of records.slice().reverse()) {
    const row = body.insertRow(0);
    const time = row.insertCell();
    time.className = "num";
    time.textContent = clock(record.ts);
    const level = row.insertCell();
    level.className = "lvl-" + record.level;
    level.textContent = record.level;
    const logger = row.insertCell();
    logger.textContent = record.logger;
    const message = row.insertCell();
    message.textContent = record.message;
  }
  while (body.rows.length > 200) body.deleteRow(body.rows.length - 1);
}

async function refreshEvents() {
  try {
    const payload = await api("/api/events?since=" + lastSeq);
    lastSeq = Math.max(lastSeq, payload.last_seq || 0);
    if (payload.events && payload.events.length) renderEvents(payload.events);
  } catch (error) {
    /* 事件流短暂失败不打断页面，下一轮继续 */
  }
}

/* ---------------------------------------------------------------- 轮询 */
async function refreshStatus() {
  try {
    const status = await api("/api/status");
    renderStatus(status);
    $("footerState").textContent = "最近一次采样 " + clock(status.generated_at) +
      " ｜ 主机 " + text(status.host.name) + " ｜ 角色 " + text(status.role);
  } catch (error) {
    $("footerState").textContent = "状态读取失败：" + error.message;
  }
}

async function bootstrap() {
  if (!token) {
    notify("地址里没有令牌。请用启动日志里带 ?t= 的完整地址打开：http://127.0.0.1:<端口>/?t=<令牌>", "error");
    return;
  }
  $("btnMac").addEventListener("click", () => switchTo("mac"));
  $("btnWin").addEventListener("click", () => switchTo("win"));
  $("btnRead").addEventListener("click", readInput);
  $("btnProbe").addEventListener("click", probeInputs);
  $("btnSave").addEventListener("click", saveConfig);
  $("btnRevert").addEventListener("click", () => api("/api/config").then(renderConfig));

  await refreshStatus();
  try {
    renderConfig(await api("/api/config"));
  } catch (error) {
    $("configState").textContent = "配置读取失败：" + error.message;
  }
  await refreshEvents();

  setInterval(() => { if (!document.hidden) refreshStatus(); }, 2000);
  setInterval(() => { if (!document.hidden) refreshEvents(); }, 3000);
}

bootstrap();
