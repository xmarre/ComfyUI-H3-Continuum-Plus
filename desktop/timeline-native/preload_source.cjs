"use strict";
const { contextBridge, webUtils, ipcRenderer } = require("electron");
const { toWSLPath } = require("./native_link.cjs");
const pending = new Set();

// Native file paths stay inside the isolated preload/main process. Page JS
// sees only registered metadata; no arbitrary IPC or HTTP client is exposed.
contextBridge.exposeInMainWorld("h3NativeMedia", {
  async linkFile(file, transaction) {
    if (typeof transaction !== "string" ||
        !/^[a-zA-Z0-9_-]{1,80}$/.test(transaction) ||
        pending.has(transaction)) {
      throw new Error("Invalid or duplicate media transaction");
    }
    const source = toWSLPath(webUtils.getPathForFile(file));
    pending.add(transaction);
    try {
      return await ipcRenderer.invoke("h3-timeline:link", {
        transaction, path: source,
      });
    } finally {
      pending.delete(transaction);
    }
  },
  cancelLink(transaction) {
    if (pending.has(transaction)) {
      ipcRenderer.send("h3-timeline:cancel", transaction);
    }
  },
});
