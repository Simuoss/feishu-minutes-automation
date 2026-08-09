/**
 * 划词答疑：在纪要/转写容器内划选 → 浮层提问 → 多轮多模态对话（sessionStorage 缓存）。
 *
 * 用法：
 *   initPassageAsk({
 *     roots: [{ el: "#summary-content", kind: "SUMMARY", getText: () => md }],
 *     storageKey: "ask:meeting:token",
 *     // 优先流式；返回 fetch Response（SSE）
 *     askStream: async (body) => apiFetch(.../ask/stream, { method:"POST", body }),
 *     // 可选回退
 *     ask: async (body) => { const res = await apiFetch(...); return res.json(); },
 *   });
 */

(function () {
  const STATE = {
    opts: null,
    selectedText: "",
    sourceKind: "SUMMARY",
    messages: [],
    pendingImages: [],
    busy: false,
    bound: false,
    renderScheduled: false,
  };

  function renderAskMarkdown(text) {
    if (typeof renderSafeMarkdown !== "function") {
      return `<pre class="passage-ask-plain">${escapeHtml(text || "")}</pre>`;
    }
    return renderSafeMarkdown(text || "", {
      resolveAssetUrl: () => null,
    });
  }

  function scheduleRender() {
    if (STATE.renderScheduled) return;
    STATE.renderScheduled = true;
    requestAnimationFrame(() => {
      STATE.renderScheduled = false;
      renderMessages();
    });
  }

  function $(sel, root) {
    return (root || document).querySelector(sel);
  }

  function ensureUi() {
    if ($("#passage-ask-pop")) return;
    const pop = document.createElement("button");
    pop.id = "passage-ask-pop";
    pop.type = "button";
    pop.className = "passage-ask-pop hidden";
    pop.innerHTML = `<i class="ri ri-chat-ai-line" aria-hidden="true"></i><span>针对这里向 AI 提问</span>`;
    document.body.appendChild(pop);

    const drawer = document.createElement("div");
    drawer.id = "passage-ask-drawer";
    drawer.className = "passage-ask-drawer hidden";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-modal", "true");
    drawer.innerHTML = `
      <div class="passage-ask-backdrop" data-ask-close></div>
      <div class="passage-ask-panel">
        <header class="passage-ask-header">
          <div>
            <h3 class="passage-ask-title">划词提问</h3>
            <p class="passage-ask-quote muted" id="passage-ask-quote"></p>
          </div>
          <button type="button" class="btn btn-sm" data-ask-close title="关闭">
            <i class="ri ri-close-line" aria-hidden="true"></i><span class="btn-label">关闭</span>
          </button>
        </header>
        <div id="passage-ask-messages" class="passage-ask-messages"></div>
        <div id="passage-ask-previews" class="passage-ask-previews"></div>
        <footer class="passage-ask-composer">
          <textarea id="passage-ask-input" rows="2" placeholder="继续追问…（首轮可直接点发送）"></textarea>
          <div class="passage-ask-actions">
            <label class="btn btn-sm passage-ask-upload">
              <i class="ri ri-image-add-line" aria-hidden="true"></i><span class="btn-label">图片</span>
              <input id="passage-ask-file" type="file" accept="image/*" multiple hidden />
            </label>
            <button type="button" class="btn btn-sm" id="passage-ask-clear" title="清空本段对话">
              <i class="ri ri-delete-bin-line" aria-hidden="true"></i><span class="btn-label">清空</span>
            </button>
            <button type="button" class="btn btn-primary btn-sm" id="passage-ask-send">
              <i class="ri ri-send-plane-2-line" aria-hidden="true"></i><span class="btn-label">发送</span>
            </button>
          </div>
          <p class="passage-ask-hint muted">回答基于划选片段及附近上下文；对话仅缓存在本机标签页。</p>
        </footer>
      </div>`;
    document.body.appendChild(drawer);

    pop.addEventListener("mousedown", (e) => e.preventDefault());
    pop.addEventListener("click", () => openDrawerFromSelection());
    drawer.addEventListener("click", (e) => {
      if (e.target.closest("[data-ask-close]")) closeDrawer();
    });
    $("#passage-ask-send")?.addEventListener("click", () => sendTurn());
    $("#passage-ask-clear")?.addEventListener("click", () => {
      if (!confirm("清空这段划词下的对话？")) return;
      STATE.messages = [];
      STATE.pendingImages = [];
      persist();
      renderMessages();
      renderPreviews();
    });
    $("#passage-ask-file")?.addEventListener("change", onPickFiles);
    $("#passage-ask-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendTurn();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("#passage-ask-drawer")?.classList.contains("hidden")) {
        closeDrawer();
      }
    });
  }

  function cacheKey() {
    const base = STATE.opts?.storageKey || "passage-ask";
    const sel = STATE.selectedText.slice(0, 80);
    return `${base}:${STATE.sourceKind}:${simpleHash(sel)}`;
  }

  function simpleHash(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return Math.abs(h).toString(36);
  }

  function persist() {
    try {
      sessionStorage.setItem(
        cacheKey(),
        JSON.stringify({
          selectedText: STATE.selectedText,
          sourceKind: STATE.sourceKind,
          messages: STATE.messages,
        })
      );
    } catch {
      /* quota */
    }
  }

  function loadCache() {
    try {
      const raw = sessionStorage.getItem(cacheKey());
      if (!raw) {
        STATE.messages = [];
        return;
      }
      const data = JSON.parse(raw);
      if (data.selectedText === STATE.selectedText) {
        STATE.messages = Array.isArray(data.messages) ? data.messages : [];
      } else {
        STATE.messages = [];
      }
    } catch {
      STATE.messages = [];
    }
  }

  function rootForNode(node) {
    const roots = STATE.opts?.roots || [];
    for (const r of roots) {
      const el = typeof r.el === "string" ? $(r.el) : r.el;
      if (el && node && el.contains(node)) return { ...r, el };
    }
    return null;
  }

  function currentArticleText(kind) {
    const roots = STATE.opts?.roots || [];
    const hit = roots.find((r) => r.kind === kind);
    if (!hit) return "";
    if (typeof hit.getText === "function") return String(hit.getText() || "");
    const el = typeof hit.el === "string" ? $(hit.el) : hit.el;
    return el ? el.innerText || el.textContent || "" : "";
  }

  function selectionBelongsToArticle(selected, kind) {
    if (!selected) return false;
    const norm = (s) => s.replace(/\s+/g, " ").trim();
    const candidates = [];
    const article = currentArticleText(kind);
    if (article) candidates.push(article);
    const roots = STATE.opts?.roots || [];
    const hit = roots.find((r) => r.kind === kind);
    if (hit) {
      const el = typeof hit.el === "string" ? $(hit.el) : hit.el;
      const displayed = el ? el.innerText || el.textContent || "" : "";
      if (displayed) candidates.push(displayed);
    }
    const nsel = norm(selected);
    return candidates.some(
      (c) => c.includes(selected) || norm(c).includes(nsel)
    );
  }

  function hidePop() {
    $("#passage-ask-pop")?.classList.add("hidden");
  }

  function onSelectionMaybe() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) {
      hidePop();
      return;
    }
    const text = String(sel.toString() || "").trim();
    if (text.length < 2 || text.length > 4000) {
      hidePop();
      return;
    }
    const anchor = sel.anchorNode;
    const root = rootForNode(anchor);
    if (!root) {
      hidePop();
      return;
    }
    if (!selectionBelongsToArticle(text, root.kind)) {
      hidePop();
      return;
    }
    STATE.selectedText = text;
    STATE.sourceKind = root.kind;
    const range = sel.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    const pop = $("#passage-ask-pop");
    if (!pop) return;
    pop.classList.remove("hidden");
    const top = window.scrollY + rect.top - 44;
    const left = window.scrollX + rect.left + rect.width / 2;
    pop.style.top = `${Math.max(8, top)}px`;
    pop.style.left = `${Math.max(8, left)}px`;
  }

  function openDrawerFromSelection() {
    hidePop();
    if (!STATE.selectedText) return;
    loadCache();
    STATE.pendingImages = [];
    const quote = $("#passage-ask-quote");
    if (quote) {
      quote.textContent =
        STATE.selectedText.length > 120
          ? `${STATE.selectedText.slice(0, 120)}…`
          : STATE.selectedText;
    }
    renderMessages();
    renderPreviews();
    $("#passage-ask-drawer")?.classList.remove("hidden");
    const input = $("#passage-ask-input");
    if (input) {
      input.value = "";
      input.placeholder = STATE.messages.length
        ? "继续追问…"
        : "可直接发送（默认：解释这段），或先写问题";
      input.focus();
    }
    if (!STATE.messages.length) {
      sendTurn({ autoFirst: true });
    } else {
      renderMessages();
    }
  }

  function closeDrawer() {
    $("#passage-ask-drawer")?.classList.add("hidden");
  }

  function renderMessages() {
    const box = $("#passage-ask-messages");
    if (!box) return;
    if (!STATE.messages.length) {
      box.innerHTML = `<p class="muted passage-ask-empty">正在基于划选内容提问…</p>`;
      return;
    }
    box.innerHTML = STATE.messages
      .map((m) => {
        const role = m.role === "assistant" ? "AI" : "我";
        const cls = m.role === "assistant" ? "is-ai" : "is-user";
        const imgs = (m.images || [])
          .map(
            (img) =>
              `<img class="passage-ask-thumb" src="data:${escapeHtml(img.media_type)};base64,${img.data_base64}" alt="" />`
          )
          .join("");
        const streaming = Boolean(m.streaming);
        const stageHint = m.stage
          ? `<p class="muted passage-ask-stage">${escapeHtml(m.stage)}</p>`
          : "";
        const body =
          m.role === "assistant"
            ? streaming && !(m.content || "").trim()
              ? `${stageHint}${thinkingHtml({ block: true })}`
              : renderAskMarkdown(m.content || "")
            : `<pre class="passage-ask-plain">${escapeHtml(m.content || "")}</pre>`;
        let thinkingBlock = "";
        if (m.role === "assistant" && (m.has_thinking || m.thinking)) {
          const content = (m.thinking || "").trim()
            ? `<pre class="passage-ask-thinking-body">${escapeHtml(m.thinking)}</pre>`
            : `<p class="muted passage-ask-thinking-empty">${
                streaming
                  ? "正在思考…"
                  : "模型已完成内部推理（本通道未返回思考原文）"
              }</p>`;
          thinkingBlock = `<details class="passage-ask-thinking"${
            streaming || (m.thinking || "").trim() ? " open" : ""
          }>
            <summary>思考过程</summary>
            ${content}
          </details>`;
        }
        return `<div class="passage-ask-bubble ${cls}">
          <div class="passage-ask-role">${role}${
            streaming ? '<span class="passage-ask-streaming"> · 生成中</span>' : ""
          }</div>
          ${imgs ? `<div class="passage-ask-thumbs">${imgs}</div>` : ""}
          ${thinkingBlock}
          <div class="passage-ask-body">${body}</div>
        </div>`;
      })
      .join("");
    box.scrollTop = box.scrollHeight;
  }

  function renderPreviews() {
    const box = $("#passage-ask-previews");
    if (!box) return;
    if (!STATE.pendingImages.length) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = STATE.pendingImages
      .map(
        (img, i) =>
          `<span class="passage-ask-preview">
            <img src="data:${escapeHtml(img.media_type)};base64,${img.data_base64}" alt="" />
            <button type="button" data-rm-img="${i}" title="移除">×</button>
          </span>`
      )
      .join("");
    box.querySelectorAll("[data-rm-img]").forEach((btn) => {
      btn.addEventListener("click", () => {
        STATE.pendingImages.splice(Number(btn.dataset.rmImg), 1);
        renderPreviews();
      });
    });
  }

  async function onPickFiles(e) {
    const files = [...(e.target.files || [])];
    e.target.value = "";
    for (const file of files.slice(0, 3 - STATE.pendingImages.length)) {
      if (!file.type.startsWith("image/")) continue;
      if (file.size > 4 * 1024 * 1024) {
        alert("单张图片不能超过 4MB");
        continue;
      }
      const data_base64 = await readFileBase64(file);
      STATE.pendingImages.push({
        media_type: file.type || "image/jpeg",
        data_base64,
      });
    }
    renderPreviews();
  }

  function readFileBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || "");
        const idx = result.indexOf(",");
        resolve(idx >= 0 ? result.slice(idx + 1) : result);
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  }

  async function readAskSse(response, handlers) {
    if (!response.body) throw new Error("浏览器不支持流式读取");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let eventName = "message";

    const flushBlock = (block) => {
      const lines = block.split(/\r?\n/);
      let ev = eventName;
      const dataLines = [];
      for (const line of lines) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) return;
      let data = {};
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch {
        return;
      }
      const fn = handlers[ev];
      if (typeof fn === "function") fn(data);
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      buf = buf.replace(/\r\n/g, "\n");
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        flushBlock(block);
      }
    }
    if (buf.trim()) flushBlock(buf);
  }

  async function sendTurn({ autoFirst = false } = {}) {
    if (STATE.busy || (!STATE.opts?.askStream && !STATE.opts?.ask)) return;
    const input = $("#passage-ask-input");
    const question = (input?.value || "").trim();
    const isFirst = STATE.messages.length === 0;
    if (!isFirst && !question && !STATE.pendingImages.length) {
      if (!autoFirst) alert("请输入追问或添加图片");
      return;
    }

    STATE.busy = true;
    const sendBtn = $("#passage-ask-send");
    if (sendBtn) sendBtn.disabled = true;

    const history = STATE.messages.map((m) => ({
      role: m.role,
      content: m.content,
      images: m.images || [],
    }));
    const images = [...STATE.pendingImages];
    const body = {
      source_kind: STATE.sourceKind,
      selected_text: STATE.selectedText,
      history,
      question: question || null,
      images,
    };

    const userPlaceholder = {
      role: "user",
      content: isFirst ? "…" : question || "（附图）",
      images,
    };
    const assistantMsg = {
      role: "assistant",
      content: "",
      images: [],
      thinking: "",
      has_thinking: false,
      streaming: true,
      stage: "正在连接模型…",
    };

    const stageLabel = (stage) => {
      if (stage === "WAITING_MODEL") return "正在等待模型首包（含上游思考）…";
      if (stage === "THINKING") return "模型思考中…";
      if (stage === "ANSWERING") return "正在生成回答…";
      return "";
    };

    if (!isFirst) {
      STATE.messages.push(userPlaceholder);
      if (input) input.value = "";
      STATE.pendingImages = [];
      renderPreviews();
    } else {
      STATE.messages.push(userPlaceholder);
      if (input) input.value = "";
      STATE.pendingImages = [];
      renderPreviews();
    }
    STATE.messages.push(assistantMsg);
    renderMessages();

    const finishError = (msg) => {
      STATE.messages = STATE.messages.filter((m) => m !== assistantMsg);
      if (isFirst) {
        STATE.messages = STATE.messages.filter((m) => m !== userPlaceholder);
        const boxEl = $("#passage-ask-messages");
        if (boxEl) boxEl.innerHTML = `<p class="login-error">${escapeHtml(msg)}</p>`;
      } else {
        STATE.messages = STATE.messages.filter((m) => m !== userPlaceholder);
        alert(msg);
        renderMessages();
      }
    };

    try {
      if (STATE.opts.askStream) {
        const res = await STATE.opts.askStream(body);
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(
            typeof err.detail === "string" ? err.detail : "提问失败，请稍后重试"
          );
        }
        let settled = false;
        let streamErr = "";
        await readAskSse(res, {
          meta: (data) => {
            userPlaceholder.content = data.user_message || userPlaceholder.content;
            scheduleRender();
          },
          status: (data) => {
            const label = stageLabel(data.stage);
            if (label) assistantMsg.stage = label;
            scheduleRender();
          },
          thinking_start: () => {
            assistantMsg.has_thinking = true;
            assistantMsg.stage = stageLabel("THINKING");
            scheduleRender();
          },
          thinking: (data) => {
            assistantMsg.has_thinking = true;
            assistantMsg.thinking += data.text || "";
            scheduleRender();
          },
          delta: (data) => {
            assistantMsg.content += data.text || "";
            if (assistantMsg.content) assistantMsg.stage = "";
            scheduleRender();
          },
          restart: () => {
            assistantMsg.content = "";
            assistantMsg.thinking = "";
            assistantMsg.has_thinking = false;
            assistantMsg.stage = stageLabel("WAITING_MODEL");
            scheduleRender();
          },
          done: (data) => {
            settled = true;
            userPlaceholder.content =
              data.user_message || userPlaceholder.content;
            assistantMsg.content = data.reply || assistantMsg.content;
            assistantMsg.thinking = data.thinking || assistantMsg.thinking;
            assistantMsg.has_thinking = Boolean(
              data.has_thinking || assistantMsg.thinking
            );
            assistantMsg.streaming = false;
            assistantMsg.stage = "";
            persist();
            renderMessages();
          },
          error: (data) => {
            settled = true;
            streamErr = data.detail || "提问失败";
          },
        });
        if (streamErr) throw new Error(streamErr);
        if (!settled) {
          if (!(assistantMsg.content || "").trim()) {
            throw new Error("模型未返回内容");
          }
          assistantMsg.streaming = false;
          persist();
          renderMessages();
        }
      } else {
        const data = await STATE.opts.ask(body);
        userPlaceholder.content = data.user_message || userPlaceholder.content;
        assistantMsg.content = data.reply || "";
        assistantMsg.thinking = data.thinking || "";
        assistantMsg.has_thinking = Boolean(data.has_thinking || data.thinking);
        assistantMsg.streaming = false;
        persist();
        renderMessages();
      }
    } catch (err) {
      finishError(err?.message || "提问失败");
    } finally {
      STATE.busy = false;
      if (sendBtn) sendBtn.disabled = false;
    }
  }

  function bindRoots() {
    if (STATE.bound) return;
    STATE.bound = true;
    document.addEventListener("mouseup", () => {
      setTimeout(onSelectionMaybe, 0);
    });
    document.addEventListener("keyup", (e) => {
      const key = e.key || "";
      if (key === "Shift" || key.startsWith("Arrow")) onSelectionMaybe();
    });
    document.addEventListener("mousedown", (e) => {
      if (e.target.closest("#passage-ask-pop") || e.target.closest("#passage-ask-drawer")) {
        return;
      }
      hidePop();
    });
  }

  window.initPassageAsk = function initPassageAsk(options) {
    STATE.opts = options || {};
    ensureUi();
    bindRoots();
  };
})();
