if (!requireAdminPage()) throw new Error("redirecting to login");

function defaultExpiresLocal() {
  const d = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function localInputToMs(value) {
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) return null;
  return ms;
}

function statusBadge(status) {
  if (status === "ACTIVE") return `<span class="badge badge-local">有效</span>`;
  if (status === "EXPIRED") return `<span class="badge badge-failed">已过期</span>`;
  return `<span class="badge badge-pending">已吊销</span>`;
}

async function loadKeys() {
  const include = $("#include-revoked").checked;
  setThinkingStatus($("#keys-status"));
  $("#keys-list").innerHTML = `<li class="meeting-item">${thinkingHtml({ block: true })}</li>`;
  const res = await apiFetch(`/access-keys?include_revoked=${include ? "true" : "false"}`);
  if (!res.ok) {
    $("#keys-status").textContent = "加载失败";
    $("#keys-list").innerHTML =
      `<li class="meeting-item"><span class="meeting-meta">加载失败</span></li>`;
    return;
  }
  const data = await res.json();
  const items = data.items || [];
  $("#keys-status").textContent = `共 ${items.length} 把密钥`;
  const list = $("#keys-list");
  if (!items.length) {
    list.innerHTML = `<li class="meeting-item"><span class="meeting-meta">暂无密钥</span></li>`;
    return;
  }
  list.innerHTML = items
    .map(
      (k) => `
      <li class="meeting-item" data-id="${k.id}">
        <div class="meeting-body">
          <div class="meeting-title-row">
            <p class="meeting-title">${escapeHtml(k.name)}${statusBadge(k.status)}</p>
            <div class="export-group">
              <button type="button" class="btn btn-sm" data-logs="${k.id}" data-name="${escapeHtml(k.name)}" title="访问日志">${btnContent("file-list-3-line", "日志")}</button>
              ${
                k.status !== "REVOKED"
                  ? `<button type="button" class="btn btn-sm" data-revoke="${k.id}" title="吊销密钥">${btnContent("forbid-2-line", "吊销")}</button>`
                  : ""
              }
            </div>
          </div>
          <p class="meeting-meta">前缀 ${escapeHtml(k.key_prefix)}… · 过期 ${formatTime(k.expires_at)} · 创建 ${formatTime(k.created_at)}</p>
        </div>
      </li>`
    )
    .join("");
}

async function createKey() {
  const name = $("#key-name").value.trim();
  const expiresAt = localInputToMs($("#key-expires").value);
  const box = $("#key-create-result");
  box.classList.add("hidden");
  if (!name || !expiresAt) {
    alert("请填写名称与过期时间");
    return;
  }
  const res = await apiFetch("/access-keys", {
    method: "POST",
    body: JSON.stringify({ name, expires_at: expiresAt }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(typeof err.detail === "string" ? err.detail : "创建失败");
    return;
  }
  const data = await res.json();
  rememberAccessKeyPlaintext(data.id, data.plaintext_key);
  box.classList.remove("hidden");
  box.innerHTML = `明文密钥仅显示一次，请立即复制保存：<code>${escapeHtml(data.plaintext_key)}</code>
    <button type="button" class="btn btn-sm" id="copy-plaintext">${btnContent("file-copy-line", "复制")}</button>`;
  $("#copy-plaintext")?.addEventListener("click", async () => {
    await copyTextToClipboard(data.plaintext_key);
  });
  $("#key-name").value = "";
  await loadKeys();
}

function closeLogsDialog() {
  $("#key-logs-dialog")?.classList.add("hidden");
}

async function loadLogs(keyId, name) {
  const dialog = $("#key-logs-dialog");
  const list = $("#logs-list");
  $("#logs-key-name").textContent = name || `#${keyId}`;
  list.innerHTML = `<li class="meeting-item">${thinkingHtml({ block: true })}</li>`;
  dialog?.classList.remove("hidden");
  const res = await apiFetch(`/access-keys/${keyId}/logs?limit=100`);
  if (!res.ok) {
    list.innerHTML = `<li class="meeting-item"><span class="meeting-meta">加载失败</span></li>`;
    return;
  }
  const data = await res.json();
  const items = data.items || [];
  if (!items.length) {
    list.innerHTML = `<li class="meeting-item"><span class="meeting-meta">暂无访问记录</span></li>`;
    return;
  }
  list.innerHTML = renderAccessLogGroups(items, { showShareId: true });
}

$("#key-expires").value = defaultExpiresLocal();
$("#create-key-btn").addEventListener("click", createKey);
$("#refresh-keys-btn").addEventListener("click", loadKeys);
$("#include-revoked").addEventListener("change", loadKeys);
$("#keys-list").addEventListener("click", async (e) => {
  const revoke = e.target.closest("[data-revoke]");
  if (revoke) {
    if (!confirm("确定吊销该密钥？绑定它的分享将无法再解锁。")) return;
    await apiFetch(`/access-keys/${revoke.dataset.revoke}`, { method: "DELETE" });
    await loadKeys();
    return;
  }
  const logs = e.target.closest("[data-logs]");
  if (logs) await loadLogs(logs.dataset.logs, logs.dataset.name);
});

$("#key-logs-dialog")?.addEventListener("click", (e) => {
  if (e.target.closest("[data-close-logs]")) closeLogsDialog();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#key-logs-dialog")?.classList.contains("hidden")) {
    closeLogsDialog();
  }
});

bindAdminNav();
checkAuth();
loadKeys();
