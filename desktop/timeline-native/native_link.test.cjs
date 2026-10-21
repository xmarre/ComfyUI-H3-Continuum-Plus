"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { toWSLPath } = require("./native_link.cjs");

test("native Explorer path converts to WSL without search", () => {
  assert.equal(toWSLPath("D:\\Movies\\A Film.mkv"), "/mnt/d/Movies/A Film.mkv");
  assert.equal(toWSLPath("C:\\Users\\René\\Videos\\film.mkv"), "/mnt/c/Users/René/Videos/film.mkv");
  assert.equal(toWSLPath("E:\\Filme\\宇宙.mkv"), "/mnt/e/Filme/宇宙.mkv");
});

test("native path validation blocks UNC/device/traversal/relative/synthetic paths", () => {
  for (const input of [
    "", "movie.mkv", "\\\\host\\share\\movie.mkv",
    "\\\\?\\D:\\movie.mkv", "D:\\..\\movie.mkv",
    "D:\\folder\\.\\movie.mkv", "/mnt/d/movie.mkv",
    "D:\\f/other.mkv", "D:\\f\\..\\film.mkv",
    "D:\\f\\film.mkv\0", null,
  ]) assert.throws(() => toWSLPath(input));
});

function loadPreload({ nativePath = "D:\\Movies\\film.mkv", wait } = {}) {
  const fs = require("node:fs");
  const vm = require("node:vm");
  const path = require("node:path");
  const script = fs.readFileSync(path.join(__dirname, "preload.cjs"), "utf8");
  let bridge;
  const calls = [];
  const electron = {
    contextBridge: {
      exposeInMainWorld(name, api) {
        assert.equal(name, "h3NativeMedia");
        bridge = api;
      },
    },
    webUtils: { getPathForFile(file) {
      assert.equal(file.name, "film.mkv");
      return nativePath;
    } },
    ipcRenderer: {
      invoke(channel, payload) {
        calls.push({ channel, payload });
        return wait ? wait() : Promise.resolve({ id: "sha256", name: "film.mkv", storage: "linked" });
      },
      send(channel, value) {
        calls.push({ channel, value });
      },
    },
  };
  vm.runInNewContext(script, {
    require(name) {
      if (name === "electron") return electron;
      throw new Error("Forbidden preload dependency: " + name);
    },
    console,
    AbortController,
  }, { filename: "preload.cjs" });
  return { bridge, calls };
}

test("bundled preload obtains native File path but transfers only allowlisted path by IPC", async () => {
  const { bridge, calls } = loadPreload();
  assert.deepEqual(Object.keys(bridge).sort(), ["cancelLink", "captureEditor", "linkFile"]);
  const asset = await bridge.linkFile({ name: "film.mkv", size: 25_000_000_000 }, "native-1");
  assert.equal(asset.storage, "linked");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].channel, "h3-timeline:link");
  assert.equal(calls[0].payload.path, "/mnt/d/Movies/film.mkv");
  assert.equal(calls[0].payload.transaction, "native-1");
  assert.equal(Object.hasOwn(asset, "path"), false);
});

test("pathless browser File fails without invoking backend or uploading bytes", async () => {
  const { bridge, calls } = loadPreload({nativePath: ""});
  await assert.rejects(bridge.linkFile({name:"film.mkv"}, "t"), /drive-letter/);
  assert.equal(calls.length, 0);
});

test("only a currently pending transaction can be cancelled", async () => {
  let reject;
  const { bridge, calls } = loadPreload({
    wait: () => new Promise((_resolve, rej) => { reject = rej; }),
  });
  const work = bridge.linkFile({name:"film.mkv"}, "tx1");
  bridge.cancelLink("unrecognized");
  bridge.cancelLink("tx1");
  assert.equal(calls.length, 2);
  assert.equal(calls[0].channel, "h3-timeline:link");
  assert.equal(calls[1].channel, "h3-timeline:cancel");
  assert.equal(calls[1].value, "tx1");
  reject(new Error("Operation cancelled"));
  await assert.rejects(work, /cancelled/);
  bridge.cancelLink("tx1");
  assert.equal(calls.length, 2);
});

test("duplicate and invalid native transaction IDs are rejected", async () => {
  let resolve;
  const { bridge } = loadPreload({
    wait: () => new Promise(res => { resolve = res; }),
  });
  const pending = bridge.linkFile({name:"film.mkv"}, "same");
  await assert.rejects(bridge.linkFile({name:"film.mkv"}, "same"), /duplicate/i);
  await assert.rejects(bridge.linkFile({name:"film.mkv"}, "../x"), /Invalid/);
  resolve({storage:"linked",id:"a"});
  await pending;
});


test("preload exposes one editor capture action without arbitrary IPC arguments",async()=>{
  const {bridge,calls}=loadPreload();
  await bridge.captureEditor({ignored:"untrusted path"});
  assert.equal(calls.length,1);
  assert.equal(calls[0].channel,"h3-timeline:capture-editor");
  assert.equal(calls[0].payload,undefined);
});
