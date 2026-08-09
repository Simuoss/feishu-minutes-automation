const params = new URLSearchParams(location.search);
const shareToken = (params.get("s") || "").trim();

const shareState = {
  sessionId: "",
  trackSessionId: "",
  visitStartedAt: Date.now(),
  allowExport: false,
  minuteToken: "",
  fullTranscriptText: "",
  segments: [],
  mediaElement: null,
  mediaHandlers: null,
  activeSegmentIndex: -1,
  summaryMarkdown: "",
  scrollMode: localStorage.getItem("minutes_scroll_mode") === "free" ? "free" : "follow",
  activeTab: "summary",
  videoProgressPeak: 0,
  lastPlayReportAt: 0,
  lastPlayReportPct: -1,
  sessionEnded: false,
  libraryItems: [],
  libraryKeys: [],
};

function ensureTrackSessionId() {
  const key = `share_track_session:${shareToken}`;
  let id = sessionStorage.getItem(key) || "";
  if (!id) {
    id =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID()
        : `t_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
    sessionStorage.setItem(key, id);
  }
  shareState.trackSessionId = id;
  return id;
}

ensureTrackSessionId();
shareState.visitStartedAt = Date.now();

const SPEAKER_LINE_RE = /^(.+?)\s+(\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s*$/;
const TIME_ANCHOR_RE = /^\d{1,2}:\d{2}:\d{2}$/;

if (!shareToken) {
  document.body.innerHTML = `<main class="view"><p class="empty">缺少分享参数</p></main>`;
  throw new Error("missing share token");
}

function sessionStorageKey() {
  return shareSessionStorageKey(shareToken);
}

function loadSavedSession() {
  // localStorage：跨标签/关浏览器仍保留；后端重启后会话会失效，需靠记住的密钥自动重解
  return localStorage.getItem(sessionStorageKey()) || "";
}

function saveSession(id) {
  shareState.sessionId = id || "";
  if (id) localStorage.setItem(sessionStorageKey(), id);
  else localStorage.removeItem(sessionStorageKey());
}

async function tryUnlockWithSavedKeys() {
  const keys = loadAccessKeyList(shareToken);
  if (!keys.length) return false;
  const res = await shareFetch(`/share/${shareToken}/try-keys`, {
    method: "POST",
    body: JSON.stringify({ keys }),
  });
  if (!res.ok) return false;
  const data = await res.json();
  // 命中密钥未知明文顺序：把列表原样保留，仅确保会话建立
  const matchedPrefix = data.matched_key_prefix || "";
  if (matchedPrefix) {
    const hit = keys.find((k) => k.startsWith(matchedPrefix));
    if (hit) rememberAccessKey(hit, shareToken);
  }
  saveSession(data.share_session);
  shareState.allowExport = Boolean(data.allow_export);
  shareState.minuteToken = data.minute_token;
  await openDetail({ allowKeyRetry: false });
  return !$("#detail-view")?.classList.contains("hidden");
}

async function shareFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (shareState.sessionId) headers.set("X-Share-Session", shareState.sessionId);
  if (shareState.trackSessionId) {
    headers.set("X-Share-Track-Session", shareState.trackSessionId);
  }
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const url = path.startsWith("http") ? path : `${API}${path.startsWith("/") ? "" : "/"}${path}`;
  return fetch(url, { ...options, headers });
}

function trackPayload(extra = {}) {
  return {
    session_id: shareState.trackSessionId,
    started_at: shareState.visitStartedAt,
    ...extra,
  };
}

async function reportTrack(action, extra = {}) {
  const body = JSON.stringify(trackPayload({ action, ...extra }));
  try {
    if (action === "SESSION_END" && typeof navigator.sendBeacon === "function") {
      const blob = new Blob([body], { type: "application/json" });
      const ok = navigator.sendBeacon(`${API}/share/${shareToken}/track`, blob);
      if (ok) return;
    }
    await shareFetch(`/share/${shareToken}/track`, {
      method: "POST",
      body,
      keepalive: action === "SESSION_END",
    });
  } catch {
    /* 埋点失败不影响观看 */
  }
}

function videoProgressStorageKey() {
  return `share_video_progress:${shareToken}`;
}

function loadSavedVideoProgress() {
  try {
    const raw = localStorage.getItem(videoProgressStorageKey());
    if (!raw) return null;
    const data = JSON.parse(raw);
    const sec = Number(data.sec);
    const pct = Number(data.pct);
    if (!Number.isFinite(sec) || sec < 5) return null;
    if (Number.isFinite(pct) && pct >= 95) return null;
    return { sec, pct: Number.isFinite(pct) ? pct : null };
  } catch {
    return null;
  }
}

function saveVideoProgress(sec, pct) {
  try {
    localStorage.setItem(
      videoProgressStorageKey(),
      JSON.stringify({
        sec: Math.max(0, Math.round(Number(sec) || 0)),
        pct: Math.max(0, Math.min(100, Math.round(Number(pct) || 0))),
        updatedAt: Date.now(),
      })
    );
  } catch {
    /* 私密模式等忽略 */
  }
}

function clearSavedVideoProgress() {
  try {
    localStorage.removeItem(videoProgressStorageKey());
  } catch {
    /* ignore */
  }
}

function showResumeHint(sec) {
  const el = $("#resume-hint");
  if (!el) return;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  const label = `${m}:${String(s).padStart(2, "0")}`;
  el.innerHTML = `已从上次进度 <strong>${escapeHtml(label)}</strong> 继续
    <button type="button" class="btn btn-sm" data-resume-restart>${btnContent("skip-back-mini-line", "从头播放")}</button>`;
  el.classList.remove("hidden");
  el.querySelector("[data-resume-restart]")?.addEventListener("click", () => {
    const media = shareState.mediaElement;
    if (media) media.currentTime = 0;
    clearSavedVideoProgress();
    el.classList.add("hidden");
  });
}

function bindVideoTracking(video) {
  if (!video || video.dataset.trackBound === "1") return;
  video.dataset.trackBound = "1";
  shareState.mediaElement = video;

  const tryResume = () => {
    if (video.dataset.resumed === "1") return;
    const saved = loadSavedVideoProgress();
    if (!saved) return;
    const duration = Number(video.duration);
    if (!Number.isFinite(duration) || duration <= 0) return;
    const target = Math.min(saved.sec, Math.max(0, duration - 2));
    if (target < 5) return;
    video.dataset.resumed = "1";
    video.currentTime = target;
    shareState.videoProgressPeak = Math.max(
      shareState.videoProgressPeak,
      saved.pct || Math.round((target / duration) * 100)
    );
    showResumeHint(target);
  };
  video.addEventListener("loadedmetadata", tryResume);
  if (video.readyState >= 1) tryResume();

  const report = (force = false) => {
    const duration = Number(video.duration);
    const current = Number(video.currentTime);
    if (!Number.isFinite(duration) || duration <= 0 || !Number.isFinite(current)) return;
    const pct = Math.max(0, Math.min(100, Math.round((current / duration) * 100)));
    if (pct > shareState.videoProgressPeak) shareState.videoProgressPeak = pct;
    const now = Date.now();
    const crossedBucket =
      Math.floor(pct / 10) > Math.floor(shareState.lastPlayReportPct / 10);
    const timed = now - shareState.lastPlayReportAt >= 30_000;
    if (!force && !crossedBucket && !timed) return;
    shareState.lastPlayReportAt = now;
    shareState.lastPlayReportPct = pct;
    saveVideoProgress(current, pct);
    reportTrack("PLAY_VIDEO", {
      video_progress_pct: shareState.videoProgressPeak,
      detail: {
        current_sec: Math.round(current),
        duration_sec: Math.round(duration),
      },
    });
  };
  video.addEventListener("timeupdate", () => report(false));
  video.addEventListener("pause", () => report(true));
  video.addEventListener("ended", () => {
    shareState.videoProgressPeak = 100;
    clearSavedVideoProgress();
    report(true);
  });
}

function endVisitSession() {
  if (shareState.sessionEnded) return;
  shareState.sessionEnded = true;
  const endedAt = Date.now();
  reportTrack("SESSION_END", {
    ended_at: endedAt,
    dwell_ms: Math.max(0, endedAt - shareState.visitStartedAt),
    video_progress_pct: shareState.videoProgressPeak || null,
  });
}

function withShareUrl(url) {
  if (!url) return url;
  // R2/S3 预签名 URL 多拼任何 query 都会验签失败 → 403
  if (/^https?:\/\//i.test(url)) {
    try {
      const target = new URL(url);
      const apiHost = new URL(API).host;
      if (target.host !== apiHost) return url;
    } catch {
      return url;
    }
  }
  return withShareSessionQuery(url, shareState.sessionId);
}

async function downloadShare(path, filenameHint) {
  const res = await shareFetch(path);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(typeof err.detail === "string" ? err.detail : res.statusText);
  }
  const blob = await res.blob();
  const disp = res.headers.get("Content-Disposition") || "";
  const match = /filename="?([^"]+)"?/i.exec(disp);
  const filename = match?.[1] || filenameHint || "download";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function parseTimestamp(value) {
  const parts = value.trim().split(":");
  if (parts.length === 3) {
    return Number(parts[0]) * 3600 + Number(parts[1]) * 60 + Number(parts[2]);
  }
  if (parts.length === 2) return Number(parts[0]) * 60 + Number(parts[1]);
  return 0;
}

function formatTimestamp(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00:00";
  const totalMs = Math.floor(seconds * 1000);
  const h = Math.floor(totalMs / 3600000);
  const m = Math.floor((totalMs % 3600000) / 60000);
  const s = Math.floor((totalMs % 60000) / 1000);
  if (h > 0) {
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function parseTranscript(text) {
  const lines = text.split(/\r?\n/);
  const headerLines = [];
  let index = 0;
  while (index < lines.length) {
    if (SPEAKER_LINE_RE.test(lines[index])) break;
    headerLines.push(lines[index]);
    index += 1;
  }
  const segments = [];
  while (index < lines.length) {
    const match = lines[index].match(SPEAKER_LINE_RE);
    if (!match) {
      index += 1;
      continue;
    }
    const speaker = match[1].trim();
    const timeSec = parseTimestamp(match[2]);
    index += 1;
    const contentLines = [];
    while (index < lines.length && !SPEAKER_LINE_RE.test(lines[index])) {
      contentLines.push(lines[index]);
      index += 1;
    }
    segments.push({ speaker, timeSec, text: contentLines.join("\n").trim() });
  }
  return { header: headerLines.join("\n").trim(), segments };
}

function renderTranscript(parsed) {
  const container = $("#transcript-scroll");
  container.innerHTML = "";
  if (parsed.header) {
    container.insertAdjacentHTML(
      "beforeend",
      `<div class="transcript-preamble">${escapeHtml(parsed.header)}</div>`
    );
  }
  parsed.segments.forEach((segment, idx) => {
    container.insertAdjacentHTML(
      "beforeend",
      `<article class="transcript-segment" data-index="${idx}" data-time="${segment.timeSec}">
        <header class="segment-meta">${escapeHtml(segment.speaker)} · ${formatTimestamp(segment.timeSec)}</header>
        <p class="segment-text">${escapeHtml(segment.text)}</p>
      </article>`
    );
  });
  container.querySelectorAll(".transcript-segment").forEach((el) => {
    el.addEventListener("click", () => {
      const time = Number(el.dataset.time);
      if (!shareState.mediaElement || !Number.isFinite(time)) return;
      shareState.mediaElement.currentTime = time;
    });
  });
}

function setupDetailSync(mediaElement, transcriptText) {
  const parsed = parseTranscript(transcriptText);
  shareState.fullTranscriptText = transcriptText;
  shareState.segments = parsed.segments;
  shareState.mediaElement = mediaElement;
  renderTranscript(parsed);
  $("#sync-toolbar").classList.remove("hidden");
}

function resolveAssetUrl(path) {
  const raw = String(path || "").trim();
  if (!raw || /^[a-z][a-z0-9+.-]*:/i.test(raw) || raw.startsWith("//")) {
    return null;
  }
  const name = raw.replace(/^\.?\/*/, "").replace(/^assets\//, "");
  if (!name || name.includes("/") || name.includes("\\") || name.includes("..")) {
    return null;
  }
  if (!/^fig-[\w.-]+\.(jpe?g|png|webp|gif)$/i.test(name)) {
    return null;
  }
  return withShareUrl(
    `${API}/share/${shareToken}/summary/assets/${encodeURIComponent(name)}`
  );
}

function renderMarkdown(markdown) {
  return renderSafeMarkdown(markdown, {
    resolveAssetUrl,
    isTimeAnchor: (code) => TIME_ANCHOR_RE.test(code),
    timeAnchorSeconds: (code) =>
      TIME_ANCHOR_RE.test(code) ? parseTimestamp(code) : null,
  });
}

function bindTimeAnchors() {
  $("#summary-content")
    .querySelectorAll(".time-anchor")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        const seconds = Number(btn.dataset.sec);
        const media = shareState.mediaElement;
        if (!media || !Number.isFinite(seconds)) return;
        media.currentTime = seconds;
        media.play?.().catch(() => {});
      });
    });
}

function switchDetailTab(tab) {
  shareState.activeTab = tab;
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.tab === tab);
  });
  $("#summary-pane").classList.toggle("hidden", tab !== "summary");
  $("#transcript-pane").classList.toggle("hidden", tab !== "transcript");
}

function applyExportVisibility() {
  $("#summary-export")?.classList.toggle("hidden", !shareState.allowExport);
  $("#transcript-export")?.classList.toggle("hidden", !shareState.allowExport);
}

function showShareSkeleton() {
  $("#unlock-view")?.classList.add("hidden");
  $("#detail-view")?.classList.remove("hidden");
  $("#detail-empty")?.classList.add("hidden");
  $("#resume-hint")?.classList.add("hidden");
  $("#media-section")?.classList.remove("hidden");
  $("#media-players").innerHTML = `
    <div class="skeleton-block skeleton-media" aria-hidden="true"></div>`;
  $("#detail-tabs")?.classList.remove("hidden");
  $("#summary-pane")?.classList.remove("hidden");
  $("#transcript-pane")?.classList.add("hidden");
  $("#summary-empty")?.classList.add("hidden");
  $("#summary-content").innerHTML = `
    <div class="skeleton-lines" aria-hidden="true">
      <div class="skeleton-line w90"></div>
      <div class="skeleton-line w70"></div>
      <div class="skeleton-line w85"></div>
      <div class="skeleton-line w60"></div>
      <div class="skeleton-line w80"></div>
    </div>`;
  $("#summary-export")?.classList.add("hidden");
}

async function openDetail({ allowKeyRetry = true } = {}) {
  showShareSkeleton();
  if (!$("#detail-title")?.textContent?.trim()) {
    $("#detail-title").innerHTML = thinkingHtml();
  }
  $("#detail-meta").textContent = "正在加载…";

  // detail / summary 并行，缩短首屏等待
  const detailPromise = shareFetch(`/share/${shareToken}/detail`);
  const summaryPromise = shareFetch(`/share/${shareToken}/summary`);

  const res = await detailPromise;
  if (res.status === 403) {
    saveSession("");
    // 后端重启后内存会话失效：用本机密钥列表重解
    if (allowKeyRetry && (await tryUnlockWithSavedKeys())) return;
    showUnlock();
    return;
  }
  if (!res.ok) {
    $("#detail-view").classList.remove("hidden");
    $("#detail-title").textContent = "加载失败";
    $("#detail-meta").textContent = "";
    $("#media-section")?.classList.add("hidden");
    $("#detail-tabs")?.classList.add("hidden");
    $("#summary-pane")?.classList.add("hidden");
    $("#detail-empty").textContent = "分享内容不可用";
    $("#detail-empty").classList.remove("hidden");
    return;
  }
  const data = await res.json();
  $("#unlock-view").classList.add("hidden");
  $("#detail-view").classList.remove("hidden");
  $("#detail-empty")?.classList.add("hidden");
  $("#detail-title").textContent = data.title || shareToken;
  document.title = `${data.title || "分享"} · 飞书妙记`;

  const metaParts = [];
  if (data.duration_ms) metaParts.push(`时长 ${formatDuration(data.duration_ms)}`);
  $("#detail-meta").textContent = metaParts.join(" · ") || "";

  let hasContent = false;
  let mediaElement = null;
  const mediaEl = $("#media-players");
  mediaEl.innerHTML = "";
  (data.media_files || []).forEach((mf) => {
    hasContent = true;
    const label = escapeHtml(mf.name);
    const mediaUrl = withShareUrl(mf.url);
    if (mf.kind === "video") {
      mediaEl.insertAdjacentHTML(
        "beforeend",
        `<div class="media-card"><p class="media-name">${label}</p><video controls preload="metadata" src="${mediaUrl}"></video></div>`
      );
      if (!mediaElement) {
        mediaElement = mediaEl.querySelector("video");
        if (mediaElement) bindVideoTracking(mediaElement);
      }
    } else if (mf.kind === "audio") {
      mediaEl.insertAdjacentHTML(
        "beforeend",
        `<div class="media-card"><p class="media-name">${label}</p><audio controls preload="metadata" src="${mediaUrl}"></audio></div>`
      );
      if (!mediaElement) {
        mediaElement = mediaEl.querySelector("audio");
        shareState.mediaElement = mediaElement;
      }
    }
  });
  if (hasContent) $("#media-section").classList.remove("hidden");
  else {
    $("#media-section")?.classList.add("hidden");
    mediaEl.innerHTML = "";
  }

  const hasTranscript = Boolean(data.has_transcript);
  if (hasTranscript) {
    shareState.fullTranscriptText = "";
    $("#transcript-scroll").innerHTML = `
      <div class="skeleton-lines" aria-hidden="true">
        <div class="skeleton-line w80"></div>
        <div class="skeleton-line w95"></div>
        <div class="skeleton-line w70"></div>
        <div class="skeleton-line w88"></div>
      </div>`;
    $("#transcript-section").classList.remove("hidden");
    hasContent = true;
  }

  // 纪要正文路与转写路并行（路内仍：签 URL → R2）
  const summaryLoad = (async () => {
    try {
      const sumRes = await summaryPromise;
      if (!sumRes.ok) {
        $("#summary-content").innerHTML = "";
        $("#summary-empty").classList.remove("hidden");
        return false;
      }
      const sum = await sumRes.json();
      shareState.summaryMarkdown = await resolveTextPayload(sum, {
        kind: "summary",
        inlineFetcher: shareFetch,
        inlinePath: `/share/${shareToken}/summary`,
      });
      const ok = Boolean(shareState.summaryMarkdown);
      $("#summary-content").innerHTML = ok
        ? renderMarkdown(shareState.summaryMarkdown)
        : "";
      $("#summary-empty").classList.toggle("hidden", ok);
      if (ok) bindTimeAnchors();
      return ok;
    } catch {
      $("#summary-content").innerHTML = "";
      $("#summary-empty").classList.remove("hidden");
      return false;
    }
  })();

  const transcriptLoad = hasTranscript
    ? (async () => {
        try {
          const tRes = await shareFetch(`/share/${shareToken}/transcript`);
          if (!tRes.ok) {
            $("#transcript-scroll").innerHTML = `<p class="muted">转写加载失败</p>`;
            return;
          }
          const tData = await tRes.json();
          const text = await resolveTextPayload(tData, {
            kind: "transcript",
            inlineFetcher: shareFetch,
            inlinePath: `/share/${shareToken}/transcript`,
          });
          shareState.fullTranscriptText = text;
          if (text) {
            if (mediaElement) setupDetailSync(mediaElement, text);
            else renderTranscript(parseTranscript(text));
          } else {
            $("#transcript-scroll").innerHTML = `<p class="muted">暂无转写</p>`;
          }
        } catch {
          $("#transcript-scroll").innerHTML = `<p class="muted">转写加载失败</p>`;
        }
      })()
    : Promise.resolve();

  const [hasSummary] = await Promise.all([summaryLoad, transcriptLoad]);

  document
    .querySelector('.tab-btn[data-tab="transcript"]')
    .classList.toggle("hidden", !hasTranscript);
  if (hasTranscript || hasSummary) {
    $("#detail-tabs").classList.remove("hidden");
    switchDetailTab(hasSummary || !hasTranscript ? "summary" : "transcript");
    hasContent = true;
  } else {
    $("#detail-tabs")?.classList.add("hidden");
    $("#summary-pane")?.classList.add("hidden");
  }
  if (!hasContent) $("#detail-empty").classList.remove("hidden");
  applyExportVisibility();
}

function showUnlock(title) {
  $("#detail-view").classList.add("hidden");
  $("#unlock-view").classList.remove("hidden");
  if (title) $("#unlock-title").textContent = title;
}

function libraryItemTimeMs(item) {
  // 优先妙记生成时间；兼容旧字段 downloaded_at
  const raw = item?.create_time || item?.downloaded_at;
  if (!raw) return 0;
  const n = Number(raw);
  if (Number.isFinite(n) && n > 1e11) return n > 1e12 ? n : n * 1000;
  const ms = Date.parse(String(raw));
  return Number.isFinite(ms) ? ms : 0;
}

function libraryKeyItems() {
  // 左侧只展示「密钥命中」的分享；最新下载最前
  return (shareState.libraryItems || [])
    .filter((item) => item.source === "KEY")
    .slice()
    .sort((a, b) => {
      const dt = libraryItemTimeMs(b) - libraryItemTimeMs(a);
      if (dt !== 0) return dt;
      return String(a.title || "").localeCompare(String(b.title || ""), "zh-CN");
    });
}

function filteredLibraryItems() {
  const q = ($("#share-library-filter")?.value || "").trim().toLowerCase();
  const items = libraryKeyItems();
  if (!q) return items;
  return items.filter((item) => {
    const hay = `${item.title || ""} ${item.minute_token || ""}`.toLowerCase();
    return hay.includes(q);
  });
}

function renderShareLibrary() {
  const list = $("#share-library-list");
  const status = $("#share-library-status");
  if (!list || !status) return;

  const localKeys = loadAccessKeyList(shareToken);
  if (!localKeys.length) {
    shareState.libraryItems = [];
    status.textContent = "尚未记住密钥";
    list.innerHTML = `<li class="share-library-empty">解锁并勾选「记住密钥」后，这里会列出该密钥可访问的全部课程。</li>`;
    return;
  }

  const items = filteredLibraryItems();
  const total = libraryKeyItems().length;
  status.textContent =
    total === 0
      ? "暂无可访问课程"
      : items.length === total
        ? `共 ${total} 门课`
        : `筛选 ${items.length} / ${total}`;

  if (!items.length) {
    list.innerHTML = `<li class="share-library-empty">${
      total === 0
        ? "当前密钥尚未关联可访问分享，或密钥已失效。"
        : "没有匹配的课程。"
    }</li>`;
    return;
  }

  list.innerHTML = items
    .map((item) => {
      const active = item.share_token === shareToken ? " is-active" : "";
      const parts = [];
      const duration = formatDuration(item.duration_ms);
      if (duration) parts.push(`时长 ${duration}`);
      const when = formatTime(item.create_time || item.downloaded_at);
      if (when) parts.push(when);
      const metaLine = parts.length ? parts.join(" · ") : "暂无时间信息";
      return `
      <li>
        <a class="share-library-item${active}" href="${escapeHtml(item.url)}" title="${escapeHtml(item.title || "")}">
          <p class="share-library-item-title">${escapeHtml(item.title || "未命名课程")}</p>
          <p class="share-library-item-meta">${escapeHtml(metaLine)}</p>
        </a>
      </li>`;
    })
    .join("");
}

async function loadShareLibrary() {
  const status = $("#share-library-status");
  const list = $("#share-library-list");
  if (!status || !list) return;

  const keys = loadAccessKeyList(shareToken);
  if (!keys.length) {
    renderShareLibrary();
    return;
  }

  status.textContent = "加载可访问课程…";
  try {
    const res = await fetch(`${API}/share/library`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys, share_tokens: [] }),
    });
    if (!res.ok) {
      status.textContent = "列表加载失败";
      list.innerHTML = `<li class="share-library-empty">暂时无法拉取密钥课程列表，请稍后刷新。</li>`;
      return;
    }
    const data = await res.json();
    shareState.libraryItems = data.items || [];
    shareState.libraryKeys = data.keys || [];
    renderShareLibrary();
  } catch {
    status.textContent = "列表加载失败";
    list.innerHTML = `<li class="share-library-empty">网络异常，无法加载课程列表。</li>`;
  }
}

async function unlockWithKey(key, { silent = false, rememberOnSuccess = true } = {}) {
  const err = $("#unlock-error");
  if (!silent) err?.classList.add("hidden");
  const value = (key || "").trim();
  if (!value) {
    if (!silent && err) {
      err.textContent = "请输入密钥";
      err.classList.remove("hidden");
    }
    return false;
  }
  const res = await shareFetch(`/share/${shareToken}/unlock`, {
    method: "POST",
    body: JSON.stringify({ key: value }),
  });
  if (!res.ok) {
    if (!silent && err) {
      const data = await res.json().catch(() => ({}));
      err.textContent = typeof data.detail === "string" ? data.detail : "解锁失败";
      err.classList.remove("hidden");
    }
    // 静默试列表时失败不删密钥：可能只是不匹配当前分享，对别的分享仍有效
    return false;
  }
  const data = await res.json();
  const remember =
    rememberOnSuccess && $("#share-remember-key")?.checked !== false;
  if (remember) rememberAccessKey(value, shareToken);
  saveSession(data.share_session);
  shareState.allowExport = Boolean(data.allow_export);
  shareState.minuteToken = data.minute_token;
  // 记住密钥后立刻刷新左侧权限列表
  if (remember) loadShareLibrary();
  await openDetail({ allowKeyRetry: false });
  return !$("#detail-view")?.classList.contains("hidden");
}

async function unlock() {
  await unlockWithKey($("#share-key-input")?.value || "", {
    silent: false,
    rememberOnSuccess: true,
  });
}

async function boot() {
  // 左侧列表与正文并行：有本机密钥时尽早展示权限范围内的课程
  const libraryPromise = loadShareLibrary();

  const metaRes = await shareFetch(`/share/${shareToken}/meta`);
  if (!metaRes.ok) {
    document.body.innerHTML = `<main class="view"><p class="empty">分享不存在或已失效</p></main>`;
    return;
  }
  const meta = await metaRes.json();
  shareState.allowExport = Boolean(meta.allow_export);
  shareState.minuteToken = meta.minute_token;
  document.title = `${meta.title || "分享"} · 飞书妙记`;
  // 尽早露出标题，后续详情/纪要并行加载
  const titleEl = $("#detail-title");
  if (titleEl) titleEl.textContent = meta.title || shareToken;

  if (meta.requires_key) {
    const savedSession = loadSavedSession();
    if (savedSession) {
      saveSession(savedSession);
      await openDetail({ allowKeyRetry: true });
      if (!$("#detail-view").classList.contains("hidden")) {
        await libraryPromise;
        return;
      }
    }
    if (await tryUnlockWithSavedKeys()) {
      await libraryPromise;
      // try-keys 成功时可能刚确认密钥可用，再刷一次列表
      loadShareLibrary();
      return;
    }
    showUnlock(meta.title);
    await libraryPromise;
    return;
  }
  saveSession("");
  await Promise.all([openDetail({ allowKeyRetry: false }), libraryPromise]);
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchDetailTab(btn.dataset.tab));
});
$("#unlock-btn")?.addEventListener("click", unlock);
$("#share-key-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") unlock();
});
$("#share-library-refresh")?.addEventListener("click", () => loadShareLibrary());
$("#share-library-filter")?.addEventListener("input", renderShareLibrary);
document.querySelectorAll("[data-export-summary]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    try {
      await downloadShare(
        `/share/${shareToken}/export/summary?format=${btn.dataset.exportSummary}`,
        `summary.${btn.dataset.exportSummary}`
      );
    } catch (e) {
      alert(e.message);
    }
  });
});
document.querySelectorAll("[data-export-transcript]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    try {
      await downloadShare(
        `/share/${shareToken}/export/transcript?format=${btn.dataset.exportTranscript}`,
        `transcript.${btn.dataset.exportTranscript}`
      );
    } catch (e) {
      alert(e.message);
    }
  });
});
function updateShareScrollModeButton() {
  const btn = $("#scroll-mode-btn");
  if (!btn) return;
  if (shareState.scrollMode === "follow") {
    setBtnContent(btn, "focus-3-line", "跟随模式");
  } else {
    setBtnContent(btn, "cursor-line", "自由模式");
  }
}

$("#scroll-mode-btn")?.addEventListener("click", () => {
  shareState.scrollMode = shareState.scrollMode === "follow" ? "free" : "follow";
  localStorage.setItem("minutes_scroll_mode", shareState.scrollMode);
  updateShareScrollModeButton();
});
updateShareScrollModeButton();

window.addEventListener("pagehide", endVisitSession);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") endVisitSession();
});

if (typeof initPassageAsk === "function") {
  initPassageAsk({
    storageKey: `ask:share:${shareToken}`,
    roots: [
      {
        el: "#summary-content",
        kind: "SUMMARY",
        getText: () => shareState.summaryMarkdown || "",
      },
      {
        el: "#transcript-scroll",
        kind: "TRANSCRIPT",
        getText: () => shareState.fullTranscriptText || "",
      },
    ],
    askStream: async (body) =>
      shareFetch(`/share/${shareToken}/ask/stream`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
  });
}

boot();
