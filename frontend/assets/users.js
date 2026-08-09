if (!requireAdminPage()) throw new Error("redirecting to login");

if (!isSuperAdminView() || !getSuperJwt()) {
  setAdminViewMode("user");
  location.replace("/");
}

function statusBadge(status) {
  const s = String(status || "").toUpperCase();
  if (s === "ACTIVE") return `<span class="badge badge-local">ACTIVE</span>`;
  return `<span class="badge badge-failed">${escapeHtml(s || "—")}</span>`;
}

async function loadUsers() {
  const status = $("#users-status");
  const tbody = $("#users-tbody");
  const empty = $("#users-empty");
  const wrap = $("#users-table-wrap");
  setThinkingStatus(status);
  try {
    const res = await apiFetch("/admin/users");
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
    }
    const data = await res.json();
    const items = data.items || [];
    status.textContent = `共 ${data.total ?? items.length} 位用户`;
    if (!items.length) {
      tbody.innerHTML = "";
      wrap?.classList.add("hidden");
      empty?.classList.remove("hidden");
      return;
    }
    empty?.classList.add("hidden");
    wrap?.classList.remove("hidden");
    tbody.innerHTML = items
      .map(
        (u) => `<tr>
          <td>${escapeHtml(String(u.id))}</td>
          <td><strong>${escapeHtml(u.username)}</strong></td>
          <td>${escapeHtml(u.display_name || u.username || "—")}</td>
          <td>${statusBadge(u.status)}</td>
          <td>${escapeHtml(String(u.meeting_count ?? 0))}</td>
          <td title="${escapeHtml(
            u.total_duration_ms
              ? formatDuration(u.total_duration_ms)
              : "0"
          )}">${escapeHtml(String(u.total_duration_text ?? "0"))}</td>
          <td>${escapeHtml(String(u.share_count ?? 0))}</td>
          <td>${escapeHtml(String(u.access_key_count ?? 0))}</td>
          <td>${escapeHtml(String(u.invites_created ?? 0))} / ${escapeHtml(String(u.invites_redeemed ?? 0))}</td>
          <td>${
            u.feishu_bound || u.feishu_authorized
              ? '<span class="badge badge-local">已绑定</span>'
              : '<span class="badge badge-pending">未绑定</span>'
          }</td>
          <td>${escapeHtml(u.created_at_text || "—")}</td>
          <td>${escapeHtml(u.updated_at_text || "—")}</td>
        </tr>`
      )
      .join("");
  } catch (e) {
    status.textContent = `加载失败：${e.message || e}`;
    tbody.innerHTML = "";
  }
}

$("#refresh-users-btn")?.addEventListener("click", loadUsers);
bindAdminNav();
checkAuth();
loadUsers();
