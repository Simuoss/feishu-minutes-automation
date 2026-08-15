const API = window.APP_CONFIG?.apiBase ?? "http://127.0.0.1:7354/api/v1";
const USER_JWT_KEY = "minutes_user_jwt";
const SUPER_JWT_KEY = "minutes_super_jwt";
const VIEW_MODE_KEY = "minutes_admin_view_mode";
/** 兼容旧 key，读一次后迁移 */
const LEGACY_ADMIN_TOKEN_KEY = "minutes_admin_token";

const $ = (sel) => document.querySelector(sel);

const authState = {
  loginUrl: "",
  reauthUrl: "",
};

function formatDuration(ms) {
  if (!ms) return null;
  const sec = Math.floor(ms / 1000);
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function formatTime(raw) {
  if (!raw) return null;
  const n = Number(raw);
  let d;
  if (Number.isFinite(n) && n > 1e11) {
    // 秒级 / 毫秒级时间戳
    d = new Date(n > 1e12 ? n : n * 1000);
  } else {
    const s = String(raw).trim();
    // 仅飞书式 YYYY.MM.DD… 把日期点换成横杠；勿动 ISO 毫秒小数点，否则会解析失败甩出原串
    const normalized = /^\d{4}\.\d{1,2}\.\d{1,2}\b/.test(s)
      ? s.replace(/^(\d{4})\.(\d{1,2})\.(\d{1,2})/, "$1-$2-$3")
      : s;
    d = new Date(normalized);
  }
  if (Number.isNaN(d.getTime())) return String(raw);
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** datetime-local → 毫秒时间戳；非法则 null */
function localInputToMs(value) {
  if (!value) return null;
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) return null;
  return ms;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * 在容器内生成二维码（依赖 /assets/vendor/qrcode.min.js 的 QRCode）。
 * @param {HTMLElement | null} container
 * @param {string} text
 * @param {number} [size]
 */
function fillQrCode(container, text, size = 160) {
  if (!container) return;
  container.innerHTML = "";
  const value = (text || "").trim();
  if (!value) {
    container.innerHTML = `<span class="muted">无链接</span>`;
    return;
  }
  if (typeof QRCode === "undefined") {
    container.innerHTML = `<span class="muted">二维码组件未加载</span>`;
    return;
  }
  // eslint-disable-next-line no-new
  new QRCode(container, {
    text: value,
    width: size,
    height: size,
    correctLevel: QRCode.CorrectLevel.M,
  });
}

/**
 * 分享创建结果卡 HTML：链接 + 二维码 + 复制；密钥区可选。
 * @param {{ url: string, keyHint?: string, keyPlain?: string | null, title?: string }} opts
 */
function shareResultCardHtml(opts) {
  const url = opts.url || "";
  const title = opts.title || "分享已就绪";
  const keyPlain = (opts.keyPlain || "").trim();
  const keyHint = (opts.keyHint || "").trim();
  const keyBlock = keyPlain
    ? `<div class="share-result-key">
        <p class="meeting-meta">访问密钥（请一并告知访客）</p>
        <code class="share-result-code">${escapeHtml(keyPlain)}</code>
        <button type="button" class="btn btn-sm" data-copy-text="${escapeHtml(keyPlain)}">${btnContent("key-2-line", "复制密钥")}</button>
      </div>`
    : keyHint
      ? `<p class="modal-hint share-result-key-hint">${escapeHtml(keyHint)}</p>`
      : "";
  return `
    <div class="share-result-card">
      <p class="share-result-title">${escapeHtml(title)}</p>
      <div class="share-result-body">
        <div class="share-result-qr" data-share-qr></div>
        <div class="share-result-info">
          <label class="field field-wide">
            <span>分享链接</span>
            <input type="text" readonly value="${escapeHtml(url)}" data-share-url-input />
          </label>
          <div class="share-result-actions">
            <button type="button" class="btn btn-primary btn-sm" data-copy-text="${escapeHtml(url)}">${btnContent("file-copy-line", "复制链接")}</button>
          </div>
          ${keyBlock}
        </div>
      </div>
    </div>`;
}

async function copyTextToClipboard(text) {
  const value = String(text || "");
  if (!value) return false;
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    window.prompt("复制失败，请手动全选复制：", value);
    return false;
  }
}

/** 本会话暂存刚创建的密钥明文，便于分享结果卡一键复制（关闭标签页即失效）。 */
function rememberAccessKeyPlaintext(keyId, plaintext) {
  const id = Number(keyId);
  const value = (plaintext || "").trim();
  if (!Number.isFinite(id) || id <= 0 || !value) return;
  try {
    sessionStorage.setItem(`access_key_plain:${id}`, value);
  } catch {
    /* ignore */
  }
}

function loadAccessKeyPlaintext(keyId) {
  const id = Number(keyId);
  if (!Number.isFinite(id) || id <= 0) return "";
  try {
    return sessionStorage.getItem(`access_key_plain:${id}`) || "";
  } catch {
    return "";
  }
}

const ACCESS_LOG_ACTION_LABELS = {
  OPEN: "打开分享",
  UNLOCK_OK: "解锁成功",
  UNLOCK_FAIL: "解锁失败",
  SHARE_REVOKED: "访问已失效分享",
  VIEW_SUMMARY: "查看纪要",
  VIEW_TRANSCRIPT: "查看转写",
  PLAY_VIDEO: "播放视频",
  EXPORT_SUMMARY: "导出纪要",
  EXPORT_TRANSCRIPT: "导出转写",
  SESSION_END: "结束访问",
  VIEW: "访问（旧）",
  EXPORT: "导出（旧）",
  MEDIA: "媒体请求（旧）",
};

const ACCESS_LOG_RESULT_LABELS = {
  SUCCESS: "成功",
  FAIL: "失败",
};

const ACCESS_LOG_FAIL_LABELS = {
  BAD_KEY: "密钥错误",
  KEY_EXPIRED: "密钥已过期",
  KEY_REVOKED: "密钥已吊销",
  SHARE_REVOKED: "分享已取消",
  EXPORT_ERROR: "导出失败",
};

const ACCESS_LOG_DEVICE_LABELS = {
  MOBILE: "手机",
  DESKTOP: "电脑",
  TABLET: "平板",
  UNKNOWN: "未知设备",
};

/** 会话摘要里优先展示的动作（有则作为标题） */
const ACCESS_LOG_SUMMARY_PRIORITY = [
  "UNLOCK_FAIL",
  "SHARE_REVOKED",
  "UNLOCK_OK",
  "OPEN",
  "VIEW_SUMMARY",
  "VIEW_TRANSCRIPT",
  "PLAY_VIDEO",
  "EXPORT_SUMMARY",
  "EXPORT_TRANSCRIPT",
  "SESSION_END",
  "VIEW",
  "EXPORT",
  "MEDIA",
];

function accessLogLabel(map, value) {
  if (value == null || value === "") return "";
  const key = String(value);
  return map[key] || key;
}

function formatAccessLogDwell(ms) {
  if (ms == null || ms < 0) return "";
  const sec = Math.round(Number(ms) / 1000);
  if (sec < 60) return `${sec} 秒`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s ? `${m} 分 ${s} 秒` : `${m} 分`;
}

function formatAccessLogClock(raw) {
  const d = new Date(typeof raw === "number" && raw < 1e12 ? raw * 1000 : Number(raw));
  if (Number.isNaN(d.getTime())) return "-";
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  const ss = String(d.getSeconds()).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

/**
 * 按 session_id 聚拢同一次访问；无 session 的旧日志各自成组。
 * @param {object[]} logs
 * @returns {{ key: string, logs: object[], latestAt: number }[]}
 */
function groupAccessLogsBySession(logs) {
  const map = new Map();
  for (const log of logs || []) {
    const key = log.session_id ? `s:${log.session_id}` : `solo:${log.id}`;
    let group = map.get(key);
    if (!group) {
      group = { key, logs: [], latestAt: 0 };
      map.set(key, group);
    }
    group.logs.push(log);
    const t = Number(log.created_at) || 0;
    if (t > group.latestAt) group.latestAt = t;
  }
  const groups = [...map.values()];
  for (const g of groups) {
    g.logs.sort((a, b) => (Number(a.created_at) || 0) - (Number(b.created_at) || 0));
  }
  groups.sort((a, b) => b.latestAt - a.latestAt);
  return groups;
}

function pickAccessLogSummaryTitle(logs) {
  const actions = new Set(logs.map((l) => l.action));
  for (const action of ACCESS_LOG_SUMMARY_PRIORITY) {
    if (actions.has(action)) return accessLogLabel(ACCESS_LOG_ACTION_LABELS, action);
  }
  const first = logs[0];
  return first ? accessLogLabel(ACCESS_LOG_ACTION_LABELS, first.action) : "访问";
}

function summarizeAccessLogGroup(logs) {
  const first = logs[0] || {};
  const last = logs[logs.length - 1] || first;
  let dwellMs = null;
  let peak = null;
  let failReason = "";
  let hasFail = false;
  let referer = "";
  let device = "";
  let os = "";
  let browser = "";
  let ip = first.ip || "";
  let minuteToken = first.minute_token || "";
  let meetingTitle = first.meeting_title || "";
  let shareId = first.share_id;
  const actionSet = new Set();

  for (const log of logs) {
    actionSet.add(log.action);
    if (log.result === "FAIL") hasFail = true;
    if (!failReason && log.fail_reason) failReason = log.fail_reason;
    if (log.dwell_ms != null && (dwellMs == null || Number(log.dwell_ms) > dwellMs)) {
      dwellMs = Number(log.dwell_ms);
    }
    if (
      log.video_progress_pct != null &&
      (peak == null || Number(log.video_progress_pct) > peak)
    ) {
      peak = Number(log.video_progress_pct);
    }
    if (!referer && log.referer) referer = log.referer;
    if (!device && log.device_type) device = log.device_type;
    if (!os && log.os) os = log.os;
    if (!browser && log.browser) browser = log.browser;
    if (!ip && log.ip) ip = log.ip;
    if (!minuteToken && log.minute_token) minuteToken = log.minute_token;
    if (!meetingTitle && log.meeting_title) meetingTitle = log.meeting_title;
    if (shareId == null && log.share_id != null) shareId = log.share_id;
  }

  if (dwellMs == null && first.created_at != null && last.created_at != null) {
    const span = Number(last.created_at) - Number(first.created_at);
    if (span > 0) dwellMs = span;
  }

  const contentBits = [];
  if (actionSet.has("VIEW_SUMMARY")) contentBits.push("纪要");
  if (actionSet.has("VIEW_TRANSCRIPT")) contentBits.push("转写");
  if (actionSet.has("PLAY_VIDEO") || peak != null) contentBits.push("视频");
  if (actionSet.has("EXPORT_SUMMARY") || actionSet.has("EXPORT_TRANSCRIPT") || actionSet.has("EXPORT")) {
    contentBits.push("导出");
  }

  return {
    title: pickAccessLogSummaryTitle(logs),
    startedAt: first.created_at,
    endedAt: last.created_at,
    dwellMs,
    peak,
    hasFail,
    failReason,
    referer,
    env: [accessLogLabel(ACCESS_LOG_DEVICE_LABELS, device), os, browser].filter(Boolean).join(" / "),
    ip: ip || "-",
    minuteToken,
    meetingTitle: meetingTitle || minuteToken || "未知会议",
    shareId,
    contentBits,
    sessionId: first.session_id || "",
  };
}

/**
 * @param {object} log
 */
function renderAccessLogEventLine(log) {
  const action = accessLogLabel(ACCESS_LOG_ACTION_LABELS, log.action);
  const result = accessLogLabel(ACCESS_LOG_RESULT_LABELS, log.result);
  const fail = accessLogLabel(ACCESS_LOG_FAIL_LABELS, log.fail_reason);
  const bits = [result, fail].filter(Boolean);
  if (log.action === "PLAY_VIDEO" && log.video_progress_pct != null) {
    bits.push(`进度 ${log.video_progress_pct}%`);
  }
  if (log.action === "SESSION_END" && log.dwell_ms != null) {
    bits.push(`停留 ${formatAccessLogDwell(log.dwell_ms)}`);
  }
  return `<li class="access-log-event">
    <span class="access-log-event-time">${escapeHtml(formatAccessLogClock(log.created_at))}</span>
    <span class="access-log-event-action">${escapeHtml(action)}${
      bits.length ? ` · ${escapeHtml(bits.join(" · "))}` : ""
    }</span>
  </li>`;
}

/**
 * @param {{ logs: object[] }} group
 * @param {{ showShareId?: boolean }} [options]
 */
function renderAccessLogGroup(group, options = {}) {
  const logs = group.logs || [];
  const summary = summarizeAccessLogGroup(logs);
  const sharePart =
    options.showShareId && summary.shareId != null ? ` · 分享 #${summary.shareId}` : "";
  const statusLabel = summary.hasFail
    ? accessLogLabel(ACCESS_LOG_FAIL_LABELS, summary.failReason) || "失败"
    : "成功";
  const statusClass = summary.hasFail ? "is-fail" : "is-ok";
  const metaBits = [
    summary.env,
    summary.dwellMs != null ? `停留 ${formatAccessLogDwell(summary.dwellMs)}` : "",
    summary.peak != null ? `播放峰值 ${summary.peak}%` : "",
    summary.contentBits.length ? `看过 ${summary.contentBits.join("、")}` : "",
  ].filter(Boolean);
  const timeRange =
    summary.startedAt === summary.endedAt
      ? formatTime(summary.startedAt)
      : `${formatTime(summary.startedAt)} → ${formatAccessLogClock(summary.endedAt)}`;

  return `
    <li class="meeting-item access-log-session">
      <div class="meeting-body">
        <p class="meeting-title">
          <span class="access-log-status ${statusClass}">${escapeHtml(statusLabel)}</span>
          ${escapeHtml(summary.meetingTitle)}
        </p>
        <p class="meeting-meta">${escapeHtml(summary.title)} · ${escapeHtml(timeRange)} · IP ${escapeHtml(summary.ip)}${sharePart}${
          metaBits.length ? ` · ${escapeHtml(metaBits.join(" · "))}` : ""
        }</p>
        ${
          summary.referer
            ? `<p class="meeting-meta">来源 ${escapeHtml(summary.referer)}</p>`
            : ""
        }
        <ul class="access-log-events">
          ${logs.map(renderAccessLogEventLine).join("")}
        </ul>
      </div>
    </li>`;
}

/**
 * @param {object[]} logs
 * @param {{ showShareId?: boolean }} [options]
 */
function renderAccessLogGroups(logs, options = {}) {
  return groupAccessLogsBySession(logs)
    .map((group) => renderAccessLogGroup(group, options))
    .join("");
}

/** Remix Icon：传入不含 ri- 前缀的类名片段，如 "search-line" */
function iconHtml(name) {
  return `<i class="ri ri-${name}" aria-hidden="true"></i>`;
}

function btnContent(iconName, label) {
  if (!label) return iconHtml(iconName);
  return `${iconHtml(iconName)}<span class="btn-label">${escapeHtml(label)}</span>`;
}

function setBtnContent(btn, iconName, label) {
  if (!btn) return;
  btn.innerHTML = btnContent(iconName, label);
}

/** 偏慢页面的统一加载态文案 */
function thinkingHtml({ block = false } = {}) {
  const cls = block ? "thinking thinking-block" : "thinking";
  return `<div class="${cls}" aria-live="polite" aria-busy="true"><span class="thinking-pulse">Thinking...</span></div>`;
}

function showThinking(el, { block = true } = {}) {
  if (!el) return;
  el.innerHTML = thinkingHtml({ block });
}

function setThinkingStatus(el) {
  if (!el) return;
  el.innerHTML = thinkingHtml({ block: false });
}

function getUserJwt() {
  const legacy = localStorage.getItem(LEGACY_ADMIN_TOKEN_KEY);
  if (legacy && !localStorage.getItem(USER_JWT_KEY)) {
    // 旧全局口令不能当 JWT 用，清掉以免误以为已登录
    localStorage.removeItem(LEGACY_ADMIN_TOKEN_KEY);
  }
  return localStorage.getItem(USER_JWT_KEY) || "";
}

function setUserJwt(token) {
  localStorage.setItem(USER_JWT_KEY, token);
  localStorage.removeItem(LEGACY_ADMIN_TOKEN_KEY);
}

function clearUserJwt() {
  localStorage.removeItem(USER_JWT_KEY);
  localStorage.removeItem(LEGACY_ADMIN_TOKEN_KEY);
}

function getSuperJwt() {
  return localStorage.getItem(SUPER_JWT_KEY) || "";
}

function setSuperJwt(token) {
  localStorage.setItem(SUPER_JWT_KEY, token);
}

function clearSuperJwt() {
  localStorage.removeItem(SUPER_JWT_KEY);
}

function getAdminViewMode() {
  const mode = localStorage.getItem(VIEW_MODE_KEY);
  if (mode === "super" && getSuperJwt()) return "super";
  return "user";
}

function setAdminViewMode(mode) {
  if (mode === "super" && getSuperJwt()) {
    localStorage.setItem(VIEW_MODE_KEY, "super");
    return;
  }
  localStorage.setItem(VIEW_MODE_KEY, "user");
}

function isSuperAdminView() {
  return getAdminViewMode() === "super";
}

/** 当前请求使用的 Bearer：超管模式用超管 JWT，否则用户 JWT */
function getActiveBearer() {
  if (isSuperAdminView() && getSuperJwt()) return getSuperJwt();
  return getUserJwt();
}

function getAdminToken() {
  return getActiveBearer();
}

function setAdminToken(token) {
  setUserJwt(token);
}

function clearAdminToken() {
  clearUserJwt();
  clearSuperJwt();
  localStorage.removeItem(VIEW_MODE_KEY);
  clearAccessTicket();
}

function requireAdminPage() {
  if (getUserJwt()) return true;
  // 首页飞书回调带 sso_ticket：允许脚本继续，由 finalizeSsoLoginFromQuery 兑换
  const params = new URLSearchParams(location.search);
  if (params.get("sso_ticket") && (location.pathname === "/" || location.pathname.endsWith("/index.html"))) {
    return true;
  }
  const next = `${location.pathname}${location.search}`;
  location.replace(`/login.html?next=${encodeURIComponent(next)}`);
  return false;
}

/** 兑换飞书 SSO 短码为 JWT；返回是否需要显示名设置弹窗 */
async function finalizeSsoLoginFromQuery() {
  const params = new URLSearchParams(location.search);
  const ticket = (params.get("sso_ticket") || "").trim();
  let setupName =
    params.get("setup_name") === "1" ||
    sessionStorage.getItem("setup_display_name") === "1";
  if (ticket) {
    try {
      const res = await fetch(`${API}/auth/feishu/sso-exchange`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticket }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg =
          typeof data.detail === "string"
            ? data.detail
            : "飞书登录凭证无效，请重试";
        location.replace(
          `/login.html?auth=error&msg=${encodeURIComponent(msg)}`
        );
        return { ok: false, setupName: false };
      }
      if (data.token) {
        setUserJwt(data.token);
        setAdminViewMode("user");
        clearAccessTicket();
      }
      if (data.setup_name) setupName = true;
    } catch {
      location.replace("/login.html?auth=error&msg=" + encodeURIComponent("飞书登录兑换失败"));
      return { ok: false, setupName: false };
    }
  }
  if (!getUserJwt()) {
    const next = `${location.pathname}${location.search}`;
    location.replace(`/login.html?next=${encodeURIComponent(next)}`);
    return { ok: false, setupName: false };
  }
  if (ticket || params.get("auth") || params.get("setup_name") || params.get("feishu")) {
    const clean = new URL(location.href);
    ["sso_ticket", "auth", "feishu", "setup_name", "msg"].forEach((k) =>
      clean.searchParams.delete(k)
    );
    history.replaceState({}, "", clean.pathname + clean.search + clean.hash);
  }
  if (setupName) sessionStorage.setItem("setup_display_name", "1");
  return { ok: true, setupName };
}

function feishuLoginUrl({ invite } = {}) {
  const qs = new URLSearchParams({ intent: "login" });
  if (invite) qs.set("invite", invite);
  return `${API}/auth/feishu/login?${qs}`;
}

function logoutAdmin() {
  clearAdminToken();
  location.replace("/login.html");
}

/** EventSource / <video src> 无法带 Authorization，用短期票挂到 query */
const accessTicketState = {
  ticket: "",
  expiresAt: 0,
};

function clearAccessTicket() {
  accessTicketState.ticket = "";
  accessTicketState.expiresAt = 0;
}

async function ensureAccessTicket() {
  if (accessTicketState.ticket && Date.now() < accessTicketState.expiresAt - 60_000) {
    return accessTicketState.ticket;
  }
  const res = await apiFetch("/auth/admin/access-ticket", { method: "POST" });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || "无法获取媒体访问票");
  }
  const data = await res.json();
  if (!data.access_ticket) {
    throw new Error("媒体访问票响应缺少 access_ticket，怀疑后端版本不匹配");
  }
  accessTicketState.ticket = data.access_ticket;
  accessTicketState.expiresAt = Date.now() + (Number(data.expires_in_seconds) || 1800) * 1000;
  return accessTicketState.ticket;
}

function withAccessTicketQuery(url) {
  const ticket = accessTicketState.ticket;
  if (!ticket) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}access_ticket=${encodeURIComponent(ticket)}`;
}

function withShareSessionQuery(url, sessionId) {
  if (!sessionId) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}share_session=${encodeURIComponent(sessionId)}`;
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getActiveBearer();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const url = path.startsWith("http") ? path : `${API}${path.startsWith("/") ? "" : "/"}${path}`;
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401 && !url.includes("/auth/login") && !url.includes("/auth/admin/login")) {
    const peek = await res.clone().json().catch(() => ({}));
    if (peek.detail === "需要管理员登录") {
      clearAdminToken();
      const next = `${location.pathname}${location.search}`;
      location.replace(`/login.html?next=${encodeURIComponent(next)}`);
      throw new Error("需要管理员登录");
    }
  }
  return res;
}

async function downloadWithAuth(path, filenameHint) {
  const res = await apiFetch(path);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  const blob = await res.blob();
  const disp = res.headers.get("Content-Disposition") || "";
  const match = /filename="?([^"]+)"?/i.exec(disp);
  const filename = match?.[1] || filenameHint || "download";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

/**
 * 更新页头飞书授权状态。列表页可传 onMissingScopes 处理权限横幅。
 */
async function checkAuth({ onMissingScopes } = {}) {
  const banner = $("#auth-banner");
  const loginLink = $("#login-link");
  const reauthBtn = $("#reauth-btn");
  const authStatus = $("#auth-status");

  // 超管无个人飞书身份：跳过 /auth/feishu/status，避免无谓 403/鉴权开销
  if (isSuperAdminView()) {
    banner?.classList.add("hidden");
    if (authStatus) {
      authStatus.textContent = "超级管理端 · 全站";
      authStatus.className = "auth-status is-warn";
    }
    if (reauthBtn) {
      reauthBtn.href = "#";
      reauthBtn.onclick = (e) => {
        e.preventDefault();
        alert("超级管理员模式无法进行飞书个人授权，请先切换回管理端");
      };
    }
    return;
  }

  try {
    const res = await apiFetch("/auth/feishu/status");
    if (!res.ok) return;
    const data = await res.json();
    authState.loginUrl = data.login_url || "";
    authState.reauthUrl = data.reauth_url || data.login_url || "";

    if (reauthBtn) {
      reauthBtn.href = "#";
      const label = data.feishu_bound ? "重新授权" : "绑定飞书";
      const labelEl = reauthBtn.querySelector(".btn-label");
      if (labelEl) labelEl.textContent = label;
      reauthBtn.onclick = async (e) => {
        e.preventDefault();
        try {
          const ticket = await ensureAccessTicket();
          const base =
            authState.reauthUrl ||
            "/api/v1/auth/feishu/login?intent=bind&reauth=1";
          const sep = base.includes("?") ? "&" : "?";
          location.href = `${base}${sep}access_ticket=${encodeURIComponent(ticket)}`;
        } catch (err) {
          alert(err.message || "无法开始飞书授权");
        }
      };
    }

    if (!data.authorized) {
      banner?.classList.remove("hidden");
      if (loginLink) loginLink.href = authState.loginUrl || "#";
      if (authStatus) {
        authStatus.textContent = "飞书未授权";
        authStatus.className = "auth-status is-off";
      }
    } else {
      banner?.classList.add("hidden");
      if (data.needs_reauth && data.missing_scopes?.length) {
        if (authStatus) {
          authStatus.textContent = "飞书授权不完整";
          authStatus.className = "auth-status is-warn";
        }
        onMissingScopes?.(data.missing_scopes);
      } else if (authStatus) {
        authStatus.textContent = "已登录";
        authStatus.className = "auth-status is-on";
      }
    }
  } catch {
    /* ignore */
  }
  if (typeof maybePromptDisplayName === "function") {
    maybePromptDisplayName();
  }
}

function bindAdminNav() {
  const logoutBtn = $("#logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", (e) => {
    e.preventDefault();
    logoutAdmin();
  });
}
