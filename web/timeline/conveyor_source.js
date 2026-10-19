// Read-only ComfyUI graph traversal for the Image Conveyor shelf shown in
// Timeline Studio. It never changes the connected IMAGE execution graph.
// A source thumbnail is not a rendered preview of intermediate image processors.
const CONVEYOR = new Set(["ImageConveyor", "SequentialBatchImageLoader"]);
const kind = (node) => String(node?.comfyClass || node?.type || "");
function linkAt(graph, id) {
  if (id == null) return null;
  return graph?.links?.[id] ?? graph?.links?.get?.(id) ?? null;
}
function nodeAt(graph, id) {
  return graph?.getNodeById?.(id) ||
    graph?._nodes?.find((node) => node.id === id) || null;
}
function imageInput(node) {
  const connected = (node?.inputs || []).filter((input) =>
    input?.link != null && (input.type === "IMAGE" ||
      (/(?:^|[_ ])image(?:[_ ]|$)/i.test(String(input.name)) &&
       !/mask|latent/i.test(String(input.name)))));
  // Explicit reroute operators can have wildcard inputs. Do not infer
  // a visual source through an arbitrary multi-input image processor.
  if (!connected.length && /(?:^|\.)Reroute$/i.test(kind(node)))
    return (node.inputs || []).filter((input) => input?.link != null).length === 1
      ? (node.inputs || []).find((input) => input?.link != null) : null;
  return connected.length === 1 ? connected[0] : null;
}
export function traceReferenceSocket(graph, sampler, slot) {
  const input = sampler?.inputs?.find((i)=>i.name==="reference_image_"+slot);
  let link = linkAt(graph, input?.link);
  const seen = new Set();
  const via = [];
  for(let hop=0;link && hop<16;hop++) {
    const node=nodeAt(graph,link.origin_id);
    if (!node || seen.has(node.id)) break;
    seen.add(node.id);
    const output=node.outputs?.[link.origin_slot];
    if (CONVEYOR.has(kind(node))) {
      const name=String(output?.name || output?.label || "");
      const match=/^ref_image_([1-8])$/.exec(name);
      return {
        connected:true, conveyor:true, node, shelfNumber:match ? +match[1] : null,
        outputName:name, via,
      };
    }
    const next=imageInput(node);
    if (!next) return {
      connected:true, conveyor:false, node, outputName:String(output?.name || ""),
      via,
      unresolved:via.length ? "Ambiguous IMAGE input path" : null,
    };
    via.push(node.title || kind(node) || "IMAGE processor");
    link=linkAt(graph,next.link);
  }
  return {connected:input?.link != null,conveyor:false,node:null,via,
    unresolved:"IMAGE source could not be traced safely"};
}
export function conveyorState(node) {
  const live=node?.__bil?.state;
  if (live && typeof live==="object") return live;
  try {
    return JSON.parse(node?.widgets?.find((w)=>w.name==="state_json")?.value || "{}");
  } catch { return {}; }
}
export function shelfImagePath(state, slot) {
  if (!Number.isInteger(slot) || slot<1 || slot>8 ||
      state?.output_mode!=="persistent_refs") return null;
  const ref=state?.reference_slots?.[slot-1];
  const name=typeof ref?.annotated==="string" ? ref.annotated : "";
  if (!name.endsWith(" [input]") || ref?.type!=="input") return null;
  // The Conveyor's authenticated thumbnail endpoint resolves within the
  // input root; never supply arbitrary absolute filesystem paths.
  const relative=name.slice(0,-8);
  if (!relative || relative.startsWith("/") || relative.includes("\\") ||
      relative.split("/").some((part)=>!part || part==="." || part===".."))
    return null;
  return relative;
}
