// Serve src/wxhook_api on 127.0.0.1:8137 and run with playwright-cli run-code.
// All API and Hook calls are mocked.
async page => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const group = {id:"review@chatroom",name:"模拟群",active:true,enabled:true,chat_enabled:true,douyin_enabled:false,blocked:false,member_count:1,message_count:200,history_complete:true};
  let revision=0, extractCalls=0, failSave=false, failExtract=false;
  const tasks=[];
  await page.route("**/ui/assets/**", route=>route.continue({url:route.request().url().replace("/ui/assets/","/web/")}));
  await page.route("**/api/**",async route=>{
    const url=new URL(route.request().url());
    let data={};
    if(url.pathname.endsWith("/state")) data={groups:[{...group,message_count:200+revision}],blocked_members:[],chatbot:{enabled:true,recent_events:[]}};
    else if(url.pathname.endsWith("/members")) data={members:[{id:"member",name:"成员"}]};
    else if(url.pathname.endsWith("/messages")) data={messages:[],total:0,has_more:false,page:1};
    else if(url.pathname.endsWith("/word-cloud")) data={group_id:group.id,words:[],sampled_messages:0};
    else if(url.pathname.endsWith("/summary")) data={summary:null};
    else if(url.pathname.endsWith("/radar")) data={group_id:group.id,error:"模拟测试"};
    else if(url.pathname.endsWith("/tasks/extract")) {
      extractCalls++;
      if(failExtract) { await route.fulfill({status:409,json:{error:{message:"模拟提取失败"}}}); return; }
      data={candidates:[1,2,3].map(i=>({title:`候选事项${i}`,owner:"",due_date:"",source_id:i,source_content:`原始记录${i}`}))};
    } else if(url.pathname.endsWith("/tasks") && route.request().method()==="POST") {
      if(failSave) { await route.fulfill({status:409,json:{error:{message:"模拟保存失败"}}}); return; }
      const body=route.request().postDataJSON();
      tasks.push({...body,id:`saved${tasks.length}`,status:"open",due_at:null,created_at:"2026-10-20T10:00:00+08:00",source_kind:"ai"});
      data={saved:true,id:tasks.at(-1).id};
    } else if(url.pathname.endsWith("/tools")) data={group_id:group.id,tasks,rules:[],hits:[],reports:[]};
    else if(url.pathname.endsWith("/config")) data={settings:{groups:[],enabled:false},supported_parsers:["parse_video"]};
    else if(url.pathname.endsWith("/jobs")) data={revision:"initial",jobs:[],pending:0};
    await route.fulfill({json:data});
  });
  await page.route("**/GetSelfProfile",route=>route.fulfill({json:{wxid:"robot"}}));
  await page.route("**/QueryDB/status",route=>route.fulfill({json:{IsLogin:1}}));
  await page.route("**/ui/messages",route=>route.fulfill({json:{messages:[]}}));
  await page.setViewportSize({width:1440,height:900});
  await page.goto("http://127.0.0.1:8137/web/manage.html");
  await page.locator("#chat-log").waitFor();
  await page.evaluate(()=>clearTimeout(pollTimer));
  await page.locator('[data-tab="members"]').click();
  const search=page.getByRole("searchbox",{name:"搜索群成员"});
  await search.fill("成");
  await search.evaluate(node=>{window.originalSearch=node;node.dispatchEvent(new CompositionEvent("compositionstart"));});
  revision++;
  await page.evaluate(()=>refreshState(true));
  assert(await search.evaluate(node=>node===window.originalSearch && document.activeElement===node),"Member search was recreated or lost focus");
  await search.press("End");
  await page.keyboard.insertText("员");
  assert(await search.inputValue()==="成员","Could not continue typing after refresh");
  await search.evaluate(node=>node.dispatchEvent(new CompositionEvent("compositionend")));
  await page.locator('[data-tab="tools"]').click();
  await page.getByRole("button",{name:"AI 提取待办",exact:true}).click();
  const candidates=()=>page.getByRole("button",{name:"核对并保存",exact:true});
  await candidates().first().waitFor();
  assert(await candidates().count()===3,"Missing extraction candidates");
  await candidates().first().click();
  await page.getByRole("button",{name:"保存待办",exact:true}).click();
  await page.waitForFunction(()=>document.getElementById("modal-title").textContent==="待办候选项" && document.querySelectorAll("#modal .tool-row").length===2);
  assert(tasks.length===1 && extractCalls===1,"Saving one candidate repeated extraction");
  await page.locator("#close-modal").click();
  await page.getByRole("button",{name:"继续核对待办",exact:true}).click();
  assert(await candidates().count()===2 && extractCalls===1,"Closing lost the remaining batch");
  failExtract=true;
  await page.getByRole("button",{name:"重新提取",exact:true}).click();
  await page.getByText("模拟提取失败",{exact:true}).waitFor();
  assert(await candidates().count()===2,"Failed re-extraction discarded the batch");
  await candidates().first().click();
  await page.getByRole("button",{name:"返回候选列表",exact:true}).click();
  assert(await candidates().count()===2,"Returning from editor lost candidates");
  await candidates().first().click();
  failSave=true;
  await page.getByRole("button",{name:"保存待办",exact:true}).click();
  await page.getByText("模拟保存失败",{exact:true}).waitFor();
  assert(tasks.length===1,"Failed save committed a candidate");
  failSave=false;
  await page.getByRole("button",{name:"保存待办",exact:true}).click();
  await page.waitForFunction(()=>document.getElementById("modal-title").textContent==="待办候选项" && document.querySelectorAll("#modal .tool-row").length===1);
  await candidates().first().click();
  await page.getByRole("button",{name:"保存待办",exact:true}).click();
  await page.getByText("本批候选已全部保存。",{exact:true}).waitFor();
  assert(tasks.length===3 && new Set(tasks.map(task=>task.title)).size===3,"Not all candidates were saved once");
  await page.goto("http://127.0.0.1:8137/web/douyin.html");
  await page.waitForFunction(()=>typeof renderJobs==="function");
  const video=await page.evaluate(()=>{
    clearTimeout(timer);
    const job={id:"a".repeat(32),group_id:"review@chatroom",status:"completed",title:"模拟视频",source_url:"https://example.com",files:[{kind:"video",name:"video.mp4",size:1000,send_status:"sending"}]};
    renderJobs({revision:"first",jobs:[job]});
    const old=document.querySelector("video"), details=old.closest("details");
    details.open=true; old.currentTime=15; old.volume=.4; old.playbackRate=1.5;
    renderJobs({revision:"new-task",jobs:[{...job,id:"b".repeat(32),files:[]},job]});
    renderJobs({revision:"same-task-update",jobs:[{...job,delivery_status:"sent",files:[{...job.files[0],send_status:"sent"},{kind:"image",name:"added.png",size:200}]}]});
    const same=document.querySelector("video")===old && old.isConnected && old.currentTime===15 && old.volume===.4 && old.playbackRate===1.5 && details.open;
    const updated=document.querySelector(".file-send-status").textContent==="Hook 已确认发送";
    renderJobs({revision:"cleanup",jobs:[{...job,files_cleaned_at:"2026-10-20",files:job.files}]});
    const cleaned=!document.querySelector("video") && !old.isConnected;
    return {same,updated,cleaned};
  });
  assert(video.same && video.updated && video.cleaned,`Video state regression: ${JSON.stringify(video)}`);
  return {memberSearch:true,candidateBatch:true,failedSave:true,failedExtraction:true,videoContinuity:true,mediaStatus:true,mediaCleanup:true};
}
