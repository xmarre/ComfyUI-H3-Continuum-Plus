import test from "node:test";
import assert from "node:assert/strict";
import { TimelineEditor } from "../web/timeline/editor.js";
import {
  selectedChunkPage, chunkWindow, overlappingChunkClips,
  setChunkPrompt, addChunkMedia, updateChunkSource, removeChunkClip,
  selectedConveyorSlots, setConveyorSlotEnabled, inheritConveyorSlots,
} from "../web/timeline/chunk_workspace.js";
import { samplerInputDescription, referenceSocketSources, activeReferenceSources } from "../web/timeline/chunk_ui.js";
import { traceReferenceSocket, collectReferenceProvenance, shelfImagePath, conveyorState, conveyorShelfAvailable } from "../web/timeline/conveyor_source.js";
import { layoutView, normalizeVertical, normalizeHorizontal, setLayoutDimension, LIMITS } from "../web/timeline/layout.js";

import {
  changeTiming,
  migrateSamplerWorkflow,
  orderSampler,
  refreshProjectWidgets,
} from "../web/timeline/sampler_layout.js";
import {
  History,
  split,
  trim,
  move,
  remove,
  paste,
  snap,
  sourceAt,
  exportTarget,
  routeClipToGuidance,
  visibleClipGeometry,
  viewerFrameDimensions,
} from "../web/timeline/core.js";
const project = () => ({
  duration_frames: 504,
  tracks: [
    {
      id: "a",
      clips: [{ id: "c", start: 168, end: 336, source_in: "35", speed: "1" }],
    },
  ],
});
test("sampler socket ordering updates live link targets without changing link identities", () => {
  const inputNames = [
    "timeline",
    "model",
    "audio_vae",
    "reference_image_1",
    "reference_video_1",
    "driving_audio",
    "sequence_prompt",
    "reference_image_4",
  ];
  const node = {
    inputs: inputNames.map((name, i) => ({ name, link: i + 1 })),
    graph: {
      links: Object.fromEntries(
        inputNames.map((_, i) => [
          i + 1,
          { id: i + 1, origin_id: 100 + i, target_slot: i },
        ]),
      ),
    },
    widgets: [],
  };
  orderSampler(node);
  assert.deepEqual(
    node.inputs.map((i) => i.name),
    [
      "timeline",
      "model",
      "sequence_prompt",
      "reference_image_1",
      "reference_image_4",
      "reference_video_1",
      "driving_audio",
      "audio_vae",
    ],
  );
  for (const [slot, input] of node.inputs.entries()) {
    assert.equal(node.graph.links[input.link].target_slot, slot);
    assert.equal(node.graph.links[input.link].origin_id, 99 + input.link);
  }
});
test("legacy sampler values migrate by name and inherit their connected project canvas", () => {
  const p = { width: 960, height: 704 };
  const data = {
    nodes: [
      {
        id: 1,
        type: "H3ContinuumTimelineStudio",
        widgets_values: [JSON.stringify(p)],
      },
      {
        id: 8,
        type: "H3ContinuumTimelineSampler",
        inputs: [{ name: "timeline", link: 5 }],
        widgets_values: [0, 123, "Strong — 39 frames", "native", 2, 17, false],
      },
    ],
    links: [[5, 1, 0, 8, 0, "H3_CONTINUUM_TIMELINE_PROJECT"]],
  };
  migrateSamplerWorkflow(data, {
    width: 1344,
    height: 768,
    prompt_mode: "Auto",
  });
  const sampler = data.nodes[1];
  assert.equal(sampler.widgets_values_named.width, 960);
  assert.equal(sampler.widgets_values_named.height, 704);
  assert.equal(sampler.widgets_values_named.base_seed, 123);
  assert.equal(sampler.widgets_values_named.regenerate_from, 2);
  assert.equal(sampler.widgets_values_named.reroll_nonce, 17);
  sampler.widgets_values_named.width = 1024;
  migrateSamplerWorkflow(data, {
    width: 1344,
    height: 768,
    prompt_mode: "Auto",
  });
  assert.equal(sampler.widgets_values_named.width, 1024);
});
test("opening Studio preserves explicit sampler dimensions while an actual canvas edit synchronizes unlinked widgets", () => {
  const p = { width: 768, height: 512, chunk_frames: 168, selection: [0, 504] };
  const projectWidget = { name: "project_json", value: JSON.stringify(p) };
  const studio = {
    comfyClass: "H3ContinuumTimelineStudio",
    widgets: [projectWidget],
  };
  const node = {
    properties: { h3_timeline_dimensions_initialized: true },
    inputs: [{ name: "timeline", link: 1 }],
    graph: { links: { 1: { origin_id: 7 } }, getNodeById: () => studio },
    widgets: [
      { name: "width", value: 960 },
      { name: "height", value: 704 },
    ],
  };
  refreshProjectWidgets(node);
  refreshProjectWidgets(node, true);
  assert.deepEqual(
    node.widgets.map((w) => w.value),
    [960, 704],
  );
  p.width = 1024;
  projectWidget.value = JSON.stringify(p);
  refreshProjectWidgets(node, true);
  assert.deepEqual(
    node.widgets.map((w) => w.value),
    [1024, 512],
  );
});
test("sampler timing controls edit the actual project selection and frame-grid chunk length", () => {
  const p = { chunk_frames: 168, selection: [0, 504], duration_frames: 504 };
  changeTiming(p, "chunks", 2);
  assert.deepEqual(p.selection, [0, 336]);
  changeTiming(p, "chunk_seconds", 5);
  assert.equal(p.chunk_frames, 120);
  assert.deepEqual(p.selection, [0, 240]);
  changeTiming(p, "chunks", 17);
  assert.equal(p.duration_frames, 2040);
  assert.deepEqual(p.selection, [0, 2040]);
});
test("delayed open-time pruning preserves newly queued ids and does not resurrect cancelled ids", async () => {
  let respond;
  const editor = {
    queuedIds: new Set(["done", "running"]),
    closed: false,
    persistView() {},
    api: {
      fetchApi: () =>
        new Promise((resolve) => {
          respond = resolve;
        }),
    },
  };
  const pending = TimelineEditor.prototype.pruneQueued.call(editor);
  editor.queuedIds.delete("running");
  editor.queuedIds.add("newly-queued");
  respond(
    new Response(
      JSON.stringify({ queue_pending: [], queue_running: [[0, "running"]] }),
    ),
  );
  await pending;
  assert.deepEqual([...editor.queuedIds], ["newly-queued"]);
});
test("split preserves noncontiguous source timestamps", () => {
  const p = project();
  split(p, ["c"], 240);
  assert.equal(p.tracks[0].clips[1].source_in, "38");
  assert.equal(p.tracks[0].clips[0].end, 240);
});
test("trim and slip preserve mapping", () => {
  const p = project();
  trim(p, ["c"], "start", 192);
  assert.equal(sourceAt(p.tracks[0].clips[0], 192), 36);
  move(p, ["c"], 24, true);
  assert.equal(p.tracks[0].clips[0].start, 192);
  assert.equal(sourceAt(p.tracks[0].clips[0], 192), 37);
});
test("undo restores source and clip identity; redo reapplies", () => {
  const h = new History(project());
  h.commit((p) => split(p, ["c"], 240));
  h.undo();
  assert.deepEqual(h.value, project());
  h.redo();
  assert.equal(h.value.tracks[0].clips.length, 2);
});
test("locked track stays immutable", () => {
  const p = project();
  p.tracks[0].locked = true;
  const before = structuredClone(p);
  split(p, ["c"], 240);
  move(p, ["c"], 20);
  remove(p, ["c"], true);
  assert.deepEqual(p, before);
});
test("copy/paste preserves relative positions", () => {
  const p = project();
  const ids = paste(p, [["a", p.tracks[0].clips[0]]], 336);
  assert.equal(ids.length, 1);
  assert.equal(p.tracks[0].clips[1].start, 336);
  assert.equal(p.tracks[0].clips[1].end, 504);
});
test("snapping threshold and left bound", () => {
  assert.equal(snap(170, [168], 3), 168);
  assert.equal(snap(180, [168], 3), 180);
  assert.equal(snap(-1, []), 0);
});
test("ripple left trim closes the cut without overlapping later footage", () => {
  const p = project();
  p.tracks[0].clips.push({
    id: "next",
    start: 336,
    end: 504,
    source_in: "0",
    speed: "1",
  });
  trim(p, ["c"], "start", 192, true);
  assert.deepEqual(
    p.tracks[0].clips.map((c) => [c.start, c.end]),
    [
      [168, 312],
      [312, 480],
    ],
  );
  assert.equal(p.tracks[0].clips[0].source_in, "36");
});
test("failed edit leaves state and history untouched", () => {
  const h = new History(project());
  assert.throws(() =>
    h.commit((p) => {
      p.duration_frames = 1;
      throw new Error("failed");
    }),
  );
  assert.deepEqual(h.value, project());
  assert.equal(h.undoStack.length, 0);
});
test("ripple delete keeps unlocked lanes in sync and respects locks", () => {
  const p = project();
  p.tracks[0].clips.push({
    id: "n",
    start: 336,
    end: 504,
    source_in: "0",
    speed: "1",
  });
  p.tracks.push(
    { id: "prompt", clips: [{ id: "p", start: 400, end: 450 }] },
    { id: "locked", locked: true, clips: [{ id: "l", start: 400, end: 450 }] },
  );
  remove(p, ["c"], true);
  assert.deepEqual(
    p.tracks[0].clips.map((c) => [c.start, c.end]),
    [[168, 336]],
  );
  assert.deepEqual(
    [p.tracks[1].clips[0].start, p.tracks[1].clips[0].end],
    [232, 282],
  );
  assert.equal(p.tracks[2].clips[0].start, 400);
});

test("ripple delete cuts a spanning soundtrack on the same source boundaries", () => {
  const p = project();
  p.tracks.push({
    id: "audio",
    clips: [{ id: "sound", start: 0, end: 504, source_in: "10", speed: "1" }],
  });
  p.markers = [{ frame: 400, end: 450 }];
  p.selection = [168, 504];
  remove(p, ["c"], true);
  assert.deepEqual(
    p.tracks[1].clips.map((c) => [c.start, c.end, c.source_in]),
    [
      [0, 168, "10"],
      [168, 336, "24"],
    ],
  );
  assert.equal(p.duration_frames, 336);
  assert.deepEqual(p.selection, [168, 336]);
  assert.deepEqual(p.markers, [{ frame: 232, end: 282 }]);
});

test("ripple trim cuts spanning audio and preserves later source mapping", () => {
  for (const [edge, frame, cutStart, rightSource] of [
    ["start", 192, 168, "8"],
    ["end", 312, 312, "14"],
  ]) {
    const p = project();
    p.tracks.push({
      id: "audio",
      clips: [{ id: "sound", start: 0, end: 504, source_in: "0", speed: "1" }],
    });
    trim(p, ["c"], edge, frame, true);
    assert.deepEqual(
      p.tracks[1].clips.map((c) => [c.start, c.end, c.source_in]),
      [
        [0, cutStart, "0"],
        [cutStart, 480, rightSource],
      ],
    );
    assert.deepEqual(
      [p.tracks[0].clips[0].start, p.tracks[0].clips[0].end],
      [168, 312],
    );
    assert.equal(p.duration_frames, 480);
  }
});

test("ripple extension inserts time in spanning audio instead of shifting its earlier part", () => {
  const p = project();
  p.tracks.push({
    id: "audio",
    clips: [{ id: "sound", start: 0, end: 504, source_in: "0", speed: "1" }],
  });
  trim(p, ["c"], "end", 360, true);
  assert.deepEqual(
    p.tracks[1].clips.map((c) => [c.start, c.end, c.source_in]),
    [
      [0, 336, "0"],
      [360, 528, "14"],
    ],
  );
  assert.equal(p.duration_frames, 528);
});

test("native desktop drag links the original without issuing an upload", async () => {
  const file = { name: "two-hour.mkv", size: 9000000000 };
  const asset = { id: "abc", name: file.name, storage: "linked" };
  const previous = globalThis.h3NativeMedia;
  const calls = [];
  try {
    globalThis.h3NativeMedia = {
      async linkFile(selected) {
        calls.push(selected);
        return asset;
      },
    };
    const imported = [];
    const editor = {
      abort: new AbortController(),
      message: () => {},
      resolveOriginal(f) {
        return TimelineEditor.prototype.resolveOriginal.call(this, f);
      },
      addImportedAsset(a) { imported.push(a); },
      api: { fetchApi() { throw new Error("Full-file upload is forbidden"); } },
    };
    await TimelineEditor.prototype.importFiles.call(editor, [file]);
    assert.deepEqual(calls, [file]);
    assert.deepEqual(imported, [asset]);
  } finally {
    globalThis.h3NativeMedia = previous;
  }
});

test("ordinary browser drag fails without scanning or copying", async () => {
  const previous = globalThis.h3NativeMedia;
  try {
    delete globalThis.h3NativeMedia;
    const editor = {
      abort: new AbortController(),
      message: () => {},
      api: { fetchApi() { throw new Error("No HTTP calls allowed"); } },
    };
    await assert.rejects(
      TimelineEditor.prototype.resolveOriginal.call(editor, { name: "movie.mkv" }),
      /Timeline Studio Desktop/,
    );
  } finally {
    globalThis.h3NativeMedia = previous;
  }
});

test("cancelled native drop cannot start source registration", async () => {
  const previous = globalThis.h3NativeMedia;
  let called = false;
  try {
    globalThis.h3NativeMedia = { linkFile: () => { called = true; } };
    const abort = new AbortController();
    abort.abort(new Error("cancelled"));
    await assert.rejects(
      TimelineEditor.prototype.resolveOriginal.call({
        abort, message: () => {},
      }, { name: "cancel.mkv" }),
      /cancelled/,
    );
    assert.equal(called, false);
  } finally {
    globalThis.h3NativeMedia = previous;
  }
});

test("closing editor cancels an in-flight native link transaction", async () => {
  const original = globalThis.h3NativeMedia;
  let cancelId = null;
  let requested = null;
  try {
    globalThis.h3NativeMedia = {
      linkFile(_file, id) {
        requested = id;
        return new Promise((_resolve, reject) => {
          this.reject = reject;
        });
      },
      cancelLink(id) {
        cancelId = id;
        this.reject(new Error("native link aborted"));
      },
    };
    const abort = new AbortController();
    const pending = TimelineEditor.prototype.resolveOriginal.call(
      { abort, message: () => {} }, { name: "huge.mkv" },
    );
    abort.abort();
    await assert.rejects(pending, /native link aborted/);
    assert.equal(cancelId, requested);
    assert.match(cancelId, /^[a-f0-9]{32}$/);
  } finally {
    globalThis.h3NativeMedia = original;
  }
});

test("Play/Pause toggles immediately while a segment is still preparing", async () => {
  let started = 0;
  let paused = 0;
  let resolveFirst;
  const player = {
    playWanted: false,
    playEpoch: 0,
    playController: null,
    nextPreview: null,
    closed: false,
    video: { paused: true, pause() { paused++; } },
    p: {duration_frames:504,tracks:[{role:"retained",clips:[{id:"c"}]}]},
    playhead: 0,
    message() {},
    playSegment() {
      started++;
      return new Promise(resolve => { resolveFirst = resolve; });
    },
    stopPlayback() {
      return TimelineEditor.prototype.stopPlayback.call(this);
    },
  };
  const request = TimelineEditor.prototype.playComposition.call(player);
  assert.equal(started, 1);
  assert.equal(player.playWanted, true);
  await TimelineEditor.prototype.playComposition.call(player);
  assert.equal(player.playWanted, false);
  assert.equal(paused, 1);
  assert.equal(started, 1);
  resolveFirst();
  await request;
});

test("blank timeline Play responds without sending a composition export request", async () => {
  const status = [];
  const editor = {
    playWanted: false,
    video: {paused:true},
    p:{duration_frames:504,tracks:[]},
    message(text) {status.push(text);},
    playSegment() {throw new Error("Empty timeline must not render");},
  };
  await TimelineEditor.prototype.playComposition.call(editor);
  assert.match(status.at(-1), /No playable footage or final audio/);
});

test("playback fetch asks for a tiny 2s first window and has a timeout", async () => {
  const sent = [];
  const editor = {
    p:{name:"project"},
    compare:{value:"composed"},
    api:{
      fetchApi(path, options) {
        sent.push({path,options});
        return Promise.resolve({
          ok:true,
          json:async()=>({url:"/composition/abc",start:0,end:48}),
        });
      },
    },
  };
  const ctrl = new AbortController();
  const response = await TimelineEditor.prototype.fetchSegment.call(
    editor, 0, 48, ctrl,
  );
  assert.equal(response.end, 48);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].options.timeoutMs, 15000);
  const data = JSON.parse(sent[0].options.body);
  assert.equal(data.length_frames, 48);
  assert.equal(data.start, 0);
});

test("pause during pending preview invalidates and aborts its request", async () => {
  let done;
  let played = 0;
  const output=[];
  const player = {
    playWanted:true,playEpoch:1,playController:null,nextPreview:null,
    closed:false,abort:new AbortController(),playhead:0,
    p:{duration_frames:504},image:{},message(text){output.push(text);},
    video:{paused:true,pause() {},play(){played++;return Promise.resolve();},
      dataset:{}},
    compare:{value:"composed"},
    fetchSegment(_start,_length,controller) {
      return new Promise(resolve => {done = () => resolve({
        start:0,end:48,url:"/preview/abc",
      });});
    },
    prefetchSegment(){},
    stopPlayback(){return TimelineEditor.prototype.stopPlayback.call(this);},
  };
  const waiting = TimelineEditor.prototype.playSegment.call(player,0,1);
  assert.equal(output.at(-1),"Preparing short playback segment…");
  player.stopPlayback();
  assert.equal(player.playWanted,false);
  done();
  await waiting;
  assert.equal(played,0);
  assert.equal(player.video.src,undefined);
});

test("source proxy for a seek is not transcoded until specifically requested", async () => {
  const source = await (await import("node:fs/promises")).readFile(
    new URL("../web/timeline/editor.js", import.meta.url), "utf8");
  assert.match(source,/A seek preview needs only four seconds/);
  assert.doesNotMatch(source,/\/proxy\/[^\n]+duration=30/);
});

test("resuming a paused buffered composition never calls backend again", async () => {
  let plays = 0;
  const messages = [];
  const editor = {
    playWanted:false,playEpoch:2,playhead:20,
    compositionStart:0,compositionEnd:48,
    video:{
      paused:true,dataset:{source:"/h3_continuum/timeline/composition/cached"},
      play(){plays++;return Promise.resolve();},
      pause(){},
    },
    p:{duration_frames:504,tracks:[{role:"retained",clips:[{start:0,end:504}]}]},
    message(text){messages.push(text);},
    prefetchSegment(start){assert.equal(start,48);},
    playSegment(){throw new Error("resume must never render again");},
  };
  await TimelineEditor.prototype.playComposition.call(editor);
  assert.equal(plays,1);
  assert.equal(editor.playWanted,true);
  assert.equal(messages.at(-1),"Playing");
});

test("short-window prefetch does not duplicate the same future segment", async () => {
  let requests=0;
  const editor = {
    playWanted:true,closed:false,playEpoch:1,
    p:{duration_frames:504},
    fetchSegment(start,length,controller) {
      requests++;
      assert.equal(start,48);
      assert.equal(length,144);
      return new Promise(()=>{});
    },
  };
  TimelineEditor.prototype.prefetchSegment.call(editor,48);
  TimelineEditor.prototype.prefetchSegment.call(editor,48);
  assert.equal(requests,1);
  assert.equal(editor.nextPreview.start,48);
  editor.nextPreview.controller.abort();
});

test("audio-only final soundtrack can be previewed without a video track", async () => {
  let started = 0;
  const player = {
    playWanted:false,playEpoch:0,video:{paused:true},
    p:{duration_frames:504,tracks:[{
      role:"audio",clips:[{start:0,end:504,audio_role:"final"}],
    }]},
    playhead:0,
    playSegment(start,epoch) {
      assert.equal(start,0);
      assert.equal(epoch,1);
      started++;
    },
  };
  await TimelineEditor.prototype.playComposition.call(player);
  assert.equal(started,1);
  assert.equal(player.playWanted,true);
});

test("completed export displays a usable file link instead of only a filename", async () => {
  const calls = [];
  const id = "a".repeat(32);
  const editor = {
    exportPending: false,
    closed: false,
    p: {duration_frames:72, selection:[0,48]},
    exportLink: {hidden:true, textContent:""},
    exportStatus: {textContent:""},
    async save() {},
    message(){},
    api: {
      fetchApi: async (url, options) => {
        calls.push([url, options]);
        let payload;
        if (url.endsWith("/export") && options.method === "POST")
          payload = {
            id, status:"running", total_frames:48,
            render_range:[0,48], resolution:"timeline", width:64, height:64,
          };
        else if (url.endsWith("/export/" + id))
          payload = {
            status:"complete",stage:"complete",
            filename:"Timeline_" + id + ".mkv",
            elapsed_seconds:2.1,
          };
        else throw new Error("Unexpected export URL: " + url);
        return {ok:true,json:async()=>payload};
      },
    },
  };
  await TimelineEditor.prototype.export.call(editor,"lossless");
  assert.equal(editor.exportPending,false);
  assert.equal(editor.exportLink.hidden,false);
  assert.equal(
    editor.exportLink.href,
    "/h3_continuum/timeline/export/" + id + "/file"
  );
  assert.match(editor.exportLink.textContent,/Download Timeline_/);
  assert.equal(calls.length,2);
});

test("export error is visible and no duplicate jobs launch during running export", async () => {
  let launches = 0;
  const id = "b".repeat(32);
  const editor = {
    exportPending:false, closed:false,
    p:{duration_frames:120,selection:[0,120]},
    exportLink:{hidden:true,textContent:""},
    exportStatus:{textContent:""},
    async save(){},
    message(){},
    api: {
      fetchApi:async(url,options)=>{
        if (url.endsWith("/export") && options.method==="POST") {
          launches++;
          return {ok:true,json:async()=>({
            id, status:"running", total_frames:120,
            render_range:[0,120], resolution:"timeline", width:64, height:64,
          })};
        }
        return {
          ok:true,
          json:async()=>({
            status:"running",stage:"encoding",
            frames_done:48,total_frames:120,
            render_range:[0,120], resolution:"timeline", width:64, height:64,elapsed_seconds:3.4,
          }),
        };
      },
    },
  };
  await TimelineEditor.prototype.export.call(editor,"h264");
  assert.match(editor.exportStatus.textContent,/40%/);
  await TimelineEditor.prototype.export.call(editor,"h264");
  assert.equal(launches,1);
  clearTimeout(editor.exportTimer);
});

test("export from a long movie submits only the 14-second selected frame range", async () => {
  const seen = [];
  const job = "c".repeat(32);
  const editor = {
    exportPending:false, closed:false,
    p:{duration_frames:159000, selection:[0,336]},
    exportLink:{hidden:true,textContent:""},
    exportStatus:{textContent:""},
    async save(){},
    message(){},
    api:{
      fetchApi:async(url,opts)=>{
        seen.push({url,opts});
        if(url.endsWith("/export") && opts.method==="POST"){
          const payload=JSON.parse(opts.body);
          assert.deepEqual(payload.render_range,[0,336]);
          assert.equal(payload.project.duration_frames,159000);
          return {ok:true,json:async()=>({
            id:job,status:"running",render_range:[0,336], resolution:"timeline", width:64, height:64,
            total_frames:336,
          })};
        }
        return {ok:true,json:async()=>({
          status:"complete",render_range:[0,336], resolution:"timeline", width:64, height:64,
          total_frames:336, frames_done:336,
          filename:"Timeline_" + job + ".mp4",elapsed_seconds:1.7,
        })};
      },
    },
  };
  await TimelineEditor.prototype.export.call(editor,"h264");
  assert.equal(seen.length,2);
  assert.match(editor.exportStatus.textContent,/336 frames|frames 0–336/);
  assert.match(editor.exportStatus.textContent,/14.00 seconds/);
  assert.equal(editor.exportLink.hidden,false);
});

test("a backend ignoring selected range is cancelled, not left exporting the film", async()=>{
  let cancelled=0;
  const id="d".repeat(32);
  const messages=[];
  const editor={
    exportPending:false,closed:false,
    p:{duration_frames:159000,selection:[0,336]},
    exportLink:{hidden:true,textContent:""},
    exportStatus:{textContent:""},
    async save(){},
    message(msg,error){messages.push([msg,error]);},
    api:{
      fetchApi:async(url,opts)=>{
        if(url.endsWith("/export") && opts.method==="POST")
          return {ok:true,json:async()=>({
            id,status:"running",render_range:[0,159000], resolution:"timeline", width:64, height:64,
            total_frames:159000,
          })};
        if(url.endsWith("/export/"+id+"/cancel")){
          cancelled++;
          return {ok:true,json:async()=>({cancelled:true})};
        }
        throw new Error("Unexpected request " + url);
      },
    },
  };
  await assert.rejects(
    TimelineEditor.prototype.export.call(editor,"lossless"),
    /Server ignored selected in\/out frames/,
  );
  assert.equal(cancelled,1);
  assert.equal(editor.exportPending,false);
});

test("highlighted movie clip wins over stale 0–336 render fields and keeps source PTS",async()=>{
  const clip={id:"scene-714",start:17136,end:17283,source_in:"714",speed:"1"};
  const project={duration_frames:159000,selection:[0,336],tracks:[
    {role:"retained",clips:[clip]},
    {role:"audio",clips:[{...clip,id:"sound",audio_role:"final"}]},
  ]};
  const target=exportTarget(project,["scene-714"]);
  assert.deepEqual([target.start,target.stop],[17136,17283]);
  assert.equal(target.kind,"clip");
  assert.equal(target.sourceIn,"714");
  assert.equal(sourceAt(clip,17136),714);
  assert.equal(sourceAt(clip,17283),720.125);

  const calls=[];
  const id="e".repeat(32);
  const editor={
    p:project,selected:["scene-714"],closed:false,exportPending:false,
    exportLink:{hidden:true,textContent:""},
    exportStatus:{textContent:""},
    edit(fn){fn(this.p);calls.push({kind:"edit",range:[...this.p.selection]});},
    async save(){calls.push({kind:"save"});},
    message(){},
    api:{fetchApi:async(url,options)=>{
      if(url.endsWith("/export") && options.method==="POST"){
        const request=JSON.parse(options.body);
        calls.push({kind:"export",request});
        return {ok:true,json:async()=>({
          status:"running",id,render_range:[17136,17283], resolution:"timeline", width:64, height:64,total_frames:147,
        })};
      }
      if(url.endsWith("/export/"+id))
        return {ok:true,json:async()=>({
          status:"complete",filename:"Timeline_"+id+".mp4",
          elapsed_seconds:1, render_range:[17136,17283], resolution:"timeline", width:64, height:64,
          total_frames:147,
        })};
      throw new Error("unexpected path "+url);
    }},
  };
  await TimelineEditor.prototype.export.call(editor,"h264");
  assert.deepEqual(project.selection,[17136,17283]);
  const submit=calls.find(x=>x.kind==="export").request;
  assert.deepEqual(submit.render_range,[17136,17283]);
  assert.deepEqual(submit.selected_clip_ids,["scene-714"]);
  assert.deepEqual(submit.project.selection,[17136,17283]);
  assert.match(editor.exportStatus.textContent,/highlighted clip/);
  assert.match(editor.exportStatus.textContent,/source in 714s/);
  assert.match(editor.exportStatus.textContent,/6.13 seconds/);
});

test("disjoint highlighted clips reject ambiguous multi-clip export",()=>{
  const p={duration_frames:10000,selection:[0,336],tracks:[
    {role:"retained",clips:[
      {id:"a",start:500,end:700},
      {id:"b",start:900,end:1000},
    ]},
  ]};
  assert.throws(()=>exportTarget(p,["a","b"]),/different timeline ranges/);
  assert.deepEqual(exportTarget(p,[]),{
    start:0,stop:336,kind:"range",clipIds:[],
  });
});

test("Close detaches dialog before throwing cleanup hooks and requests export cancellation",async()=>{
  const events=[], id="f".repeat(32);
  const editor={
    closed:false,exportPending:true,exportId:id,
    root:{close(){events.push("dialog-close");},remove(){events.push("removed");}},
    onClose(){events.push("onClose");},
    persistView(){events.push("view");throw Error("graph unavailable");},
    abort:{abort(){events.push("abort");}},
    resize:{disconnect(){events.push("resize");throw Error("resize unavailable");}},
    stopPlayback(){events.push("stop");throw Error("decoder unavailable");},
    video:{removeAttribute(){}},
    api:{fetchApi:async(path,options)=>{
      events.push(path);
      return {ok:true,json:async()=>({cancelled:true})};
    }},
  };
  TimelineEditor.prototype.close.call(editor);
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(editor.closed,true);
  assert.deepEqual(events.slice(0,3),["dialog-close","removed","onClose"]);
  assert.ok(events.includes("/h3_continuum/timeline/export/"+id+"/cancel"));
  assert.equal(editor.exportPending,false);
});

test("closing during async save never starts a surprise background export",async()=>{
  let finishSave, requests=0;
  const editor={
    closed:false,exportPending:false,selected:[],
    p:{duration_frames:336,selection:[0,336],tracks:[]},
    exportLink:{hidden:true,textContent:""},
    exportStatus:{textContent:""},
    save(){return new Promise(resolve=>{finishSave=resolve;});},
    api:{fetchApi(){requests++;throw Error("must never start export");}},
    message(){},
  };
  const exporting=TimelineEditor.prototype.export.call(editor,"lossless");
  editor.closed=true;
  finishSave();
  await exporting;
  assert.equal(requests,0);
});

test("close during accepted-but-unanswered export POST cancels returned job",async()=>{
  let finishPost, cancels=0;
  const id="9".repeat(32);
  const editor={
    closed:false,exportPending:false,selected:[],
    p:{duration_frames:336,selection:[0,336],tracks:[]},
    exportLink:{hidden:true,textContent:""},
    exportStatus:{textContent:""},
    async save(){},
    message(){},
    api:{fetchApi:async(path,options)=>{
      if(path.endsWith("/export")&&options.method==="POST")
        return await new Promise(resolve=>{finishPost=()=>resolve({
          ok:true,json:async()=>({
            id,total_frames:336,render_range:[0,336], resolution:"timeline", width:64, height:64,
          }),
        });});
      if(path.endsWith("/export/"+id+"/cancel")){
        cancels++;
        return {ok:true,json:async()=>({cancelled:true})};
      }
      throw Error("unexpected path "+path);
    }},
  };
  const exporting=TimelineEditor.prototype.export.call(editor,"h264");
  await new Promise(resolve=>setImmediate(resolve));
  editor.closed=true;
  finishPost();
  await exporting;
  assert.equal(cancels,1);
});

test("retained source routes to H3 video guidance at original PTS without exporting", () => {
  const p = {
    assets: [{ id: "asset-1", video: { width: 1920, height: 1080 } }],
    selection: [0, 336],
    tracks: [{
      id: "source", role: "retained", clips: [{
        id: "scene-714", asset_id: "asset-1",
        start: 17136, end: 17283, source_in: "714",
        speed: "1", audio_role: "final", padding: "error",
      }],
    }],
  };
  routeClipToGuidance(p, ["scene-714"]);
  assert.deepEqual(p.selection, [17136, 17283]);
  assert.equal(p.tracks.length, 2);
  const guide = p.tracks[1].clips[0];
  assert.equal(p.tracks[1].role, "video_guide");
  assert.equal(guide.asset_id, "asset-1");
  assert.equal(guide.source_in, "714");
  assert.equal(guide.speed, "1");
  assert.equal(guide.audio_role, "neither");
  assert.equal(guide.start, 17136);
  assert.equal(guide.end, 17283);
  const firstId = guide.id;
  // An unrelated later guide must not shadow the explicit selected clip.
  p.tracks.push({ id: "other-guide", role: "video_guide", clips: [] });
  p.tracks[0].clips[0].source_in = "720";
  routeClipToGuidance(p, ["scene-714"]);
  assert.equal(p.tracks.length, 3);
  assert.equal(p.tracks[2].role, "video_guide");
  assert.equal(p.tracks[2].clips.length, 1);
  assert.equal(p.tracks[2].clips[0].id, firstId);
  assert.equal(p.tracks[2].clips[0].source_in, "720");
  assert.throws(() => routeClipToGuidance(p, ["no-such-clip"]), /exactly one retained/);
});

test("native export rejects backend that lies about original source pixels", async () => {
  const id="a".repeat(32);
  let cancelled=0;
  const p={
    duration_frames:504, selection:[0,48],
    assets:[{id:"source",video:{width:1920,height:1080}}],
    tracks:[{role:"retained",clips:[{
      id:"c",asset_id:"source",start:0,end:48,
      source_in:"0",speed:"1",
    }]}],
  };
  const editor={
    p,selected:["c"],closed:false,exportPending:false,
    exportResolutionChoice:"source",
    exportLink:{hidden:true,textContent:""},
    exportStatus:{textContent:""},
    async save(){},message(){},
    api:{fetchApi:async(url,options)=>{
      if(url.endsWith("/export") && options.method==="POST"){
        const req=JSON.parse(options.body);
        assert.equal(req.resolution,"source");
        return {ok:true,json:async()=>({
          id,total_frames:48,render_range:[0,48],
          resolution:"source",width:768,height:512,
        })};
      }
      if(url.endsWith("/export/"+id+"/cancel")){
        cancelled++;
        return {ok:true,json:async()=>({cancelled:true})};
      }
      throw Error("unexpected request "+url);
    }},
  };
  await assert.rejects(
    TimelineEditor.prototype.export.call(editor,"lossless"),
    /ignored export resolution or source pixel dimensions/,
  );
  assert.equal(cancelled,1);
  assert.equal(editor.exportPending,false);
});

test("Timeline queue preserves State Manager unsaved-text protection without false success", async () => {
  const managerError = new Error("connected positive/default Text Box contains unsaved text");
  managerError.code = "DSM_UNSAVED_MANAGED_TEXT";
  let prompts = 0;
  const studio = {id:1, widgets:[]};
  const sampler = {
    id:2, comfyClass:"H3ContinuumTimelineSampler",
    inputs:[{name:"timeline",link:11}],widgets:[],
  };
  const editor = {
    p:{duration_frames:504,chunk_frames:168,selection:[0,168]},
    node:studio,queuedIds:new Set(),async save(){},
    persistView(){},message(){},
    app:{
      graph:{_nodes:[studio,sampler],links:{11:{origin_id:1}},
        setDirtyCanvas(){}},
      async graphToPrompt(){return {
        output:{"1":{inputs:{project_json:""}},"2":{inputs:{}}},
        workflow:{nodes:[
          {id:1,widgets_values:[""]},{id:2,widgets_values:[]},
        ]},
      };},
    },
    api:{
      async fetchApi(){return {ok:true,json:async()=>({
        jobs:[{start_chunk:0,requires_previous_job:false}],
      })};},
      async queuePrompt(){prompts++;throw managerError;},
    },
  };
  await assert.rejects(
    TimelineEditor.prototype.queue.call(editor),
    (error) => {
      assert.match(error.message,/0 of 1 job\(s\) confirmed/);
      assert.match(error.message,/Save connected/);
      assert.equal(error.cause,managerError);
      return true;
    },
  );
  assert.equal(prompts,1);
  assert.equal(editor.queuedIds.size,0);
});

test("rapid scrubbing requests only the final thumbnail", async () => {
  let previewCalls=0,drawCalls=0,viewSaves=0;
  const editor={
    p:{duration_frames:1000},closed:false,seekTimer:null,
    stopPlayback(){},drawPlayhead(){drawCalls++;},
    async preview(){previewCalls++;},
    guard(fn){return fn();},
    persistView(){viewSaves++;},
  };
  TimelineEditor.prototype.seek.call(editor,14);
  TimelineEditor.prototype.seek.call(editor,15);
  TimelineEditor.prototype.seek.call(editor,16);
  assert.equal(editor.playhead,16);
  assert.equal(drawCalls,3);
  assert.equal(viewSaves,3);
  await new Promise((resolve)=>setTimeout(resolve,120));
  assert.equal(previewCalls,1);
});

test("a multi-hour clip exposes only visible pixels and real trim edges", () => {
  const fullWidth = 700_000;
  const middle = visibleClipGeometry(180, fullWidth, 290_000, 291_600);
  assert.equal(middle.left, 289_988);
  assert.equal(middle.width, 1_624);
  assert.equal(middle.offset, 289_808);
  assert.equal(middle.showStartTrim, false);
  assert.equal(middle.showEndTrim, false);
  const start = visibleClipGeometry(180, fullWidth, 100, 2_000);
  assert.equal(start.left, 180);
  assert.equal(start.showStartTrim, true);
  assert.equal(start.showEndTrim, false);
  const end = visibleClipGeometry(180, fullWidth, fullWidth - 1_000, fullWidth + 1_000);
  assert.equal(end.showStartTrim, false);
  assert.equal(end.showEndTrim, true);
  assert.equal(visibleClipGeometry(180, 100, 700_000, 700_800), null);
});

test("main viewer requests display-density source pixels, not 240x136 filmstrip tiles", () => {
  const source = {video:{width:1920,height:1080}};
  assert.deepEqual(viewerFrameDimensions(source,740,420,1),[740,416]);
  assert.deepEqual(viewerFrameDimensions(source,740,420,2),[1480,833]);
  assert.deepEqual(viewerFrameDimensions(source,3000,2000,2),[1920,1080]);
  assert.deepEqual(
    viewerFrameDimensions({video:{width:320,height:180}},800,600,1),
    [320,180],
  );
});

test("late two-hour selection becomes two real 168-frame H3 chunks", () => {
  const p={duration_frames:2*3600*24,chunk_frames:168,
    selection:[14952,15288],tracks:[],assets:[]};
  const visible=selectedChunkPage(p);
  assert.equal(visible.total,2);
  assert.deepEqual(visible.chunks.map((x)=>[x.index,x.start,x.end]),
    [[89,14952,15120],[90,15120,15288]]);
  assert.deepEqual(chunkWindow(p,89),[14952,15120]);
  assert.deepEqual(selectedChunkPage({...p,selection:[14980,15140]}).chunks
    .map((x)=>[x.selectedStart,x.selectedEnd]),
    [[14980,15120],[15120,15140]]);
});

test("chunk prompt and source slices compile as exact independent native window inputs", () => {
  const img="a".repeat(64),movie="b".repeat(64),audio="c".repeat(64);
  const p={duration_frames:2*3600*24,chunk_frames:168,
    selection:[14952,15288],assets:[
      {id:img,name:"Reference",kind:"image",video:{start_time:"0"}},
      {id:movie,name:"Movie",kind:"video",video:{width:1920,height:1080,start_time:"0"},audio:{}},
      {id:audio,name:"Voice",kind:"audio",audio:{start_time:"0"}},
    ],tracks:[{id:"retained",role:"retained",clips:[
      {id:"original",asset_id:movie,start:14952,end:15288,
        source_in:"623",speed:"1",padding:"hold",audio_role:"neither"},
    ]}]};
  const first=setChunkPrompt(p,89,"slow push-in"); 
  const second=setChunkPrompt(p,90,"cut to wide shot");
  assert.equal(first.override,true);
  assert.deepEqual([first.start,first.end],[14952,15120]);
  assert.deepEqual([second.start,second.end],[15120,15288]);
  const ref=addChunkMedia(p,89,"image_reference",img);
  const video=addChunkMedia(p,89,"video_guide",movie);
  const sound=addChunkMedia(p,89,"audio",audio,"guide");
  assert.deepEqual([ref.start,ref.end],[14952,15120]);
  assert.deepEqual([video.start,video.end],[14952,15120]);
  assert.equal(video.source_in,"623");
  assert.equal(sound.audio_role,"guide");
  assert.equal(overlappingChunkClips(p,90,["image_reference"]).length,0);
  assert.equal(overlappingChunkClips(p,90,["video_guide"]).length,0);
  updateChunkSource(p,sound.id,"23.5");
  assert.equal(sound.source_in,"23.5");
  updateChunkSource(p,sound.id,"47/2");
  assert.equal(sound.source_in,"47/2");
  assert.throws(()=>updateChunkSource(p,sound.id,"2/0"),/Invalid source/);
  assert.throws(()=>updateChunkSource(p,sound.id,"-1"),/Invalid source/);
  assert.throws(()=>addChunkMedia(p,89,"image_reference",img),/already active/);
  removeChunkClip(p,ref.id);
  assert.equal(overlappingChunkClips(p,89,["image_reference"]).length,0);
});

test("connected native sampler precedence is visible independently of authored inputs", () => {
  const studio={id:1,comfyClass:"H3ContinuumTimelineStudio"};
  const sampler={id:2,comfyClass:"H3ContinuumTimelineSampler",
    widgets:[{name:"base_seed",value:42},{name:"continuity",value:"Strong"},
      {name:"reference_size",value:"Match Output"}],
    inputs:[
      {name:"timeline",link:1},{name:"sequence_prompt",link:2},
      {name:"reference_image_1",link:3},{name:"driving_audio",link:4},
      {name:"width",link:5},
    ]};
  const nodes={1:studio,2:sampler};
  sampler.graph={links:{
    1:{origin_id:1},2:{origin_id:3},3:{origin_id:4},
    4:{origin_id:5},5:{origin_id:6}},
    getNodeById(id){return nodes[id];}};
  const editor={node:studio,p:{selection:[0,336],chunk_frames:168,
    width:768,height:512},app:{graph:{_nodes:[studio,sampler]}}};
  const result=samplerInputDescription(editor);
  assert.equal(result.promptOverridden,true);
  assert.equal(result.audioOverridden,true);
  assert.ok(result.persistent.some((x)=>x.includes("reference_image_1")));
  assert.equal(result.persistentPictureSockets,1);
  assert.match(result.overrides.join(" "),/width comes from a connected node/);
  assert.deepEqual(result.settings.slice(0,2),
    [["Chunks","2"],["Chunk duration","7.000s (168f)"]]);
});

test("changing the connected sampler chunk duration reflows selected authored chunk inputs", () => {
  const assetId="a".repeat(64);
  const p={
    duration_frames:2*3600*24,chunk_frames:168,
    selection:[14952,15288],
    assets:[{id:assetId,kind:"video",video:{start_time:"0"}}],
    tracks:[],
  };
  setChunkPrompt(p,89,"chunk A");
  setChunkPrompt(p,90,"chunk B");
  const clip=addChunkMedia(p,89,"video_guide",assetId);
  updateChunkSource(p,clip.id,"623");
  changeTiming(p,"chunk_seconds",8);
  assert.equal(p.chunk_frames,192);
  assert.deepEqual(p.selection,[14784,15168]);
  const newFirst=Math.floor(p.selection[0]/192);
  assert.deepEqual(selectedChunkPage(p).chunks.map((c)=>c.index),
    [newFirst,newFirst+1]);
  const prompts=p.tracks.filter((t)=>t.role==="prompt")
    .flatMap((t)=>t.clips).sort((a,b)=>a.start-b.start);
  assert.deepEqual(prompts.map((c)=>[c.start,c.end,c.text]),[
    [14784,14976,"chunk A"],[14976,15168,"chunk B"],
  ]);
  assert.equal(clip.start,14784);
  assert.equal(clip.end,14976);
  assert.equal(clip.source_in,"623");
  assert.equal(clip.chunk_index,newFirst);
});

test("audio chunks inherit the displayed soundtrack's late source cut", () => {
  const id="f".repeat(64);
  const p={duration_frames:2*3600*24,chunk_frames:168,
    selection:[14952,15288],
    assets:[{id,name:"Soundtrack",kind:"audio",audio:{start_time:"0"}}],
    tracks:[{id:"sound",role:"audio",clips:[{
      id:"existing-sound",asset_id:id,start:14952,end:15288,
      source_in:"714",speed:"1",padding:"silence",audio_role:"final",
    }]}]};
  const cut=addChunkMedia(p,90,"audio",id,"guide");
  assert.equal(cut.start,15120);
  assert.equal(cut.end,15288);
  assert.equal(cut.source_in,"721");
  assert.equal(cut.audio_role,"guide");
  assert.equal(p.tracks[0].clips[0].audio_role,"final");
});

test("changing selected chunk duration does not move authored clips in other project regions", () => {
  const p = {chunk_frames:168,duration_frames:2*3600*24,
    selection:[14952,15288],tracks:[],assets:[]};
  const farIndex=150;
  const outside=setChunkPrompt(p,farIndex,"Existing later take prompt");
  const original={start:outside.start,end:outside.end,text:outside.text,
    index:outside.chunk_index};
  setChunkPrompt(p,89,"Selected chunk prompt");
  changeTiming(p,"chunk_seconds",8);
  assert.deepEqual({
    start:outside.start,end:outside.end,text:outside.text,
    index:outside.chunk_index,
  },original);
  assert.deepEqual([p.selection[0],p.selection[1]],[14784,15168]);
});


test("Image Conveyor references are selected by connected sampler socket per H3 chunk", () => {
  const project={duration_frames:504,chunk_frames:168,selection:[0,504],
    tracks:[],assets:[]};
  assert.equal(selectedConveyorSlots(project,0),null);
  setConveyorSlotEnabled(project,0,2,false,[1,2,3]);
  assert.deepEqual(selectedConveyorSlots(project,0),[1,3]);
  assert.equal(selectedConveyorSlots(project,1),null);
  setConveyorSlotEnabled(project,0,1,false,[1,2,3]);
  setConveyorSlotEnabled(project,0,3,false,[1,2,3]);
  assert.deepEqual(selectedConveyorSlots(project,0),[]);
  setConveyorSlotEnabled(project,0,2,true,[1,2,3]);
  assert.deepEqual(selectedConveyorSlots(project,0),[2]);
  assert.throws(()=>setConveyorSlotEnabled(project,0,4,true,[1,2,3]),/Connect/);
  inheritConveyorSlots(project,0);
  assert.equal(selectedConveyorSlots(project,0),null);
  assert.equal("conveyor_reference_slots" in project,false);
});

test("Conveyor shelf filenames and thumbnail URLs follow actual wired source outputs", () => {
  const studio={id:1,comfyClass:"H3ContinuumTimelineStudio"};
  const source={
    id:9,comfyClass:"ImageConveyor",
    widgets:[{name:"state_json",value:JSON.stringify({
      output_mode:"persistent_refs",
      reference_slots:[
        {type:"input",filename:"Alice.png",annotated:"characters/Alice.png [input]"},
        null,
        {type:"input",filename:"Bob.jpg",annotated:"Bob.jpg [input]"},
      ],
    })}],
    outputs:Array.from({length:14},(_,i)=>({name:i>=6?"ref_image_"+(i-5):"misc"})),
    properties:{image_conveyor_reference_enabled:[true,false,true]},
  };
  const sampler={id:2,comfyClass:"H3ContinuumTimelineSampler",
    inputs:[
      {name:"timeline",link:1},
      {name:"reference_image_1",link:11},
      {name:"reference_image_3",link:13},
    ],
  };
  const nodes=[studio,sampler,source], links={
    1:{origin_id:1,origin_slot:0},
    11:{origin_id:9,origin_slot:6},
    13:{origin_id:9,origin_slot:8},
  };
  const graph={_nodes:nodes,links,getNodeById(id){return nodes.find((n)=>n.id===id)}};
  sampler.graph=graph;
  const editor={app:{graph},node:studio,
    p:{chunk_frames:168,selection:[0,168]}};
  const sourceInputs=referenceSocketSources(editor);
  assert.deepEqual(sourceInputs.filter((x)=>x.connected).map((x)=>x.slot),[1,3]);
  assert.match(sourceInputs[0].label,/Alice.png/);
  assert.match(sourceInputs[0].thumbnail,/relative_path=characters%2FAlice.png/);
  assert.match(sourceInputs[2].label,/Bob.jpg/);
  assert.equal(sourceInputs[1].connected,false);
  const desc=samplerInputDescription(editor);
  assert.deepEqual(desc.connectedReferenceSlots,[1,3]);
  source.__bil={state:{output_mode:"queue_group",reference_slots:[]}};
  const dynamic=referenceSocketSources(editor);
  assert.equal(dynamic[0].dynamic,true);
  assert.match(dynamic[0].label,/queue-group image 2/);
  assert.equal(dynamic[0].thumbnail,null);
  assert.equal(dynamic[0].unavailable,false);
});


test("Conveyor masks follow selected chunks on duration change and collisions fail without mutation",()=>{
  const p={
    chunk_frames:168,duration_frames:168*160,
    selection:[14952,15288],assets:[],tracks:[],
    conveyor_reference_slots:{"89":[1,3],"90":[],"150":[4]},
  };
  changeTiming(p,"chunk_seconds",8);
  assert.equal(p.chunk_frames,192);
  assert.deepEqual(p.selection,[14784,15168]);
  assert.deepEqual(p.conveyor_reference_slots,{
    "77":[1,3],"78":[],"131":[4],
  });
  const conflicting={
    chunk_frames:168,duration_frames:168*12,
    selection:[336,504],assets:[],tracks:[],
    conveyor_reference_slots:{"0":[1],"1":[2]},
  };
  const initial=JSON.stringify(conflicting);
  assert.throws(()=>changeTiming(conflicting,"chunk_seconds",15),/merges two/);
  assert.equal(JSON.stringify(conflicting),initial);
});

test("Conveyor shelf thumbnails trace through Media Resolution IMAGE processors", () => {
  const studio={id:1,comfyClass:"H3ContinuumTimelineStudio"};
  const conveyor={id:8,comfyClass:"ImageConveyor",
    widgets:[{name:"state_json",value:JSON.stringify({
      output_mode:"persistent_refs",
      reference_slots:[{
        type:"input",filename:"Alice.png",
        annotated:"actors/Alice.png [input]",source_version:"mtime-v8",
      }],
    })}],
    outputs:Array.from({length:14},(_,i)=>({name:i===6?"ref_image_1":"misc"})),
  };
  const mp={id:9,title:"Media Resolution",comfyClass:"UniversalMediaResolution",
    inputs:[{name:"image",type:"IMAGE",link:30},
      {name:"megapixels",type:"FLOAT",link:31}],
    outputs:[{name:"IMAGE",type:"IMAGE"}],
  };
  const sampler={id:2,comfyClass:"H3ContinuumTimelineSampler",
    inputs:[{name:"timeline",link:1},
      {name:"reference_image_1",type:"IMAGE",link:32}]};
  const nodes=[studio,conveyor,mp,sampler];
  const graph={
    _nodes:nodes,
    links:{
      1:{origin_id:1,origin_slot:0},
      30:{origin_id:8,origin_slot:6},
      31:{origin_id:11,origin_slot:0},
      32:{origin_id:9,origin_slot:0},
    },
    getNodeById(id){return nodes.find((n)=>n.id===id)},
  };
  sampler.graph=graph;
  const editor={node:studio,app:{graph},p:{
    chunk_frames:168,selection:[0,336],
  }};
  const traced=traceReferenceSocket(graph,sampler,1);
  assert.equal(traced.conveyor,true);
  assert.equal(traced.shelfNumber,1);
  assert.deepEqual(traced.via,["Media Resolution"]);
  const sources=referenceSocketSources(editor);
  assert.match(sources[0].label,/Alice.png.*Media Resolution/);
  assert.match(sources[0].thumbnail,/relative_path=actors%2FAlice.png/);
  assert.match(sources[0].thumbnail,/size=160/);
  assert.match(sources[0].thumbnail,/v=mtime-v8/);
  assert.equal(sources[0].unavailable,false);
  assert.equal(shelfImagePath({
    output_mode:"persistent_refs",
    reference_slots:[{type:"input",annotated:"../secret.png [input]"}],
  },1),null);
  // Do not invent a source when an intermediate node combines two images.
  mp.inputs.push({name:"image_2",type:"IMAGE",link:33});
  graph.links[33]={origin_id:8,origin_slot:7};
  const ambiguous=referenceSocketSources(editor)[0];
  assert.equal(ambiguous.conveyor,false);
  assert.equal(ambiguous.thumbnail,null);
});

test("reference checkbox edits persist without stopping video or rebuilding movie tracks", () => {
  const p={duration_frames:504,chunk_frames:168,selection:[0,336],
    tracks:[],assets:[]};
  const observed=[];
  const editor={
    editVersion:0,history:{commit(fn){fn(p)}},
    chunkBoard:null,closed:false,
    persistWidget(){observed.push("persist")},
    scheduleChunkCompile(){observed.push("compile")},
    stopPlayback(){throw new Error("Video should continue playing")},
    render(){throw new Error("Movie track must not be rebuilt")},
  };
  TimelineEditor.prototype.editChunkReferences.call(editor,(next)=>
    setConveyorSlotEnabled(next,0,2,false,[1,2,3]));
  assert.equal(editor.editVersion,1);
  assert.deepEqual(p.conveyor_reference_slots,{"0":[1,3]});
  assert.deepEqual(observed,["persist","compile"]);
});

test("main preview reuses decoded nearby filmstrip before starting another thumbnail seek", () => {
  const clip={id:"source-a"};
  const entries=new Map([
    ["source-a:14904:48:/thumb/1",{
      complete:true,naturalWidth:240,src:"/thumb/1",
    }],
    ["source-a:14952:48:/thumb/2",{
      complete:true,naturalWidth:240,src:"/thumb/2",
    }],
    ["source-b:14952:48:/thumb/wrong",{
      complete:true,naturalWidth:240,src:"/thumb/wrong",
    }],
    ["source-a:14976:48:/thumb/pending",{
      complete:false,naturalWidth:0,src:"/thumb/pending",
    }],
  ]);
  const ctx={filmTileCache:entries};
  assert.equal(TimelineEditor.prototype.cachedFilmstripStill.call(ctx,clip,14954),
    "/thumb/2");
  assert.equal(TimelineEditor.prototype.cachedFilmstripStill.call(ctx,clip,14500),
    null);
});

test("Media Resolution wildcard IMAGE input resolves upstream Conveyor without fabricating other inputs",()=>{
  const conveyor={id:8,comfyClass:"ImageConveyor",
    widgets:[{name:"state_json",value:JSON.stringify({
      output_mode:"persistent_refs",
      reference_slots:[{
        type:"input",annotated:"references/character.png [input]",
        filename:"character.png",source_version:"v9",
      }],
    })}],
    outputs:Array.from({length:14},(_,i)=>
      ({type:i>=6?"IMAGE":"INT",name:i===6?"ref_image_1":"other"})),
  };
  const scalar={id:11,comfyClass:"Scalar",outputs:[{type:"FLOAT",name:"megapixels"}]};
  const processor={id:10,title:"Media Resolution — Image Conveyor",
    comfyClass:"SmartImageResize",
    inputs:[
      {name:"pixels",type:"*",link:101},
      {name:"megapixels",type:"*",link:102},
    ],outputs:[{type:"IMAGE",name:"image"}]};
  const sampler={id:2,comfyClass:"H3ContinuumTimelineSampler",
    inputs:[{name:"timeline",link:1},{name:"reference_image_1",link:103}]};
  const studio={id:1,comfyClass:"H3ContinuumTimelineStudio"};
  const nodes=[conveyor,scalar,processor,sampler,studio];
  const graph={_nodes:nodes,getNodeById(id){return nodes.find(n=>n.id===id)},
    links:{
      1:{origin_id:1,origin_slot:0},
      101:{origin_id:8,origin_slot:6,type:"IMAGE"},
      102:{origin_id:11,origin_slot:0,type:"FLOAT"},
      103:{origin_id:10,origin_slot:0,type:"IMAGE"},
    }};
  sampler.graph=graph;
  const trace=traceReferenceSocket(graph,sampler,1);
  assert.equal(trace.conveyor,true);
  assert.equal(trace.shelfNumber,1);
  assert.deepEqual(trace.via,["Media Resolution — Image Conveyor"]);
  const editor={node:studio,app:{graph},p:{chunk_frames:168,selection:[0,168]}};
  const sources=referenceSocketSources(editor);
  assert.match(sources[0].thumbnail,/references%2Fcharacter.png/);
  assert.match(sources[0].label,/character.png/);
  assert.match(sources[0].label,/Media Resolution/);
  assert.equal(sources[0].reason,null);
  // A FLOAT control with a reference-like name is not a second IMAGE path.
  processor.inputs[1].name="reference_strength";
  assert.equal(traceReferenceSocket(graph,sampler,1).shelfNumber,1);
  // An unknown wildcard second IMAGE is genuinely ambiguous.
  processor.inputs[1].type="*";
  graph.links[102].type="IMAGE";
  scalar.outputs[0].type="IMAGE";
  assert.equal(traceReferenceSocket(graph,sampler,1).conveyor,false);
});
test("missing output labels, normalized shelf files and traversal failure are diagnosed",()=>{
  const image={id:8,comfyClass:"ImageConveyor",
    outputs:Array.from({length:14},(_,i)=>({type:i>=6?"IMAGE":"STRING"})),
    widgets:[{name:"state_json",value:JSON.stringify({
      output_mode:"persistent_refs",
      reference_slots:[
        {type:"input",subfolder:"refs",filename:"person.webp"},
      ],
    })}],
  };
  const sampler={inputs:[{name:"reference_image_1",link:51}]};
  const graph={_nodes:[image],links:{51:{origin_id:8,origin_slot:6}},
    getNodeById(id){return id===8?image:null}};
  const trace=traceReferenceSocket(graph,sampler,1);
  assert.equal(trace.shelfNumber,1);
  assert.equal(shelfImagePath(conveyorState(image),1),"refs/person.webp");
  assert.equal(shelfImagePath({output_mode:"persistent_refs",reference_slots:[
    {type:"input",annotated:"C:/windows/secret.png [input]"},
  ]},1),null);
  assert.equal(shelfImagePath({output_mode:"persistent_refs",reference_slots:[
    {type:"input",relative_path:"nested/../../secret.png"},
  ]},1),null);
});

test("preview/chunk/timeline splitter bounds preserve usable space",()=>{
  const initial=normalizeVertical(700,null,null);
  assert.ok(initial.preview>250,JSON.stringify(initial));
  assert.ok(initial.chunk>=LIMITS.chunks);
  assert.ok(initial.timeline>=LIMITS.timeline);
  assert.equal(initial.preview+initial.chunk+initial.timeline,700);
  const expanded=normalizeVertical(700,560,220);
  assert.equal(expanded.preview+expanded.chunk+expanded.timeline,700);
  assert.ok(expanded.timeline>=LIMITS.timeline);
  assert.ok(expanded.chunk>=LIMITS.chunks);
  assert.ok(expanded.preview>=LIMITS.preview);
  const compact=normalizeVertical(220,600,500);
  assert.equal(compact.preview+compact.chunk+compact.timeline,220);
  assert.ok(compact.preview>0 && compact.chunk>0 && compact.timeline>0);
  const restored=normalizeVertical(1100,560,220);
  assert.ok(restored.preview>=560);
  assert.ok(restored.timeline>=LIMITS.timeline);
  const empty=normalizeVertical(0,null,null);
  assert.deepEqual(empty,{preview:0,chunk:0,timeline:0});
});

test("resizing sidebars cannot remove the preview and does not change model inputs",()=>{
  const widths=normalizeHorizontal(1050,580,560);
  assert.equal(widths.left+widths.right+widths.center,1038);
  assert.ok(widths.center>=LIMITS.center);
  assert.ok(widths.left>=LIMITS.sidebar);
  assert.ok(widths.right>=LIMITS.sidebar);
  const narrow=normalizeHorizontal(580,600,600);
  assert.equal(narrow.left+narrow.right+narrow.center,568);
  assert.ok(narrow.center>=LIMITS.center-1);
  const valid=layoutView({
    preview:410,chunk:270,left:205,right:250,card:485,
    thumb:120,track:72,invalid:1337,
  });
  assert.equal(valid.preview,410);
  assert.equal(valid.track,72);
  assert.equal(valid.invalid,undefined);
  assert.equal(setLayoutDimension(valid,"thumb",999).thumb,260);
  assert.throws(()=>setLayoutDimension(valid,"no-such-panel",20),/Invalid/);
  assert.throws(()=>setLayoutDimension(valid,"preview",NaN),/Invalid/);
  assert.deepEqual(layoutView(null),{});
});

test("resized Conveyor preview requests enough original pixels and zoom stays full detail",()=>{
  const conveyor={id:8,comfyClass:"ImageConveyor",
    widgets:[{name:"state_json",value:JSON.stringify({
      output_mode:"persistent_refs",
      reference_slots:[{annotated:"refs/portrait.png [input]",type:"input",
        filename:"portrait.png"}],
    })}],
    outputs:Array.from({length:14},(_,i)=>({
      name:i===6?"ref_image_1":"other",type:"IMAGE"})),
  };
  const node={id:1,comfyClass:"H3ContinuumTimelineStudio"};
  const sampler={id:2,comfyClass:"H3ContinuumTimelineSampler",
    inputs:[{name:"timeline",link:1},{name:"reference_image_1",link:10}]};
  const graph={_nodes:[node,sampler,conveyor],links:{
    1:{origin_id:1,origin_slot:0},
    10:{origin_id:8,origin_slot:6,type:"IMAGE"},
  },getNodeById(id){return this._nodes.find(x=>x.id===id)}};
  sampler.graph=graph;
  const editor={node,app:{graph},p:{chunk_frames:168,selection:[0,168]},
    layout:{thumb:250}};
  const thumbnails=referenceSocketSources(editor);
  assert.match(thumbnails[0].thumbnail,/size=250/);
  editor.layout.thumb=999;
  assert.match(referenceSocketSources(editor)[0].thumbnail,/size=512/);
  editor.layout.thumb=54;
  assert.match(referenceSocketSources(editor)[0].thumbnail,/size=160/);
});

test("multi-input Media Resolution displays all proven Conveyor source thumbnails without guessing an output",()=>{
  const studio={id:1,comfyClass:"H3ContinuumTimelineStudio"};
  const conveyor={id:8,comfyClass:"ImageConveyor",
    widgets:[{name:"state_json",value:JSON.stringify({
      output_mode:"persistent_refs",reference_slots:[
        {type:"input",filename:"subject.png",annotated:"refs/subject.png [input]"},
        {type:"input",filename:"style.png",annotated:"refs/style.png [input]"},
      ],
    })}],
    outputs:Array.from({length:14},(_,i)=>({
      type:i>=6?"IMAGE":"STRING",
      name:i===6?"ref_image_1":i===7?"ref_image_2":"misc",
    })),
  };
  const scalar={id:19,comfyClass:"FloatValue",outputs:[{name:"MP",type:"FLOAT"}]};
  const resolution={
    id:9,title:"Media Resolution — Image Conveyor",
    comfyClass:"MediaResolutionMultiInput",
    inputs:[
      {name:"image_1",type:"IMAGE",link:21},
      {name:"image_2",type:"IMAGE",link:22},
      {name:"megapixels",type:"FLOAT",link:23},
    ],
    outputs:[{name:"result",type:"IMAGE"}],
  };
  const sampler={id:2,comfyClass:"H3ContinuumTimelineSampler",
    inputs:[{name:"timeline",link:1},{name:"reference_image_1",link:31}]};
  const nodes=[studio,conveyor,resolution,sampler,scalar];
  const graph={_nodes:nodes,links:{
    1:{origin_id:1,origin_slot:0},
    21:{origin_id:8,origin_slot:6,type:"IMAGE"},
    22:{origin_id:8,origin_slot:7,type:"IMAGE"},
    23:{origin_id:19,origin_slot:0,type:"FLOAT"},
    31:{origin_id:9,origin_slot:0,type:"IMAGE"},
  },getNodeById(id){return nodes.find(n=>n.id===id)}};
  sampler.graph=graph;
  assert.match(traceReferenceSocket(graph,sampler,1).unresolved,/Ambiguous IMAGE/);
  const provenance=collectReferenceProvenance(graph,sampler,1);
  assert.equal(provenance.truncated,false);
  assert.deepEqual(provenance.images.map(x=>x.shelfNumber),[1,2]);
  const editor={node:studio,app:{graph},
    p:{chunk_frames:168,selection:[0,336]},layout:{thumb:140}};
  const ref=referenceSocketSources(editor)[0];
  assert.equal(ref.conveyor,false);
  assert.equal(ref.candidatePreviews.length,2);
  assert.match(ref.candidatePreviews[0].thumbnail,/refs%2Fsubject.png/);
  assert.match(ref.candidatePreviews[1].thumbnail,/refs%2Fstyle.png/);
  assert.equal(ref.candidatePreviews[0].nodeId,8);
  assert.deepEqual(ref.candidatePreviews[0].via,["Media Resolution — Image Conveyor"]);
  // Disconnected branches, scalar MP, and unrelated Conveyor shelves are not
  // invented in the candidate list.
  resolution.inputs.pop();
  resolution.inputs.push({name:"reference_strength",type:"FLOAT",link:23});
  assert.equal(collectReferenceProvenance(graph,sampler,1).images.length,2);
  const lone=resolution.inputs.pop();
  resolution.inputs.pop();
  assert.deepEqual(collectReferenceProvenance(graph,sampler,1).images.map(x=>x.shelfNumber),[1]);
  resolution.inputs.push({name:"image_2",type:"IMAGE",link:22},lone);
  const other={id:12,comfyClass:"ImageConveyor",outputs:conveyor.outputs,
    widgets:[{name:"state_json",value:conveyor.widgets[0].value}]};
  graph._nodes.push(other);
  assert.equal(collectReferenceProvenance(graph,sampler,1).images.length,2);
});

test("Conveyor output toggles and shelf presence determine currently available references",()=>{
  const state={output_mode:"persistent_refs",
    reference_slots:Array.from({length:8},(_,i)=>({
      annotated:"ref"+(i+1)+".png [input]",type:"input",
    })),
    reference_output_enabled:[true,true,true,true,false,false,false,false],
  };
  const node={properties:{}};
  assert.deepEqual(Array.from({length:8},(_,i)=>
    conveyorShelfAvailable(node,state,i+1)),
    [true,true,true,true,false,false,false,false]);
  // Pending canvas toggles take precedence over serialized backend state.
  node.properties.image_conveyor_reference_enabled=[
    true,false,true,false,false,false,false,false];
  assert.deepEqual(Array.from({length:8},(_,i)=>
    conveyorShelfAvailable(node,state,i+1)),
    [true,false,true,false,false,false,false,false]);
  delete node.properties.image_conveyor_reference_enabled;
  state.reference_slots[1]=null;
  assert.equal(conveyorShelfAvailable(node,state,2),false);
  assert.equal(conveyorShelfAvailable(node,{...state,output_mode:"queue_group"},1),false);
  const runtime={...state};
  delete runtime.reference_output_enabled;
  const serialized={...state,
    reference_output_enabled:[false,true,false,true,false,false,false,false]};
  node.widgets=[{name:"state_json",value:JSON.stringify(serialized)}];
  assert.equal(conveyorShelfAvailable(node,runtime,1),false);
  assert.equal(conveyorShelfAvailable(node,runtime,2),false); // shelf is empty
  assert.equal(conveyorShelfAvailable(node,runtime,4),true);
});
test("eight shared upstream shelf candidates yield four unique active per-chunk reference cards",()=>{
  const studio={id:1,comfyClass:"H3ContinuumTimelineStudio"};
  const conveyor={id:8,comfyClass:"ImageConveyor",
    widgets:[{name:"state_json",value:JSON.stringify({
      output_mode:"persistent_refs",
      reference_slots:Array.from({length:8},(_,i)=>({
        annotated:"refs/person-"+(i+1)+".png [input]",
        type:"input",filename:"person-"+(i+1)+".png",
      })),
      reference_output_enabled:[true,true,true,true,false,false,false,false],
    })}],
    outputs:Array.from({length:14},(_,i)=>({
      name:i>=6?"ref_image_"+(i-5):"extra",
      type:i>=6?"IMAGE":"STRING",
    })),
  };
  const processor={id:9,comfyClass:"MediaResolution",
    title:"Media Resolution — Image Conveyor",
    inputs:Array.from({length:8},(_,i)=>({
      name:"image_"+(i+1),type:"IMAGE",link:100+i,
    })),
    outputs:Array.from({length:8},(_,i)=>({
      type:"IMAGE",name:"reference_"+(i+1),
    })),
  };
  const sampler={id:2,comfyClass:"H3ContinuumTimelineSampler",
    inputs:[{name:"timeline",link:1},
      ...Array.from({length:8},(_,i)=>({
        name:"reference_image_"+(i+1),type:"IMAGE",link:200+i,
      }))]};
  const graph={_nodes:[studio,conveyor,processor,sampler],
    getNodeById(id){return this._nodes.find(n=>n.id===id)},
    links:{1:{origin_id:1,origin_slot:0}}};
  for(let i=0;i<8;i++) {
    graph.links[100+i]={origin_id:8,origin_slot:6+i,type:"IMAGE"};
    graph.links[200+i]={origin_id:9,origin_slot:i,type:"IMAGE"};
  }
  sampler.graph=graph;
  const project={chunk_frames:168,duration_frames:336,
    selection:[0,336],assets:[],tracks:[]};
  const editor={node:studio,app:{graph},p:project,layout:{thumb:140}};
  const raw=referenceSocketSources(editor);
  assert.equal(raw.length,8);
  assert.ok(raw.every(x=>x.candidatePreviews.length===8));
  const available=activeReferenceSources(raw);
  assert.deepEqual(available.map(x=>x.slot),[1,2,3,4]);
  assert.equal(new Set(available.map(x=>x.thumbnail)).size,4);
  for(let i=0;i<4;i++)assert.match(available[i].thumbnail,
    new RegExp("person-"+(i+1)+"\\.png"));
  const settings=samplerInputDescription(editor);
  assert.deepEqual(settings.connectedReferenceSlots,[1,2,3,4,5,6,7,8]);
  assert.deepEqual(settings.availableReferenceSlots,[1,2,3,4]);
  assert.equal(settings.availableReferenceSlots.length,4);
  // The checkbox changes only the chosen chunk's physical sampler socket
  // mask. There is no imported image and no extra media track.
  setConveyorSlotEnabled(project,0,2,false,settings.availableReferenceSlots);
  assert.deepEqual(selectedConveyorSlots(project,0),[1,3,4]);
  assert.equal(selectedConveyorSlots(project,1),null);
  // A newly enabled fifth output becomes visible automatically to chunks
  // that inherit. Explicit masks retain their authored choices.
  conveyor.properties={image_conveyor_reference_enabled:[
    true,true,true,true,true,false,false,false]};
  const more=activeReferenceSources(referenceSocketSources(editor));
  assert.deepEqual(more.map(x=>x.slot),[1,2,3,4,5]);
  assert.deepEqual(selectedConveyorSlots(project,0),[1,3,4]);
  assert.equal(selectedConveyorSlots(project,1),null);
});
