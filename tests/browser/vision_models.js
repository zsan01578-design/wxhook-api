// Run against tests/browser/mock_server.py; no real model or WeChat calls.
async page => {
  await page.unrouteAll({behavior: "wait"});
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const profiles = [], saves = [], errors = [];
  let activeId = "", fallbackIds = [], failSave = false, failLoad = false;
  page.on("pageerror", error => errors.push(error.message));
  const snapshot = () => ({configured: !!activeId, active_id: activeId, fallback_ids: fallbackIds,
    models: profiles.map(profile => ({...profile, active: profile.id === activeId})), load_error: ""});
  await page.route("**/api/manage/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
    let data = {};
    if (path.endsWith("/state")) data = {groups: [], blocked_members: [], chatbot: {enabled: true, recent_events: []}};
    else if (path.endsWith("/chat-model") || path.endsWith("/image-model")) data = {models: [], configured: false};
    else if (path.endsWith("/chat-search")) data = {enabled: false, used_today: 0, daily_limit: 1000};
    else if (path.endsWith("/ai-usage")) data = {models: []};
    else if (path.endsWith("/vision-model")) {
      if (failLoad) { await route.fulfill({status: 503, json: {error: {message: "模拟视觉配置读取失败"}}}); return; }
      data = snapshot();
    } else if (path.endsWith("/vision-model/fallbacks")) {
      fallbackIds = request.postDataJSON().fallback_ids; data = {saved: true};
    } else if (path.includes("/vision-models")) {
      const id = path.split("/")[4];
      if (path.endsWith("/activate")) { activeId = id; fallbackIds = fallbackIds.filter(key => key !== id); }
      else if (method === "DELETE") profiles.splice(profiles.findIndex(profile => profile.id === id), 1);
      else {
        const payload = request.postDataJSON(); saves.push({method, path, payload});
        if (failSave) { await route.fulfill({json: {saved: false, test: {ok: false, message: "模拟模型不支持图片输入"}}}); return; }
        const {api_key, ...settings} = payload;
        const profile = {...settings, id: id || String(profiles.length + 1), has_api_key: !!api_key || profiles.find(item => item.id === id)?.has_api_key};
        const existing = profiles.findIndex(item => item.id === id);
        if (existing >= 0) profiles[existing] = profile; else profiles.push(profile);
        activeId ||= profile.id;
        await route.fulfill({json: {saved: true, profile, test: {ok: true, message: "视觉测试通过", reply: "红色色块"}}}); return;
      }
      data = {saved: true, settings: snapshot()};
    }
    await route.fulfill({json: data});
  });
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto("http://127.0.0.1:8138/manage");
  await page.getByRole("button", {name: "AI 模型配置", exact: true}).click();
  await page.getByRole("button", {name: "视觉模型", exact: true}).click();
  assert(await page.getByRole("button", {name: "视觉模型", exact: true}).getAttribute("aria-pressed") === "true", "Vision tab is not selected");
  assert(await page.getByRole("button", {name: "配置联网搜索", exact: true}).count() === 0, "Vision tab contains chat search");
  for (const name of ["主视觉模型", "备用视觉模型"]) {
    await page.getByRole("button", {name: "添加视觉模型", exact: true}).click();
    assert(await page.getByLabel("图片尺寸", {exact: true}).count() === 0, "Image generation setting in vision editor");
    assert(await page.getByRole("checkbox").count() === 0, "Vision support should be automatic");
    await page.getByLabel("配置名称", {exact: true}).fill(name);
    await page.getByLabel("Base URL", {exact: true}).fill("https://vision.example/v1");
    await page.getByLabel("模型名称", {exact: true}).fill("example/vision");
    await page.getByLabel("API Key", {exact: true}).fill("test-key");
    await page.getByRole("button", {name: "保存并测试", exact: true}).click();
    await page.getByText("模型回复：红色色块", {exact: false}).waitFor();
    assert(saves.at(-1).payload.supports_vision === true, "Vision support not sent");
    assert(await page.getByLabel("API Key", {exact: true}).inputValue() === "", "Key not cleared");
    await page.getByRole("button", {name: "返回模型列表", exact: true}).click();
  }
  const backup = () => page.locator(".model-card").filter({has: page.getByRole("heading", {name: "备用视觉模型", exact: true})});
  await backup().getByRole("button", {name: "加入备用", exact: true}).click();
  await backup().getByText("备用 1", {exact: true}).waitFor();
  await backup().getByRole("button", {name: "查看 / 修改", exact: true}).click();
  await page.getByLabel("配置名称", {exact: true}).fill("修改后的视觉模型");
  failSave = true;
  await page.getByRole("button", {name: "保存并测试", exact: true}).click();
  await page.getByText("模拟模型不支持图片输入", {exact: false}).waitFor();
  assert(profiles[1].name === "备用视觉模型", "Failed test changed saved profile");
  failSave = false;
  await page.getByRole("button", {name: "保存并测试", exact: true}).click();
  await page.getByText("模型回复：红色色块", {exact: false}).waitFor();
  await page.getByRole("button", {name: "返回模型列表", exact: true}).click();
  await page.screenshot({path: "D:/wxhook/.tools/project-audit/vision-model-desktop.png", fullPage: true});
  await page.getByRole("button", {name: "设为首选", exact: true}).click();
  await page.locator(".model-card").filter({has: page.getByRole("heading", {name: "主视觉模型", exact: true})}).getByRole("button", {name: "删除", exact: true}).click();
  await page.getByRole("button", {name: "删除模型", exact: true}).click();
  await page.getByRole("button", {name: "添加视觉模型", exact: true}).waitFor();
  assert(profiles.length === 1 && activeId === profiles[0].id, "Wrong profile deleted or activated");
  await page.setViewportSize({width: 390, height: 844});
  await page.screenshot({path: "D:/wxhook/.tools/project-audit/vision-model-mobile.png", fullPage: true});
  await page.getByRole("button", {name: "查看 / 修改", exact: true}).click();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile page overflow");
  await page.screenshot({path: "D:/wxhook/.tools/project-audit/vision-model-mobile-editor.png", fullPage: true});
  await page.getByRole("button", {name: "返回模型列表", exact: true}).click();
  await page.getByRole("button", {name: "生图模型", exact: true}).click();
  await page.getByRole("button", {name: "添加生图模型", exact: true}).waitFor();
  await page.getByRole("button", {name: "对话模型", exact: true}).click();
  await page.getByRole("button", {name: "添加模型", exact: true}).waitFor();
  failLoad = true;
  await page.getByRole("button", {name: "视觉模型", exact: true}).click();
  await page.getByText("模拟视觉配置读取失败", {exact: true}).waitFor();
  assert(await page.getByRole("button", {name: "添加视觉模型", exact: true}).count() === 0, "Failed load permits saving defaults");
  assert(errors.length === 0, errors.join("\n"));
  return {passed: true, saves: saves.length, errors};
}
