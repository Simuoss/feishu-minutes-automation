/**
 * 管理端左侧栏：会议列表 / 分享 / 密钥；会议详情额外挂载成本与脱敏区。
 * 页面通过 body[data-admin-page] 声明当前项：meetings | meeting | shares | keys
 */
function mountAdminSidebar() {
  const root = document.getElementById("admin-sidebar");
  if (!root) return;

  const page = document.body.dataset.adminPage || "meetings";
  const isMeeting = page === "meeting";

  const navItems = [
    { id: "meetings", href: "/", icon: "ri-list-unordered", label: "会议列表" },
    { id: "shares", href: "/shares.html", icon: "ri-share-forward-line", label: "分享管理" },
    { id: "keys", href: "/keys.html", icon: "ri-key-2-line", label: "密钥管理" },
  ];

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
        <h3 class="sidebar-section-title">脱敏复核</h3>
        <button id="refresh-redaction-btn" class="btn btn-sm" type="button" title="刷新">
          <i class="ri ri-refresh-line" aria-hidden="true"></i>
        </button>
      </div>
      <div id="redaction-panel" class="redaction-panel sidebar-redaction hidden">
        <p id="redaction-summary" class="redaction-summary"></p>
        <div id="redaction-list" class="redaction-list"></div>
      </div>
      <p id="sidebar-redaction-empty" class="sidebar-empty">带图纪要脱敏后可在此复核。</p>
    </section>`
    : "";

  root.innerHTML = `
    <div class="sidebar-brand">
      <a href="/" class="sidebar-brand-link">
        <strong>飞书妙记</strong>
        <span>管理端</span>
      </a>
    </div>
    <nav class="sidebar-nav" aria-label="主导航">${navHtml}</nav>
    ${meetingExtras}
    <div class="sidebar-footer">
      <span id="auth-status" class="auth-status">授权检查中…</span>
      <div class="sidebar-footer-actions">
        <a id="reauth-btn" class="btn btn-sm" href="#"><i class="ri ri-shield-user-line" aria-hidden="true"></i><span class="btn-label">飞书授权</span></a>
        <a id="logout-btn" class="btn btn-sm" href="#"><i class="ri ri-logout-box-r-line" aria-hidden="true"></i><span class="btn-label">退出</span></a>
      </div>
    </div>
  `;
}

mountAdminSidebar();
