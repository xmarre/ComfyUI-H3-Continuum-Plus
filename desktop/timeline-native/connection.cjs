"use strict";
const http = require("node:http");
const https = require("node:https");

function localURL(raw) {
  if (typeof raw !== "string" || raw.length > 2048) {
    throw new Error("Enter the URL of your running local ComfyUI server");
  }
  const url = new URL(raw);
  if (!["http:", "https:"].includes(url.protocol) ||
      !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname) ||
      url.username || url.password) {
    throw new Error("Only local ComfyUI addresses (localhost or 127.0.0.1) are supported");
  }
  url.hash = "";
  url.search = "";
  url.pathname = "/";
  return url;
}

function candidates(preferred) {
  const items = [
    preferred,
    "http://127.0.0.1:8188/",
    "http://localhost:8188/",
    "http://127.0.0.1:8189/",
    "http://localhost:8189/",
    "http://127.0.0.1:8190/",
    "http://localhost:8190/",
  ];
  const seen = new Set();
  return items.flatMap(item => {
    if (!item) return [];
    try {
      const url = localURL(item);
      if (seen.has(url.href)) return [];
      seen.add(url.href);
      return [url];
    } catch {
      return [];
    }
  });
}

function probe(url, { timeoutMs = 1600, httpImpl, httpsImpl } = {}) {
  const target = new URL("/system_stats", localURL(url.href || String(url)));
  const client = target.protocol === "https:"
    ? (httpsImpl || https)
    : (httpImpl || http);
  return new Promise((resolve, reject) => {
    let done = false;
    let size = 0;
    const finish = (error, result) => {
      if (done) return;
      done = true;
      if (error) reject(error);
      else resolve(result);
    };
    const req = client.get(target, { timeout: timeoutMs }, res => {
      if (res.statusCode === 401 || res.statusCode === 403) {
        res.resume();
        finish(new Error("ComfyUI authentication rejected the health check"));
        return;
      }
      if (res.statusCode !== 200) {
        res.resume();
        finish(new Error("HTTP " + res.statusCode + " at /system_stats"));
        return;
      }
      const chunks = [];
      res.on("data", data => {
        size += data.length;
        if (size > 256 * 1024) {
          req.destroy(new Error("Server returned an oversized health response"));
        } else chunks.push(data);
      });
      res.on("end", () => {
        try {
          const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          if (!parsed || typeof parsed.system !== "object" ||
              !Array.isArray(parsed.devices)) {
            throw new Error("That port does not identify a ComfyUI server");
          }
          finish(null, target.origin);
        } catch (error) {
          finish(error);
        }
      });
      res.on("error", error => finish(error));
    });
    req.on("timeout", () => req.destroy(new Error("Connection timed out")));
    req.on("error", error => finish(error));
  });
}

function escapeHTML(value) {
  return String(value).replace(/[&<>"']/g, c =>
    ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" })[c]);
}

function startupHTML({ heading, detail, url, attempts = [] }) {
  const errorList = attempts.slice(0, 8).map(entry =>
    "<li><code>" + escapeHTML(entry.url) + "</code> — " +
    escapeHTML(entry.reason) + "</li>").join("");
  return '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
    '<meta http-equiv="Content-Security-Policy" content="default-src &apos;none&apos;; style-src &apos;unsafe-inline&apos;; script-src &apos;unsafe-inline&apos;; base-uri &apos;none&apos;; connect-src &apos;none&apos;">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>Continuum Timeline Desktop · Connection</title>' +
    '<style>:root{color-scheme:dark;font-family:system-ui,Segoe UI,Arial,sans-serif}' +
    'body{margin:0;background:#16181d;color:#f2f4f8;min-height:100vh;display:grid;place-items:center}' +
    'main{box-sizing:border-box;width:min(750px,92vw);background:#20242c;border:1px solid #444b57;border-radius:12px;padding:32px}' +
    'h1{font-size:1.35rem;margin:0 0 18px}p{line-height:1.6;color:#e0e5eb}' +
    'label{display:block;margin-top:24px;margin-bottom:8px}' +
    'form{display:flex;gap:10px}input{flex:1;min-width:0;padding:11px;background:#11151a;color:white;border:1px solid #657080;border-radius:6px}' +
    'button{background:#447bea;color:white;border:0;border-radius:6px;padding:11px 18px;cursor:pointer;font-weight:600}' +
    'small,li{color:#c1cbd7}ul{max-height:160px;overflow:auto}code{overflow-wrap:anywhere}</style>' +
    '</head><body><main><h1>' + escapeHTML(heading) + '</h1><p>' +
    escapeHTML(detail) + '</p>' +
    '<form id="connect"><label for="url" style="display:none">ComfyUI URL</label>' +
    '<input id="url" type="url" required aria-label="ComfyUI URL" value="' +
    escapeHTML(url || "http://127.0.0.1:8188/") +
    '" placeholder="http://127.0.0.1:8188/"><button type="submit">Connect</button></form>' +
    '<p><small>Use the same local URL and port that work in your normal browser. ' +
    'The desktop wrapper does not start ComfyUI, scan your drives or copy videos.</small></p>' +
    (errorList ? '<details><summary>Connection diagnostics</summary><ul>' +
      errorList + '</ul></details>' : '') +
    '<script>document.getElementById("connect").addEventListener("submit",event=>{' +
    'event.preventDefault();location.assign(document.getElementById("url").value);' +
    '});</script></main></body></html>';
}

module.exports = { localURL, candidates, probe, escapeHTML, startupHTML };
