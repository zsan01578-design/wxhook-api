"use strict";

const $ = id => document.getElementById(id);
const state = { groups: [], blocked_members: [], startup: null, sync: null, media: null, callbacks: null };
let selectedId = "", tab = "messages", view = "groups", filter = "all", blacklistTab = "groups";
let members = [], messages = [], audit = [], memberQuery = "", detailVersion = "", listVersion = "";
let refreshJob, pollTimer, toastTimer, detailToken = 0, mutating = false, connected = false;
let messagePage = 1, messageQuery = "", messageMeta = {total: 0, has_more: false};
let ownWxid = "", resetChatScroll = true;
const chatSignatures = new WeakMap();

function el(tag, className = "", text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function button(text, className, action, iconName) {
  const node = el("button", className);
  node.type = "button";
  if (iconName) node.append(icon(iconName));
  if (text) node.append(document.createTextNode(text));
  if (action) node.addEventListener("click", action);
  return node;
}
function iconButton(name, label, action) {
  const node = button("", "icon-button", action, name);
  node.title = label;
  node.setAttribute("aria-label", label);
  return node;
}
function toast(text, error = false) {
  clearTimeout(toastTimer);
  $("toast").textContent = text;
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => $("toast").hidden = true, error ? 6500 : 3000);
}
async function api(path, body, method = "POST") {
  const options = { cache: "no-store", signal: AbortSignal.timeout(15000) };
  if (body !== undefined) Object.assign(options, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    let message = data.error?.message || data.detail || `请求失败（${response.status}）`;
    if (Array.isArray(message) || data.error?.code === "validation_error") message = "请检查填写内容：群 ID 须以 @chatroom 结尾，成员须填写真实 wxid，名称最多 80 字。";
    throw new Error(message);
  }
  return data;
}
const groupPath = id => `/api/manage/groups/${encodeURIComponent(id)}`;
const memberPath = (group, member) => `${groupPath(group)}/members/${encodeURIComponent(member)}`;
const current = () => state.groups.find(group => group.id === selectedId);
const nameOf = group => group.name || group.id;
const robotEnabled = group => Boolean(group.active && group.douyin_enabled);
const robotStatus = group => group.blocked ? "已拉黑" : robotEnabled(group) ? "机器人已启用" : "机器人未启用";
function dateText(value, short = false) {
  if (!value) return "暂无消息";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", short ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false } : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function avatar(group, large = false) {
  const node = el("div", `group-avatar${large ? " large" : ""}`);
  node.setAttribute("aria-hidden", "true");
  const texts = Array.from(group.name || "群聊管理");
  const seed = Array.from(group.id).reduce((value, char) => value + char.charCodeAt(0), 0);
  const tones = ["tone-1", "tone-2", "tone-3", ""];
  for (let i = 0; i < 4; i++) node.append(el("span", `member-avatar ${tones[(seed + i) % 4]}`, texts[i % texts.length]));
  return node;
}
function memberAvatar(member, i = 0) { return el("div", `member-avatar tone-${i % 4}`, Array.from(member.name || member.id.replace(/^wxid_/, ""))[0] || "群"); }
function badge(text, kind = "") { return el("span", `badge ${kind}`, text); }
function empty(title, description, iconName = "chat") {
  const node = el("div", "empty-content");
  node.append(icon(iconName), el("h3", "", title), el("p", "", description));
  return node;
}

async function mutation(source, path, body, text, method = "PATCH") {
  if (mutating) return false;
  mutating = true;
  if (source) source.disabled = true;
  try {
    await api(path, body, method);
    await refreshState(true);
    toast(text);
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  } finally {
    mutating = false;
    if (source?.isConnected) source.disabled = false;
  }
}
function toggleSwitch(label, checked, disabled, onChange) {
  const node = button("", "switch");
  node.setAttribute("role", "switch");
  node.setAttribute("aria-label", label);
  node.setAttribute("aria-checked", String(checked));
  node.disabled = disabled;
  node.addEventListener("click", () => onChange(node));
  return node;
}

function renderOverview() {
  const data = [
    ["users", state.groups.length, "管理群聊"],
    ["chat", state.groups.filter(robotEnabled).length, "已启用机器人"],
    ["video", state.groups.filter(group => !robotEnabled(group) && !group.blocked).length, "未启用机器人"],
    ["shield", state.groups.filter(group => group.blocked).length + state.blocked_members.length, "黑名单对象"]
  ];
  $("overview").replaceChildren(...data.map(([symbol, value, label]) => {
    const node = el("div", "stat"), art = el("div", "stat-icon"), copy = el("div");
    art.append(icon(symbol)); copy.append(el("strong", "", String(value).padStart(2, "0")), el("p", "", label)); node.append(art, copy); return node;
  }));
  $("group-total").textContent = `${state.groups.length} 个群聊`;
}
function renderList() {
  const query = $("group-search").value.trim().toLowerCase();
  const rows = state.groups.filter(group => (!query || `${group.name} ${group.id} ${group.note}`.toLowerCase().includes(query)) &&
    (filter === "all" || (filter === "active" ? robotEnabled(group) : filter === "inactive" ? !robotEnabled(group) && !group.blocked : group.blocked)))
    .sort((left, right) => Number(robotEnabled(right)) - Number(robotEnabled(left)));
  const nodes = rows.map(group => {
    const enabled = robotEnabled(group);
    const node = button("", `group-item ${group.blocked ? "robot-blocked" : enabled ? "robot-on" : "robot-off"}${selectedId === group.id && view === "groups" ? " selected" : ""}`, () => selectGroup(group.id));
    node.setAttribute("aria-pressed", String(selectedId === group.id && view === "groups"));
    const copy = el("div", "group-item-copy"), title = el("div", "group-item-title"), meta = el("div", "group-item-meta"), status = el("span");
    title.append(el("strong", "", nameOf(group)));
    if (group.pinned) title.append(icon("pin"));
    status.className = "robot-state";
    status.append(el("span", `tiny-dot${group.blocked ? " red" : !enabled ? " gray" : ""}`), document.createTextNode(robotStatus(group)));
    meta.append(status, el("span", "", group.members_sync === "complete" ? `${group.current_member_count} 位群成员` : `${group.member_count} 位已发现成员`));
    copy.append(title, el("p", "", group.last_message || group.note || "等待群内第一条消息"), meta);
    node.append(avatar(group), copy); return node;
  });
  $("group-list").replaceChildren(...(nodes.length ? nodes : [el("div", "list-empty", state.groups.length ? "没有符合条件的群聊" : "还没有群聊\n点击 + 添加，或等待群消息自动发现")]));
}
function selectGroup(id) {
  selectedId = id; tab = "messages"; memberQuery = ""; messagePage = 1; messageQuery = ""; view = "groups"; detailVersion = ""; resetChatScroll = true;
  document.body.classList.add("mobile-detail");
  updateView(); renderList();
  renderHeader(current());
  $("tab-content").replaceChildren(empty("正在载入群聊", "请稍候…", "clock"));
  $("inspector").replaceChildren();
  loadDetail().catch(error => toast(error.message, true));
}
function updateView() {
  const blacklist = view === "blacklist";
  $("page-title").textContent = blacklist ? "黑名单管理" : "群聊管理";
  $("nav-groups").classList.toggle("selected", !blacklist);
  $("nav-blacklist").classList.toggle("selected", blacklist);
  $("nav-groups").setAttribute("aria-current", blacklist ? "false" : "page");
  $("nav-blacklist").setAttribute("aria-current", blacklist ? "page" : "false");
  $("group-workspace").hidden = blacklist || !current();
  $("welcome").hidden = blacklist || Boolean(current());
  $("blacklist-view").hidden = !blacklist;
  document.querySelector(".workspace").classList.toggle("chat-selected", !blacklist && Boolean(current()));
  $("overview").hidden = !blacklist && Boolean(current());
  updateChatMode();
  if (blacklist) renderBlacklist();
  else if (!current()) renderWelcome();
}
function renderWelcome(error) {
  const node = $("welcome"), art = el("div", "welcome-icon"); art.append(icon("users"));
  const steps = el("div", "welcome-steps");
  ["登录微信自动同步", "按群配置功能", "查看持久化聊天记录"].forEach((text, i) => { const item = el("span"); item.append(el("b", "", i + 1), document.createTextNode(text)); steps.append(item); });
  node.replaceChildren(art, el("h2", "", error ? "暂时无法载入群聊" : "你的群聊，从这里管理"), el("p", "", error || "微信登录后会自动同步群聊、可读取的成员名单和聊天记录，也可以手动添加群聊。"), button(error ? "重新加载" : "添加群聊", "button primary", error ? () => refreshState(true) : addGroupDialog, error ? "refresh" : "plus"), steps);
}

function renderHeader(group) {
  const copy = el("div", "group-heading-copy"), title = el("div", "group-heading-title"), id = el("p", "group-id"), tools = el("div", "header-tools");
  title.append(el("h2", "", nameOf(group)), badge(robotStatus(group), group.blocked ? "red" : !robotEnabled(group) ? "gray" : ""));
  id.append(document.createTextNode(group.id), iconButton("info", "群 ID 说明", () => infoDialog("群 ID", "这是微信中群聊的唯一标识。管理系统中的群名称和备注仅保存在本机，不会修改微信群名称。")));
  copy.append(title, id);
  const pin = iconButton("pin", group.pinned ? "取消置顶" : "置顶群聊", event => mutation(event.currentTarget, groupPath(group.id), { pinned: !group.pinned }, group.pinned ? "已取消置顶" : "群聊已置顶"));
  pin.setAttribute("aria-pressed", String(group.pinned));
  pin.title = group.pinned ? "取消在同类群聊中的置顶" : "在相同机器人状态的群聊中置顶";
  tools.append(pin, iconButton("edit", "编辑群资料", () => editGroupDialog(group)));
  $("group-header").replaceChildren(avatar(group, true), copy, tools);
  $("member-tab-count").textContent = group.member_count;
  $("restriction-banner").hidden = group.active;
  $("restriction-banner").textContent = group.blocked ? "此群已加入黑名单，所有自动功能均已停止。移出黑名单后恢复原有配置。" : "此群的自动功能已暂停，打开群功能总开关即可恢复。";
  document.querySelectorAll("[data-tab]").forEach(node => { node.classList.toggle("active", node.dataset.tab === tab); node.setAttribute("aria-current", node.dataset.tab === tab ? "page" : "false"); });
  updateChatMode();
}

function updateChatMode() {
  const chatting = view === "groups" && Boolean(current()) && tab === "messages";
  $("group-workspace").classList.toggle("chatting", chatting);
  $("tab-content").classList.toggle("chat-content", chatting);
}
function renderFeatures(group) {
  const content = $("tab-content"), heading = el("div", "section-heading"), copy = el("div");
  copy.append(el("h3", "", "群功能设置"), el("p", "", "按群开启所需功能，打造各自的使用方式。"));
  heading.append(copy, el("span", "section-kicker", "独立配置 / 即时生效"));
  const master = el("div", "master-setting"), masterCopy = el("div");
  masterCopy.append(el("h3", "", "群功能总开关"), el("p", "", "关闭后暂停此群全部自动功能，保留下面的设置。"));
  master.append(masterCopy, toggleSwitch("群功能总开关", group.enabled, group.blocked, source => mutation(source, groupPath(group.id), { enabled: !group.enabled }, group.enabled ? "已暂停此群自动功能" : "已恢复此群自动功能")));
  const list = el("div", "feature-list");
  const features = [
    ["video", "分享解析", "自动识别群内抖音、小红书、B站分享，解析视频与图集。", "douyin_enabled", !group.active],
    ["download", "自动保存视频与图片", "解析成功后保存到本机，并用于自动回传到来源群。", "auto_download", !group.active || !group.douyin_enabled]
  ];
  features.forEach(([symbol, title, description, key, disabled]) => {
    const row = el("div", "feature-row"), art = el("div", "feature-icon"), text = el("div", "feature-copy");
    art.append(icon(symbol)); text.append(el("h3", "", title), el("p", "", description));
    row.append(art, text, toggleSwitch(title, group[key], disabled, source => mutation(source, groupPath(group.id), { [key]: !group[key] }, `${title}已${group[key] ? "关闭" : "开启"}`))); list.append(row);
  });
  const note = el("div", "feature-footnote"); note.append(icon("info"), el("span", "", "回传媒体需要启用自动保存。群内处理提示和回传开关在“资源”页统一配置；关闭自动保存后只保留解析链接。"));
  const runtime = el("div", "feature-footnote"), media = state.media;
  if (media) {
    const provider = {parse_video: "parse-video-py（抖音 / 小红书 / B站）", api: "指定接口（仅抖音）", builtin: "公开分享页（仅抖音）"}[media.parser_provider] || media.parser_provider;
    const warnings = [];
    if (!media.feedback_available) warnings.push("群内发送总开关已关闭，无法发送提示或媒体");
    if (!media.notify_group) warnings.push("开始、失败和完成提示已关闭");
    if (!media.send_results) warnings.push("媒体自动回传已关闭");
    runtime.append(icon("info"), el("span", "", `当前解析来源：${provider}。${warnings.length ? warnings.join("；") + "。" : "群内提示和媒体回传已开启。"}`));
    const resourceLink = el("a", "text-link", "查看资源与解析记录"); resourceLink.href = "/douyin"; runtime.append(resourceLink);
  }
  const safety = el("div", "safety-section"), safetyCopy = el("div"), safetyTitle = el("h3");
  safetyTitle.append(icon("shield"), document.createTextNode("群聊黑名单"));
  safetyCopy.append(safetyTitle, el("p", "", "拉黑后不再处理此群的自动功能，消息仍可记录。"));
  safety.append(safetyCopy, button(group.blocked ? "移出黑名单" : "加入黑名单", `button ${group.blocked ? "" : "danger"} small`, () => blockGroupDialog(group), group.blocked ? "check" : "block"));
  content.replaceChildren(heading, master, list, note, ...(media ? [runtime] : []), safety);
}
function renderInspector(group) {
  const node = $("inspector"), heading = el("div", "inspector-heading");
  heading.append(el("h3", "", "群聊资料"), iconButton("edit", "编辑群资料", () => editGroupDialog(group)));
  const lines = [["机器人状态", group.blocked ? "已拉黑" : robotEnabled(group) ? "已启用" : "未启用"], [group.members_sync === "complete" ? "微信当前群成员" : "已发现成员", `${group.members_sync === "complete" ? group.current_member_count : group.member_count} 人`], ["成员黑名单", `${group.blocked_member_count} 人`], ["已保存消息", `${group.message_count} 条`], ["最近活跃", dateText(group.last_seen, true)]];
  const rows = lines.map(([label, value]) => { const row = el("div", "info-line"); row.append(el("span", "", label), el("strong", "", value)); return row; });
  const noteLabel = el("div", "note-label"); noteLabel.append(el("span", "", "群备注"), button("编辑", "text-link", () => editGroupDialog(group)));
  const memberSection = el("div", "inspector-members"), memberHeading = el("div", "inspector-heading");
  memberHeading.append(el("h3", "", "群成员"), button("查看全部", "text-link", () => setTab("members"), "arrow")); memberSection.append(memberHeading);
  if (!members.length) memberSection.append(el("p", "inspector-hint", "等待群成员发言后自动发现，也可以手动添加成员。"));
  members.slice(0, 5).forEach((member, i) => {
    const row = el("div", "member-preview"), copy = el("div", "member-preview-copy");
    copy.append(el("strong", "", member.name || member.id), el("p", "", member.blocked ? "已加入黑名单" : member.last_seen ? "已发现成员" : "手动添加"));
    row.append(memberAvatar(member, i), copy, iconButton(member.blocked ? "check" : "block", member.blocked ? `解除 ${member.name || member.id} 黑名单` : `拉黑 ${member.name || member.id}`, () => blockMemberDialog(group.id, member))); memberSection.append(row);
  });
  memberSection.append(el("p", "inspector-hint", group.members_sync === "complete" ? "已同步微信当前成员关系，离群成员的历史记录和本地设置会保留。" : "尚未取得完整微信成员关系，此处为已发现或手动添加的成员。"));
  const tip = el("div", "inspector-tip"); tip.append(icon("shield"), el("strong", "", "让群聊保持有序"), el("p", "", "黑名单只停止本系统的自动处理，不会退群、踢人或屏蔽微信消息。成员黑名单仅在所在群生效。"));
  const shortcuts = el("div", "inspector-actions");
  shortcuts.append(button("功能配置", "button small", () => setTab("features"), "settings"), button("群黑名单", "button small", () => blockGroupDialog(group), "shield"));
  node.replaceChildren(heading, shortcuts, ...rows, noteLabel, el("p", "group-note", group.note || "还没有备注，记录这个群的用途吧。"), memberSection, tip);
}
function renderMembers(group) {
  const heading = el("div", "section-heading"), copy = el("div");
  copy.append(el("h3", "", "群成员管理"), el("p", "", group.members_sync === "complete" ? "已同步微信成员名单，保留历史成员与本地添加记录；黑名单仅在当前群生效。" : "暂未取得完整微信成员关系，展示已发现与手动添加的成员。")); heading.append(copy);
  const tools = el("div", "member-toolbar"), search = el("div", "search-field"), input = el("input");
  input.type = "search"; input.placeholder = "搜索名称或 wxid"; input.setAttribute("aria-label", "搜索群成员"); input.value = memberQuery;
  input.addEventListener("input", () => { memberQuery = input.value; renderMemberTable(group); });
  search.append(icon("search"), input); tools.append(search, button("添加成员", "button small", () => addMemberDialog(group), "plus"));
  const container = el("div"); container.id = "member-table-container";
  $("tab-content").replaceChildren(heading, tools, container); renderMemberTable(group);
}
function renderMemberTable(group) {
  const query = memberQuery.trim().toLowerCase(), filtered = members.filter(member => `${member.id} ${member.name}`.toLowerCase().includes(query));
  if (!filtered.length) { $("member-table-container").replaceChildren(empty(members.length ? "没有匹配的成员" : "等待成员加入管理", "可手动添加成员，也可等待群成员发送消息后自动发现。", "users")); return; }
  const table = el("table", "member-table"), head = el("thead"), headRow = el("tr"), body = el("tbody");
  ["成员", "状态", "最近发言", "操作"].forEach((label, i) => headRow.append(el("th", i === 2 ? "last-active" : "", label))); head.append(headRow);
  filtered.forEach((member, i) => {
    const row = el("tr"), nameCell = el("td"), person = el("div", "member-cell"), copy = el("div");
    copy.append(el("div", "member-name", member.name || member.id), el("div", "member-id", `${member.id}${member.membership === "left" ? " · 历史成员" : member.membership === "current" ? " · 群内成员" : ""}`)); person.append(memberAvatar(member, i), copy); nameCell.append(person);
    const status = el("td"); status.append(badge(member.blocked ? "已拉黑" : "正常", member.blocked ? "red" : "gray"));
    const actions = el("td"), buttons = el("div", "member-actions"); buttons.append(button("备注", "", () => editMemberDialog(group, member)), button(member.blocked ? "解除" : "拉黑", "block", () => blockMemberDialog(group.id, member))); actions.append(buttons);
    row.append(nameCell, status, el("td", "last-active member-id", dateText(member.last_seen, true)), actions); body.append(row);
  });
  table.append(head, body); $("member-table-container").replaceChildren(table);
}
function renderMessages() {
  const group = current();
  let log = $("chat-log");
  if (!log || log.dataset.group !== group.id) {
    const toolbar = el("div", "chat-toolbar"), search = el("form", "message-search"), input = el("input", "form-input");
    input.type = "search"; input.placeholder = "搜索聊天记录"; input.value = messageQuery; input.maxLength = 200; input.setAttribute("aria-label", "搜索聊天记录");
    const submit = button("", "icon-button", null, "search"); submit.type = "submit"; submit.setAttribute("aria-label", "搜索");
    search.append(input, submit);
    search.addEventListener("submit", event => { event.preventDefault(); messageQuery = input.value.trim(); messagePage = 1; detailVersion = ""; resetChatScroll = true; loadDetail().catch(error => toast(error.message, true)); });
    const pager = el("div", "message-pager"), prev = button("", "icon-button", () => turnMessagePage(-1), "back"), next = button("", "icon-button", () => turnMessagePage(1), "arrow");
    prev.id = "chat-newer"; prev.title = "较新的消息"; prev.setAttribute("aria-label", "上一页");
    next.id = "chat-older"; next.title = "更早的消息"; next.setAttribute("aria-label", "下一页");
    const pageLabel = el("span"); pageLabel.id = "chat-page"; pager.append(prev, pageLabel, next);
    const count = el("span", "chat-count"); count.id = "chat-count"; toolbar.append(search, count, pager);
    const notice = el("p", "chat-history-notice"); notice.id = "chat-history-notice";
    const viewport = el("div", "chat-viewport");
    log = el("div", "chat-log"); log.id = "chat-log"; log.dataset.group = group.id; log.setAttribute("role", "log"); log.setAttribute("aria-label", "群聊消息");
    const jump = button("回到最新消息", "chat-jump", latestMessages, "chat"); jump.id = "chat-jump"; jump.hidden = true;
    log.addEventListener("scroll", () => { jump.hidden = messagePage === 1 && !messageQuery && log.scrollHeight - log.clientHeight - log.scrollTop < 80; });
    viewport.append(log, jump); $("tab-content").replaceChildren(toolbar, notice, viewport);
    resetChatScroll = true;
  }
  $("chat-newer").disabled = messagePage <= 1;
  $("chat-older").disabled = !messageMeta.has_more;
  $("chat-page").textContent = `第 ${messagePage} 页`;
  $("chat-count").textContent = `${messageMeta.total} 条${messageQuery ? "匹配记录" : "消息"}`;
  const notice = $("chat-history-notice");
  notice.hidden = !group.history_error && group.history_complete;
  notice.textContent = group.history_error || "正在同步历史消息，可先查看已保存的记录。";
  notice.classList.toggle("chat-error", Boolean(group.history_error));
  const signature = JSON.stringify([messages, members.map(person => [person.id, person.name, person.blocked]), messagePage, messageQuery, ownWxid]);
  if (chatSignatures.get(log) === signature && !resetChatScroll) return;
  const oldTop = log.scrollTop, oldHeight = log.scrollHeight, follow = resetChatScroll || oldHeight - log.clientHeight - oldTop < 80;
  chatSignatures.set(log, signature);
  const stack = el("div", "message-stack");
  let lastTimestamp = 0;
  [...messages].reverse().forEach((message, i) => {
    if (!i || message.timestamp - lastTimestamp > 300) stack.append(el("div", "chat-time-divider", dateText(new Date(message.timestamp * 1000).toISOString(), true)));
    lastTimestamp = message.timestamp;
    const person = members.find(member => member.id === message.sender) || { id: message.sender || "未知成员" };
    const self = Boolean(ownWxid) && message.sender === ownWxid;
    const row = el("div", `chat-message${self ? " is-self" : ""}`), body = el("div", "chat-message-body"), meta = el("div", "chat-meta");
    meta.append(el("span", "", self ? "我" : person.name || person.id), el("time", "", new Date(message.timestamp * 1000).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false})));
    if (person.blocked) meta.append(badge("已拉黑", "red"));
    body.append(meta, el("div", "chat-bubble", message.content || "（空消息）"));
    if (message.type !== 1 || message.truncated) body.append(el("p", "form-note", `${message.type !== 1 ? `类型 ${message.type} · 展示原始内容` : ""}${message.truncated ? " · 内容过长已截断" : ""}`));
    row.append(memberAvatar(person, i), body); stack.append(row);
  });
  log.replaceChildren(messages.length ? stack : empty(messageQuery ? "没有匹配的聊天记录" : "还没有聊天消息", messageQuery ? "换个关键词试试。" : "群消息同步后会显示在这里。"));
  resetChatScroll = false;
  requestAnimationFrame(() => {
    if (!log.isConnected || log.dataset.group !== selectedId) return;
    log.scrollTop = follow ? log.scrollHeight : Math.max(0, oldTop + log.scrollHeight - oldHeight);
    $("chat-jump").hidden = messagePage === 1 && !messageQuery && follow;
  });
}
function turnMessagePage(delta) { messagePage += delta; detailVersion = ""; resetChatScroll = true; loadDetail().catch(error => toast(error.message, true)); }
function latestMessages() { messagePage = 1; messageQuery = ""; detailVersion = ""; resetChatScroll = true; const input = document.querySelector(".message-search input"); if (input) input.value = ""; loadDetail().catch(error => toast(error.message, true)); }
function renderAudit() {
  const heading = el("div", "section-heading"), copy = el("div"); copy.append(el("h3", "", "操作记录"), el("p", "", "记录当前群的配置调整与黑名单操作，最近 100 条。")); heading.append(copy);
  const rows = audit.map(event => { const node = el("div", "audit-row"); node.append(el("div", "", event.action), el("p", "", event.target), el("time", "", dateText(event.created_at))); return node; });
  $("tab-content").replaceChildren(heading, ...(rows.length ? rows : [empty("还没有操作记录", "修改功能或管理黑名单后，操作会记录在这里。", "clock")]));
}
function setTab(value) { if (tab !== value) resetChatScroll = true; tab = value; detailVersion = ""; updateChatMode(); loadDetail().catch(error => toast(error.message, true)); }
async function loadDetail() {
  const group = current();
  if (!group || view !== "groups") return;
  const token = ++detailToken, requestedTab = tab, requestedPage = messagePage, requestedQuery = messageQuery;
  const [people, inbox, history] = await Promise.all([
    api(`${groupPath(group.id)}/members`),
    requestedTab === "messages" ? api(`${groupPath(group.id)}/messages?page=${requestedPage}&q=${encodeURIComponent(requestedQuery)}`) : Promise.resolve(null),
    requestedTab === "audit" ? api(`/api/manage/audit?group=${encodeURIComponent(group.id)}`) : Promise.resolve(null)
  ]);
  if (token !== detailToken || group.id !== selectedId || requestedTab !== tab || view !== "groups") return;
  members = people.members;
  messages = inbox?.messages || [];
  if (inbox) messageMeta = inbox;
  audit = history?.events || [];
  const version = JSON.stringify([current(), members, messages, messageMeta.total, requestedPage, requestedQuery, audit, tab, state.media, ownWxid]);
  if (version === detailVersion) return;
  detailVersion = version;
  renderHeader(current()); renderInspector(current());
  if (tab === "features") renderFeatures(current());
  else if (tab === "members") renderMembers(current());
  else if (tab === "messages") renderMessages();
  else renderAudit();
}

function renderBlacklist() {
  const root = $("blacklist-view"), heading = el("div", "section-heading"), copy = el("div");
  copy.append(el("h3", "", "黑名单"), el("p", "", "统一管理群聊与群成员的自动功能访问权限。")); heading.append(copy);
  const tabs = el("div", "blacklist-tabs"), blockedGroups = state.groups.filter(group => group.blocked);
  tabs.append(button(`群聊黑名单 ${blockedGroups.length}`, blacklistTab === "groups" ? "active" : "", () => { blacklistTab = "groups"; renderBlacklist(); }), button(`成员黑名单 ${state.blocked_members.length}`, blacklistTab === "members" ? "active" : "", () => { blacklistTab = "members"; renderBlacklist(); }));
  const rows = blacklistTab === "groups" ? blockedGroups : state.blocked_members;
  const cards = rows.map(item => {
    const node = el("div", "blacklist-card"), copy = el("div", "blacklist-card-copy");
    copy.append(el("h3", "", item.name || item.id), el("p", "", blacklistTab === "groups" ? item.id : `所在群：${item.group_name || item.group_id}`));
    const art = blacklistTab === "groups" ? avatar(item) : memberAvatar(item);
    node.append(art, copy, button("移出黑名单", "button small", () => blacklistTab === "groups" ? blockGroupDialog(item) : blockMemberDialog(item.group_id, item))); return node;
  });
  root.replaceChildren(heading, tabs, ...(cards.length ? cards : [empty("黑名单是空的", "在群聊的功能配置或成员列表中，可将对象加入黑名单。", "shield")]), el("p", "form-note", "黑名单仅停止本系统自动处理，不会退出微信群、踢出成员或屏蔽微信消息。成员黑名单仅在对应群生效。"));
}

function showDialog(title, eyebrow = "群聊管理") {
  $("modal-title").textContent = title; $("modal-eyebrow").textContent = eyebrow; $("modal-body").replaceChildren();
  if (!$("modal").open) $("modal").showModal();
  return $("modal-body");
}
function formField(form, label, placeholder, value = "", multiline = false, max = 80) {
  const id = `field-${Math.random().toString(36).slice(2)}`, caption = el("label", "form-label", label), input = el(multiline ? "textarea" : "input", "form-input");
  input.id = id; input.maxLength = max; input.value = value; input.placeholder = placeholder; input.autocomplete = "off"; caption.htmlFor = id;
  if (multiline) input.rows = 4;
  form.append(caption, input); return input;
}
function formFooter(form, label = "保存") {
  const error = el("p", "form-error"); error.setAttribute("role", "alert");
  const actions = el("div", "form-actions"), submit = button(label, "button primary"); submit.type = "submit";
  actions.append(button("取消", "button", () => $("modal").close()), submit); form.append(error, actions); return { error, submit };
}
function bindForm(form, footer, save) {
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (footer.submit.disabled || mutating) return;
    mutating = true; footer.submit.disabled = true; footer.error.textContent = "";
    let saved = false;
    try { await save(); saved = true; $("modal").close(); await refreshState(true); toast("已保存，配置立即生效"); }
    catch (error) { if (saved) toast(`配置已保存，刷新失败：${error.message}`, true); else footer.error.textContent = error.message; }
    finally { mutating = false; footer.submit.disabled = false; }
  });
}
function infoDialog(title, description) {
  const body = showDialog(title); body.append(el("p", "modal-copy", description)); const actions = el("div", "form-actions"); actions.append(button("知道了", "button primary", () => $("modal").close())); body.append(actions);
}
function confirmDialog(title, description, save, label = "确认", danger = false) {
  const body = showDialog(title, "访问管理"), form = el("form"); form.append(el("p", "modal-copy", description));
  const footer = formFooter(form, label); if (danger) footer.submit.className = "button danger"; body.append(form); bindForm(form, footer, save);
}
function blockGroupDialog(group) {
  confirmDialog(group.blocked ? "移出群聊黑名单？" : "将群聊加入黑名单？", `${nameOf(group)}\n\n${group.blocked ? "解除后将按照此前的功能配置恢复自动处理。" : "此群内全部成员将停止触发本系统的自动功能。原有配置会保留，群消息仍可记录。此操作不会退出微信群。"}`, () => api(groupPath(group.id), { blocked: !group.blocked }, "PATCH"), group.blocked ? "确认解除" : "确认拉黑", !group.blocked);
}
function blockMemberDialog(group, member) {
  confirmDialog(member.blocked ? "解除成员黑名单？" : "将成员加入黑名单？", `${member.name || member.id}\n\n${member.blocked ? "解除后，此成员可再次触发所在群已开启的自动功能。" : "此成员在当前群内将无法触发自动功能；在其他群不受影响。此操作不会踢出成员。"}`, () => api(memberPath(group, member.id), { blocked: !member.blocked }, "PATCH"), member.blocked ? "确认解除" : "确认拉黑", !member.blocked);
}
function editGroupDialog(group) {
  const body = showDialog("编辑群聊资料"), form = el("form"), name = formField(form, "群名称", group.wechat_name || "填写便于识别的群名称", group.local_name ?? group.name), note = formField(form, "群备注", "记录群用途或管理说明", group.note, true, 500);
  form.append(el("p", "form-note", "名称和备注仅保存在本机。群名称留空时自动使用同步到的微信名称。"));
  const footer = formFooter(form); body.append(form); bindForm(form, footer, () => api(groupPath(group.id), { name: name.value.trim(), note: note.value.trim() }, "PATCH"));
}
function addMemberDialog(group) {
  const body = showDialog("添加群成员"), form = el("form"), id = formField(form, "成员 wxid", "wxid_xxxxxxxxxx", "", false, 256), name = formField(form, "成员备注（选填）", "为成员设置易读的名称"); id.required = true;
  form.append(el("p", "form-note", `添加到「${nameOf(group)}」的本地管理名单，不会邀请用户加入微信群。`));
  const footer = formFooter(form, "添加成员"); body.append(form); bindForm(form, footer, () => api(`${groupPath(group.id)}/members`, { id: id.value.trim(), name: name.value.trim() }));
}
function editMemberDialog(group, member) {
  const body = showDialog("编辑成员备注"), form = el("form"), name = formField(form, "备注名称", member.wechat_name || "成员备注，仅保存在本机", member.local_name ?? member.name);
  form.append(el("p", "form-note", member.id)); const footer = formFooter(form); body.append(form); bindForm(form, footer, () => api(memberPath(group.id, member.id), { name: name.value.trim() }, "PATCH"));
}
function addGroupDialog(mode = "manual") {
  const body = showDialog("添加群聊"), modes = el("div", "dialog-mode");
  modes.append(button("手动添加", mode === "manual" ? "active" : "", () => addGroupDialog("manual")), button("从微信查找", mode === "search" ? "active" : "", () => addGroupDialog("search"))); body.append(modes);
  if (mode === "search") {
    const form = el("form", "import-search"), input = el("input", "form-input"), submit = button("查找", "button primary"), results = el("div");
    input.required = true; input.maxLength = 80; input.placeholder = "输入群名称或群 ID"; input.setAttribute("aria-label", "查找微信群聊"); submit.type = "submit";
    form.append(input, submit); body.append(form, el("p", "form-note", "从已登录微信的联系人中查找，最多返回 20 条匹配联系人中的群聊。新群也可等待消息自动发现。"), results);
    form.addEventListener("submit", async event => {
      event.preventDefault(); submit.disabled = true; results.replaceChildren(el("p", "form-note", "正在查询微信联系人…"));
      try {
        const data = await api(`/api/manage/contacts?keyword=${encodeURIComponent(input.value.trim())}`);
        const rows = data.groups.map(group => {
          const row = el("div", "import-result"), copy = el("div"); copy.append(el("h3", "", group.name || group.id), el("p", "", group.id));
          const exists = state.groups.some(item => item.id === group.id), add = button(exists ? "已添加" : "添加", "button small", async event => {
            if (await mutation(event.currentTarget, "/api/manage/groups", group, "群聊已添加", "POST")) { add.textContent = "已添加"; add.disabled = true; }
          }); add.disabled = exists; row.append(copy, add); return row;
        }); results.replaceChildren(...(rows.length ? rows : [el("p", "form-note", "没有找到匹配群聊。可更换关键词，或使用群 ID 手动添加。") ]));
      } catch (error) { results.replaceChildren(el("p", "form-error", error.message)); }
      finally { submit.disabled = false; }
    }); return;
  }
  const form = el("form"), id = formField(form, "群 ID", "例如 123456789@chatroom", "", false, 140), name = formField(form, "群名称（选填）", "例如：产品交流群"); id.required = true; id.pattern = "[A-Za-z0-9_\\-]{1,128}@chatroom";
  form.append(el("p", "form-note", "请填写真实群 ID，不能填写群昵称。添加到本地管理名单后，可单独开启功能；不会创建新的微信群。"));
  const footer = formFooter(form, "添加群聊"); body.append(form); bindForm(form, footer, async () => { const data = await api("/api/manage/groups", { id: id.value.trim(), name: name.value.trim() }); selectedId = data.id; tab = "messages"; memberQuery = ""; messagePage = 1; messageQuery = ""; resetChatScroll = true; view = "groups"; detailVersion = ""; document.body.classList.add("mobile-detail"); });
}

async function checkConnection() {
  try {
    const data = await api("/QueryDB/status");
    connected = String(data.IsLogin ?? data.data?.IsLogin) === "1";
    $("connection-dot").className = `live-dot${connected ? "" : " error"}`;
    $("connection-label").textContent = connected ? "微信已连接" : "等待微信登录";
    if (connected) {
      let identity = data.currentWxid || data.data?.currentWxid || "";
      if (!identity) {
        try { const profile = await api("/GetSelfProfile", {}); identity = profile.data?.wxid || profile.wxid || ""; } catch { /* Identity is optional for bubble alignment. */ }
      }
      if (typeof identity === "string" && identity && identity !== ownWxid) { ownWxid = identity; detailVersion = ""; }
    }
  } catch {
    connected = false; $("connection-dot").className = "live-dot error"; $("connection-label").textContent = "微信未连接";
  }
}

function connectionDialog() {
  const body = showDialog("连接与消息接收", "本机服务"), status = el("p", "modal-copy", $("connection-label").textContent), startup = el("p", "form-note", state.startup?.message || "启动 main.py 后，在原版微信中完成登录。群消息通过 Hook 回调自动接收。"), result = el("p", "form-note"), actions = el("div", "form-actions");
  actions.append(button("检查连接", "button", async event => { const source = event.currentTarget; source.disabled = true; await checkConnection(); status.textContent = $("connection-label").textContent; try { callbackInfo(received, await api("/api/callback/status")); } catch (error) { received.textContent = `读取回调诊断失败：${error.message}`; } source.disabled = false; }), button("启用消息接收", "button primary", async event => {
    const source = event.currentTarget; source.disabled = true; result.textContent = "正在设置回调…";
    try { const data = await api("/set_callback", { url: `http://127.0.0.1:${location.port || (location.protocol === "https:" ? "443" : "80")}/receiveChatBotMsg` }); if (String(data.ret) !== "0") throw new Error(data.retmsg || "Hook 未确认启用成功"); result.textContent = "Hook 已确认设置回调地址。请让目标群发送新消息，再点“检查连接”查看接收与拒收情况。"; }
    catch (error) { result.textContent = `启用失败：${error.message}`; }
    finally { source.disabled = false; }
  }));
  const callbacks = state.callbacks;
  const received = el("p", "form-note");
  callbackInfo(received, callbacks);
  body.append(status, startup, received, result, actions);
}

function callbackInfo(node, data) {
  if (!data) { node.textContent = "请更新并重启服务后查看实时回调接收状态。"; return; }
  let text = `本次启动：接收成功 ${data.accepted ?? data.total ?? 0} 条，校验拒收 ${data.rejected ?? 0} 条。`;
  if (data.last_accepted_at || data.last_received_at) text += ` 最近接收：${dateText(data.last_accepted_at || data.last_received_at)}。`;
  if (data.last_rejection) {
    const detail = data.last_rejection.fields.map(item => `${item.field}：${item.error === "missing" ? "缺少字段" : `类型或格式不符（${item.input_type} / ${item.error}）`}`).join("；");
    text += ` 最近拒收 HTTP ${data.last_rejection.http_status}：${detail}。`;
  } else if (!(data.accepted ?? data.total)) text += " 尚未验证到实时消息，请在群里发新消息后检查。";
  node.textContent = text;
}

async function refreshState(force = false) {
  if (refreshJob) { await refreshJob; if (!force) return; }
  refreshJob = (async () => {
    try {
      Object.assign(state, await api("/api/manage/state"));
      if (!selectedId || !current()) selectedId = (state.groups.find(robotEnabled) || state.groups[0])?.id || "";
      const version = JSON.stringify(state.groups);
      if (version !== listVersion) { listVersion = version; renderList(); }
      const sync = state.sync;
      $("sync-status").textContent = sync?.message || "等待微信自动同步…";
      $("sync-status").title = (sync?.warnings || []).join("\n");
      $("sync-phase").className = `live-dot${sync?.phase === "error" || sync?.phase === "partial" ? " error" : sync?.phase === "idle" ? "" : " muted"}`;
      $("sync-now").disabled = sync?.enabled === false;
      renderOverview(); updateView(); await loadDetail();
    } catch (error) {
      if (!listVersion) { $("welcome").hidden = false; $("group-workspace").hidden = true; renderWelcome(error.message); const retry = el("div", "list-empty", "群聊载入失败"); retry.append(document.createElement("br"), button("重新加载", "button small", () => refreshState(true))); $("group-list").replaceChildren(retry); }
      else if (force) toast(`刷新失败：${error.message}`, true);
      $("connection-label").textContent = "数据刷新失败"; $("connection-dot").className = "live-dot error";
    }
  })();
  try { await refreshJob; } finally { refreshJob = null; }
}
async function poll() {
  clearTimeout(pollTimer);
  if (document.hidden) return;
  await refreshState();
  if (!document.hidden) pollTimer = setTimeout(poll, 3000);
}
$("group-search").addEventListener("input", renderList);
document.querySelectorAll("[data-filter]").forEach(node => node.addEventListener("click", () => {
  filter = node.dataset.filter; document.querySelectorAll("[data-filter]").forEach(item => { item.classList.toggle("active", item === node); item.setAttribute("aria-pressed", String(item === node)); }); renderList();
}));
document.querySelectorAll("[data-tab]").forEach(node => node.addEventListener("click", () => setTab(node.dataset.tab)));
$("add-group").addEventListener("click", () => addGroupDialog());
$("add-group-small").addEventListener("click", () => addGroupDialog());
$("refresh").addEventListener("click", async event => { const source = event.currentTarget; source.disabled = true; await Promise.all([refreshState(true), checkConnection()]); source.disabled = false; });
$("sync-now").addEventListener("click", async event => { const source = event.currentTarget; source.disabled = true; try { await api("/api/manage/sync", {}); toast("已安排后台同步，进度会自动更新"); } catch (error) { toast(error.message, true); } finally { source.disabled = false; } });
$("nav-groups").addEventListener("click", () => { view = "groups"; document.body.classList.remove("mobile-detail"); updateView(); renderList(); loadDetail().catch(error => toast(error.message, true)); });
$("nav-blacklist").addEventListener("click", () => { view = "blacklist"; document.body.classList.add("mobile-detail"); updateView(); renderList(); });
$("back-to-list").addEventListener("click", () => { view = "groups"; document.body.classList.remove("mobile-detail"); updateView(); renderList(); });
$("connection-settings").addEventListener("click", connectionDialog);
$("connection-indicator").addEventListener("click", connectionDialog);
$("close-modal").addEventListener("click", () => $("modal").close());
$("modal").addEventListener("click", event => { if (event.target === $("modal")) { const rect = $("modal").getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) $("modal").close(); } });
document.addEventListener("visibilitychange", () => { clearTimeout(pollTimer); if (!document.hidden) { poll(); checkConnection(); } });
renderOverview(); checkConnection(); poll();
