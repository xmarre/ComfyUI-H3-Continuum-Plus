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

export function timelineWheelPan(deltaX,deltaY,{ctrlKey=false,metaKey=false}={}) {
  if(ctrlKey||metaKey)return 0;
  return Number.isFinite(deltaX)?deltaX:0;
}

/** Zoom the complete source clock around the mouse's exact point. */
export function timelineWheelZoom(zoom,scroll,pointerX,deltaY,viewport,duration,
                                  {ctrlKey=false,metaKey=false,shiftKey=false}={}) {
  if(ctrlKey||metaKey||shiftKey||!Number.isFinite(deltaY)||deltaY===0)
    return null;
  const factor=Math.exp(-Math.max(-500,Math.min(500,deltaY))*.002);
  const next=Math.max(.1,Math.min(1000,zoom*factor));
  const anchor=(scroll+pointerX-180)*24/zoom;
  const width=movieTimelineWidth(duration,next,viewport);
  return {zoom:next,scrollLeft:Math.max(0,
    Math.min(Math.max(0,width-viewport),180+anchor*next/24-pointerX)),
    anchorFrame:anchor};
}
