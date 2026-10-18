import test from "node:test";
import assert from "node:assert/strict";
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
