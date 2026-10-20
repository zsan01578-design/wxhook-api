// Start tests/browser/mock_server.py first; no real Hook or model is used.
async page => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  await page.unrouteAll({behavior:"wait"});
  const errors = [];
  const onError = error => errors.push(error.message);
  page.on("pageerror", onError);
  const group = {id:"review@chatroom",name:"项目检查群",local_name:"项目检查群",enabled:true,active:true,chat_enabled:true,douyin_enabled:true,auto_download:true,blocked:false,tools_enabled:true,member_count:1,current_member_count:1,blocked_member_count:0,message_count:1,members_sync:"complete",history_complete:true,last_seen:"2026-10-20T12:00:00+08:00"};
  const radar = {group_id:group.id,days:14,from:"2026-10-07",to:"2026-10-20",messages:1,text_messages:1,active_members:1,daily:[{date:"2026-10-20",count:1,text_count:1,active_members:1}],hourly:Array.from({length:24},(_,hour)=>({hour,count:hour===12?1:0})),top_members:[{id:"member",name:"模拟成员",count:1}],words:[],peak_hour:{hour:12,count:1},peak_day:{date:"2026-10-20",count:1}};
  let empty = false;
  await page.route("**/api/manage/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let data = {};
    if (path.endsWith("/state")) data = {groups:empty?[]:[group],blocked_members:[],sync:{enabled:false,phase:"idle",message:"模拟数据"},chatbot:{enabled:true,mention_names:["机器人"],recent_events:[]}};
    else if (path.endsWith("/members")) data = {members:[{id:"member",name:"模拟成员",message_count:1,blocked:false}]};
    else if (path.endsWith("/messages")) data = {messages:[{id:1,msgid:"90071992547409931234",type:1,sender:"member",content:"模拟聊天内容",timestamp:1792468800}],total:1,page:1,has_more:false};
    else if (path.endsWith("/word-cloud")) data = {group_id:group.id,words:[{text:"检查",count:3}],sampled_messages:1};
    else if (path.endsWith("/summary")) data = {summary:{summary:"模拟今日总结",message_count:1,updated_at:"2026-10-20T12:00:00+08:00"}};
    else if (path.endsWith("/radar")) data = radar;
    else if (path.endsWith("/tools")) data = {group_id:group.id,tasks:[],rules:[],hits:[],reports:[],auto_reports:false,task_reminders:false};
    else if (path.endsWith("/audit")) data = {events:[]};
    else if (path.endsWith("/chat-models")) data = {models:[],search:{enabled:false},configured_mention_names:[]};
    await route.fulfill({json:data});
  });
  const widths = [1440,390];
  const results = [];
  for (const width of widths) {
    await page.setViewportSize({width,height:900});
    await page.goto("http://127.0.0.1:8138/manage");
    await page.locator(".group-item").first().click();
    await page.locator("#chat-log").waitFor();
    for (const name of ["features","members","messages","radar","tools","audit"]) {
      await page.locator(`[data-tab="${name}"]`).click();
      await page.evaluate(() => loadDetail());
      assert(await page.locator("#tab-content").textContent(), `Empty ${name} tab at ${width}px`);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Horizontal page overflow in ${name} at ${width}px`);
    }
    await page.locator('[data-tab="tools"]').click();
    await page.evaluate(() => loadDetail());
    await page.getByRole("button",{name:"新增",exact:true}).click();
    assert(await page.getByLabel("待办事项",{exact:true}).isVisible(), `Task dialog unavailable at ${width}px`);
    await page.locator("#close-modal").click();
    await page.locator('[data-tab="messages"]').click();
    await page.evaluate(() => loadDetail());
    await page.screenshot({path:`D:/wxhook/.tools/project-audit/manage-${width}.png`,fullPage:true});
    await page.goto("http://127.0.0.1:8138/douyin");
    await page.waitForFunction(() => !document.getElementById("save-settings").disabled);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Resource page overflow at ${width}px`);
    await page.screenshot({path:`D:/wxhook/.tools/project-audit/douyin-${width}.png`,fullPage:true});
    await page.goto("http://127.0.0.1:8138/messages");
    await page.locator("#send-form").waitFor();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Message page overflow at ${width}px`);
    await page.screenshot({path:`D:/wxhook/.tools/project-audit/messages-${width}.png`,fullPage:true});
    results.push({width,tabs:6,taskDialog:true,pages:3,overflow:false});
  }
  empty = true;
  await page.goto("http://127.0.0.1:8138/manage");
  await page.waitForFunction(() => !document.getElementById("welcome").hidden);
  page.off("pageerror", onError);
  assert(errors.length === 0, `Uncaught JavaScript errors: ${JSON.stringify(errors)}`);
  return {viewports:results,emptyState:true,uncaughtErrors:errors};
}
