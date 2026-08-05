if (!requireAdminPage()) throw new Error("redirecting to login");

const editState = {
  shareId: null,
  batchIds: null,
  keys: [],
  items: [],
  selected: new Set(),
};

function statusBadge(share) {
  if (share.revoked_at) return `<span class="badge badge-failed">已吊销</span>`;
  if (share.access_mode === "KEY_REQUIRED") {
    return `<span class="badge badge-pending">需密钥</span>`;
  }
  return `<span class="badge badge-local">公开</span>`;
}

function keyLabel(keyId) {
  if (!keyId) return "—";
  const key = editState.keys.find((k) => k.id === keyId);
  if (!key) return `#${keyId}`;
  return `${key.name}（${key.key_prefix}…）`;
}

function fillKeySelects() {
  const active = editState.keys.filter((k) => k.status === "ACTIVE");
  const filterKey = $("#filter-key");
  const prev = filterKey.value;
  const keyOptions = active
    .map(
      (k) =>
        `<option value="${k.id}">${escapeHtml(k.name)}（${escapeHtml(k.key_prefix)}…）</option>`
    )
    .join("");
  filterKey.innerHTML = `
    <option value="">全部</option>
    <option value="none">无密钥（公开）</option>
    ${keyOptions}`;
  if ([...filterKey.options].some((o) => o.value === prev)) filterKey.value = prev;

  const editSelect = $("#edit-key-select");
  editSelect.innerHTML = active.length
    ? active
        .map(
          (k) =>
            `<option value="${k.id}">${escapeHtml(k.name)}（${escapeHtml(k.key_prefix)}…）</option>`
        )
        .join("")
    : `<option value="">暂无可用密钥</option>`;
}

async function loadKeysCache() {
  const res = await apiFetch("/access-keys?include_revoked=true");
  if (!res.ok) {
    editState.keys = [];
    fillKeySelects();
    return;
  }
  const data = await res.json();
  editState.keys = data.items || [];
  fillKeySelects();
}

function syncEditModeUi() {
  const mode = $("#edit-access-mode")?.value;
  $("#edit-key-field")?.classList.toggle("hidden", mode !== "KEY_REQUIRED");
}

function closeEditDialog() {
  $("#share-edit-dialog").classList.add("hidden");
  editState.shareId = null;
  editState.batchIds = null;
}

function openEditDialog(share) {
  editState.shareId = share.id;
  editState.batchIds = null;
  $("#share-edit-heading").textContent = "编辑分享";
  $("#share-edit-title").textContent =
    `${share.title || share.minute_token} · #${share.id}`;
  $("#edit-access-mode").value = share.access_mode || "PUBLIC";
  $("#edit-allow-export").checked = Boolean(share.allow_export);
  fillKeySelects();
  if (share.access_key_id) $("#edit-key-select").value = String(share.access_key_id);
  syncEditModeUi();
  $("#share-edit-dialog").classList.remove("hidden");
}

function openBatchEditDialog(ids) {
  editState.shareId = null;
  editState.batchIds = ids;
  $("#share-edit-heading").textContent = "批量改权限";
  $("#share-edit-title").textContent = `将对选中的 ${ids.length} 条有效分享统一应用以下设置`;
  $("#edit-access-mode").value = "PUBLIC";
  $("#edit-allow-export").checked = false;
  fillKeySelects();
  syncEditModeUi();
  $("#share-edit-dialog").classList.remove("hidden");
}

function readEditPayload() {
  const access_mode = $("#edit-access-mode").value;
  const allow_export = $("#edit-allow-export").checked;
  const access_key_id =
    access_mode === "KEY_REQUIRED"
      ? Number($("#edit-key-select").value) || null
      : null;
  if (access_mode === "KEY_REQUIRED" && !access_key_id) {
    alert("请选择一把有效密钥");
    return null;
  }
  return { access_mode, allow_export, access_key_id };
}

async function saveEdit() {
  const payload = readEditPayload();
  if (!payload) return;

  if (editState.batchIds?.length) {
    const res = await apiFetch("/shares/batch-update", {
      method: "POST",
      body: JSON.stringify({ share_ids: editState.batchIds, ...payload }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(typeof err.detail === "string" ? err.detail : "批量更新失败");
      return;
    }
    const data = await res.json();
    closeEditDialog();
    editState.selected.clear();
    await loadShares();
    $("#shares-status").textContent =
      `批量更新完成：成功 ${data.success_count}，失败 ${data.failed_count}`;
    return;
  }

  if (!editState.shareId) return;
  const res = await apiFetch(`/shares/${editState.shareId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(typeof err.detail === "string" ? err.detail : "保存失败");
    return;
  }
  closeEditDialog();
  await loadShares();
}

function buildFilterQuery() {
  const qs = new URLSearchParams({
    status: $("#filter-status").value || "ACTIVE",
    sort_by: $("#filter-sort-by").value || "CREATED_AT",
    sort_order: $("#filter-sort-order").value || "DESC",
    limit: "300",
    offset: "0",
  });
  const q = ($("#filter-q").value || "").trim();
  if (q) qs.set("q", q);
  const from = localInputToMs($("#filter-created-from").value);
  const to = localInputToMs($("#filter-created-to").value);
  if (from != null) qs.set("created_from", String(from));
  if (to != null) qs.set("created_to", String(to));
  const allow = $("#filter-allow-export").value;
  if (allow) qs.set("allow_export", allow);
  const mode = $("#filter-access-mode").value;
  if (mode) qs.set("access_mode", mode);
  const key = $("#filter-key").value;
  if (key === "none") qs.set("no_access_key", "true");
  else if (key) qs.set("access_key_id", key);
  return qs;
}

function resetFilters() {
  $("#filter-q").value = "";
  $("#filter-created-from").value = "";
  $("#filter-created-to").value = "";
  $("#filter-key").value = "";
  $("#filter-allow-export").value = "";
  $("#filter-access-mode").value = "";
  $("#filter-status").value = "ACTIVE";
  $("#filter-sort-by").value = "CREATED_AT";
  $("#filter-sort-order").value = "DESC";
}

function updateSelectionUi() {
  const count = editState.selected.size;
  $("#selection-status").textContent = count ? `已选 ${count} 条` : "未选中";
  $("#batch-copy-btn").disabled = count === 0;
  const activeSelected = [...editState.selected].filter((id) => {
    const item = editState.items.find((s) => s.id === id);
    return item && !item.revoked_at;
  });
  $("#batch-edit-btn").disabled = activeSelected.length === 0;
  $("#batch-revoke-btn").disabled = activeSelected.length === 0;

  const selectable = editState.items.map((s) => s.id);
  const allSelected =
    selectable.length > 0 && selectable.every((id) => editState.selected.has(id));
  setBtnContent(
    $("#select-all-shares-btn"),
    allSelected ? "close-circle-line" : "checkbox-multiple-line",
    allSelected ? "取消全选" : "全选本页"
  );
}

async function loadShares() {
  setThinkingStatus($("#shares-status"));
  $("#shares-list").innerHTML = `<li class="meeting-item">${thinkingHtml({ block: true })}</li>`;
  const qs = buildFilterQuery();
  const res = await apiFetch(`/shares?${qs}`);
  if (!res.ok) {
    $("#shares-status").textContent = "加载失败";
    $("#shares-list").innerHTML =
      `<li class="meeting-item"><span class="meeting-meta">加载失败</span></li>`;
    return;
  }
  const data = await res.json();
  const items = data.items || [];
  editState.items = items;
  const visible = new Set(items.map((s) => s.id));
  editState.selected = new Set([...editState.selected].filter((id) => visible.has(id)));

  const total = data.total ?? items.length;
  $("#shares-status").textContent =
    total === items.length ? `共 ${total} 条分享` : `显示 ${items.length} / 共 ${total} 条`;

  const list = $("#shares-list");
  if (!items.length) {
    list.innerHTML = `<li class="meeting-item"><span class="meeting-meta">暂无分享</span></li>`;
    updateSelectionUi();
    return;
  }

  list.innerHTML = items
    .map((s) => {
      const title = s.title || s.minute_token;
      const revoked = Boolean(s.revoked_at);
      const checked = editState.selected.has(s.id) ? "checked" : "";
      return `
      <li class="meeting-item" data-id="${s.id}">
        <input type="checkbox" data-select="${s.id}" ${checked} />
        <div class="meeting-body">
          <div class="meeting-title-row">
            <p class="meeting-title">${escapeHtml(title)}${statusBadge(s)}</p>
            <div class="export-group">
              <button type="button" class="btn btn-sm" data-copy="${escapeHtml(s.url)}" title="复制链接">${btnContent("file-copy-line", "复制")}</button>
              ${
                revoked
                  ? ""
                  : `<button type="button" class="btn btn-sm" data-edit="${s.id}" title="编辑分享">${btnContent("edit-line", "编辑")}</button>
                     <button type="button" class="btn btn-sm" data-revoke="${s.id}" title="取消分享">${btnContent("link-unlink", "取消")}</button>`
              }
            </div>
          </div>
          <p class="meeting-meta">
            ${s.access_mode === "PUBLIC" ? "公开" : "需密钥"} ·
            ${s.allow_export ? "可导出" : "不可导出"} ·
            密钥 ${escapeHtml(keyLabel(s.access_key_id))} ·
            创建 ${formatTime(s.created_at)}
            ${revoked ? ` · 吊销 ${formatTime(s.revoked_at)}` : ""}
          </p>
          <p class="meeting-meta"><code>${escapeHtml(s.url)}</code></p>
          <p class="meeting-meta">会议 ${escapeHtml(s.minute_token)} · share #${s.id}</p>
        </div>
      </li>`;
    })
    .join("");
  updateSelectionUi();
}

function selectedActiveIds() {
  return [...editState.selected].filter((id) => {
    const item = editState.items.find((s) => s.id === id);
    return item && !item.revoked_at;
  });
}

$("#apply-filter-btn").addEventListener("click", loadShares);
$("#reset-filter-btn").addEventListener("click", async () => {
  resetFilters();
  await loadShares();
});
$("#refresh-shares-btn").addEventListener("click", loadShares);
$("#filter-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadShares();
});
$("#edit-access-mode").addEventListener("change", syncEditModeUi);
$("#share-edit-save").addEventListener("click", saveEdit);
$("#share-edit-dialog").addEventListener("click", (e) => {
  if (e.target.closest("[data-close-dialog]")) closeEditDialog();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#share-edit-dialog").classList.contains("hidden")) {
    closeEditDialog();
  }
});

$("#select-all-shares-btn").addEventListener("click", () => {
  const ids = editState.items.map((s) => s.id);
  const allSelected = ids.length > 0 && ids.every((id) => editState.selected.has(id));
  if (allSelected) editState.selected.clear();
  else ids.forEach((id) => editState.selected.add(id));
  $("#shares-list")
    .querySelectorAll("[data-select]")
    .forEach((cb) => {
      cb.checked = editState.selected.has(Number(cb.dataset.select));
    });
  updateSelectionUi();
});

$("#batch-copy-btn").addEventListener("click", async () => {
  const lines = editState.items
    .filter((s) => editState.selected.has(s.id))
    .map((s) => `${s.title || s.minute_token}\n${s.url}`);
  if (!lines.length) return;
  const text = lines.join("\n\n");
  try {
    await navigator.clipboard.writeText(text);
    $("#shares-status").textContent = `已复制 ${lines.length} 条链接`;
  } catch {
    window.prompt("请手动复制：", text);
  }
});

$("#batch-edit-btn").addEventListener("click", () => {
  const ids = selectedActiveIds();
  if (!ids.length) {
    alert("请先选中有效分享");
    return;
  }
  openBatchEditDialog(ids);
});

$("#batch-revoke-btn").addEventListener("click", async () => {
  const ids = selectedActiveIds();
  if (!ids.length) {
    alert("请先选中有效分享");
    return;
  }
  if (!confirm(`确定取消选中的 ${ids.length} 条分享？链接将立即失效。`)) return;
  const res = await apiFetch("/shares/batch-revoke", {
    method: "POST",
    body: JSON.stringify({ share_ids: ids }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(typeof err.detail === "string" ? err.detail : "批量取消失败");
    return;
  }
  const data = await res.json();
  editState.selected.clear();
  await loadShares();
  $("#shares-status").textContent =
    `批量取消完成：成功 ${data.success_count}，失败 ${data.failed_count}`;
});

$("#shares-list").addEventListener("change", (e) => {
  const cb = e.target.closest("[data-select]");
  if (!cb) return;
  const id = Number(cb.dataset.select);
  if (cb.checked) editState.selected.add(id);
  else editState.selected.delete(id);
  updateSelectionUi();
});

$("#shares-list").addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-copy]");
  if (copy) {
    try {
      await navigator.clipboard.writeText(copy.dataset.copy);
      $("#shares-status").textContent = "链接已复制";
    } catch {
      window.prompt("请手动复制：", copy.dataset.copy);
    }
    return;
  }
  const edit = e.target.closest("[data-edit]");
  if (edit) {
    const share = editState.items.find((s) => String(s.id) === String(edit.dataset.edit));
    if (!share) {
      alert("找不到该分享，请刷新后重试");
      return;
    }
    openEditDialog(share);
    return;
  }
  const revoke = e.target.closest("[data-revoke]");
  if (revoke) {
    if (!confirm("确定取消该分享？链接将立即失效。")) return;
    const res = await apiFetch(`/shares/${revoke.dataset.revoke}`, { method: "DELETE" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert(typeof err.detail === "string" ? err.detail : "取消失败");
      return;
    }
    await loadShares();
  }
});

bindAdminNav();
checkAuth();
(async () => {
  await loadKeysCache();
  await loadShares();
})();
