// Run via playwright-cli against tests/browser/mock_server.py; no real providers.
async page => {
  await page.unrouteAll({behavior: "wait"});
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const errors = [], changes = [], orders = {chat: [], image: []};
  page.on("pageerror", error => errors.push(error.message));
  const snapshot = kind => ({configured: true, active_id: "a", fallback_ids: orders[kind],
    models: ["a", "b", "c"].map((id, index) => ({id, name: ["首选平台", "备用平台B", "备用平台C"][index],
      model: `${kind}-${id}`, base_url: `https://${id}.example/v1`, has_api_key: true, active: id === "a", size: "1024x1024", response_format: "default"})),
    last_route: {used_id: "b", attempts: [{profile_id: "a", status: "failed", message: "模型账号余额不足"}, {profile_id: "b", status: "success"}]}});
  await page.route("**/api/manage/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let data = {};
    if (path.endsWith("/state")) data = {groups: [], blocked_members: [], sync: {enabled: false}, chatbot: {enabled: true, recent_events: []}};
    else if (path.endsWith("/chat-search")) data = {enabled: false, used_today: 0, daily_limit: 1000};
    else if (/\/(chat|image)-model\/fallbacks$/.test(path)) {
      const kind = path.includes("/image-") ? "image" : "chat";
      assert(route.request().method() === "PUT", "wrong method");
      orders[kind] = route.request().postDataJSON().fallback_ids;
      changes.push({kind, ids: [...orders[kind]]});
      data = {saved: true, settings: snapshot(kind)};
    } else if (/\/(chat|image)-model$/.test(path)) data = snapshot(path.includes("/image-") ? "image" : "chat");
    await route.fulfill({json: data});
  });
  await page.setViewportSize({width: 1400, height: 1100});
  await page.goto("http://127.0.0.1:8138/manage");
  await page.getByRole("button", {name: "AI 模型配置", exact: true}).click();
  const card = name => page.locator(".model-card").filter({has: page.getByRole("heading", {name, exact: true})});
  for (const kind of ["chat", "image"]) {
    if (kind === "image") await page.getByRole("button", {name: "生图模型", exact: true}).click();
    await card("备用平台B").getByRole("button", {name: "加入备用", exact: true}).click();
    await page.waitForFunction(() => !modelSaving);
    await card("备用平台C").getByRole("button", {name: "加入备用", exact: true}).click();
    await page.waitForFunction(() => !modelSaving);
    await card("备用平台C").getByRole("button", {name: "上移", exact: true}).click();
    await page.waitForFunction(() => !modelSaving);
    assert(orders[kind].join(",") === "c,b", "wrong fallback priority");
    assert(await card("备用平台C").getByRole("button", {name: "上移", exact: true}).isDisabled(), "first backup can move above primary");
    assert(await card("备用平台B").getByRole("button", {name: "下移", exact: true}).isDisabled(), "last backup can move below end");
    await card("备用平台C").getByRole("button", {name: "下移", exact: true}).click();
    await page.waitForFunction(() => !modelSaving);
    assert(orders[kind].join(",") === "b,c", "down move failed");
    await card("备用平台B").getByRole("button", {name: "移出备用", exact: true}).click();
    await page.waitForFunction(() => !modelSaving);
    assert(orders[kind].join(",") === "c", "remove failed");
    assert(await page.getByText("最近完成的调用：使用 备用平台B", {exact: true}).isVisible(), "actual provider missing");
  }
  await page.setViewportSize({width: 420, height: 900});
  await page.screenshot({path: "D:/wxhook/.tools/project-audit/model-failover-mobile.png", fullPage: true});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "mobile overflow");
  assert(errors.length === 0, errors.join("\n"));
  return {passed: true, changes};
}
