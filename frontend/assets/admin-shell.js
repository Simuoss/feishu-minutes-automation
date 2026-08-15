/**
 * 管理端左侧栏：会议列表 / 分享 / 密钥；会议详情额外挂载成本与脱敏区。
 * 页面通过 body[data-admin-page] 声明当前项：meetings | meeting | shares | keys
 *
 * 「管理端」手势：点 10 次 → 停 ≥1s → 再点 3 次 → 输入超管口令解锁。
 */
function mountAdminSidebar() {
  const root = document.getElementById("admin-sidebar");
  if (!root) return;

  const page = document.body.dataset.adminPage || "meetings";
  const isMeeting = page === "meeting";
  const superUnlocked = Boolean(getSuperJwt());
  const superView = isSuperAdminView();

  const navItems = [
    { id: "meetings", href: "/", icon: "ri-list-unordered", label: "会议列表" },
    { id: "shares", href: "/shares.html", icon: "ri-share-forward-line", label: "分享管理" },
    { id: "keys", href: "/keys.html", icon: "ri-key-2-line", label: "密钥管理" },
  ];
  if (superView) {
    navItems.push({
      id: "users",
      href: "/users.html",
      icon: "ri-admin-line",
      label: "管理员列表",
    });
    navItems.push({
      id: "voiceprints",
      href: "/voiceprints.html",
      icon: "ri-user-voice-line",
      label: "声纹人物",
    });
    navItems.push({
      id: "system-configs",
      href: "/system-configs.html",
      icon: "ri-settings-3-line",
      label: "系统配置",
    });
  }

  const navHtml = navItems
    .map((item) => {
      const active =
        item.id === page || (item.id === "meetings" && page === "meeting")
          ? " is-active"
          : "";
      return `<a class="sidebar-nav-item${active}" href="${item.href}">
        <i class="ri ${item.icon}" aria-hidden="true"></i>
        <span>${item.label}</span>
      </a>`;
    })
    .join("");

  const meetingExtras = isMeeting
    ? `
    <section class="sidebar-section">
      <h3 class="sidebar-section-title">成本与质量</h3>
      <div id="summary-metrics" class="summary-metrics sidebar-metrics hidden" aria-label="质量与成本"></div>
      <p id="sidebar-metrics-empty" class="sidebar-empty">生成纪要后显示 Token、耗时与配图漏斗。</p>
    </section>
    <section class="sidebar-section sidebar-section-grow">
      <div class="sidebar-section-head">
        <h3 class="sidebar-section-title">脱敏记录</h3>
        <button id="refresh-redaction-btn" class="btn btn-sm" type="button" title="刷新">
          <i class="ri ri-refresh-line" aria-hidden="true"></i>
        </button>
      </div>
      <div id="redaction-panel" class="redaction-panel sidebar-redaction hidden">
        <p id="redaction-hint" class="redaction-hint">以下为已打码结果，请提醒参会成员下次注意勿入镜敏感信息。</p>
        <p id="redaction-summary" class="redaction-summary"></p>
        <div id="redaction-list" class="redaction-list"></div>
      </div>
      <p id="sidebar-redaction-empty" class="sidebar-empty">带图纪要脱敏后可在此查看记录。</p>
    </section>`
    : "";

  const modeToggle = superUnlocked
    ? `<div class="sidebar-mode-toggle" role="group" aria-label="界面模式">
        <button type="button" class="btn btn-sm${superView ? "" : " is-active"}" data-view-mode="user">管理端</button>
        <button type="button" class="btn btn-sm${superView ? " is-active" : ""}" data-view-mode="super">超级管理端</button>
      </div>`
    : "";

  const inviteBtn = !superView
    ? `<button id="invite-btn" class="btn btn-sm" type="button" title="生成邀请链接">
        <i class="ri ri-user-add-line" aria-hidden="true"></i><span class="btn-label">邀请注册</span>
      </button>`
    : "";
  const accountBtn = !superView
    ? `<button id="account-btn" class="btn btn-sm" type="button" title="账号设置">
        <i class="ri ri-user-settings-line" aria-hidden="true"></i><span class="btn-label">账号</span>
      </button>`
    : "";

  root.innerHTML = `
    <div class="sidebar-brand">
      <div class="sidebar-brand-link">
        <a href="/" class="sidebar-brand-title"><strong>飞书妙记</strong></a>
        <span id="sidebar-brand-role" class="sidebar-brand-role${superView ? " is-super" : ""}">${
          superView ? "超级管理端" : "管理端"
        }</span>
      </div>
      ${modeToggle}
    </div>
    <nav class="sidebar-nav" aria-label="主导航">${navHtml}</nav>
    ${meetingExtras}
    <div class="sidebar-footer">
      <span id="auth-status" class="auth-status">授权检查中…</span>
      <div class="sidebar-footer-actions">
        ${accountBtn}
        ${inviteBtn}
        <a id="reauth-btn" class="btn btn-sm" href="#"><i class="ri ri-shield-user-line" aria-hidden="true"></i><span class="btn-label">飞书授权</span></a>
        <a id="logout-btn" class="btn btn-sm" href="#"><i class="ri ri-logout-box-r-line" aria-hidden="true"></i><span class="btn-label">退出</span></a>
      </div>
    </div>
    <div id="invite-toast" class="sidebar-toast hidden" role="status"></div>
  `;

  ensureSuperUnlockModal();
  ensureAccountModals();
  bindSuperGesture(root.querySelector("#sidebar-brand-role"));
  bindModeToggle(root);
  bindSuperModal(document);
  bindInvite(root);
  bindAccount(root);
}

function ensureSuperUnlockModal() {
  // 挂到 body，避免侧栏 overflow/层叠上下文把弹窗压到主区按钮下面
  let modal = document.getElementById("super-unlock-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "super-unlock-modal";
    modal.className = "modal hidden";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.innerHTML = `
      <div class="modal-backdrop" data-close-super></div>
      <div class="modal-card">
        <h3 class="modal-title">解锁超级管理端</h3>
        <p class="modal-msg">输入超级管理员口令以查看全站数据（只读）。</p>
        <label class="field field-wide">
          <span>超级管理员 Token</span>
          <input id="super-token-input" type="password" autocomplete="off" />
        </label>
        <p id="super-unlock-error" class="login-error hidden"></p>
        <div class="modal-actions">
          <button type="button" class="btn" data-close-super>取消</button>
          <button type="button" class="btn btn-primary" id="super-unlock-btn">解锁</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
  } else if (modal.parentElement !== document.body) {
    document.body.appendChild(modal);
  }
}

function bindSuperGesture(roleEl) {
  if (!roleEl) return;
  let phase = "idle"; // idle | counting10 | waitGap | counting3
  let count = 0;
  let lastClickAt = 0;
  let gapTimer = null;

  const reset = () => {
    phase = "idle";
    count = 0;
    lastClickAt = 0;
    if (gapTimer) {
      clearTimeout(gapTimer);
      gapTimer = null;
    }
  };

  roleEl.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const now = Date.now();

    if (phase === "idle" || phase === "counting10") {
      if (phase === "idle") {
        phase = "counting10";
        count = 0;
      }
      if (lastClickAt && now - lastClickAt > 1500) {
        reset();
        phase = "counting10";
      }
      count += 1;
      lastClickAt = now;
      if (count >= 10) {
        phase = "waitGap";
        count = 0;
        if (gapTimer) clearTimeout(gapTimer);
        gapTimer = setTimeout(() => {
          // 空档已满 1s，等待第二段 3 次
          phase = "counting3";
          count = 0;
          lastClickAt = 0;
        }, 1000);
      }
      return;
    }

    if (phase === "waitGap") {
      // 空档未满又点了，重置
      reset();
      return;
    }

    if (phase === "counting3") {
      if (lastClickAt && now - lastClickAt > 2000) {
        reset();
        return;
      }
      count += 1;
      lastClickAt = now;
      if (count >= 3) {
        reset();
        openSuperUnlockModal();
      }
    }
  });
}

function openSuperUnlockModal() {
  const modal = document.getElementById("super-unlock-modal");
  const err = document.getElementById("super-unlock-error");
  const input = document.getElementById("super-token-input");
  if (!modal) return;
  err?.classList.add("hidden");
  if (input) input.value = "";
  modal.classList.remove("hidden");
  input?.focus();
}

function closeSuperUnlockModal() {
  document.getElementById("super-unlock-modal")?.classList.add("hidden");
}

function bindSuperModal(root) {
  const scope = root === document ? document : root;
  scope.querySelectorAll("[data-close-super]").forEach((el) => {
    if (el.dataset.boundSuperClose) return;
    el.dataset.boundSuperClose = "1";
    el.addEventListener("click", closeSuperUnlockModal);
  });
  const btn = document.getElementById("super-unlock-btn");
  const input = document.getElementById("super-token-input");
  if (btn?.dataset.boundSuperUnlock) return;
  if (btn) btn.dataset.boundSuperUnlock = "1";
  const unlock = async () => {
    const err = document.getElementById("super-unlock-error");
    const token = (input?.value || "").trim();
    if (!token) {
      if (err) {
        err.textContent = "请输入超级管理员口令";
        err.classList.remove("hidden");
      }
      return;
    }
    const res = await fetch(`${API}/auth/super/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      if (err) {
        err.textContent = typeof data.detail === "string" ? data.detail : "口令错误";
        err.classList.remove("hidden");
      }
      return;
    }
    const data = await res.json();
    if (!data.token) {
      if (err) {
        err.textContent = "未返回超级管理员会话";
        err.classList.remove("hidden");
      }
      return;
    }
    setSuperJwt(data.token);
    setAdminViewMode("super");
    clearAccessTicket();
    location.reload();
  };
  btn?.addEventListener("click", unlock);
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") unlock();
  });
}

function bindModeToggle(root) {
  root.querySelectorAll("[data-view-mode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.getAttribute("data-view-mode");
      if (mode === "super" && !getSuperJwt()) return;
      setAdminViewMode(mode === "super" ? "super" : "user");
      clearAccessTicket();
      location.reload();
    });
  });
}

async function bindInvite(root) {
  const btn = root.querySelector("#invite-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const toast = root.querySelector("#invite-toast");
    try {
      const res = await apiFetch("/auth/invites", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : "生成失败");
      }
      const url = data.invite_url || "";
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(url);
      }
      if (toast) {
        toast.textContent = url ? `邀请链接已复制：${url}` : "已生成邀请码";
        toast.classList.remove("hidden");
        setTimeout(() => toast.classList.add("hidden"), 5000);
      } else {
        window.prompt("邀请链接（请复制）", url);
      }
    } catch (e) {
      if (toast) {
        toast.textContent = e.message || "生成邀请失败";
        toast.classList.remove("hidden");
        setTimeout(() => toast.classList.add("hidden"), 4000);
      } else {
        alert(e.message || "生成邀请失败");
      }
    }
  });
}

function ensureAccountModals() {
  if (!document.getElementById("display-name-modal")) {
    const nameModal = document.createElement("div");
    nameModal.id = "display-name-modal";
    nameModal.className = "modal hidden";
    nameModal.setAttribute("role", "dialog");
    nameModal.setAttribute("aria-modal", "true");
    nameModal.innerHTML = `
      <div class="modal-backdrop" data-close-display-name></div>
      <div class="modal-card">
        <h2>确认显示名</h2>
        <p class="modal-hint">这是你在平台上的显示名称（会议列表「所有者」等）。可保持飞书昵称或自行修改。</p>
        <label class="field field-wide">
          <span>显示名</span>
          <input id="display-name-input" type="text" maxlength="64" autocomplete="nickname" />
        </label>
        <p id="display-name-error" class="login-error hidden"></p>
        <div class="modal-actions">
          <button id="display-name-save" class="btn btn-primary" type="button">确认</button>
        </div>
      </div>`;
    document.body.appendChild(nameModal);
  }
  if (!document.getElementById("account-settings-modal")) {
    const acc = document.createElement("div");
    acc.id = "account-settings-modal";
    acc.className = "modal hidden";
    acc.setAttribute("role", "dialog");
    acc.setAttribute("aria-modal", "true");
    acc.innerHTML = `
      <div class="modal-backdrop" data-close-account></div>
      <div class="modal-card">
        <h2>账号设置</h2>
        <p id="account-feishu-meta" class="modal-hint"></p>
        <label class="field field-wide">
          <span>显示名</span>
          <input id="account-display-name-input" type="text" maxlength="64" autocomplete="nickname" />
        </label>
        <label class="field field-wide">
          <span>新密码（可选，至少 6 位）</span>
          <input id="account-password-input" type="password" autocomplete="new-password" placeholder="留空则不修改" />
        </label>
        <label id="account-old-password-field" class="field field-wide hidden">
          <span>原密码</span>
          <input id="account-old-password-input" type="password" autocomplete="current-password" />
        </label>
        <p id="account-settings-error" class="login-error hidden"></p>
        <div class="modal-actions">
          <button id="account-settings-save" class="btn btn-primary" type="button">保存</button>
          <button class="btn" type="button" data-close-account>取消</button>
        </div>
      </div>`;
    document.body.appendChild(acc);
  }
}

async function maybePromptDisplayName() {
  if (isSuperAdminView()) return;
  if (sessionStorage.getItem("setup_display_name") !== "1") return;
  const modal = document.getElementById("display-name-modal");
  const input = document.getElementById("display-name-input");
  const err = document.getElementById("display-name-error");
  if (!modal || !input) return;
  try {
    const res = await apiFetch("/auth/me");
    if (!res.ok) return;
    const me = await res.json();
    if (!me.needs_display_name_setup) {
      sessionStorage.removeItem("setup_display_name");
      return;
    }
    input.value = me.display_name || me.feishu_name || "";
    err?.classList.add("hidden");
    modal.classList.remove("hidden");
  } catch {
    /* ignore */
  }
}

function bindAccount(root) {
  const openBtn = root.querySelector("#account-btn");
  const nameModal = document.getElementById("display-name-modal");
  const accModal = document.getElementById("account-settings-modal");

  document.getElementById("display-name-save")?.addEventListener("click", async () => {
    const input = document.getElementById("display-name-input");
    const err = document.getElementById("display-name-error");
    const name = (input?.value || "").trim();
    if (!name) {
      if (err) {
        err.textContent = "请填写显示名";
        err.classList.remove("hidden");
      }
      return;
    }
    const res = await apiFetch("/auth/me", {
      method: "PATCH",
      body: JSON.stringify({ display_name: name }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (err) {
        err.textContent = typeof data.detail === "string" ? data.detail : "保存失败";
        err.classList.remove("hidden");
      }
      return;
    }
    sessionStorage.removeItem("setup_display_name");
    nameModal?.classList.add("hidden");
  });

  nameModal?.addEventListener("click", (e) => {
    // 首次确认不允许点遮罩跳过
    if (e.target.closest("[data-close-display-name]")) return;
  });

  openBtn?.addEventListener("click", async () => {
    const err = document.getElementById("account-settings-error");
    err?.classList.add("hidden");
    try {
      const res = await apiFetch("/auth/me");
      const me = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(me.detail || "加载账号失败");
      const meta = document.getElementById("account-feishu-meta");
      if (meta) {
        meta.textContent = me.feishu_bound
          ? `已绑定飞书${me.feishu_name ? `（${me.feishu_name}）` : ""} · 内部账号 ${me.username}`
          : `未绑定飞书 · 内部账号 ${me.username} · 可点侧栏「绑定飞书」`;
      }
      const nameInput = document.getElementById("account-display-name-input");
      if (nameInput) nameInput.value = me.display_name || "";
      const pwd = document.getElementById("account-password-input");
      const oldField = document.getElementById("account-old-password-field");
      const oldInput = document.getElementById("account-old-password-input");
      if (pwd) pwd.value = "";
      if (oldInput) oldInput.value = "";
      oldField?.classList.toggle("hidden", !me.has_password);
      accModal?.classList.remove("hidden");
      accModal.dataset.hasPassword = me.has_password ? "1" : "0";
    } catch (e) {
      alert(e.message || "加载账号失败");
    }
  });

  document.getElementById("account-settings-save")?.addEventListener("click", async () => {
    const err = document.getElementById("account-settings-error");
    err?.classList.add("hidden");
    const display_name = (
      document.getElementById("account-display-name-input")?.value || ""
    ).trim();
    if (!display_name) {
      if (err) {
        err.textContent = "显示名不能为空";
        err.classList.remove("hidden");
      }
      return;
    }
    try {
      let res = await apiFetch("/auth/me", {
        method: "PATCH",
        body: JSON.stringify({ display_name }),
      });
      let data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : "保存显示名失败");
      }
      const password = (
        document.getElementById("account-password-input")?.value || ""
      ).trim();
      if (password) {
        const body = { password };
        if (accModal?.dataset.hasPassword === "1") {
          body.old_password =
            document.getElementById("account-old-password-input")?.value || "";
        }
        res = await apiFetch("/auth/me/password", {
          method: "POST",
          body: JSON.stringify(body),
        });
        data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(typeof data.detail === "string" ? data.detail : "设置密码失败");
        }
      }
      sessionStorage.removeItem("setup_display_name");
      accModal?.classList.add("hidden");
    } catch (e) {
      if (err) {
        err.textContent = e.message || "保存失败";
        err.classList.remove("hidden");
      }
    }
  });

  accModal?.addEventListener("click", (e) => {
    if (e.target.closest("[data-close-account]")) {
      accModal.classList.add("hidden");
    }
  });
}

mountAdminSidebar();
