// Explicit local captures. No upload, project edit, clipboard write or reference
// routing happens implicitly; the user downloads the resulting PNG.
function png(canvas) {
  return new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):
    reject(Error("Could not encode the capture")),"image/png"));
}
export function downloadCapture(blob,name) {
  const url=URL.createObjectURL(blob);
  const link=document.createElement("a");
  link.href=url;link.download=name;document.body.append(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),60000);
}
export async function saveViewerFrame(editor) {
  const image=editor.image;
  const video=editor.video;
  // Save the actual visible picture, including the current composed player.
  const source=!video.hidden && video.readyState>=2 ? video :
    !image.hidden && image.complete && image.naturalWidth &&
      (!editor.previewStillUrl || image.src===new URL(editor.previewStillUrl,location.href).href) ? image : null;
  if(!source)throw Error("No loaded preview frame to capture. Seek to a picture first.");
  const canvas=document.createElement("canvas");
  canvas.width=source.videoWidth||source.naturalWidth;
  canvas.height=source.videoHeight||source.naturalHeight;
  canvas.getContext("2d").drawImage(source,0,0);
  downloadCapture(await png(canvas),`continuum-frame-${editor.playhead}.png`);
  editor.message(`Displayed frame saved · ${canvas.width}×${canvas.height} PNG`);
}
export async function captureWorkspace(editor) {
  if(globalThis.h3NativeMedia?.captureEditor) {
    const data=await globalThis.h3NativeMedia.captureEditor();
    const response=await fetch(data);
    downloadCapture(await response.blob(),"continuum-editor.png");
  } else {
    if(!navigator.mediaDevices?.getDisplayMedia)
      throw Error("Editor screenshots require the current Timeline Desktop or a browser with screen sharing support.");
    // The browser chooser is invoked only by the explicit Screenshot action.
    // Stop every track on success, cancellation or decode failure.
    let stream;
    const video=document.createElement("video");
    try {
      stream=await navigator.mediaDevices.getDisplayMedia({video:true,audio:false});
      video.srcObject=stream;video.muted=true;
      await video.play();
      // play() has decoded a frame. A static shared window may never emit
      // another frame, so waiting for a subsequent frame callback can hang.
      await new Promise(resolve=>requestAnimationFrame(resolve));
      const canvas=document.createElement("canvas");
      canvas.width=video.videoWidth;canvas.height=video.videoHeight;
      canvas.getContext("2d").drawImage(video,0,0);
      downloadCapture(await png(canvas),"continuum-editor.png");
    } finally {
      stream?.getTracks().forEach(track=>track.stop());
      video.pause();video.srcObject=null;
    }
  }
  editor.message("Screenshot saved locally · attach the PNG when reporting an issue");
}
