"use strict";
// Pure, testable boundary: only real native File paths, never inferred names.
function toWSLPath(nativePath) {
  if (typeof nativePath !== "string" || nativePath.includes("\0")) {
    throw new Error("Drag a Windows Explorer file from a mounted drive");
  }
  // Reject device, UNC, relative paths and hidden native namespace aliases.
  const match = /^([A-Za-z]):\\([^\r\n]+)$/.exec(nativePath);
  if (!match) {
    throw new Error("Only Windows drive-letter files mounted in WSL are supported");
  }
  const components = match[2].split("\\");
  if (components.some(p => !p || p === "." || p === ".." || p.includes("/"))) {
    throw new Error("Invalid Windows source path");
  }
  return "/mnt/" + match[1].toLowerCase() + "/" + components.join("/");
}

module.exports = { toWSLPath };
