// Run against tests/browser/mock_server.py; all model APIs are mocked.
async page => {
  await page.unrouteAll({behavior:"wait"});
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const errors = [], saves = [], profiles = [];
  let activeId = "", failSave = false, failLoad = false;
  page.on("pageerror", error => errors.push(error.message));
  const preview = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZYkAAAAASUVORK5CYII=";
  const snapshot = () => ({configured:!!activeId,active_id:activeId,models:profiles.map(profile => ({...profile,active:profile.id===activeId})),load_error:""});
  await page.route("**/api/manage/**", async route => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
    let data = {};
    if (path.endsWith("/state")) data = {groups:[],blocked_members:[],sync:{enabled:false,phase:"idle",message:"模拟数据"},chatbot:{enabled:true,mention_names:["机器人"],recent_events:[]}};
    else if (path.endsWith("/chat-model")) data = {models:[],configured:false};
    else if (path.endsWith("/chat-search")) data = {enabled:false,used_today:0,daily_limit:1000};
    else if (path.endsWith("/image-model")) {
      if (failLoad) { await route.fulfill({status:503,json:{error:{message:"模拟生图配置读取失败"}}}); return; }
      data = snapshot();
    } else if (path.includes("/image-models")) {
      const id = path.split("/")[4];
      if (path.endsWith("/activate")) activeId = id;
      else if (method === "DELETE") profiles.splice(profiles.findIndex(profile => profile.id === id), 1);
      else {
        const payload = request.postDataJSON(); saves.push({method,path,payload});
        if (failSave) { await route.fulfill({json:{saved:false,test:{ok:false,message:"模拟服务拒绝生图参数"}}}); return; }
        const {api_key,...settings} = payload;
        const profile = {...settings,id:id || String(profiles.length+1),has_api_key:!!api_key || profiles.find(item => item.id===id)?.has_api_key};
        const existing = profiles.findIndex(item => item.id === id);
        if (existing >= 0) profiles[existing] = profile; else profiles.push(profile);
        activeId ||= profile.id;
        data = {saved:true,profile:{...profile,active:activeId===profile.id},settings:snapshot(),test:{ok:true,message:"生图测试通过，配置已保存。",preview}};
        await route.fulfill({json:data}); return;
      }
      data = {saved:true,settings:snapshot()};
    }
    await route.fulfill({json:data});
  });
  await page.setViewportSize({width:1440,height:1000});
  await page.goto("http://127.0.0.1:8138/manage");
  await page.getByRole("button",{name:"AI 模型配置",exact:true}).click();
  await page.getByRole("button",{name:"生图模型",exact:true}).click();
  await page.getByRole("button",{name:"添加生图模型",exact:true}).click();
  assert(await page.getByLabel("深度思考",{exact:true}).count() === 0, "Chat-only setting shown in image editor");
  await page.getByLabel("配置名称",{exact:true}).fill("主生图模型");
  await page.getByLabel("Base URL",{exact:true}).fill("https://image.example/v1");
  await page.getByLabel("模型名称",{exact:true}).fill("example/image");
  await page.getByLabel("API Key",{exact:true}).fill("ui-dummy-key");
  await page.getByLabel("图片尺寸",{exact:true}).fill("1024x1536");
  await page.getByLabel("图片返回格式",{exact:true}).selectOption("b64_json");
  await page.getByRole("button",{name:"保存并测试",exact:true}).click();
  await page.locator(".model-test-result.passed").waitFor();
  await page.waitForFunction(() => !modelSaving);
  assert(saves[0].payload.response_format === "b64_json" && saves[0].payload.size === "1024x1536" && !("enable_thinking" in saves[0].payload), "Wrong image request fields");
  assert(await page.getByLabel("API Key",{exact:true}).inputValue() === "", "Saved key was echoed");
  assert(await page.getByAltText("生图连接测试预览").isVisible(), "Test image preview missing");
  await page.waitForFunction(() => document.querySelector(".image-model-preview").naturalWidth > 0);
  await page.screenshot({path:"D:/wxhook/.tools/project-audit/image-model-preview.png",fullPage:true});
  await page.getByRole("button",{name:"返回模型列表",exact:true}).click();
  await page.getByRole("button",{name:"添加生图模型",exact:true}).click();
  await page.getByLabel("配置名称",{exact:true}).fill("备用生图模型");
  await page.getByLabel("Base URL",{exact:true}).fill("https://backup.example/v1");
  await page.getByLabel("模型名称",{exact:true}).fill("backup/image");
  await page.getByLabel("图片尺寸",{exact:true}).fill("");
  await page.getByRole("button",{name:"保存并测试",exact:true}).click();
  await page.locator(".model-test-result.passed").waitFor();
  await page.waitForFunction(() => !modelSaving);
  await page.getByRole("button",{name:"返回模型列表",exact:true}).click();
  const backup = page.locator(".model-card").filter({has:page.getByRole("heading",{name:"备用生图模型",exact:true})});
  await backup.getByRole("button",{name:"查看 / 修改",exact:true}).click();
  assert(await page.getByLabel("图片尺寸",{exact:true}).inputValue() === "", "Empty default size was lost");
  await page.getByLabel("配置名称",{exact:true}).fill("修改后的生图模型");
  failSave = true;
  await page.getByRole("button",{name:"保存并测试",exact:true}).click();
  await page.locator(".model-test-result.failed").waitFor();
  await page.waitForFunction(() => !modelSaving);
  assert(profiles[1].name === "备用生图模型", "Failed test changed saved profile");
  failSave = false;
  await page.getByRole("button",{name:"保存并测试",exact:true}).click();
  await page.locator(".model-test-result.passed").waitFor();
  await page.waitForFunction(() => !modelSaving);
  assert(saves.at(-1).method === "PUT" && saves.at(-1).payload.api_key === null, "Editing did not use existing profile");
  await page.getByRole("button",{name:"返回模型列表",exact:true}).click();
  await page.locator(".model-card").filter({has:page.getByRole("heading",{name:"修改后的生图模型",exact:true})}).getByRole("button",{name:"设为首选",exact:true}).click();
  await page.waitForFunction(() => !modelSaving);
  assert(activeId === "2", "Switch did not activate image profile");
  await page.locator(".model-card").filter({has:page.getByRole("heading",{name:"主生图模型",exact:true})}).getByRole("button",{name:"删除",exact:true}).click();
  await page.getByRole("button",{name:"删除模型",exact:true}).click();
  await page.getByRole("button",{name:"添加生图模型",exact:true}).waitFor();
  await page.waitForFunction(() => !modelSaving);
  assert(profiles.length === 1 && activeId === "2", "Wrong profile deleted");
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1), "Image list overflows mobile viewport");
  await page.screenshot({path:"D:/wxhook/.tools/project-audit/image-model-mobile-list.png",fullPage:true});
  await page.getByRole("button",{name:"查看 / 修改",exact:true}).click();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth+1), "Image editor overflows mobile viewport");
  await page.screenshot({path:"D:/wxhook/.tools/project-audit/image-model-mobile-editor.png",fullPage:true});
  await page.getByRole("button",{name:"返回模型列表",exact:true}).click();
  await page.getByRole("button",{name:"对话模型",exact:true}).click();
  await page.getByRole("button",{name:"添加模型",exact:true}).click();
  assert(await page.getByLabel("深度思考",{exact:true}).isVisible(), "Chat editor lost thinking setting");
  assert(await page.getByLabel("图片尺寸",{exact:true}).count() === 0, "Image setting leaked into chat editor");
  await page.getByRole("button",{name:"返回模型列表",exact:true}).click();
  failLoad = true;
  await page.getByRole("button",{name:"生图模型",exact:true}).click();
  await page.getByText("模拟生图配置读取失败",{exact:true}).waitFor();
  assert(await page.getByRole("button",{name:"添加生图模型",exact:true}).count() === 0, "Failed load permits saving defaults");
  assert(errors.length === 0, errors.join("\n"));
  return {imageProfileCrud:true,preview:true,failedTestPreservesProfile:true,loadFailure:true,separateChatEditor:true,mobileOverflow:false};
}
