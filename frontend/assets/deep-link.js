/**
 * 检索助手卡片的跳转落地：切到对应 tab，把命中位置滚出来并高亮。
 * 转写按段序号（与 renderTranscript 的 data-index 对齐），纪要渲染后没有行号，
 * 只能靠链接里带的一小段引文在 DOM 里找。
 */
(function () {
  const QUOTE_FALLBACK_LENGTHS = [30, 20, 12];

  function readParams() {
    const query = new URLSearchParams(location.search);
    const tab = query.get("tab");
    if (tab !== "transcript" && tab !== "summary") return null;
    const seg = Number(query.get("seg"));
    const segs = Number(query.get("segs"));
    return {
      tab,
      seg: Number.isFinite(seg) && seg >= 0 ? Math.trunc(seg) : null,
      segs: Number.isFinite(segs) && segs > 0 ? Math.trunc(segs) : 1,
      quote: (query.get("q") || "").trim(),
    };
  }

  function highlightSegments(seg, count) {
    const container = document.querySelector("#transcript-scroll");
    if (!container || seg === null) return false;
    const hits = [];
    for (let i = seg; i < seg + count; i += 1) {
      const el = container.querySelector(`.transcript-segment[data-index="${i}"]`);
      if (el) hits.push(el);
    }
    if (!hits.length) return false;
    hits.forEach((el) => el.classList.add("is-hit"));
    hits[0].scrollIntoView({ block: "center" });
    return true;
  }

  function findInText(root, needle) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const idx = (node.nodeValue || "").indexOf(needle);
      if (idx >= 0) return { node, idx, length: needle.length };
    }
    return null;
  }

  function highlightQuote(quote) {
    const root = document.querySelector("#summary-content");
    if (!root || !quote) return false;

    let found = findInText(root, quote);
    for (const len of QUOTE_FALLBACK_LENGTHS) {
      if (found || quote.length <= len) break;
      found = findInText(root, quote.slice(0, len));
    }
    if (!found) return false;

    const tail = found.node.splitText(found.idx);
    tail.splitText(found.length);
    const mark = document.createElement("mark");
    mark.className = "is-hit";
    mark.textContent = tail.nodeValue;
    tail.parentNode?.replaceChild(mark, tail);
    mark.scrollIntoView({ block: "center" });
    return true;
  }

  /**
   * @param {(tab: string) => void} switchTab 页面自己的切 tab 函数
   * @param {{hasTranscript?: boolean, hasSummary?: boolean}} available
   * @returns {boolean} 是否接管了初始 tab
   */
  function applyAgentDeepLink(switchTab, available) {
    const params = readParams();
    if (!params) return false;
    const ok =
      params.tab === "transcript"
        ? available?.hasTranscript !== false
        : available?.hasSummary !== false;
    if (!ok) return false;

    switchTab(params.tab);
    // 等一帧，让 tab 从 hidden 里出来再算滚动位置
    requestAnimationFrame(() => {
      if (params.tab === "transcript") highlightSegments(params.seg, params.segs);
      else highlightQuote(params.quote);
    });
    return true;
  }

  window.applyAgentDeepLink = applyAgentDeepLink;
})();
