// Serve src/wxhook_api on 127.0.0.1:8137, then run this file using
// playwright-cli run-code --filename=tests/browser/manage_refresh.js.
// All API and Hook calls use fixtures; no live WeChat service is needed.
async page => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const group = {id:"review@chatroom",name:"审查模拟群",active:true,enabled:true,chat_enabled:true,douyin_enabled:false,blocked:false,member_count:1,message_count:100,history_complete:true};
  let revision = 0, reportRevision = 0;
  const messageRows = (pageNumber) => Array.from({length:50},(_,i) => {
    const id = 200 + revision - i - (pageNumber - 1) * 50;
    return {id,msgid:String(id),sender:"member",type:1,timestamp:1792400000+id,content:`消息 ${id}` + (id % 7 === 0 ? "\n较长消息\n第三行内容" : "")};
  });
  await page.route("**/ui/assets/**", route => route.continue({url: route.request().url().replace("/ui/assets/", "/web/")}));
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url());
    let data = {};
    if (url.pathname.endsWith("/state")) data = {groups:[{...group,message_count:200+revision}],blocked_members:[],chatbot:{enabled:true,recent_events:[]}};
    else if (url.pathname.endsWith("/members")) data = {members:[{id:"member",name:"成员"}]};
    else if (url.pathname.endsWith("/messages")) {
      const pageNumber = Number(url.searchParams.get("page") || 1);
      data = {messages:messageRows(pageNumber),total:200+revision,has_more:true,page:pageNumber};
    }
    else if (url.pathname.endsWith("/word-cloud")) data = {group_id:group.id,words:[],sampled_messages:0};
    else if (url.pathname.endsWith("/summary")) data = {summary:null};
    else if (url.pathname.endsWith("/radar")) data = {group_id:group.id,error:"模拟测试"};
    else if (url.pathname.endsWith("/tools")) data = {group_id:group.id,tasks:[],rules:[],hits:[],reports:[{kind:"week",start_date:"2026-10-12",end_date:"2026-10-18",content:`测试周报 ${reportRevision}\n` + "报表正文\n".repeat(30),message_count:1,sampled_count:1,updated_at:"2026-10-20"}]};
    await route.fulfill({json:data});
  });
  await page.route("**/GetSelfProfile", route => route.fulfill({json:{wxid:"robot"}}));
  await page.route("**/QueryDB/status", route => route.fulfill({json:{IsLogin:1}}));
  await page.setViewportSize({width:1440,height:900});
  await page.goto("http://127.0.0.1:8137/web/manage.html");
  await page.locator(".chat-bubble").first().waitFor();
  await page.evaluate(() => clearTimeout(pollTimer));
  const refresh = async () => {
    await page.evaluate(() => refreshState(true));
    await page.evaluate(() => new Promise(requestAnimationFrame));
  };
  const visible = () => page.evaluate(() => {
    const log = document.getElementById("chat-log"), bounds = log.getBoundingClientRect();
    const node = [...log.querySelectorAll("[data-message-id]")].find(n=>n.getBoundingClientRect().bottom>bounds.top);
    return {id:node?.dataset.messageId,top:node?.getBoundingClientRect().top-bounds.top};
  });
  const sameAnchor = (before, after) => assert(before.id === after.id && Math.abs(before.top-after.top) < 1, `Reading position moved: ${JSON.stringify({before,after})}`);
  await page.locator("#chat-log").evaluate(node=>node.scrollTop=1200);
  const before = await visible();
  revision += 5;
  await refresh();
  sameAnchor(before, await visible());
  revision += 100;
  await refresh();
  sameAnchor(before, await visible());
  assert(await page.locator("#chat-jump").isVisible(), "Missing navigation after the anchor left the page");
  await page.locator("#chat-log").evaluate(node => { node.scrollTop = node.scrollHeight; node.dispatchEvent(new Event("scroll")); });
  await refresh();
  assert(await page.locator("#chat-jump").isVisible(), "Stale page hid the latest-message navigation at its bottom");
  await page.locator("#chat-jump").click();
  await page.waitForFunction(() => document.querySelector(".chat-bubble:last-child") && document.getElementById("chat-count").textContent.includes("305"));
  await page.locator(".chat-bubble").filter({hasText:/^消息 305$/}).waitFor();
  assert(await page.locator("#chat-jump").isHidden(), "Latest navigation did not reset reading state");
  revision++;
  await refresh();
  assert(await page.locator(".chat-bubble").last().textContent() === "消息 306", "Bottom did not follow latest messages");
  await page.getByRole("button",{name:"下一页",exact:true}).click();
  await page.getByText("第 2 页",{exact:true}).waitFor();
  const older = await visible();
  revision += 3;
  await refresh();
  sameAnchor(older, await visible());
  await page.locator('[data-tab="tools"]').click();
  await page.locator(".tool-report summary").click();
  await page.locator("#tab-content").evaluate(node=>node.scrollTop=100);
  const toolsScroll = await page.locator("#tab-content").evaluate(node=>node.scrollTop);
  revision++;
  reportRevision++;
  await refresh();
  assert(await page.locator(".tool-report").evaluate(n=>n.open), "Report collapsed during refresh");
  assert((await page.locator(".tool-report-content").textContent()).includes("测试周报 1"), "Report contents stopped updating");
  assert(await page.locator("#tab-content").evaluate(node=>node.scrollTop) === toolsScroll, "Tools scroll position changed");
  await page.locator(".tool-report summary").click();
  revision++;
  await refresh();
  assert(!await page.locator(".tool-report").evaluate(n=>n.open), "User-closed report reopened");
  return {readingAnchor:true,largeBurst:true,latestNavigation:true,bottomFollowing:true,olderPage:true,reportExpansion:true,reportUpdates:true,toolsScroll:true};
}
