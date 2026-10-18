"use strict";
const { contextBridge, webUtils } = require("electron");
const { linkNativeFile } = require("./native_link.cjs");
const pending = new Map();

// The bridge does not expose absolute paths, arbitrary file readers, Node.js,
// an unrestricted IPC channel or a general-purpose HTTP client to web content.
contextBridge.exposeInMainWorld("h3NativeMedia", {
  linkFile(file, requestId) {
    return linkNativeFile(file, requestId, {
      getPathForFile: (value) => webUtils.getPathForFile(value),
      fetchImpl: (...args) => fetch(...args),
      pending,
    });
  },
  cancelLink(requestId) {
    pending.get(requestId)?.abort();
  },
});
