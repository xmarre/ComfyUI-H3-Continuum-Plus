// Chunk-focused authoring controls. The timeline project stays the single
// source of truth for H3 causal inputs; sampler-node overrides are inspectable.
import {
  selectedChunkPage, overlappingChunkClips, setChunkPrompt,
  addChunkMedia, updateChunkSource, removeChunkClip,
  selectedConveyorSlots, setConveyorSlotEnabled, inheritConveyorSlots,
} from "./chunk_workspace.js";
import { changeTiming, selectionChunks } from "./sampler_layout.js";
import { sourceAt } from "./core.js";
import { traceReferenceSocket, collectReferenceProvenance, conveyorState, shelfImagePath, conveyorShelfAvailable } from "./conveyor_source.js";

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
function conveyorSourcePreviews(editor,graph,sampler,slot) {
  const ancestry=collectReferenceProvenance(graph,sampler,slot);
  const pixelRatio=typeof devicePixelRatio==="number" ? devicePixelRatio : 1;
  const size=Math.max(160,Math.min(512,
    Math.ceil((editor.layout?.thumb || 100)*pixelRatio)));
  return {
    candidates:ancestry.images.map(({node,shelfNumber,via})=>{
      const state=conveyorState(node);
      const ref=state.reference_slots?.[shelfNumber-1];
      const path=shelfImagePath(state,shelfNumber);
      const params=path ? new URLSearchParams({
        relative_path:path,size:String(size),
        ...((ref?.source_version||ref?.mtime_ns)
          ? {v:String(ref.source_version||ref.mtime_ns)} : {}),
      }).toString() : null;
      const uri=params ? "/image-conveyor/thumbnail?"+params : null;
      const thumbnail=uri && typeof editor.api?.apiURL==="function"
        ? editor.api.apiURL(uri) : uri;
      return {
        shelfNumber,nodeId:node.id,thumbnail,
        filename:ref?.filename||path?.split("/").at(-1)||null,
        label:"Conveyor #"+node.id+" · Shelf "+shelfNumber,
        via,
        state:state.output_mode||"unknown",
        active:conveyorShelfAvailable(node,state,shelfNumber),
      };
    }),
    truncated:ancestry.truncated,
  };
}
export function referenceSocketSources(editor) {
  const sampler=linkedSampler(editor);
  if (!sampler) return [];
  const graph=sampler.graph || editor.app?.graph;
  return Array.from({length:8},(_,i)=>{
    const slot=i+1;
    const source=traceReferenceSocket(graph,sampler,slot);
    // A processor with multiple IMAGE inputs is not a one-to-one mapping.
    // Still show definite upstream Conveyor images instead of an empty card.
    const ancestry=source.connected && !source.conveyor
      ? conveyorSourcePreviews(editor,graph,sampler,slot)
      : {candidates:[],truncated:false};
    const node=source.node;
    const state=source.conveyor ? conveyorState(node) : {};
    const shelfNumber=source.shelfNumber;
    const ref=shelfNumber ? state.reference_slots?.[shelfNumber-1] : null;
    const path=shelfImagePath(state,shelfNumber);
    const persistent=state.output_mode==="persistent_refs";
    const active=source.conveyor &&
      conveyorShelfAvailable(node,state,shelfNumber);
    const disabled=source.conveyor && persistent && shelfNumber &&
      !active && !!ref;
    const inactive=source.conveyor && persistent && shelfNumber &&
      !active;
    const dynamic=source.conveyor && !persistent;
    const filename=ref?.filename || path?.split("/").at(-1);
    // Request enough pixels for the resized thumbnail (and high-DPI
    // displays) while respecting Image Conveyor's 64–512 px server limit.
    const pixelRatio=typeof devicePixelRatio==="number" ? devicePixelRatio : 1;
    const pixels=Math.max(160,Math.min(512,
      Math.ceil((editor.layout?.thumb || 100)*pixelRatio)));
    const thumbnailPath=path ? "/image-conveyor/thumbnail?"+
      new URLSearchParams({
        relative_path:path,size:String(pixels),
        ...((ref?.source_version || ref?.mtime_ns)
          ? {v:String(ref.source_version || ref.mtime_ns)} : {}),
      }).toString() : null;
    // ComfyUI may serve under a URL prefix; use the same API router as the
    // Conveyor's own thumbnail widgets instead of a hardcoded origin root.
    const src=thumbnailPath && typeof editor.api?.apiURL==="function"
      ? editor.api.apiURL(thumbnailPath) : thumbnailPath;
    const direct=source.conveyor ? "Image Conveyor #"+node.id :
      (node?.title || node?.comfyClass || "Connected IMAGE");
    const via=source.via?.length ? " via "+source.via.join(" → ") : "";
    return {
      slot,connected:source.connected,conveyor:source.conveyor,
      shelfNumber,nodeId:node?.id ?? null,dynamic:!!dynamic,
      label:direct+
        (shelfNumber ? (persistent ? " · Shelf "+shelfNumber :
          " · queue-group image "+(shelfNumber+1)) : "")+
        (filename ? " · "+filename : "")+via,
      unavailable:!!inactive,
      reason:disabled ? "Disabled in Image Conveyor" :
        inactive ? "Shelf empty" :
        source.unresolved || (source.conveyor && !shelfNumber
          ? "Not a reference-image output" :
          source.conveyor && persistent && !path
            ? "Shelf image has no resolvable input-file path" : null),
      thumbnail:src,
      previewNote:via ? "Conveyor source image; processed by "+source.via.join(" → ") :
        "Conveyor source image",
      sourceVersion:ref?.source_version || ref?.mtime_ns || null,
      candidatePreviews:ancestry.candidates,
      ancestryTruncated:ancestry.truncated,
    };
  });
}

// One selectable tile per *currently active* persistent reference output,
// never eight copies of the complete upstream shelf. Selection IDs remain
// physical Timeline Sampler reference_image_N sockets. For opaque multi-input
// processors, the same ordinal Conveyor shelf image is a source illustration,
// not a claim that its pixels equal the post-processor output.
export function activeReferenceSources(sources) {
  const cards=[];
  for (const source of sources) {
    if (!source.connected) continue;
    if (source.conveyor) {
      if(!source.unavailable && !source.dynamic)
        cards.push({...source,inferred:false});
      continue;
    }
    // The previous implementation rendered *all* 8 reachable images under
    // every output. Match at most the shelf ordinal for this sampler socket.
    const matches=(source.candidatePreviews||[]).filter(candidate=>
      candidate.shelfNumber===source.slot && candidate.active);
    const uniqueNodes=new Set(matches.map(candidate=>String(candidate.nodeId)));
    if(matches.length===1 && uniqueNodes.size===1) {
      const candidate=matches[0];
      cards.push({
        ...source,
        thumbnail:candidate.thumbnail,
        label:(candidate.filename||candidate.label)+
          " · Conveyor shelf "+candidate.shelfNumber,
        previewNote:"Original Conveyor image before Media Resolution",
        inferred:true,
      });
    } else if(matches.length>1) {
      // Multiple active Conveyors claim the same ordinal: never pretend one
      // is the image associated with this sampler socket.
      cards.push({...source,thumbnail:null,candidatePreviews:[],
        label:"Reference socket "+source.slot+
          " · multiple Conveyor sources (mapping unresolved)",inferred:true});
    }
  }
  return cards;
}

export function samplerInputDescription(editor) {
  const sampler = linkedSampler(editor);
  if (!sampler) return {
    sampler:null, project:editor.p,connectedReferenceSlots:[],
    persistentPictureSockets:0,
    warning:"Connect exactly one Continuum Timeline Sampler to this Studio",
  };
  const widget = (name) => sampler.widgets?.find((w) => w.name === name)?.value;
  const linked = (name) => nodeInput(sampler, name).linked;
  const connectedReferenceSlots = Array.from({length:8},(_,i)=>i+1)
    .filter((slot)=>linked("reference_image_"+slot));
  const pinned = connectedReferenceSlots.map((slot)=>"reference_image_"+slot);
  const persistent = [
    ...pinned,
    ...["reference_video_1","driving_audio","first_frame","last_frame"]
      .filter(linked),
  ].map((name)=>name+" ← "+nodeInput(sampler,name).from);
  const supplied = ["model","clip","video_vae","audio_vae","sampler","sigmas"]
    .map((name)=>[name,nodeInput(sampler,name)])
    .map(([name,input])=>name+": "+
      (input.linked ? input.from : "not connected"));
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
    persistent, supplied, overrides, persistentPictureSockets: pinned.length,
    connectedReferenceSlots, project: editor.p,
    promptOverridden: externalPrompt,
    audioOverridden: linked("driving_audio"),
    settings: [
      ["Chunks",String(selectionChunks(editor.p))],
      ["Chunk duration", (editor.p.chunk_frames/24).toFixed(3)+"s ("+editor.p.chunk_frames+"f)"],
      ["Output dimensions",
        ["width","height"].map((name)=>linked(name)
          ? name+": connected" : name+": "+(widget(name) ?? editor.p[name])).join(" · ")],
      ["Continuity",String(widget("continuity") ?? "sampler default")],
      ["Seed",String(widget("base_seed") ?? "sampler default")],
      ["Continuation method",String(widget("continuation_method") ?? "sampler default")],
      ["Audio continuity",String(widget("audio_continuity") ?? "sampler default")],
      ["Reference size",String(widget("reference_size") ?? "sampler default")],
      ["Video reference size",String(widget("video_reference_size") ?? "sampler default")],
      ["Run storage",String(widget("run_storage") ?? "sampler default")],
      ["Run name",String(widget("run_name") ?? "") || "(automatic)"],
      ["Prompt mode",String(widget("prompt_mode") ?? "Auto")],
      ["Regenerate from",String(widget("regenerate_from") ?? "Auto")],
      ["Reroll nonce",String(widget("reroll_nonce") ?? 0)],
      ["Conditioning lookahead",String(widget("conditioning_lookahead") ?? "node default")],
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
function conveyorReferenceControls(editor, index, sources) {
  const group=el("div",{class:"h3ts-conveyor-refs"});
  const selected=selectedConveyorSlots(editor.p,index);
  const connected=sources.filter((s)=>s.connected);
  group.append(el("strong",{text:"Image Conveyor references · "+
    (selected===null ? "inherit connected" : "chunk-specific selection")}));
  group.append(el("small",{text:"Checks select actual sampler reference IMAGE sockets for this chunk; empty/disabled Conveyor outputs provide no image. <Picture N> numbers are assigned contiguously after live inputs resolve."}));
  if (!connected.length)
    group.append(el("p",{class:"h3ts-chunk-warning",
      text:"Connect Image Conveyor ref_image_1…8 outputs to the corresponding Continuum Timeline Sampler reference_image_1…8 inputs."}));
  let picture=0;
  const allConveyor=connected.every((source)=>source.conveyor);
  for(const source of connected) {
    const checked=selected===null || selected.includes(source.slot);
    const expectedPicture = allConveyor && checked && !source.unavailable &&
      !source.dynamic ? ++picture : null;
    const toggle=el("input",{
      type:"checkbox",
      "aria-label":"Chunk "+(index+1)+" reference input "+source.slot,
    });
    toggle.checked=checked;
    toggle.addEventListener("change",()=>editor.guard(()=>
      editor.editChunkReferences((next)=>
        setConveyorSlotEnabled(next,index,source.slot,toggle.checked,
          connected.map((x)=>x.slot)))));
    const label=el("label",{class:"h3ts-conveyor-ref-label"},
      toggle,
      el("span",{text:"Ref "+source.slot+" · "+source.label+
        (expectedPicture ? " · expected <Picture "+expectedPicture+">" : "")}),
    );
    const item=el("div",{class:"h3ts-conveyor-ref"},label);
    if(source.thumbnail) {
      const thumb=el("img",{
        class:"h3ts-conveyor-thumb",
        loading:"eager",
        alt:"Image Conveyor reference "+source.slot,
        title:(source.previewNote || "Conveyor source image")+" · click for larger preview",
        src:source.thumbnail,
      });
      const status=el("small",{class:"h3ts-chunk-warning",text:""});
      thumb.addEventListener("error",()=>{
        thumb.hidden=true;
        status.textContent="Thumbnail unavailable · use Conveyor Refresh";
      });
      thumb.addEventListener("click",()=>{
        const overlay=el("dialog",{class:"h3ts-ref-lightbox",
          "aria-label":"Reference image preview"},
          el("header",{},el("strong",{text:source.label}),
            button("Close",()=>overlay.close())),
          el("img",{src:source.thumbnail.replace(/([?&])size=\d+/, "$1size=512"),
            alt:source.label}),
          el("p",{text:source.previewNote || "Source reference preview"}),
        );
        editor.root.append(overlay);
        overlay.addEventListener("close",()=>overlay.remove(),{once:true});
        overlay.showModal();
      });
      item.append(thumb,status);
    } else if (source.candidatePreviews?.length) {
      const candidates=source.candidatePreviews;
      const group=el("div",{class:"h3ts-conveyor-candidate-list"});
      const label=el("small",{class:"h3ts-conveyor-candidate-description",
        text:candidates.length+" connected Conveyor shelf source"+
          (candidates.length===1?"":"s")+
          " · this processed IMAGE output has multiple inputs; its exact"+
          " pixel composition cannot be inferred from the graph"});
      group.append(label);
      const gallery=el("div",{class:"h3ts-conveyor-candidate-gallery"});
      for(const candidate of candidates) {
        const tile=el("div",{class:"h3ts-conveyor-candidate"});
        if(candidate.thumbnail) {
          const image=el("img",{
            loading:"lazy",alt:candidate.label+
              (candidate.filename?" "+candidate.filename:""),
            src:candidate.thumbnail,
            title:"Upstream shelf original, not the processed output",
          });
          image.addEventListener("error",()=>{
            image.hidden=true;
            tile.append(el("small",{text:"Original image unavailable"}));
          });
          image.addEventListener("click",()=>{
            const zoom=el("dialog",{class:"h3ts-ref-lightbox"},
              el("header",{},el("strong",{text:candidate.label}),
                button("Close",()=>zoom.close())),
              el("img",{
                src:candidate.thumbnail.replace(/([?&])size=\d+/,"$1size=512"),
                alt:candidate.label,
              }),
              el("p",{text:"Upstream Conveyor original. The intervening node"+
                " combines or processes IMAGE inputs; this is not a"+
                " representation of the exact output tensor."}));
            editor.root.append(zoom);
            zoom.addEventListener("close",()=>zoom.remove(),{once:true});
            zoom.showModal();
          });
          tile.append(image);
        }
        tile.append(el("small",{text:candidate.label+
          (candidate.filename?" · "+candidate.filename:"")+
          (!candidate.thumbnail?" · no stable shelf file":"")}));
        gallery.append(tile);
      }
      group.append(gallery);
      if(source.ancestryTruncated)group.append(
        el("small",{class:"h3ts-chunk-warning",
          text:"Further upstream branches were truncated for safety"}));
      item.append(group);
    } else if (source.conveyor && source.dynamic) {
      item.append(el("small",{text:"Dynamic image · pixels resolved during queue"}));
    } else if (!source.reason) {
      item.append(el("small",{text:"No source thumbnail available"}));
    }
    if(source.reason)
      item.append(el("small",{class:"h3ts-chunk-warning",
        text:source.unavailable ? "Inactive: "+source.reason :
          source.candidatePreviews?.length
            ? "Processor has multiple IMAGE inputs; showing upstream shelf images"
            : source.reason}));
    group.append(item);
  }
  group.append(button("Inherit connected references",()=>editor.guard(()=>
    editor.editChunkReferences((next)=>
      inheritConveyorSlots(next,index)))));
  return group;
}

function compiledLine(node, window, inputs, ownPrompt = "", chunkIndex = window?.index) {
  if (!window) {
    if (!Number.isInteger(chunkIndex)) {
      node.textContent="Waiting for backend input inspection…";
      return;
    }
    const selection=selectedConveyorSlots(inputs.project,chunkIndex);
    const connected=inputs.connectedReferenceSlots || [];
    const slots=selection===null ? connected :
      selection.filter((slot)=>connected.includes(slot));
    const clips=overlappingChunkClips(inputs.project,chunkIndex);
    const num=(role)=>clips.filter(({track})=>track.role===role).length;
    node.textContent="Authored inputs · prompt: "+
      (ownPrompt ? "chunk-specific" : inputs.promptOverridden
        ? "connected prompt fallback" : "project script")+
      " · Conveyor inputs: "+(slots.join(", ") || "none")+
      " · video: "+num("video_guide")+
      " · audio: "+num("audio")+
      " · waiting for backend compile";
    return;
  }
  const prompt = inputs.promptOverridden
    ? (ownPrompt
      ? "Chunk prompt overrides connected sequence_prompt: "+ownPrompt
      : "Connected sequence_prompt supplies this chunk's prompt (runtime source)")
    : "Compiled prompt: "+(window.prompt || "(empty)");
  const selection = selectedConveyorSlots(inputs.project, window.index);
  const selectedSockets = selection === null
    ? (inputs.connectedReferenceSlots || [])
    : selection.filter((slot)=>(inputs.connectedReferenceSlots || []).includes(slot));
  const possibleImageTotal = selectedSockets.length + window.images.length;
  const imageBudgetWarning = possibleImageTotal > 8
    ? " · WARNING: selected persistent sockets plus scoped images may exceed H3's eight-picture limit"
    : "";
  const warning = (window.warnings?.length
    ? " · "+window.warnings.join(" · ") : "") + imageBudgetWarning;
  node.textContent = prompt +
    " | connected ref sockets selected: "+
      (selectedSockets.join(", ") || "none")+
    " (nonempty tensors resolved at sampling)"+
    ", media-file references: "+window.images.length +
    ", video guides: "+window.video.length +
    ", guide audio: "+(inputs.audioOverridden ? "node driving_audio override" : window.guide.length)+
    ", audio refs: "+window.voice.length +
    ", final audio: "+window.final.length+warning;
}
// Detect Conveyor shelf/preset/toggle changes without rebuilding prompt
// textareas, losing card scroll position, or polling backend media decoders.
export function refreshConveyorControls(editor, force = false) {
  if (!editor.chunkBoard || editor.closed) return;
  const sources=referenceSocketSources(editor);
  const signature=JSON.stringify(sources);
  if (!force && signature===editor.conveyorSignature) return;
  editor.conveyorSignature=signature;
  for (const card of editor.chunkBoard.querySelectorAll("[data-chunk-index]")) {
    const index=Number(card.dataset.chunkIndex);
    const previous=card.querySelector(".h3ts-conveyor-refs");
    if (previous) previous.replaceWith(conveyorReferenceControls(editor,index,sources));
  }
  updateChunkCompiledLines(editor);
}
export function renderChunkWorkspace(editor) {
  const host = editor.chunkBoard;
  if (!host) return;
  const p = editor.p;
  const page = selectedChunkPage(p,editor.chunkPage || 0,8);
  editor.chunkPage = page.page;
  const sampler = samplerInputDescription(editor);
  const referenceSources = referenceSocketSources(editor);
  editor.conveyorSignature=JSON.stringify(referenceSources);
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
  const refreshInputs=button("Refresh Conveyor connections",()=>{
    refreshConveyorControls(editor,true);
  });
  heading.append(refreshInputs);
  for (const [name,title,min,max,step,defaultSize] of [
    ["card","Chunk card minimum width",275,900,25,390],
    ["thumb","Reference thumbnail size",54,260,10,100],
    ["track","Timeline track height",44,160,4,60],
  ]) {
    const value=editor.layout?.[name]??defaultSize;
    const slider=el("input",{
      type:"range",min,max,step,value,
      "aria-label":title,
      title,
    });
    const output=el("output",{text:Math.round(value)+"px"});
    slider.addEventListener("input",()=>{
      editor.updateLayout(name,Number(slider.value),{persist:false});
      output.textContent=editor.layout[name]+"px";
    });
    slider.addEventListener("change",()=>{
      editor.persistView();
      if(name==="thumb")refreshConveyorControls(editor,true);
    });
    heading.append(el("label",{class:"h3ts-chunk-layout-control"},
      el("span",{text:title.split(" ")[0]+
        (name==="thumb"?" thumbs":name==="card"?" width":" height")}),
      slider,output));
  }
  heading.append(button("Reset panel sizes",()=>{
    editor.layout={};
    editor.applyLayout();
    editor.persistView();
    renderChunkWorkspace(editor);
  }));
  const settings=el("details",{class:"h3ts-chunk-settings"});
  settings.open=!!editor.samplerDetailsOpen;
  settings.append(el("summary",{text:sampler.warning ||
    "Connected sampler · "+sampler.connectedReferenceSlots.length+
    " image inputs · "+(p.chunk_frames/24).toFixed(3)+" s/chunk · "+
    (sampler.settings?.find(([name])=>name==="Continuity")?.[1] || "continuity unknown")+
    " · expand for complete settings and overrides"}));
  const details=el("div",{class:"h3ts-chunk-settings-body"});
  if (sampler.warning) details.append(el("p",{text:sampler.warning}));
  else {
    details.append(el("p",{text:sampler.settings.map(([k,v])=>k+": "+v).join("  |  ")}));
    details.append(el("p",{text:"Native model inputs: "+sampler.supplied.join(" · ")}));
    details.append(el("p",{text:"Persistent conditioning sockets: "+
      (sampler.persistent.join(" · ") || "none connected")}));
    if (sampler.overrides.length) details.append(
      el("p",{class:"h3ts-chunk-warning",text:"OVERRIDES: "+sampler.overrides.join(" · ")}));
  }
  settings.append(details);
  settings.addEventListener("toggle",()=>{editor.samplerDetailsOpen=settings.open;});
  host.append(settings);
  const pager = el("div",{class:"h3ts-chunk-pager"},
    button("Previous chunks",()=>{editor.chunkPage--;renderChunkWorkspace(editor);editor.scheduleChunkCompile();}),
    el("span",{text:"Page "+(page.page+1)+" / "+page.pages+
      " · select a chunk to seek or fill its input slots"}),
    button("Next chunks",()=>{editor.chunkPage++;renderChunkWorkspace(editor);editor.scheduleChunkCompile();}));
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
    const selectionOrdinal = chunk.index -
      Math.floor(p.selection[0] / p.chunk_frames) + 1;
    card.append(el("h4",{},button(
      "CHUNK "+selectionOrdinal+"/"+page.total+" · global #"+(chunk.index+1),
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
      ownPrompt?.clip.text || "", chunk.index);
    card.append(preview);
    card.append(conveyorReferenceControls(editor,chunk.index,referenceSources));
    const video=sectionForRole(editor,chunk.index,"video_guide");
    const audioRoute=el("select",{"aria-label":"Chunk audio routing"},
      ...["guide","reference","final","both"].map((v)=>
        el("option",{value:v,text:v})));
    audioRoute.value="guide";
    const audio=sectionForRole(editor,chunk.index,"audio",audioRoute);
    card.append(control("Add cut video guidance",video),
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
      if (asset?.video && ["image_reference","video_guide"].includes(track.role))
        row.prepend(el("img",{
          class:"h3ts-chunk-asset-preview",
          loading:"lazy",alt:asset.alias || asset.name,
          src:"/h3_continuum/timeline/thumbnail/"+asset.id+"?pts="+
            sourceAt(clip,start).toFixed(6),
        }));
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
      const files=Array.from(e.dataTransfer?.files || []);
      if (!assetId && !files.length) return;
      e.preventDefault();e.stopPropagation();
      editor.guard(async ()=>{
        // Native linking hashes the original. Keep large Explorer drops
        // sequential so we do not saturate WSL disk I/O with movie hashes.
        const assets=[];
        if (assetId) assets.push(editor.p.assets.find((a)=>a.id===assetId));
        else for (const file of files)
          assets.push(await editor.resolveOriginal(file));
        for (const asset of assets) {
          if (!asset) throw new Error("Chunk drop referenced missing media");
          if (asset.kind==="image")
            throw new Error(
              "Chunk references must come from connected Image Conveyor shelf outputs; select their checkboxes in this chunk."
            );
          const role=asset.video ? "video_guide" : "audio";
          editor.edit((next)=>{
            if (!next.assets.some((a)=>a.id===asset.id)) next.assets.push(asset);
            const clip=addChunkMedia(next,chunk.index,role,asset.id);
            editor.selected=[clip.id];
          });
        }
        editor.message("Registered original media linked directly to H3 chunk "+
          (chunk.index+1)+" without an intermediate export.");
      });
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
      Number(element.dataset.compiledChunk),
    );
}
