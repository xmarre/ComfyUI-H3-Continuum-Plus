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
    /drive-letter files mounted in WSL/,
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

test("Bundled sandbox preload exposes only native link and cancellation", async () => {
  const fs = require("node:fs");
  const vm = require("node:vm");
  const path = require("node:path");
  const script = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
  let bridge;
  const sandbox = {
    require(id) {
      if (id === "electron") {
        return {
          contextBridge: {
            exposeInMainWorld(key, value) {
              assert.equal(key, "h3NativeMedia");
              bridge = value;
            },
          },
          webUtils: {
            getPathForFile: () => "D:\\Movies\\movie.mkv",
          },
        };
      }
      throw new Error("Sandbox preload loaded forbidden module: " + id);
    },
    fetch: async (path, options) => {
      assert.equal(path, "/h3_continuum/timeline/link");
      assert.deepEqual(JSON.parse(options.body), {path: "/mnt/d/Movies/movie.mkv"});
      return { ok: true, json: async () => ({ storage: "linked", id: "ok" }) };
    },
    AbortController,
    console,
    setTimeout,
    clearTimeout,
  };
  vm.runInNewContext(script, sandbox, { filename: "preload.cjs" });
  assert.deepEqual(Object.keys(bridge).sort(), ["cancelLink", "linkFile"]);
  const result = await bridge.linkFile({ name: "movie.mkv" }, "real");
  assert.equal(result.id, "ok");
});

test("Native window keeps sandboxing and trusted ComfyUI microphone access", async () => {
  const fs = require("node:fs");
  const vm = require("node:vm");
  const nodePath = require("node:path");
  const source = fs.readFileSync(nodePath.join(__dirname, "main.cjs"), "utf8");
  let options, permissionHandler, trustedContents;
  class Window {
    static getAllWindows() { return []; }
    constructor(input) {
      options = input;
      this.webContents = {
        setWindowOpenHandler() {},
        on() {},
      };
      trustedContents = this.webContents;
    }
    loadURL() { return Promise.resolve(); }
  }
  const app = {
    requestSingleInstanceLock: () => true,
    on: () => {},
    whenReady: () => Promise.resolve(),
    quit() { throw new Error("Unexpected application shutdown"); },
  };
  const electron = {
    app,
    BrowserWindow: Window,
    session: {
      defaultSession: {
        setPermissionRequestHandler(fn) { permissionHandler = fn; },
      },
    },
  };
  vm.runInNewContext(source, {
    require(id) {
      if (id === "electron") return electron;
      if (id === "node:path") return nodePath;
      throw new Error("unexpected native module");
    },
    process: { argv: [] },
    URL,
    console,
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(options.webPreferences.sandbox, true);
  assert.equal(options.webPreferences.nodeIntegration, false);
  assert.equal(options.webPreferences.contextIsolation, true);
  assert.equal(options.webPreferences.webSecurity, true);
  const check = (contents, permission, url) => {
    let allowed = null;
    permissionHandler(contents, permission, value => {allowed = value;}, {
      requestingUrl: url,
    });
    return allowed;
  };
  const local = "http://127.0.0.1:8188/";
  assert.equal(check(trustedContents, "media", local), true);
  assert.equal(check(trustedContents, "clipboard-read", local), true);
  assert.equal(check(trustedContents, "midiSysex", local), false);
  assert.equal(check({}, "media", local), false);
  assert.equal(check(trustedContents, "media", "https://example.com"), false);
});
