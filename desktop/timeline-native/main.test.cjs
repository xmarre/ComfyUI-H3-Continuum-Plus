"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const connection = require("./connection.cjs");

const ticks = async () => {
  for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
};

async function launch({ found = true, compatible = true } = {}) {
  const source = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
  const loads = [];
  const listeners = new Map();
  const windowListeners = new Map();
  const ipc = new Map();
  const ipcEvents = new Map();
  let policy, currentURL = "", title = "", contents, exitCalls = 0;
  const fakeWindow = class {
    static getAllWindows() { return []; }
    constructor(options) {
      this.options = options;
      this.webContents = {
        on(name, cb) {listeners.set(name, cb);},
        setWindowOpenHandler() {},
        getURL() {return currentURL;},
        mainFrame:{url:"http://127.0.0.1:8188/"},
        async capturePage() {return {toDataURL(){return "data:image/png;base64,capture";}};},
      };
      contents = this.webContents;
    }
    isDestroyed() {return false;}
    on(name, cb) {windowListeners.set(name, cb);}
    close() {windowListeners.get("close")?.();}
    destroy() {windowListeners.get("close")?.();}
    loadURL(url) {currentURL = url; loads.push(url); return Promise.resolve();}
    setTitle(value) {title = value;}
  };
  const electron = {
    app: {
      requestSingleInstanceLock: () => true,
      on() {},
      whenReady: () => Promise.resolve(),
      getPath: () => "C:\\fake-app-settings",
      quit() {throw new Error("Unexpected app termination");},
      exit(code) { assert.equal(code,0); exitCalls++; },
    },
    BrowserWindow: fakeWindow,
    session: {defaultSession: {
      setPermissionRequestHandler(handler) {policy = handler;},
    }},
    ipcMain: {
      handle(name, handler) { ipc.set(name, handler); },
      on(name, handler) {ipcEvents.set(name, handler);},
    },
  };
  const fakeConnection = {
    ...connection,
    candidates() {return [new URL("http://127.0.0.1:8188/")];},
    probe: async () => {
      if (!found) throw new Error("ECONNREFUSED");
      return "http://127.0.0.1:8188";
    },
    verifyTimelineRuntime: async () => {
      if (!compatible) {
        throw new Error(
          "Timeline Studio backend is stale; apply PR #51 and restart ComfyUI"
        );
      }
      return { contract: connection.NATIVE_PATH_CONTRACT };
    },
  };
  let fetched;
  vm.runInNewContext(source, {
    require(name) {
      if (name === "electron") return electron;
      if (name === "node:path") return path;
      if (name === "node:fs") return {
        readFileSync() {throw new Error("No cached server URL");},
        mkdirSync() {},
        writeFileSync() {},
      };
      if (name === "./connection.cjs") return fakeConnection;
      throw new Error("Unexpected native module " + name);
    },
    process: {argv:[],env:{}},
    __dirname,
    URL,
    console,
    AbortController,
    fetch: async (url, options) => {
      fetched = {url: String(url), options};
      return {ok:true,json:async()=>({id:"sha",storage:"linked"})};
    },
  }, { filename: "main.cjs" });
  await ticks();
  return {loads,listeners,windowListeners,ipc,ipcEvents,get policy(){return policy;},
    get title(){return title;}, get url(){return currentURL;},
    get contents(){return contents;}, get fetched(){return fetched;},
    get exitCalls(){return exitCalls;}};
}

test("Electron startup first displays diagnostics and then loads verified ComfyUI", async () => {
  const app = await launch();
  assert.ok(app.loads[0].startsWith("data:text/html"));
  assert.ok(app.loads.some(url => url === "http://127.0.0.1:8188/"));
  assert.match(app.title, /127.0.0.1:8188/);
  let granted;
  app.policy(app.contents, "media", value => granted = value, {
    requestingUrl:"http://127.0.0.1:8188/",
  });
  assert.equal(granted, true);
  app.policy(app.contents, "media", value => granted = value, {
    requestingUrl:"http://another-site.invalid/",
  });
  assert.equal(granted, false);
  assert.equal(app.ipc.has("h3-timeline:link"), true);
});

test("unreachable server displays a useful connection screen instead of blank white", async () => {
  const app = await launch({found:false});
  assert.ok(app.loads.every(url => url.startsWith("data:text/html")));
  assert.match(decodeURIComponent(app.loads.at(-1)), /ComfyUI not reachable/);
  assert.match(decodeURIComponent(app.loads.at(-1)), /ECONNREFUSED/);
  assert.match(decodeURIComponent(app.loads.at(-1)), /same local URL/);
});

test("native link IPC only permits the active local ComfyUI frame", async () => {
  const app = await launch();
  const link = app.ipc.get("h3-timeline:link");
  const request = {path:"/mnt/d/Movies/film.mkv",transaction:"tx-1"};
  const sender = {sender:app.contents,senderFrame:{url:"http://127.0.0.1:8188/"}};
  const response = await link(sender,request);
  assert.equal(response.storage,"linked");
  assert.equal(app.fetched.url,"http://127.0.0.1:8188/h3_continuum/timeline/link");
  assert.deepEqual(JSON.parse(app.fetched.options.body), {path:request.path});
  await assert.rejects(link({...sender,senderFrame:{url:"http://evil.invalid"}},request),/restricted/);
  await assert.rejects(link(sender,{path:"/etc/passwd",transaction:"tx-2"}),/Invalid/);
  await assert.rejects(link(sender,{path:"/mnt/d/../secret",transaction:"tx-3"}),/Invalid/);
});

test("navigation out of ComfyUI origin is blocked", async () => {
  const app = await launch();
  const navigate = app.listeners.get("will-navigate");
  let prevented = false;
  navigate({preventDefault() { prevented = true; }}, "https://external.invalid/");
  assert.equal(prevented, true);
  prevented = false;
  navigate({preventDefault() { prevented = true; }}, "http://127.0.0.1:8188/another-page");
  assert.equal(prevented, false);
});

test("a failed ComfyUI page load displays the connection diagnostic", async () => {
  const app = await launch();
  app.listeners.get("did-fail-load")({}, -102, "ERR_CONNECTION_REFUSED",
    "http://127.0.0.1:8188/", true);
  await ticks();
  assert.match(decodeURIComponent(app.loads.at(-1)), /Failed to display ComfyUI/);
  assert.match(decodeURIComponent(app.loads.at(-1)), /ERR_CONNECTION_REFUSED/);
});

test("stale backend/frontend stops desktop before old recursive editor is opened", async () => {
  const app = await launch({ compatible: false });
  assert.ok(app.loads.every(url => url.startsWith("data:text/html")));
  assert.match(decodeURIComponent(app.loads.at(-1)), /Timeline Studio installation is outdated/);
  assert.match(decodeURIComponent(app.loads.at(-1)), /apply PR #51 and restart ComfyUI/i);
  const denied = app.ipc.get("h3-timeline:link");
  await assert.rejects(denied({
    sender:app.contents,senderFrame:{url:"http://127.0.0.1:8188/"},
  },{transaction:"x",path:"/mnt/d/movie.mkv"}), /restricted/);
});

test("native Windows close handles pending jobs even if web editor is unresponsive", async () => {
  const app = await launch();
  assert.ok(app.windowListeners.has("close"));
  let prevented = false;
  app.listeners.get("before-input-event")(
    {preventDefault() {prevented = true;}},
    {type:"keyDown", control:true, shift:true, key:"Q"},
  );
  assert.equal(prevented, true);
});

test("application never closes for an unrelated keyboard event", async () => {
  const app = await launch();
  let prevented = false;
  app.listeners.get("before-input-event")(
    {preventDefault() {prevented = true;}},
    {type:"keyDown", control:true, shift:false, key:"Q"},
  );
  assert.equal(prevented, false);
});

test("Windows title-bar X closes even if renderer never responds to beforeunload",async()=>{
  const native=await launch();
  const close=native.windowListeners.get("close");
  assert.equal(typeof close,"function");
  let prevented=false;
  close({preventDefault(){prevented=true;}});
  assert.equal(prevented,true);
  assert.equal(native.exitCalls,1);
  // Reentrant native destroy/close never schedules a second exit.
  close({preventDefault(){throw Error("Double exit") }});
  assert.equal(native.exitCalls,1);
});

test("Ctrl+Shift+Q exits native process without relying on ComfyUI renderer",async()=>{
  const native=await launch();
  let prevented=false;
  native.listeners.get("before-input-event")(
    {preventDefault(){prevented=true;}},
    {type:"keyDown", control:true, shift:true,key:"q"},
  );
  assert.equal(prevented,true);
  assert.equal(native.exitCalls,1);
});


test("editor capture only returns the active application surface to its main frame",async()=>{
  const app=await launch();
  const capture=app.ipc.get("h3-timeline:capture-editor");
  assert.equal(await capture({sender:app.contents,senderFrame:app.contents.mainFrame}),
    "data:image/png;base64,capture");
  await assert.rejects(capture({sender:app.contents,
    senderFrame:{url:"http://127.0.0.1:8188/embedded"}}),/restricted/);
  await assert.rejects(capture({sender:{},senderFrame:app.contents.mainFrame}),/restricted/);
  await assert.rejects(capture({sender:app.contents,
    senderFrame:{url:"https://unrelated.invalid"}}),/restricted/);
  const startup=await launch({compatible:false});
  await assert.rejects(startup.ipc.get("h3-timeline:capture-editor")({
    sender:startup.contents,senderFrame:startup.contents.mainFrame}),/restricted/);
});
