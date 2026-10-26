// Run against tests/browser/mock_server.py. All data is isolated; no real Hook.
async page => {
  const assert = (condition,message) => { if (!condition) throw new Error(message); };
  const group = "favorites-ui@chatroom", base = `http://127.0.0.1:8138/api/manage/groups/${group}/favorites`;
  const errors = [];
  page.on("pageerror",error => errors.push(error.message));
  await page.request.post("http://127.0.0.1:8138/api/manage/groups",{data:{id:group,name:"收藏测试群"}});
  const existing = await (await page.request.get(base)).json();
  for (const item of existing.items || []) await page.request.delete(`${base}/${item.id}`);
  const results = [];
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    await page.goto("http://127.0.0.1:8138/manage");
    await page.getByRole("button").filter({hasText:"收藏测试群"}).first().click();
    await page.locator('[data-tab="favorites"]').click();
    await page.getByRole("button",{name:"添加收藏",exact:true}).waitFor();
    assert(await page.getByText("把喜欢的分享收在这里").isVisible(),"Missing empty state");
    await page.getByRole("button",{name:"添加收藏",exact:true}).click();
    await page.getByLabel("分享链接或分享文案",{exact:true}).fill(`https://example.com/recipe-${width}`);
    await page.getByLabel("收藏标题",{exact:true}).fill("红烧肉家常菜教程 <script>测试</script>");
    await page.getByLabel("标签（用逗号分隔）",{exact:true}).fill("美食,聚餐");
    await page.getByLabel("收藏备注",{exact:true}).fill("周末和朋友一起做");
    await page.getByRole("button",{name:"加入收藏",exact:true}).click();
    await page.locator(".favorite-card").waitFor();
    assert(await page.locator(".favorite-card h4").textContent() === "红烧肉家常菜教程 <script>测试</script>","Title not preserved as text");
    await page.getByLabel("搜索群收藏",{exact:true}).fill("上次那个红烧肉教程在哪");
    await page.getByRole("button",{name:"搜索",exact:true}).click();
    await page.waitForFunction(() => document.querySelector(".favorite-result-meta")?.textContent.includes("1 条收藏"));
    await page.locator(".favorite-card").getByRole("button",{name:"编辑",exact:true}).click();
    await page.getByLabel("收藏备注",{exact:true}).fill("周末必做，准备五花肉");
    await page.getByRole("button",{name:"保存修改",exact:true}).click();
    await page.getByText("周末必做，准备五花肉",{exact:true}).waitFor();
    await page.locator(".favorite-card").getByRole("button",{name:"查看",exact:true}).click();
    await page.getByText("此收藏没有本地媒体，请通过原始分享链接查看。",{exact:true}).waitFor();
    await page.locator("#close-modal").click();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1),`Page overflow at ${width}px`);
    await page.screenshot({path:`D:/wxhook/.tools/favorites-${width}.png`,fullPage:true});
    // Polling must not overwrite an unsubmitted search draft.
    await page.getByLabel("搜索群收藏",{exact:true}).fill("未提交的搜索内容");
    await page.evaluate(() => { detailVersion = ""; return loadDetail(); });
    assert(await page.getByLabel("搜索群收藏",{exact:true}).inputValue() === "未提交的搜索内容","Polling overwrote draft");
    await page.locator(".favorite-card").getByRole("button",{name:"删除",exact:true}).click();
    await page.locator("#modal").getByRole("button",{name:"删除收藏",exact:true}).click();
    await page.waitForFunction(() => document.querySelectorAll(".favorite-card").length === 0 && !document.getElementById("modal").open);
    results.push({width,add:true,search:true,edit:true,detail:true,delete:true,draftPreserved:true,overflow:false});
  }
  assert(!errors.length,JSON.stringify(errors));
  return {viewports:results,uncaughtErrors:errors};
}
