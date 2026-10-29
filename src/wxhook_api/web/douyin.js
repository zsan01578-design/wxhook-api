"use strict";

const element = (id) => document.getElementById(id);
let revision = "";
let timer;
let polling = false;
let configReady = false;

function updateProvider() {
  element("api-settings").hidden = element("parser-provider").value === "builtin";
}
element("parser-provider").addEventListener("change", updateProvider);

function message(id, text, error = false) {
  element(id).textContent = text;
  element(id).classList.toggle("error", error);
}

async function request(path, body) {
  const options = { cache: "no-store" };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const { response, data } = await fetchJSON(path, options);
  if (!response.ok) {
    if (data.error?.code === "validation_error" && data.error.message === "request validation failed") {
      throw new Error("后台拒绝此配置。请先停止旧服务并重新运行 main.py，再刷新页面；同时确认填写的是群 ID，不能填群昵称。");
    }
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
  configReady = false;
  element("save-settings").disabled = true;
  try {
    const data = await request("/api/douyin/config");
    element("parser-provider").value = data.settings.parser_provider || "parse_video";
    updateProvider();
    element("parser-api-url").value = data.settings.parser_api_url || "";
    element("media-parser-url").value = data.settings.media_parser_url || "";
    element("enabled").checked = data.settings.enabled;
    element("groups").value = data.settings.groups.join("\n");
    element("auto-download").checked = data.settings.auto_download;
    element("notify-group").checked = data.settings.notify_group ?? true;
    element("send-results").checked = data.settings.send_results ?? true;
    element("output-dir").textContent = data.output_dir;
    element("file-limit").textContent = `单文件上限 ${data.max_file_mb} MB；图集按原顺序保存。`;
    const cleanup = data.cleanup_policy;
    element("cleanup-policy").textContent = cleanup?.enabled ? `每天 ${String(cleanup.hour).padStart(2, "0")}:00（北京时间）清理：已回传媒体保留 ${cleanup.sent_hours} 小时，其他媒体保留 ${cleanup.other_hours / 24} 天，解析记录保留 ${cleanup.record_days} 天。服务启动时也会补清理过期文件。` : "自动清理已关闭，媒体文件会继续保留在本机。";
    message("config-status", data.settings.enabled && data.settings.groups.length ? `默认名单已启用 ${data.settings.groups.length} 个群，独立配置以群管理为准。` : "默认群名单未启用，独立配置以群管理为准。");
    if (element("parser-provider").value === "api" && !element("parser-api-url").value) message("config-status", "请填写完整解析接口地址并保存。", true);
    if (element("parser-provider").value === "media_parser" && !element("media-parser-url").value.trim()) message("config-status", "选择 media-parser 时请填写本机服务地址，例如 http://127.0.0.1:8051。", true);
    if (data.feedback_available === false) message("config-status", "群内发送总开关已关闭；如需提示和回传，请检查 WXHOOK_DOUYIN_FEEDBACK_ENABLED 后重启。", true);
    if (!data.supported_parsers?.includes("parse_video")) {
      message("config-status", "检测到页面与后台版本不一致。请停止原 PyCharm 任务或原终端服务，重新运行根目录 main.py 后刷新页面。", true);
    } else {
      configReady = true;
      element("save-settings").disabled = false;
    }
  } catch (error) {
    message("config-status", `读取配置失败：${error.message}`, true);
  }
}

element("group-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = element("save-settings");
  if (!configReady || button.disabled) return;
  button.disabled = true;
  message("config-status", "正在保存…");
  try {
    const groups = [...new Set(element("groups").value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean))];
    const enabled = element("enabled").checked;
    const provider = element("parser-provider").value;
    const apiUrl = element("parser-api-url").value.trim();
    const mediaUrl = element("media-parser-url").value.trim();
    await request("/api/douyin/config", { enabled, groups, auto_download: element("auto-download").checked, notify_group: element("notify-group").checked, send_results: element("send-results").checked, parser_provider: provider, parser_api_url: apiUrl, media_parser_url: mediaUrl });
    element("groups").value = groups.join("\n");
    message("config-status", enabled && groups.length ? `默认名单已保存，共 ${groups.length} 个群；独立配置以群管理为准。` : "已保存，默认群名单未启用；独立配置以群管理为准。");
    if (provider === "api" && !apiUrl) message("config-status", "配置已保存，但接口地址为空，收到链接时将提示解析失败。", true);
    if (provider === "media_parser" && !mediaUrl) message("config-status", "配置已保存，但 media-parser 地址为空，解析会失败。", true);
  } catch (error) {
    message("config-status", `保存失败：${error.message}`, true);
  } finally {
    button.disabled = !configReady;
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

function syncChildren(parent, children) {
  const wanted = new Set(children);
  for (const child of [...parent.children]) if (!wanted.has(child)) child.remove();
  let cursor = parent.firstElementChild;
  for (const child of children) {
    if (child === cursor) cursor = cursor.nextElementSibling;
    else if (parent.moveBefore && child.parentNode === parent) parent.moveBefore(child, cursor);
    else parent.insertBefore(child, cursor);
  }
}

function renderJobs(data) {
  if (data.revision === revision) return;
  revision = data.revision;
  element("job-count").textContent = `${data.jobs.length} 条`;
  if (!data.jobs.length) {
    const empty = node("div", "empty-state");
    empty.append(node("h3", "", "暂无解析记录"), node("p", "", "新任务的解析结果会显示在这里，过期记录会自动清理。"));
    const settings = node("a", "inline-link empty-action", "前往解析设置 →");
    settings.href = "#settings";
    empty.append(settings);
    element("jobs").replaceChildren(empty);
    return;
  }
  const openJobs = new Set([...element("jobs").querySelectorAll("details[open]")].map((item) => item.dataset.job));
  const cards = new Map([...element("jobs").querySelectorAll(".media-job")].map(card => [card.dataset.job, card]));
  const orderedCards = [];
  for (const job of data.jobs) {
    const card = cards.get(job.id) || node("article", "message-card media-job");
    card.dataset.job = job.id;
    const info = node("div", "media-job-info");
    const header = node("div", "message-header");
    const label = job.status === "failed" || job.status === "interrupted" ? states[job.status] : deliveries[job.delivery_status] || states[job.status] || job.status;
    const hasError = job.status === "failed" || ["partial", "failed", "unknown"].includes(job.delivery_status);
    header.append(node("strong", "message-peer", job.title || "群聊分享"), node("span", hasError ? "count error" : "count", label));
    info.append(header, node("p", "message-meta", `${job.group_id} · ${job.sender}`));
    if (job.author) info.append(node("p", "message-meta", `作者：${job.author} · ${job.kind === "images" ? "图集" : "视频"}`));
    if (job.parser_provider) info.append(node("p", "message-meta", `解析来源：${{parse_video: "parse-video-py", api: "指定接口", builtin: "公开分享页", media_parser: "media-parser"}[job.parser_provider] || job.parser_provider}${job.parser_fallback ? "（失败后回退）" : ""}${job.cache_hit ? " · 缓存命中" : ""}${job.platform ? ` · ${{douyin: "抖音", xiaohongshu: "小红书", bilibili: "B站"}[job.platform] || job.platform}` : ""}`));
    if (job.error) info.append(node("p", "feedback error", job.error));
    if (job.delivery_error) info.append(node("p", "feedback error", `回传：${job.delivery_error}`));
    if (job.storage_error) info.append(node("p", "feedback error", job.storage_error));
    if (job.cleanup_error) info.append(node("p", "feedback error", job.cleanup_error));
    if (job.files_cleaned_at) info.append(node("p", "message-meta", `文件已清理 · ${new Date(job.files_cleaned_at).toLocaleString("zh-CN", {hour12: false})} · 解析记录仍保留`));
    else if (job.files?.some(file => file.cleaned)) info.append(node("p", "message-meta", "部分文件已按保留规则清理。"));
    const noticeNames = { start: "开始提示", failure: "失败提示", delivery_failure: "回传失败提示", complete: "完成提示" };
    for (const [stage, result] of Object.entries(job.notifications || {})) {
      if (["failed", "unknown"].includes(result.status)) info.append(node("p", "feedback error", `${noticeNames[stage] || "群提示"}未确认：${result.error || "请检查 Hook"}`));
    }
    info.append(safeLink(job.source_url, "原始分享 ↗"));
    const oldInfo = card.querySelector(".media-job-info");
    if (oldInfo) oldInfo.replaceWith(info); else card.prepend(info);
    card.querySelector(".asset-links")?.remove();
    const files = job.files_cleaned_at ? [] : (job.files || []).filter(file => !file.cleaned);
    if (files.length) {
      let details = card.querySelector(".media-details");
      if (!details) {
        details = node("details", "media-details");
        details.append(node("summary"), node("div", "media-grid"));
        details.open = openJobs.has(job.id);
        card.append(details);
      }
      details.dataset.job = job.id;
      details.querySelector("summary").textContent = `查看已保存文件（${files.length} 个）`;
      const grid = details.querySelector(".media-grid");
      const tiles = new Map([...grid.children].map(tile => [tile.dataset.file, tile]));
      const orderedTiles = [];
      for (const file of files) {
        const url = `/api/douyin/files/${encodeURIComponent(job.id)}/${encodeURIComponent(file.name)}`;
        const key = JSON.stringify([file.name, file.kind]);
        let tile = tiles.get(key);
        if (!tile) {
          tile = node("div", "media-tile");
          tile.dataset.file = key;
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
          tile.append(safeLink(`${url}?download=true`, ""));
        }
        if (file.kind === "image") tile.querySelector("img").alt = `${job.title || "图集"} ${file.name}`;
        tile.querySelector("a").textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB ↓`;
        tile.querySelector(".file-send-status")?.remove();
        if (file.send_status) tile.append(node("p", "message-meta file-send-status", {sent: "Hook 已确认发送", sending: "正在发送", failed: "发送失败", unknown: "发送结果待确认", skipped: "已停止回传"}[file.send_status] || file.send_status));
        orderedTiles.push(tile);
      }
      syncChildren(grid, orderedTiles);
    } else {
      card.querySelector(".media-details")?.remove();
      if (job.status === "parsed" && !job.files_cleaned_at) {
        const links = node("div", "asset-links");
        (job.assets || []).forEach((asset, index) => {
          if (asset.urls?.[0]) links.append(safeLink(asset.urls[0], `${asset.kind === "image" ? "图片" : "视频"} ${index + 1} ↗`));
        });
        card.append(links);
      }
    }
    orderedCards.push(card);
  }
  syncChildren(element("jobs"), orderedCards);
}

async function poll() {
  clearTimeout(timer);
  if (polling || document.hidden) return;
  polling = true;
  try {
    const data = await request("/api/douyin/jobs");
    renderJobs(data);
    const cleanup = data.cleanup;
    if (cleanup?.enabled) {
      const next = cleanup.next_run_at ? `下次清理：${new Date(cleanup.next_run_at).toLocaleString("zh-CN", {hour12:false})}` : "正在检查过期文件";
      const last = cleanup.last_run_at ? ` · 上次清理 ${cleanup.deleted_files || 0} 个媒体文件、${cleanup.deleted_records || 0} 条过期记录，释放 ${((cleanup.bytes_freed || 0) / 1024 / 1024).toFixed(1)} MB` : "";
      const skipped = cleanup.skipped_unsafe ? ` · ${cleanup.skipped_unsafe} 个目录无法验证，已跳过` : "";
      message("cleanup-status", cleanup.error || `${next}${last}${skipped}`, Boolean(cleanup.error));
    } else message("cleanup-status", "");
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
