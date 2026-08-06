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
  const next = `${location.pathname}${location.search}`;
  location.replace(`/login.html?next=${encodeURIComponent(next)}`);
  return false;
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
      reauthBtn.onclick = async (e) => {
        e.preventDefault();
        try {
          const ticket = await ensureAccessTicket();
          const base = authState.reauthUrl || "/api/v1/auth/feishu/login";
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
}

function bindAdminNav() {
  const logoutBtn = $("#logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", (e) => {
    e.preventDefault();
    logoutAdmin();
  });
}
