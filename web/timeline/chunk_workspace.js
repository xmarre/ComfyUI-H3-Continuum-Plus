// Chunk-first authoring operations. All spans use the same 24 fps project
// frame clock as timeline/model.py and the connected Continuum sampler.
import { makeTrack, placeAsset, sourceAt, uid, ratio } from "./core.js";

export function chunkWindow(project, index) {
  if (!Number.isSafeInteger(index) || index < 0) throw new Error("Invalid chunk");
  const start = index * project.chunk_frames;
  if (start >= project.duration_frames) throw new Error("Chunk outside project");
  return [start, Math.min(project.duration_frames, start + project.chunk_frames)];
}
export function selectedChunkPage(project, page = 0, pageSize = 8) {
  const start = Math.floor(project.selection[0] / project.chunk_frames);
  const stop = Math.ceil(project.selection[1] / project.chunk_frames);
  const total = stop - start;
  const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1);
  const current = Math.min(Math.max(0, page | 0), lastPage);
  return {
    total, page: current, pages: lastPage + 1,
    chunks: Array.from(
      { length: Math.min(pageSize, total - current * pageSize) },
      (_, i) => {
        const index = start + current * pageSize + i;
        const [a, b] = chunkWindow(project, index);
        return { index, start: a, end: b,
          selectedStart: Math.max(a, project.selection[0]),
          selectedEnd: Math.min(b, project.selection[1]) };
      },
    ),
  };
}
export function overlappingChunkClips(project, index, roles = null) {
  const [start, end] = chunkWindow(project, index);
  return project.tracks.flatMap((track) =>
    (track.muted || (roles && !roles.includes(track.role)))
      ? [] : track.clips
        .filter((clip) => !clip.muted && clip.start < end && start < clip.end)
        .map((clip) => ({ track, clip,
          start: Math.max(clip.start, start),
          end: Math.min(clip.end, end) })),
  );
}
function lane(project, role, name) {
  // An authored chunk lane is deliberately separate from existing edit lanes,
  // so a new guide is topmost without changing any other clip or track.
  let track = project.tracks.find((t) =>
    t.role === role && t.chunk_workspace === true && !t.locked && !t.muted);
  if (!track) {
    track = { ...makeTrack(role, name), chunk_workspace: true };
    project.tracks.push(track);
  }
  return track;
}
// Per-chunk selection of persistent *node sockets*, not local asset files.
// The absence of a mask preserves legacy "all connected" behavior.
export function selectedConveyorSlots(project, index) {
  chunkWindow(project, index);
  return project.conveyor_reference_slots?.[String(index)] ?? null;
}
export function setConveyorSlotEnabled(project, index, slot, enabled, connected = []) {
  chunkWindow(project, index);
  if (!Number.isInteger(slot) || slot < 1 || slot > 8)
    throw new Error("Invalid Conveyor reference socket");
  if (!connected.includes(slot))
    throw new Error("Connect this reference output to the Timeline Sampler first");
  const explicit = selectedConveyorSlots(project, index);
  const selection = new Set(explicit === null ? connected : explicit);
  if (enabled) selection.add(slot);
  else selection.delete(slot);
  project.conveyor_reference_slots ||= {};
  project.conveyor_reference_slots[String(index)] = [...selection].sort((a,b)=>a-b);
}
export function inheritConveyorSlots(project, index) {
  chunkWindow(project, index);
  delete project.conveyor_reference_slots?.[String(index)];
  if (project.conveyor_reference_slots &&
      !Object.keys(project.conveyor_reference_slots).length)
    delete project.conveyor_reference_slots;
}

export function setChunkPrompt(project, index, text) {
  const [start, end] = chunkWindow(project, index);
  const track = lane(project, "prompt", "Chunk prompts");
  let clip = track.clips.find((c) =>
    c.chunk_prompt === true && c.start === start && c.end === end);
  if (!String(text).trim()) {
    if (clip) track.clips = track.clips.filter((c) => c !== clip);
    return null;
  }
  if (!clip) {
    clip = {
      id: uid(), start, end, chunk_prompt: true, chunk_index: index, source_in: "0",
      speed: "1", padding: "error", audio_role: "neither",
      override: true, text: "",
    };
    track.clips.push(clip);
  }
  clip.text = String(text);
  return clip;
}
export function addChunkMedia(project, index, role, assetId, audioRole = "guide") {
  if (!["image_reference", "video_guide", "audio"].includes(role))
    throw new Error("Chunk slots accept reference images, video guidance or audio");
  const asset = project.assets.find((a) => a.id === assetId);
  if (!asset) throw new Error("Select a registered source asset");
  if (role === "image_reference" && asset.kind !== "image")
    throw new Error("Reference image slots require a still image");
  if (role === "video_guide" && !asset.video)
    throw new Error("Video guidance requires video footage");
  if (role === "audio" && !asset.audio)
    throw new Error("Audio slots require an audio stream");
  if (role === "audio" && !["guide", "final", "reference", "both"].includes(audioRole))
    throw new Error("Invalid audio conditioning role");
  const [start, end] = chunkWindow(project, index);
  if (role === "image_reference") {
    const active = overlappingChunkClips(project, index, ["image_reference"]);
    if (active.some(({ clip }) => clip.asset_id === assetId))
      throw new Error("This reference image is already active in the chunk");
    if (new Set(active.map(({ clip }) => clip.asset_id)).size >= 8)
      throw new Error("H3 supports at most eight active images including persistent references");
  }
  const track = lane(project, role, {
    image_reference: "Chunk reference images",
    video_guide: "Chunk video guidance",
    audio: "Chunk audio",
  }[role]);
  const clip = placeAsset(project, asset, track, start, end - start);
  // A video cut from retained footage must start at that source's actual PTS,
  // including late cuts in a multi-hour source. An unrelated asset starts at
  // its own stream origin and can be trimmed directly in the chunk card.
  const mappedSource = overlappingChunkClips(project, index,
    ["retained","audio","video_guide"])
    .find((row) => row.clip !== clip && row.clip.asset_id === assetId &&
      row.start <= start);
  if (mappedSource)
    clip.source_in = String(sourceAt(mappedSource.clip, start));
  clip.end = end;
  clip.chunk_workspace = true;
  clip.chunk_index = index;
  clip.audio_role = role === "audio" ? audioRole : "neither";
  clip.padding = role === "audio" ? "silence" : "hold";
  // Preserve existing lane content; overlapping guides follow top-track
  // precedence in the native compositor. The new authored lane is topmost.
  if (role === "video_guide") {
    project.tracks.splice(project.tracks.indexOf(track), 1);
    project.tracks.push(track);
  }
  return clip;
}
export function updateChunkSource(project, clipId, sourceIn) {
  const raw = String(sourceIn).trim();
  // The backend accepts exact rational PTS. Preserve that precision, rather
  // than rejecting its legal "numerator/denominator" representation.
  if (!/^-?\d+(?:\.\d+)?(?:\/-?\d+(?:\.\d+)?)?$/.test(raw))
    throw new Error("Invalid source timestamp");
  const value = ratio(raw);
  if (!Number.isFinite(value) || value < 0)
    throw new Error("Invalid source timestamp");
  const clip = project.tracks.flatMap((t) => t.clips).find((c) => c.id === clipId);
  if (!clip) throw new Error("Chunk clip no longer exists");
  clip.source_in = raw;
}
export function removeChunkClip(project, clipId) {
  const track = project.tracks.find((t) =>
    t.clips.some((c) => c.id === clipId));
  if (!track) throw new Error("Chunk clip no longer exists");
  track.clips = track.clips.filter((c) => c.id !== clipId);
}
