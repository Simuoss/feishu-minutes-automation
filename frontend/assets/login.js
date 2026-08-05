const params = new URLSearchParams(location.search);
const next = params.get("next") || "/";

async function tryLogin() {
  const tokenInput = $("#admin-token-input");
  const userInput = $("#admin-username-input");
  const passInput = $("#admin-password-input");
  const err = $("#login-error");
  err.classList.add("hidden");

  const token = (tokenInput?.value || "").trim();
  const username = (userInput?.value || "").trim();
  const password = (passInput?.value || "").trim();

  const body = token
    ? { token }
    : { username: username || "admin", password };

  if (!token && !password) {
    err.textContent = "请输入管理员口令，或用户名与密码";
    err.classList.remove("hidden");
    return;
  }

  const res = await fetch(`${API}/auth/admin/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    err.textContent = typeof data.detail === "string" ? data.detail : "口令错误";
    err.classList.remove("hidden");
    return;
  }
  const data = await res.json().catch(() => ({}));
  const bearer = (data.token || token || password || "").trim();
  if (!bearer) {
    err.textContent = "登录成功但未返回可用口令，请检查 ADMIN_TOKEN 配置";
    err.classList.remove("hidden");
    return;
  }
  setAdminToken(bearer);
  location.replace(next.startsWith("/") ? next : "/");
}

if (getAdminToken()) {
  location.replace(next.startsWith("/") ? next : "/");
}

$("#login-btn").addEventListener("click", tryLogin);
["#admin-token-input", "#admin-username-input", "#admin-password-input"].forEach((sel) => {
  const el = $(sel);
  if (!el) return;
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") tryLogin();
  });
});
($("#admin-token-input") || $("#admin-password-input"))?.focus();
