// Timeline uses V3.4's visual order. Persisted values are migrated by name;
// reordering connected sockets also updates LiteGraph's link slot indices.
export const SAMPLER_INPUT_ORDER = [
  "timeline",
  "model",
  "clip",
  "video_vae",
  "sampler",
  "sigmas",
  "sequence_prompt",
  "first_frame",
  "last_frame",
  ...Array.from({ length: 8 }, (_, i) => `reference_image_${i + 1}`),
  "reference_video_1",
  "driving_audio",
  "audio_vae",
];
export const SAMPLER_WIDGET_ORDER = [
  "prompt_mode",
  "chunks",
  "chunk_seconds",
  "width",
  "height",
  "continuity",
  "base_seed",
  "control_after_generate",
  "audio_continuity",
  "run_storage",
  "regenerate_from",
  "reroll_nonce",
  "run_name",
  "reference_size",
  "video_reference_size",
  "continuation_method",
  "managed_prompt_source_json",
  "conditioning_lookahead",
  "job_index",
  "diagnostics",
  "debug",
  "show_preview",
  "project_id",
  "strict_compatibility",
  "emit_refine_state",
];
const ORIGINAL_WIDGETS = [
  "job_index",
  "base_seed",
  "continuity",
  "continuation_method",
  "regenerate_from",
  "reroll_nonce",
  "emit_refine_state",
];
const RESTORED_WIDGETS = [
  ...ORIGINAL_WIDGETS.slice(0, 2),
  "control_after_generate",
  ...ORIGINAL_WIDGETS.slice(2),
  "audio_continuity",
  "diagnostics",
  "debug",
  "show_preview",
  "run_storage",
  "run_name",
  "reference_size",
  "project_id",
  "video_reference_size",
  "strict_compatibility",
  "managed_prompt_source_json",
  "conditioning_lookahead",
  "prompt_mode",
  "width",
  "height",
];
const TRANSIENT = new Set(["chunks", "chunk_seconds"]);

function ordered(items, names) {
  const ranks = new Map(names.map((name, i) => [name, i]));
  return [...items].sort(
    (a, b) =>
      (ranks.get(a.name) ?? names.length) - (ranks.get(b.name) ?? names.length),
  );
}
export function orderSampler(node) {
  node.inputs = ordered(node.inputs || [], SAMPLER_INPUT_ORDER);
  for (const [slot, input] of node.inputs.entries()) {
    const link = node.graph?.links?.[input.link];
    if (link) link.target_slot = slot;
  }
  node.widgets = ordered(node.widgets || [], SAMPLER_WIDGET_ORDER);
}

export function samplerStudio(node) {
  const input = node.inputs?.find((i) => i.name === "timeline");
  const link = node.graph?.links?.[input?.link];
  const studio = link && node.graph.getNodeById(link.origin_id);
  return studio?.comfyClass === "H3ContinuumTimelineStudio" ? studio : null;
}
export function studioProject(studio) {
  try {
    return JSON.parse(
      studio?.widgets?.find((w) => w.name === "project_json")?.value,
    );
  } catch {
    return null;
  }
}
export function selectionChunks(p) {
  return (
    Math.ceil(p.selection[1] / p.chunk_frames) -
    Math.floor(p.selection[0] / p.chunk_frames)
  );
}
export function changeTiming(p, name, value) {
  const oldSize = p.chunk_frames;
  const oldFirst = Math.floor(p.selection[0] / oldSize);
  const oldCount = selectionChunks(p);
  const count = name === "chunks" ? Math.max(1, Math.round(value)) : oldCount;
  if (name === "chunk_seconds") {
    const nextSize = Math.max(96, Math.min(360, Math.round(value * 24)));
    if (nextSize !== oldSize) {
      // The first visible authored chunk remains near the same movie time;
      // exact H3 chunk boundaries are globally anchored to output frame 0.
      const newFirst = Math.floor(p.selection[0] / nextSize);
      // Preflight collision detection before mutating any clip or selection,
      // so a rejected edit leaves History's project unchanged.
      const previousSelections=p.conveyor_reference_slots;
      let migrated=null;
      if (previousSelections) {
        migrated={};
        for (const [key, selected] of Object.entries(previousSelections)) {
          const oldIndex=Number(key);
          const newIndex=oldIndex>=oldFirst && oldIndex<oldFirst+oldCount
            ? newFirst+oldIndex-oldFirst
            : Math.floor(oldIndex*oldSize/nextSize);
          if (String(newIndex) in migrated)
            throw new Error(
              "Changing chunk duration merges two independently configured "+
              "reference selections. Resolve their overlap before resizing."
            );
          migrated[String(newIndex)]=selected;
        }
      }
      for (const t of p.tracks || [])
        for (const c of t.clips || []) {
          if (!c.chunk_prompt && !c.chunk_workspace) continue;
          const oldIndex = Number.isInteger(c.chunk_index)
            ? c.chunk_index : Math.floor(c.start / oldSize);
          // Only reflow the authored selection. Other project regions may
          // contain accepted takes or deliberately timed source footage; moving
          // their chunk-owned clips as a side effect would corrupt their PTS
          // alignment and invalidate unrelated windows.
          if (oldIndex < oldFirst || oldIndex >= oldFirst + oldCount) continue;
          const newIndex = newFirst + oldIndex - oldFirst;
          c.chunk_index = newIndex;
          c.start = newIndex * nextSize;
          c.end = (newIndex + 1) * nextSize;
          // Preserve authored source_in and speed: only output placement and
          // the source-out inferred from its new duration change.
        }
      if (migrated) p.conveyor_reference_slots=migrated;
      p.chunk_frames = nextSize;
      p.selection[0] = newFirst * nextSize;
    }
  }
  p.selection[1] =
    (Math.floor(p.selection[0] / p.chunk_frames) + count) * p.chunk_frames;
  let last = Math.max(p.duration_frames, p.selection[1]);
  for (const t of p.tracks || [])
    for (const c of t.clips || []) last = Math.max(last, c.end);
  p.duration_frames = last;
}

export function refreshProjectWidgets(node, syncDimensions = false) {
  const p = studioProject(samplerStudio(node));
  if (!p) return;
  const values = {
    chunks: selectionChunks(p),
    chunk_seconds: p.chunk_frames / 24,
  };
  const canvasChanged =
    node.__timelineProjectCanvas &&
    (node.__timelineProjectCanvas[0] !== p.width ||
      node.__timelineProjectCanvas[1] !== p.height);
  if (
    (syncDimensions && canvasChanged) ||
    !node.properties.h3_timeline_dimensions_initialized
  ) {
    for (const name of ["width", "height"])
      if (!node.inputs?.some((i) => i.name === name && i.link != null))
        values[name] = p[name];
    node.properties.h3_timeline_dimensions_initialized = true;
  }
  node.__timelineProjectCanvas = [p.width, p.height];
  for (const [name, value] of Object.entries(values)) {
    const widget = node.widgets?.find((w) => w.name === name);
    if (widget) widget.value = value;
  }
}

export function installProjectWidgets(node) {
  if (node.__timelineProjectWidgets) return;
  node.__timelineProjectWidgets = true;
  for (const [name, value, options] of [
    [
      "chunks",
      3,
      {
        min: 1,
        max: 65536,
        step: 10,
        step2: 1,
        precision: 0,
        tooltip:
          "Selected logical chunks in Timeline Studio. Editing this control changes the project's render out; long selections are queued in bounded jobs.",
      },
    ],
    [
      "chunk_seconds",
      7,
      {
        min: 4,
        max: 15,
        step: 1,
        step2: 0.1,
        precision: 3,
        tooltip:
          "Timeline Studio chunk length in seconds, quantized to the 24 fps output frame grid.",
      },
    ],
  ]) {
    const widget = node.addWidget(
      "number",
      name,
      value,
      (next) => {
        samplerStudio(node)?.__timelineEditProject?.((p) =>
          changeTiming(p, name, Number(next)),
        );
        refreshProjectWidgets(node);
      },
      { ...options, serialize: false },
    );
    widget.serialize = false;
  }
  for (const name of ["width", "height"]) {
    const widget = node.widgets?.find((w) => w.name === name);
    if (!widget) continue;
    const callback = widget.callback;
    widget.callback = function (value, ...args) {
      callback?.call(this, value, ...args);
      node.properties.h3_timeline_dimensions_initialized = true;
      samplerStudio(node)?.__timelineEditProject?.((p) => {
        p[name] = Number(value);
      });
    };
  }
  const serialize = node.onSerialize;
  node.onSerialize = function (info) {
    serialize?.apply(this, arguments);
    info.widgets_values_named = Object.fromEntries(
      this.widgets
        .filter((w) => w.serialize !== false)
        .map((w) => [w.name, w.value]),
    );
    info.properties ||= {};
    info.properties.h3_timeline_sampler_layout = 1;
  };
}

export function migrateSamplerWorkflow(graphData, defaults = {}) {
  for (const node of graphData?.nodes || []) {
    if (node.type !== "H3ContinuumTimelineSampler") continue;
    let previous = node.widgets_values_named;
    if (!previous) {
      const values = node.widgets_values || [];
      const names = values.length === 7 ? ORIGINAL_WIDGETS : RESTORED_WIDGETS;
      previous = Object.fromEntries(
        values.map((value, i) => [names[i], value]).filter(([name]) => name),
      );
    }
    const input = node.inputs?.find((i) => i.name === "timeline");
    const link = graphData.links?.find(
      (l) => (Array.isArray(l) ? l[0] : l.id) === input?.link,
    );
    const origin = link && (Array.isArray(link) ? link[1] : link.origin_id);
    const studio = graphData.nodes.find((n) => n.id === origin);
    let p;
    try {
      p = JSON.parse(
        studio?.widgets_values_named?.project_json ??
          studio?.widgets_values?.[0],
      );
    } catch {}
    const values = {
      control_after_generate: "fixed",
      ...defaults,
      width: p?.width ?? defaults.width,
      height: p?.height ?? defaults.height,
      ...previous,
    };
    node.widgets_values_named = Object.fromEntries(
      Object.entries(values).filter(([name]) => !TRANSIENT.has(name)),
    );
    node.widgets_values = SAMPLER_WIDGET_ORDER.filter(
      (name) => name in node.widgets_values_named,
    ).map((name) => node.widgets_values_named[name]);
    node.properties ||= {};
    node.properties.h3_timeline_dimensions_initialized = true;
    node.properties.h3_timeline_sampler_layout = 1;
  }
}
