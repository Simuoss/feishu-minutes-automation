const libraryState = {
  items: [],
  keys: [],
};

function keyStatusBadge(status) {
  if (status === "VALID") return `<span class="badge badge-local">有效</span>`;
  if (status === "EXPIRED") return `<span class="badge badge-failed">已过期</span>`;
  if (status === "REVOKED") return `<span class="badge badge-pending">已吊销</span>`;
  return `<span class="badge badge-failed">无效</span>`;
}

function accessBadge(item) {
  if (item.access_mode === "PUBLIC") {
    return `<span class="badge badge-local">公开</span>`;
  }
  return `<span class="badge badge-pending">需密钥</span>`;
}

function renderKeys() {
  const box = $("#library-keys");
  const empty = $("#library-keys-empty");
  const localKeys = loadAccessKeyList();
  if (!localKeys.length) {
    box.innerHTML = "";
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  const statusByPrefix = new Map(
    (libraryState.keys || []).map((k) => [k.key_prefix, k])
  );
  box.innerHTML = localKeys
    .map((raw) => {
      const prefix = raw.slice(0, 10);
      const st = statusByPrefix.get(prefix);
      const status = st?.status || "UNKNOWN";
      const count = st?.share_count ?? "—";
      return `
        <div class="meeting-item">
          <div class="meeting-body">
            <div class="meeting-title-row">
              <p class="meeting-title"><code>${escapeHtml(prefix)}…</code>${keyStatusBadge(status)}</p>
              <div class="export-group">
                <button type="button" class="btn btn-sm" data-forget-key="${escapeHtml(raw)}" title="从本机移除">${btnContent("delete-bin-line", "移除")}</button>
              </div>
            </div>
            <p class="meeting-meta">可访问分享约 ${count} 条（以服务端校验为准）</p>
          </div>
        </div>`;
    })
    .join("");
}

function filteredItems() {
  const q = ($("#library-filter").value || "").trim().toLowerCase();
  if (!q) return libraryState.items;
  return libraryState.items.filter((item) => {
    const hay = `${item.title || ""} ${item.minute_token || ""} ${item.share_token || ""}`.toLowerCase();
    return hay.includes(q);
  });
}

function renderList() {
  const items = filteredItems();
  const list = $("#library-list");
  $("#library-status").textContent = `共 ${libraryState.items.length} 条可访问分享${
    items.length !== libraryState.items.length ? `，筛选后 ${items.length} 条` : ""
  }`;
  if (!items.length) {
    list.innerHTML = `<li class="meeting-item"><span class="meeting-meta">暂无可访问分享</span></li>`;
    return;
  }
  list.innerHTML = items
    .map((item) => {
      const keyHint = item.matched_key_prefix
        ? `密钥 ${escapeHtml(item.matched_key_prefix)}…`
        : item.source === "KNOWN_TOKEN"
          ? "本机曾打开的公开分享"
          : "—";
      return `
      <li class="meeting-item">
        <div class="meeting-body">
          <div class="meeting-title-row">
            <p class="meeting-title">${escapeHtml(item.title)}${accessBadge(item)}</p>
            <div class="export-group">
              <a class="btn btn-sm btn-primary" href="${escapeHtml(item.url)}">${btnContent("external-link-line", "打开")}</a>
              <button type="button" class="btn btn-sm" data-copy-url="${escapeHtml(item.url)}" title="复制链接">${btnContent("file-copy-line", "复制")}</button>
            </div>
          </div>
          <p class="meeting-meta">
            ${item.allow_export ? "可导出" : "不可导出"} · ${keyHint}
          </p>
          <p class="meeting-meta"><code>${escapeHtml(item.url)}</code></p>
        </div>
      </li>`;
    })
    .join("");
}

async function loadLibrary() {
  setThinkingStatus($("#library-status"));
  $("#library-list").innerHTML = `<li class="meeting-item">${thinkingHtml({ block: true })}</li>`;
  const keys = loadAccessKeyList();
  const share_tokens = loadKnownShareTokens();
  if (!keys.length && !share_tokens.length) {
    libraryState.items = [];
    libraryState.keys = [];
    renderKeys();
    renderList();
    $("#library-status").textContent = "本机没有已保存的密钥或访问记录";
    return;
  }

  const res = await fetch(`${API}/share/library`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys, share_tokens }),
  });
  if (!res.ok) {
    $("#library-status").textContent = "加载失败，请稍后重试";
    $("#library-list").innerHTML =
      `<li class="meeting-item"><span class="meeting-meta">加载失败</span></li>`;
    return;
  }
  const data = await res.json();
  libraryState.items = data.items || [];
  libraryState.keys = data.keys || [];
  renderKeys();
  renderList();
}

$("#refresh-library-btn").addEventListener("click", loadLibrary);
$("#library-filter").addEventListener("input", renderList);
$("#library-keys").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-forget-key]");
  if (!btn) return;
  if (!confirm("从本机移除这把密钥？不会影响服务端密钥本身。")) return;
  forgetAccessKey(btn.dataset.forgetKey);
  loadLibrary();
});
$("#library-list").addEventListener("click", async (e) => {
  const copy = e.target.closest("[data-copy-url]");
  if (!copy) return;
  try {
    await navigator.clipboard.writeText(copy.dataset.copyUrl);
    $("#library-status").textContent = "链接已复制";
  } catch {
    window.prompt("请手动复制：", copy.dataset.copyUrl);
  }
});

loadLibrary();
