"use strict";
const { app, BrowserWindow, session } = require("electron");
const path = require("node:path");

function allowedLocalURL(raw) {
  const url = new URL(raw);
  if (url.protocol !== "http:" ||
      !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
      url.username || url.password) {
    throw new Error("Continuum Timeline Desktop only opens a loopback ComfyUI server");
  }
  return url;
}

function localTarget() {
  const cli = process.argv.find(arg => arg.startsWith("--comfy-url="));
  return allowedLocalURL(cli ? cli.slice("--comfy-url=".length) : "http://127.0.0.1:8188/");
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => BrowserWindow.getAllWindows()[0]?.focus());
  app.whenReady().then(() => {
    let target;
    try { target = localTarget(); }
    catch (error) { console.error(error.message); app.quit(); return; }
    const window = new BrowserWindow({
      width: 1600,
      height: 1050,
      minWidth: 820,
      minHeight: 520,
      title: "Continuum Timeline Desktop",
      webPreferences: {
        preload: path.join(__dirname, "preload.cjs"),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        webSecurity: true,
        allowRunningInsecureContent: false,
      },
    });
    // Trusted local ComfyUI microphone/clipboard functionality must keep
    // working. Every other renderer/permission/origin remains denied.
    const permitted = new Set([
      "media", "clipboard-read", "clipboard-sanitized-write", "fullscreen",
    ]);
    session.defaultSession.setPermissionRequestHandler(
      (contents, permission, callback, details) => {
        let origin = null;
        try { origin = new URL(details?.requestingUrl).origin; }
        catch { /* No valid requesting origin. */ }
        callback(
          contents === window.webContents &&
          origin === target.origin &&
          permitted.has(permission),
        );
      },
    );
    window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    window.webContents.on("will-navigate", (event, destination) => {
      try {
        if (new URL(destination).origin !== target.origin) event.preventDefault();
      } catch { event.preventDefault(); }
    });
    window.loadURL(target.toString()).catch(error => console.error("ComfyUI startup:", error.message));
  });
  app.on("window-all-closed", () => app.quit());
}
