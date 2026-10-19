// Read-only graph inspection. Never alters IMAGE tensor paths or executes a node.
// Conservative lineage: an IMAGE connection may pass through media-resolution,
// reroute or pass-through nodes whose LiteGraph sockets use wildcard types.
const CONVEYOR = new Set(["ImageConveyor", "SequentialBatchImageLoader"]);
const kind = (node) => String(node?.comfyClass || node?.type || "");
const nameFor = (node) => String(node?.title || kind(node) || "IMAGE processor");
function linkAt(graph, id) {
  if (id == null) return null;
  return graph?.links?.[id] ?? graph?.links?.get?.(id) ?? null;
}
function nodeAt(graph, id) {
  return graph?.getNodeById?.(id) ||
    graph?._nodes?.find((node) => node.id === id) || null;
}
function isImageType(value) {
  // IMAGE / IMAGE? / IMAGE,BATCH, but not masks/latents.
  return /(?:^|[,| ])IMAGE(?:$|[?,| ])/i.test(String(value ?? ""));
}
function isImageName(value) {
  return /(?:^|[_ ])(?:image|images|pixels|reference)(?:$|[_ ])/i.test(
    String(value ?? "")) &&
    !/(?:mask|latent|width|height|size|resolution)/i.test(String(value));
}
function referenceOutput(node, index) {
  const name=String(node.outputs?.[index]?.name || node.outputs?.[index]?.label || "");
  const match=/^ref_image_([1-8])$/.exec(name);
  // ImageConveyor.RETURN_NAMES fixes ref_image_1..8 at output slots 6..13.
  // LiteGraph versions sometimes omit custom output names on serialized
  // nodes; only use the index fallback when the class is known exactly.
  const ordinal=match ? +match[1] :
    kind(node)==="ImageConveyor" && index>=6 && index<=13 ? index-5 : null;
  return {ordinal,name};
}
function candidates(graph, node) {
  const linked=(node.inputs || []).filter(input=>linkAt(graph,input.link));
  if (!linked.length) return [];
  const image=linked.filter(input=>{
    const link=linkAt(graph,input.link);
    const source=nodeAt(graph,link.origin_id);
    const output=source?.outputs?.[link.origin_slot];
    return isImageType(input.type) ||
      isImageType(link?.type) ||
      isImageType(output?.type) ||
      isImageName(input.name);
  });
  // Do not attribute a mixed IMAGE output to just one upstream source.
  if (image.length>1) return null;
  if (image.length===1) return image;
  // Wildcard/untyped pass-throughs often have only one linked input.
  if (linked.length===1) return linked;
  // Multiple links with no type information: only trace if all but one are
  // demonstrably scalar or non-image links.
  const possible=linked.filter(input=>{
    const link=linkAt(graph,input.link);
    const source=nodeAt(graph,link.origin_id);
    const output=source?.outputs?.[link.origin_slot];
    const type=String(input.type || link?.type || output?.type || "");
    return !type || type==="*" || isImageType(type);
  });
  return possible.length===1 ? possible : null;
}
export function traceReferenceSocket(graph, sampler, slot) {
  const input=sampler?.inputs?.find(i=>i.name==="reference_image_"+slot);
  let link=linkAt(graph,input?.link);
  const seen=new Set(),via=[];
  let lastNode=null;
  for(let hop=0;link && hop<24;hop++){
    const node=nodeAt(graph,link.origin_id);
    if (!node || seen.has(node.id)) break;
    seen.add(node.id);
    lastNode=node;
    if(CONVEYOR.has(kind(node))) {
      const {ordinal,name}=referenceOutput(node,link.origin_slot);
      if (ordinal!==null) return {
        connected:true,conveyor:true,node,shelfNumber:ordinal,
        outputName:name,via,
      };
      return {connected:true,conveyor:false,node,via,
        unresolved:"Upstream Conveyor output is not a reference image socket"};
    }
    const next=candidates(graph,node);
    if (!next || next.length!==1) {
      return {connected:true,conveyor:false,node,via,
        unresolved:!next ? "Ambiguous IMAGE inputs in "+nameFor(node)
          : "No identifiable upstream IMAGE input in "+nameFor(node)};
    }
    via.push(nameFor(node));
    link=linkAt(graph,next[0].link);
  }
  return {
    connected:input?.link != null,conveyor:false,node:lastNode,via,
    unresolved:"Image source unavailable or cyclic graph path",
  };
}
export function conveyorState(node) {
  const live=node?.__bil?.state;
  if (live && typeof live==="object") return live;
  try {
    return JSON.parse(node?.widgets?.find(w=>w.name==="state_json")?.value || "{}");
  } catch { return {}; }
}
export function shelfImagePath(state, slot) {
  if (!Number.isInteger(slot) || slot<1 || slot>8 ||
      state?.output_mode!=="persistent_refs") return null;
  const ref=state?.reference_slots?.[slot-1];
  if (!ref || String(ref.type || "input")!=="input") return null;
  const annotated=String(ref.annotated || "");
  const source=annotated.endsWith(" [input]") ? annotated.slice(0,-8) :
    String(ref.relative_path || (
      ref.filename ?
        (ref.subfolder ? ref.subfolder+"/" : "")+ref.filename : ""));
  const relative=source.trim().replace(/\\/g,"/");
  if (!relative || relative.startsWith("/") ||
      /^[a-zA-Z]:/.test(relative) ||
      relative.split("/").some(part=>!part || part==="." || part===".."))
    return null;
  return relative;
}
