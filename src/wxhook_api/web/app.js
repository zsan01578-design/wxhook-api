"use strict";

const $ = (id) => document.getElementById(id);
let settings = null;
let inboxVersion = "";
let pollTimer;
let polling = false;

function feedback(id, text, kind = "") {
  const element = $(id);
  element.textContent = text;
  element.classList.remove("error", "success");
  if (kind) element.classList.add(kind);
}

async function api(path, body) {
  const options = { cache: "no-store", signal: AbortSignal.timeout(15000) };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error?.message || data.retmsg || data.msg || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

function businessSuccess(data) {
  if (!data || typeof data !== "object" || (data.ret !== 0 && data.ret !== "0")) {
    throw new Error(data?.retmsg || data?.msg || "Hook 未返回成功，请检查微信状态后再试。");
  }
}

async function loadSettings() {
  if (!settings) settings = await api("/health");
  $("hook-address").textContent = `Hook · ${settings.hook_base_url}`;
  return settings;
}

async function checkStatus() {
  const button = $("check-status");
  button.disabled = true;
  $("hook-status").textContent = "正在检查微信状态…";
  $("status-dot").className = "dot";
  try {
    await loadSettings();
    const result = await api("/QueryDB/status");
    const state = result.IsLogin ?? result.data?.IsLogin;
    if (String(state) === "1") {
      $("hook-status").textContent = "微信已登录";
      $("status-dot").className = "dot online";
    } else {
      $("hook-status").textContent = String(state) === "0" ? "Hook 已连接，微信未登录" : "Hook 已响应，登录状态未知";
      $("status-dot").className = "dot offline";
    }
  } catch (error) {
    $("hook-status").textContent = `连接失败：${error.message}`;
    $("status-dot").className = "dot offline";
  } finally {
    button.disabled = false;
  }
}

$("check-status").addEventListener("click", checkStatus);
$("setup-callback").addEventListener("click", async () => {
  const button = $("setup-callback");
  button.disabled = true;
  feedback("callback-status", "正在设置消息回调…");
  try {
    await loadSettings();
    // The Hook and this service run on the same test computer.
    const url = `http://127.0.0.1:${new URL(window.location.href).port || (window.location.protocol === "https:" ? "443" : "80")}/receiveChatBotMsg`;
    const result = await api("/set_callback", { url });
    businessSuccess(result);
    feedback("callback-status", "消息接收已启用。请让另一个微信账号给当前账号发送消息，验证是否收到。", "success");
  } catch (error) {
    feedback("callback-status", `启用失败：${error.message}`, "error");
  } finally {
    button.disabled = false;
  }
});

$("send-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("send-button");
  if (button.disabled) return;
  const recipient = $("recipient").value.trim();
  const message = $("message").value;
  if (!recipient || !message.trim()) {
    feedback("send-status", "请填写接收对象和消息内容。", "error");
    return;
  }
  button.disabled = true;
  feedback("send-status", "正在发送…");
  try {
    const result = await api("/SendTextMsg", { wxidorgid: recipient, msg: message });
    businessSuccess(result);
    feedback("send-status", `已发送给 ${recipient}`, "success");
    // Preserve any new draft entered while the previous request was in flight.
    if ($("message").value === message) $("message").value = "";
  } catch (error) {
    feedback("send-status", `发送未确认：${error.message} 请先查看微信确认结果，再决定是否重试。`, "error");
  } finally {
    button.disabled = false;
  }
});

$("message").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    $("send-form").requestSubmit();
  }
});
$("use-filehelper").addEventListener("click", () => {
  $("recipient").value = "filehelper";
  $("message").focus();
});

function node(tag, className, text) {
  const element = document.createElement(tag);
  element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function renderMessages(data) {
  const version = `${data.instance}:${data.total}`;
  if (version === inboxVersion) return;
  inboxVersion = version;
  $("message-count").textContent = `${data.messages.length} 条`;
  const list = $("message-list");
  if (!data.messages.length) {
    const empty = node("div", "empty-state");
    const icon = node("span", "empty-icon", "↙");
    icon.setAttribute("aria-hidden", "true");
    empty.append(icon, node("h3", "", "等待第一条消息"), node("p", "", "启用消息接收后，让另一个微信账号给当前账号发送一条消息。"));
    list.replaceChildren(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const message of data.messages) {
    const card = node("article", "message-card");
    const header = node("div", "message-header");
    header.append(node("span", "message-peer", message.roomid || message.sender || message.wxid || "未知来源"));
    const date = new Date(message.received_at);
    header.append(node("time", "message-time", date.toLocaleTimeString("zh-CN", { hour12: false })));
    const type = message.type === 1 ? "文本" : `消息类型 ${message.type}`;
    const meta = message.roomid ? `群消息 · ${type} · 发送人 ${message.sender || "未知"}` : `私聊 · ${type}`;
    const content = node("p", "message-content", message.content || "（内容为空）");
    const bottom = node("div", "message-bottom");
    bottom.append(node("span", "message-id", `ID ${message.msgid}`));
    const target = message.roomid || message.sender || message.wxid;
    if (target) {
      const reply = node("button", "reply-button", message.roomid ? "回复到群聊 ↗" : "回复 ↗");
      reply.type = "button";
      reply.addEventListener("click", () => {
        $("recipient").value = target;
        feedback("send-status", `已选择回复对象：${target}`);
        $("message").focus();
      });
      bottom.append(reply);
    }
    card.append(header, node("p", "message-meta", meta), content);
    if (message.truncated) card.append(node("p", "message-meta", "内容过长，仅展示前 20,000 个字符。"));
    card.append(bottom);
    fragment.append(card);
  }
  // Incoming text is always inserted with textContent, never interpreted as HTML.
  list.replaceChildren(fragment);
}

async function pollMessages() {
  clearTimeout(pollTimer);
  if (document.hidden || polling) return;
  polling = true;
  try {
    const data = await api("/ui/messages");
    if (data.startup) {
      const phase = data.startup.phase;
      feedback("callback-status", data.startup.message, phase === "ready" ? "success" : phase === "error" ? "error" : "");
      if (phase === "ready") {
        $("hook-status").textContent = "微信已登录";
        $("status-dot").className = "dot online";
      } else if (phase === "waiting_login") {
        $("hook-status").textContent = "Hook 已连接，等待微信登录";
        $("status-dot").className = "dot offline";
      } else if (phase === "waiting_hook" || phase === "starting") {
        $("hook-status").textContent = "正在等待微信 Hook…";
        $("status-dot").className = "dot";
      }
    }
    renderMessages(data);
    feedback("inbox-status", `已接收 ${data.total} 条 · 正在等待新消息`);
  } catch (error) {
    feedback("inbox-status", `消息刷新失败：${error.message}`, "error");
  } finally {
    polling = false;
    if (!document.hidden) pollTimer = setTimeout(pollMessages, 2000);
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) pollMessages();
  else clearTimeout(pollTimer);
});
checkStatus();
pollMessages();
