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
  assert.match(status.at(-1), /No video on the timeline/);
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
