const params = new URLSearchParams(location.search);
const shareToken = (params.get("s") || "").trim();

const shareState = {
  sessionId: "",
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
};

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
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const url = path.startsWith("http") ? path : `${API}${path.startsWith("/") ? "" : "/"}${path}`;
  return fetch(url, { ...options, headers });
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

async function openDetail({ allowKeyRetry = true } = {}) {
  $("#unlock-view")?.classList.add("hidden");
  $("#detail-view")?.classList.remove("hidden");
  $("#detail-title").innerHTML = thinkingHtml();
  $("#detail-meta").textContent = "";
  showThinking($("#detail-empty"));
  $("#detail-empty")?.classList.remove("hidden");
  $("#media-section")?.classList.add("hidden");
  $("#detail-tabs")?.classList.add("hidden");
  $("#summary-pane")?.classList.add("hidden");
  $("#transcript-pane")?.classList.add("hidden");

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
  $("#detail-meta").textContent = metaParts.join(" · ");

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
      if (!mediaElement) mediaElement = mediaEl.querySelector("video");
    } else if (mf.kind === "audio") {
      mediaEl.insertAdjacentHTML(
        "beforeend",
        `<div class="media-card"><p class="media-name">${label}</p><audio controls preload="metadata" src="${mediaUrl}"></audio></div>`
      );
      if (!mediaElement) mediaElement = mediaEl.querySelector("audio");
    }
  });
  if (hasContent) $("#media-section").classList.remove("hidden");

  const hasTranscript = Boolean(data.has_transcript);
  if (hasTranscript) {
    shareState.fullTranscriptText = "";
    $("#transcript-scroll").innerHTML = thinkingHtml({ block: true });
    $("#transcript-section").classList.remove("hidden");
    hasContent = true;
  }

  let hasSummary = false;
  try {
    const sumRes = await summaryPromise;
    if (sumRes.ok) {
      const sum = await sumRes.json();
      shareState.summaryMarkdown = sum.content || "";
      hasSummary = Boolean(shareState.summaryMarkdown);
      $("#summary-content").innerHTML = hasSummary
        ? renderMarkdown(shareState.summaryMarkdown)
        : "";
      $("#summary-empty").classList.toggle("hidden", hasSummary);
      if (hasSummary) bindTimeAnchors();
    } else {
      $("#summary-empty").classList.remove("hidden");
    }
  } catch {
    $("#summary-empty").classList.remove("hidden");
  }

  document
    .querySelector('.tab-btn[data-tab="transcript"]')
    .classList.toggle("hidden", !hasTranscript);
  if (hasTranscript || hasSummary) {
    $("#detail-tabs").classList.remove("hidden");
    switchDetailTab(hasSummary || !hasTranscript ? "summary" : "transcript");
    hasContent = true;
  }
  if (!hasContent) $("#detail-empty").classList.remove("hidden");
  applyExportVisibility();

  if (hasTranscript) {
    try {
      const tRes = await shareFetch(`/share/${shareToken}/transcript`);
      if (tRes.ok) {
        const tData = await tRes.json();
        const text = tData.transcript || "";
        shareState.fullTranscriptText = text;
        if (text) {
          if (mediaElement) setupDetailSync(mediaElement, text);
          else renderTranscript(parseTranscript(text));
        } else {
          $("#transcript-scroll").innerHTML = `<p class="muted">暂无转写</p>`;
        }
      } else {
        $("#transcript-scroll").innerHTML = `<p class="muted">转写加载失败</p>`;
      }
    } catch {
      $("#transcript-scroll").innerHTML = `<p class="muted">转写加载失败</p>`;
    }
  }
}

function showUnlock(title) {
  $("#detail-view").classList.add("hidden");
  $("#unlock-view").classList.remove("hidden");
  if (title) $("#unlock-title").textContent = title;
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
  const metaRes = await fetch(`${API}/share/${shareToken}/meta`);
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
      if (!$("#detail-view").classList.contains("hidden")) return;
    }
    if (await tryUnlockWithSavedKeys()) return;
    showUnlock(meta.title);
    return;
  }
  saveSession("");
  await openDetail({ allowKeyRetry: false });
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchDetailTab(btn.dataset.tab));
});
$("#unlock-btn")?.addEventListener("click", unlock);
$("#share-key-input")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") unlock();
});
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

boot();
