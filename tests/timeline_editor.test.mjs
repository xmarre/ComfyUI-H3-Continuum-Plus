import test from "node:test";
import assert from "node:assert/strict";
import { TimelineEditor } from "../web/timeline/editor.js";
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
            render_range:[0,48],
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
            render_range:[0,120],
          })};
        }
        return {
          ok:true,
          json:async()=>({
            status:"running",stage:"encoding",
            frames_done:48,total_frames:120,
            render_range:[0,120],elapsed_seconds:3.4,
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
            id:job,status:"running",render_range:[0,336],
            total_frames:336,
          })};
        }
        return {ok:true,json:async()=>({
          status:"complete",render_range:[0,336],
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
            id,status:"running",render_range:[0,159000],
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
          status:"running",id,render_range:[17136,17283],total_frames:147,
        })};
      }
      if(url.endsWith("/export/"+id))
        return {ok:true,json:async()=>({
          status:"complete",filename:"Timeline_"+id+".mp4",
          elapsed_seconds:1, render_range:[17136,17283],
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
            id,total_frames:336,render_range:[0,336],
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
