if (!requireAdminPage()) throw new Error("redirecting to login");

const detailState = {
  fullTranscriptText: "",
  segments: [],
  mediaElement: null,
  mediaHandlers: null,
  activeSegmentIndex: -1,
  token: "",
  summaryMarkdown: "",
  summaryStream: null,
  streamBuffer: "",
  streamRenderScheduled: false,
  summaryStatus: "",
  lastStatus: null,
  stageStartedAt: 0,
  progressTicker: null,
  planCount: 0,
  activeTab: "summary",
  scrollMode: localStorage.getItem("minutes_scroll_mode") === "free" ? "free" : "follow",
};

const SPEAKER_LINE_RE = /^(.+?)\s+(\d{1,2}:\d{2}:\d{2}(?:\.\d{1,3})?)\s*$/;
const TIME_ANCHOR_RE = /^\d{1,2}:\d{2}:\d{2}$/;
const IMAGE_LINE_RE = /^!\[([^\]]*)\]\(\s*([^)\s]+)\s*\)$/;

function parseTimestamp(value) {
  const parts = value.trim().split(":");
  if (parts.length === 3) {
    return Number(parts[0]) * 3600 + Number(parts[1]) * 60 + Number(parts[2]);
  }
  if (parts.length === 2) {
    return Number(parts[0]) * 60 + Number(parts[1]);
  }
  return 0;
}

function formatTimestamp(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "00:00:00";
  const totalMs = Math.floor(seconds * 1000);
  const h = Math.floor(totalMs / 3600000);
  const m = Math.floor((totalMs % 3600000) / 60000);
  const s = Math.floor((totalMs % 60000) / 1000);
  const ms = totalMs % 1000;
  if (h > 0) {
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  if (ms > 0) {
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
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
    segments.push({
      speaker,
      timeSec,
      text: contentLines.join("\n").trim(),
    });
  }

  return {
    header: headerLines.join("\n").trim(),
    segments,
  };
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
      `
      <article class="transcript-segment" data-index="${idx}" data-time="${segment.timeSec}">
        <header class="segment-meta">${escapeHtml(segment.speaker)} · ${formatTimestamp(segment.timeSec)}</header>
        <p class="segment-text">${escapeHtml(segment.text)}</p>
      </article>
    `
    );
  });

  container.querySelectorAll(".transcript-segment").forEach((el) => {
    el.addEventListener("click", () => {
      const time = Number(el.dataset.time);
      if (!detailState.mediaElement || !Number.isFinite(time)) return;
      detailState.mediaElement.currentTime = time;
      if (detailState.scrollMode === "follow") {
        syncTranscriptToMedia({ force: true });
      }
    });
  });
}

function findSegmentIndex(currentTime) {
  const segments = detailState.segments;
  if (!segments.length) return -1;
  let active = 0;
  for (let i = 0; i < segments.length; i += 1) {
    if (segments[i].timeSec <= currentTime + 0.05) active = i;
    else break;
  }
  return active;
}

function syncTranscriptToMedia({ force = false } = {}) {
  if (detailState.scrollMode !== "follow" && !force) return;
  const media = detailState.mediaElement;
  const container = $("#transcript-scroll");
  if (!media || !container || !detailState.segments.length) return;

  const index = findSegmentIndex(media.currentTime);
  if (index < 0) return;

  const segments = container.querySelectorAll(".transcript-segment");
  segments.forEach((el, i) => {
    el.classList.toggle("is-active", i === index);
  });

  if (index !== detailState.activeSegmentIndex || force) {
    detailState.activeSegmentIndex = index;
    const activeEl = segments[index];
    if (activeEl) {
      activeEl.scrollIntoView({ block: "center", behavior: force ? "auto" : "smooth" });
    }
  }
}

function updateScrollModeButton() {
  const btn = $("#scroll-mode-btn");
  const hint = $("#sync-hint");
  if (!btn) return;
  if (detailState.scrollMode === "follow") {
    setBtnContent(btn, "focus-3-line", "跟随模式");
    btn.classList.add("is-follow");
    if (hint) hint.textContent = "播放或拖动进度条时，转写自动滚动到对应位置";
  } else {
    setBtnContent(btn, "cursor-line", "自由模式");
    btn.classList.remove("is-follow");
    if (hint) hint.textContent = "转写可独立滚动；点击段落可跳转播放位置";
  }
}

function toggleScrollMode() {
  detailState.scrollMode = detailState.scrollMode === "follow" ? "free" : "follow";
  localStorage.setItem("minutes_scroll_mode", detailState.scrollMode);
  updateScrollModeButton();
  if (detailState.scrollMode === "follow") {
    syncTranscriptToMedia({ force: true });
  } else {
    $("#transcript-scroll")
      .querySelectorAll(".transcript-segment.is-active")
      .forEach((el) => el.classList.remove("is-active"));
    detailState.activeSegmentIndex = -1;
  }
}

function setupDetailSync(mediaElement, transcriptText) {
  teardownDetailSync();
  if (!mediaElement || !transcriptText) return;

  const parsed = parseTranscript(transcriptText);
  detailState.fullTranscriptText = transcriptText;
  detailState.segments = parsed.segments;
  detailState.mediaElement = mediaElement;

  renderTranscript(parsed);
  updateScrollModeButton();
  $("#sync-toolbar").classList.remove("hidden");

  const onTimeUpdate = () => syncTranscriptToMedia();
  const onSeeked = () => syncTranscriptToMedia({ force: true });
  const onSeeking = () => syncTranscriptToMedia({ force: true });
  const onPlay = () => syncTranscriptToMedia({ force: true });

  mediaElement.addEventListener("timeupdate", onTimeUpdate);
  mediaElement.addEventListener("seeked", onSeeked);
  mediaElement.addEventListener("seeking", onSeeking);
  mediaElement.addEventListener("play", onPlay);

  detailState.mediaHandlers = { onTimeUpdate, onSeeked, onSeeking, onPlay };
  syncTranscriptToMedia({ force: true });
}

function teardownDetailSync() {
  const media = detailState.mediaElement;
  const handlers = detailState.mediaHandlers;
  if (media && handlers) {
    media.removeEventListener("timeupdate", handlers.onTimeUpdate);
    media.removeEventListener("seeked", handlers.onSeeked);
    media.removeEventListener("seeking", handlers.onSeeking);
    media.removeEventListener("play", handlers.onPlay);
  }
  detailState.fullTranscriptText = "";
  detailState.segments = [];
  detailState.mediaElement = null;
  detailState.mediaHandlers = null;
  detailState.activeSegmentIndex = -1;
  $("#sync-toolbar")?.classList.add("hidden");
  const scroll = $("#transcript-scroll");
  if (scroll) scroll.innerHTML = "";
}

function anchorSeconds(text) {
  if (!TIME_ANCHOR_RE.test(text)) return null;
  return parseTimestamp(text);
}

function renderInlineMarkdown(text) {
  let html = escapeHtml(text);
  // 时间锚点渲染为可点击按钮，其余行内代码保持原样
  html = html.replace(/`([^`]+)`/g, (_match, code) => {
    const seconds = anchorSeconds(code);
    if (seconds !== null) {
      return `<button type="button" class="time-anchor" data-sec="${seconds}">${code}</button>`;
    }
    return `<code>${code}</code>`;
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return html;
}

// 纪要里的图片写成 assets/fig-01.jpg，需要转成后端的配图接口地址
function resolveAssetUrl(path) {
  if (/^https?:\/\//i.test(path)) return path;
  const name = path.replace(/^\.?\/*/, "").replace(/^assets\//, "");
  if (!name || name.includes("/")) return null;
  if (!detailState.token) return null;
  return withAccessTicketQuery(
    `${API}/meetings/${detailState.token}/summary/assets/${encodeURIComponent(name)}`
  );
}

function renderMarkdown(markdown) {
  const lines = markdown.split(/\r?\n/);
  const out = [];
  let listTag = null;
  let inCodeBlock = false;
  let awaitingCaption = false;
  const codeBuffer = [];

  const closeFigure = () => {
    if (awaitingCaption) {
      out.push("</figure>");
      awaitingCaption = false;
    }
  };

  const closeList = () => {
    if (listTag) {
      out.push(`</${listTag}>`);
      listTag = null;
    }
  };
  const openList = (tag) => {
    if (listTag !== tag) {
      closeList();
      out.push(`<${tag}>`);
      listTag = tag;
    }
  };

  lines.forEach((raw) => {
    const line = raw.trimEnd();

    if (awaitingCaption && !inCodeBlock) {
      // 图注必须紧跟在图片后面，否则这张图就单独成块
      if (line.startsWith("图：")) {
        out.push(`<figcaption>${renderInlineMarkdown(line.slice(2))}</figcaption></figure>`);
        awaitingCaption = false;
        return;
      }
      closeFigure();
    }

    if (line.trimStart().startsWith("```")) {
      if (inCodeBlock) {
        out.push(`<pre><code>${escapeHtml(codeBuffer.join("\n"))}</code></pre>`);
        codeBuffer.length = 0;
      } else {
        closeList();
      }
      inCodeBlock = !inCodeBlock;
      return;
    }
    if (inCodeBlock) {
      codeBuffer.push(raw);
      return;
    }

    if (!line.trim()) {
      closeList();
      return;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      return;
    }

    if (/^-{3,}$/.test(line)) {
      closeList();
      out.push("<hr />");
      return;
    }

    const image = line.match(IMAGE_LINE_RE);
    if (image) {
      const url = resolveAssetUrl(image[2]);
      if (url) {
        closeList();
        const alt = escapeHtml(image[1]);
        out.push(
          `<figure><a href="${url}" target="_blank" rel="noopener">` +
            `<img src="${url}" alt="${alt}" loading="lazy" /></a>`
        );
        awaitingCaption = true;
      }
      return;
    }

    if (line.startsWith("> ")) {
      closeList();
      out.push(`<blockquote>${renderInlineMarkdown(line.slice(2))}</blockquote>`);
      return;
    }

    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      openList("ul");
      out.push(`<li>${renderInlineMarkdown(bullet[1])}</li>`);
      return;
    }

    const ordered = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ordered) {
      openList("ol");
      out.push(`<li>${renderInlineMarkdown(ordered[1])}</li>`);
      return;
    }

    closeList();
    out.push(`<p>${renderInlineMarkdown(line)}</p>`);
  });

  if (inCodeBlock && codeBuffer.length) {
    out.push(`<pre><code>${escapeHtml(codeBuffer.join("\n"))}</code></pre>`);
  }
  closeFigure();
  closeList();
  return out.join("\n");
}

function bindTimeAnchors() {
  $("#summary-content")
    .querySelectorAll(".time-anchor")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        const seconds = Number(btn.dataset.sec);
        const media = detailState.mediaElement;
        if (!media || !Number.isFinite(seconds)) return;
        media.currentTime = seconds;
        media.play?.().catch(() => {});
        media.scrollIntoView({ block: "nearest", behavior: "smooth" });
      });
    });
}

function switchDetailTab(tab) {
  detailState.activeTab = tab;
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.tab === tab);
  });
  $("#summary-pane").classList.toggle("hidden", tab !== "summary");
  $("#transcript-pane").classList.toggle("hidden", tab !== "transcript");
}

function summaryMetaText(meta) {
  if (!meta) return "";
  const parts = [];
  if (meta.generated_at) parts.push(`生成于 ${formatTime(meta.generated_at)}`);
  if (meta.summary_chars) parts.push(`${meta.summary_chars} 字`);
  if (meta.anchor_total) {
    const aligned = meta.anchor_aligned ? `，校正 ${meta.anchor_aligned} 个` : "";
    parts.push(`${meta.anchor_total} 个时间锚点${aligned}`);
  }
  if (meta.figure_used) {
    const dropped = (meta.figure_planned || 0) - meta.figure_used;
    parts.push(`配图 ${meta.figure_used} 张${dropped > 0 ? `，弃用 ${dropped} 张` : ""}`);
  }
  if (meta.figure_redacted || meta.figure_abandoned) {
    parts.push(
      `脱敏 打码 ${meta.figure_redacted || 0} / 放弃 ${meta.figure_abandoned || 0}`
    );
  }
  if (meta.model) parts.push(meta.model);
  if (meta.elapsed_seconds) parts.push(`耗时 ${Math.round(meta.elapsed_seconds)}s`);
  if (meta.truncated) parts.push("⚠️ 输出被截断，结尾可能不完整");
  return parts.join(" · ");
}

const SUMMARY_STAGES = [
  {
    id: "queue",
    title: "排队等待",
    desc: "任务已提交，等待生成池空位",
  },
  {
    id: "parse",
    title: "解析转写",
    desc: "整理发言段落，为成文与锚点做准备",
  },
  {
    id: "figures",
    title: "准备配图",
    desc: "抽帧判断共享屏幕，规划并筛选截图",
  },
  {
    id: "redact",
    title: "敏感脱敏",
    desc: "扫描敏感信息、打码并复核",
  },
  {
    id: "write",
    title: "模型成文",
    desc: "读图与转写，按知识结构写纪要正文",
  },
  {
    id: "finalize",
    title: "收尾落盘",
    desc: "校正时间锚点、清理配图并写入本地",
  },
];

function resolveSummaryStageId(data) {
  if (!data) return "queue";
  if (data.status === "FAILED") return "finalize";
  if (data.status === "QUEUED" || data.status === "PENDING") return "queue";
  const stage = String(data.stage || "");
  const percent = Number(data.percent) || 0;
  if (/排队/.test(stage)) return "queue";
  if (/解析转写/.test(stage) || (percent > 0 && percent < 6)) return "parse";
  if (/敏感|打码|脱敏|扫描 .* 张截图/.test(stage) || (percent >= 32 && percent < 40)) {
    return "redact";
  }
  if (
    /探测视频|抽帧|共享屏幕|规划截图|筛选|挑出|看抽帧|抽取画面样本/.test(stage) ||
    (percent >= 6 && percent < 32)
  ) {
    return "figures";
  }
  if (
    /模型|读 .* 张截图|生成中|重试/.test(stage) ||
    (percent >= 40 && percent < 90) ||
    detailState.streamBuffer.length > 0
  ) {
    return "write";
  }
  if (/校正|清理|写入|落盘|同步/.test(stage) || percent >= 90) return "finalize";
  if (percent <= 0) return "queue";
  if (percent < 8) return "parse";
  if (percent < 32) return "figures";
  if (percent < 40) return "redact";
  if (percent < 90) return "write";
  return "finalize";
}

function renderSummaryStages(data) {
  const root = $("#summary-stage-steps");
  if (!root) return;
  const activeId = resolveSummaryStageId(data);
  const activeIndex = SUMMARY_STAGES.findIndex((s) => s.id === activeId);
  const failed = data?.status === "FAILED";

  root.innerHTML = SUMMARY_STAGES.map((stage, index) => {
    let stateClass = "is-pending";
    if (failed && index === activeIndex) stateClass = "is-failed";
    else if (index < activeIndex) stateClass = "is-done";
    else if (index === activeIndex) stateClass = "is-active";
    return `
      <li class="summary-stage ${stateClass}" data-stage="${stage.id}">
        <span class="summary-stage-index">阶段 ${index + 1}/${SUMMARY_STAGES.length}</span>
        <span class="summary-stage-title">${escapeHtml(stage.title)}</span>
        <span class="summary-stage-desc">${escapeHtml(stage.desc)}</span>
      </li>`;
  }).join("");
}

function showSummaryProgress(visible, { label = "", statusHint = null } = {}) {
  const root = $("#summary-progress");
  const labelEl = $("#summary-progress-label");
  if (!root) return;
  root.classList.toggle("hidden", !visible);
  if (visible) {
    renderSummaryStages(detailState.lastStatus || statusHint || { status: "QUEUED", percent: 0, stage: "排队中" });
  }
  if (labelEl) labelEl.textContent = label;
}

function renderMetrics(metrics) {
  const root = $("#summary-metrics");
  const empty = $("#sidebar-metrics-empty");
  if (!root) return;
  if (!metrics || !metrics.has_summary) {
    root.classList.add("hidden");
    root.innerHTML = "";
    if (empty) empty.classList.remove("hidden");
    return;
  }
  const tokens =
    metrics.total_tokens != null
      ? `${metrics.total_tokens} tok（入 ${metrics.input_tokens || 0} / 出 ${metrics.output_tokens || 0}）`
      : "—";
  const funnel = [
    metrics.figure_planned != null ? `备选 ${metrics.figure_planned}` : null,
    metrics.figure_for_writing != null ? `成文 ${metrics.figure_for_writing}` : null,
    metrics.figure_used != null ? `采用 ${metrics.figure_used}` : null,
  ]
    .filter(Boolean)
    .join(" → ");
  const redact = [
    metrics.figure_scanned != null ? `扫描 ${metrics.figure_scanned}` : null,
    metrics.figure_sensitive != null ? `敏感 ${metrics.figure_sensitive}` : null,
    metrics.figure_redacted != null ? `打码 ${metrics.figure_redacted}` : null,
    metrics.figure_abandoned != null ? `放弃 ${metrics.figure_abandoned}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  root.innerHTML = `
    <div class="metrics-grid metrics-grid-sidebar">
      <div><span class="metrics-label">Token</span><span>${escapeHtml(tokens)}</span></div>
      <div><span class="metrics-label">耗时</span><span>${metrics.elapsed_seconds != null ? `${Math.round(metrics.elapsed_seconds)}s` : "—"}</span></div>
      <div><span class="metrics-label">锚点</span><span>${metrics.anchor_total || 0}（校正 ${metrics.anchor_aligned || 0}，可疑 ${metrics.anchor_suspicious_count || 0}）</span></div>
      <div><span class="metrics-label">配图漏斗</span><span>${escapeHtml(funnel || "—")}</span></div>
      <div><span class="metrics-label">脱敏</span><span>${escapeHtml(redact || "—")}</span></div>
      <div><span class="metrics-label">截断</span><span>${metrics.truncated ? "是" : "否"}</span></div>
    </div>`;
  root.classList.remove("hidden");
  if (empty) empty.classList.add("hidden");
}

async function loadMetrics(token) {
  try {
    const res = await apiFetch(`/meetings/${token}/summary/metrics`);
    if (!res.ok) {
      renderMetrics(null);
      return;
    }
    renderMetrics(await res.json());
  } catch {
    renderMetrics(null);
  }
}

function renderRedactionPanel(audit) {
  const panel = $("#redaction-panel");
  const list = $("#redaction-list");
  const summary = $("#redaction-summary");
  const empty = $("#sidebar-redaction-empty");
  if (!panel || !list || !summary) return;
  if (!audit || !(audit.figures || []).length) {
    panel.classList.add("hidden");
    list.innerHTML = "";
    if (empty) empty.classList.remove("hidden");
    return;
  }
  if (empty) empty.classList.add("hidden");
  summary.textContent = `扫描 ${audit.scanned} · 敏感 ${audit.sensitive} · 打码 ${audit.redacted} · 放弃 ${audit.abandoned}`;
  const interesting = (audit.figures || []).filter(
    (f) => f.sensitive || f.status === "ABANDONED" || f.status === "MANUAL_APPROVED"
  );
  if (!interesting.length) {
    list.innerHTML = `<p class="redaction-empty">本场未检出敏感配图。</p>`;
    panel.classList.remove("hidden");
    return;
  }
  list.innerHTML = interesting
    .map((fig) => {
      const regionsText = (fig.regions || [])
        .map(
          (r) =>
            `${r.label || "区域"} (${Number(r.x1).toFixed(2)},${Number(r.y1).toFixed(2)})-(${Number(r.x2).toFixed(2)},${Number(r.y2).toFixed(2)})`
        )
        .join("；");
      const lastReason =
        fig.abandon_reason ||
        (fig.attempts && fig.attempts.length
          ? fig.attempts[fig.attempts.length - 1].reason
          : "");
      const token = detailState.token;
      const originalSrc = fig.original_relative
        ? withAccessTicketQuery(
            `${API}/meetings/${token}/redaction/originals/${encodeURIComponent(fig.figure_id)}.jpg`
          )
        : "";
      const assetSrc = fig.asset_relative
        ? withAccessTicketQuery(
            `${API}/meetings/${token}/summary/assets/${encodeURIComponent(fig.figure_id)}.jpg`
          )
        : "";
      return `
      <article class="redaction-card" data-figure-id="${escapeHtml(fig.figure_id)}">
        <header>
          <strong>${escapeHtml(fig.figure_id)}</strong>
          <span class="redaction-status status-${escapeHtml(String(fig.status).toLowerCase())}">${escapeHtml(fig.status)}</span>
        </header>
        <div class="redaction-images">
          ${originalSrc ? `<figure><img src="${originalSrc}" alt="原图" loading="lazy" /><figcaption>原图</figcaption></figure>` : "<p>无原图</p>"}
          ${assetSrc ? `<figure><img src="${assetSrc}" alt="打码图" loading="lazy" /><figcaption>打码/成文用图</figcaption></figure>` : "<p>无成文图</p>"}
        </div>
        <p class="redaction-regions">${escapeHtml(regionsText || "无坐标")}</p>
        ${lastReason ? `<p class="redaction-reason">${escapeHtml(lastReason)}</p>` : ""}
        <div class="redaction-actions">
          <button type="button" class="btn btn-sm" data-approve="${escapeHtml(fig.figure_id)}">按当前框放行</button>
          <button type="button" class="btn btn-sm" data-abandon="${escapeHtml(fig.figure_id)}">放弃此图</button>
        </div>
      </article>`;
    })
    .join("");
  panel.classList.remove("hidden");
}

async function loadRedactionAudit(token) {
  try {
    const res = await apiFetch(`/meetings/${token}/redaction/audit`);
    if (!res.ok) {
      renderRedactionPanel(null);
      return;
    }
    renderRedactionPanel(await res.json());
  } catch {
    renderRedactionPanel(null);
  }
}

function renderSummary(markdown, meta) {
  detailState.summaryMarkdown = markdown || "";
  const content = $("#summary-content");
  const hasSummary = Boolean(markdown);

  content.innerHTML = hasSummary ? renderMarkdown(markdown) : "";
  $("#summary-empty").classList.toggle("hidden", hasSummary);
  $("#copy-summary-btn").classList.toggle("hidden", !hasSummary);
  $("#summary-meta").textContent = hasSummary ? summaryMetaText(meta) : "";
  setBtnContent(
    $("#generate-summary-btn"),
    hasSummary ? "refresh-line" : "magic-line",
    hasSummary ? "重新生成" : "生成纪要"
  );
  if (hasSummary) bindTimeAnchors();
  if (detailState.token) {
    loadMetrics(detailState.token);
    loadRedactionAudit(detailState.token);
  }
}

async function loadSummary(token) {
  try {
    const res = await apiFetch(`/meetings/${token}/summary`);
    if (res.status === 404) {
      renderSummary("", null);
      return false;
    }
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    renderSummary(data.content, data.meta);
    return true;
  } catch {
    renderSummary("", null);
    return false;
  }
}

function closeSummaryStream() {
  if (detailState.summaryStream) {
    detailState.summaryStream.close();
    detailState.summaryStream = null;
  }
  detailState.streamBuffer = "";
  detailState.streamRenderScheduled = false;
  detailState.planCount = 0;
  stopSummaryTicker();
  const plan = document.getElementById("summary-plan");
  if (plan) {
    plan.innerHTML = "";
    plan.classList.add("hidden");
  }
}

function isViewportNearBottom() {
  return window.innerHeight + window.scrollY >= document.body.scrollHeight - 120;
}

function scheduleStreamRender() {
  // 增量到得很密，每片都全量渲染会卡；节流到 200ms 一次即可
  if (detailState.streamRenderScheduled) return;
  detailState.streamRenderScheduled = true;
  setTimeout(() => {
    detailState.streamRenderScheduled = false;
    const followBottom = isViewportNearBottom();
    $("#summary-content").innerHTML = renderMarkdown(detailState.streamBuffer);
    $("#summary-empty").classList.add("hidden");
    if (followBottom) window.scrollTo({ top: document.body.scrollHeight });
  }, 200);
}

function planItemHtml(item) {
  const expect = item.expect ? escapeHtml(item.expect) : "（未说明预期画面）";
  return `<li><span class="plan-time">${escapeHtml(item.timestamp || "")}</span>${expect}</li>`;
}

function renderPlanItems(items) {
  const list = $("#summary-plan");
  detailState.planCount = items.length;
  list.innerHTML = items.map(planItemHtml).join("");
  list.classList.toggle("hidden", items.length === 0);
}

function appendPlanItem(item) {
  const list = $("#summary-plan");
  list.insertAdjacentHTML("beforeend", planItemHtml(item));
  list.classList.remove("hidden");
  detailState.planCount = item.index || detailState.planCount + 1;
  paintSummaryProgress();
}

function describeSummaryStatus(data) {
  if (data.status === "QUEUED") {
    return data.queue_position > 0
      ? `排队中，前面还有 ${data.queue_position} 个任务`
      : "排队中，即将开始";
  }
  return data.stage || "生成中…";
}

function summaryProgressSuffix() {
  const chars = detailState.streamBuffer.length;
  if (chars > 0) return ` · 已生成 ${chars} 字`;
  if (detailState.planCount > 0) return ` · 已挑出 ${detailState.planCount} 处`;
  if (!detailState.stageStartedAt) return "";
  // 读图 prefill 期间一分多钟不吐字，没有秒数会让人以为卡死
  const seconds = Math.round((Date.now() - detailState.stageStartedAt) / 1000);
  return seconds >= 5 ? ` · 已等待 ${seconds}s` : "";
}

function paintSummaryProgress() {
  if (!detailState.lastStatus) return;
  showSummaryProgress(true, {
    label: `${describeSummaryStatus(detailState.lastStatus)}${summaryProgressSuffix()}`,
  });
}

function stopSummaryTicker() {
  if (detailState.progressTicker) {
    clearInterval(detailState.progressTicker);
    detailState.progressTicker = null;
  }
  // 置空避免定时器最后一拍把已经隐藏的进度条重新画出来
  detailState.lastStatus = null;
  detailState.stageStartedAt = 0;
}

function applySummaryStatus(data) {
  detailState.summaryStatus = data.status || "";
  const generating = data.status === "QUEUED" || data.status === "GENERATING";
  $("#generate-summary-btn").disabled = generating;
  if (!generating) {
    stopSummaryTicker();
    return;
  }

  if (data.stage !== detailState.lastStatus?.stage) {
    detailState.stageStartedAt = Date.now();
  }
  detailState.lastStatus = data;
  if (!detailState.progressTicker) {
    detailState.progressTicker = setInterval(paintSummaryProgress, 1000);
  }
  paintSummaryProgress();
}

function isSummaryInProgress() {
  return detailState.summaryStatus === "QUEUED" || detailState.summaryStatus === "GENERATING";
}

async function openSummaryStream(token) {
  closeSummaryStream();
  await ensureAccessTicket();
  const source = new EventSource(
    withAccessTicketQuery(`${API}/meetings/${token}/summary/stream`)
  );
  detailState.summaryStream = source;

  source.addEventListener("snapshot", (e) => {
    const data = JSON.parse(e.data);
    renderPlanItems(data.plan_items || []);
    if (data.buffer) {
      // 中途打开页面时，先把已经生成的部分补齐
      detailState.streamBuffer = data.buffer;
      scheduleStreamRender();
    }
    applySummaryStatus(data);
  });

  source.addEventListener("status", (e) => applySummaryStatus(JSON.parse(e.data)));

  source.addEventListener("plan", (e) => appendPlanItem(JSON.parse(e.data)));

  source.addEventListener("plan_reset", () => renderPlanItems([]));

  source.addEventListener("delta", (e) => {
    // 正文开始输出，规划清单已完成使命
    renderPlanItems([]);
    detailState.streamBuffer += JSON.parse(e.data).text;
    scheduleStreamRender();
  });

  source.addEventListener("reset", () => {
    detailState.streamBuffer = "";
    $("#summary-content").innerHTML = "";
  });

  source.addEventListener("done", (e) => {
    const data = JSON.parse(e.data);
    detailState.streamBuffer = "";
    renderPlanItems([]);
    stopSummaryTicker();
    showSummaryProgress(false);
    $("#generate-summary-btn").disabled = false;
    renderSummary(data.content, data.meta);
  });

  source.addEventListener("failed", (e) => {
    const data = JSON.parse(e.data);
    detailState.streamBuffer = "";
    renderPlanItems([]);
    stopSummaryTicker();
    detailState.lastStatus = { status: "FAILED", percent: 0, stage: data.message || "生成失败" };
    showSummaryProgress(true, {
      label: `生成失败：${data.message || "未知原因"}`,
      statusHint: detailState.lastStatus,
    });
    $("#generate-summary-btn").disabled = false;
  });
}

async function generateSummary() {
  const token = detailState.token;
  if (!token) return;

  const btn = $("#generate-summary-btn");
  const mode = ($("#summary-run-mode")?.value || "FULL").toUpperCase();
  const force = mode !== "FULL" || Boolean(detailState.summaryMarkdown);

  // 下载后可能已在后台自动生成，重复 POST 只会干扰状态展示
  if (isSummaryInProgress()) {
    showSummaryProgress(true, { label: "已在生成中，请稍候…" });
    return;
  }
  try {
    const prog = await apiFetch(`/meetings/${token}/summary/progress`);
    if (prog.ok) {
      const data = await prog.json();
      applySummaryStatus(data);
      if (data.status === "QUEUED" || data.status === "GENERATING") {
        openSummaryStream(token);
        return;
      }
    }
  } catch {
    /* 404 表示尚未排队，继续提交 */
  }

  btn.disabled = true;
  if (mode === "FULL" || mode === "WRITE") {
    $("#summary-content").innerHTML = "";
  }
  const modeLabel =
    {
      FULL: "完整生成",
      REDACT: "重新脱敏",
      WRITE: "重新成文",
      R2_SYNC: "同步 R2",
    }[mode] || mode;
  showSummaryProgress(true, {
    label: `已提交（${modeLabel}），排队中…`,
    statusHint: { status: "QUEUED", percent: 0, stage: "排队中" },
  });

  try {
    const res = await apiFetch(`/meetings/${token}/summary`, {
      method: "POST",
      body: JSON.stringify({ force, mode }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || res.statusText);
    }
    openSummaryStream(token);
  } catch (e) {
    detailState.lastStatus = { status: "FAILED", percent: 0, stage: e.message };
    showSummaryProgress(true, {
      label: `提交失败：${e.message}`,
      statusHint: detailState.lastStatus,
    });
    btn.disabled = false;
  }
}

async function approveRedactionFigure(figureId) {
  const token = detailState.token;
  if (!token || !figureId) return;
  const auditRes = await apiFetch(`/meetings/${token}/redaction/audit`);
  if (!auditRes.ok) throw new Error("读取脱敏审计失败");
  const audit = await auditRes.json();
  const fig = (audit.figures || []).find((f) => f.figure_id === figureId);
  if (!fig || !(fig.regions || []).length) {
    throw new Error("该图没有可用坐标，无法按框放行");
  }
  const res = await apiFetch(`/meetings/${token}/redaction/approve`, {
    method: "POST",
    body: JSON.stringify({ figure_id: figureId, regions: fig.regions }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  const data = await res.json();
  renderRedactionPanel(data.audit);
}

async function abandonRedactionFigure(figureId) {
  const token = detailState.token;
  if (!token || !figureId) return;
  const res = await apiFetch(`/meetings/${token}/redaction/abandon`, {
    method: "POST",
    body: JSON.stringify({ figure_id: figureId, reason: "人工放弃" }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  const data = await res.json();
  renderRedactionPanel(data.audit);
}

async function openDetail(minuteToken) {
  teardownDetailSync();
  closeSummaryStream();
  detailState.token = minuteToken;
  $("#detail-title").innerHTML = thinkingHtml();
  $("#detail-meta").textContent = "";
  $("#media-section").classList.add("hidden");
  $("#media-players").innerHTML = "";
  $("#transcript-section").classList.add("hidden");
  $("#transcript-scroll").innerHTML = "";
  $("#detail-tabs").classList.add("hidden");
  $("#summary-pane").classList.add("hidden");
  $("#transcript-pane").classList.add("hidden");
  showThinking($("#detail-empty"));
  $("#detail-empty").classList.remove("hidden");
  $("#generate-summary-btn").disabled = false;
  showSummaryProgress(false);
  renderSummary("", null);

  try {
    const res = await apiFetch(`/meetings/local/${minuteToken}`);
    if (!res.ok) throw new Error("本地资源不存在，请先下载该会议");
    const data = await res.json();

    $("#detail-title").textContent = data.title || minuteToken;
    document.title = `${data.title || minuteToken} · 飞书妙记`;

    const metaParts = [];
    if (data.duration_ms) metaParts.push(`时长 ${formatDuration(data.duration_ms)}`);
    if (data.downloaded_at) metaParts.push(`下载于 ${formatTime(data.downloaded_at)}`);
    metaParts.push(data.minute_token);
    $("#detail-meta").textContent = metaParts.join(" · ");
    $("#detail-empty").classList.add("hidden");

    // 媒体访问票与纪要并行，避免串行等待
    const [, hasSummary] = await Promise.all([
      ensureAccessTicket(),
      loadSummary(minuteToken),
    ]);

    let hasContent = false;
    let mediaElement = null;
    const mediaEl = $("#media-players");

    (data.media_files || []).forEach((mf) => {
      hasContent = true;
      const label = escapeHtml(mf.name);
      // R2 预签名为绝对 URL，不可再拼 access_ticket；否则签名失效
      let mediaUrl = mf.url || "";
      if (/^https?:\/\//i.test(mediaUrl)) {
        try {
          const target = new URL(mediaUrl);
          const apiHost = new URL(API).host;
          if (target.host === apiHost) {
            mediaUrl = withAccessTicketQuery(mediaUrl);
          }
        } catch {
          /* keep as-is */
        }
      } else {
        mediaUrl = withAccessTicketQuery(
          mediaUrl ||
            `${API}/meetings/local/${detailState.token}/media/${encodeURIComponent(mf.name)}`
        );
      }
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
      } else {
        mediaEl.insertAdjacentHTML(
          "beforeend",
          `<div class="media-card"><p class="media-name">${label}</p><a class="btn btn-sm" href="${mediaUrl}" target="_blank" rel="noopener">${btnContent("download-2-line", "下载文件")}</a></div>`
        );
      }
    });

    if (hasContent) $("#media-section").classList.remove("hidden");

    if (data.transcript) {
      detailState.fullTranscriptText = data.transcript;
      if (mediaElement) {
        setupDetailSync(mediaElement, data.transcript);
      } else {
        renderTranscript(parseTranscript(data.transcript));
      }
      $("#transcript-section").classList.remove("hidden");
      hasContent = true;
    }

    document
      .querySelector('.tab-btn[data-tab="transcript"]')
      .classList.toggle("hidden", !data.transcript);

    if (data.transcript || hasSummary) {
      $("#detail-tabs").classList.remove("hidden");
      switchDetailTab(hasSummary || !data.transcript ? "summary" : "transcript");
      hasContent = true;
      openSummaryStream(minuteToken);
    }

    if (!hasContent) {
      $("#detail-empty").classList.remove("hidden");
    }
  } catch (e) {
    $("#detail-title").textContent = "加载失败";
    $("#detail-empty").textContent = e.message;
    $("#detail-empty").classList.remove("hidden");
  }
}

$("#scroll-mode-btn").addEventListener("click", toggleScrollMode);
$("#generate-summary-btn").addEventListener("click", generateSummary);
$("#refresh-redaction-btn")?.addEventListener("click", () => {
  if (detailState.token) loadRedactionAudit(detailState.token);
});
$("#redaction-list")?.addEventListener("click", async (e) => {
  const approveId = e.target.closest("[data-approve]")?.getAttribute("data-approve");
  const abandonId = e.target.closest("[data-abandon]")?.getAttribute("data-abandon");
  try {
    if (approveId) await approveRedactionFigure(approveId);
    if (abandonId) await abandonRedactionFigure(abandonId);
  } catch (err) {
    alert(err.message || String(err));
  }
});
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchDetailTab(btn.dataset.tab));
});
$("#copy-summary-btn").addEventListener("click", async () => {
  if (!detailState.summaryMarkdown) return;
  const btn = $("#copy-summary-btn");
  try {
    await navigator.clipboard.writeText(detailState.summaryMarkdown);
    setBtnContent(btn, "check-line", "已复制");
  } catch {
    setBtnContent(btn, "error-warning-line", "复制失败");
  }
  setTimeout(() => {
    setBtnContent(btn, "file-copy-line", "复制 Markdown");
  }, 1500);
});
$("#copy-transcript-btn").addEventListener("click", async () => {
  const text = detailState.fullTranscriptText;
  if (!text) return;
  const btn = $("#copy-transcript-btn");
  try {
    await navigator.clipboard.writeText(text);
    setBtnContent(btn, "check-line", "已复制");
  } catch {
    setBtnContent(btn, "error-warning-line", "复制失败");
  }
  setTimeout(() => {
    setBtnContent(btn, "file-copy-line", "复制全文");
  }, 1500);
});

window.addEventListener("beforeunload", () => {
  teardownDetailSync();
  closeSummaryStream();
});

async function exportSummary(format) {
  if (!detailState.token) return;
  try {
    await downloadWithAuth(
      `/meetings/${detailState.token}/export/summary?format=${format}`,
      `summary.${format}`
    );
  } catch (e) {
    alert(`导出失败：${e.message}`);
  }
}

async function exportTranscript(format) {
  if (!detailState.token) return;
  try {
    await downloadWithAuth(
      `/meetings/${detailState.token}/export/transcript?format=${format}`,
      `transcript.${format}`
    );
  } catch (e) {
    alert(`导出失败：${e.message}`);
  }
}

async function loadShareKeyOptions() {
  const select = $("#share-key-select");
  if (!select) return;
  const res = await apiFetch("/access-keys");
  if (!res.ok) return;
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

function syncShareModeUi() {
  const mode = $("#share-access-mode")?.value;
  const wrap = $("#share-key-field");
  if (wrap) wrap.classList.toggle("hidden", mode !== "KEY_REQUIRED");
}

async function refreshShareList() {
  const box = $("#share-list");
  if (!box || !detailState.token) return;
  const res = await apiFetch(`/meetings/${detailState.token}/shares`);
  if (!res.ok) {
    box.innerHTML = `<p class="modal-hint">加载分享列表失败</p>`;
    return;
  }
  const data = await res.json();
  const items = data.items || [];
  if (!items.length) {
    box.innerHTML = `<p class="modal-hint">暂无分享链接</p>`;
    return;
  }
  box.innerHTML = items
    .map(
      (s) => `
      <div class="share-row" data-id="${s.id}">
        <div class="share-row-main">
          <code class="share-url">${escapeHtml(s.url)}</code>
          <p class="meeting-meta">${s.access_mode === "PUBLIC" ? "公开" : "需密钥"} · ${
            s.allow_export ? "可导出" : "不可导出"
          }</p>
        </div>
        <div class="share-row-actions">
          <button type="button" class="btn btn-sm" data-copy-share="${escapeHtml(s.url)}" title="复制">${btnContent("file-copy-line", "复制")}</button>
          <button type="button" class="btn btn-sm" data-revoke-share="${s.id}" title="吊销">${btnContent("link-unlink", "吊销")}</button>
        </div>
      </div>`
    )
    .join("");
}

async function openShareDialog() {
  $("#share-result")?.classList.add("hidden");
  await loadShareKeyOptions();
  syncShareModeUi();
  await refreshShareList();
  $("#share-dialog")?.classList.remove("hidden");
}

function closeShareDialog() {
  $("#share-dialog")?.classList.add("hidden");
}

async function createShare() {
  const access_mode = $("#share-access-mode").value;
  const allow_export = $("#share-allow-export").checked;
  const access_key_id =
    access_mode === "KEY_REQUIRED" ? Number($("#share-key-select").value) || null : null;
  if (access_mode === "KEY_REQUIRED" && !access_key_id) {
    alert("请先创建并选择一把密钥");
    return;
  }
  const res = await apiFetch(`/meetings/${detailState.token}/shares`, {
    method: "POST",
    body: JSON.stringify({ access_mode, allow_export, access_key_id }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    alert(typeof err.detail === "string" ? err.detail : "创建分享失败");
    return;
  }
  const data = await res.json();
  const result = $("#share-result");
  result.classList.remove("hidden");
  result.innerHTML = `已创建：<code>${escapeHtml(data.url)}</code>
    <button type="button" class="btn btn-sm" id="copy-new-share">${btnContent("file-copy-line", "复制链接")}</button>`;
  $("#copy-new-share")?.addEventListener("click", async () => {
    await navigator.clipboard.writeText(data.url);
  });
  await refreshShareList();
}

document.querySelectorAll("[data-export-summary]").forEach((btn) => {
  btn.addEventListener("click", () => exportSummary(btn.dataset.exportSummary));
});
document.querySelectorAll("[data-export-transcript]").forEach((btn) => {
  btn.addEventListener("click", () => exportTranscript(btn.dataset.exportTranscript));
});
$("#share-btn")?.addEventListener("click", openShareDialog);
$("#share-access-mode")?.addEventListener("change", syncShareModeUi);
$("#share-create-btn")?.addEventListener("click", createShare);
$("#share-dialog")?.addEventListener("click", async (e) => {
  if (e.target.closest("[data-close-dialog]")) {
    closeShareDialog();
    return;
  }
  const copy = e.target.closest("[data-copy-share]");
  if (copy) {
    await navigator.clipboard.writeText(copy.dataset.copyShare);
    return;
  }
  const revoke = e.target.closest("[data-revoke-share]");
  if (revoke) {
    if (!confirm("确定吊销该分享链接？")) return;
    await apiFetch(`/shares/${revoke.dataset.revokeShare}`, { method: "DELETE" });
    await refreshShareList();
  }
});

bindAdminNav();
checkAuth();

const token = new URLSearchParams(location.search).get("token")?.trim();
if (!token) {
  location.replace("/");
} else {
  openDetail(token);
}
