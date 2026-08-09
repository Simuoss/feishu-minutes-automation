/**
 * 纪要 Markdown 安全渲染：marked 解析 + DOMPurify 消毒。
 * 图片只允许经 resolveAssetUrl 转成的同站资源；拒绝外链/javascript/data。
 */
(function (global) {
  "use strict";

  if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
    console.error("SafeMarkdown 依赖 marked 与 DOMPurify，请先引入 vendor 脚本");
  }

  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeAttr(text) {
    return escapeHtml(text).replace(/'/g, "&#39;");
  }

  function isDangerousHref(href) {
    const raw = String(href || "").trim();
    if (!raw) return true;
    // 协议相对、data、javascript 等一律拒绝
    if (/^\/\//.test(raw) || /^[a-z][a-z0-9+.-]*:/i.test(raw)) {
      // 允许经 resolve 后的绝对 http(s) 同站 API（由 resolveAssetUrl 产出）
      if (/^https?:\/\//i.test(raw)) return false;
      return true;
    }
    return false;
  }

  /**
   * @param {string} markdown
   * @param {{
   *   resolveAssetUrl: (path: string) => string | null,
   *   isTimeAnchor?: (code: string) => boolean,
   *   timeAnchorSeconds?: (code: string) => number | null,
   * }} options
   */
  function renderSafeMarkdown(markdown, options) {
    const opts = options || {};
    const resolveAssetUrl = opts.resolveAssetUrl || (() => null);
    const isTimeAnchor = opts.isTimeAnchor || (() => false);
    const timeAnchorSeconds = opts.timeAnchorSeconds || (() => null);
    const source = String(markdown || "");

    const renderer = new marked.Renderer();

    renderer.image = function image(token) {
      const href = typeof token === "string" ? token : token.href;
      const text = typeof token === "string" ? arguments[2] : token.text;
      const url = resolveAssetUrl(href || "");
      if (!url || isDangerousHref(url)) return "";
      const alt = escapeAttr(text || "");
      const safeUrl = escapeAttr(url);
      return (
        `<figure class="md-figure">` +
        `<a href="${safeUrl}" target="_blank" rel="noopener noreferrer">` +
        `<img src="${safeUrl}" alt="${alt}" loading="lazy" />` +
        `</a></figure>`
      );
    };

    renderer.codespan = function codespan(token) {
      const code = typeof token === "string" ? token : token.text;
      const text = String(code || "");
      if (isTimeAnchor(text)) {
        const sec = timeAnchorSeconds(text);
        if (sec !== null && Number.isFinite(sec)) {
          return (
            `<button type="button" class="time-anchor" data-sec="${sec}">` +
            `${escapeHtml(text)}</button>`
          );
        }
      }
      return `<code>${escapeHtml(text)}</code>`;
    };

    renderer.link = function link(token) {
      const href = typeof token === "string" ? token : token.href;
      const text = typeof token === "string" ? arguments[2] : token.text;
      const label = escapeHtml(text || href || "");
      // 纪要正文内链接一律不跟跳（防钓鱼/开重定向）；只保留文本
      if (!href || isDangerousHref(href) || /^https?:\/\//i.test(href)) {
        return label;
      }
      // 仅保留页内锚点一类相对引用
      if (String(href).startsWith("#")) {
        return `<a href="${escapeAttr(href)}">${label}</a>`;
      }
      return label;
    };

    renderer.html = function html() {
      // 禁止原始 HTML 块进入文档
      return "";
    };

    let dirty = marked.parse(source, {
      async: false,
      gfm: true,
      breaks: true,
      renderer,
    });

    // 紧跟图片的「图：」段落收成 figcaption
    dirty = dirty.replace(
      /<\/figure>\s*<p>\s*图：([\s\S]*?)<\/p>/g,
      "<figcaption>$1</figcaption></figure>"
    );

    return DOMPurify.sanitize(dirty, {
      USE_PROFILES: { html: true },
      ADD_TAGS: ["button", "figure", "figcaption"],
      ADD_ATTR: ["data-sec", "target", "rel", "loading", "class", "type"],
      FORBID_TAGS: ["style", "iframe", "object", "embed", "form", "input"],
      FORBID_ATTR: ["style", "onerror", "onload", "onclick"],
    });
  }

  global.renderSafeMarkdown = renderSafeMarkdown;
})(typeof window !== "undefined" ? window : globalThis);
