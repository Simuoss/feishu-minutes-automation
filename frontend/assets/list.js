if (!requireAdminPage()) throw new Error("redirecting to login");

const LIST_CACHE_KEY = "minutes_list_cache_v1";
const PAGE_SIZE_KEY = "minutes_page_size";
const PAGE_SIZE_OPTIONS = [10, 20, 50, 100];
// 飞书 search 单次最多 30；进页/刷新时按这个粒度拉完整个时间窗
const CLOUD_PAGE_SIZE = 30;
const DOWNLOAD_CONCURRENCY = 5;

function loadPageSize() {
  const raw = Number(localStorage.getItem(PAGE_SIZE_KEY));
  return PAGE_SIZE_OPTIONS.includes(raw) ? raw : 20;
}

const state = {
  allItems: [],
  items: [],
  selected: new Set(),
  pageIndex: 0,
  pageSize: loadPageSize(),
  filteredTotal: 0,
  downloadProgress: {},
  progressTokens: new Set(),
  progressBatchTimer: null,
  sortOrder: "desc",
  syncing: false,
  lastSyncedAt: null,
  pendingBatchSummary: null,
  pendingBatchDownload: null,
};

function meetingTitle(item) {
  return item.title || item.display_info || item.description || item.minute_token || "未命名会议";
}

function meetingMetaParts(item) {
  const parts = [];
  if (item.create_time) parts.push(formatTime(item.create_time));
  const duration = item.duration_ms ? formatDuration(item.duration_ms) : item.duration_text;
  if (duration) parts.push(duration);
  if (item.owner_name) parts.push(`所有者 ${item.owner_name}`);
  else if (item.owner_id) parts.push(`所有者 ${item.owner_id}`);
  if (item.note_id != null) parts.push(`note ${item.note_id}`);
  parts.push(item.minute_token);
  return parts.filter(Boolean);
}

function meetingExtraLine(item) {
  if (item.keywords) return `关键词：${item.keywords}`;
  return "";
}

function meetingLink(item) {
  return item.url || item.app_link || "";
}

function openMeeting(token) {
  location.href = `/meeting.html?token=${encodeURIComponent(token)}`;
}

function itemProgressBlock(token) {
  const p = state.downloadProgress[token];
  if (!p) return "";
  const statusClass =
    p.status === "failed" ? "is-failed" : p.status === "completed" ? "is-done" : "is-active";
  return `
    <div class="item-progress ${statusClass}" data-progress="${token}">
      <div class="item-progress-bar"><div class="item-progress-fill" style="width:${p.percent}%"></div></div>
      <p class="item-progress-label">${escapeHtml(p.stage || "")}${p.percent != null ? ` · ${p.percent}%` : ""}</p>
    </div>
  `;
}

function setItemProgress(token, progress) {
  state.downloadProgress[token] = progress;
  const row = document.querySelector(`.meeting-item[data-token="${token}"]`);
  if (!row) return;
  let block = row.querySelector(`[data-progress="${token}"]`);
  if (!block) {
    const body = row.querySelector(".meeting-body");
    if (body) body.insertAdjacentHTML("beforeend", itemProgressBlock(token));
    block = row.querySelector(`[data-progress="${token}"]`);
  }
  if (!block) return;
  const statusClass =
    progress.status === "failed" ? "is-failed" : progress.status === "completed" ? "is-done" : "is-active";
  block.className = `item-progress ${statusClass}`;
  const fill = block.querySelector(".item-progress-fill");
  const label = block.querySelector(".item-progress-label");
  if (fill) fill.style.width = `${progress.percent ?? 0}%`;
  if (label) {
    label.textContent = `${progress.stage || ""}${progress.percent != null ? ` · ${progress.percent}%` : ""}`;
  }
}

async function pollDownloadProgressBatch() {
  const tokens = [...state.progressTokens];
  if (!tokens.length) {
    if (state.progressBatchTimer) {
      clearInterval(state.progressBatchTimer);
      state.progressBatchTimer = null;
    }
    return;
  }
  try {
    const res = await apiFetch("/meetings/download/progress/batch", {
      method: "POST",
      body: JSON.stringify({ minute_tokens: tokens }),
    });
    if (!res.ok) return;
    const data = await res.json();
    for (const item of data.items || []) {
      setItemProgress(item.minute_token, item);
    }
  } catch {
    /* ignore */
  }
}

function startProgressPolling(token) {
  state.progressTokens.add(token);
  if (state.progressBatchTimer) return;
  state.progressBatchTimer = setInterval(pollDownloadProgressBatch, 400);
  pollDownloadProgressBatch();
}

function stopProgressPolling(token) {
  state.progressTokens.delete(token);
  if (!state.progressTokens.size && state.progressBatchTimer) {
    clearInterval(state.progressBatchTimer);
    state.progressBatchTimer = null;
  }
}

function canViewLocal(item) {
  return Boolean(item.is_local || item.local_status === "COMPLETED");
}

function localBadge(item) {
  if (item.is_local) return `<span class="badge badge-local">已本地</span>`;
  if (item.local_status === "DOWNLOADING") {
    return `<span class="badge badge-pending">下载中</span>`;
  }
  if (item.local_status === "FAILED") {
    return `<span class="badge badge-failed">失败</span>`;
  }
  return "";
}

function normalizeSummaryStatus(status) {
  // 后端 broker 用 COMPLETED，列表落盘状态用 READY；统一成前端识别的值
  if (status === "COMPLETED") return "READY";
  return status || "NONE";
}

function summaryBadge(item) {
  switch (normalizeSummaryStatus(item.summary_status)) {
    case "READY":
      return `<span class="badge badge-summary">有纪要</span>`;
    case "GENERATING":
      return `<span class="badge badge-pending">纪要生成中</span>`;
    case "QUEUED":
      return `<span class="badge badge-pending">纪要排队中</span>`;
    case "FAILED":
      return `<span class="badge badge-failed">纪要失败</span>`;
    default:
      return "";
  }
}

async function refreshSummaryBadges() {
  // 只问进行中的纪要进度，绝不再打飞书 list
  const inflight = state.allItems.filter((item) => {
    const status = normalizeSummaryStatus(item.summary_status);
    return status === "GENERATING" || status === "QUEUED";
  });
  if (!inflight.length) return;

  try {
    const res = await apiFetch("/meetings/summary/progress/batch", {
      method: "POST",
      body: JSON.stringify({
        minute_tokens: inflight.map((item) => item.minute_token),
      }),
    });
    if (!res.ok) return;
    const data = await res.json();
    const byToken = new Map((data.items || []).map((row) => [row.minute_token, row]));
    let changed = false;
    for (const item of inflight) {
      const row = byToken.get(item.minute_token);
      if (!row) continue;
      const next = normalizeSummaryStatus(row.status || item.summary_status);
      if (next !== normalizeSummaryStatus(item.summary_status)) {
        item.summary_status = next;
        changed = true;
      }
    }
    if (changed) {
      saveListCache(state.allItems);
      applyLocalView();
    }
  } catch {
    /* ignore */
  }
}

function defaultTimeRange() {
  const end = new Date();
  const start = new Date(end.getTime() - 30 * 24 * 60 * 60 * 1000);
  const toLocal = (d) => {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  $("#start-time").value = toLocal(start);
  $("#end-time").value = toLocal(end);
}

function buildCloudSyncParams(pageToken) {
  // 进页/刷新只带时间窗拉全量；关键词、时长、排序、翻页都在本地做
  const params = new URLSearchParams({
    page_size: String(CLOUD_PAGE_SIZE),
    sort_order: "desc",
  });
  const start = $("#start-time").value;
  const end = $("#end-time").value;
  if (start) params.set("start_time", start);
  if (end) params.set("end_time", end);
  if (pageToken) params.set("page_token", pageToken);
  return params;
}

function parseMeetingTime(value) {
  if (!value) return null;
  const normalized = String(value).trim().replace(/\./g, "-").replace(" ", "T");
  const ms = Date.parse(normalized);
  return Number.isNaN(ms) ? null : ms;
}

function filterAndSortAllItems() {
  const keyword = $("#search-input").value.trim().toLowerCase();
  const startRaw = $("#start-time").value;
  const endRaw = $("#end-time").value;
  const start = startRaw ? Date.parse(startRaw) : null;
  const end = endRaw ? Date.parse(endRaw) : null;
  const minDur = $("#min-duration").value.trim();
  const maxDur = $("#max-duration").value.trim();
  const minMs = minDur === "" ? null : Number(minDur) * 60_000;
  const maxMs = maxDur === "" ? null : Number(maxDur) * 60_000;

  let rows = state.allItems.filter((item) => {
    if (keyword) {
      const hay = [
        item.title,
        item.display_info,
        item.keywords,
        item.owner_name,
        item.owner_id,
        item.minute_token,
        item.description,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      if (!hay.includes(keyword)) return false;
    }
    const created = parseMeetingTime(item.create_time);
    if (start != null && !Number.isNaN(start) && created != null && created < start) return false;
    if (end != null && !Number.isNaN(end) && created != null && created > end) return false;
    if (minMs != null && !Number.isNaN(minMs) && (item.duration_ms == null || item.duration_ms < minMs)) {
      return false;
    }
    if (maxMs != null && !Number.isNaN(maxMs) && (item.duration_ms == null || item.duration_ms > maxMs)) {
      return false;
    }
    return true;
  });

  rows.sort((a, b) => {
    const ka = parseMeetingTime(a.create_time) || 0;
    const kb = parseMeetingTime(b.create_time) || 0;
    return state.sortOrder === "asc" ? ka - kb : kb - ka;
  });
  return rows;
}

function applyLocalView({ resetPage = false } = {}) {
  if (resetPage) state.pageIndex = 0;
  const filtered = filterAndSortAllItems();
  state.filteredTotal = filtered.length;
  const pageCount = Math.max(1, Math.ceil(filtered.length / state.pageSize) || 1);
  if (state.pageIndex >= pageCount) state.pageIndex = pageCount - 1;
  if (state.pageIndex < 0) state.pageIndex = 0;
  const start = state.pageIndex * state.pageSize;
  state.items = filtered.slice(start, start + state.pageSize);
  renderList();
  updatePager(pageCount);
  const sortLabel = state.sortOrder === "asc" ? "正序（旧→新）" : "倒序（新→旧）";
  const syncHint = state.syncing
    ? " · 正在同步云端…"
    : state.lastSyncedAt
      ? ` · 云端 ${describeCacheAge(state.lastSyncedAt)}`
      : "";
  setStatus(
    `共 ${filtered.length} 条 · 第 ${filtered.length ? state.pageIndex + 1 : 0}/${filtered.length ? pageCount : 0} 页 · ${sortLabel}${syncHint}`
  );
}

function updatePager(pageCount) {
  $("#prev-page").disabled = state.pageIndex <= 0;
  $("#next-page").disabled = state.pageIndex + 1 >= pageCount || state.filteredTotal === 0;
  const indicator = $("#page-indicator");
  if (indicator) {
    indicator.textContent =
      state.filteredTotal === 0
        ? "0 / 0"
        : `${state.pageIndex + 1} / ${pageCount}`;
  }
}

function renderList() {
  const list = $("#meeting-list");
  list.innerHTML = "";

  if (!state.items.length) {
    list.innerHTML = `<li class="meeting-item"><span class="meeting-meta">暂无数据</span></li>`;
    return;
  }

  state.items.forEach((item) => {
    const li = document.createElement("li");
    li.className = "meeting-item";
    li.dataset.token = item.minute_token;

    const canOpen = canViewLocal(item);
    const titleClass = canOpen ? "meeting-title clickable" : "meeting-title";
    const title = meetingTitle(item);
    const extra = meetingExtraLine(item);
    const link = meetingLink(item);
    const meta = meetingMetaParts(item).join(" · ");
    const viewBtn = canOpen
      ? `<button type="button" class="btn btn-sm btn-view" data-view="${item.minute_token}">${btnContent("eye-line", "本地查看")}</button>`
      : "";

    li.innerHTML = `
      <input type="checkbox" data-token="${item.minute_token}" ${state.selected.has(item.minute_token) ? "checked" : ""} />
      <div class="meeting-body">
        <div class="meeting-title-row">
          <p class="${titleClass}" data-token="${item.minute_token}" data-open="${canOpen}">${escapeHtml(title)}${localBadge(item)}${summaryBadge(item)}</p>
          ${viewBtn}
        </div>
        <p class="meeting-meta">${escapeHtml(meta)}</p>
        ${extra ? `<p class="meeting-desc">${escapeHtml(extra)}</p>` : ""}
        ${link ? `<p class="meeting-link"><a href="${escapeHtml(link)}" target="_blank" rel="noopener">在飞书中打开</a></p>` : ""}
        ${itemProgressBlock(item.minute_token)}
      </div>
    `;
    list.appendChild(li);
  });

  list.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.addEventListener("change", () => {
      const token = cb.dataset.token;
      if (cb.checked) state.selected.add(token);
      else state.selected.delete(token);
      updateDownloadBtn();
    });
  });

  list.querySelectorAll(".meeting-title.clickable").forEach((el) => {
    el.addEventListener("click", () => openMeeting(el.dataset.token));
  });

  list.querySelectorAll(".btn-view").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openMeeting(btn.dataset.view);
    });
  });
}

function selectedLocalItems() {
  return state.allItems.filter(
    (item) => state.selected.has(item.minute_token) && canViewLocal(item)
  );
}

function updateSelectAllBtnLabel() {
  const btn = $("#select-all-btn");
  if (!btn) return;
  const tokens = state.items.map((item) => item.minute_token).filter(Boolean);
  const allSelected = tokens.length > 0 && tokens.every((token) => state.selected.has(token));
  if (allSelected) setBtnContent(btn, "close-circle-line", "取消全选");
  else setBtnContent(btn, "checkbox-multiple-line", "全选本页");
}

function updateDownloadBtn() {
  $("#download-btn").disabled = state.selected.size === 0;
  const locals = selectedLocalItems();
  // 纪要只能基于本地转写生成，没下载的选中项不算数
  $("#batch-summary-btn").disabled = locals.length === 0;
  const shareBtn = $("#share-selected-btn");
  if (shareBtn) shareBtn.disabled = locals.length === 0;
  updateSelectAllBtnLabel();
}

function setStatus(msg, { thinking = false } = {}) {
  const el = $("#list-status");
  if (!el) return;
  if (thinking) {
    setThinkingStatus(el);
    return;
  }
  el.textContent = msg;
}

function showListThinking() {
  const list = $("#meeting-list");
  if (!list) return;
  list.innerHTML = `<li class="meeting-item">${thinkingHtml({ block: true })}</li>`;
}

function hideScopeBanner() {
  $("#scope-banner").classList.add("hidden");
}

function showScopeError(result) {
  const banner = $("#scope-banner");
  const link = $("#scope-auth-link");
  const title = $("#scope-banner-title");
  const msg = $("#scope-banner-msg");

  if (result.needs_app_scope && result.scope_auth_url) {
    banner.classList.remove("hidden");
    link.href = result.scope_auth_url;
    setBtnContent(link, "external-link-line", "去飞书开通权限");
    link.removeAttribute("target");
    title.textContent = "下载失败：应用权限不足";
    msg.textContent = result.error_message || "请在飞书开发者后台为应用开通妙记相关权限并发布应用。";
    return true;
  }

  if (result.needs_user_login) {
    banner.classList.remove("hidden");
    link.href = authState.reauthUrl || `${API}/auth/feishu/login?reauth=1`;
    setBtnContent(link, "shield-keyhole-line", "重新授权");
    link.removeAttribute("target");
    title.textContent = "下载失败：用户授权不足";
    msg.textContent =
      result.error_message ||
      "当前登录未包含下载所需权限，请点击重新授权并在飞书授权页勾选全部妙记权限。";
    return true;
  }

  return false;
}

function showMissingScopeWarning(missingScopes) {
  if (!missingScopes?.length) {
    return;
  }
  const banner = $("#scope-banner");
  const link = $("#scope-auth-link");
  const title = $("#scope-banner-title");
  const msg = $("#scope-banner-msg");
  banner.classList.remove("hidden");
  link.href = authState.reauthUrl || `${API}/auth/feishu/login?reauth=1`;
  setBtnContent(link, "shield-keyhole-line", "重新授权");
  link.removeAttribute("target");
  title.textContent = "授权不完整，无法下载";
  msg.textContent = `缺少权限：${missingScopes.join("、")}。请重新授权；若仍失败，请在飞书开发者后台开通对应用户权限并发布应用。`;
}

function saveListCache(items) {
  try {
    localStorage.setItem(
      LIST_CACHE_KEY,
      JSON.stringify({ saved_at: Date.now(), items })
    );
  } catch {
    /* 无痕模式或配额已满：缓存只是加速手段，写不进去不影响正常加载 */
  }
}

function loadListCache() {
  let cached;
  try {
    cached = JSON.parse(localStorage.getItem(LIST_CACHE_KEY) || "null");
  } catch {
    localStorage.removeItem(LIST_CACHE_KEY);
    return null;
  }
  if (!cached?.items?.length) return null;
  return cached;
}

function describeCacheAge(savedAt) {
  const minutes = Math.round((Date.now() - savedAt) / 60000);
  if (minutes < 1) return "刚刚同步";
  if (minutes < 60) return `${minutes} 分钟前同步`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} 小时前同步` : `${Math.round(hours / 24)} 天前同步`;
}

function mergeByMinuteToken(freshItems, previousItems) {
  // 只按会议 id 对齐：云端条目为准，本地进行中的纪要状态先保留，避免同步瞬间徽章闪回
  const prev = new Map(previousItems.map((item) => [item.minute_token, item]));
  return freshItems.map((item) => {
    const old = prev.get(item.minute_token);
    if (!old) return item;
    const keepInFlight =
      (old.summary_status === "QUEUED" || old.summary_status === "GENERATING") &&
      (!item.summary_status || item.summary_status === "NONE");
    return keepInFlight ? { ...item, summary_status: old.summary_status } : item;
  });
}

function showCachedListFirst() {
  const cached = loadListCache();
  if (!cached) return;
  state.allItems = cached.items;
  state.lastSyncedAt = cached.saved_at || null;
  applyLocalView({ resetPage: true });
  setStatus("Thinking...", { thinking: true });
}

async function fetchCloudPage(pageToken) {
  const res = await apiFetch(`/meetings/cloud?${buildCloudSyncParams(pageToken)}`);
  if (res.status === 401) {
    const err = await res.json().catch(() => ({}));
    const detail = err.detail || {};
    // 管理端已通过；这里的 401 来自飞书用户授权缺失
    const loginUrl = detail.login_url || "/api/v1/auth/feishu/login";
    $("#auth-banner").classList.remove("hidden");
    $("#login-link").href = loginUrl;
    throw new Error(detail.message || "请先登录飞书账号");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
  }
  return res.json();
}

async function refreshFromCloud() {
  if (state.syncing) return;
  state.syncing = true;
  if (!state.allItems.length) {
    showListThinking();
    setStatus("Thinking...", { thinking: true });
  } else {
    applyLocalView();
    setStatus("Thinking...", { thinking: true });
  }
  try {
    const byToken = new Map();
    let pageToken = null;
    let guard = 0;
    do {
      const data = await fetchCloudPage(pageToken);
      const batch = data.items || [];
      let added = 0;
      for (const item of batch) {
        if (!item?.minute_token || byToken.has(item.minute_token)) continue;
        byToken.set(item.minute_token, item);
        added += 1;
      }
      // 飞书偶发 has_more=true 却不再给出新会议时立刻停，避免空转
      pageToken = data.has_more && added > 0 ? data.page_token : null;
      guard += 1;
      if (byToken.size > 0 && !state.allItems.length) {
        setStatus(`Thinking... · ${byToken.size}`);
      } else {
        setStatus("Thinking...", { thinking: true });
      }
    } while (pageToken && guard < 200);

    const collected = [...byToken.values()];
    state.allItems = mergeByMinuteToken(collected, state.allItems);
    state.lastSyncedAt = Date.now();
    saveListCache(state.allItems);
    applyLocalView({ resetPage: true });
  } catch (e) {
    if (state.allItems.length) {
      applyLocalView();
      setStatus(`云端同步失败：${e.message}（下方仍是本地数据）`);
      return;
    }
    setStatus(`加载失败：${e.message}`);
    state.items = [];
    renderList();
    updatePager(1);
  } finally {
    state.syncing = false;
    if (state.allItems.length) applyLocalView();
  }
}

async function downloadOne(token, options = {}) {
  const res = await apiFetch(`/meetings/download?sync=true`, {
    method: "POST",
    body: JSON.stringify({
      minute_tokens: [token],
      skip_if_completed: options.skip_if_completed !== false,
      redownload_media: Boolean(options.redownload_media),
      redownload_transcript: Boolean(options.redownload_transcript),
    }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  const data = await res.json();
  return data.results[0];
}

function closeDownloadBatchDialog() {
  $("#download-batch-dialog").classList.add("hidden");
  state.pendingBatchDownload = null;
}

function openDownloadBatchDialog(plan) {
  state.pendingBatchDownload = plan;
  $("#download-redownload-media").checked = false;
  $("#download-redownload-transcript").checked = false;
  $("#download-batch-msg").textContent =
    `选中 ${plan.tokens.length} 个会议，其中 ${plan.localCount} 个已同步到本地。` +
    `可勾选是否覆盖已有音视频/转写；未勾选则保留本地文件，只下载尚未同步的会议。`;
  $("#download-batch-dialog").classList.remove("hidden");
}

async function runBatchDownload({
  tokens,
  redownload_media = false,
  redownload_transcript = false,
} = {}) {
  if (!tokens.length) return;

  hideScopeBanner();
  $("#download-btn").disabled = true;
  $("#select-all-btn").disabled = true;
  $("#batch-summary-btn").disabled = true;
  $("#share-selected-btn").disabled = true;

  tokens.forEach((token) => {
    setItemProgress(token, { percent: 0, stage: "排队中", status: "pending" });
  });

  const failed = [];
  let scopeShown = false;
  let completed = 0;
  let cursor = 0;

  const runOne = async (token) => {
    setItemProgress(token, { percent: 0, stage: "准备下载", status: "downloading" });
    startProgressPolling(token);
    try {
      const result = await downloadOne(token, {
        skip_if_completed: true,
        redownload_media,
        redownload_transcript,
      });
      stopProgressPolling(token);
      if (result.status === "COMPLETED") {
        setItemProgress(token, { percent: 100, stage: "下载完成", status: "completed" });
        const row = state.allItems.find((item) => item.minute_token === token);
        if (row) {
          row.is_local = true;
          row.local_status = "COMPLETED";
        }
        completed += 1;
      } else if (result.status === "FAILED") {
        setItemProgress(token, {
          percent: state.downloadProgress[token]?.percent ?? 0,
          stage: "下载失败",
          status: "failed",
        });
        failed.push(result);
        if (!scopeShown && showScopeError(result)) scopeShown = true;
      }
    } catch (e) {
      stopProgressPolling(token);
      setItemProgress(token, { percent: 0, stage: "下载失败", status: "failed" });
      failed.push({ minute_token: token, status: "FAILED", error_message: e.message });
    } finally {
      setStatus(
        `下载中 ${completed + failed.length}/${tokens.length}` +
          (failed.length ? `，失败 ${failed.length}` : "")
      );
    }
  };

  try {
    setStatus(`开始下载（并发 ${DOWNLOAD_CONCURRENCY}）…`);
    const workers = Array.from(
      { length: Math.min(DOWNLOAD_CONCURRENCY, tokens.length) },
      async () => {
        while (cursor < tokens.length) {
          const index = cursor;
          cursor += 1;
          await runOne(tokens[index]);
        }
      }
    );
    await Promise.all(workers);

    if (failed.length) {
      const first = failed[0];
      if (!scopeShown && showScopeError(first)) scopeShown = true;
      applyLocalView();
      setStatus(`完成，${failed.length} 个失败。${scopeShown ? "请按上方提示开通权限或重新登录。" : ""}`);
    } else {
      state.selected.clear();
      applyLocalView();
      setStatus("全部下载完成");
    }
    saveListCache(state.allItems);
  } catch (e) {
    setStatus(`下载失败：${e.message}`);
  } finally {
    tokens.forEach(stopProgressPolling);
    updateDownloadBtn();
    $("#select-all-btn").disabled = false;
  }
}

async function downloadSelected() {
  const tokens = [...state.selected];
  if (!tokens.length) return;

  const localCount = tokens.filter((token) => {
    const item = state.allItems.find((row) => row.minute_token === token);
    return item && canViewLocal(item);
  }).length;

  if (localCount > 0) {
    openDownloadBatchDialog({ tokens, localCount });
    return;
  }

  await runBatchDownload({ tokens });
}

function selectAllVisible() {
  const tokens = state.items.map((item) => item.minute_token).filter(Boolean);
  if (!tokens.length) return;
  const allSelected = tokens.every((token) => state.selected.has(token));
  if (allSelected) {
    tokens.forEach((token) => state.selected.delete(token));
  } else {
    tokens.forEach((token) => state.selected.add(token));
  }
  renderList();
  updateDownloadBtn();
}

function closeSummaryBatchDialog() {
  $("#summary-batch-dialog").classList.add("hidden");
  state.pendingBatchSummary = null;
}

function openSummaryBatchDialog(plan) {
  state.pendingBatchSummary = plan;
  const missing = plan.missing.length;
  const existing = plan.existing.length;
  const skipped = plan.inFlight.length;
  const parts = [
    `选中 ${plan.localCount} 个已下载会议`,
    `其中 ${existing} 个已有纪要`,
    `${missing} 个尚无纪要`,
  ];
  if (skipped) parts.push(`${skipped} 个正在生成中将跳过`);
  $("#summary-batch-msg").textContent =
    `${parts.join("，")}。可选择保留已有纪要，或对已有纪要的会议重新生成（会覆盖现有内容）。`;
  $("#summary-batch-dialog").classList.remove("hidden");
}

function classifyBatchSummaryTargets(items) {
  const missing = [];
  const existing = [];
  const inFlight = [];
  for (const item of items) {
    const status = normalizeSummaryStatus(item.summary_status);
    if (status === "QUEUED" || status === "GENERATING") {
      inFlight.push(item);
    } else if (status === "READY") {
      existing.push(item);
    } else {
      // NONE / FAILED 都当作需要生成
      missing.push(item);
    }
  }
  return { missing, existing, inFlight, localCount: items.length };
}

async function enqueueSummary(token, force) {
  const res = await apiFetch(`/meetings/${token}/summary`, {
    method: "POST",
    body: JSON.stringify({ force }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const detail = err.detail;
    throw new Error(
      typeof detail === "string" ? detail : detail?.message || res.statusText
    );
  }
  return res.json();
}

async function runBatchSummary({ tokens, force }) {
  if (!tokens.length) {
    setStatus("没有需要生成的会议");
    return;
  }

  const btn = $("#batch-summary-btn");
  btn.disabled = true;
  $("#download-btn").disabled = true;
  $("#select-all-btn").disabled = true;
  $("#share-selected-btn").disabled = true;

  let ok = 0;
  let failed = 0;
  const errors = [];

  try {
    for (let i = 0; i < tokens.length; i += 1) {
      const token = tokens[i];
      setStatus(`正在提交纪要 ${i + 1}/${tokens.length}…`);
      try {
        await enqueueSummary(token, force);
        ok += 1;
        const row = state.allItems.find((item) => item.minute_token === token);
        if (row) row.summary_status = "QUEUED";
      } catch (e) {
        failed += 1;
        errors.push(`${token}: ${e.message}`);
      }
    }

    saveListCache(state.allItems);
    applyLocalView();
    if (failed) {
      setStatus(
        `已提交 ${ok} 个，失败 ${failed} 个。${errors[0] || ""}` +
          (force ? "（重新生成）" : "（仅缺失）")
      );
    } else {
      setStatus(
        force
          ? `已提交 ${ok} 个纪要任务（含重新生成），可在列表徽章查看进度`
          : `已提交 ${ok} 个纪要任务，可在列表徽章查看进度`
      );
    }
  } finally {
    updateDownloadBtn();
    $("#select-all-btn").disabled = false;
  }
}

async function batchGenerateSummaries() {
  const locals = selectedLocalItems();
  if (!locals.length) {
    setStatus("请先选中已下载到本地的会议");
    return;
  }

  const plan = classifyBatchSummaryTargets(locals);
  if (!plan.missing.length && !plan.existing.length) {
    setStatus(
      plan.inFlight.length
        ? `选中的 ${plan.inFlight.length} 个会议已在生成中，无需重复提交`
        : "没有可生成纪要的会议"
    );
    return;
  }

  if (plan.existing.length) {
    openSummaryBatchDialog(plan);
    return;
  }

  await runBatchSummary({
    tokens: plan.missing.map((item) => item.minute_token),
    force: false,
  });
}

function showAuthFailure(message) {
  const banner = $("#auth-banner");
  const text = banner.querySelector("span");
  banner.classList.remove("hidden");
  if (text) text.textContent = `登录失败：${message}`;
  setStatus(`登录失败：${message}`);
}

defaultTimeRange();
$("#page-size").value = String(state.pageSize);
$("#sort-order").value = state.sortOrder;

$("#page-size").addEventListener("change", (e) => {
  const next = Number(e.target.value);
  state.pageSize = PAGE_SIZE_OPTIONS.includes(next) ? next : 20;
  localStorage.setItem(PAGE_SIZE_KEY, String(state.pageSize));
  applyLocalView({ resetPage: true });
});
$("#sort-order").addEventListener("change", (e) => {
  state.sortOrder = e.target.value;
  applyLocalView({ resetPage: true });
});
function syncShareBatchModeUi() {
  const mode = $("#share-batch-access-mode")?.value;
  const wrap = $("#share-batch-key-field");
  if (wrap) wrap.classList.toggle("hidden", mode !== "KEY_REQUIRED");
}

async function loadShareBatchKeyOptions() {
  const select = $("#share-batch-key-select");
  if (!select) return;
  const res = await apiFetch("/access-keys");
  if (!res.ok) {
    select.innerHTML = `<option value="">加载密钥失败</option>`;
    return;
  }
  const data = await res.json();
  const active = (data.items || []).filter((k) => k.status === "ACTIVE");
  select.innerHTML = active.length
    ? active
        .map(
          (k) =>
            `<option value="${k.id}">${escapeHtml(k.name)}（${escapeHtml(k.key_prefix)}…）</option>`
        )
        .join("")
    : `<option value="">暂无可用密钥，请先到密钥管理创建</option>`;
}

function closeShareBatchDialog() {
  $("#share-batch-dialog").classList.add("hidden");
}

async function openShareBatchDialog(items) {
  $("#share-batch-msg").textContent = `将为已选中的 ${items.length} 个本地会议创建/复用分享链接。`;
  await loadShareBatchKeyOptions();
  syncShareBatchModeUi();
  $("#share-batch-dialog").classList.remove("hidden");
}

function shareLineForItem(item, url) {
  const title = meetingTitle(item);
  const time = item.create_time ? formatTime(item.create_time) || String(item.create_time) : "时间未知";
  return `${title} ${time} ${url}`;
}

async function shareSelected() {
  const items = selectedLocalItems();
  if (!items.length) {
    setStatus("请先选中已下载到本地的会议");
    return;
  }
  await openShareBatchDialog(items);
}

async function confirmShareSelected() {
  const items = selectedLocalItems();
  if (!items.length) {
    closeShareBatchDialog();
    setStatus("请先选中已下载到本地的会议");
    return;
  }

  const access_mode = $("#share-batch-access-mode").value;
  const allow_export = $("#share-batch-allow-export").checked;
  const access_key_id =
    access_mode === "KEY_REQUIRED"
      ? Number($("#share-batch-key-select").value) || null
      : null;
  if (access_mode === "KEY_REQUIRED" && !access_key_id) {
    alert("请先创建并选择一把密钥");
    return;
  }

  closeShareBatchDialog();

  const btn = $("#share-selected-btn");
  btn.disabled = true;
  try {
    setStatus(`正在批量准备 ${items.length} 个分享链接…`);
    const res = await apiFetch("/shares/batch", {
      method: "POST",
      body: JSON.stringify({
        minute_tokens: items.map((item) => item.minute_token),
        access_mode,
        allow_export,
        access_key_id: access_mode === "KEY_REQUIRED" ? access_key_id : null,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : "批量分享失败");
    }
    const data = await res.json();
    const byToken = new Map((data.items || []).map((row) => [row.minute_token, row]));
    const lines = [];
    const failed = [];
    for (const item of items) {
      const row = byToken.get(item.minute_token);
      if (row?.url) {
        lines.push(shareLineForItem(item, row.url));
      } else {
        failed.push(`${meetingTitle(item)}：${row?.error || "未返回分享链接"}`);
      }
    }

    if (!lines.length) {
      setStatus(`分享失败：${failed[0] || "没有可用链接"}`);
      return;
    }

    const text = lines.join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setStatus(
        failed.length
          ? `已复制 ${lines.length} 条分享链接，${failed.length} 个失败`
          : `已复制 ${lines.length} 条分享链接（课程名 + 时间 + 链接）`
      );
    } catch {
      window.prompt("复制失败，请手动全选复制：", text);
      setStatus(`已生成 ${lines.length} 条分享链接，请手动复制`);
    }
  } catch (e) {
    setStatus(`分享失败：${e.message}`);
  } finally {
    updateDownloadBtn();
  }
}

$("#search-btn").addEventListener("click", () => applyLocalView({ resetPage: true }));
$("#refresh-btn").addEventListener("click", () => {
  if (state.syncing) {
    setStatus("正在同步云端，请稍候…");
    return;
  }
  refreshFromCloud();
});
$("#search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") applyLocalView({ resetPage: true });
});
$("#select-all-btn").addEventListener("click", selectAllVisible);
$("#download-btn").addEventListener("click", downloadSelected);
$("#batch-summary-btn").addEventListener("click", batchGenerateSummaries);
$("#share-selected-btn").addEventListener("click", shareSelected);
$("#share-batch-access-mode").addEventListener("change", syncShareBatchModeUi);
$("#share-batch-confirm").addEventListener("click", confirmShareSelected);
$("#share-batch-dialog").addEventListener("click", (e) => {
  if (e.target.closest("[data-close-dialog]")) closeShareBatchDialog();
});

$("#download-batch-confirm").addEventListener("click", async () => {
  const plan = state.pendingBatchDownload;
  const redownload_media = $("#download-redownload-media").checked;
  const redownload_transcript = $("#download-redownload-transcript").checked;
  closeDownloadBatchDialog();
  if (!plan) return;
  await runBatchDownload({
    tokens: plan.tokens,
    redownload_media,
    redownload_transcript,
  });
});

$("#download-batch-dialog").addEventListener("click", (e) => {
  if (e.target.closest("[data-close-dialog]")) closeDownloadBatchDialog();
});

$("#summary-batch-keep").addEventListener("click", async () => {
  const plan = state.pendingBatchSummary;
  closeSummaryBatchDialog();
  if (!plan) return;
  await runBatchSummary({
    tokens: plan.missing.map((item) => item.minute_token),
    force: false,
  });
});

$("#summary-batch-force").addEventListener("click", async () => {
  const plan = state.pendingBatchSummary;
  closeSummaryBatchDialog();
  if (!plan) return;
  await runBatchSummary({
    tokens: [...plan.missing, ...plan.existing].map((item) => item.minute_token),
    force: true,
  });
});

$("#summary-batch-dialog").addEventListener("click", (e) => {
  if (e.target.closest("[data-close-dialog]")) closeSummaryBatchDialog();
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#download-batch-dialog").classList.contains("hidden")) {
    closeDownloadBatchDialog();
    return;
  }
  if (!$("#summary-batch-dialog").classList.contains("hidden")) {
    closeSummaryBatchDialog();
    return;
  }
  if (!$("#share-batch-dialog").classList.contains("hidden")) {
    closeShareBatchDialog();
  }
});

$("#next-page").addEventListener("click", () => {
  const pageCount = Math.max(1, Math.ceil(state.filteredTotal / state.pageSize) || 1);
  if (state.pageIndex + 1 >= pageCount) return;
  state.pageIndex += 1;
  applyLocalView();
});

$("#prev-page").addEventListener("click", () => {
  if (state.pageIndex <= 0) return;
  state.pageIndex -= 1;
  applyLocalView();
});

showCachedListFirst();
checkAuth({ onMissingScopes: showMissingScopeWarning });

const bootParams = new URLSearchParams(location.search);
const bootAuth = bootParams.get("auth");
if (bootAuth === "ok") {
  history.replaceState({}, "", location.pathname);
  checkAuth({ onMissingScopes: showMissingScopeWarning });
  refreshFromCloud();
} else if (bootAuth === "error") {
  const raw = bootParams.get("msg") || "授权失败，请重试";
  let message = raw;
  try {
    message = decodeURIComponent(raw);
  } catch {
    /* 保持原样 */
  }
  history.replaceState({}, "", location.pathname);
  // 先展示失败原因，再拉列表；避免“加载失败/请先登录”把真正的 OAuth 错误盖掉
  showAuthFailure(message);
  refreshFromCloud().finally(() => {
    showAuthFailure(message);
  });
} else {
  refreshFromCloud();
}

setInterval(refreshSummaryBadges, 8000);
bindAdminNav();
