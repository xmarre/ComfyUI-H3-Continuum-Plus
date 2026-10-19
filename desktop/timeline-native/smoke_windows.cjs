"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { toWSLPath } = require("./native_link.cjs");

// This is a Windows-only CI runner smoke: actually execute the packaged
// Electron app, render a mock ComfyUI page, supply a real disk-backed File
// through Chromium's DOM file-input protocol, and verify native path IPC.
async function main() {
  if (process.platform !== "win32") {
    throw new Error("The packaged native desktop smoke requires Windows");
  }
  const dist = path.join(__dirname, "dist");
  const filename = fs.readdirSync(dist).find(name =>
    /^Continuum-Timeline-Desktop-.*\.exe$/i.test(name));
  if (!filename) throw new Error("Windows portable executable not found");
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), "h3-native-desktop-"));
  const file = path.join(temp, "small-smoke.mkv");
  fs.writeFileSync(file, "H3_TEST_ONLY; not real media");
  const expectedPath = toWSLPath(file);
  let linked = 0;
  const server = http.createServer((req,res) => {
    if (req.url === "/system_stats") {
      res.writeHead(200, {"content-type":"application/json"});
      res.end(JSON.stringify({system:{os:"windows-stub"},devices:[]}));
    } else if (req.url === "/h3_continuum/timeline/runtime") {
      res.writeHead(200, {"content-type":"application/json","cache-control":"no-store"});
      res.end(JSON.stringify({
        contract:"h3-timeline-chunk-inputs-v6",
        transport:"native-selected-path",
        drive_scan:false,browser_movie_upload:false,
        editor_url:"/extensions/ComfyUI-H3-Continuum-Plus/timeline/editor.js",
        entry_url:"/extensions/ComfyUI-H3-Continuum-Plus/timeline_studio.js",
      }));
    } else if (req.url === "/extensions/ComfyUI-H3-Continuum-Plus/timeline/editor.js") {
      res.writeHead(200, {"content-type":"text/javascript"});
      res.end('export const NATIVE_PATH_CONTRACT = "h3-timeline-chunk-inputs-v6";\n' +
        'native.linkFile(file, transaction);');
    } else if (req.url === "/extensions/ComfyUI-H3-Continuum-Plus/timeline_studio.js") {
      res.writeHead(200, {"content-type":"text/javascript"});
      res.end('const H3_TIMELINE_ENTRY_CONTRACT = "h3-timeline-chunk-inputs-v6";\n' +
        'import "./timeline/editor.js?v=h3-timeline-chunk-inputs-v6";');
    } else if (req.url === "/") {
      res.writeHead(200, {"content-type":"text/html; charset=utf-8"});
      res.end("<!doctype html><title>ComfyUI smoke</title><body>" +
        "H3_SMOKE_READY<input id='h3-smoke-file' type='file'></body>");
    } else if (req.url === "/h3_continuum/timeline/link") {
      assert.equal(req.method, "POST");
      const parts = [];
      let bytes = 0;
      req.on("data", data => {
        bytes += data.length;
        if (bytes > 4096) req.destroy();
        else parts.push(data);
      });
      req.on("end", () => {
        const body = JSON.parse(Buffer.concat(parts).toString("utf8"));
        assert.deepEqual(body, {path:expectedPath});
        linked++;
        res.writeHead(200, {"content-type":"application/json"});
        res.end(JSON.stringify({id:"h3-smoke-asset",storage:"linked"}));
      });
    } else {
      res.writeHead(404);
      res.end("Unknown endpoint");
    }
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  let child;
  let timer;
  try {
    const port = server.address().port;
    const exe = path.join(dist, filename);
    const output = [];
    child = spawn(exe, [
      "--comfy-url=http://127.0.0.1:" + port + "/",
      "--h3-smoke-file=" + file,
    ], {
      cwd: __dirname,
      windowsHide: true,
      env: {...process.env, H3_TIMELINE_DESKTOP_SMOKE:"1"},
      stdio: ["ignore", "pipe", "pipe"],
    });
    child.stdout.on("data", data => output.push(String(data)));
    child.stderr.on("data", data => output.push(String(data)));
    const exit = new Promise((resolve,reject) => {
      child.on("error",reject);
      child.on("exit", (code,signal) => resolve({code,signal}));
    });
    const timeout = new Promise((_resolve,reject) => {
      timer = setTimeout(() => {
        child.kill();
        reject(new Error("Packaged Electron startup did not complete in 120 seconds"));
      }, 120000);
    });
    const result = await Promise.race([exit,timeout]);
    if (result.code !== 0 || linked !== 1) {
      throw new Error("Packaged desktop startup/native File IPC failed: " +
        JSON.stringify({exit:result,linked,output:output.join("").slice(-8000)}));
    }
    console.log("Packaged Electron displayed the UI, resolved a genuine OS-backed " +
      "File through its preload, and linked only the original path over IPC.");
  } finally {
    if (timer) clearTimeout(timer);
    if (child && child.exitCode === null) child.kill();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(temp, {recursive:true,force:true});
  }
}
main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
