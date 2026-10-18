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
/**
 * Render action target: a highlighted output clip takes precedence over the
 * previously typed global [in,out) range. The clip's timeline timestamps
 * remain absolute; its source_in/speed continue to determine input PTS.
 *
 * Multiple separately placed clips are ambiguous: never export the first
 * range silently or merge widely separated footage into a long movie.
 */
export function exportTarget(p, selectedIds = []) {
  const ids = new Set(selectedIds);
  const candidates = (p.tracks || []).flatMap((track) => {
    if (track.muted) return [];
    if (!["retained", "generated", "audio"].includes(track.role)) return [];
    return track.clips
      .filter((clip) => ids.has(clip.id) && !clip.muted &&
        (track.role !== "audio" ||
         ["final", "both"].includes(clip.audio_role)))
      .map((clip) => ({ clip, track }));
  });
  if (candidates.length) {
    const first = candidates[0].clip;
    if (candidates.some(({ clip }) =>
      clip.start !== first.start || clip.end !== first.end)) {
      throw new Error(
        "Selected clips have different timeline ranges. " +
        "Select one clip or set Render [in,out) explicitly."
      );
    }
    const start = first.start, stop = first.end;
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(stop) ||
        start < 0 || stop <= start || stop > p.duration_frames) {
      throw new Error("Selected clip has an invalid timeline range");
    }
    return {
      start, stop, kind: "clip",
      clipIds: candidates.map(({ clip }) => clip.id),
      sourceIn: first.source_in,
    };
  }
  const [start, stop] = p.selection;
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(stop) ||
      start < 0 || stop <= start || stop > p.duration_frames) {
    throw new Error("Invalid Render [in,out) selection");
  }
  return { start, stop, kind: "range", clipIds: [] };
}

/**
 * Route the selected retained source directly to H3 temporal video guidance.
 * This uses the registered asset and its source PTS, not an exported re-encode.
 * Repeating the action refreshes the guide rather than duplicating it.
 */
export function routeClipToGuidance(p, ids) {
  const sources = selectedClips(p, ids)
    .filter(([track]) => track.role === "retained");
  if (sources.length !== 1)
    throw new Error("Select exactly one retained video clip to guide H3");
  const [, source] = sources[0];
  const asset = p.assets.find((a) => a.id === source.asset_id);
  if (!asset?.video)
    throw new Error("Selected source has no video stream");
  let lane = p.tracks.find((t) =>
    t.role === "video_guide" && !t.muted && !t.locked &&
    t.clips.some((c) => c.guidance_source_clip_id === source.id));
  let guide = lane?.clips.find((c) =>
    c.guidance_source_clip_id === source.id);
  if (!lane) {
    // Use a separate, visible guide lane so existing hand-authored guidance
    // cannot silently mask the new clip via bottom-to-top compositing order.
    lane = makeTrack("video_guide", "H3 video guidance");
    p.tracks.push(lane);
  }
  const copy = {
    ...clone(source),
    id: guide?.id || uid(),
    guidance_source_clip_id: source.id,
    audio_role: "neither",
    guide_disabled: false,
  };
  if (guide) Object.assign(guide, copy);
  else lane.clips.push(copy);
  // Guidance is composited bottom-to-top; a later guide lane must not mask
  // the explicit user selection when the action is repeated.
  p.tracks.splice(p.tracks.indexOf(lane), 1);
  p.tracks.push(lane);
  p.selection = [source.start, source.end];
  return copy;
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
  for (const [, c] of selectedClips(p, ids)) {
    const old = clone(c);
    if (edge === "start") {
      c.start = Math.min(c.end - 1, Math.max(0, frame));
      c.source_in = String(sourceAt(old, c.start));
    } else c.end = Math.max(c.start + 1, Math.min(p.duration_frames, frame));
    if (ripple) {
      const delta = c.end - c.start - (old.end - old.start);
      if (edge === "start") {
        c.start = old.start;
        c.end = old.end + delta;
      }
      if (delta < 0) {
        const start = edge === "start" ? old.start : c.end;
        rippleCut(p, start, start - delta, c.id);
      } else if (delta > 0)
        rippleInsert(p, edge === "start" ? old.start : old.end, delta, c.id);
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
  const removed = [];
  for (const track of p.tracks) {
    if (track.locked) continue;
    removed.push(...track.clips.filter((c) => ids.includes(c.id)));
    track.clips = track.clips.filter((c) => !ids.includes(c.id));
  }
  if (!ripple) return;
  const intervals = [];
  for (const c of removed.sort((a, b) => a.start - b.start)) {
    const last = intervals.at(-1);
    if (last && c.start <= last[1]) last[1] = Math.max(last[1], c.end);
    else intervals.push([c.start, c.end]);
  }
  // Close each removed span on every unlocked lane, latest span first.
  for (const [start, end] of intervals.reverse()) rippleCut(p, start, end);
}
function rippleCut(p, start, end, skipId) {
  const cut = end - start;
  const at = (f) => (f <= start ? f : Math.max(start, f - cut));
  for (const track of p.tracks)
    if (!track.locked)
      track.clips = track.clips.flatMap((c) => {
        if (c.id === skipId) return [c];
        if (c.end <= start) return [c];
        if (c.start >= end)
          return [{ ...c, start: c.start - cut, end: c.end - cut }];
        const result = [];
        if (c.start < start) result.push({ ...c, end: start });
        if (c.end > end)
          result.push({
            ...c,
            id: result.length ? uid() : c.id,
            start,
            end: c.end - cut,
            source_in: String(sourceAt(c, end)),
          });
        return result;
      });
  p.duration_frames = Math.max(
    1,
    p.duration_frames - cut,
    ...p.tracks.flatMap((t) => t.clips.map((c) => c.end)),
  );
  if (p.selection) {
    const a = Math.min(at(p.selection[0]), p.duration_frames - 1);
    p.selection = [
      a,
      Math.max(a + 1, Math.min(at(p.selection[1]), p.duration_frames)),
    ];
  }
  if (p.markers)
    p.markers = p.markers.flatMap((m) => {
      const stop = m.end ?? m.frame + 1;
      if (m.frame >= start && stop <= end) return [];
      const frame = at(m.frame),
        after = at(stop);
      return after > frame
        ? [{ ...m, frame, ...(m.end == null ? {} : { end: after }) }]
        : [];
    });
}
function rippleInsert(p, frame, count, skipId) {
  for (const track of p.tracks)
    if (!track.locked)
      track.clips = track.clips.flatMap((c) => {
        if (c.id === skipId || c.end <= frame) return [c];
        if (c.start >= frame)
          return [{ ...c, start: c.start + count, end: c.end + count }];
        return [
          { ...c, end: frame },
          {
            ...c,
            id: uid(),
            start: frame + count,
            end: c.end + count,
            source_in: String(sourceAt(c, frame)),
          },
        ];
      });
  p.duration_frames += count;
  if (p.selection)
    p.selection = p.selection.map((f) => (f >= frame ? f + count : f));
  for (const marker of p.markers || []) {
    if (marker.frame >= frame) marker.frame += count;
    if (marker.end >= frame) marker.end += count;
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
    const candidate = clone(this.value);
    fn(candidate);
    this.undoStack.push(clone(this.value));
    if (this.undoStack.length > 100) this.undoStack.shift();
    this.value = candidate;
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
    // Audio streams commonly end a few milliseconds before their video.
    padding: track.role === "audio" ? "silence" : "error",
    audio_role: track.role === "audio" ? "final" : "neither",
    alias: asset.alias || "",
    name: asset.name,
  };
  track.clips.push(c);
  p.duration_frames = Math.max(p.duration_frames, end);
  return c;
}
