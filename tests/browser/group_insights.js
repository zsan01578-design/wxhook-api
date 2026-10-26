// Run against tests/browser/mock_server.py. All model results are mocked.
async page => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const group = "insights-ui@chatroom", other = "insights-ui-other@chatroom";
  const errors = [], calls = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.request.post("http://127.0.0.1:8138/api/manage/groups", {data:{id:group, name:"群聊趣味测试"}});
  await page.request.post("http://127.0.0.1:8138/api/manage/groups", {data:{id:other, name:"另一个趣味群"}});
  let release = null;
  const png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII=";
  await page.route("**/insights/*", async route => {
    const body = route.request().postDataJSON();
    calls.push({url:route.request().url(), body});
    if (body.term === "延迟测试") await new Promise(resolve => { release = resolve; });
    if (body.term === "失败测试") { await route.fulfill({status:409, json:{error:{message:"模型暂不可用，请重试。"}}}); return; }
    const kind = route.request().url().split("/").pop();
    await route.fulfill({json:{group_id:group, kind, text:kind === "comic" ? "格1：修复故障\n格2：庆祝\n格3：测试\n格4：收尾" : "周三战神：本机归档中的故事 <script>不会执行</script>", message_count:20, sampled_count:12,
      sources:[{id:123, group_id:group, name:"成员 <img onerror=alert(1)>", timestamp:1793000000, content:"周三终于修好啦 <script>原话</script>"}],
      ...(kind === "comic" ? {image_data_url:png} : {})}});
  });
  const results = [];
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height:900});
    await page.goto("http://127.0.0.1:8138/manage");
    await page.getByRole("button").filter({hasText:"群聊趣味测试"}).first().click();
    await page.locator('[data-tab="tools"]').click();
    await page.getByRole("button", {name:"群聊梗百科", exact:true}).click();
    await page.getByLabel("梗关键词", {exact:true}).fill("周三战神");
    // Polling must not reset the form or trigger a model request.
    const before = calls.length;
    await page.evaluate(() => { detailVersion = ""; return loadDetail(); });
    assert(await page.getByLabel("梗关键词", {exact:true}).inputValue() === "周三战神", "Draft lost during refresh");
    assert(calls.length === before, "Polling unexpectedly called a model");
    await page.getByRole("button", {name:"查找这个梗", exact:true}).click();
    await page.locator(".insight-sources").waitFor();
    assert((await page.locator(".insight-text").first().textContent()).includes("<script>不会执行</script>"), "Model text must be literal text");
    assert(await page.locator(".insight-sources img,.insight-sources script").count() === 0, "Source HTML was executed");
    await page.locator("#close-modal").click();
    await page.getByRole("button", {name:"爬楼助手", exact:true}).click();
    await page.getByLabel("聊天范围（北京时间）", {exact:true}).selectOption("最近2小时");
    await page.getByLabel("从消息编号之后（可选）", {exact:true}).fill("123");
    await page.getByRole("button", {name:"开始爬楼", exact:true}).click();
    await page.locator(".insight-sources").waitFor();
    assert(calls.at(-1).body.after_id === 123 && calls.at(-1).body.window === "最近2小时", "Catch-up range was not sent");
    await page.locator("#close-modal").click();
    await page.getByRole("button", {name:"聊天变漫画", exact:true}).click();
    await page.getByLabel("仅选这些消息编号（可选）", {exact:true}).fill("123,124,130");
    await page.getByRole("button", {name:"生成四格漫画", exact:true}).click();
    await page.getByRole("link", {name:"下载漫画", exact:true}).waitFor();
    assert(JSON.stringify(calls.at(-1).body.message_ids) === "[123,124,130]", "Selected messages were not sent");
    assert(await page.locator(".favorite-full-media").evaluate(image => image.complete && image.naturalWidth > 0), "Comic image did not render");
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "Page overflows");
    assert(await page.locator("#modal").evaluate(modal => modal.scrollWidth <= modal.clientWidth + 1), "Dialog overflows");
    await page.screenshot({path:`D:/wxhook/.tools/group-insights-${width}.png`, fullPage:true});
    await page.locator("#close-modal").click();
    results.push({width, meme:true, catchup:true, comic:true, sources:true, draftPreserved:true});
  }
  // A late reply must not replace a newly opened dialog.
  await page.getByRole("button", {name:"群聊梗百科", exact:true}).click();
  await page.getByLabel("梗关键词", {exact:true}).fill("延迟测试");
  await page.getByRole("button", {name:"查找这个梗", exact:true}).click();
  await page.waitForFunction(() => document.querySelector(".insight-result")?.textContent.includes("正在分析"));
  await page.locator("#close-modal").click();
  await page.getByRole("button", {name:"爬楼助手", exact:true}).click();
  assert(release, "Slow request did not start"); release();
  await page.waitForResponse(response => response.url().includes("/insights/meme"));
  assert(await page.locator("#modal-title").textContent() === "爬楼助手", "Late reply replaced new dialog");
  assert(await page.locator(".insight-sources").count() === 0, "Late sources leaked into new dialog");
  await page.locator("#close-modal").click();
  await page.getByRole("button", {name:"群聊梗百科", exact:true}).click();
  await page.getByLabel("梗关键词", {exact:true}).fill("失败测试");
  await page.getByRole("button", {name:"查找这个梗", exact:true}).click();
  await page.getByText("模型暂不可用，请重试。", {exact:true}).waitFor();
  assert(await page.getByRole("button", {name:"查找这个梗", exact:true}).isEnabled(), "Retry stayed disabled");
  await page.locator("#close-modal").click();
  assert(!errors.length, JSON.stringify(errors));
  return {viewports:results, staleDialogProtected:true, failureRetry:true, calls:calls.length, uncaughtErrors:errors};
}
