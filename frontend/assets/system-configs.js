if (!requireAdminPage()) throw new Error("redirecting to login");

if (!isSuperAdminView() || !getSuperJwt()) {
  setAdminViewMode("user");
  location.replace("/");
}

function rowHtml(item) {
  const key = String(item.key || "");
  return `<tr data-key="${escapeHtml(key)}">
    <td><code>${escapeHtml(key)}</code></td>
    <td><input class="config-desc-input" data-field="description" value="${escapeHtml(
      item.description || ""
    )}" /></td>
    <td><input class="config-value-input" data-field="value" value="${escapeHtml(
      item.value || ""
    )}" /></td>
    <td><input class="config-remark-input" data-field="remark" value="${escapeHtml(
      item.remark || ""
    )}" /></td>
    <td>${escapeHtml(item.updated_at_text || "—")}</td>
    <td>
      <button type="button" class="btn btn-sm" data-save-key="${escapeHtml(key)}">
        <i class="ri ri-save-line" aria-hidden="true"></i><span class="btn-label">保存</span>
      </button>
    </td>
  </tr>`;
}

async function loadConfigs() {
  const status = $("#configs-status");
  const tbody = $("#configs-tbody");
  const empty = $("#configs-empty");
  const wrap = $("#configs-table-wrap");
  setThinkingStatus(status);
  try {
    const res = await apiFetch("/admin/system-configs");
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
    }
    const data = await res.json();
    const items = data.items || [];
    status.textContent = `共 ${data.total ?? items.length} 项配置`;
    if (!items.length) {
      tbody.innerHTML = "";
      wrap?.classList.add("hidden");
      empty?.classList.remove("hidden");
      return;
    }
    empty?.classList.add("hidden");
    wrap?.classList.remove("hidden");
    tbody.innerHTML = items.map(rowHtml).join("");
  } catch (e) {
    status.textContent = `加载失败：${e.message || e}`;
    tbody.innerHTML = "";
  }
}

async function saveConfig(key, row) {
  const status = $("#configs-status");
  const description = row.querySelector('[data-field="description"]')?.value ?? "";
  const value = row.querySelector('[data-field="value"]')?.value ?? "";
  const remark = row.querySelector('[data-field="remark"]')?.value ?? "";
  setThinkingStatus(status);
  try {
    const res = await apiFetch(`/admin/system-configs/${encodeURIComponent(key)}`, {
      method: "PUT",
      body: JSON.stringify({ description, value, remark }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
    }
    const item = await res.json();
    row.outerHTML = rowHtml(item);
    status.textContent = `已保存 ${key}`;
  } catch (e) {
    status.textContent = `保存失败：${e.message || e}`;
  }
}

$("#refresh-configs-btn")?.addEventListener("click", loadConfigs);
$("#configs-tbody")?.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-save-key]");
  if (!btn) return;
  const key = btn.getAttribute("data-save-key");
  const row = btn.closest("tr");
  if (!key || !row) return;
  saveConfig(key, row);
});

bindAdminNav();
checkAuth();
loadConfigs();
