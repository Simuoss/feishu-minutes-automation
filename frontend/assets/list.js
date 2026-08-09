if (!requireAdminPage()) throw new Error("redirecting to login");

function listCacheKey() {
  return isSuperAdminView() ? "minutes_list_cache_super_v1" : "minutes_list_cache_v1";
}
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
  progressItems: new Map(),
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

function openMeeting(token, ownerId) {
  let url = `/meeting.html?token=${encodeURIComponent(token)}`;
  if (isSuperAdminView() && ownerId != null && ownerId !== "") {
    url += `&owner_user_id=${encodeURIComponent(ownerId)}`;
  }
  location.href = url;
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

function progressItemKey(token, ownerId) {
  return ownerId != null && ownerId !== "" ? `${ownerId}:${token}` : String(token);
}

function ownerUserIdFromItem(item) {
  const raw = item?.owner_id ?? item?.owner_user_id;
  if (raw == null || raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

async function pollDownloadProgressBatch() {
  const items = [...state.progressItems.values()];
  if (!items.length) {
    if (state.progressBatchTimer) {
      clearInterval(state.progressBatchTimer);
      state.progressBatchTimer = null;
    }
    return;
  }
  try {
    const res = await apiFetch("/meetings/download/progress/batch", {
      method: "POST",
      body: JSON.stringify({ items }),
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

function startProgressPolling(token, ownerId) {
  const key = progressItemKey(token, ownerId);
  state.progressItems.set(key, {
    minute_token: token,
    owner_user_id: ownerId != null && ownerId !== "" ? Number(ownerId) : null,
  });
  if (state.progressBatchTimer) return;
  state.progressBatchTimer = setInterval(pollDownloadProgressBatch, 1200);
  pollDownloadProgressBatch();
}

function stopProgressPolling(token, ownerId) {
  state.progressItems.delete(progressItemKey(token, ownerId));
  if (!state.progressItems.size && state.progressBatchTimer) {
    clearInterval(state.progressBatchTimer);
    state.progressBatchTimer = null;
  }
}

function canViewLocal(item) {
  return Boolean(item.is_local || item.local_status === "COMPLETED");
}

function localBadge(item) {
  if (item.is_local || item.local_status === "COMPLETED") {
    return `<span class="badge badge-local">已同步</span>`;
  }
  if (item.local_status === "DOWNLOADING") {
    return `<span class="badge badge-pending">同步中</span>`;
  }
  if (item.local_status === "FAILED") {
    return `<span class="badge badge-failed">同步失败</span>`;
  }
  return `<span class="badge">未同步</span>`;
}

const STATUS_FILTER_OPTIONS = [
  { value: "UNSYNCED", label: "未同步" },
  { value: "SYNCING", label: "同步中" },
  { value: "SYNCED", label: "已同步" },
  { value: "NO_SUMMARY", label: "未生成纪要" },
  { value: "SUMMARY_GENERATING", label: "生成纪要中" },
  { value: "HAS_SUMMARY", label: "有纪要" },
];

/** @type {Map<string, {selected: Set<string>, options: {value:string,label:string}[], onChange: Function}>} */
const multiSelectState = new Map();

function selectedStatusFilters() {
  return getMultiSelectValues("status-filter");
}

function selectedSpeakerFilters() {
  return getMultiSelectValues("speaker-filter");
}

function selectedOwnerFilters() {
  return [...document.querySelectorAll("#owner-filter-tags .filter-tag.is-active")]
    .map((el) => el.dataset.ownerId)
    .filter(Boolean);
}

function getMultiSelectValues(id) {
  const st = multiSelectState.get(id);
  return st ? [...st.selected] : [];
}

function renderMultiSelect(id) {
  const root = document.getElementById(id);
  const st = multiSelectState.get(id);
  if (!root || !st) return;
  const placeholder = root.dataset.placeholder || "请选择…";
  const selectedLabels = st.options
    .filter((opt) => st.selected.has(opt.value))
    .map((opt) => opt.label);
  const triggerText = selectedLabels.length
    ? `已选 ${selectedLabels.length} 项`
    : placeholder;
  const optionsHtml = st.options.length
    ? st.options
        .map(
          (opt) => `<label class="multi-select-option">
            <input type="checkbox" value="${escapeHtml(opt.value)}"${
              st.selected.has(opt.value) ? " checked" : ""
            } />
            <span>${escapeHtml(opt.label)}</span>
          </label>`
        )
        .join("")
    : `<div class="multi-select-empty">暂无可选项</div>`;
  const chipsHtml = selectedLabels.length
    ? st.options
        .filter((opt) => st.selected.has(opt.value))
        .map(
          (opt) => `<button type="button" class="multi-select-chip" data-value="${escapeHtml(
            opt.value
          )}">${escapeHtml(opt.label)}<i class="ri ri-close-line" aria-hidden="true"></i></button>`
        )
        .join("")
    : "";
  const open = root.classList.contains("is-open");
  root.innerHTML = `
    <button type="button" class="multi-select-trigger" aria-expanded="${open ? "true" : "false"}">
      <span>${escapeHtml(triggerText)}</span>
      <i class="ri ri-arrow-down-s-line" aria-hidden="true"></i>
    </button>
    <div class="multi-select-panel${open ? "" : " hidden"}">${optionsHtml}</div>
    <div class="multi-select-chips">${chipsHtml}</div>
  `;
}

function bindMultiSelect(id, { options, onChange }) {
  const root = document.getElementById(id);
  if (!root) return;
  const prev = multiSelectState.get(id);
  const selected = new Set(prev ? prev.selected : []);
  const valid = new Set(options.map((o) => o.value));
  for (const value of [...selected]) {
    if (!valid.has(value)) selected.delete(value);
  }
  multiSelectState.set(id, { selected, options, onChange });
  if (!root.dataset.boundMultiSelect) {
    root.dataset.boundMultiSelect = "1";
    root.addEventListener("click", (e) => {
      const st = multiSelectState.get(id);
      if (!st) return;
      const trigger = e.target.closest(".multi-select-trigger");
      if (trigger && root.contains(trigger)) {
        // 阻止冒泡：innerHTML 重绘后原 target 已脱离 DOM，
        // 文档级监听会误判为「点在外面」并立刻关掉面板
        e.stopPropagation();
        root.classList.toggle("is-open");
        renderMultiSelect(id);
        return;
      }
      const chip = e.target.closest(".multi-select-chip");
      if (chip && root.contains(chip)) {
        e.stopPropagation();
        st.selected.delete(chip.dataset.value || "");
        renderMultiSelect(id);
        st.onChange();
        return;
      }
      if (e.target.closest(".multi-select-panel") && root.contains(e.target)) {
        e.stopPropagation();
      }
    });
    root.addEventListener("change", (e) => {
      const st = multiSelectState.get(id);
      const input = e.target;
      if (!st || !(input instanceof HTMLInputElement) || input.type !== "checkbox") return;
      if (!root.contains(input)) return;
      e.stopPropagation();
      if (input.checked) st.selected.add(input.value);
      else st.selected.delete(input.value);
      renderMultiSelect(id);
      st.onChange();
    });
    document.addEventListener("click", (e) => {
      if (!root.classList.contains("is-open")) return;
      const target = e.target;
      // 重绘后旧节点已脱离 DOM，不能当「点在外面」处理
      if (target instanceof Node && (!target.isConnected || root.contains(target))) return;
      root.classList.remove("is-open");
      renderMultiSelect(id);
    });
  }
  renderMultiSelect(id);
}

function syncSpeakerFilterOptions() {
  const names = new Set();
  for (const item of state.allItems) {
    for (const name of item.speakers || []) {
      const n = String(name || "").trim();
      if (n) names.add(n);
    }
  }
  const options = [...names]
    .sort((a, b) => a.localeCompare(b, "zh"))
    .map((name) => ({ value: name, label: name }));
  bindMultiSelect("speaker-filter", {
    options,
    onChange: () => applyLocalView({ resetPage: true }),
  });
}

function itemMatchesOneStatus(item, filter) {
  const local = item.local_status || "NONE";
  const synced = Boolean(item.is_local || local === "COMPLETED");
  const summary = normalizeSummaryStatus(item.summary_status);
  switch (filter) {
    case "UNSYNCED":
      return !synced && local !== "DOWNLOADING";
    case "SYNCING":
      return local === "DOWNLOADING";
    case "SYNCED":
      return synced;
    case "NO_SUMMARY":
      return synced && summary !== "READY" && summary !== "GENERATING" && summary !== "QUEUED";
    case "SUMMARY_GENERATING":
      return summary === "GENERATING" || summary === "QUEUED";
    case "HAS_SUMMARY":
      return summary === "READY";
    default:
      return true;
  }
}

function matchesStatusFilters(item, filters) {
  if (!filters.length) return true;
  return filters.some((filter) => itemMatchesOneStatus(item, filter));
}

function matchesOwnerFilters(item, ownerIds) {
  if (!ownerIds.length) return true;
  return ownerIds.includes(String(item.owner_id || ""));
}

function matchesSpeakerFilters(item, speakers) {
  if (!speakers.length) return true;
  const present = new Set((item.speakers || []).map((n) => String(n)));
  return speakers.some((name) => present.has(name));
}

function bindFilterTagClicks(container, onChange) {
  if (!container || container.dataset.boundFilterTags) return;
  container.dataset.boundFilterTags = "1";
  container.addEventListener("click", (e) => {
    const tag = e.target.closest(".filter-tag");
    if (!tag || !container.contains(tag)) return;
    tag.classList.toggle("is-active");
    onChange();
  });
}

/** 超管用户表：整页生命周期只请求一次 /admin/users */
let adminUsersCache = null;
let adminUsersPromise = null;

async function ensureAdminUsers() {
  if (adminUsersCache) return adminUsersCache;
  if (adminUsersPromise) return adminUsersPromise;
  adminUsersPromise = (async () => {
    const owners = new Map();
    try {
      const res = await apiFetch("/admin/users");
      if (res.ok) {
        const data = await res.json();
      for (const user of data.items || []) {
        if (user?.id == null) continue;
        owners.set(
          String(user.id),
          user.display_name || user.username || `用户 ${user.id}`
        );
      }
      }
    } catch {
      /* 回退到列表内已有归属 */
    }
    adminUsersCache = owners;
    return owners;
  })().finally(() => {
    adminUsersPromise = null;
  });
  return adminUsersPromise;
}

async function loadOwnerFilterTags() {
  const field = $("#owner-filter-field");
  const box = $("#owner-filter-tags");
  if (!field || !box) return;
  if (!isSuperAdminView()) {
    field.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  field.classList.remove("hidden");
  const prev = new Set(selectedOwnerFilters());
  const owners = new Map(await ensureAdminUsers());
  for (const item of state.allItems) {
    if (item.owner_id == null || item.owner_id === "") continue;
    const id = String(item.owner_id);
    if (!owners.has(id)) {
      owners.set(id, item.owner_name || `用户 ${id}`);
    }
  }

  const entries = [...owners.entries()].sort((a, b) =>
    String(a[1]).localeCompare(String(b[1]), "zh")
  );
  if (!entries.length) {
    box.innerHTML = `<span class="meeting-meta">暂无管理员</span>`;
    return;
  }
  box.innerHTML = entries
    .map(
      ([id, name]) =>
        `<button type="button" class="filter-tag${prev.has(id) ? " is-active" : ""}" data-owner-id="${escapeHtml(id)}">${escapeHtml(name)}</button>`
    )
    .join("");
}

function syncOwnerFilterOptions() {
  return loadOwnerFilterTags();
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
    case "QUEUED":
      return `<span class="badge badge-pending">生成纪要中</span>`;
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
        items: inflight.map((item) => ({
          minute_token: item.minute_token,
          owner_user_id: ownerUserIdFromItem(item),
        })),
      }),
    });
    if (!res.ok) return;
    const data = await res.json();
    const byKey = new Map(
      (data.items || []).map((row) => [
        progressItemKey(row.minute_token, row.owner_user_id),
        row,
      ])
    );
    let changed = false;
    for (const item of inflight) {
      const row = byKey.get(progressItemKey(item.minute_token, ownerUserIdFromItem(item)));
      if (!row) continue;
      const next = normalizeSummaryStatus(row.status || item.summary_status);
      if (next !== normalizeSummaryStatus(item.summary_status)) {
        item.summary_status = next;
        changed = true;
      }
      const total = Number(row.llm_slots_total);
      const free = Number(row.llm_slots_free);
      const poolHint =
        Number.isFinite(total) && total > 0
          ? `大模型空闲 ${Number.isFinite(free) ? Math.max(0, free) : 0}/${total}`
          : "";
      const stageBase = row.stage || (next === "QUEUED" ? "排队中" : "生成纪要中");
      setItemProgress(item.minute_token, {
        percent: row.percent,
        stage: poolHint ? `${stageBase} · ${poolHint}` : stageBase,
        status: next === "QUEUED" ? "pending" : "downloading",
      });
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
  const statusFilters = selectedStatusFilters();
  const speakerFilters = selectedSpeakerFilters();
  const ownerFilters = selectedOwnerFilters();

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
        ...(item.speakers || []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      if (!hay.includes(keyword)) return false;
    }
    if (!matchesStatusFilters(item, statusFilters)) return false;
    if (!matchesSpeakerFilters(item, speakerFilters)) return false;
    if (!matchesOwnerFilters(item, ownerFilters)) return false;
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
    ? isSuperAdminView()
      ? " · 正在加载全站…"
      : " · 正在同步云端…"
    : state.lastSyncedAt
      ? isSuperAdminView()
        ? ` · 全站 ${describeCacheAge(state.lastSyncedAt)}`
        : ` · 云端 ${describeCacheAge(state.lastSyncedAt)}`
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
    const ownerAttr =
      item.owner_id != null && item.owner_id !== ""
        ? ` data-owner="${escapeHtml(String(item.owner_id))}"`
        : "";
    const viewBtn = canOpen
      ? `<button type="button" class="btn btn-sm btn-view" data-view="${item.minute_token}"${ownerAttr}>${btnContent("eye-line", "查看")}</button>`
      : "";

    li.innerHTML = `
      <input type="checkbox" data-token="${item.minute_token}" ${state.selected.has(item.minute_token) ? "checked" : ""} />
      <div class="meeting-body">
        <div class="meeting-title-row">
          <p class="${titleClass}" data-token="${item.minute_token}" data-open="${canOpen}"${ownerAttr}>${escapeHtml(title)}${localBadge(item)}${summaryBadge(item)}</p>
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
    el.addEventListener("click", () => openMeeting(el.dataset.token, el.dataset.owner));
  });

  list.querySelectorAll(".btn-view").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openMeeting(btn.dataset.view, btn.dataset.owner);
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
      listCacheKey(),
      JSON.stringify({ saved_at: Date.now(), items })
    );
  } catch {
    /* 无痕模式或配额已满：缓存只是加速手段，写不进去不影响正常加载 */
  }
}

function loadListCache() {
  let cached;
  try {
    cached = JSON.parse(localStorage.getItem(listCacheKey()) || "null");
  } catch {
    localStorage.removeItem(listCacheKey());
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

/** 翻页同步中：已拉到的云端页覆盖同 token，其余缓存项先保留，避免列表先缩后涨 */
function mergeCloudProgress(partialFresh, baseline) {
  const seen = new Set(partialFresh.map((item) => item.minute_token));
  const merged = mergeByMinuteToken(partialFresh, baseline);
  const pending = baseline.filter((item) => item?.minute_token && !seen.has(item.minute_token));
  return pending.length ? merged.concat(pending) : merged;
}

function showCachedListFirst() {
  const cached = loadListCache();
  if (!cached) return;
  state.allItems = cached.items;
  state.lastSyncedAt = cached.saved_at || null;
  syncOwnerFilterOptions();
  syncSpeakerFilterOptions();
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

async function fetchStoredList() {
  const res = await apiFetch("/meetings/stored?limit=2000");
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const detail =
      typeof err.detail === "string"
        ? err.detail
        : res.status === 404
          ? "全站列表接口不存在，请重启后端后再试"
          : res.statusText || "加载失败";
    throw new Error(detail);
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
    let collected;
    const previousBaseline = state.allItems;
    if (isSuperAdminView()) {
      // 超管：全站列表与用户表并行（用户表整页只打一次）
      const [data] = await Promise.all([fetchStoredList(), ensureAdminUsers()]);
      collected = data.items || [];
    } else {
      // 普通用户：page_token 必须串行；每页回来就渐进渲染，缩短首屏空白
      const byToken = new Map();
      let pageToken = null;
      let guard = 0;
      let firstPaint = true;
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
        const partial = [...byToken.values()];
        state.allItems = mergeCloudProgress(partial, previousBaseline);
        state.lastSyncedAt = Date.now();
        syncSpeakerFilterOptions();
        applyLocalView(firstPaint ? { resetPage: true } : undefined);
        firstPaint = false;
        setStatus(
          pageToken
            ? `Thinking... · 已同步 ${byToken.size}`
            : "Thinking...",
          { thinking: true }
        );
      } while (pageToken && guard < 200);
      collected = [...byToken.values()];
    }

    state.allItems = mergeByMinuteToken(collected, previousBaseline);
    state.lastSyncedAt = Date.now();
    saveListCache(state.allItems);
    syncOwnerFilterOptions();
    syncSpeakerFilterOptions();
    applyLocalView({ resetPage: true });
  } catch (e) {
    if (state.allItems.length) {
      applyLocalView();
      setStatus(
        isSuperAdminView()
          ? `全站列表加载失败：${e.message}（下方仍是缓存）`
          : `云端同步失败：${e.message}（下方仍是本地数据）`
      );
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

  const jobs = tokens.map((token) => {
    const row = state.allItems.find((item) => item.minute_token === token);
    return { token, ownerId: ownerUserIdFromItem(row) };
  });

  jobs.forEach(({ token }) => {
    setItemProgress(token, { percent: 0, stage: "排队中", status: "pending" });
  });

  const failed = [];
  let scopeShown = false;
  let completed = 0;
  let cursor = 0;

  const markCompleted = (token) => {
    setItemProgress(token, { percent: 100, stage: "下载完成", status: "completed" });
    const row = state.allItems.find((item) => item.minute_token === token);
    if (row) {
      row.is_local = true;
      row.local_status = "COMPLETED";
    }
    completed += 1;
  };

  const waitUntilDownloadSettled = async (token) => {
    const deadline = Date.now() + 30 * 60 * 1000;
    while (Date.now() < deadline) {
      const progress = state.downloadProgress[token];
      if (progress && (progress.status === "completed" || progress.status === "failed")) {
        return progress;
      }
      await new Promise((r) => setTimeout(r, 400));
    }
    return state.downloadProgress[token] || { status: "failed", stage: "下载超时" };
  };

  const runOne = async ({ token, ownerId }) => {
    setItemProgress(token, { percent: 0, stage: "准备下载", status: "downloading" });
    startProgressPolling(token, ownerId);
    try {
      const result = await downloadOne(token, {
        skip_if_completed: true,
        redownload_media,
        redownload_transcript,
      });
      if (result.status === "COMPLETED") {
        stopProgressPolling(token, ownerId);
        markCompleted(token);
      } else if (result.status === "FAILED") {
        stopProgressPolling(token, ownerId);
        setItemProgress(token, {
          percent: state.downloadProgress[token]?.percent ?? 0,
          stage: "下载失败",
          status: "failed",
        });
        failed.push(result);
        if (!scopeShown && showScopeError(result)) scopeShown = true;
      } else {
        // DOWNLOADING：依赖 progress batch 轮询直到终态
        const progress = await waitUntilDownloadSettled(token);
        stopProgressPolling(token, ownerId);
        if (progress.status === "completed") {
          markCompleted(token);
        } else {
          setItemProgress(token, {
            percent: progress.percent ?? 0,
            stage: progress.stage || "下载失败",
            status: "failed",
          });
          const fail = {
            minute_token: token,
            status: "FAILED",
            error_message: progress.error_message || progress.stage || "下载失败",
          };
          failed.push(fail);
          if (!scopeShown && showScopeError(fail)) scopeShown = true;
        }
      }
    } catch (e) {
      stopProgressPolling(token, ownerId);
      setItemProgress(token, { percent: 0, stage: "下载失败", status: "failed" });
      failed.push({ minute_token: token, status: "FAILED", error_message: e.message });
    } finally {
      setStatus(
        `下载中 ${completed + failed.length}/${jobs.length}` +
          (failed.length ? `，失败 ${failed.length}` : "")
      );
    }
  };

  try {
    setStatus(`开始下载（并发 ${DOWNLOAD_CONCURRENCY}）…`);
    const workers = Array.from(
      { length: Math.min(DOWNLOAD_CONCURRENCY, jobs.length) },
      async () => {
        while (cursor < jobs.length) {
          const index = cursor;
          cursor += 1;
          await runOne(jobs[index]);
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
    jobs.forEach(({ token, ownerId }) => stopProgressPolling(token, ownerId));
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

function closeShareBatchResultDialog() {
  $("#share-batch-result-dialog")?.classList.add("hidden");
}

function showShareBatchResult({ lines, failed, firstUrl, keyHint, keyPlain }) {
  const dialog = $("#share-batch-result-dialog");
  if (!dialog) return;
  $("#share-batch-result-msg").textContent = failed.length
    ? `成功 ${lines.length} 条，失败 ${failed.length} 条`
    : `已生成 ${lines.length} 条分享链接`;
  const card = $("#share-batch-result-card");
  if (firstUrl) {
    card.innerHTML = shareResultCardHtml({
      url: firstUrl,
      title: lines.length > 1 ? "第一条链接（可扫码）" : "分享已就绪",
      keyPlain: keyPlain || null,
      keyHint: keyHint || "",
    });
    fillQrCode(card.querySelector("[data-share-qr]"), firstUrl, 160);
    card.querySelectorAll("[data-copy-text]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await copyTextToClipboard(btn.dataset.copyText || "");
      });
    });
  } else {
    card.innerHTML = "";
  }
  const list = $("#share-batch-result-list");
  const okItems = lines.map((line) => `<li>${escapeHtml(line)}</li>`).join("");
  const failItems = failed
    .map((line) => `<li class="login-error">${escapeHtml(line)}</li>`)
    .join("");
  list.innerHTML = okItems + failItems;
  const copyAll = $("#share-batch-copy-all");
  if (copyAll) {
    copyAll.onclick = async () => {
      const ok = await copyTextToClipboard(lines.join("\n"));
      if (ok) setStatus(`已复制 ${lines.length} 条分享链接`);
    };
  }
  dialog.classList.remove("hidden");
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
  const keySelect = $("#share-batch-key-select");
  const access_key_id =
    access_mode === "KEY_REQUIRED" ? Number(keySelect?.value) || null : null;
  if (access_mode === "KEY_REQUIRED" && !access_key_id) {
    alert("请先创建并选择一把密钥");
    return;
  }
  const keyLabel =
    access_mode === "KEY_REQUIRED" && keySelect?.selectedOptions?.[0]
      ? keySelect.selectedOptions[0].textContent.trim()
      : "";

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
    let firstUrl = "";
    for (const item of items) {
      const row = byToken.get(item.minute_token);
      if (row?.url) {
        if (!firstUrl) firstUrl = row.url;
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
      setStatus(`已生成 ${lines.length} 条分享链接`);
    }
    const keyPlain =
      access_mode === "KEY_REQUIRED" ? loadAccessKeyPlaintext(access_key_id) : "";
    showShareBatchResult({
      lines,
      failed,
      firstUrl,
      keyPlain,
      keyHint:
        !keyPlain && keyLabel
          ? `需使用密钥：${keyLabel}。本会话未缓存明文（仅刚创建的密钥可一键复制）。`
          : "",
    });
  } catch (e) {
    setStatus(`分享失败：${e.message}`);
  } finally {
    updateDownloadBtn();
  }
}

$("#search-btn").addEventListener("click", () => applyLocalView({ resetPage: true }));
$("#refresh-btn").addEventListener("click", () => {
  if (state.syncing) {
    setStatus(isSuperAdminView() ? "正在加载全站，请稍候…" : "正在同步云端，请稍候…");
    return;
  }
  refreshFromCloud();
});
$("#search-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") applyLocalView({ resetPage: true });
});
bindMultiSelect("status-filter", {
  options: STATUS_FILTER_OPTIONS,
  onChange: () => applyLocalView({ resetPage: true }),
});
bindFilterTagClicks($("#owner-filter-tags"), () => applyLocalView({ resetPage: true }));
$("#select-all-btn").addEventListener("click", selectAllVisible);
$("#download-btn").addEventListener("click", downloadSelected);
$("#batch-summary-btn").addEventListener("click", batchGenerateSummaries);
$("#share-selected-btn").addEventListener("click", shareSelected);
$("#share-batch-access-mode").addEventListener("change", syncShareBatchModeUi);
$("#share-batch-confirm").addEventListener("click", confirmShareSelected);
$("#share-batch-dialog").addEventListener("click", (e) => {
  if (e.target.closest("[data-close-dialog]")) closeShareBatchDialog();
});
$("#share-batch-result-dialog")?.addEventListener("click", (e) => {
  if (e.target.closest("[data-close-batch-result]")) closeShareBatchResultDialog();
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
  if (!$("#share-batch-result-dialog")?.classList.contains("hidden")) {
    closeShareBatchResultDialog();
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

async function bootListPage() {
  const bootParams = new URLSearchParams(location.search);
  let authError = null;
  if (bootParams.get("auth") === "error") {
    authError = bootParams.get("msg") || "授权失败，请重试";
    try {
      authError = decodeURIComponent(authError);
    } catch {
      /* keep */
    }
  }
  const sso = await finalizeSsoLoginFromQuery();
  if (!sso.ok) return;

  showCachedListFirst();
  await checkAuth({ onMissingScopes: showMissingScopeWarning });
  if (typeof maybePromptDisplayName === "function") {
    maybePromptDisplayName();
  }
  if (authError) {
    showAuthFailure(authError);
    await refreshFromCloud();
    showAuthFailure(authError);
  } else {
    refreshFromCloud();
  }
  setInterval(refreshSummaryBadges, 8000);
  bindAdminNav();
}

bootListPage();
