const params = new URLSearchParams(location.search);
const next = params.get("next") || "/";

async function tryLogin() {
  const input = $("#admin-token-input");
  const err = $("#login-error");
  const token = (input.value || "").trim();
  err.classList.add("hidden");
  if (!token) {
    err.textContent = "请输入管理员口令";
    err.classList.remove("hidden");
    return;
  }
  const res = await fetch(`${API}/auth/admin/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    err.textContent = typeof data.detail === "string" ? data.detail : "口令错误";
    err.classList.remove("hidden");
    return;
  }
  setAdminToken(token);
  location.replace(next.startsWith("/") ? next : "/");
}

if (getAdminToken()) {
  location.replace(next.startsWith("/") ? next : "/");
}

$("#login-btn").addEventListener("click", tryLogin);
$("#admin-token-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") tryLogin();
});
$("#admin-token-input").focus();
