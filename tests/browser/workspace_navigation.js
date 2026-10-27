// Run against tests/browser/mock_server.py. Uses only its isolated data.
async page => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  await page.unrouteAll({behavior: "wait"});
  const errors = [];
  const onError = error => errors.push(error.message);
  page.on("pageerror", onError);
  const base = "http://127.0.0.1:8138";
  const sizes = [1440, 1024, 768, 390, 320];
  const openSystem = async () => {
    const toggle = page.locator(".app-menu-toggle");
    if (await toggle.isVisible() && await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
  };
  const noOverflow = async context => {
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Horizontal overflow: ${context}`);
  };
  for (const width of sizes) {
    await page.setViewportSize({width, height: 900});
    for (const [path, label] of [["/", "群聊管理"], ["/private", "私聊问答"], ["/messages", "微信消息"], ["/douyin", "分享解析"]]) {
      await page.goto(base + path);
      const nav = page.getByRole("navigation", {name: "主导航", exact: true});
      await nav.locator(".app-nav-link").first().waitFor();
      assert(await nav.locator(".app-nav-link").count() === 8, `Missing shared destinations on ${path}`);
      assert((await nav.locator('[aria-current="page"]').textContent()).trim() === label, `Wrong active destination on ${path}`);
      await noOverflow(`${path} at ${width}`);
      if (width === 1440 || width === 390) await page.screenshot({path: `D:/wxhook/.tools/project-audit/layout-${path === "/" ? "groups" : path.slice(1)}-${width}.png`, fullPage: true});
    }
    // Switching views retains an unsubmitted edit and supports browser history.
    await page.getByRole("link", {name: "解析设置", exact: true}).first().click();
    await page.waitForFunction(() => !document.getElementById("save-settings").disabled);
    await page.locator("#groups").fill("layout-draft@chatroom");
    await noOverflow(`resource settings at ${width}`);
    if (width === 1440 || width === 390) await page.screenshot({path: `D:/wxhook/.tools/project-audit/layout-settings-${width}.png`, fullPage: true});
    await page.getByRole("link", {name: "解析记录", exact: true}).click();
    assert(await page.locator("#resource-results").isVisible(), "Results view did not open");
    await page.goBack();
    assert(await page.locator("#groups").isVisible(), "History did not restore settings");
    assert(await page.locator("#groups").inputValue() === "layout-draft@chatroom", "Switching views discarded edits");

    // Every utility page links directly to working management dialogs/views.
    await page.goto(base + "/private");
    await openSystem();
    await page.locator("#ai-model-settings").click();
    await page.locator("#modal[open]").waitFor();
    assert((await page.locator("#modal-title").textContent()).includes("模型"), "Cross-page model settings did not open");
    await page.locator("#close-modal").click();
    assert(!new URL(page.url()).hash, "Closed settings left a stale URL");
    await openSystem();
    await page.locator("#connection-settings").click();
    await page.getByRole("heading", {name: "连接与消息接收"}).waitFor();
    await page.keyboard.press("Escape");
    await page.locator("#modal").waitFor({state: "hidden"});
    await openSystem();
    await page.locator("#nav-blacklist").click();
    await page.locator("#blacklist-view").waitFor();
    assert(!await page.locator(".conversations").isVisible(), "Blacklist still shows unrelated group list");
    await noOverflow(`blacklist at ${width}`);
    await page.locator("#nav-groups").click();
    await page.locator(".conversations").waitFor();
    await page.goBack();
    await page.locator("#blacklist-view").waitFor();
    await page.goForward();
    await page.locator(".conversations").waitFor();
  }
  page.off("pageerror", onError);
  assert(errors.length === 0, `JavaScript errors: ${errors.join("; ")}`);
  return {widths: sizes, pages: 4, sharedDestinations: 8, deepLinks: true, history: true, draftRetained: true, errors};
}
