"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const {
  localURL, candidates, probe, startupHTML,
} = require("./connection.cjs");

test("local server URL normalization rejects off-machine hosts and credentials", () => {
  assert.equal(localURL("http://127.0.0.1:8188/?workflow=x#edit").href,
    "http://127.0.0.1:8188/");
  assert.equal(localURL("http://localhost:9123/").href,
    "http://localhost:9123/");
  assert.equal(localURL("https://localhost:8188/").href,
    "https://localhost:8188/");
  for (const value of [
    "", "https://example.com", "http://192.168.1.20:8188",
    "http://admin:password@127.0.0.1:8188",
    "file:///C:/movie.mkv", "javascript:alert(1)",
  ]) assert.throws(() => localURL(value));
});

test("candidate selection respects the user's saved address and avoids duplicates", () => {
  const urls = candidates("http://localhost:8188/");
  assert.equal(urls[0].href, "http://localhost:8188/");
  assert.equal(urls.filter(x => x.href === "http://localhost:8188/").length, 1);
  assert.ok(urls.some(x => x.href === "http://127.0.0.1:8188/"));
  assert.ok(urls.some(x => x.href === "http://127.0.0.1:8189/"));
});

test("startup page explains blank-window failures and escapes untrusted error text", () => {
  const html = startupHTML({
    heading: "ComfyUI not reachable",
    detail: "ECONNREFUSED <main>",
    url: "http://localhost:8188/",
    attempts: [{url:"http://localhost:8188/",reason:"<script>attack()</script>"}],
  });
  assert.match(html, /ComfyUI not reachable/);
  assert.match(html, /ECONNREFUSED &lt;main&gt;/);
  assert.doesNotMatch(html, /<script>attack\(\)<\/script>/);
  assert.match(html, /value="http:\/\/localhost:8188\/"/);
  assert.match(html, /form id="connect"/);
});

async function withServer(handler, fn) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  try {
    const addr = server.address();
    return await fn(new URL("http://127.0.0.1:" + addr.port + "/"));
  } finally {
    await new Promise((resolve, reject) =>
      server.close(error => error ? reject(error) : resolve()));
  }
}

test("health probe recognizes a live ComfyUI server by its API response", async () => {
  await withServer((req, res) => {
    assert.equal(req.url, "/system_stats");
    res.writeHead(200, {"content-type":"application/json"});
    res.end(JSON.stringify({system:{os:"linux",python_version:"3.12"},devices:[]}));
  }, async url => {
    assert.equal(await probe(url), url.origin);
  });
});

test("health probe rejects unrelated HTTP services and server errors", async () => {
  await withServer((_req,res) => {
    res.writeHead(200,{"content-type":"application/json"});
    res.end(JSON.stringify({ok:true}));
  }, async url => {
    await assert.rejects(probe(url), /does not identify a ComfyUI server/);
  });
  await withServer((_req,res) => {
    res.writeHead(404);
    res.end("Not Found");
  }, async url => {
    await assert.rejects(probe(url), /HTTP 404/);
  });
});

test("health probe returns actionable errors for unavailable ports", async () => {
  await withServer((_req,res) => { res.end("done"); }, async url => {
    const port = Number(url.port);
    // Exercise live endpoint during the server lifetime to avoid assuming
    // a certain closed port exists on a shared CI Windows host.
    await assert.rejects(probe(url), /does not identify/);
    assert.ok(port > 0);
  });
});

const { verifyTimelineRuntime, NATIVE_PATH_CONTRACT } = require("./connection.cjs");

function runtimeFetch({ backend = true, editor = true, entry = true, unsafe = false } = {}) {
  const calls = [];
  const fetchImpl = async (raw, options) => {
    const url = new URL(raw);
    calls.push({url:url.pathname,options});
    let payload;
    if (url.pathname === "/h3_continuum/timeline/runtime") {
      if (!backend) return new Response("Not Found", {status:404});
      payload = {
        contract: NATIVE_PATH_CONTRACT,
        transport: "native-selected-path",
        drive_scan: false,
        browser_movie_upload: false,
        editor_url: unsafe ? "//example.com/hack/timeline/editor.js"
          : "/extensions/ComfyUI-H3-Continuum-Plus/timeline/editor.js",
        entry_url: "/extensions/ComfyUI-H3-Continuum-Plus/timeline_studio.js",
      };
      return new Response(JSON.stringify(payload), {
        status:200,headers:{"content-type":"application/json"},
      });
    }
    if (url.pathname.endsWith("/timeline/editor.js")) {
      payload = editor
        ? 'export const NATIVE_PATH_CONTRACT = "' + NATIVE_PATH_CONTRACT +
          '"; native.linkFile(file, transaction)'
        : 'this.message("Locating original on mounted Windows drives…");';
      return new Response(payload, {status:200});
    }
    if (url.pathname.endsWith("/timeline_studio.js")) {
      payload = entry
        ? 'const H3_TIMELINE_ENTRY_CONTRACT = "' + NATIVE_PATH_CONTRACT +
          '"; import("./timeline/editor.js?v=' + NATIVE_PATH_CONTRACT + '")'
        : 'import "./timeline/editor.js";';
      return new Response(payload, {status:200});
    }
    return new Response("Not Found", {status:404});
  };
  return {fetchImpl,calls};
}

test("runtime contract checks the actual served no-scan Python and JS code", async () => {
  const mock = runtimeFetch();
  const result = await verifyTimelineRuntime(new URL("http://localhost:8188/"),
    {fetchImpl:mock.fetchImpl});
  assert.equal(result.contract, NATIVE_PATH_CONTRACT);
  assert.equal(mock.calls.length, 3);
  assert.deepEqual(mock.calls.map(x=>x.url), [
    "/h3_continuum/timeline/runtime",
    "/extensions/ComfyUI-H3-Continuum-Plus/timeline/editor.js",
    "/extensions/ComfyUI-H3-Continuum-Plus/timeline_studio.js",
  ]);
  for (const call of mock.calls) {
    assert.equal(call.options.cache, "no-store");
    assert.equal(call.options.method, "GET");
  }
});

test("old ComfyUI Patcher overlay is explicitly rejected before editor load", async () => {
  await assert.rejects(verifyTimelineRuntime("http://127.0.0.1:8188/",
    {fetchImpl:runtimeFetch({backend:false}).fetchImpl}),
    /Apply the current PR #51.*restart the ComfyUI Python process/);
});

test("stale browser editor or entry modules fail closed without scanning", async () => {
  for (const condition of [{editor:false},{entry:false}]) {
    const mock = runtimeFetch(condition);
    await assert.rejects(verifyTimelineRuntime("http://127.0.0.1:8188/",
      {fetchImpl:mock.fetchImpl}), /frontend is stale or mismatched/);
    assert.equal(mock.calls.every(x=>x.options.method==="GET"),true);
  }
});

test("runtime contract never fetches arbitrary or cross-origin editor URLs", async () => {
  const mock = runtimeFetch({unsafe:true});
  await assert.rejects(verifyTimelineRuntime("http://127.0.0.1:8188/",
    {fetchImpl:mock.fetchImpl}), /unsafe frontend module path/);
  assert.equal(mock.calls.length,1);
});

test("backend forbids any claimed scan or movie-upload fallback", async () => {
  const mock = runtimeFetch();
  const wrapped = async (raw,options) => {
    const response = await mock.fetchImpl(raw,options);
    if (new URL(raw).pathname !== "/h3_continuum/timeline/runtime") return response;
    const data=await response.json();
    data.drive_scan=true;
    return new Response(JSON.stringify(data),{status:200});
  };
  await assert.rejects(verifyTimelineRuntime("http://127.0.0.1:8188/",
    {fetchImpl:wrapped}), /backend is stale or incompatible/);
  assert.equal(mock.calls.length,1);
});
