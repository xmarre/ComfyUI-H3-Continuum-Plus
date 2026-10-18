"use strict";
const { app, BrowserWindow, session, ipcMain } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const {
  localURL, candidates, probe, startupHTML, verifyTimelineRuntime,
} = require("./connection.cjs");

function preferredServer() {
  const cli = process.argv.find(arg => arg.startsWith("--comfy-url="));
  if (cli) return cli.slice("--comfy-url=".length);
  try {
    return JSON.parse(fs.readFileSync(path.join(app.getPath("userData"), "server.json"), "utf8")).url;
  } catch { return undefined; }
}

function storeServer(url) {
  try {
    const directory = app.getPath("userData");
    fs.mkdirSync(directory, { recursive: true });
    fs.writeFileSync(path.join(directory, "server.json"),
      JSON.stringify({ url: url.href }) + "\n", { encoding: "utf8", mode: 0o600 });
  } catch (error) {
    console.warn("Cannot remember local ComfyUI address:", error.message);
  }
}

function authorizedSender(event, window, server) {
  if (!server || window.isDestroyed() ||
      event.sender !== window.webContents ||
      !event.senderFrame) return false;
  try {
    return localURL(event.senderFrame.url).origin === server.origin;
  } catch { return false; }
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => BrowserWindow.getAllWindows()[0]?.focus());
  app.whenReady().then(async () => {
    const window = new BrowserWindow({
      width: 1600, height: 1050,
      minWidth: 820, minHeight: 520,
      title: "Continuum Timeline Desktop",
      backgroundColor: "#16181d",
      webPreferences: {
        preload: path.join(__dirname, "preload.cjs"),
        contextIsolation: true, nodeIntegration: false,
        sandbox: true, webSecurity: true,
        allowRunningInsecureContent: false,
      },
    });

    const smoke = process.env.H3_TIMELINE_DESKTOP_SMOKE === "1";

    async function runPackagedSmoke() {
      if (!smoke) return;
      try {
        const inputFile = process.argv.find(arg => arg.startsWith("--h3-smoke-file="));
        if (!inputFile) throw new Error("Missing packaged smoke file");
        const file = inputFile.slice("--h3-smoke-file=".length);
        const debuggerAPI = window.webContents.debugger;
        debuggerAPI.attach("1.3");
        try {
          const { root } = await debuggerAPI.sendCommand("DOM.getDocument");
          const { nodeId } = await debuggerAPI.sendCommand("DOM.querySelector",
            { nodeId: root.nodeId, selector: "#h3-smoke-file" });
          if (!nodeId) throw new Error("Packaged smoke file-input element missing");
          await debuggerAPI.sendCommand("DOM.setFileInputFiles", {
            nodeId, files: [file],
          });
          const result = await window.webContents.executeJavaScript(
            '(async () => {' +
            '  if (!document.body.textContent.includes("H3_SMOKE_READY"))' +
            '    throw new Error("ComfyUI mock document did not render");' +
            '  if (!window.h3NativeMedia)' +
            '    throw new Error("Sandboxed native preload did not load");' +
            '  const input = document.querySelector("#h3-smoke-file");' +
            '  const asset = await window.h3NativeMedia.linkFile(input.files[0], "smoke");' +
            '  return { id: asset.id, storage: asset.storage };' +
            '})()',
          );
          if (result.id !== "h3-smoke-asset" || result.storage !== "linked") {
            throw new Error("Packaged native path IPC returned unexpected asset");
          }
          console.log("H3_NATIVE_DESKTOP_SMOKE_PASSED");
          app.exit(0);
        } finally {
          debuggerAPI.detach();
        }
      } catch (error) {
        console.error("H3_NATIVE_DESKTOP_SMOKE_FAILED:", error);
        app.exit(9);
      }
    }

    let selected = null;
    let mode = "startup";
    let serial = 0;
    const pendingLinks = new Map();

    // Never let a pending source hash/IPC request keep the desktop process
    // alive after a normal Windows close. Aborting the fetch also disconnects
    // the aiohttp request so the WSL backend stops its bounded source read.
    window.on("close", () => {
      ++serial;
      for (const job of pendingLinks.values()) job.abort.abort();
      pendingLinks.clear();
    });
    window.webContents.on("before-input-event", (event, input) => {
      if (input.type === "keyDown" &&
          input.control && input.shift &&
          input.key?.toLowerCase() === "q") {
        event.preventDefault();
        window.close();
      }
    });

    async function display(heading, detail, url, attempts = []) {
      mode = "startup";
      const page = startupHTML({ heading, detail, url, attempts });
      try {
        await window.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(page));
      } catch (error) {
        console.error("Cannot display connection diagnostics:", error.message);
      }
    }

    async function connect(target, { skipProbe = false } = {}) {
      const ticket = ++serial;
      let url;
      try { url = localURL(target.href || String(target)); }
      catch (error) {
        await display("Invalid ComfyUI address", error.message, String(target));
        return;
      }
      await display("Connecting to ComfyUI", "Checking " + url.href + " …", url.href);
      if (!skipProbe) {
        try {
          await probe(url);
        } catch (error) {
          if (ticket === serial) {
            await display("Could not connect to ComfyUI",
              error.message + ". Confirm that ComfyUI is running, then use the same local URL as your browser.",
              url.href, [{ url: url.href, reason: error.message }]);
          }
          return;
        }
      }
      if (ticket !== serial || window.isDestroyed()) return;
      try {
        // Verify the actually running Python backend AND its served JS. A
        // stale ComfyUI Patcher overlay must not run a recursive movie scan.
        await verifyTimelineRuntime(url);
      } catch (error) {
        if (ticket === serial && !window.isDestroyed()) {
          await display(
            "Timeline Studio installation is outdated",
            error.message,
            url.href,
            [{ url: url.href, reason: error.message }],
          );
        }
        return;
      }
      if (ticket !== serial || window.isDestroyed()) return;
      selected = url;
      mode = "comfy";
      try {
        // The desktop owns an isolated Electron cache; clearing it is safe
        // and prevents an old extension ES module surviving a PR replacement.
        await session.defaultSession.clearCache?.();
        await session.defaultSession.clearStorageData?.({
          storages: ["cachestorage", "serviceworkers"],
        });
        await window.loadURL(url.href);
        if (ticket === serial && !window.isDestroyed()) {
          if (smoke) {
            await runPackagedSmoke();
            return;
          }
          storeServer(url);
          window.setTitle("Continuum Timeline Desktop — " + url.host);
        }
      } catch (error) {
        if (ticket === serial && !window.isDestroyed()) {
          selected = null;
          await display("ComfyUI page could not load", error.message, url.href);
        }
      }
    }

    const permitted = new Set([
      "media", "clipboard-read", "clipboard-sanitized-write", "fullscreen",
    ]);
    session.defaultSession.setPermissionRequestHandler(
      (contents, permission, callback, details) => {
        let origin = null;
        try { origin = new URL(details?.requestingUrl).origin; }
        catch { /* No valid requesting origin. */ }
        callback(!!(contents === window.webContents && mode === "comfy" &&
          selected && origin === selected.origin && permitted.has(permission)));
      },
    );
    window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    window.webContents.on("will-navigate", (event, destination) => {
      if (mode !== "comfy") {
        event.preventDefault();
        if (window.webContents.getURL().startsWith("data:text/html")) {
          void connect(destination);
        }
        return;
      }
      try {
        if (new URL(destination).origin !== selected.origin) event.preventDefault();
      } catch { event.preventDefault(); }
    });
    window.webContents.on("will-redirect", (event, destination) => {
      if (mode !== "comfy") return;
      try {
        if (new URL(destination).origin !== selected.origin) event.preventDefault();
      } catch { event.preventDefault(); }
    });
    window.webContents.on("did-fail-load", (_event, code, description, url, isMainFrame) => {
      if (code === -3 || !isMainFrame || mode !== "comfy" ||
          !selected || url !== selected.href) return;
      const current = selected;
      selected = null;
      ++serial;
      void display("Failed to display ComfyUI",
        description + " (" + code + "). Check the server address and port.", current.href);
    });
    window.webContents.on("render-process-gone", (_event, details) => {
      console.error("ComfyUI renderer exited:", details.reason);
      ++serial;
      selected = null;
      void display("ComfyUI window crashed",
        "Renderer exited: " + details.reason + ". Restart or try another local URL.",
        preferredServer());
    });

    ipcMain.handle("h3-timeline:link", async (event, request) => {
      if (!authorizedSender(event, window, selected) || mode !== "comfy") {
        throw new Error("Native media linking is restricted to the active local ComfyUI frontend");
      }
      const { transaction, path: source } = request || {};
      if (typeof transaction !== "string" ||
          !/^[a-zA-Z0-9_-]{1,80}$/.test(transaction) ||
          typeof source !== "string" ||
          !/^\/mnt\/[a-zA-Z]\/[^\0\r\n]+$/.test(source) ||
          source.split("/").some(s => s === "." || s === "..")) {
        throw new Error("Invalid native file link request");
      }
      if (pendingLinks.has(transaction)) throw new Error("Duplicate media transaction");
      const abort = new AbortController();
      pendingLinks.set(transaction, { abort, sender: event.sender });
      try {
        const res = await fetch(new URL("/h3_continuum/timeline/link", selected), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: source }),
          signal: abort.signal,
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data?.error || "Original file could not be linked");
        if (data.storage !== "linked" && data.storage !== "managed") {
          throw new Error("Invalid media registration response");
        }
        return data;
      } finally {
        pendingLinks.delete(transaction);
      }
    });
    ipcMain.on("h3-timeline:cancel", (event, transaction) => {
      if (!authorizedSender(event, window, selected)) return;
      const job = pendingLinks.get(transaction);
      if (job?.sender === event.sender) job.abort.abort();
    });

    await display("Starting Continuum Timeline Desktop",
      "Checking your local ComfyUI server. No file or drive scanning will occur.",
      preferredServer());
    const attempted = [];
    for (const url of candidates(preferredServer())) {
      try {
        await probe(url);
        await connect(url, { skipProbe: true });
        return;
      } catch (error) {
        attempted.push({ url: url.href, reason: error.message });
      }
    }
    await display("ComfyUI not reachable",
      "The desktop application started correctly, but could not find a running ComfyUI server at the common local ports. Enter the exact local URL from your browser to retry.",
      preferredServer(), attempted);
  }).catch(error => console.error("Desktop initialization:", error));
  app.on("window-all-closed", () => app.quit());
}
