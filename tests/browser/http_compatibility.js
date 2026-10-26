// Run against mock_server.py. Simulate browsers without AbortSignal.timeout.
async page => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  await page.unrouteAll({behavior: "wait"});
  await page.addInitScript(() => { Object.defineProperty(AbortSignal, "timeout", {value: undefined, configurable: true}); });
  const errors = [];
  const onError = error => errors.push(error.message);
  page.on("pageerror", onError);
  const requests = [];
  const callback = "http://[::1]:8123/receiveChatBotMsg";
  await page.route("**/health", route => route.fulfill({json: {status: "ok", hook_base_url: "http://127.0.0.1:30001", callback_url: callback}}));
  await page.route("**/QueryDB/status", route => route.fulfill({json: {IsLogin: true}}));
  await page.route("**/set_callback", route => {
    requests.push(route.request().postDataJSON());
    return route.fulfill({json: {ret: 0}});
  });
  try {
    await page.goto("http://127.0.0.1:8138/messages");
    await page.waitForFunction(() => document.getElementById("hook-status").textContent === "微信已登录");
    await page.locator("#setup-callback").click();
    await page.waitForFunction(() => document.getElementById("callback-status").classList.contains("success"));
    assert(requests.length === 1 && requests[0].url === callback, "Callback must use the backend listener");
    await page.goto("http://127.0.0.1:8138/douyin");
    await page.waitForFunction(() => !document.getElementById("save-settings").disabled);
    await page.goto("http://127.0.0.1:8138/manage");
    await page.waitForFunction(() => typeof fetchJSON === "function");
    const checks = await page.evaluate(async () => {
      const originalFetch = window.fetch;
      const originalSetTimeout = window.setTimeout;
      const originalClearTimeout = window.clearTimeout;
      const timers = new Set();
      // Track only the helper's explicitly chosen timeout; background polling
      // has independent timers and must keep running normally.
      window.setTimeout = (fn, ms, ...args) => {
        const id = originalSetTimeout(fn, ms, ...args);
        if (ms === 23) timers.add(id);
        return id;
      };
      window.clearTimeout = id => { timers.delete(id); originalClearTimeout(id); };
      const results = [];
      try {
        for (const mode of ["success", "http_error", "fetch_error", "json_error", "slow_headers", "slow_body"]) {
          let signal;
          const pending = () => new Promise((resolve, reject) => {
            signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), {once: true});
          });
          window.fetch = async (path, options) => {
            if (path !== "/compat-check") return originalFetch(path, options);
            signal = options.signal;
            if (mode === "fetch_error") throw new TypeError("Network failure");
            if (mode === "slow_headers") return pending();
            return {ok: mode !== "http_error", status: mode === "http_error" ? 503 : 200, json: async () => {
              if (mode === "json_error") throw new SyntaxError("Invalid JSON");
              if (mode === "slow_body") return pending();
              return {value: 42};
            }};
          };
          let result, error;
          try { result = await fetchJSON("/compat-check", {}, 23); } catch (e) { error = e.name; }
          results.push({mode, value: result?.data.value, status: result?.response.status, error, aborted: signal.aborted, timers: timers.size});
        }
      } finally {
        window.fetch = originalFetch;
        window.setTimeout = originalSetTimeout;
        window.clearTimeout = originalClearTimeout;
      }
      return results;
    });
    assert(checks.every(row => row.timers === 0), "Request timers leaked");
    assert(checks[0].value === 42 && checks[1].status === 503, "Response data lost");
    assert(checks[2].error === "TypeError" && checks[3].error === "SyntaxError", "Errors were swallowed");
    assert(checks.slice(4).every(row => row.aborted && row.error === "AbortError"), "Timeout must cover headers and body");
    assert(errors.length === 0, errors.join("\n"));
    return {pages: 3, callback, checks, errors};
  } finally {
    page.off("pageerror", onError);
    await page.unrouteAll({behavior: "wait"});
  }
}
