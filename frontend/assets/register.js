const params = new URLSearchParams(location.search);
const next = params.get("next") || "/";
const prefill = params.get("invite") || params.get("code") || "";

if (prefill) {
  const inviteInput = $("#invite-code-input");
  if (inviteInput) inviteInput.value = prefill;
}

async function tryRegister() {
  const err = $("#register-error");
  err.classList.add("hidden");
  const invite_code = ($("#invite-code-input")?.value || "").trim();
  const username = ($("#register-username-input")?.value || "").trim();
  const password = ($("#register-password-input")?.value || "").trim();
  if (!invite_code) {
    err.textContent = "请填写邀请码";
    err.classList.remove("hidden");
    return;
  }
  if (!username || !password) {
    err.textContent = "请填写用户名与密码";
    err.classList.remove("hidden");
    return;
  }

  const res = await fetch(`${API}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, invite_code }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    err.textContent = typeof data.detail === "string" ? data.detail : "注册失败";
    err.classList.remove("hidden");
    return;
  }
  const data = await res.json().catch(() => ({}));
  const bearer = (data.token || "").trim();
  if (!bearer) {
    err.textContent = "注册成功但未返回会话";
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

$("#register-btn").addEventListener("click", tryRegister);
["#invite-code-input", "#register-username-input", "#register-password-input"].forEach(
  (sel) => {
    const el = $(sel);
    if (!el) return;
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") tryRegister();
    });
  }
);
($("#invite-code-input")?.value ? $("#register-username-input") : $("#invite-code-input"))?.focus();
