async page => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let callbacks = {accepted: 4, rejected: 0, processing: {processed: 2, failed: 1, pending: 1, inflight: 0,
    running: false, last_error: {at: "2026-10-23T01:00:00Z", code: "storage_unavailable"}}};
  let backup = {enabled: true, last_error: "备份失败，请检查磁盘空间。"};
  await page.route("**/ui/assets/**", route => route.continue({url: route.request().url().replace("/ui/assets/", "/web/")}));
  await page.route("**/api/**", route => {
    const url = route.request().url();
    if (url.includes("/api/callback/status")) return route.fulfill({json: callbacks});
    return route.fulfill({json: {groups: [], blocked_members: [], callbacks,
      runtime: {backup,
        media_storage: {error: "磁盘剩余空间不足，已暂停新增文件。"}}}});
  });
  await page.route("**/GetSelfProfile", route => route.fulfill({json: {wxid: "mock"}}));
  await page.route("**/QueryDB/status", route => route.fulfill({json: {IsLogin: 1}}));
  await page.goto("http://127.0.0.1:8137/web/manage.html");
  await page.waitForFunction(() => state.callbacks?.processing);
  await page.locator("#connection-settings").click();
  let text = await page.locator("dialog").innerText();
  assert(text.includes("后台处理完成 2 条，处理失败 1 条，排队 1 条"), "delivery counters missing");
  assert(text.includes("消息处理任务已停止") && text.includes("本地存储暂不可用"), "worker failure not visible");
  assert(text.includes("已暂停新增文件") && text.includes("备份失败"), "storage protection status missing");
  backup = {enabled: true, last_error: "", cleanup_error: "部分旧备份暂时无法清理；最新备份仍可使用。",
    last_success_at: "2026-10-23T01:00:00Z", retained_limit: 7};
  await page.locator("#close-modal").click();
  await page.evaluate(() => refreshState(true));
  await page.locator("#connection-settings").click();
  text = await page.locator("dialog").innerText();
  assert(text.includes("旧备份暂时无法清理") && text.includes("最新备份仍可使用"), "retention warning hidden by snapshot success");
  assert(!text.includes("备份失败"), "retention failure presented as snapshot failure");
  backup.cleanup_error = "";
  await page.locator("#close-modal").click();
  await page.evaluate(() => refreshState(true));
  await page.locator("#connection-settings").click();
  text = await page.locator("dialog").innerText();
  assert(text.includes("最近备份") && !text.includes("旧备份暂时无法清理"), "retention recovery warning remained");
  callbacks = {accepted: 5, rejected: 0, processing: {processed: 4, failed: 1, pending: 0, inflight: 1, running: true, storage_waiting: 1}};
  await page.getByRole("button", {name: "检查连接", exact: true}).click();
  await page.waitForFunction(() => document.querySelector("dialog").textContent.includes("正在等待本地存储恢复"));
  text = await page.locator("dialog").innerText();
  assert(text.includes("当前暂停接收新回调"), "storage wait did not explain ingress pause");
  callbacks = {accepted: 5, rejected: 0, processing: {processed: 4, failed: 1, pending: 0, inflight: 0, running: true, storage_waiting: 0}};
  await page.getByRole("button", {name: "检查连接", exact: true}).click();
  await page.waitForFunction(() => {
    const text = document.querySelector("dialog").textContent;
    return text.includes("后台处理完成 4 条") && !text.includes("正在等待本地存储恢复");
  });
  text = await page.locator("dialog").innerText();
  assert(!text.includes("消息处理任务已停止"), "recovered worker warning remained");
  assert(!text.includes("正在等待本地存储恢复"), "recovered storage warning remained");
  assert(errors.length === 0, errors.join("\n"));
  return {passed: true, checks: ["delivery counters", "worker exit", "safe error messages", "disk and backup status", "backup retention and recovery", "storage wait and recovery", "recovery"]};
}
