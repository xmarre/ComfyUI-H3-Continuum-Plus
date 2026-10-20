// Timeline Studio UI geometry only. Values belong to node view properties and
// never enter the model's causal project, prompt, or media export contract.
export const LIMITS=Object.freeze({
  preview:110, chunks:150, timeline:110,
  sidebar:135, center:280, thumb:54,
});
export const DEFAULTS=Object.freeze({
  left:190,right:225,card:390,thumb:100,track:60,
});
export function clamp(value,min,max) {
  return Math.max(min,Math.min(max,value));
}
export function normalizeVertical(available,preview,chunk) {
  available=Math.max(0,Math.floor(available));
  // If the viewport is smaller than the normal minima, keep timeline
  // accessible rather than allowing one pane to crush the others to zero.
  const scale=Math.min(1,available/(LIMITS.preview+LIMITS.chunks+LIMITS.timeline));
  const pmin=Math.floor(LIMITS.preview*scale);
  const cmin=Math.floor(LIMITS.chunks*scale);
  const tmin=available-pmin-cmin>=LIMITS.timeline*scale
    ? Math.ceil(LIMITS.timeline*scale) : 0;
  if(available===0)return {preview:0,chunk:0,timeline:0};
  const requestedPreview=Number.isFinite(preview)?preview:available*.42;
  const requestedChunk=Number.isFinite(chunk)?chunk:available*.35;
  let p=clamp(Math.round(requestedPreview),pmin,available-cmin-tmin);
  let c=clamp(Math.round(requestedChunk),cmin,available-p-tmin);
  return {preview:p,chunk:c,timeline:available-p-c};
}
export function normalizeHorizontal(width,left,right) {
  const gutter=12,available=Math.max(0,Math.floor(width)-gutter);
  const scale=Math.min(1,available/(LIMITS.sidebar*2+LIMITS.center));
  const minside=Math.floor(LIMITS.sidebar*scale);
  const middle=Math.ceil(LIMITS.center*scale);
  const l=clamp(Math.round(left??DEFAULTS.left),minside,available-minside-middle);
  const r=clamp(Math.round(right??DEFAULTS.right),minside,available-l-middle);
  return {left:l,right:r,center:available-l-r};
}
export function setLayoutDimension(layout,name,value) {
  const options={
    left:[LIMITS.sidebar,600],
    right:[LIMITS.sidebar,600],
    card:[275,900],
    thumb:[LIMITS.thumb,260],
    track:[44,160],
    preview:[LIMITS.preview,2400],
    chunk:[LIMITS.chunks,2400],
  };
  const bounds=options[name];
  if(!bounds || !Number.isFinite(Number(value)))
    throw new Error("Invalid layout dimension");
  return {...layout,[name]:clamp(Math.round(Number(value)),...bounds)};
}
export function layoutView(raw) {
  const input=raw && typeof raw==="object" ? raw : {};
  const out={};
  for(const key of ["preview","chunk","left","right","card","thumb","track"]){
    if(input[key]==null)continue;
    try{out[key]=setLayoutDimension({},key,input[key])[key]}catch{}
  }
  return out;
}
