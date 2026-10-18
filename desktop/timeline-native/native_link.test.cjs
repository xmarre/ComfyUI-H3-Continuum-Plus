"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { toWSLPath, linkNativeFile } = require("./native_link.cjs");

test("Converts native Windows Explorer paths directly without filesystem searches", () => {
  assert.equal(toWSLPath("D:\\Movies\\A Film.mkv"), "/mnt/d/Movies/A Film.mkv");
  assert.equal(toWSLPath("C:\\Users\\René\\Videos\\movie.mkv"), "/mnt/c/Users/René/Videos/movie.mkv");
});

test("Rejects UNC, namespace aliases, traversal, synthetic or pathless files", () => {
  for (const path of ["", "movie.mkv", "\\\\host\\share\\movie.mkv", "\\\\?\\D:\\movie.mkv", "D:\\..\\movie.mkv", "D:\\folder\\.\\movie.mkv", "/mnt/d/movie.mkv", "D:\\f/other.mkv", null]) {
    assert.throws(() => toWSLPath(path));
  }
});

test("Links an actual dropped File path without source-byte upload or exposing path", async () => {
  const file = { name: "two-hour.mkv", size: 34_000_000_000 };
  const calls = [];
  const pending = new Map();
  const asset = { id: "sha", name: "two-hour.mkv", storage: "linked" };
  const result = await linkNativeFile(file, "drag1", {
    getPathForFile(value) { assert.equal(value, file); return "E:\\Films\\two-hour.mkv"; },
    async fetchImpl(url, options) {
      calls.push([url, options]);
      assert.equal(options.method, "POST");
      assert.deepEqual(JSON.parse(options.body), { path: "/mnt/e/Films/two-hour.mkv" });
      assert.ok(!(options.body instanceof Blob));
      return { ok: true, json: async () => asset };
    },
    pending,
  });
  assert.deepEqual(result, asset);
  assert.equal(Object.hasOwn(result, "path"), false);
  assert.deepEqual(calls.map(([url]) => url), ["/h3_continuum/timeline/link"]);
  assert.equal(pending.size, 0);
});

test("Bridge forbids empty native paths before making any request", async () => {
  let requested = false;
  await assert.rejects(
    linkNativeFile({}, "synthetic", {
      getPathForFile: () => "",
      fetchImpl: async () => { requested = true; },
      pending: new Map(),
    }),
    /Windows Explorer/,
  );
  assert.equal(requested, false);
});

test("Native cancellation aborts pending HTTP request and releases transaction", async () => {
  const pending = new Map();
  const work = linkNativeFile({}, "cancel-one", {
    getPathForFile: () => "D:\\test.mkv",
    fetchImpl: (_url, {signal}) => new Promise((_resolve,reject) => {
      signal.addEventListener("abort", () => reject(new Error("aborted")), { once:true });
    }),
    pending,
  });
  await Promise.resolve();
  assert.equal(pending.has("cancel-one"), true);
  pending.get("cancel-one").abort();
  await assert.rejects(work, /aborted/);
  assert.equal(pending.size, 0);
});

test("Server rejection never causes a managed upload fallback", async () => {
  let attempts = 0;
  await assert.rejects(
    linkNativeFile({}, "reject", {
      getPathForFile: () => "D:\\x.mkv",
      fetchImpl: async () => {
        attempts++;
        return { ok: false, json: async () => ({ error: "Source changed" }) };
      },
      pending: new Map(),
    }),
    /Source changed/,
  );
  assert.equal(attempts, 1);
});
