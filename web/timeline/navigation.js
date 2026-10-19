// Full-source navigation must never be clipped to the selected H3 render
// window. These are view-only geometry operations, not generation inputs.
export function movieTimelineWidth(durationFrames, zoom, viewportWidth, gutter=210) {
  if (!(Number.isFinite(durationFrames)&&durationFrames>=0 &&
        Number.isFinite(zoom)&&zoom>0))throw new Error("Invalid movie timeline");
  return Math.max(viewportWidth,gutter+(durationFrames*zoom)/24);
}
export function scrollToFrame(frame,zoom,viewportWidth,durationFrames,margin=215) {
  const width=movieTimelineWidth(durationFrames,zoom,viewportWidth);
  const x=180+(frame*zoom)/24;
  return Math.max(0,Math.min(Math.max(0,width-viewportWidth),x-margin));
}
export function fitMovieZoom(durationFrames,viewportWidth) {
  return Math.max(.1,Math.min(1000,
    (Math.max(200,viewportWidth)-210)*24/Math.max(1,durationFrames)));
}
export function fitRangeZoom(start,end,viewportWidth) {
  return Math.max(.1,Math.min(1000,
    (Math.max(200,viewportWidth)-210)*24/Math.max(1,end-start)));
}
