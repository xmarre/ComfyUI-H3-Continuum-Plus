// Pure editing operations shared by the editor and executable frontend tests.
export const clone = (value) => JSON.parse(JSON.stringify(value));
export const uid = () =>
  globalThis.crypto.randomUUID
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), (v) =>
        v.toString(16).padStart(2, "0"),
      ).join("");
export function ratio(value) {
  const [a, b = 1] = String(value).split("/").map(Number);
  return a / b;
}
export function sourceAt(clip, frame) {
  return (
    ratio(clip.source_in || 0) +
    ((frame - clip.start) * ratio(clip.speed || 1)) / 24
  );
}
export function snap(frame, points, threshold = 3) {
  const near = points
    .filter((p) => Math.abs(p - frame) <= threshold)
    .sort((a, b) => Math.abs(a - frame) - Math.abs(b - frame));
  return Math.max(0, Math.round(near[0] ?? frame));
}
export function selectedClips(p, ids) {
  return p.tracks.flatMap((t) =>
    t.locked
      ? []
      : t.clips.filter((c) => ids.includes(c.id)).map((c) => [t, c]),
  );
}
export function split(p, ids, frame) {
  for (const [track, c] of selectedClips(p, ids)) {
    if (frame <= c.start || frame >= c.end) continue;
    const right = {
      ...clone(c),
      id: uid(),
      start: frame,
      source_in: String(sourceAt(c, frame)),
    };
    c.end = frame;
    track.clips.push(right);
  }
}
export function trim(p, ids, edge, frame, ripple = false) {
  for (const [track, c] of selectedClips(p, ids)) {
    const old = clone(c);
    if (edge === "start") {
      c.start = Math.min(c.end - 1, Math.max(0, frame));
      c.source_in = String(sourceAt(old, c.start));
    } else c.end = Math.max(c.start + 1, Math.min(p.duration_frames, frame));
    if (ripple) {
      const delta = c.end - c.start - (old.end - old.start);
      for (const other of track.clips)
        if (other.id !== c.id && other.start >= old.end) {
          other.start += delta;
          other.end += delta;
        }
      p.duration_frames = Math.max(
        p.duration_frames,
        ...track.clips.map((x) => x.end),
      );
    }
  }
}
export function move(p, ids, delta, slip = false) {
  const chosen = selectedClips(p, ids);
  if (!chosen.length) return;
  delta = Math.round(delta);
  if (!slip)
    delta = Math.max(delta, -Math.min(...chosen.map(([, c]) => c.start)));
  for (const [, c] of chosen) {
    if (slip) c.source_in = String(sourceAt(c, c.start + delta));
    else {
      c.start += delta;
      c.end += delta;
    }
  }
  p.duration_frames = Math.max(
    p.duration_frames,
    ...p.tracks.flatMap((t) => t.clips.map((c) => c.end)),
  );
}
export function remove(p, ids, ripple = false) {
  for (const track of p.tracks) {
    if (track.locked) continue;
    const removed = track.clips
      .filter((c) => ids.includes(c.id))
      .sort((a, b) => b.start - a.start);
    track.clips = track.clips.filter((c) => !ids.includes(c.id));
    if (ripple)
      for (const c of removed)
        for (const next of track.clips)
          if (next.start >= c.end) {
            next.start -= c.end - c.start;
            next.end -= c.end - c.start;
          }
  }
}
export function paste(p, clipboard, frame) {
  if (!clipboard.length) return [];
  const origin = Math.min(...clipboard.map(([, c]) => c.start));
  const ids = [];
  for (const [trackId, c] of clipboard) {
    const t = p.tracks.find((x) => x.id === trackId);
    if (!t || t.locked) continue;
    const next = {
      ...clone(c),
      id: uid(),
      start: frame + c.start - origin,
      end: frame + c.end - origin,
    };
    t.clips.push(next);
    ids.push(next.id);
    p.duration_frames = Math.max(p.duration_frames, next.end);
  }
  return ids;
}
export class History {
  constructor(project) {
    this.value = clone(project);
    this.undoStack = [];
    this.redoStack = [];
  }
  commit(fn) {
    this.undoStack.push(clone(this.value));
    if (this.undoStack.length > 100) this.undoStack.shift();
    fn(this.value);
    this.redoStack = [];
  }
  undo() {
    if (this.undoStack.length) {
      this.redoStack.push(this.value);
      this.value = this.undoStack.pop();
    }
  }
  redo() {
    if (this.redoStack.length) {
      this.undoStack.push(this.value);
      this.value = this.redoStack.pop();
    }
  }
}
export function makeTrack(role, name = role) {
  return {
    id: uid(),
    role,
    name,
    clips: [],
    locked: false,
    muted: false,
    hidden: false,
  };
}
export function placeAsset(p, asset, track, frame, duration) {
  if (track.locked) throw new Error("Unlock this track before placing media");
  if (track.role === "image_reference" && asset.kind !== "image")
    throw new Error(
      "Image reference sets require a still image; use a Video guidance track for movie footage",
    );
  const end = frame + Math.max(1, duration);
  const c = {
    id: uid(),
    asset_id: asset.id,
    start: frame,
    end,
    source_in: String(
      ratio((asset.video || asset.audio || {}).start_time || 0),
    ),
    speed: "1",
    padding: "error",
    audio_role: track.role === "audio" ? "final" : "neither",
    alias: asset.alias || "",
    name: asset.name,
  };
  track.clips.push(c);
  p.duration_frames = Math.max(p.duration_frames, end);
  return c;
}
