const params = new URLSearchParams(location.search);
const next = params.get("next") || "/";

async function tryLogin() {
  const userInput = $("#admin-username-input");
  const passInput = $("#admin-password-input");
  const err = $("#login-error");
  err.classList.add("hidden");

  const username = (userInput?.value || "").trim();
  const password = (passInput?.value || "").trim();
  if (!username || !password) {
    err.textContent = "请输入用户名与密码";
    err.classList.remove("hidden");
    return;
  }

  const res = await fetch(`${API}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    err.textContent = typeof data.detail === "string" ? data.detail : "登录失败";
    err.classList.remove("hidden");
    return;
  }
  const data = await res.json().catch(() => ({}));
  const bearer = (data.token || "").trim();
  if (!bearer) {
    err.textContent = "登录成功但未返回会话，请检查服务端 JWT 配置";
    err.classList.remove("hidden");
    return;
  }
  setUserJwt(bearer);
  setAdminViewMode("user");
  location.replace(next.startsWith("/") ? next : "/");
}

if (getUserJwt()) {
  location.replace(next.startsWith("/") ? next : "/");
}

$("#login-btn").addEventListener("click", tryLogin);
["#admin-username-input", "#admin-password-input"].forEach((sel) => {
  const el = $(sel);
  if (!el) return;
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") tryLogin();
  });
});
$("#admin-username-input")?.focus();
