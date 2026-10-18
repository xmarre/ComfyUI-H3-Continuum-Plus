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

async function linkNativeFile(file, requestId, { getPathForFile, fetchImpl, pending }) {
  if (typeof requestId !== "string" || !/^[a-zA-Z0-9_-]{1,80}$/.test(requestId)) {
    throw new Error("Invalid native import transaction");
  }
  if (pending.has(requestId)) {
    throw new Error("Duplicate native import transaction");
  }
  // getPathForFile is Electron's privileged API, *not* HTML File.name.
  const source = toWSLPath(getPathForFile(file));
  const controller = new AbortController();
  pending.set(requestId, controller);
  try {
    const response = await fetchImpl("/h3_continuum/timeline/link", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: source }),
      signal: controller.signal,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result?.error || "Original media could not be linked");
    if (result?.storage !== "linked" && result?.storage !== "managed") {
      throw new Error("Unexpected Timeline Studio media registration response");
    }
    // Return media metadata only, never disclose a filesystem path to page JS.
    return result;
  } finally {
    pending.delete(requestId);
  }
}

module.exports = { toWSLPath, linkNativeFile };
