// Run through playwright-cli against the local static server on port 8137.
async page => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  await page.unrouteAll({behavior:"wait"});
  await page.route("**/ui/assets/**", route => route.continue({url:route.request().url().replace("/ui/assets/", "/web/")}));
  let mode = "failure", saves = [], release;
  const config = {settings:{enabled:true,groups:["review@chatroom"],auto_download:false,notify_group:false,send_results:false,parser_provider:"builtin",parser_api_url:""},supported_parsers:["parse_video","builtin"],output_dir:"mock",max_file_mb:100};
  await page.route("**/api/douyin/config", async route => {
    if (route.request().method() === "POST") { saves.push(route.request().postDataJSON()); await route.fulfill({json:{saved:true}}); return; }
    if (mode === "failure") { await route.fulfill({status:503,json:{error:{message:"模拟配置读取失败"}}}); return; }
    if (mode === "pending") await new Promise(resolve => release = resolve);
    await route.fulfill({json:mode === "mismatch" ? {...config,supported_parsers:[]} : config});
  });
  await page.route("**/api/douyin/jobs", route => route.fulfill({json:{revision:"empty",jobs:[],pending:0}}));
  await page.route("**/ui/messages", route => route.fulfill({json:{instance:"mock",total:1,messages:[{wxid:"review@chatroom",sender:"member",roomid:"",type:1,msgid:"90071992547409931234",content:"<img src=x onerror=alert(1)>",received_at:"2026-10-20T12:00:00+08:00"}]}}));
  await page.goto("http://127.0.0.1:8137/web/douyin.html");
  await page.getByText("读取配置失败：模拟配置读取失败",{exact:true}).waitFor();
  assert(await page.locator("#save-settings").isDisabled(), "Failed configuration load still permits overwriting saved settings");
  await page.locator("#group-form").evaluate(form => form.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
  assert(saves.length === 0, "Failed load submitted default settings");
  mode = "pending";
  await page.goto("http://127.0.0.1:8137/web/douyin.html");
  assert(await page.locator("#save-settings").isDisabled(), "Pending load permits saving defaults");
  await page.waitForFunction(() => document.getElementById("config-status"));
  // Ensure the intercepted request has entered its wait before releasing it.
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert(typeof release === "function", "Configuration request did not start");
  mode = "success"; release();
  await page.waitForFunction(() => !document.getElementById("save-settings").disabled);
  assert(await page.locator("#groups").inputValue() === "review@chatroom", "Loaded settings were not restored");
  await page.locator("#save-settings").click();
  await page.getByText("默认名单已保存，共 1 个群；独立配置以群管理为准。",{exact:true}).waitFor();
  assert(saves.length === 1 && saves[0].auto_download === false && saves[0].parser_provider === "builtin", "Saving lost loaded values");
  mode = "mismatch";
  await page.evaluate(() => loadConfig());
  assert(await page.locator("#save-settings").isDisabled(), "Incompatible backend permits saving");
  await page.locator("#group-form").evaluate(form => form.dispatchEvent(new Event("submit",{bubbles:true,cancelable:true})));
  assert(saves.length === 1, "Incompatible backend received a save");
  await page.route("**/health", route => route.fulfill({json:{hook_base_url:"mock"}}));
  await page.route("**/QueryDB/status", route => route.fulfill({json:{IsLogin:0}}));
  await page.goto("http://127.0.0.1:8137/web/index.html");
  await page.locator(".message-card").waitFor();
  assert(await page.locator(".message-content img").count() === 0, "Callback text was interpreted as HTML");
  assert((await page.locator(".message-id").textContent()).includes("90071992547409931234"), "Message ID lost precision");
  await page.getByRole("button",{name:"回复到群聊 ↗",exact:true}).click();
  assert(await page.locator("#recipient").inputValue() === "review@chatroom", "A group callback without roomid targets a private member");
  return {configLoadFailure:true,pendingLoad:true,configSave:true,backendMismatch:true,groupReply:true,textEscaping:true,largeMessageId:true};
}
