"use strict";

// Keep the timeout active through JSON body consumption. AbortController is
// supported by browsers that do not yet implement AbortSignal.timeout().
async function fetchJSON(path, options = {}, timeout = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, { ...options, signal: controller.signal });
    const data = await response.json();
    return { response, data };
  } finally {
    clearTimeout(timer);
  }
}
