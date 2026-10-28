"use strict";

const $ = (id) => document.getElementById(id);
let timer = null;
let configReady = false;
let saving = false;
let editVersion = 0;

function node(tag, className, text) {
  const result = document.createElement(tag);
  result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

function status(id, text, error = false) {
  const target = $(id);
  target.textContent = text;
  target.classList.toggle("error", error);
  target.classList.toggle("success", !error && Boolean(text));
}

async function request(path, options = {}) {
  const { response, data } = await fetchJSON(path, { cache: "no-store", ...options });
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail[0]?.msg : data.detail;
    throw new Error(detail || data.error?.message || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function emptyState() {
  const empty = node("div", "empty-state");
  empty.append(node("span", "empty-icon", "✉"), node("h3", "", "还没有私聊联系人"), node("p", "", "让对方给机器人发一条私聊消息，再回到这里启用。"));
  return empty;
}

function contactRow(contact) {
  const row = node("div", "contact-row");
  const main = node("div", "contact-main");
  main.append(node("div", "contact-id", contact.id));
  const name = document.createElement("input");
  name.className = "contact-name";
  name.placeholder = "备注（可选）";
  name.maxLength = 80;
  name.value = contact.name || "";
  name.setAttribute("aria-label", `联系人 ${contact.id} 的备注`);
  name.addEventListener("change", () => saveContact(contact.id, { name: name.value }));
  main.append(name);
  const meta = node("div", "contact-meta");
  const bits = [contact.enabled ? "已启用" : "未启用"];
  if (contact.last_message) bits.push(contact.last_message);
  if (contact.last_reply_at) bits.push(`最近回复 ${formatTime(contact.last_reply_at)}`);
  meta.textContent = bits.join(" · ");
  main.append(meta);
  const label = node("label", "contact-switch");
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = Boolean(contact.enabled);
  toggle.addEventListener("change", () => saveContact(contact.id, { enabled: toggle.checked }));
  label.append(toggle, document.createTextNode(contact.enabled ? "启用" : "关闭"));
  row.append(main, label);
  return row;
}

function renderContacts(state) {
  $("contact-count").textContent = `${state.contacts.length} 个`;
  $("model-status").textContent = state.model_configured ? "AI 模型已配置。" : "尚未配置 AI 模型；启用前请先在“AI 模型”页配置。";
  const list = $("contacts");
  if (!state.contacts.length) {
    list.replaceChildren(emptyState());
    return;
  }
  list.replaceChildren(...state.contacts.map(contactRow));
}

function renderForm(state) {
  $("enabled").checked = Boolean(state.enabled);
  $("personality").value = state.personality || "";
}

function updateSaveButton() {
  $("save-config").disabled = !configReady || saving;
}

function configEdited() {
  editVersion += 1;
  updateSaveButton();
  if (configReady) status("config-status", "有未保存的更改。");
}

async function load({ form = false } = {}) {
  const state = await request("/api/private/state");
  renderContacts(state);
  if (form && !configReady) {
    renderForm(state);
    configReady = true;
    updateSaveButton();
    status("config-status", "");
  }
}

async function saveContact(contact, changes) {
  try {
    const data = await request(`/api/private/contacts/${encodeURIComponent(contact)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    renderContacts(data.state);
    status("contacts-status", "联系人设置已保存。");
  } catch (error) {
    status("contacts-status", error.message, true);
    await load().catch(() => {});
  }
}

document.querySelectorAll("[data-persona]").forEach((button) => button.addEventListener("click", () => {
  $("personality").value = button.dataset.persona;
  configEdited();
  $("personality").focus();
}));

$("personality").addEventListener("input", configEdited);
$("enabled").addEventListener("change", configEdited);

$("config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!configReady || saving) return;
  saving = true;
  const submittedVersion = editVersion;
  updateSaveButton();
  status("config-status", "正在保存…");
  try {
    const data = await request("/api/private/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: $("enabled").checked, personality: $("personality").value }),
    });
    if (submittedVersion === editVersion) {
      renderForm(data.state);
      status("config-status", "已保存，下一条私聊立即生效。");
    } else {
      status("config-status", "先前设置已保存，当前更改尚未保存。");
    }
    renderContacts(data.state);
  } catch (error) {
    status("config-status", error.message, true);
  } finally {
    saving = false;
    updateSaveButton();
  }
});

$("refresh").addEventListener("click", () => load({ form: !configReady })
  .then(() => status("contacts-status", "已刷新。"))
  .catch((error) => status("contacts-status", error.message, true)));

load({ form: true }).catch((error) => status("config-status", error.message, true));

timer = setInterval(() => {
  const active = document.activeElement;
  if (active && active.closest && (active.closest("#contacts") || active.closest("#config-form"))) return;
  load().catch(() => {});
}, 5000);
window.addEventListener("beforeunload", () => clearInterval(timer));
