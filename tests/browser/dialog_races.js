// Run through playwright-cli against the isolated static server on port 8137.
async page => {
  const failures=[];
  const assert=(condition,message)=>{if(!condition) failures.push(message);};
  const group={id:"review@chatroom",name:"模拟群",enabled:true,member_count:0};
  const settings=name=>({models:[{id:"model",name,model:"mock",base_url:"https://model.invalid/v1",active:true}]});
  let firstModel, modelEntered, saveEntered, releaseSave, modelCalls=0;
  await page.unrouteAll({behavior:"wait"});
  await page.route("**/ui/assets/**",route=>route.continue({url:route.request().url().replace("/ui/assets/","/web/")}));
  await page.route("**/api/**",async route=>{
    const url=new URL(route.request().url());
    let data={};
    if(url.pathname.endsWith("/chat-model")) {
      modelCalls++;
      if(modelCalls===1) {
        await new Promise(resolve=>{firstModel=resolve;modelEntered();});
        await route.fulfill(firstModel.response); return;
      }
      data=settings("最新模型");
    } else if(url.pathname.endsWith("/chat-search")) data={enabled:false,used_today:0,daily_limit:1000};
    else if(route.request().method()==="PATCH") {
      await new Promise(resolve=>{releaseSave=resolve;saveEntered();}); data={saved:true};
    } else if(url.pathname.endsWith("/state")) data={groups:[group],blocked_members:[],chatbot:{configured_mention_names:[],recent_events:[]}};
    else if(url.pathname.endsWith("/members")) data={members:[]};
    else if(url.pathname.endsWith("/messages")) data={messages:[],total:0,has_more:false};
    else if(url.pathname.endsWith("/word-cloud")) data={group_id:group.id,words:[]};
    else if(url.pathname.endsWith("/radar")) data={group_id:group.id,error:"模拟测试"};
    else if(url.pathname.endsWith("/summary")) data={summary:null};
    await route.fulfill({json:data});
  });
  await page.route("**/QueryDB/status",route=>route.fulfill({json:{IsLogin:0}}));
  await page.goto("http://127.0.0.1:8137/web/manage.html");
  await page.locator("#chat-log").waitFor();
  await page.evaluate(()=>clearTimeout(pollTimer));

  const entered=new Promise(resolve=>modelEntered=resolve);
  await page.evaluate(()=>{window.oldDialogRequest=modelSettingsDialog();});
  await entered;
  await page.locator("#close-modal").click();
  await page.evaluate(()=>addGroupDialog());
  await page.getByLabel("群名称（选填）",{exact:true}).fill("未保存的群名");
  firstModel.response={status:503,json:{error:{message:"模拟旧请求失败"}}};firstModel();
  await page.evaluate(()=>window.oldDialogRequest);
  const groupName=page.getByLabel("群名称（选填）",{exact:true});
  assert(await groupName.count()===1,"Old model error erased the new dialog form");
  if(await groupName.count()) assert(await groupName.inputValue()==="未保存的群名","New form input was lost");

  modelCalls=0;
  const reopened=new Promise(resolve=>modelEntered=resolve);
  await page.evaluate(()=>{window.oldDialogRequest=modelSettingsDialog();});
  await reopened;
  await page.locator("#close-modal").click();
  await page.evaluate(()=>modelSettingsDialog());
  await page.getByRole("heading",{name:"最新模型",exact:true}).waitFor();
  firstModel.response={json:settings("过时模型")};firstModel();
  await page.evaluate(()=>window.oldDialogRequest);
  assert(await page.getByRole("heading",{name:"最新模型",exact:true}).count()===1,"Old response overwrote the reopened model dialog");

  await page.evaluate(()=>editGroupDialog(state.groups[0]));
  const saving=new Promise(resolve=>saveEntered=resolve);
  await page.getByRole("button",{name:"保存",exact:true}).click();
  await saving;
  await page.locator("#close-modal").click();
  await page.evaluate(()=>mentionSettingsDialog());
  await page.getByLabel("机器人显示名（每行一个，不含 @）",{exact:true}).fill("尚未保存的新名字");
  releaseSave();
  await page.waitForFunction(()=>!mutating);
  assert(await page.locator("#modal").evaluate(node=>node.open),"Finishing an older save closed the new dialog");
  assert(await page.getByLabel("机器人显示名（每行一个，不含 @）",{exact:true}).inputValue()==="尚未保存的新名字","Older save changed the new form");
  if(failures.length) throw new Error(failures.join("; "));
  return {staleError:true,reopenedModelDialog:true,saveDoesNotCloseNewDialog:true};
}
