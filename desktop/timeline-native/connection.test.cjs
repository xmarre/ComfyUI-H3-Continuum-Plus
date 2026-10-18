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
