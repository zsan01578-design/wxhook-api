"use strict";

const $ = id => document.getElementById(id);
const state = { groups: [], blocked_members: [], startup: null, sync: null, media: null, callbacks: null };
let selectedId = "", tab = "messages", view = "groups", filter = "all", blacklistTab = "groups";
let members = [], messages = [], audit = [], memberQuery = "", detailVersion = "", listVersion = "";
let refreshJob, pollTimer, toastTimer, detailToken = 0, mutating = false, connected = false;
let messagePage = 1, messageQuery = "", messageMeta = {total: 0, has_more: false};
let ownWxid = "", resetChatScroll = true;
let modelSaving = false;
let dialogVersion = 0;
let wordCloud = null, chatSummary = null, chatRadar = null;
let insightRequest = null;
let groupTools = null, radarDays = 14;
let taskView = "open", taskPage = 1;
let favoritesData = null, favoriteQuery = "", favoritePage = 1, favoriteSearch = null;
const chatSignatures = new WeakMap();
const taskCandidates = new Map();

function el(tag, className = "", text) {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function formatBytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "未知";
  if (number < 1024) return `${number} B`;
  if (number < 1024 ** 2) return `${(number / 1024).toFixed(1)} KB`;
  if (number < 1024 ** 3) return `${(number / 1024 ** 2).toFixed(1)} MB`;
  return `${(number / 1024 ** 3).toFixed(2)} GB`;
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
async function api(path, body, method = "POST", timeout = 15000) {
  const options = { cache: "no-store", signal: AbortSignal.timeout(timeout) };
  if (body !== undefined) Object.assign(options, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    let message = data.error?.message || data.detail || `请求失败（${response.status}）`;
    if (path.includes("/favorites") && (Array.isArray(message) || data.error?.code === "validation_error")) message = "请检查链接和标题；最多 12 个标签，每个不超过 24 字，备注最多 1000 字。";
    else if (path.includes("/insights/") && (Array.isArray(message) || data.error?.code === "validation_error")) message = "请检查梗关键词、时间范围和消息编号；漫画最多100条，爬楼最多200条。";
    else if (path.includes("/tools") && (Array.isArray(message) || data.error?.code === "validation_error")) message = "请检查必填内容、日期与冷却时间，固定回复规则需要填写回复内容。";
    else if (Array.isArray(message) || (data.error?.code === "validation_error" && !["/api/manage/chat-model", "/api/manage/vision-model"].some(prefix => path.startsWith(prefix)) && path !== "/api/manage/chatbot/mentions")) message = "请检查填写内容：群 ID 须以 @chatroom 结尾，成员须填写真实 wxid，名称最多 80 字。";
    throw new Error(message);
  }
  return data;
}
const groupPath = id => `/api/manage/groups/${encodeURIComponent(id)}`;
const memberPath = (group, member) => `${groupPath(group)}/members/${encodeURIComponent(member)}`;
const current = () => state.groups.find(group => group.id === selectedId);
const nameOf = group => group.name || group.id;
const robotEnabled = group => Boolean(group.active && (group.douyin_enabled || group.chat_enabled || group.tools_enabled));
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
  favoriteQuery = ""; favoritePage = 1; favoriteSearch = null; favoritesData = null;
  taskView = "open"; taskPage = 1;
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
  $("restriction-banner").textContent = state.chatbot?.account_error ? "检测到微信账号变化，全部自动功能已暂停。请切回原账号后重启；新账号须使用独立资料库。" : group.blocked ? "此群已加入黑名单，所有自动功能均已停止。移出黑名单后恢复原有配置。" : "此群的自动功能已暂停，打开群功能总开关即可恢复。";
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
    ["chat", "@机器人问答", "只有被 @ 才回复；每条提问最多一条消息，不连续发言。", "chat_enabled", !group.active],
    ["chat", "自动 AI 日总结", "每天 18:00 后将本群文本样本发给当前模型；需同时开启 @问答。默认关闭。", "summary_enabled", !group.active],
    ["video", "分享解析", "自动识别群内抖音、小红书、B站分享，解析视频与图集。", "douyin_enabled", !group.active],
    ["download", "自动保存视频与图片", "解析成功后保存到本机，并用于自动回传到来源群。", "auto_download", !group.active || !group.douyin_enabled]
  ];
  features.forEach(([symbol, title, description, key, disabled]) => {
    const row = el("div", "feature-row"), art = el("div", "feature-icon"), text = el("div", "feature-copy");
    art.append(icon(symbol)); text.append(el("h3", "", title), el("p", "", description));
    row.append(art, text, toggleSwitch(title, group[key], disabled, source => mutation(source, groupPath(group.id), { [key]: !group[key] }, `${title}已${group[key] ? "关闭" : "开启"}`))); list.append(row);
  });
  const note = el("div", "feature-footnote"); note.append(icon("info"), el("span", "", "回传媒体需要启用自动保存。群内处理提示和回传开关在“资源”页统一配置；关闭自动保存后只保留解析链接。"));
  const chatNote = el("div", "feature-footnote");
  const chat = state.chatbot;
  const chatStatus = chat?.enabled === false ? "@问答总开关已关闭。" : `${chat?.answer_configured ? "文字问答接口已接入。" : "普通文字问答尚未配置。"}${chat?.image_configured ? "生图已接入，可发送 @机器人 生图：描述。" : "可在 AI 模型中添加生图模型。"}`;
  chatNote.append(icon("info"), el("span", "", `${chatStatus}${chat?.last_error ? ` ${chat.last_error}` : ""}`), button("配置 AI 模型", "text-link", modelSettingsDialog));
  const diagnostic = el("div", "model-test-result");
  const recent = chat?.recent_requests?.find(item => item.group_id === group.id) || chat?.recent_events?.find(item => item.group_id === group.id);
  const knownNames = chat?.mention_names?.length ? chat.mention_names : recent?.mention_names;
  const names = knownNames?.length ? knownNames.join("、") : "尚未识别";
  const route = {image: "生图模型", chat: "文字问答 / 群工具", image_intent: "正在理解生图需求", image_clarification: "生图需求待补充，已停止生成", image_intent_error: "生图意图识别失败，已停止生成", image_mention_unresolved: "绘图请求的 @位置无法确认，已停止调用模型"}[recent?.model_route];
  const version = state.runtime?.revision ? `服务版本：${state.runtime.revision.slice(0, 12)} · 图片路由：${chat?.image_routing_version || "旧版，请更新重启"}` : "当前服务未返回版本信息，请运行一键更新重启。";
  const intentSource = {semantic: "对话模型提取", command: "直接生图指令", keyword: "关键词识别"}[recent?.image_intent_source];
  diagnostic.append(el("p", "", `当前识别的机器人称呼：${names}\n${version}\n${recent ? `最近实时消息：${dateText(recent.received_at)}\n${route ? `处理分支：${route}\n` : ""}${intentSource ? `图片需求：${intentSource}，描述 ${recent.image_prompt_chars} 字\n` : ""}处理状态：${recent.message}` : "本次启动尚未收到此群的实时消息。历史同步显示的消息不会触发问答。"}`), button("配置机器人群内显示名", "button small", mentionSettingsDialog));
  const runtime = el("div", "feature-footnote"), media = state.media;
  if (media) {
    const provider = {parse_video: "parse-video-py（抖音 / 小红书 / B站）", api: "指定接口（仅抖音）", builtin: "公开分享页（仅抖音）"}[media.parser_provider] || media.parser_provider;
    const warnings = [];
    if (!media.feedback_available) warnings.push("群内发送总开关已关闭，无法发送提示或媒体");
    if (!media.notify_group) warnings.push("开始和失败提示已关闭");
    if (!media.send_results) warnings.push("媒体自动回传已关闭");
    runtime.append(icon("info"), el("span", "", `当前解析来源：${provider}。${warnings.length ? warnings.join("；") + "。" : "群内提示和媒体回传已开启。"}`));
    const resourceLink = el("a", "text-link", "查看资源与解析记录"); resourceLink.href = "/douyin"; runtime.append(resourceLink);
  }
  const safety = el("div", "safety-section"), safetyCopy = el("div"), safetyTitle = el("h3");
  safetyTitle.append(icon("shield"), document.createTextNode("群聊黑名单"));
  safetyCopy.append(safetyTitle, el("p", "", "拉黑后不再处理此群的自动功能，消息仍可记录。"));
  safety.append(safetyCopy, button(group.blocked ? "移出黑名单" : "加入黑名单", `button ${group.blocked ? "" : "danger"} small`, () => blockGroupDialog(group), group.blocked ? "check" : "block"));
  content.replaceChildren(heading, master, list, chatNote, diagnostic, note, ...(media ? [runtime] : []), safety);
}
function renderInspector(group) {
  const node = $("inspector"), heading = el("div", "inspector-heading");
  heading.append(el("h3", "", "群聊资料"), iconButton("edit", "编辑群资料", () => editGroupDialog(group)));
  const lines = [["机器人状态", group.blocked ? "已拉黑" : robotEnabled(group) ? "已启用" : "未启用"], [group.members_sync === "complete" ? "微信当前群成员" : "已发现成员", `${group.members_sync === "complete" ? group.current_member_count : group.member_count} 人`], ["成员黑名单", `${group.blocked_member_count} 人`], ["已保存消息", `${group.message_count} 条`], ["最近活跃", dateText(group.last_seen, true)]];
  const rows = lines.map(([label, value]) => { const row = el("div", "info-line"); row.append(el("span", "", label), el("strong", "", value)); return row; });
  const noteLabel = el("div", "note-label"); noteLabel.append(el("span", "", "群备注"), button("编辑", "text-link", () => editGroupDialog(group)));
  const cloudSection = el("section", "inspector-word-cloud"), cloudHeading = el("div", "inspector-heading");
  cloudHeading.append(el("h3", "", "群聊词云")); cloudSection.append(cloudHeading, el("p", "inspector-hint", "约每 30 秒更新，可点击页面顶部刷新。"));
  if (wordCloud?.group_id !== group.id || wordCloud.error) {
    cloudSection.append(el("p", "inspector-hint", wordCloud?.error || "正在生成词云…"));
  } else if (!wordCloud.words.length) {
    cloudSection.append(el("p", "word-cloud-empty", "暂无可展示的词语"), el("p", "inspector-hint", "群内文字消息积累后，会自动生成词云。"));
  } else {
    cloudSection.append(renderWordCloud(wordCloud.words), el("p", "inspector-hint", `最近 ${wordCloud.sampled_messages} 条成员文本消息（不含机器人） · 字越大，出现越多。点击词语查看相关消息。`));
  }
  const summarySection = el("section", "inspector-summary"), summaryHeading = el("div", "inspector-heading"), summaryActions = el("div", "summary-actions");
  summaryHeading.append(el("h3", "", "今日群聊总结"));
  const refreshSummary = button("刷新总结", "button small", async event => {
    const source = event.currentTarget; source.disabled = true; source.textContent = "生成中…";
    try {
      const result = await api(`${groupPath(group.id)}/summary`, {}, "POST", 60000);
      chatSummary = result.summary; detailVersion = ""; renderInspector(group); toast("群聊总结已刷新");
    } catch (error) { toast(error.message, true); }
    finally { if (source.isConnected) { source.disabled = false; source.textContent = "刷新总结"; } }
  });
  summaryActions.append(refreshSummary); summaryHeading.append(summaryActions); summarySection.append(summaryHeading);
  if (chatSummary?.group_id !== group.id || !chatSummary) {
    summarySection.append(el("p", "inspector-hint", group.summary_enabled ? "自动总结已开启；群功能与 @问答开启时，18:00 后生成。" : "自动总结已关闭。点击刷新将本群文本样本发送给当前模型。"));
  } else {
    summarySection.append(el("div", "chat-summary-text", chatSummary.summary), el("p", "inspector-hint", `${chatSummary.message_count} 条成员消息 · 更新于 ${dateText(chatSummary.updated_at, true)}`));
  }
  const radarSection = renderRadar(chatRadar, group);
  const tip = el("div", "inspector-tip"); tip.append(icon("shield"), el("strong", "", "让群聊保持有序"), el("p", "", "黑名单只停止本系统的自动处理，不会退群、踢人或屏蔽微信消息。成员黑名单仅在所在群生效。"));
  const shortcuts = el("div", "inspector-actions");
  shortcuts.append(button("功能配置", "button small", () => setTab("features"), "settings"), button("群黑名单", "button small", () => blockGroupDialog(group), "shield"));
  node.replaceChildren(heading, shortcuts, ...rows, noteLabel, el("p", "group-note", group.note || "还没有备注，记录这个群的用途吧。"), cloudSection, radarSection, summarySection, tip);
}
function renderRadar(radar, group) {
  const section = el("section", "inspector-radar"), heading = el("div", "inspector-heading");
  heading.append(el("h3", "", "群聊雷达")); section.append(heading, el("p", "inspector-hint", "约每 30 秒更新，可点击页面顶部刷新。"));
  if (!radar || radar.group_id !== group.id || radar.error) {
    section.append(el("p", "inspector-hint", radar?.error || "正在统计群聊活动…"));
    return section;
  }
  const peak = radar.peak_hour?.count ? `${String(radar.peak_hour.hour).padStart(2, "0")}:00` : "暂无";
  const day = radar.peak_day?.count ? `${radar.peak_day.date.slice(5)} · ${radar.peak_day.count} 条` : "暂无";
  const stats = el("div", "radar-stats");
  [[radar.messages, "消息"], [radar.active_members, "活跃成员"], [peak, "高峰时段"], [day, "最活跃日"]].forEach(([value, label]) => { const item = el("div", "radar-stat"); item.append(el("strong", "", String(value)), el("span", "", label)); stats.append(item); });
  const chart = el("div", "radar-chart");
  const max = Math.max(1, ...radar.daily.map(item => item.count));
  radar.daily.forEach(item => { const bar = el("div", "radar-bar-wrap"), barValue = el("span", "radar-value", item.count ? String(item.count) : ""); bar.title = `${item.date} · ${item.count} 条消息 · ${item.active_members} 位成员`; bar.setAttribute("aria-label", bar.title); bar.style.setProperty("--bar-ratio", item.count / max); const barFill = el("div", "radar-bar"); bar.append(barValue, barFill, el("small", "", item.date.slice(5))); chart.append(bar); });
  const people = el("div", "radar-people");
  radar.top_members.slice(0, 5).forEach((person, index) => { const row = el("div", "radar-person"); row.append(memberAvatar(person, index), el("span", "", person.name || person.id), el("strong", "", `${person.count} 条`)); people.append(row); });
  if (!radar.top_members.length) people.append(el("p", "inspector-hint", "暂无成员发言"));
  const words = radar.words.length ? radar.words.map(word => `${word.text} ${word.count}`).join(" · ") : "暂无热门词";
  section.append(stats, el("p", "radar-label", "每日消息量"), chart, el("p", "radar-label", "活跃成员 TOP 5"), people, el("p", "inspector-hint", `热门词：${words} · 统计 ${radar.from} 至 ${radar.to}`));
  return section;
}
function renderRadarPage(group) {
  const root = $("tab-content"), heading = el("div", "section-heading"), copy = el("div"), periods = el("div", "tool-actions");
  copy.append(el("h3", "", "群聊雷达"), el("p", "", "按北京时间统计本机已归档记录，排除机器人消息；热门词取最近 1000 条成员文本。"));
  [7, 14, 30].forEach(days => { const control = button(`${days} 天`, `button small${days === radarDays ? " primary" : ""}`, () => { radarDays = days; detailVersion = ""; loadDetail().catch(error => toast(error.message, true)); }); control.setAttribute("aria-pressed", String(days === radarDays)); periods.append(control); });
  heading.append(copy, periods);
  const chart = renderRadar(chatRadar, group); chart.classList.add("radar-expanded");
  const hourly = el("section", "tool-card"), cells = el("div", "radar-hours");
  hourly.append(el("h3", "", "24 小时活跃分布"), el("p", "tool-description", "颜色越深，累计消息越多。"));
  const max = Math.max(1, ...(chatRadar?.hourly || []).map(item => item.count));
  (chatRadar?.hourly || []).forEach(item => { const cell = el("div", "radar-hour"); cell.style.backgroundColor = `rgba(7, 173, 103, ${.06 + .6 * item.count / max})`; cell.title = `${item.hour}:00–${item.hour}:59 · ${item.count} 条`; cell.append(el("span", "", `${String(item.hour).padStart(2, "0")}:00`), el("strong", "", `${item.count} 条`)); cells.append(cell); });
  hourly.append(cells);
  root.replaceChildren(heading, chart, hourly);
}
const toolsPath = group => `${groupPath(group)}/tools`;
async function reloadTools(group) {
  const data = await api(toolsPath(group));
  if (selectedId === group && tab === "tools") { groupTools = data; detailVersion = ""; renderTools(current()); }
}
async function toolsAction(source, group, path, body, method = "POST", message = "已保存") {
  source.disabled = true;
  try { await api(`${toolsPath(group)}${path}`, body, method, 65000); await reloadTools(group); toast(message); }
  catch (error) { toast(error.message, true); }
  finally { if (source.isConnected) source.disabled = false; }
}
function toolCard(title, description) {
  const card = el("section", "tool-card"); card.append(el("h3", "", title), el("p", "tool-description", description)); return card;
}

const favoritesPath = group => `${groupPath(group)}/favorites`;
const favoriteFile = (group, item, file) => `${favoritesPath(group)}/${encodeURIComponent(item.id)}/files/${encodeURIComponent(file.name)}`;
async function loadFavorites(group) {
  const key = JSON.stringify([group, favoriteQuery, favoritePage]);
  if (favoriteSearch?.key === key && performance.now() < favoriteSearch.expires) return favoriteSearch.data;
  return api(`${favoritesPath(group)}?q=${encodeURIComponent(favoriteQuery)}&page=${favoritePage}`);
}
async function reloadFavorites(group) {
  favoriteSearch = null; detailVersion = "";
  if (selectedId === group && tab === "favorites") await loadDetail();
}
function favoriteDialog(group, item = {}) {
  const body = showDialog(item.id ? "编辑收藏" : "添加群收藏"), form = el("form");
  const text = item.id ? null : formField(form, "分享链接或分享文案", "粘贴一条分享内容", "", true, 8192);
  const title = formField(form, "收藏标题", item.id ? "填写标题" : "可留空，解析后自动填写", item.title || "", false, 220);
  const tags = formField(form, "标签（用逗号分隔）", "例如：美食, 红烧肉", (item.tags || []).join(", "), false, 320);
  const notes = formField(form, "收藏备注", "写下为什么想收藏它", item.notes || "", true, 1000);
  if (text) text.required = true; else title.required = true;
  if (!item.id) form.append(el("p", "form-note", "抖音、小红书和 B站会尝试保存文案与媒体；其他文章链接保存为书签。保存媒体可能需要几分钟。"));
  const footer = formFooter(form, item.id ? "保存修改" : "加入收藏"); body.append(form);
  bindForm(form, footer, async () => {
    const data = {title:title.value,tags:tags.value.split(/[,，\n]/).map(value => value.trim()).filter(Boolean),notes:notes.value};
    if (text) data.text = text.value;
    const result = await api(`${favoritesPath(group)}${item.id ? `/${item.id}` : ""}`, data, item.id ? "PUT" : "POST", 210000);
    favoriteSearch = null;
    if (result.item?.status === "partial") toast(result.item.error, true);
  });
}
function renderFavorites(group) {
  const root = $("tab-content"), data = favoritesData;
  if (!data || data.group_id !== group.id) { root.replaceChildren(empty("收藏暂时无法加载", "请刷新重试。", "folder")); return; }
  if (data.error) { root.replaceChildren(empty("收藏暂时无法加载", data.error, "folder")); return; }
  const focused = document.activeElement?.id === "favorite-query", draft = focused ? document.activeElement.value : favoriteQuery, selection = focused ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  const heading = el("div", "section-heading"), copy = el("div");
  copy.append(el("h3", "", "群收藏"), el("p", "", "让分享留下来，也让它更容易被找到。"));
  heading.append(copy, button("添加收藏", "button primary small", () => favoriteDialog(group.id), "plus"));
  const hint = el("p", "tool-description", "群里发送“收藏＋链接”，或发完分享后在 10 分钟内说“收藏”。查找时可以问“上次那个红烧肉教程在哪”。群内命令需要启用 @问答；资料仅在当前群内检索。"), search = el("form", "favorite-search"), input = el("input");
  input.id = "favorite-query"; input.type = "search"; input.maxLength = 300; input.value = draft;
  input.placeholder = "上次那个红烧肉教程在哪"; input.setAttribute("aria-label", "搜索群收藏");
  const submit = button("搜索", "button small primary"); submit.type = "submit";
  const smart = button("AI 扩展搜索", "button small", async event => {
    const query = input.value.trim(); if (!query) return;
    const source = event.currentTarget; source.disabled = true; source.textContent = "正在查找…";
    favoriteQuery = query; favoritePage = 1;
    const key = JSON.stringify([group.id, query, 1]);
    try {
      const result = await api(`${favoritesPath(group.id)}/search`, {query}, "POST", 25000);
      if (group.id !== selectedId || favoriteQuery !== query || tab !== "favorites") return;
      favoriteSearch = {key, data:result, expires:performance.now()+60000}; favoritesData = result; detailVersion = ""; renderFavorites(current());
    } catch (error) { toast(error.message, true); }
    finally { source.disabled = false; source.textContent = "AI 扩展搜索"; }
  });
  search.append(input, submit, smart);
  search.addEventListener("submit", event => { event.preventDefault(); favoriteQuery = input.value.trim(); favoritePage = 1; reloadFavorites(group.id).catch(error => toast(error.message, true)); });
  const meta = el("div", "favorite-result-meta", `${data.total} 条收藏${data.search_mode === "semantic" ? " · 已扩展同义关键词" : " · 本地关键词检索"}`);
  const grid = el("div", "favorite-grid");
  (data.items || []).forEach(item => {
    const card = el("article", "favorite-card"), preview = el("div", "favorite-preview"), files = item.files || [];
    const image = files.find(file => file.kind === "image"), video = files.find(file => file.kind === "video");
    if (image) { const node = el("img"); node.src = favoriteFile(group.id,item,image); node.alt = ""; node.loading = "lazy"; preview.append(node); }
    else preview.append(icon(video ? "video" : "folder"));
    const content = el("div", "favorite-copy"), tags = el("div", "favorite-tags"), actions = el("div", "tool-actions");
    content.append(el("h4", "", item.title), el("p", "favorite-meta", `${({douyin:"抖音",xiaohongshu:"小红书",bilibili:"B站",link:"链接书签"})[item.platform] || item.platform}${item.author ? ` · ${item.author}` : ""} · ${dateText(item.created_at,true)}`));
    (item.tags || []).forEach(tag => tags.append(el("span", "favorite-tag", tag))); content.append(tags);
    if (item.notes) content.append(el("p", "favorite-notes", item.notes));
    content.append(el("p", "favorite-meta", `${files.length} 个本地媒体 · ${item.status === "saving" ? "正在保存" : item.status === "partial" ? "媒体未完整保存" : "已收藏"}`));
    if (item.error) content.append(el("p", "favorite-warning", item.error));
    const source = el("a", "button small", "原始分享");
    try { const url = new URL(item.source_url); if (["http:","https:"].includes(url.protocol)) source.href = url.href; } catch {}
    source.target = "_blank"; source.rel = "noopener noreferrer";
    actions.append(source, button("查看", "button small", () => favoriteDetail(group.id,item)), button("编辑", "button small", () => favoriteDialog(group.id,item)));
    if (item.status === "partial") actions.append(button("重试媒体", "button small", async event => {
      const source = event.currentTarget; source.disabled = true;
      try { const result = await api(`${favoritesPath(group.id)}/${item.id}/retry`, {}, "POST", 210000); await reloadFavorites(group.id); toast(result.item.error || "媒体已保存", Boolean(result.item.error)); }
      catch (error) { toast(error.message,true); } finally { source.disabled = false; }
    }));
    actions.append(button("删除", "text-link", () => confirmDialog("删除这条收藏？", "同时删除这条收藏的本地媒体副本。原分享和聊天记录会保留。", async () => { await api(`${favoritesPath(group.id)}/${item.id}`, {}, "DELETE"); await reloadFavorites(group.id); }, "删除收藏", true)));
    content.append(actions); card.append(preview,content); grid.append(card);
  });
  if (!data.items?.length) grid.append(empty(favoriteQuery ? "没有找到相关收藏" : "把喜欢的分享收在这里", favoriteQuery ? "换个关键词，或尝试 AI 扩展搜索。" : "点击“添加收藏”，或者在群里发送“收藏＋链接”。", "folder"));
  const pages = el("div", "task-pagination"), previous = button("上一页", "button small", () => { favoritePage--; reloadFavorites(group.id).catch(error => toast(error.message,true)); }), next = button("下一页", "button small", () => { favoritePage++; reloadFavorites(group.id).catch(error => toast(error.message,true)); });
  previous.disabled = favoritePage <= 1; next.disabled = !data.has_more;
  pages.append(previous,el("span","",`第 ${favoritePage} 页`),next);
  root.replaceChildren(heading,hint,search,meta,grid,pages);
  if (focused) { input.focus(); try { input.setSelectionRange(...selection); } catch {} }
}
function favoriteDetail(group, item) {
  const body = showDialog(item.title, "群收藏"), description = el("p","tool-source",item.source_text || item.title);
  body.append(description);
  (item.files || []).forEach(file => {
    const url = favoriteFile(group,item,file), media = el(file.kind === "image" ? "img" : "video", "favorite-full-media");
    media.src = url;
    if (file.kind === "image") { media.alt = item.title; media.loading = "lazy"; }
    else { media.controls = true; media.preload = "metadata"; }
    const link = el("a","text-link",`下载 ${file.name}（${formatBytes(file.size)}）`); link.href = `${url}?download=true`;
    body.append(media,link);
  });
  if (!item.files?.length) body.append(el("p","form-note","此收藏没有本地媒体，请通过原始分享链接查看。"));
}
function taskDateText(timestamp) {
  if (!timestamp) return "未设置截止时间";
  return new Date(timestamp).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});
}
function updateTaskCountdowns() {
  if (document.hidden) return;
  const currentTime = Date.now();
  document.querySelectorAll("[data-task-countdown]").forEach(node => {
    const due = Number(node.dataset.taskCountdown);
    node.classList.toggle("overdue", Boolean(due && due <= currentTime));
    if (!due) { node.textContent = "未设置截止时间"; return; }
    const seconds = Math.ceil(Math.abs(due - currentTime) / 1000), days = Math.floor(seconds / 86400);
    const clock = [Math.floor(seconds % 86400 / 3600), Math.floor(seconds % 3600 / 60), seconds % 60].map(value => String(value).padStart(2, "0")).join(":");
    node.textContent = `${due > currentTime ? "剩余" : "已超时"} ${days ? `${days}天 ` : ""}${clock}`;
  });
}
function taskSection(group, data) {
  const section = el("section", "task-section"), heading = el("div", "task-heading"), actions = el("div", "tool-actions");
  heading.append(el("h3", "", "群待办"));
  actions.append(button("新增", "button small", () => taskDialog(group.id), "plus"), button(taskCandidates.get(group.id)?.length ? "继续核对待办" : "AI 提取待办", "button small primary", event => extractTaskDialog(event.currentTarget, group.id), "search"));
  heading.append(actions); section.append(heading);
  const remindLabel = el("label", "tool-check"), remind = el("input"); remind.type = "checkbox"; remind.checked = data.task_reminders;
  remind.addEventListener("change", event => toolsAction(event.currentTarget, group.id, "/preferences", {task_reminders: remind.checked}, "PATCH"));
  remindLabel.append(remind, document.createTextNode("每日汇总到期的手动 / AI 待办")); section.append(remindLabel);
  const tabs = el("div", "task-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "待办状态");
  [["open", "未完成"], ["done", "已完成"], ["cancelled", "已取消"]].forEach(([status, label]) => {
    const control = button("", `task-tab${taskView === status ? " active" : ""}`, () => { taskView = status; taskPage = 1; renderTools(group); });
    control.append(document.createTextNode(label), el("span", "task-count", String(data.tasks.filter(task => task.status === status).length)));
    control.setAttribute("role", "tab"); control.setAttribute("aria-selected", String(taskView === status)); tabs.append(control);
  });
  section.append(tabs);
  const filtered = data.tasks.filter(task => task.status === taskView), pageCount = Math.max(1, Math.ceil(filtered.length / 25));
  taskPage = Math.min(taskPage, pageCount);
  const list = el("div", "task-list"); list.setAttribute("role", "tabpanel"); list.setAttribute("aria-label", {open: "未完成待办", done: "已完成待办", cancelled: "已取消待办"}[taskView]);
  filtered.slice((taskPage - 1) * 25, taskPage * 25).forEach(task => {
    const row = el("div", "task-row"), copy = el("div", "task-copy"), title = el("div", "task-title"), controls = el("div", "task-controls");
    title.append(el("strong", "", task.title), badge({reminder: "群提醒", ai: "AI 提取", manual: "手动新增"}[task.source_kind] || "历史待办")); copy.append(title);
    const creator = task.creator_display_name || task.creator_name || "未知（历史记录）", meta = el("div", "task-meta");
    const creatorLabel = el("span", "", `创建者 ${creator}`); creatorLabel.title = task.creator_id || creator;
    meta.append(creatorLabel, el("span", "", `创建于 ${taskDateText(task.created_at)}`));
    if (task.owner) meta.append(el("span", "", `负责人 ${task.owner}`));
    copy.append(meta, el("p", "task-deadline", task.due_at == null ? "截止时间未设置" : `截止 ${taskDateText(task.due_at * 1000)}（北京时间）`));
    if (task.status === "done" && task.completed_at) copy.append(el("p", "task-deadline", `${task.completion_reason === "deadline" ? "到期自动完成于" : "完成于"} ${taskDateText(task.completed_at)}`));
    if (task.reminder_id) copy.append(el("p", "task-delivery", `提醒状态：${{pending: "待提醒", sending: "发送中", sent: "已提醒", cancelled: "已停止", unknown: "发送未确认", failed: "发送失败", skipped: "已跳过"}[task.reminder_status] || "待确认"}`));
    if (task.status === "open") {
      const countdown = el("span", "task-countdown"); countdown.dataset.taskCountdown = task.due_at == null ? "" : String(task.due_at * 1000); countdown.setAttribute("role", "timer");
      controls.append(countdown);
    }
    const commands = el("div", "task-commands");
    commands.append(iconButton("edit", "编辑待办", () => taskDialog(group.id, task)), iconButton(task.status === "open" ? "check" : "refresh", task.status === "open" ? "标记完成" : "重新打开", event => toolsAction(event.currentTarget, group.id, `/tasks/${task.id}`, {status: task.status === "open" ? "done" : "open"}, "PATCH")));
    if (task.status === "open") commands.append(iconButton("close", "取消待办", () => confirmDialog("取消这条待办？", task.title, () => api(`${toolsPath(group.id)}/tasks/${task.id}`, {status: "cancelled"}, "PATCH").then(() => reloadTools(group.id)), "取消待办", true)));
    controls.append(commands); row.append(copy, controls); list.append(row);
  });
  if (!filtered.length) list.append(el("p", "task-empty", {open: "暂无未完成待办", done: "暂无已完成待办", cancelled: "暂无已取消待办"}[taskView]));
  section.append(list);
  if (pageCount > 1) {
    const pages = el("div", "task-pagination"), previous = iconButton("back", "上一页待办", () => { taskPage--; renderTools(group); }), next = iconButton("arrow", "下一页待办", () => { taskPage++; renderTools(group); });
    previous.disabled = taskPage <= 1; next.disabled = taskPage >= pageCount;
    pages.append(el("span", "", `共 ${filtered.length} 条 · ${taskPage} / ${pageCount} 页`), previous, next); section.append(pages);
  }
  return section;
}
function renderTools(group) {
  const root = $("tab-content"), data = groupTools;
  if (!data || data.group_id !== group.id || data.error) { root.replaceChildren(empty("群工具暂时无法加载", data?.error || "请刷新后重试。")); return; }
  const expanded = new Set([...root.querySelectorAll(".tool-report[open]")].map(node => node.dataset.reportKey));
  const oldScroll = root.scrollTop;
  const reports = toolCard("周报 / 月报", "总结上一完整自然周（周一至周日）或自然月；开启自动生成后会在本机保存，群里可 @机器人发送“周报”或“月报”。");
  const reportActions = el("div", "tool-actions");
  [["week", "生成上周周报"], ["month", "生成上月月报"]].forEach(([kind, label]) => reportActions.append(button(label, "button small", event => toolsAction(event.currentTarget, group.id, `/reports/${kind}`, {}, "POST", "报表已生成"))));
  const auto = el("label", "tool-check"), checkbox = el("input"); checkbox.type = "checkbox"; checkbox.checked = data.auto_reports;
  checkbox.addEventListener("change", event => toolsAction(event.currentTarget, group.id, "/preferences", {auto_reports: checkbox.checked}, "PATCH")); auto.append(checkbox, document.createTextNode("自动生成周报 / 月报")); reportActions.append(auto); reports.append(reportActions);
  data.reports.slice(0, 6).forEach(report => { const details = el("details", "tool-report"), summary = el("summary", "", `${report.kind === "week" ? "周报" : "月报"} · ${report.start_date} 至 ${report.end_date}`); details.dataset.reportKey = JSON.stringify([group.id, report.kind, report.start_date]); details.open = expanded.has(details.dataset.reportKey); details.append(summary, el("p", "tool-report-content", report.content), el("p", "tool-description", `${report.message_count} 条成员文本 · 选取 ${report.sampled_count} 条生成 · ${dateText(report.updated_at, true)}`)); reports.append(details); });
  if (!data.reports.length) reports.append(el("p", "tool-description", "还没有报表，可点击生成。"));
  const rules = toolCard("关键词规则", "实时文字消息按关键词匹配，可记录命中或回复固定内容。每条消息最多触发一条规则，支持冷却时间，并遵循群总开关和黑名单。");
  rules.append(button("添加规则", "button small", () => ruleDialog(group.id)));
  data.rules.forEach(rule => { const row = el("div", "tool-row"), copy = el("div", "tool-row-copy"), actions = el("div", "tool-actions"); copy.append(el("strong", "", rule.name), el("p", "", `${rule.enabled ? "已启用" : "已关闭"} · ${rule.match_mode === "exact" ? "完全匹配" : "包含"}“${rule.keyword}” · ${rule.action === "reply" ? "固定回复" : "记录命中"} · 冷却 ${rule.cooldown} 秒`)); actions.append(button("编辑", "button small", () => ruleDialog(group.id, rule)), button("删除", "button small", () => confirmDialog("删除关键词规则？", rule.name, () => api(`${toolsPath(group.id)}/rules/${rule.id}`, {}, "DELETE").then(() => reloadTools(group.id)), "删除", true))); row.append(copy, actions); rules.append(row); });
  const tasks = taskSection(group, data);
  const hits = toolCard("最近关键词命中", "显示最近 30 条触发记录，重复回调不会重复回复。");
  data.hits.forEach(hit => { const row = el("div", "tool-hit"); row.append(el("strong", "", `${hit.rule_name} · ${{recorded:"已记录",sending:"发送中 / 未确认",sent:"已发送",unknown:"发送未确认",skipped:"已停止"}[hit.status] || hit.status}`), el("p", "", hit.content), el("small", "", `${hit.sender} · ${dateText(hit.created_at, true)}`)); hits.append(row); });
  if (!data.hits.length) hits.append(el("p", "tool-description", "暂无命中记录。"));
  const insights = toolCard("群聊趣味工具", "从本群归档找梗、快速补课，或把聊天改编成四格漫画。仅在点击生成时调用模型，网页结果可查看来源；不会自动发送到微信群。");
  const insightActions = el("div", "tool-actions");
  [["meme", "群聊梗百科"], ["catchup", "爬楼助手"], ["comic", "聊天变漫画"]].forEach(([kind, label]) => insightActions.append(button(label, "button small", () => insightDialog(group, kind))));
  insights.append(insightActions, el("p", "tool-description", "群内可发送：@机器人 梗百科：关键词 / 爬楼 最近2小时 / 聊天变漫画 最近30条。"));
  root.replaceChildren(insights, tasks, reports, rules, hits);
  root.scrollTop = oldScroll;
  updateTaskCountdowns();
}
function insightDialog(group, kind) {
  const titles = {meme: "群聊梗百科", catchup: "爬楼助手", comic: "聊天变漫画"};
  const body = showDialog(titles[kind], nameOf(group)), version = dialogVersion;
  const form = el("form"), result = el("section", "insight-result");
  result.setAttribute("aria-live", "polite");
  form.append(el("p", "modal-copy", kind === "meme" ? "查找本群归档中的关键词和早期上下文，解释含义并附原话。最早归档记录不等于真正起源。" : kind === "comic" ? "将选取的成员聊天改编成一张四格漫画。会调用对话模型和生图模型，可能需要数分钟；结果在这里预览和下载。" : "按话题整理结论、待办和未解决问题，并附来源消息。时间按北京时间，最多读取最近7天。"));
  let term, windowInput, limit, after, ids;
  if (kind === "meme") term = formField(form, "梗关键词", "例如：周三战神", "", false, 80);
  else {
    windowInput = selectField(form, "聊天范围（北京时间）", [["今天", "今天"], ["最近2小时", "最近2小时"], ["最近24小时", "最近24小时"], ["最近3天", "最近3天"], ["最近7天", "最近7天"]], "今天");
    limit = formField(form, "最多选取条数", kind === "comic" ? "1～100" : "1～200", kind === "comic" ? "50" : "200", false, 3);
    limit.type = "number"; limit.min = "1"; limit.max = kind === "comic" ? "100" : "200"; limit.required = true;
    after = formField(form, "从消息编号之后（可选）", "例如：123，仍受上述时间范围限制", "", false, 19);
    ids = formField(form, "仅选这些消息编号（可选）", "例如：123,124,130；不能与起始编号同时填写", "", false, 2100);
  }
  if (term) term.required = true;
  const footer = formFooter(form, kind === "comic" ? "生成四格漫画" : kind === "meme" ? "查找这个梗" : "开始爬楼");
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (footer.submit.disabled) return;
    const payload = kind === "meme" ? {term: term.value.trim()} : {window: windowInput.value, limit: Number(limit.value)};
    if (after?.value.trim()) payload.after_id = Number(after.value.trim());
    if (ids?.value.trim()) payload.message_ids = ids.value.trim().split(/[,，\s]+/).map(Number);
    const numbers = [...(payload.message_ids || []), ...(payload.after_id === undefined ? [] : [payload.after_id])];
    if (numbers.some(value => !Number.isSafeInteger(value) || value < 1)) { footer.error.textContent = "消息编号必须为有效的正整数。"; return; }
    footer.submit.disabled = true; footer.error.textContent = "";
    result.replaceChildren(el("p", "tool-description", kind === "comic" ? "正在创作分镜和漫画，请稍候…" : "正在分析本群记录…"));
    try {
      const data = await api(`${groupPath(group.id)}/insights/${kind}`, payload, "POST", kind === "comic" ? 390000 : 140000);
      if (dialogVersion !== version || !$("modal").open) return;
      result.replaceChildren(el("p", "insight-text", data.text), el("p", "tool-description", `选取 ${data.sampled_count} 条 / 匹配 ${data.message_count} 条成员文本`));
      if (data.image_data_url) {
        const image = el("img", "favorite-full-media"); image.alt = "由本群聊天改编的四格漫画"; image.src = data.image_data_url;
        const download = el("a", "button small", "下载漫画"); download.href = data.image_data_url; download.download = "群聊四格漫画.png";
        result.append(image, download, el("p", "tool-description", "群聊创意改编，人物和对白不代表事实复现。"));
      }
      if (data.sources.length) {
        const sources = el("details", "insight-sources"); sources.open = true; sources.append(el("summary", "", "来源原话"));
        data.sources.forEach(source => {
          const quote = el("blockquote");
          quote.append(el("p", "tool-description", `#${source.id} · ${dateText(source.timestamp * 1000)} · ${source.name}`), el("p", "insight-text", source.content));
          sources.append(quote);
        }); result.append(sources);
      }
    } catch (error) {
      if (dialogVersion === version && $("modal").open) { result.replaceChildren(); footer.error.textContent = error.message; }
    } finally { footer.submit.disabled = false; }
  });
  body.append(form, result);
}
function selectField(form, label, values, value) {
  const caption = el("label", "form-label", label), select = el("select", "form-input");
  select.id = `select-${Math.random().toString(36).slice(2)}`; caption.htmlFor = select.id;
  values.forEach(([id, text]) => { const option = el("option", "", text); option.value = id; select.append(option); }); select.value = value; form.append(caption, select); return select;
}
function ruleDialog(group, rule = {}) {
  const body = showDialog(rule.id ? "编辑关键词规则" : "添加关键词规则"), form = el("form");
  const name = formField(form, "规则名称", "例如：故障记录", rule.name || ""), keyword = formField(form, "关键词", "填写要匹配的文字", rule.keyword || "", false, 200);
  const mode = selectField(form, "匹配方式", [["contains", "包含关键词"], ["exact", "完全匹配"]], rule.match_mode || "contains"), action = selectField(form, "触发动作", [["record", "仅记录命中"], ["reply", "发送固定回复"]], rule.action || "record");
  const reply = formField(form, "固定回复内容", "动作选择固定回复时必填", rule.reply || "", true, 2000), cooldown = formField(form, "冷却时间（秒）", "10～86400", String(rule.cooldown || 60)); cooldown.type = "number"; cooldown.min = "10"; cooldown.max = "86400";
  const enabled = selectField(form, "规则状态", [["false", "关闭"], ["true", "启用"]], String(Boolean(rule.enabled)));
  name.required = keyword.required = true; const footer = formFooter(form); body.append(form);
  bindForm(form, footer, async () => { if (action.value === "reply" && !reply.value.trim()) throw new Error("请填写固定回复内容"); await api(`${toolsPath(group)}/rules${rule.id ? `/${rule.id}` : ""}`, {name:name.value,keyword:keyword.value,match_mode:mode.value,action:action.value,reply:reply.value,cooldown:Number(cooldown.value),enabled:enabled.value === "true"}, rule.id ? "PUT" : "POST"); await reloadTools(group); });
}
function taskDialog(group, candidate = {}, fromBatch = false) {
  const deadline = candidate.due_at == null ? (candidate.due_date ? `${candidate.due_date}T23:59:59` : "") : new Date(candidate.due_at * 1000 + 8 * 3600000).toISOString().slice(0, 19);
  const body = showDialog(candidate.id ? "编辑群待办" : "新增群待办"), form = el("form"), title = formField(form, "待办事项", "需要完成什么", candidate.title || "", false, 200), owner = formField(form, "负责人", "可留空", candidate.owner || "", false, 80), due = formField(form, "截止时间（北京时间）", "", deadline); due.type = "datetime-local"; due.step = "1"; due.required = Boolean(candidate.reminder_id); title.required = true;
  if (candidate.source_content) form.append(el("p", "tool-source", `来源消息：${candidate.source_content}`));
  if (fromBatch) body.append(button("返回候选列表", "text-link", () => showTaskCandidates(group)));
  const footer = formFooter(form, "保存待办"); body.append(form);
  bindForm(form, footer, async () => {
    await api(`${toolsPath(group)}/tasks${candidate.id ? `/${candidate.id}` : ""}`, {title:title.value,owner:owner.value,due_at:due.value ? `${due.value}+08:00` : "",source_id:candidate.source_id || null}, candidate.id ? "PUT" : "POST");
    if (fromBatch) {
      const remaining = (taskCandidates.get(group) || []).filter(item => item !== candidate);
      if (remaining.length) taskCandidates.set(group, remaining); else taskCandidates.delete(group);
    }
  }, fromBatch ? () => showTaskCandidates(group, "本批候选已全部保存。") : undefined);
}
function showTaskCandidates(group, emptyText = "没有发现明确的待办。") {
  const candidates = taskCandidates.get(group) || [];
  const body = showDialog("待办候选项");
  body.append(el("p", "modal-copy", "逐条核对并保存，未保存的候选可在本次页面中继续核对。"));
  candidates.forEach(candidate => {
    const row = el("div", "tool-row"), copy = el("div", "tool-row-copy");
    copy.append(el("strong", "", candidate.title), el("p", "", `${candidate.owner || "未分配"} · ${candidate.due_date || "无截止日"}`), el("p", "tool-source", candidate.source_content));
    row.append(copy, button("核对并保存", "button small", () => taskDialog(group, candidate, true))); body.append(row);
  });
  if (!candidates.length) body.append(el("p", "modal-copy", emptyText));
  body.append(button("重新提取", "button small", event => extractTaskDialog(event.currentTarget, group, true)));
}
async function extractTaskDialog(source, group, force = false) {
  if (!force && taskCandidates.get(group)?.length) { showTaskCandidates(group); return; }
  source.disabled = true; source.textContent = "提取中…";
  try {
    const result = await api(`${toolsPath(group)}/tasks/extract`, {}, "POST", 65000);
    taskCandidates.set(group, result.candidates);
    showTaskCandidates(group);
  } catch (error) { toast(error.message, true); }
  finally { if (source.isConnected) { source.disabled = false; source.textContent = taskCandidates.get(group)?.length ? "继续核对待办" : "AI 提取待办"; } }
}
function renderWordCloud(words) {
  const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 220 240"); svg.setAttribute("class", "word-cloud");
  svg.setAttribute("role", "group"); svg.setAttribute("aria-label", "群聊高频词，点击词语搜索聊天记录");
  const measure = document.createElement("canvas").getContext("2d"), boxes = [];
  const palette = ["#07955d", "#287b8d", "#5d739d", "#90805b", "#758c6a", "#956d80"];
  const max = words[0].count, min = words[words.length - 1].count;
  words.forEach((word, index) => {
    let size = max === min ? 17 : 11 + 18 * Math.sqrt((word.count - min) / (max - min));
    measure.font = `600 ${size}px "Microsoft YaHei", sans-serif`;
    let width = measure.measureText(word.text).width;
    if (width > 200) { size *= 200 / width; width = 200; }
    const height = size * 1.3;
    let box;
    for (let step = 0; step < 1400; step++) {
      const angle = step * .32, radius = step * .13;
      const x = 110 + Math.cos(angle) * radius, y = 120 + Math.sin(angle) * radius * .9;
      const candidate = {left: x - width / 2 - 3, right: x + width / 2 + 3, top: y - height / 2 - 2, bottom: y + height / 2 + 2, x, y};
      if (candidate.left < 2 || candidate.right > 218 || candidate.top < 2 || candidate.bottom > 238) continue;
      if (boxes.some(b => candidate.left < b.right && candidate.right > b.left && candidate.top < b.bottom && candidate.bottom > b.top)) continue;
      box = candidate; break;
    }
    if (!box) return;
    boxes.push(box);
    const text = document.createElementNS(ns, "text"), title = document.createElementNS(ns, "title");
    text.setAttribute("x", box.x); text.setAttribute("y", box.y);
    text.setAttribute("text-anchor", "middle"); text.setAttribute("dominant-baseline", "central");
    text.setAttribute("font-size", size.toFixed(1)); text.setAttribute("font-weight", "600");
    text.setAttribute("fill", palette[index % palette.length]); text.setAttribute("role", "button"); text.setAttribute("tabindex", "0");
    text.setAttribute("aria-label", `${word.text}，${word.count} 次，搜索相关消息`);
    text.textContent = word.text; title.textContent = `${word.text} · ${word.count} 次`; text.append(title);
    const search = () => { messageQuery = word.text; messagePage = 1; detailVersion = ""; resetChatScroll = true; const input = document.querySelector(".message-search input"); if (input) input.value = word.text; setTab("messages"); };
    text.addEventListener("click", search);
    text.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); search(); } });
    svg.append(text);
  });
  return svg;
}
function renderMembers(group) {
  const description = group.members_sync === "complete" ? "已同步微信成员名单，保留历史成员与本地添加记录；黑名单仅在当前群生效。" : "暂未取得完整微信成员关系，展示已发现与手动添加的成员。";
  const existing = $("member-table-container");
  if (existing?.dataset.group === group.id) {
    $("member-description").textContent = description;
    renderMemberTable(group);
    return;
  }
  const heading = el("div", "section-heading"), copy = el("div");
  const hint = el("p", "", description); hint.id = "member-description";
  copy.append(el("h3", "", "群成员管理"), hint); heading.append(copy);
  const tools = el("div", "member-toolbar"), search = el("div", "search-field"), input = el("input");
  input.type = "search"; input.placeholder = "搜索名称或 wxid"; input.setAttribute("aria-label", "搜索群成员"); input.value = memberQuery;
  input.addEventListener("input", () => { memberQuery = input.value; renderMemberTable(group); });
  search.append(icon("search"), input); tools.append(search, button("添加成员", "button small", () => addMemberDialog(group), "plus"));
  const container = el("div"); container.id = "member-table-container";
  container.dataset.group = group.id;
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
    log.addEventListener("scroll", () => { jump.hidden = log.dataset.stale !== "true" && messagePage === 1 && !messageQuery && log.scrollHeight - log.clientHeight - log.scrollTop < 80; });
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
  const oldTop = log.scrollTop, oldHeight = log.scrollHeight, follow = resetChatScroll || (messagePage === 1 && !messageQuery && oldHeight - log.clientHeight - oldTop < 80);
  const logTop = log.getBoundingClientRect().top;
  const anchor = follow ? null : [...log.querySelectorAll("[data-message-id]")].find(row => row.getBoundingClientRect().bottom > logTop);
  const anchorOffset = anchor ? anchor.getBoundingClientRect().top - logTop : 0;
  // A busy group can push the entire reading position outside the latest page.
  // Keep that page visible until navigation, instead of replacing it with unrelated messages.
  if (anchor && !messages.some(message => String(message.id) === anchor.dataset.messageId)) {
    log.dataset.stale = "true";
    $("chat-jump").hidden = false;
    return;
  }
  chatSignatures.set(log, signature);
  delete log.dataset.stale;
  const stack = el("div", "message-stack");
  let lastTimestamp = 0;
  [...messages].reverse().forEach((message, i) => {
    if (!i || message.timestamp - lastTimestamp > 300) stack.append(el("div", "chat-time-divider", dateText(new Date(message.timestamp * 1000).toISOString(), true)));
    lastTimestamp = message.timestamp;
    const person = members.find(member => member.id === message.sender) || { id: message.sender || "未知成员" };
    const self = Boolean(ownWxid) && message.sender === ownWxid;
    const row = el("div", `chat-message${self ? " is-self" : ""}`), body = el("div", "chat-message-body"), meta = el("div", "chat-meta");
    row.dataset.messageId = String(message.id);
    meta.append(el("span", "", self ? "我" : person.name || person.id), el("time", "", new Date(message.timestamp * 1000).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false})));
    meta.append(el("span", "message-number", `#${message.id}`));
    if (person.blocked) meta.append(badge("已拉黑", "red"));
    body.append(meta, el("div", "chat-bubble", message.content || "（空消息）"));
    if (message.type !== 1 || message.truncated) body.append(el("p", "form-note", `${message.type !== 1 ? `类型 ${message.type} · 展示原始内容` : ""}${message.truncated ? " · 内容过长已截断" : ""}`));
    row.append(memberAvatar(person, i), body); stack.append(row);
  });
  log.replaceChildren(messages.length ? stack : empty(messageQuery ? "没有匹配的聊天记录" : "还没有聊天消息", messageQuery ? "换个关键词试试。" : "群消息同步后会显示在这里。"));
  resetChatScroll = false;
  const replacement = anchor && [...log.querySelectorAll("[data-message-id]")].find(row => row.dataset.messageId === anchor.dataset.messageId);
  if (follow) log.scrollTop = log.scrollHeight;
  else if (replacement) log.scrollTop += replacement.getBoundingClientRect().top - log.getBoundingClientRect().top - anchorOffset;
  else log.scrollTop = oldTop;
  $("chat-jump").hidden = messagePage === 1 && !messageQuery && follow;
}
function turnMessagePage(delta) { messagePage += delta; detailVersion = ""; resetChatScroll = true; loadDetail().catch(error => toast(error.message, true)); }
function latestMessages() { messagePage = 1; messageQuery = ""; detailVersion = ""; resetChatScroll = true; const input = document.querySelector(".message-search input"); if (input) input.value = ""; loadDetail().catch(error => toast(error.message, true)); }
function renderAudit() {
  const heading = el("div", "section-heading"), copy = el("div"); copy.append(el("h3", "", "操作记录"), el("p", "", "记录当前群的配置调整与黑名单操作，最近 100 条。")); heading.append(copy);
  const rows = audit.map(event => { const node = el("div", "audit-row"); node.append(el("div", "", event.action), el("p", "", event.target), el("time", "", dateText(event.created_at))); return node; });
  $("tab-content").replaceChildren(heading, ...(rows.length ? rows : [empty("还没有操作记录", "修改功能或管理黑名单后，操作会记录在这里。", "clock")]));
}
function setTab(value) { if (tab !== value) resetChatScroll = true; tab = value; detailVersion = ""; updateChatMode(); loadDetail().catch(error => toast(error.message, true)); }
function loadInsights(group, force = false) {
  // Keep only the current view's statistics. Poll messages independently of
  // these history scans; share an in-flight request across tab changes.
  const day = new Date().toLocaleDateString("en-CA", {timeZone: "Asia/Shanghai"});
  const key = JSON.stringify([group.id, radarDays, ownWxid, day]);
  if (!force && insightRequest?.key === key && performance.now() < insightRequest.expires) return insightRequest.promise;
  const request = {key, expires: Infinity, promise: null};
  request.promise = Promise.all([
    api(`${groupPath(group.id)}/word-cloud`).catch(error => ({group_id: group.id, error: error.message || "词云暂时无法加载，稍后自动重试。"})),
    api(`${groupPath(group.id)}/radar?days=${radarDays}`).catch(error => ({group_id: group.id, error: error.message}))
  ]).then(results => {
    // Errors retry on the next poll. Old requests only update their own entry.
    request.expires = performance.now() + (results.some(result => !result || result.error) ? 0 : 30000);
    return results;
  });
  insightRequest = request;
  return request.promise;
}

async function loadDetail(force = false) {
  const group = current();
  if (!group || view !== "groups") return;
  if (force) favoriteSearch = null;
  const token = ++detailToken, requestedTab = tab, requestedPage = messagePage, requestedQuery = messageQuery;
  const [people, inbox, history, insights, summaryResponse, toolsResponse, favoritesResponse] = await Promise.all([
    api(`${groupPath(group.id)}/members`),
    requestedTab === "messages" ? api(`${groupPath(group.id)}/messages?page=${requestedPage}&q=${encodeURIComponent(requestedQuery)}`) : Promise.resolve(null),
    requestedTab === "audit" ? api(`/api/manage/audit?group=${encodeURIComponent(group.id)}`) : Promise.resolve(null),
    loadInsights(group, force),
    api(`${groupPath(group.id)}/summary`).catch(() => ({summary: null})),
    requestedTab === "tools" ? api(toolsPath(group.id)).catch(error => ({group_id: group.id, error: error.message})) : Promise.resolve(null),
    requestedTab === "favorites" ? loadFavorites(group.id).catch(error => ({group_id:group.id,error:error.message})) : Promise.resolve(null)
  ]);
  const [cloud, radarResponse] = insights;
  if (token !== detailToken || group.id !== selectedId || requestedTab !== tab || view !== "groups") return;
  members = people.members;
  wordCloud = cloud;
  chatSummary = summaryResponse.summary;
  chatRadar = radarResponse;
  groupTools = toolsResponse;
  favoritesData = favoritesResponse;
  messages = inbox?.messages || [];
  if (inbox) messageMeta = inbox;
  audit = history?.events || [];
  const version = JSON.stringify([current(), members, messages, messageMeta.total, requestedPage, requestedQuery, audit, cloud, chatSummary, chatRadar, groupTools, favoritesData, tab, state.media, ownWxid, state.chatbot]);
  if (version === detailVersion) return;
  detailVersion = version;
  renderHeader(current()); renderInspector(current());
  if (tab === "features") renderFeatures(current());
  else if (tab === "members") renderMembers(current());
  else if (tab === "messages") renderMessages();
  else if (tab === "radar") renderRadarPage(current());
  else if (tab === "tools") renderTools(current());
  else if (tab === "favorites") renderFavorites(current());
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
  dialogVersion++;
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
function bindForm(form, footer, save, onSaved) {
  const version = dialogVersion;
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (footer.submit.disabled || mutating) return;
    mutating = true; footer.submit.disabled = true; footer.error.textContent = "";
    let saved = false;
    try {
      await save(); saved = true;
      const active = dialogVersion === version && $("modal").open;
      if (active) $("modal").close();
      await refreshState(true); toast("已保存，配置立即生效");
      if (onSaved && active && dialogVersion === version) onSaved();
    }
    catch (error) { if (saved) toast(`配置已保存，刷新失败：${error.message}`, true); else footer.error.textContent = error.message; }
    finally { mutating = false; footer.submit.disabled = false; }
  });
}
function infoDialog(title, description) {
  const body = showDialog(title); body.append(el("p", "modal-copy", description)); const actions = el("div", "form-actions"); actions.append(button("知道了", "button primary", () => $("modal").close())); body.append(actions);
}

function mentionSettingsDialog() {
  const body = showDialog("机器人群内显示名", "@问答识别"), form = el("form");
  form.append(el("p", "modal-copy", "填写微信中 @机器人时显示的名字。接口返回的昵称为空，或群内昵称与账号昵称不同时，需要在这里补充。"));
  const names = formField(form, "机器人显示名（每行一个，不含 @）", "填写机器人在群内显示的名字", (state.chatbot?.configured_mention_names || []).join("\n"), true, 1700);
  form.append(el("p", "form-note", "不同群的显示名可分行填写，最多 20 个。保存后立即生效，请用另一个微信账号发送新的 @提问验证。"));
  const footer = formFooter(form, "保存显示名"); body.append(form);
  bindForm(form, footer, () => api("/api/manage/chatbot/mentions", {mention_names: names.value.split(/\r?\n/).map(name => name.trim().replace(/^@/, "").trim()).filter(Boolean)}));
}

async function modelSettingsDialog(kind = "chat") {
  kind = ["image", "vision"].includes(kind) ? kind : "chat";
  const isImage = kind === "image", isVision = kind === "vision", isChat = kind === "chat";
  const modelLabel = isImage ? "生图" : isVision ? "视觉" : "对话";
  const modelPath = `/api/manage/${kind}-models`;
  const body = showDialog(`AI ${modelLabel}模型`, "机器人 · 全部群共用");
  const version = dialogVersion;
  const active = () => $("modal").open && dialogVersion === version;
  body.append(el("p", "form-note", "正在读取模型配置…"));
  let settings, search;
  try { [settings, search] = await Promise.all([api(`/api/manage/${kind}-model`), isChat ? api("/api/manage/chat-search") : Promise.resolve(null)]); }
  catch (error) { if (active()) body.replaceChildren(el("p", "form-error", error.message)); return; }
  if (!active()) return;
  body.replaceChildren();
  const categories = el("div", "model-card-actions");
  const chatTab = button("对话模型", `button small${isChat ? " primary" : ""}`, () => modelSettingsDialog());
  const imageTab = button("生图模型", `button small${isImage ? " primary" : ""}`, () => modelSettingsDialog("image"));
  const visionTab = button("视觉模型", `button small${isVision ? " primary" : ""}`, () => modelSettingsDialog("vision"));
  chatTab.setAttribute("aria-pressed", String(isChat)); imageTab.setAttribute("aria-pressed", String(isImage)); visionTab.setAttribute("aria-pressed", String(isVision));
  categories.append(chatTab, imageTab, visionTab); body.append(categories);
  body.append(el("p", "modal-copy", isImage ? "生图模型独立于对话模型。@机器人 后发送“生图：图片描述”“帮我画一个图：一只猫”等绘图请求即可调用，每条消息最多回复一张图片。" : isVision ? "视觉模型独立配置，专门用于图片理解和最近图片的追问。群成员发送图片并 @机器人，或发图后 @机器人 提问，即可调用。普通文字问答继续使用对话模型。" : "可保存多个第三方模型配置。新增和修改时自动测试；普通问答、语义识别和群资料分析共用此处的调用顺序。"));
  if (isVision) body.append(el("p", "form-note", "图片会压缩后发送给视觉模型服务商；与对话、生图共用每日 AI 调用额度。未配置独立视觉模型时，兼容原有已开启图片理解的对话模型。"));
  body.append(el("p", "form-note", "每次请求从首选开始，余额不足、密钥失效、限流或服务不可用时按备用顺序切换，成功即停止。最多添加四个备用；未加入备用的配置不参与调用。"));
  if (isImage) body.append(el("p", "form-note", "生图提交后超时、图片下载失败或返回无效图片时不自动重新生成，避免重复计费。各次尝试使用各自的模型、尺寸和密钥。"));
  if (isImage) body.append(el("p", "form-note", "复杂描述和“刚才那张再亮一点”等后续修改会使用当前对话模型提取需求，多一次对话调用并计入 AI 额度；语义不清时会追问。明确完整的“生图：描述”通常直接生成。实际图片尺寸以此处配置为准。"));
  if (isChat) {
    body.append(el("p", "form-note", "图片理解可在“视觉模型”中单独添加和管理；原有已开启图片理解的对话模型仍可兼容使用。"));
  }
  if (isChat) {
    const searchCard = el("section", "model-card"), searchCopy = el("div", "model-card-copy");
    searchCopy.append(
      el("h3", "", "联网搜索"),
      el("p", "", search.base_url || "https://api.deepseek.com/v1"),
      el("p", "form-note", `${search.enabled ? "已开启" : "未开启"} · 今日已用 ${search.used_today} / ${search.daily_limit} 次 · ${search.has_api_key ? "已保存 API Key" : "未配置 API Key"}`),
    );
    const searchHeading = el("div", "model-card-heading");
    searchHeading.append(searchCopy, el("span", search.enabled ? "badge" : "badge gray", search.enabled ? "已开启" : "未开启"));
    const searchActions = el("div", "model-card-actions");
    searchActions.append(button("配置联网搜索", "button small", () => searchSettingsDialog(search)));
    searchCard.append(searchHeading, searchActions);
    body.append(searchCard);
    const usageCard = el("section", "model-card"), usageCopy = el("div", "model-card-copy");
    usageCopy.append(el("h3", "", "AI 用量（近 7 天）"), el("p", "form-note", "正在读取…"));
    usageCard.append(usageCopy); body.append(usageCard);
    api("/api/manage/ai-usage?days=7").then(usage => {
      if (!active()) return;
      const totals = usage.totals || {}, limits = usage.limits || {};
      usageCopy.replaceChildren(
        el("h3", "", "AI 用量（近 7 天）"),
        el("p", "form-note", `${totals.requests || 0} 次调用 · 输入 ${totals.prompt_tokens || 0} tokens · 输出 ${totals.completion_tokens || 0} tokens。额度：全局 ${limits.daily ?? "?"} 次/日，单群 ${limits.group ?? "?"}，单成员 ${limits.member ?? "?"}。`),
        el("p", "form-note", usage.models.length ? "按模型：" + usage.models.slice(0, 4).map(model => `${model.model || "未知"} ${(model.prompt_tokens || 0) + (model.completion_tokens || 0)} tokens`).join(" · ") : "尚无 token 记录；服务商返回 usage 后自动统计。"),
      );
    }).catch(error => {
      if (active()) usageCopy.replaceChildren(el("h3", "", "AI 用量"), el("p", "form-note", `读取用量失败：${error.message}`));
    });
  }
  const toolbar = el("div", "model-list-toolbar");
  toolbar.append(el("span", "form-note", `已保存 ${settings.models.length} 个${modelLabel}模型`), button(isChat ? "添加模型" : `添加${modelLabel}模型`, "button primary", () => modelEditorDialog({}, kind)));
  body.append(toolbar);
  if (settings.load_error) body.append(el("p", "form-error", settings.load_error));
  if (!settings.models.length) body.append(el("p", "modal-copy", "尚未配置模型。添加第一个模型并测试通过后会自动启用。"));
  const result = el("div", "model-test-result"); result.hidden = true; result.setAttribute("role", "status");
  const fallbackIds = settings.fallback_ids || [];
  const orderedIds = [settings.active_id, ...fallbackIds].filter(Boolean);
  const profileName = id => settings.models.find(profile => profile.id === id)?.name || settings.models.find(profile => profile.id === id)?.model || "已删除配置";
  if (orderedIds.length) body.append(el("p", "form-note", `调用顺序：${orderedIds.map((id, index) => `${index + 1}. ${profileName(id)}`).join(" → ")}`));
  const route = settings.last_route;
  if (route?.attempts?.length) {
    body.append(el("p", "form-note", `最近完成的调用：${route.used_id ? `使用 ${profileName(route.used_id)}` : "未成功"}`));
    for (const attempt of route.attempts) body.append(el("p", "form-note", `${profileName(attempt.profile_id)}：${attempt.status === "success" ? "成功" : attempt.message || "已停止"}`));
  }
  const cooldown = settings.cooldown || {};
  if (Object.keys(cooldown).length) {
    body.append(el("p", "form-note", `连接失败后暂时跳过：${Object.entries(cooldown).map(([id, seconds]) => `${profileName(id)}（剩余 ${Math.ceil(seconds)} 秒）`).join("、")}`));
  }
  const orderedProfiles = [...orderedIds.map(id => settings.models.find(profile => profile.id === id)).filter(Boolean), ...settings.models.filter(profile => !orderedIds.includes(profile.id))];
  const saveFallbacks = ids => modelAction(`/api/manage/${kind}-model/fallbacks`, "PUT", "正在保存备用顺序…", "备用顺序已保存", {fallback_ids: ids});
  for (const profile of orderedProfiles) {
    const fallbackIndex = fallbackIds.indexOf(profile.id);
    const card = el("section", "model-card"), heading = el("div", "model-card-heading"), copy = el("div", "model-card-copy");
    copy.append(el("h3", "", profile.name || profile.model), el("p", "", profile.model), el("p", "", profile.base_url));
    heading.append(copy, el("span", profile.active || fallbackIndex >= 0 ? "badge" : "badge gray", profile.active ? "首选" : fallbackIndex >= 0 ? `备用 ${fallbackIndex + 1}` : "未参与"));
    const thinking = profile.enable_thinking === false ? "关闭" : profile.enable_thinking === true ? "开启" : "跟随默认";
    const vision = isImage ? "" : ` · 图片理解：${profile.supports_vision ? "开启" : "关闭"}`;
    card.append(heading, el("p", "form-note", `${isImage ? `尺寸：${profile.size || "跟随默认"} · 返回：${profile.response_format === "default" ? "跟随默认" : profile.response_format}` : `深度思考：${thinking}${vision}`} · ${profile.has_api_key ? "已保存 API Key" : "未配置 API Key"}`));
    const actions = el("div", "model-card-actions");
    actions.append(button("查看 / 修改", "button small", () => modelEditorDialog(profile, kind)));
    const activate = button(profile.active ? "当前首选" : "设为首选", "button small primary", () => modelAction(`${modelPath}/${profile.id}/activate`, "POST", isImage ? "正在切换生图模型…" : "正在测试并切换模型…", "首选已更新；原首选需要手动加入备用"));
    activate.disabled = profile.active; actions.append(activate);
    if (!profile.active) {
      const backup = button(fallbackIndex < 0 ? "加入备用" : "移出备用", "button small", () => saveFallbacks(fallbackIndex < 0 ? [...fallbackIds, profile.id] : fallbackIds.filter(id => id !== profile.id)));
      backup.disabled = fallbackIndex < 0 && fallbackIds.length >= 4;
      actions.append(backup);
      if (fallbackIndex >= 0) {
        for (const [offset, label] of [[-1, "上移"], [1, "下移"]]) {
          const move = button(label, "button small", () => {
            const ids = [...fallbackIds], target = fallbackIndex + offset;
            [ids[fallbackIndex], ids[target]] = [ids[target], ids[fallbackIndex]];
            return saveFallbacks(ids);
          });
          move.disabled = fallbackIndex + offset < 0 || fallbackIndex + offset >= fallbackIds.length;
          actions.append(move);
        }
      }
    }
    const remove = button("删除", "button small danger", () => {
      const form = el("form");
      body.replaceChildren(el("p", "modal-copy", `确认删除「${profile.name || profile.model}」？此操作只删除本机保存的模型配置。`), form);
      const footer = formFooter(form, "删除模型"); footer.submit.className = "button danger";
      form.querySelector("button").onclick = () => modelSettingsDialog(kind);
      form.addEventListener("submit", event => { event.preventDefault(); modelAction(`${modelPath}/${profile.id}`, "DELETE", "正在删除…", "模型已删除"); });
      form.append(result);
    });
    remove.disabled = profile.active; remove.title = profile.active ? "请先启用其他模型再删除" : "删除模型配置";
    actions.append(remove); card.append(actions); body.append(card);
  }
  body.append(result);
  async function modelAction(path, method, pending, success, payload = {}) {
    if (modelSaving || mutating) return;
    modelSaving = true; mutating = true;
    const controls = [...body.querySelectorAll("button"), $("close-modal")], disabled = controls.map(node => node.disabled);
    controls.forEach(node => node.disabled = true);
    result.hidden = false; result.className = "model-test-result"; result.textContent = pending;
    try {
      const data = await api(path, payload, method, isVision ? 120000 : 60000);
      if (!data.saved) throw new Error(data.test?.message || "操作未完成，请重试。");
      await modelSettingsDialog(kind); toast(success); await refreshState(true);
    } catch (error) { result.classList.add("failed"); result.textContent = error.message; }
    finally { modelSaving = false; mutating = false; controls.forEach((node, index) => node.disabled = disabled[index]); }
  }
}

function searchSettingsDialog(settings = {}) {
  const body = showDialog("联网搜索", "机器人 · 全部群共用");
  body.append(button("返回模型列表", "text-link", modelSettingsDialog));
  const form = el("form");
  form.append(el("p", "modal-copy", "开启后，每个 @机器人 的文本问题会先调用 DeepSeek 联网搜索，再交给当前 AI 模型回答。回复不附来源链接；搜索失败时自动回退普通回答。"));
  const enabledLabel = el("label", "form-label", "联网搜索开关"), enabled = el("select", "form-input");
  enabled.id = "search-enabled"; enabledLabel.htmlFor = enabled.id;
  [["true", "开启"], ["false", "关闭"]].forEach(([value, text]) => {
    const option = el("option", "", text); option.value = value; enabled.append(option);
  });
  enabled.value = settings.enabled ? "true" : "false";
  enabled.selectedIndex = settings.enabled ? 0 : 1;
  form.append(enabledLabel, enabled);
  const base = formField(form, "搜索 Base URL", "https://api.deepseek.com/v1", settings.base_url || "https://api.deepseek.com/v1", false, 2048);
  base.required = true; base.type = "url";
  form.append(el("p", "form-note", "使用 DeepSeek 搜索接口，系统会调用该地址下的 /tools/search。"));
  const key = formField(form, "搜索 API Key", settings.has_api_key ? "已保存密钥；留空沿用，更换地址时请重新填写" : "填写 DeepSeek API Key", "", false, 4096);
  key.type = "password"; key.spellcheck = false;
  form.append(el("p", "form-note", "密钥仅保存在运行服务的电脑上，页面不会回显。"));
  const count = formField(form, "每次搜索结果数", "1–10", String(settings.result_count ?? 5), false, 2);
  count.type = "number"; count.min = "1"; count.max = "10"; count.required = true;
  const limit = formField(form, "每日搜索上限", "默认 1000 次", String(settings.daily_limit ?? 1000), false, 7);
  limit.type = "number"; limit.min = "1"; limit.max = "100000"; limit.required = true;
  form.append(el("p", "form-note", `按北京时间统计，请求前预留次数，失败或超时不退回，跨天自动归零。今日已用 ${settings.used_today ?? 0} 次，剩余 ${settings.remaining_today ?? 0} 次。`));
  const result = el("div", "model-test-result"); result.setAttribute("role", "status"); result.setAttribute("aria-live", "polite"); result.hidden = true;
  form.append(result);
  const footer = formFooter(form, "保存搜索配置");
  footer.error.setAttribute("role", "alert");
  body.append(form);
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (footer.submit.disabled || mutating) return;
    mutating = true; footer.submit.disabled = true; footer.error.textContent = "";
    result.hidden = false; result.className = "model-test-result"; result.textContent = "正在保存…";
    try {
      const data = await api("/api/manage/chat-search", {
        enabled: enabled.value === "true",
        base_url: base.value.trim(),
        api_key: key.value.trim() || null,
        result_count: Number(count.value),
        daily_limit: Number(limit.value),
      }, "POST");
      if (!data.saved) throw new Error(data.message || "保存失败，请检查搜索配置。");
      $("modal").close(); toast("联网搜索配置已保存"); await refreshState(true);
    } catch (error) {
      result.classList.add("failed"); result.textContent = error.message;
    } finally {
      mutating = false; footer.submit.disabled = false;
    }
  });
}
function modelEditorDialog(settings = {}, kind = "chat") {
  const isImage = kind === "image", isVision = kind === "vision", label = isImage ? "生图模型" : isVision ? "视觉模型" : " AI 模型";
  const body = showDialog(`${settings.id ? "修改" : "添加"}${label}`, "机器人 · 全部群共用");
  body.append(button("返回模型列表", "text-link", () => modelSettingsDialog(kind)));
  const form = el("form");
  form.append(el("p", "modal-copy", isImage ? "保存时实际生成一张测试图片，可能产生服务商费用，最多等待 3 分钟。测试通过后保存，第一个生图模型自动启用；切换已保存的生图模型不会额外生成测试图片。" : isVision ? "保存时自动发送一张内置色块图片，测试图片输入与文字回复，可能产生服务商费用。测试通过后保存，第一个视觉模型自动启用。" : "保存时自动发送一条测试消息。第一个模型会自动启用；其他新增模型测试通过后，可在列表中点击启用。"));
  const name = formField(form, "配置名称", isImage ? "例如：主生图模型" : isVision ? "例如：主视觉模型" : "例如：硅基流动 DeepSeek", settings.name || "", false, 80);
  const base = formField(form, "Base URL", "例如 https://api.siliconflow.cn/v1", settings.base_url, false, 2048);
  base.required = true; base.type = "url";
  form.append(el("p", "form-note", isImage ? "使用兼容 OpenAI Images 的服务，地址通常以 /v1 结尾；也可填写完整的 /images/generations 地址。" : "使用兼容 OpenAI 的 Chat Completions 服务，地址通常以 /v1 结尾。"));
  const model = formField(form, "模型名称", "填写服务商的完整模型 ID", settings.model, false, 256);
  model.required = true;
  const thinkingLabel = el("label", "form-label", "深度思考"), thinking = el("select", "form-input");
  thinking.id = "model-thinking"; thinkingLabel.htmlFor = thinking.id;
  [["default", "跟随模型默认"], ["off", "关闭（优先快速回复）"], ["on", "开启"]].forEach(([value, text]) => {
    const option = el("option", "", text); option.value = value; thinking.append(option);
  });
  thinking.value = settings.enable_thinking === false ? "off" : settings.enable_thinking === true ? "on" : "default";
  let vision = null;
  if (!isImage) {
    form.append(thinkingLabel, thinking, el("p", "form-note", "硅基流动支持切换思考模式的模型可选择关闭；其他服务或不支持切换的模型请选择“跟随模型默认”。"));
    if (isVision) form.append(el("p", "form-note", "请选择支持 OpenAI 兼容 image_url 图片输入的多模态模型。群内图片及相关上下文会发送给此服务商。"));
    else {
      const visionRow = el("label", "tool-check");
      vision = el("input"); vision.type = "checkbox"; vision.checked = Boolean(settings.supports_vision);
      visionRow.append(vision, document.createTextNode("支持图片理解（多模态）"));
      form.append(visionRow, el("p", "form-note", "兼容原有图片理解配置。建议在“视觉模型”中单独添加；未配置独立视觉模型时，带图提问可使用此处已开启图片理解的模型。"));
    }
  }
  let size, responseFormat;
  if (isImage) {
    size = formField(form, "图片尺寸", "例如 1024x1024；也可填写 auto 或留空", settings.size ?? "1024x1024", false, 20);
    const formatLabel = el("label", "form-label", "图片返回格式");
    responseFormat = el("select", "form-input"); responseFormat.id = "image-response-format"; formatLabel.htmlFor = responseFormat.id;
    [["default", "跟随服务默认"], ["b64_json", "Base64 图片"], ["url", "图片 URL"]].forEach(([value, text]) => {
      const option = el("option", "", text); option.value = value; responseFormat.append(option);
    });
    responseFormat.value = settings.response_format || "default";
    form.append(formatLabel, responseFormat, el("p", "form-note", "尺寸与返回格式须受所选模型支持；不确定时尺寸留空、返回格式跟随默认。每次生成一张图片，生图与对话共用每日 AI 调用额度。"));
  }
  const key = formField(form, "API Key", settings.has_api_key ? "已保存密钥；留空沿用，更换地址时请重新填写" : "填写 API Key；本地免密服务可留空", "", false, 4096);
  key.type = "password"; key.spellcheck = false;
  form.append(el("p", "form-note", isImage ? "密钥仅保存在运行服务的电脑上，页面不会回显。测试图片仅在此处预览，不发到微信群。" : "密钥仅保存在运行服务的电脑上，页面不会回显。测试消息只发给模型服务，结果显示在此处。"));
  const result = el("div", "model-test-result"); result.setAttribute("role", "status"); result.setAttribute("aria-live", "polite"); result.hidden = true;
  form.append(result);
  const footer = formFooter(form, "保存并测试");
  footer.error.setAttribute("role", "alert");
  body.append(form);
  form.addEventListener("submit", async event => {
    event.preventDefault(); if (modelSaving || mutating) return;
    modelSaving = true; mutating = true;
    const fields = [name, base, model, thinking, vision, key, size, responseFormat, ...body.querySelectorAll("button"), $("close-modal")].filter(Boolean);
    fields.forEach(node => node.disabled = true);
    footer.submit.textContent = "正在测试…"; footer.error.textContent = "";
    result.hidden = false; result.className = "model-test-result";
    result.textContent = isImage ? "正在生成测试图片，请稍候（最多 3 分钟）…" : isVision ? "正在发送内置图片测试视觉模型，请稍候…" : "正在向模型发送测试消息，请稍候…";
    try {
      const path = `/api/manage/${kind}-models${settings.id ? `/${settings.id}` : ""}`;
      const payload = {name: name.value.trim(), base_url: base.value.trim(), model: model.value.trim(), api_key: key.value.trim() || null};
      if (isImage) Object.assign(payload, {size: size.value.trim(), response_format: responseFormat.value});
      else Object.assign(payload, {enable_thinking: thinking.value === "default" ? null : thinking.value === "on",
                                   supports_vision: isVision || Boolean(vision?.checked)});
      const data = await api(path, payload, settings.id ? "PUT" : "POST", isImage ? 210000 : isVision ? 120000 : 60000);
      if (!data.saved || !data.test?.ok) {
        result.classList.add("failed"); result.textContent = `测试未通过：${data.test?.message || "请检查模型配置。"}\n原有配置保持生效，请修改后重试。`;
        return;
      }
      settings = data.profile;
      $("modal-title").textContent = `修改${label}`;
      key.value = ""; key.placeholder = settings.has_api_key ? "已保存密钥；留空沿用，更换地址时请重新填写" : "本地免密服务可留空";
      base.value = settings.base_url; name.value = settings.name;
      if (vision) vision.checked = Boolean(settings.supports_vision);
      result.classList.add("passed"); result.textContent = isImage ? data.test.message : `${data.test.message}\n模型回复：${data.test.reply}`;
      if (isImage && data.test.preview?.startsWith("data:image/png;base64,")) {
        const preview = el("img", "image-model-preview"); preview.alt = "生图连接测试预览";
        preview.addEventListener("load", () => result.scrollIntoView({block: "nearest"}), {once: true});
        preview.src = data.test.preview; result.append(preview);
      }
      await refreshState(true);
    } catch (error) {
      result.classList.add("failed"); result.textContent = error.name === "TimeoutError" ? "请求超时，请重新打开配置查看保存状态后重试。" : `保存或测试失败：${error.message}`;
    } finally {
      modelSaving = false; mutating = false; fields.forEach(node => node.disabled = false); footer.submit.textContent = "保存并测试";
    }
  });
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
  const storage = el("p", "form-note");
  const runtime = state.runtime;
  if (runtime) {
    const backup = runtime.backup;
    storage.textContent = [runtime.media_storage?.error,
      backup?.enabled ? (backup.last_error || backup.cleanup_error || (backup.last_success_at ? `最近备份：${dateText(backup.last_success_at)}，保留最近 ${backup.retained_limit} 份。` : "自动备份等待首次完成。")) : "自动备份已关闭。"].filter(Boolean).join(" ");
    const retention = runtime.retention, database = retention?.database;
    if (database) {
      storage.textContent += ` 管理库约 ${formatBytes(database.size_bytes)}：消息 ${database.chat_messages ?? 0} 条，回复凭据 ${database.chat_reply_claims ?? 0} 条，待办 ${database.group_tasks ?? 0} 条，提醒 ${database.member_reminders ?? 0} 条。${retention.chat_days ? `聊天记录保留 ${retention.chat_days} 天。` : "聊天记录长期保留。"}`;
    }
    if (retention?.last_error) storage.textContent += ` ${retention.last_error}`;
  }
  body.append(status, startup, received, storage, result, actions);
}

function callbackInfo(node, data) {
  if (!data) { node.textContent = "请更新并重启服务后查看实时回调接收状态。"; return; }
  let text = `本次启动：已接收 ${data.accepted ?? data.total ?? 0} 条，校验拒收 ${data.rejected ?? 0} 条。`;
  const processing = data.processing;
  if (processing) {
    text += ` 后台处理完成 ${processing.processed ?? 0} 条，处理失败 ${processing.failed ?? 0} 条，排队 ${processing.pending ?? 0} 条，处理中 ${processing.inflight ?? 0} 条。`;
    if (!processing.running) text += " 消息处理任务已停止，请重启服务；当前拒绝接收新回调。";
    else if (processing.recovering) text += " 正在恢复已保存的待处理回调。";
    else if (processing.storage_waiting) text += " 正在等待本地存储恢复，消息和会话顺序已保留，当前暂停接收新回调。";
    else if (processing.stalled) text += " 消息队列长时间未推进，请检查连接和存储。";
    if (processing.last_error) {
      const labels = {account_blocked:"微信账号校验受限",storage_unavailable:"本地存储暂不可用",hook_unavailable:"微信连接暂不可用",handler_failed:"后台处理异常"};
      text += ` 最近处理异常：${labels[processing.last_error.code] || "后台处理异常"}（${dateText(processing.last_error.at)}）。`;
    }
  }
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
      renderOverview(); updateView(); await loadDetail(force);
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
$("ai-model-settings").addEventListener("click", modelSettingsDialog);
$("connection-indicator").addEventListener("click", connectionDialog);
$("close-modal").addEventListener("click", () => { if (!modelSaving) $("modal").close(); });
$("modal").addEventListener("cancel", event => { if (modelSaving) event.preventDefault(); });
$("modal").addEventListener("click", event => { if (!modelSaving && event.target === $("modal")) { const rect = $("modal").getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) $("modal").close(); } });
document.addEventListener("visibilitychange", () => { clearTimeout(pollTimer); if (!document.hidden) { poll(); checkConnection(); } });
setInterval(updateTaskCountdowns, 1000);
renderOverview(); checkConnection(); poll();
