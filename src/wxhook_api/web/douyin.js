"use strict";

const element = (id) => document.getElementById(id);
let revision = "";
let timer;
let polling = false;

function message(id, text, error = false) {
  element(id).textContent = text;
  element(id).classList.toggle("error", error);
}

async function request(path, body) {
  const options = { cache: "no-store", signal: AbortSignal.timeout(15000) };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail[0]?.msg : data.detail;
    throw new Error(detail || data.error?.message || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

function node(tag, className, text) {
  const result = document.createElement(tag);
  result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

async function loadConfig() {
  try {
    const data = await request("/api/douyin/config");
    element("enabled").checked = data.settings.enabled;
    element("groups").value = data.settings.groups.join("\n");
    element("auto-download").checked = data.settings.auto_download;
    element("notify-group").checked = data.settings.notify_group ?? true;
    element("send-results").checked = data.settings.send_results ?? true;
    element("output-dir").textContent = data.output_dir;
    element("file-limit").textContent = `单文件上限 ${data.max_file_mb} MB；图集按原顺序保存。`;
    message("config-status", data.settings.enabled && data.settings.groups.length ? `默认名单已启用 ${data.settings.groups.length} 个群，独立配置以群管理为准。` : "默认群名单未启用，独立配置以群管理为准。");
    if (data.feedback_available === false) message("config-status", "群内发送总开关已关闭；如需提示和回传，请检查 WXHOOK_DOUYIN_FEEDBACK_ENABLED 后重启。", true);
  } catch (error) {
    message("config-status", `读取配置失败：${error.message}`, true);
  }
}

element("group-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = element("save-settings");
  button.disabled = true;
  message("config-status", "正在保存…");
  try {
    const groups = [...new Set(element("groups").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean))];
    const enabled = element("enabled").checked;
    await request("/api/douyin/config", { enabled, groups, auto_download: element("auto-download").checked, notify_group: element("notify-group").checked, send_results: element("send-results").checked });
    element("groups").value = groups.join("\n");
    message("config-status", enabled && groups.length ? `默认名单已保存，共 ${groups.length} 个群；独立配置以群管理为准。` : "已保存，默认群名单未启用；独立配置以群管理为准。");
  } catch (error) {
    message("config-status", `保存失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
});

async function recentGroups() {
  try {
    const data = await request("/ui/messages");
    const groups = [...new Set(data.messages.map((item) => item.roomid || (item.wxid?.endsWith("@chatroom") ? item.wxid : "")).filter(Boolean))];
    if (!groups.length) return;
    const fragment = document.createDocumentFragment();
    for (const group of groups) {
      const button = node("button", "secondary group-choice", `+ ${group}`);
      button.type = "button";
      button.addEventListener("click", () => {
        const existing = element("groups").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
        element("groups").value = [...new Set([...existing, group])].join("\n");
        message("config-status", "已添加群 ID，请点击保存群配置。");
      });
      fragment.append(button);
    }
    element("recent-groups").replaceChildren(fragment);
  } catch (_) {
    // Group discovery is optional; the configured list remains editable.
  }
}

const states = { queued: "排队中", parsing: "解析中", downloading: "保存中", sending: "回传中", completed: "已保存", parsed: "已解析", failed: "解析失败", interrupted: "已中断" };
const deliveries = { sent: "已回传", partial: "部分回传", failed: "回传失败", unknown: "回传待确认" };

function safeLink(url, label, className = "text-button") {
  try {
    const target = new URL(url, window.location.origin);
    if (!["http:", "https:"].includes(target.protocol)) return node("span", "", "无效链接");
    const link = node("a", className, label);
    link.href = target.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch (_) {
    return node("span", "", "无效链接");
  }
}

function renderJobs(data) {
  if (data.revision === revision) return;
  revision = data.revision;
  element("job-count").textContent = `${data.jobs.length} 条`;
  if (!data.jobs.length) return;
  const openJobs = new Set([...element("jobs").querySelectorAll("details[open]")].map((item) => item.dataset.job));
  const fragment = document.createDocumentFragment();
  for (const job of data.jobs) {
    const card = node("article", "message-card media-job");
    const header = node("div", "message-header");
    const label = job.status === "failed" || job.status === "interrupted" ? states[job.status] : deliveries[job.delivery_status] || states[job.status] || job.status;
    const hasError = job.status === "failed" || ["partial", "failed", "unknown"].includes(job.delivery_status);
    header.append(node("strong", "message-peer", job.title || "抖音分享"), node("span", hasError ? "count error" : "count", label));
    card.append(header, node("p", "message-meta", `${job.group_id} · ${job.sender}`));
    if (job.author) card.append(node("p", "message-meta", `作者：${job.author} · ${job.kind === "images" ? "图集" : "视频"}`));
    if (job.error) card.append(node("p", "feedback error", job.error));
    if (job.delivery_error) card.append(node("p", "feedback error", `回传：${job.delivery_error}`));
    if (job.storage_error) card.append(node("p", "feedback error", job.storage_error));
    const noticeNames = { start: "开始提示", failure: "失败提示", delivery_failure: "回传失败提示", complete: "完成提示" };
    for (const [stage, result] of Object.entries(job.notifications || {})) {
      if (["failed", "unknown"].includes(result.status)) card.append(node("p", "feedback error", `${noticeNames[stage] || "群提示"}未确认：${result.error || "请检查 Hook"}`));
    }
    card.append(safeLink(job.source_url, "原始分享 ↗"));
    const files = job.files || [];
    if (files.length) {
      const details = node("details", "media-details");
      details.dataset.job = job.id;
      details.open = openJobs.has(job.id);
      details.append(node("summary", "", `查看已保存文件（${files.length} 个）`));
      const grid = node("div", "media-grid");
      for (const file of files) {
        const url = `/api/douyin/files/${encodeURIComponent(job.id)}/${encodeURIComponent(file.name)}`;
        const tile = node("div", "media-tile");
        if (file.kind === "image") {
          const image = node("img", "media-preview");
          image.loading = "lazy";
          image.alt = `${job.title || "图集"} ${file.name}`;
          image.src = url;
          tile.append(image);
        } else {
          const video = node("video", "media-preview");
          video.controls = true;
          video.preload = "none";
          video.src = url;
          tile.append(video);
        }
        tile.append(safeLink(`${url}?download=true`, `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB ↓`));
        if (file.send_status) tile.append(node("p", "message-meta", {sent: "Hook 已确认发送", sending: "正在发送", failed: "发送失败", unknown: "发送结果待确认", skipped: "已停止回传"}[file.send_status] || file.send_status));
        grid.append(tile);
      }
      details.append(grid);
      card.append(details);
    } else if (job.status === "parsed") {
      const links = node("div", "asset-links");
      (job.assets || []).forEach((asset, index) => {
        if (asset.urls?.[0]) links.append(safeLink(asset.urls[0], `${asset.kind === "image" ? "图片" : "视频"} ${index + 1} ↗`));
      });
      card.append(links);
    }
    fragment.append(card);
  }
  element("jobs").replaceChildren(fragment);
}

async function poll() {
  clearTimeout(timer);
  if (polling || document.hidden) return;
  polling = true;
  try {
    const data = await request("/api/douyin/jobs");
    renderJobs(data);
    message("jobs-status", `正在等待群内分享 · 队列中 ${data.pending} 个任务`);
    await recentGroups();
  } catch (error) {
    message("jobs-status", `刷新失败：${error.message}`, true);
  } finally {
    polling = false;
    if (!document.hidden) timer = setTimeout(poll, 2000);
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(timer);
  else poll();
});
loadConfig();
poll();
