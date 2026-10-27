"use strict";

// Shared navigation for all four workspaces. Keep destinations and labels here.
(() => {
  const navigation = document.querySelector(".app-navigation");
  if (!navigation) return;
  const page = document.body.dataset.page;
  const icons = {
    groups: '<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 5a3 3 0 0 1 0 6M18 15a5 5 0 0 1 3 5"/>',
    messages: '<path d="M21 11a9 9 0 0 1-9 9H4l-2 2V11a9 9 0 0 1 19 0Z"/><path d="M7 9h9M7 13h6"/>',
    private: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/>',
    media: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m10 8 6 4-6 4V8Z"/>',
    blacklist: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m9 12 2 2 4-4"/>',
    models: '<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 1v4m6-4v4M9 19v4m6-4v4M1 9h4m-4 6h4m14-6h4m-4 6h4M9 9h6v6H9Z"/>',
    connection: '<path d="M8 3v5m8-5v5M6 8h12v3a6 6 0 0 1-12 0V8Zm6 9v4"/>',
    docs: '<path d="M5 3h10l4 4v14H5V3Zm9 0v5h5M8 12h8m-8 4h6"/>'
  };
  const symbol = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name]}</svg>`;
  const item = (key, label, href, id = "") => `<a class="app-nav-link${page === key ? " selected" : ""}" href="${href}"${id ? ` id="${id}"` : ""}${page === key ? ' aria-current="page"' : ""}>${symbol(key)}<span>${label}</span></a>`;
  navigation.innerHTML = `
    <div class="app-nav-heading"><a class="app-brand" href="/" aria-label="wxhook 工作台"><span class="app-brand-icon">${symbol("messages")}</span><span>wxhook<small>微信工作台</small></span></a><button class="app-menu-toggle" type="button" aria-expanded="false" aria-controls="app-system-nav">设置与帮助</button></div>
    <div class="app-nav-section"><p class="app-nav-caption">工作空间</p><div class="app-work-links">
      ${item("groups", "群聊管理", "/", "nav-groups")}
      ${item("private", "私聊问答", "/private")}
      ${item("messages", "微信消息", "/messages")}
      ${item("media", "分享解析", "/douyin")}
    </div></div>
    <div class="app-nav-section app-system-nav" id="app-system-nav"><p class="app-nav-caption">管理与设置</p>
      ${item("blacklist", "黑名单", "/#blacklist", "nav-blacklist")}
      ${item("models", "AI 模型", "/#models", "ai-model-settings")}
      ${item("connection", "连接设置", "/#connection", "connection-settings")}
      <a class="app-nav-link" href="/docs" target="_blank" rel="noopener">${symbol("docs")}<span>接口文档 ↗</span></a>
    </div>
    <div class="app-nav-footer"><span class="app-local-dot"></span>本地工作台<span>WXHOOK</span></div>`;
  const toggle = navigation.querySelector(".app-menu-toggle");
  const setMenu = open => {
    navigation.classList.toggle("menu-open", open);
    toggle.setAttribute("aria-expanded", String(open));
  };
  toggle.addEventListener("click", () => setMenu(toggle.getAttribute("aria-expanded") !== "true"));
  navigation.addEventListener("click", event => { if (event.target.closest("a")) setMenu(false); });
  navigation.addEventListener("keydown", event => {
    if (event.key === "Escape") { setMenu(false); toggle.focus(); }
  });

  // Resource results and configuration have separate, linkable views.
  const tabs = [...document.querySelectorAll("[data-page-tab]")];
  if (tabs.length) {
    const activate = () => {
      const selected = tabs.find(tab => tab.hash === location.hash) || tabs[0];
      tabs.forEach(tab => {
        const active = tab === selected;
        tab.classList.toggle("active", active);
        if (active) tab.setAttribute("aria-current", "page");
        else tab.removeAttribute("aria-current");
        document.getElementById(tab.dataset.pageTab).hidden = !active;
      });
    };
    window.addEventListener("hashchange", activate);
    activate();
  }
})();
