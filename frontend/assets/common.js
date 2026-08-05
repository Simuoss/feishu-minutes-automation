const API = window.APP_CONFIG?.apiBase ?? "http://127.0.0.1:7354/api/v1";
const ADMIN_TOKEN_KEY = "minutes_admin_token";

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
  const d = Number.isFinite(n) && n > 1e12 ? new Date(n) : new Date(String(raw).replace(/\./g, "-"));
  if (Number.isNaN(d.getTime())) return String(raw);
  return d.toLocaleString("zh-CN");
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

function getAdminToken() {
  return localStorage.getItem(ADMIN_TOKEN_KEY) || "";
}

function setAdminToken(token) {
  localStorage.setItem(ADMIN_TOKEN_KEY, token);
}

function clearAdminToken() {
  localStorage.removeItem(ADMIN_TOKEN_KEY);
  clearAccessTicket();
}

function requireAdminPage() {
  if (getAdminToken()) return true;
  const next = `${location.pathname}${location.search}`;
  location.replace(`/login.html?next=${encodeURIComponent(next)}`);
  return false;
}

function logoutAdmin() {
  clearAdminToken();
  location.replace("/login.html");
}

/** EventSource / <video src> 无法带 Authorization，用短期票挂到 query（不是 ADMIN_TOKEN） */
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
  accessTicketState.expiresAt = Date.now() + (Number(data.expires_in_seconds) || 7200) * 1000;
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
  const token = getAdminToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const url = path.startsWith("http") ? path : `${API}${path.startsWith("/") ? "" : "/"}${path}`;
  const res = await fetch(url, { ...options, headers });
  if (res.status === 401 && !url.includes("/auth/admin/login")) {
    const peek = await res.clone().json().catch(() => ({}));
    // 飞书 OAuth 缺失也会 401，但 detail 是带 login_url 的对象，不能清管理员登录态
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
  try {
    const res = await apiFetch("/auth/feishu/status");
    if (!res.ok) return;
    const data = await res.json();
    authState.loginUrl = data.login_url || "";
    authState.reauthUrl = data.reauth_url || data.login_url || "";

    const banner = $("#auth-banner");
    const loginLink = $("#login-link");
    const reauthBtn = $("#reauth-btn");
    const authStatus = $("#auth-status");

    if (reauthBtn) reauthBtn.href = authState.reauthUrl || "#";

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
}

function bindAdminNav() {
  const logoutBtn = $("#logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", (e) => {
    e.preventDefault();
    logoutAdmin();
  });
}
