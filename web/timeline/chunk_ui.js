// Chunk-focused authoring controls. The timeline project stays the single
// source of truth for H3 causal inputs; sampler-node overrides are inspectable.
import {
  selectedChunkPage, overlappingChunkClips, setChunkPrompt,
  addChunkMedia, updateChunkSource, removeChunkClip,
} from "./chunk_workspace.js";
import { changeTiming, selectionChunks } from "./sampler_layout.js";
import { sourceAt } from "./core.js";

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, String(value));
  }
  node.append(...children.filter((v) => v != null));
  return node;
}
function button(text, action, title = text) {
  const node = el("button", {type:"button",text,title});
  node.addEventListener("click", action);
  return node;
}
function frameTime(f) {
  const seconds = f / 24;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor(seconds / 60) % 60;
  const s = Math.floor(seconds) % 60;
  const ff = f % 24;
  return [h,m,s,ff].map((n)=>String(n).padStart(2,"0")).join(":");
}
function nodeInput(sampler, name) {
  const input = sampler?.inputs?.find((i) => i.name === name);
  const graphLink = sampler?.graph?.links?.[input?.link];
  const upstream = graphLink && sampler.graph.getNodeById?.(graphLink.origin_id);
  return { linked: !!graphLink, from: upstream?.title || upstream?.comfyClass ||
    (graphLink ? "connected node" : null) };
}
export function linkedSampler(editor) {
  const samplers = (editor.app?.graph?._nodes || []).filter((n) => {
    if (n.comfyClass !== "H3ContinuumTimelineSampler") return false;
    const link = n.graph?.links?.[
      n.inputs?.find((i) => i.name === "timeline")?.link
    ];
    return link?.origin_id === editor.node.id;
  });
  return samplers.length === 1 ? samplers[0] : null;
}
export function samplerInputDescription(editor) {
  const sampler = linkedSampler(editor);
  if (!sampler) return {sampler:null,warning:"Connect exactly one Continuum Timeline Sampler to this Studio"};
  const widget = (name) => sampler.widgets?.find((w) => w.name === name)?.value;
  const linked = (name) => nodeInput(sampler, name).linked;
  const pinned = Array.from({length:8},(_,i)=>"reference_image_"+(i+1))
    .filter(linked);
  const persistent = [
    ...pinned.map((x)=>"Reference "+x.slice(-1)),
    ...["reference_video_1","driving_audio","first_frame","last_frame"]
      .filter(linked),
  ];
  const overrides = [];
  const externalPrompt = linked("sequence_prompt") ||
    (typeof widget("sequence_prompt") === "string" &&
      !!widget("sequence_prompt").trim());
  if (externalPrompt) overrides.push("sequence_prompt supplies inherited prompts; authored chunk prompts override it inside their own spans");
  if (linked("driving_audio")) overrides.push("driving_audio overrides timeline guide audio");
  if (linked("reference_video_1")) overrides.push("persistent video reference applies where no scoped guide");
  if (linked("width")) overrides.push("width comes from a connected node");
  if (linked("height")) overrides.push("height comes from a connected node");
  return {
    sampler,
    persistent, overrides, promptOverridden: externalPrompt,
    audioOverridden: linked("driving_audio"),
    settings: [
      ["Chunks",String(selectionChunks(editor.p))],
      ["Chunk duration", (editor.p.chunk_frames/24).toFixed(3)+"s ("+editor.p.chunk_frames+"f)"],
      ["Output dimensions",
        ["width","height"].map((name)=>linked(name)
          ? name+": connected" : name+": "+(widget(name) ?? editor.p[name])).join(" · ")],
      ["Continuity",String(widget("continuity") ?? "sampler default")],
      ["Seed",String(widget("base_seed") ?? "sampler default")],
      ["Reference size",String(widget("reference_size") ?? "sampler default")],
      ["Video reference size",String(widget("video_reference_size") ?? "sampler default")],
      ["Run storage",String(widget("run_storage") ?? "sampler default")],
    ],
  };
}
const roleName = {
  image_reference: "Reference image",
  video_guide: "H3 video guide",
  audio: "Audio",
  retained: "Original footage",
  generated: "Accepted take",
  prompt: "Prompt region",
};
function control(label, input) {
  return el("label",{class:"h3ts-chunk-control"},el("span",{text:label}),input);
}
function pickAsset(editor, role, onPick) {
  const assets = editor.p.assets.filter((a) =>
    role === "image_reference" ? a.kind === "image"
      : role === "video_guide" ? !!a.video : !!a.audio);
  const select = el("select",{"aria-label":"Choose "+role},
    el("option",{value:"",text:assets.length ? "Choose imported media…" : "Import media first"}));
  for (const a of assets) select.append(
    el("option",{value:a.id,text:a.alias || a.name}));
  const buttonAdd = button("Add",() => {
    if (!select.value) return;
    editor.guard(()=>onPick(select.value));
  });
  if (!assets.length) buttonAdd.disabled = true;
  return {select,buttonAdd};
}
function sectionForRole(editor, index, role, audioRoute) {
  const row = el("div",{class:"h3ts-chunk-add"});
  const choose = pickAsset(editor,role,(assetId)=>{
    let created;
    editor.edit((p)=>{
      created=addChunkMedia(p,index,role,assetId,audioRoute?.value || "guide");
      editor.selected=[created.id];
    });
  });
  row.append(choose.select);
  if (audioRoute) row.append(audioRoute);
  row.append(choose.buttonAdd);
  return row;
}
function compiledLine(node, window, inputs, ownPrompt = "") {
  if (!window) {
    node.textContent = "Resolving project inputs…";
    return;
  }
  const prompt = inputs.promptOverridden
    ? (ownPrompt
      ? "Chunk prompt overrides connected sequence_prompt: "+ownPrompt
      : "Connected sequence_prompt supplies this chunk's prompt (runtime source)")
    : "Compiled prompt: "+(window.prompt || "(empty)");
  const warning = window.warnings?.length ? " · "+window.warnings.join(" · ") : "";
  node.textContent = prompt +
    " | images: "+window.images.length +
    ", video guides: "+window.video.length +
    ", guide audio: "+(inputs.audioOverridden ? "node driving_audio override" : window.guide.length)+
    ", audio refs: "+window.voice.length +
    ", final audio: "+window.final.length+warning;
}
export function renderChunkWorkspace(editor) {
  const host = editor.chunkBoard;
  if (!host) return;
  const p = editor.p;
  const page = selectedChunkPage(p,editor.chunkPage || 0,8);
  editor.chunkPage = page.page;
  const sampler = samplerInputDescription(editor);
  host.replaceChildren();
  const heading = el("div",{class:"h3ts-chunk-heading"},
    el("strong",{text:"CHUNK INPUTS · THE ACTUAL H3 TIMELINE"}),
    el("span",{text:page.total+" selected chunks · "+
      (p.chunk_frames/24).toFixed(3)+"s per chunk · 24 fps"}),
  );
  const count = el("input",{type:"number",min:"1",max:"65536",value:selectionChunks(p),
    "aria-label":"Selected H3 chunk count"});
  count.addEventListener("change",()=>editor.edit((next)=>
    changeTiming(next,"chunks",Number(count.value))));
  const seconds = el("input",{type:"number",min:"4",max:"15",step:"0.041666667",
    value:(p.chunk_frames/24).toFixed(3),"aria-label":"H3 chunk duration seconds"});
  seconds.addEventListener("change",()=>editor.edit((next)=>
    changeTiming(next,"chunk_seconds",Number(seconds.value))));
  const focus = el("input",{type:"checkbox","aria-label":"Focus on selected H3 chunks"});
  focus.checked = editor.chunkFocus !== false;
  focus.addEventListener("change",()=>{
    editor.chunkFocus = focus.checked;
    editor.scroller.scrollLeft = 0;
    editor.zoom = focus.checked ? Math.max(30,Math.min(160,
      (editor.scroller.clientWidth - 210)/((p.selection[1]-p.selection[0])/24))) : 60;
    editor.persistView();
    editor.render();
  });
  heading.append(
    control("Selected chunks",count),control("Seconds / chunk",seconds),
    control("Chunk-focused timeline",focus));
  host.append(heading);
  const settings = el("div",{class:"h3ts-chunk-settings"});
  settings.append(el("strong",{text:"CONNECTED SAMPLER · EFFECTIVE INPUT SOURCES"}));
  if (sampler.warning) settings.append(el("p",{text:sampler.warning}));
  else {
    settings.append(el("p",{text:sampler.settings.map(([k,v])=>k+": "+v).join("  |  ")}));
    settings.append(el("p",{text:"Persistent node inputs: "+
      (sampler.persistent.join(", ") || "none connected")}));
    if (sampler.overrides.length) settings.append(
      el("p",{class:"h3ts-chunk-warning",text:"OVERRIDES: "+sampler.overrides.join(" · ")}));
  }
  host.append(settings);
  const pager = el("div",{class:"h3ts-chunk-pager"},
    button("Previous chunks",()=>{editor.chunkPage--;renderChunkWorkspace(editor);}),
    el("span",{text:"Page "+(page.page+1)+" / "+page.pages+
      " · select a chunk to seek or fill its input slots"}),
    button("Next chunks",()=>{editor.chunkPage++;renderChunkWorkspace(editor);}));
  pager.firstChild.disabled=page.page===0;
  pager.lastChild.disabled=page.page>=page.pages-1;
  host.append(pager);
  const cards=el("div",{class:"h3ts-chunk-cards"});
  for (const chunk of page.chunks) {
    const items = overlappingChunkClips(p,chunk.index);
    const ownPrompt = items.find(({clip,track})=>
      track.role==="prompt" && clip.chunk_prompt && clip.start===chunk.start &&
      clip.end===chunk.end);
    const card=el("section",{class:"h3ts-chunk-card",
      "data-chunk-index":chunk.index});
    const sub=chunk.selectedStart!==chunk.start || chunk.selectedEnd!==chunk.end
      ? " · selection trims this chunk" : "";
    card.append(el("h4",{},button("CHUNK "+(chunk.index+1),
      ()=>editor.seek(chunk.start)),
      el("small",{text:frameTime(chunk.start)+" — "+frameTime(chunk.end)+
        " · "+(chunk.end-chunk.start)+" frames"+sub})));
    const prompt = el("textarea",{
      "aria-label":"Prompt for chunk "+(chunk.index+1),
      placeholder:"Write prompt for this chunk; replaces inherited script (global prompt remains)",
      rows:"3",
    });
    prompt.value=ownPrompt?.clip.text || "";
    prompt.addEventListener("change",()=>editor.edit((next)=>
      setChunkPrompt(next,chunk.index,prompt.value)));
    card.append(control("Per-chunk H3 prompt",prompt));
    const preview=el("div",{class:"h3ts-chunk-compiled",
      "data-compiled-chunk":chunk.index});
    compiledLine(preview,editor.compiledWindows?.get(chunk.index),sampler,
      ownPrompt?.clip.text || "");
    card.append(preview);
    const picture=sectionForRole(editor,chunk.index,"image_reference");
    const video=sectionForRole(editor,chunk.index,"video_guide");
    const audioRoute=el("select",{"aria-label":"Chunk audio routing"},
      ...["guide","reference","final","both"].map((v)=>
        el("option",{value:v,text:v})));
    audioRoute.value="guide";
    const audio=sectionForRole(editor,chunk.index,"audio",audioRoute);
    card.append(control("Add reference image",picture),
      control("Add cut video guidance",video),
      control("Add cut audio (guide / reference / final)",audio));
    for (const {track,clip,start,end} of items) {
      if (track.role==="prompt" && clip.chunk_prompt) continue;
      const asset=p.assets.find((a)=>a.id===clip.asset_id);
      const isEditable=!!clip.chunk_workspace &&
        clip.start===chunk.start && clip.end===chunk.end;
      const row=el("div",{class:"h3ts-chunk-assigned"},
        el("span",{text:(roleName[track.role] || track.role) +
          (track.role==="audio" ? " ["+clip.audio_role+"]" : "")+
          ": "+(asset?.alias || asset?.name || clip.text || "source")+
          " · "+(end-start)+"f"}));
      if (isEditable && track.role!=="image_reference") {
        const sourceIn=el("input",{type:"text",value:clip.source_in,
          "aria-label":"Source PTS for chunk "+(chunk.index+1)});
        sourceIn.addEventListener("change",()=>editor.guard(()=>
          editor.edit((next)=>updateChunkSource(next,clip.id,sourceIn.value))));
        row.append(control("Source PTS (s)",sourceIn));
        row.append(el("small",{text:"→ "+sourceAt(clip,clip.end).toFixed(3)+"s"}));
      }
      row.append(isEditable
        ? button("Remove",()=>editor.edit((next)=>removeChunkClip(next,clip.id)))
        : button("Inspect",()=>{
          editor.selected=[clip.id];
          editor.seek(Math.max(clip.start,chunk.start));
          editor.renderInspector();
        }));
      card.append(row);
    }
    card.addEventListener("dragover",(e)=>e.preventDefault());
    card.addEventListener("drop",(e)=>{
      const assetId=e.dataTransfer?.getData("application/x-h3-asset");
      if (!assetId) return;
      e.preventDefault();e.stopPropagation();
      const asset=editor.p.assets.find((a)=>a.id===assetId);
      const role=asset?.kind==="image" ? "image_reference"
        : asset?.video ? "video_guide" : "audio";
      editor.guard(()=>editor.edit((next)=>
        addChunkMedia(next,chunk.index,role,assetId)));
    });
    cards.append(card);
  }
  host.append(cards);
}
// Only repaint derived output lines; do not tear down a textarea while
// the user is typing a new chunk prompt.
export function updateChunkCompiledLines(editor) {
  const inputs=samplerInputDescription(editor);
  for (const element of editor.chunkBoard?.querySelectorAll("[data-compiled-chunk]") || [])
    compiledLine(
      element,
      editor.compiledWindows?.get(Number(element.dataset.compiledChunk)),
      inputs,
      overlappingChunkClips(editor.p,Number(element.dataset.compiledChunk),["prompt"])
        .find(({clip}) => clip.chunk_prompt)?.clip.text || "",
    );
}
