/**
 * 检索助手前端：管理端 agent.html 与分享页 share-agent.html 共用。
 * 两边的差别只有身份怎么带——登录侧靠 JWT，访客侧靠请求体里的本机密钥。
 */
(function () {
  const MODE = document.body.dataset.agentMode === "guest" ? "guest" : "admin";
  const GUEST_SESSION_KEY = "agent_guest_session";

  const state = {
    sessionId: null,
    busy: false,
    sessions: [],
    library: [],
  };

  function $(sel) {
    return document.querySelector(sel);
  }

  function guestSessionId() {
    let id = localStorage.getItem(GUEST_SESSION_KEY) || "";
    if (!id) {
      id = `g-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
      localStorage.setItem(GUEST_SESSION_KEY, id);
    }
    return id;
  }

  function guestIdentity() {
    return {
      keys: typeof loadAccessKeyList === "function" ? loadAccessKeyList() : [],
      share_tokens:
        typeof loadKnownShareTokens === "function" ? loadKnownShareTokens() : [],
    };
  }

  async function callApi(path, body) {
    if (MODE === "admin") {
      return apiFetch(`/agent${path}`, {
        method: "POST",
        body: JSON.stringify(body || {}),
      });
    }
    return fetch(`${API}/share/agent${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Share-Session": guestSessionId(),
      },
      body: JSON.stringify({ ...guestIdentity(), ...(body || {}) }),
    });
  }

  async function fetchSessions() {
    if (MODE === "admin") return apiFetch("/agent/sessions");
    return callApi("/sessions/list");
  }

  async function fetchMessages(sessionId) {
    if (MODE === "admin") {
      return apiFetch(`/agent/sessions/${sessionId}/messages`);
    }
    return callApi(`/sessions/${sessionId}/messages`);
  }

  // ---------- 渲染 ----------

  function stream() {
    return $("#agent-stream");
  }

  function clearHello() {
    stream()?.querySelector(".agent-hello")?.remove();
  }

  function scrollToBottom() {
    const el = stream();
    if (el) el.scrollTop = el.scrollHeight;
  }

  function appendUser(text) {
    clearHello();
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg-user";
    el.innerHTML = `<div class="agent-bubble">${escapeHtml(text)}</div>`;
    stream()?.appendChild(el);
    scrollToBottom();
  }

  function appendAssistant() {
    clearHello();
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg-assistant";
    el.innerHTML = `<div class="agent-bubble agent-markdown"></div>`;
    stream()?.appendChild(el);
    scrollToBottom();
    return el.querySelector(".agent-bubble");
  }

  function renderMarkdownInto(node, text) {
    if (!node) return;
    if (typeof renderSafeMarkdown === "function") {
      node.innerHTML = renderSafeMarkdown(text);
    } else {
      node.textContent = text;
    }
  }

  const TOOL_LABELS = { search: "检索", read: "阅读", send: "发送" };

  function appendTrace(name, brief) {
    clearHello();
    const el = document.createElement("div");
    el.className = "agent-trace is-running";
    el.innerHTML = `
      <i class="ri ri-loader-4-line agent-trace-spin" aria-hidden="true"></i>
      <span class="agent-trace-name">${escapeHtml(TOOL_LABELS[name] || name)}</span>
      <span class="agent-trace-brief">${escapeHtml(brief || "")}</span>
      <span class="agent-trace-result"></span>`;
    stream()?.appendChild(el);
    scrollToBottom();
    return el;
  }

  function finishTrace(el, summary) {
    if (!el) return;
    el.classList.remove("is-running");
    const icon = el.querySelector("i");
    if (icon) icon.className = "ri ri-check-line";
    const slot = el.querySelector(".agent-trace-result");
    if (slot) slot.textContent = summary || "";
  }

  // 一句话的等待条：从点发送到模型开口之间总有几秒，不占位就像卡住了
  function appendPending(text) {
    clearHello();
    const el = document.createElement("div");
    el.className = "agent-trace is-running";
    el.innerHTML = `
      <i class="ri ri-loader-4-line agent-trace-spin" aria-hidden="true"></i>
      <span class="agent-trace-name"></span>`;
    el.querySelector(".agent-trace-name").textContent = text;
    stream()?.appendChild(el);
    scrollToBottom();
    return el;
  }

  function appendThinking() {
    clearHello();
    const el = document.createElement("details");
    el.className = "agent-think";
    el.open = true;
    el.innerHTML = `
      <summary class="agent-think-head">
        <i class="ri ri-loader-4-line agent-trace-spin" aria-hidden="true"></i>
        <span class="agent-think-label">思考中…</span>
      </summary>
      <div class="agent-think-body"></div>`;
    stream()?.appendChild(el);
    scrollToBottom();
    return el;
  }

  function pushThinking(el, text) {
    const body = el?.querySelector(".agent-think-body");
    if (!body) return;
    body.textContent += text;
    // 思考块自己滚，不把下面的正文顶走
    body.scrollTop = body.scrollHeight;
    scrollToBottom();
  }

  function finishThinking(el) {
    if (!el) return;
    // 两步工具之间的思考要留在屏幕上，收起来用户会以为中间卡住了
    el.open = true;
    const icon = el.querySelector("i");
    if (icon) icon.className = "ri ri-lightbulb-flash-line";
    const chars = (el.querySelector(".agent-think-body")?.textContent || "").length;
    const label = el.querySelector(".agent-think-label");
    if (label) label.textContent = chars ? `思考过程 · ${chars} 字` : "思考过程";
  }

  function replayTool(msg) {
    const meta = msg.meta || {};
    if (meta.kind === "thinking") {
      const el = appendThinking();
      pushThinking(el, msg.content || "");
      finishThinking(el);
      return;
    }
    const el = appendTrace(meta.name || "tool", meta.brief || "");
    finishTrace(el, meta.summary || msg.content || "");
  }

  function appendCard(card) {
    clearHello();
    const el = document.createElement("div");
    el.className = "agent-card";
    const where = `${card.meeting_title || ""} · ${card.meeting_time || ""}`;
    const lines =
      card.start_line === card.end_line
        ? `第 ${card.start_line} 行`
        : `第 ${card.start_line}-${card.end_line} 行`;
    el.innerHTML = `
      <div class="agent-card-head">
        <i class="ri ri-double-quotes-l" aria-hidden="true"></i>
        <span class="agent-card-where">${escapeHtml(where)}</span>
        <span class="agent-card-lines">${escapeHtml(lines)}</span>
      </div>
      ${card.note ? `<p class="agent-card-note">${escapeHtml(card.note)}</p>` : ""}
      <blockquote class="agent-card-quote">${escapeHtml(card.quote || "")}</blockquote>
      <div class="agent-card-foot">
        <a class="btn btn-sm" href="${escapeHtml(card.url || "#")}" target="_blank" rel="noopener">
          <i class="ri ri-external-link-line" aria-hidden="true"></i><span class="btn-label">跳到原文</span>
        </a>
      </div>`;
    stream()?.appendChild(el);
    scrollToBottom();
  }

  function appendNotice(text, kind) {
    const el = document.createElement("div");
    el.className = `agent-notice${kind === "error" ? " is-error" : ""}`;
    el.textContent = text;
    stream()?.appendChild(el);
    scrollToBottom();
  }

  function renderQuota(limit, used) {
    const el = $("#agent-quota");
    if (!el) return;
    if (!limit) {
      el.textContent = used ? `今日已问 ${used} 次` : "";
      return;
    }
    el.textContent = `今日剩余 ${Math.max(0, limit - used)} / ${limit} 次`;
  }

  function renderSessions() {
    const list = $("#agent-sessions");
    const empty = $("#agent-sessions-empty");
    if (!list) return;
    if (!state.sessions.length) {
      list.innerHTML = "";
      empty?.classList.remove("hidden");
      return;
    }
    empty?.classList.add("hidden");
    list.innerHTML = state.sessions
      .map((s) => {
        const active = s.id === state.sessionId ? " is-active" : "";
        const when = typeof formatTime === "function" ? formatTime(s.updated_at) : "";
        return `
        <li>
          <button class="agent-session-item${active}" type="button" data-session-id="${s.id}">
            <span class="agent-session-title">${escapeHtml(s.title || "新对话")}</span>
            <span class="agent-session-meta">${escapeHtml(when || "")} · ${s.turn_count} 轮</span>
          </button>
        </li>`;
      })
      .join("");
    list.querySelectorAll("[data-session-id]").forEach((btn) => {
      btn.addEventListener("click", () => openSession(Number(btn.dataset.sessionId)));
    });
  }

  // ---------- 会话 ----------

  async function loadSessions() {
    try {
      const res = await fetchSessions();
      if (!res.ok) return;
      const data = await res.json();
      state.sessions = data.items || [];
      renderQuota(data.quota_limit || 0, data.quota_used || 0);
      renderSessions();
    } catch {
      /* 列表拉不到不影响提问 */
    }
  }

  async function openSession(sessionId) {
    if (state.busy) return;
    state.sessionId = sessionId;
    renderSessions();
    const el = stream();
    if (el) el.innerHTML = "";
    try {
      const res = await fetchMessages(sessionId);
      if (!res.ok) {
        appendNotice("这段对话读不出来了", "error");
        return;
      }
      const data = await res.json();
      for (const msg of data.items || []) {
        if (msg.role === "USER") appendUser(msg.content);
        else if (msg.role === "ASSISTANT") {
          renderMarkdownInto(appendAssistant(), msg.content);
        } else if (msg.role === "CARD") appendCard(msg.meta || {});
        else if (msg.role === "TOOL") replayTool(msg);
      }
      if (!(data.items || []).length) {
        appendNotice("这段对话还是空的，问一句吧");
      }
    } catch {
      appendNotice("网络异常，历史没读出来", "error");
    }
  }

  async function newSession({ silent = false } = {}) {
    const res = await callApi("/sessions", {});
    if (!res.ok) {
      const detail = await res
        .json()
        .then((d) => d.detail)
        .catch(() => "");
      if (!silent) appendNotice(detail || "新建对话失败", "error");
      return null;
    }
    const session = await res.json();
    state.sessionId = session.id;
    state.sessions = [session, ...state.sessions];
    renderSessions();
    const el = stream();
    if (el && !silent) el.innerHTML = "";
    return session.id;
  }

  // ---------- SSE ----------

  async function readSse(response, handlers) {
    if (!response.body) throw new Error("浏览器不支持流式读取");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    const flush = (block) => {
      let ev = "message";
      const dataLines = [];
      for (const line of block.split(/\r?\n/)) {
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
        flush(buf.slice(0, idx));
        buf = buf.slice(idx + 2);
      }
    }
    if (buf.trim()) flush(buf);
  }

  async function send() {
    if (state.busy) return;
    const input = $("#agent-input");
    const question = (input?.value || "").trim();
    if (!question) return;

    if (!state.sessionId) {
      const created = await newSession({ silent: true });
      if (!created) return;
    }

    state.busy = true;
    const sendBtn = $("#agent-send");
    if (sendBtn) sendBtn.disabled = true;
    if (input) input.value = "";
    appendUser(question);

    // 一轮里可能是「说一句 → 查一次 → 再说一句」，每段正文各占一个气泡，
    // 思考和工具痕迹按发生顺序插在中间，这样等待期间屏幕上一直有东西在动
    let bubble = null;
    let segmentText = "";
    let think = null;
    let sawText = false;
    const traces = new Map();
    let pending = appendPending("正在准备…");

    const setPending = (text) => {
      const slot = pending?.querySelector(".agent-trace-name");
      if (slot) slot.textContent = text;
    };
    const dropPending = () => {
      pending?.remove();
      pending = null;
    };
    const closeThink = () => {
      finishThinking(think);
      think = null;
    };

    try {
      const res = await callApi(`/sessions/${state.sessionId}/chat/stream`, {
        question,
      });
      if (!res.ok) {
        const detail = await res
          .json()
          .then((d) => d.detail)
          .catch(() => "");
        appendNotice(detail || `提问失败（${res.status}）`, "error");
        return;
      }
      await readSse(res, {
        status: (data) => {
          if (data.stage === "QUEUED") setPending("前面还有人在问，排队中…");
          else if (data.stage === "READY") setPending("正在整理课程目录…");
          else if (data.stage === "WAITING_MODEL") setPending("模型正在思考…");
        },
        segment: (data) => {
          dropPending();
          closeThink();
          if (data.kind === "thinking") {
            think = appendThinking();
          } else {
            // 另起一段正文：清掉当前气泡，等第一个增量来了再建，免得留空壳
            bubble = null;
            segmentText = "";
          }
        },
        thinking: (data) => {
          dropPending();
          if (!think) think = appendThinking();
          pushThinking(think, data.text || "");
        },
        tool_start: (data) => {
          dropPending();
          closeThink();
          const existing = traces.get(data.name);
          if (existing) {
            // 模型刚决定调用时先占位，工具真正跑起来会带上检索词再刷一遍
            const brief = existing.querySelector(".agent-trace-brief");
            if (brief && data.brief) brief.textContent = data.brief;
            return;
          }
          traces.set(data.name, appendTrace(data.name, data.brief));
        },
        tool_end: (data) => {
          finishTrace(traces.get(data.name), data.summary);
          traces.delete(data.name);
          // 下一步可能是再想一会儿，先占着别让屏幕空掉
          pending = appendPending("正在想下一步…");
        },
        card: (data) => appendCard(data),
        delta: (data) => {
          dropPending();
          closeThink();
          if (!bubble) {
            bubble = appendAssistant();
            segmentText = "";
          }
          segmentText += data.text || "";
          sawText = true;
          renderMarkdownInto(bubble, segmentText);
          scrollToBottom();
        },
        done: (data) => {
          dropPending();
          closeThink();
          // 增量一路没来（比如上游不支持流式）才用整段回答兜底
          if (data.reply && !sawText) {
            if (!bubble) bubble = appendAssistant();
            renderMarkdownInto(bubble, data.reply);
          }
          if (typeof data.quota_remaining === "number") {
            const el = $("#agent-quota");
            if (el) el.textContent = `今日剩余 ${data.quota_remaining} 次`;
          }
          scrollToBottom();
        },
        error: (data) => {
          dropPending();
          closeThink();
          appendNotice(data.detail || "提问失败", "error");
        },
      });
    } catch {
      appendNotice("网络中断了，这一轮没跑完", "error");
    } finally {
      dropPending();
      closeThink();
      for (const el of traces.values()) finishTrace(el, "已中断");
      state.busy = false;
      if (sendBtn) sendBtn.disabled = false;
      loadSessions();
    }
  }

  // ---------- 访客侧的课程列表 ----------

  function libraryFilterValue() {
    return ($("#share-library-filter")?.value || "").trim().toLowerCase();
  }

  function renderLibrary() {
    const list = $("#share-library-list");
    const status = $("#share-library-status");
    if (!list || !status) return;

    const q = libraryFilterValue();
    const total = state.library.length;
    const items = q
      ? state.library.filter((item) =>
          `${item.title || ""} ${item.minute_token || ""}`.toLowerCase().includes(q)
        )
      : state.library;

    status.textContent = !total
      ? "暂无可访问课程"
      : items.length === total
        ? `共 ${total} 门课`
        : `筛选 ${items.length} / ${total}`;

    if (!items.length) {
      list.innerHTML = `<li class="share-library-empty">${
        total === 0
          ? "本机还没有记住可用密钥，先去分享页解锁并勾选「记住密钥」。"
          : "没有匹配的课程。"
      }</li>`;
      return;
    }

    list.innerHTML = items
      .map((item) => {
        const when =
          typeof formatTime === "function" ? formatTime(item.create_time) : "";
        return `
        <li>
          <a class="share-library-item" href="${escapeHtml(item.url || "#")}" title="${escapeHtml(item.title || "")}">
            <p class="share-library-item-title">${escapeHtml(item.title || "未命名课程")}</p>
            <p class="share-library-item-meta">${escapeHtml(when || "暂无时间信息")}</p>
          </a>
        </li>`;
      })
      .join("");
  }

  async function loadLibrary() {
    const identity = guestIdentity();
    if (!identity.keys.length && !identity.share_tokens.length) {
      state.library = [];
      renderLibrary();
      return;
    }
    try {
      const res = await fetch(`${API}/share/library`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(identity),
      });
      if (!res.ok) {
        $("#share-library-status").textContent = "列表加载失败";
        return;
      }
      const data = await res.json();
      state.library = data.items || [];
      renderLibrary();
    } catch {
      const status = $("#share-library-status");
      if (status) status.textContent = "列表加载失败";
    }
  }

  // ---------- 装配 ----------

  function bind() {
    $("#agent-send")?.addEventListener("click", send);
    $("#agent-input")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
    $("#agent-new-btn")?.addEventListener("click", async () => {
      if (state.busy) return;
      await newSession();
    });
    $("#agent-sessions-refresh")?.addEventListener("click", loadSessions);
    $("#share-library-refresh")?.addEventListener("click", loadLibrary);
    $("#share-library-filter")?.addEventListener("input", renderLibrary);
  }

  async function init() {
    bind();
    if (MODE === "guest") await loadLibrary();
    await loadSessions();
    if (state.sessions.length) await openSession(state.sessions[0].id);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
