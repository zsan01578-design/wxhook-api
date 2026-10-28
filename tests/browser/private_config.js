// Run against the isolated tests/browser/mock_server.py service on port 8138.
async page => {
  const results = [];
  const origin = "http://127.0.0.1:8138";
  const deferred = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
  };
  const settle = () => page.evaluate(() => new Promise(done => {
    requestAnimationFrame(() => requestAnimationFrame(done));
  }));
  const submitDirectly = async () => {
    await page.locator("#config-form").dispatchEvent("submit");
    await settle();
  };

  async function scenario(name, options, exercise) {
    const failures = [];
    const check = (condition, message) => { if (!condition) failures.push(message); };
    const initial = deferred();
    const save = deferred();
    const duplicate = deferred();
    const saveEntered = deferred();
    const posts = [];
    const active = new Set();
    let reads = 0;
    let firstSaveCompleted = false;
    let persisted = {
      enabled: true, personality: "已存配置", model_configured: true,
      identity_known: true, queue: 0, sent: 0, last_error: "", contacts: [],
    };
    const handler = route => {
      const task = (async () => {
        const req = route.request();
        const url = new URL(req.url());
        if (url.origin !== origin) return route.abort();
        if (url.pathname === "/api/private/state" && req.method() === "GET") {
          reads++;
          if (reads === 1 && options.failInitial) {
            await initial.promise;
            return route.fulfill({ status: 503, json: { detail: "模拟首次读取失败" } });
          }
          return route.fulfill({ json: persisted });
        }
        if (url.pathname === "/api/private/config" && req.method() === "POST") {
          const body = req.postDataJSON();
          posts.push(body);
          const number = posts.length;
          saveEntered.resolve();
          if (options.slowSave) {
            // Keep an illegal concurrent response from hiding A's overwrite of B.
            if (number > 1 && !firstSaveCompleted) await duplicate.promise;
            await save.promise;
          }
          firstSaveCompleted = true;
          if (options.failSave && number === 1) {
            return route.fulfill({ status: 503, json: { detail: "模拟保存失败" } });
          }
          persisted = { ...persisted, ...body };
          return route.fulfill({ json: { state: persisted } });
        }
        if (req.method() === "GET" && (url.pathname === "/private" || url.pathname.startsWith("/ui/assets/"))) {
          return route.continue();
        }
        failures.push(`出现非预期请求：${req.method()} ${url.pathname}`);
        return route.abort();
      })();
      active.add(task);
      task.then(() => active.delete(task), () => active.delete(task));
      return task;
    };
    await page.route("**/*", handler);
    try {
      await page.goto(`${origin}/private`);
      // Keep polling out of deterministic request-order tests; refresh remains real UI.
      await page.evaluate(() => clearInterval(timer));
      if (!options.failInitial) {
        await page.waitForFunction(() => document.getElementById("personality").value === "已存配置");
      }
      await exercise({ check, posts, initial, save, saveEntered, persisted: () => persisted });
    } catch (error) {
      failures.push(error.message);
    } finally {
      initial.resolve();
      save.resolve();
      duplicate.resolve();
      await Promise.allSettled([...active]);
      try { await page.goto("about:blank"); }
      finally { await page.unroute("**/*", handler); }
    }
    results.push({ name, passed: failures.length === 0, failures });
  }

  // Removing the readiness guard permits input/presets/direct submit to persist default false.
  await scenario("首次读取失败与刷新恢复", { failInitial: true }, async ({ check, posts, initial, persisted }) => {
    const button = page.locator("#save-config");
    check(await button.isDisabled(), "首次读取尚未返回时保存应禁用");
    await page.locator("#personality").fill("读取期间的草稿");
    check(await button.isDisabled(), "读取期间输入不得解锁保存");
    await submitDirectly();
    check(posts.length === 0, "首次读取返回前直接 submit 不得 POST");
    initial.resolve();
    await page.waitForFunction(() => document.getElementById("config-status").textContent.includes("模拟首次读取失败"));
    await page.locator("#personality").fill("读取失败后的草稿");
    check(await button.isDisabled(), "读取失败后输入不得解锁保存");
    await page.locator("[data-persona]").first().click();
    check(await button.isDisabled(), "读取失败后预设不得解锁保存");
    await page.locator("#enabled").check();
    check(await button.isDisabled(), "读取失败后开关不得解锁保存");
    await page.locator("#enabled").uncheck();
    await submitDirectly();
    check(posts.length === 0, "读取失败后直接 submit 不得 POST");
    check(persisted().enabled === true, "未读取配置不应把已启用配置覆盖为 false");

    await page.locator("#refresh").click();
    await page.waitForFunction(() => document.getElementById("contacts-status").textContent.includes("已刷新"));
    check(await page.locator("#enabled").isChecked(), "刷新应恢复服务器已启用配置");
    check(await page.locator("#personality").inputValue() === "已存配置", "刷新应首次初始化表单");
    check(await button.isEnabled(), "刷新读取成功后保存应恢复");
    check(!await page.locator("#config-status").evaluate(el => el.classList.contains("error")), "恢复成功不得残留首次读取错误");
    await page.locator("#personality").fill("恢复后的草稿");
    await button.click();
    await page.waitForFunction(() => document.getElementById("config-status").textContent.includes("已保存"));
    check(posts.at(-1)?.enabled === true && posts.at(-1)?.personality === "恢复后的草稿", "恢复后保存必须基于已读取配置");
  });

  // Removing saving/edit-version protection permits duplicate POST or overwrites newer edits.
  const edits = [
    { name: "输入", apply: () => page.locator("#personality").fill("草稿B"), value: "草稿B", enabled: true },
    { name: "预设", apply: () => page.locator("[data-persona]").first().click(), value: "你说话温和、耐心，先理解对方的感受，再给出简短真诚的建议。", enabled: true },
    { name: "开关", apply: () => page.locator("#enabled").uncheck(), value: "性格A", enabled: false },
  ];
  for (const edit of edits) {
    await scenario(`慢保存期间${edit.name}保留草稿且禁止并发`, { slowSave: true }, async ({ check, posts, save, saveEntered }) => {
      await page.locator("#personality").fill("性格A");
      await page.locator("#save-config").click();
      await saveEntered.promise;
      check(await page.locator("#save-config").isDisabled(), "保存中按钮应禁用");
      await edit.apply();
      check(await page.locator("#save-config").isDisabled(), "保存中编辑不得解锁按钮");
      await submitDirectly();
      await submitDirectly();
      check(posts.length === 1, "保存中重复 submit 不得并发 POST");
      check(posts[0]?.personality === "性格A" && posts[0]?.enabled === true, "在途请求必须使用提交时配置快照");
      const response = page.waitForResponse(item => item.url() === `${origin}/api/private/config` && item.request().method() === "POST");
      save.resolve();
      await (await response).finished();
      await settle();
      check(await page.locator("#personality").inputValue() === edit.value, "旧保存响应不得覆盖新性格草稿");
      check(await page.locator("#enabled").isChecked() === edit.enabled, "旧保存响应不得覆盖新开关草稿");
      check((await page.locator("#config-status").textContent()).includes("未保存"), "旧保存成功必须明确当前新草稿未保存");
      check(await page.locator("#save-config").isEnabled(), "旧保存完成后应允许保存新草稿");
      if (posts.length !== 1) return; // Already failed; release illegal pending requests in finally.
      // A normal refresh after initialization must not erase the pending draft.
      await page.locator("#refresh").click();
      await page.waitForFunction(() => document.getElementById("contacts-status").textContent.includes("已刷新"));
      check(await page.locator("#personality").inputValue() === edit.value, "初始化后的刷新不得覆盖草稿");
      const before = posts.length;
      await page.locator("#save-config").click();
      await page.waitForFunction(() => document.getElementById("config-status").textContent.includes("下一条私聊立即生效"));
      check(posts.length === before + 1, "新草稿应能够随后保存");
      check(posts.at(-1)?.personality === edit.value && posts.at(-1)?.enabled === edit.enabled, "随后保存应发送新草稿而非旧响应");
    });
  }

  // A rejection must release saving without discarding the draft or making it look saved.
  await scenario("保存失败保留新草稿并可重试", { failSave: true, slowSave: true }, async ({ check, posts, save, saveEntered }) => {
    await page.locator("#personality").fill("性格A");
    await page.locator("#save-config").click();
    await saveEntered.promise;
    await page.locator("#personality").fill("失败时的新草稿");
    save.resolve();
    await page.waitForFunction(() => document.getElementById("config-status").textContent.includes("模拟保存失败"));
    check(await page.locator("#personality").inputValue() === "失败时的新草稿", "失败应保留新草稿");
    check(await page.locator("#save-config").isEnabled(), "失败后应恢复保存按钮");
    await page.locator("#save-config").click();
    await page.waitForFunction(() => document.getElementById("config-status").textContent.includes("下一条私聊立即生效"));
    check(posts.length === 2 && posts[1].personality === "失败时的新草稿", "失败后应能重试当前草稿");
    check(!await page.locator("#config-status").evaluate(el => el.classList.contains("error")), "重试成功后应解除错误提示");
  });

  if (results.some(result => !result.passed)) throw new Error(JSON.stringify(results, null, 2));
  return { passed: results.length, results };
}
