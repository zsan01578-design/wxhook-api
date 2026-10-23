// Run against the same isolated static server as manage_refresh.js (port 8137).
async page => {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const groups = ["first", "second"].map(name => ({id:`${name}@chatroom`,name,active:true,enabled:true,chat_enabled:true,member_count:1,message_count:1}));
  const requests = {cloud:0,radar:0,messages:0};
  let failRadar = false, holdRadar = false, releaseRadar, radarStarted;
  await page.route("**/ui/assets/**", route => route.continue({url:route.request().url().replace("/ui/assets/", "/web/")}));
  await page.route("**/api/**", async route => {
    const url = new URL(route.request().url()), group = decodeURIComponent(url.pathname.split("/")[4] || "");
    let data = {};
    if (url.pathname.endsWith("/state")) data = {groups,blocked_members:[],chatbot:{enabled:true}};
    else if (url.pathname.endsWith("/members")) data = {members:[]};
    else if (url.pathname.endsWith("/messages")) {
      requests.messages++;
      data = {messages:[],total:requests.messages,page:1,has_more:false};
    } else if (url.pathname.endsWith("/word-cloud")) {
      requests.cloud++;
      data = {group_id:group,words:[{text:group,count:1}],sampled_messages:1};
    } else if (url.pathname.endsWith("/radar")) {
      requests.radar++;
      if (holdRadar) {
        holdRadar = false;
        await new Promise(resolve => { releaseRadar = resolve; radarStarted(); });
      }
      if (failRadar) { await route.fulfill({status:503,json:{detail:"temporary failure"}}); return; }
      data = {group_id:group,days:Number(url.searchParams.get("days")),messages:1,active_members:1,daily:[],hourly:[],top_members:[],words:[]};
    } else if (url.pathname.endsWith("/summary")) data = {summary:null};
    await route.fulfill({json:data});
  });
  await page.route("**/GetSelfProfile", route => route.fulfill({json:{wxid:"robot"}}));
  await page.route("**/QueryDB/status", route => route.fulfill({json:{IsLogin:1}}));
  await page.goto("http://127.0.0.1:8137/web/manage.html");
  await page.waitForFunction(() => chatRadar?.group_id === "first@chatroom" && ownWxid === "robot");
  await page.evaluate(async () => {
    clearTimeout(pollTimer);
    await refreshState(true);
    window.testElapsed = 0;
    const initial = performance.now();
    performance.now = () => initial + window.testElapsed;
  });
  const baseline = {...requests};
  await page.evaluate(async () => { for (let i=0; i<9; i++) { window.testElapsed += 3000; await refreshState(); } });
  assert(requests.cloud === baseline.cloud && requests.radar === baseline.radar, "Unchanged statistics were fetched on every message poll");
  assert(requests.messages === baseline.messages + 9, "Message polling stopped with statistics");
  await page.evaluate(async () => { window.testElapsed += 3001; await refreshState(); });
  assert(requests.radar === baseline.radar + 1 && requests.cloud === baseline.cloud + 1, "Statistics did not refresh after 30 seconds");
  await page.evaluate(() => refreshState(true));
  assert(requests.radar === baseline.radar + 2, "Manual refresh did not bypass statistics cache");
  await page.evaluate(async () => { selectedId = "second@chatroom"; await loadDetail(); });
  assert(await page.evaluate(() => chatRadar.group_id === selectedId && wordCloud.group_id === selectedId), "Switching groups reused another group's statistics");
  const switched = requests.radar;
  await page.evaluate(async () => { radarDays = 7; await loadDetail(); });
  assert(requests.radar === switched + 1 && await page.evaluate(() => chatRadar.days === 7), "Changing the date window reused old statistics");
  await page.evaluate(async () => { ownWxid = "new_robot"; await loadDetail(); });
  assert(requests.radar === switched + 2, "Changing the bot identity reused old statistics");
  // Concurrent tab refreshes share expensive requests, while detailToken still
  // prevents a slow response for the previous group from replacing the new one.
  holdRadar = true;
  const started = new Promise(resolve => { radarStarted = resolve; });
  await page.evaluate(() => { window.testElapsed += 31000; window.slowDetails = Promise.all([loadDetail(), loadDetail()]); });
  await started;
  assert(requests.radar === switched + 3, "Concurrent refreshes duplicated statistics requests");
  await page.evaluate(async () => { selectedId = "first@chatroom"; await loadDetail(); });
  releaseRadar();
  await page.evaluate(() => window.slowDetails);
  assert(await page.evaluate(() => chatRadar.group_id === "first@chatroom"), "Old statistics replaced the current group");
  const settled = requests.radar;
  await page.evaluate(() => loadDetail());
  assert(requests.radar === settled, "Old response evicted the current group's cache");
  failRadar = true;
  await page.evaluate(() => refreshState(true));
  assert(await page.evaluate(() => Boolean(chatRadar.error)), "Statistics failure was not shown");
  failRadar = false;
  await page.evaluate(() => refreshState());
  assert(await page.evaluate(() => !chatRadar.error), "Failed statistics were cached instead of retried");
  assert(errors.length === 0, errors.join("\n"));
  return {nineMessagePolls:9,extraStatisticsRequestsWithin27Seconds:0,expiry:true,manualRefresh:true,groupSwitch:true,dateWindow:true,botIdentity:true,concurrentRefresh:true,staleResponse:true,errorRetry:true};
}
