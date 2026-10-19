// Checked by Continuum Timeline Desktop against the *served* extension code.
// Do not invoke filesystem discovery or reintroduce browser video uploads.
export const NATIVE_PATH_CONTRACT = "h3-timeline-scene-audio-v15";
import {
  History,
  clone,
  uid,
  ratio,
  sourceAt,
  snap,
  selectedClips,
  exportTarget,
  routeClipToGuidance,
  visibleClipGeometry,
  viewerFrameDimensions,
  split,
  mergeAdjacentClips,
  setSceneStart,
  restoreOriginalMovie,
  trim,
  move,
  remove,
  paste,
  makeTrack,
  placeAsset,
} from "./core.js";
import { renderChunkWorkspace, updateChunkCompiledLines, samplerInputDescription, refreshConveyorControls } from "./chunk_ui.js?v=h3-timeline-scene-audio-v15";
import { layoutView, normalizeVertical, normalizeHorizontal, setLayoutDimension, DEFAULTS, LIMITS } from "./layout.js";
import { movieTimelineWidth, scrollToFrame, fitMovieZoom, fitRangeZoom, timelineWheelPan } from "./navigation.js";
import { selectedChunkPage } from "./chunk_workspace.js";
const BASE = "/h3_continuum/timeline";
const ROLES = [
  "retained",
  "video_guide",
  "image_reference",
  "audio",
  "prompt",
  "generated",
];
const LABELS = {
  retained: "Original footage",
  video_guide: "Video guidance",
  image_reference: "Image reference set",
  audio: "Audio",
  prompt: "Prompt regions",
  generated: "Generated takes",
};
const el = (tag, attrs = {}, ...children) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  n.append(...children.filter((x) => x != null));
  return n;
};
const button = (text, fn, title = text) =>
  el("button", { type: "button", text, title, onclick: fn });
const time = (f) =>
  `${Math.floor(f / 86400)
    .toString()
    .padStart(2, "0")}:${Math.floor(f / 1440) % 60}`.replace(/:(\d)$/, ":0$1") +
  `:${(Math.floor(f / 24) % 60).toString().padStart(2, "0")}:${(f % 24).toString().padStart(2, "0")}`;
export async function request(api, path, options = {}) {
  const r = await api.fetchApi(BASE + path, options);
  if (!r.ok) {
    let message;
    try {
      message = (await r.json()).error;
    } catch {
      message = r.statusText;
    }
    throw new Error(message || "Timeline request failed");
  }
  return r.json();
}
export class TimelineEditor {
  constructor({ node, widget, api, app }) {
    this.node = node;
    this.widget = widget;
    this.api = api;
    this.app = app;
    const project = JSON.parse(widget.value);
    // View state lives in node properties: it is saved with the workflow but
    // never reaches the prompt, so scrolling cannot invalidate execution.
    node.properties ??= {};
    const view = {
      ...(project.ui || {}),
      ...(node.properties.h3_timeline_view || {}),
    };
    delete project.ui;
    this.history = new History(project);
    this.serverRevision = this.p.revision || 0;
    this.selected = view.selected || [];
    this.clipboard = [];
    this.chunkFocus = view.chunkFocus ?? true;
    this.playhead = view.playhead ?? this.p.selection[0];

    this.previewMode = view.previewMode || "composed";
    this.chunkPage = view.chunkPage || 0;
    this.layout = layoutView(view.layout);
    this.layoutDrag = null;
    this.chunkInspectionEpoch = 0;
    this.chunkCompileTimer = null;
    this.compiledWindows = null;
    this.exportResolutionChoice = view.exportResolution || "source";
    this.zoom =
      view.zoom ||
      Math.min(160, Math.max(30,
        (innerWidth - 600) * 24 /
        (this.chunkFocus ? Math.max(24, this.p.selection[1]-this.p.selection[0])
          : Math.max(24, this.p.duration_frames))));
    this.abort = new AbortController();
    this.queuedIds = new Set(view.queuedPrompts || []);
    this.proxy = false;
    this.playWanted = false;
    this.playEpoch = 0;
    this.playController = null;
    this.nextPreview = null;
    this.closed = false;
    this.editVersion = 0;
    this.waveCache = new Map();
    // Keep image elements across viewport redraws: detaching and recreating
    // them on each scroll otherwise restarts in-flight 1080p thumbnail seeks.
    this.filmTileCache = new Map();
    this.seekTimer = null;
    this.root = el("dialog", {
      class: "h3ts",
      tabindex: "0",
      "aria-label": "Continuum Timeline Studio",
    });
    this.root.addEventListener("cancel", (e) => {
      e.preventDefault();
      this.close();
    });
    this.root.addEventListener("keydown", (e) => this.key(e));
    this.root.addEventListener("pointerdown", (e) => {
      if (!e.target.closest("input,textarea,select,button,[role=separator]")) this.root.focus();
    });
    document.body.append(this.root);
    this.root.showModal();
    this.build();
    this.applyLayout();
    this.render();
    this.conveyorPoll = setInterval(() => refreshConveyorControls(this), 1250);
    // A focused edit range changes initial zoom/location, never the source
    // movie's scrollable extent. Saved scroll position takes precedence.
    const legacyFocusedStart = this.chunkFocus &&
      (view.scroll ?? 0) === 0 && this.p.selection[0] > 0;
    this.scroller.scrollLeft = Number.isFinite(view.scroll) &&
      !legacyFocusedStart ? view.scroll :
      scrollToFrame(this.p.selection[0],this.zoom,
        this.scroller.clientWidth,this.p.duration_frames);
    this.scheduleDraw();
    this.root.focus();
    this.guard(() => this.pruneQueued());
  }
  // Keep only prompt ids the server still has pending or running.
  async pruneQueued() {
    if (!this.queuedIds.size) return;
    const checked = new Set(this.queuedIds);
    const response = await this.api.fetchApi("/queue");
    if (!response.ok) return;
    const queue = await response.json();
    if (this.closed) return;
    const live = new Set(
      [...(queue.queue_pending || []), ...(queue.queue_running || [])].map(
        (row) => row[1],
      ),
    );
    this.queuedIds = new Set(
      [...this.queuedIds].filter((id) => !checked.has(id) || live.has(id)),
    );
    this.persistView();
  }
  get p() {
    return this.history.value;
  }
  message(text, error = false) {
    this.status.textContent = text;
    this.status.classList.toggle("error", error);
  }
  async guard(fn) {
    try {
      return await fn();
    } catch (e) {
      if (e.name !== "AbortError") this.message(e.message, true);
    }
  }
  edit(fn) {
    this.stopPlayback();
    this.editVersion++;
    this.history.commit(fn);
    this.persistWidget();
    this.render();
  }
  editChunkReferences(fn) {
    // The reference checkbox edits only input routing. Do not stop an active
    // source preview, rebuild the movie track DOM, or restart decodes.
    this.editVersion++;
    this.history.commit(fn);
    this.persistWidget();
    this.compiledWindows=null;
    refreshConveyorControls(this, true);
    this.scheduleChunkCompile();
  }
  // Resizers operate only on DOM view geometry. They never edit the H3
  // project or trigger model input compilation.
  layoutAvailableHeight() {
    const sized=new Set([this.main,this.chunkBoard,this.scroller,
      this.previewDivider,this.chunkDivider]);
    const fixed=[...this.root.children].filter(el=>!sized.has(el))
      .reduce((sum,el)=>sum+el.getBoundingClientRect().height,0);
    const grips=(this.previewDivider?.offsetHeight||8)+
      (this.chunkDivider?.offsetHeight||8);
    return Math.max(0,this.root.clientHeight-fixed-grips);
  }
  applyLayout() {
    if (!this.main || !this.chunkBoard || !this.scroller || this.closed) return;
    const vertical=normalizeVertical(
      this.layoutAvailableHeight(),this.layout.preview,this.layout.chunk);
    const horizontal=normalizeHorizontal(
      this.main.clientWidth||this.root.clientWidth,
      this.layout.left,this.layout.right);
    this.displayLayout={...vertical,...horizontal};
    const style=this.root.style;
    style.setProperty("--h3ts-preview-h",vertical.preview+"px");
    style.setProperty("--h3ts-chunk-h",vertical.chunk+"px");
    style.setProperty("--h3ts-left-w",horizontal.left+"px");
    style.setProperty("--h3ts-right-w",horizontal.right+"px");
    style.setProperty("--h3ts-card-min", (this.layout.card??DEFAULTS.card)+"px");
    style.setProperty("--h3ts-ref-size", (this.layout.thumb??DEFAULTS.thumb)+"px");
    style.setProperty("--h3ts-track-h", (this.layout.track??DEFAULTS.track)+"px");
    for(const [name,node] of Object.entries(this.splitters||{})){
      const key={preview:"preview",chunk:"chunk",
        left:"left",right:"right"}[name];
      node.setAttribute("aria-valuenow",String(Math.round(this.displayLayout[key])));
    }
    // The dedicated scroller ResizeObserver already coalesces visible
    // timeline redraws. Do not rebuild filmstrip DOM on every drag pixel.
  }
  updateLayout(name,value,{persist=true}={}) {
    this.layout=setLayoutDimension(this.layout,name,value);
    this.applyLayout();
    if(persist)this.persistView();
  }
  splitter(axis,name,label) {
    const separator=el("div",{
      class:"h3ts-splitter h3ts-splitter-"+axis,
      role:"separator",tabindex:"0",
      "aria-label":label,
      title:label+" · drag or use arrow keys · double-click to reset",
      "aria-orientation":axis==="horizontal"?"horizontal":"vertical",
      "aria-valuemin":"0","aria-valuemax":"2400",
    });
    const adjust=(delta)=>{
      const baseline=this.displayLayout?.[name]??this.layout[name]??0;
      const value=name==="right" ? baseline-delta : baseline+delta;
      this.updateLayout(name,value);
    };
    separator.addEventListener("keydown",e=>{
      const delta=axis==="horizontal"
        ? (e.key==="ArrowDown"?24:e.key==="ArrowUp"?-24:null)
        : (e.key==="ArrowRight"?24:e.key==="ArrowLeft"?-24:null);
      if(delta!=null){e.preventDefault();adjust(delta);}
      if(e.key==="Home"||e.key==="End"){
        e.preventDefault();
        const isMax=e.key==="End";
        const bounds={
          preview:[LIMITS.preview,10000],chunk:[LIMITS.chunks,10000],
          left:[LIMITS.sidebar,600],right:[LIMITS.sidebar,600],
        }[name];
        this.updateLayout(name,isMax?bounds[1]:bounds[0]);
      }
    });
    separator.addEventListener("dblclick",()=>{
      const next={...this.layout};
      delete next[name];
      this.layout=next;this.applyLayout();this.persistView();
    });
    separator.addEventListener("pointerdown",e=>{
      if(e.button!==0 || this.layoutDrag)return;
      e.preventDefault();e.stopPropagation();
      const startCoord=axis==="horizontal"?e.clientY:e.clientX;
      const initial=this.displayLayout?.[name]??0;
      this.layoutDrag={name};
      separator.classList.add("h3ts-dragging");
      separator.setPointerCapture?.(e.pointerId);
      const move=event=>{
        if(!this.layoutDrag||this.layoutDrag.name!==name)return;
        const offset=(axis==="horizontal"?event.clientY:event.clientX)-startCoord;
        const target=initial+(name==="right"?-offset:offset);
        this.updateLayout(name,target,{persist:false});
      };
      const finish=()=>{
        separator.removeEventListener("pointermove",move);
        separator.removeEventListener("pointerup",finish);
        separator.removeEventListener("pointercancel",finish);
        separator.classList.remove("h3ts-dragging");
        if(this.layoutDrag?.name===name){
          this.layoutDrag=null;
          // Record the actual clamped dimension, not an unreachable desired
          // size, so reopening with the same dialog geometry is stable.
          this.layout[name]=this.displayLayout?.[name]??this.layout[name];
          this.persistView();
        }
      };
      separator.addEventListener("pointermove",move);
      separator.addEventListener("pointerup",finish);
      separator.addEventListener("pointercancel",finish);
    });
    return separator;
  }
  persistView() {
    this.node.properties.h3_timeline_view = {
      playhead: this.playhead,
      zoom: this.zoom,
      selected: [...this.selected],
      queuedPrompts: [...this.queuedIds],
      scroll: this.scroller?.scrollLeft || 0,
      previewMode: this.previewMode,
      chunkFocus: this.chunkFocus,
      chunkPage: this.chunkPage,
      exportResolution: this.exportResolutionChoice,
      layout: {...this.layout},
    };
  }
  persistWidget() {
    this.persistView();
    // Undo snapshots carry the revision they were taken at; the server's
    // current revision is authoritative for the next optimistic save.
    this.p.revision = this.serverRevision;
    delete this.p.ui;
    this.widget.value = JSON.stringify(this.p);
    this.node.title = "Timeline Studio · " + this.p.name;
    this.node.__timelineSummary = [
      `${this.p.width}×${this.p.height} · ${(this.p.duration_frames / 24).toFixed(2)} s · 24 fps`,
      `${this.p.chunk_frames} f chunks · render ${(this.p.selection[0] / 24).toFixed(2)}–${(this.p.selection[1] / 24).toFixed(2)} s`,
    ];
    this.node.setDirtyCanvas?.(true, true);
    this.node.__timelineRefreshSamplers?.();
  }
  async save() {
    const version = this.editVersion;
    const saved = await request(this.api, "/project", {
      method: "POST",
      body: JSON.stringify({
        project: this.p,
        expected_revision: this.serverRevision,
      }),
    });
    this.serverRevision = saved.revision;
    if (version === this.editVersion) this.history.value = saved;
    else this.p.revision = saved.revision;
    this.persistWidget();
    this.message(
      version === this.editVersion
        ? "Project saved · revision " + saved.revision
        : "Earlier snapshot saved · newer edits remain unsaved",
    );
  }
  async reload() {
    this.stopPlayback();
    const p = await request(this.api, "/project/" + this.p.project_id);
    this.history = new History(p);
    this.serverRevision = p.revision;
    this.selected = [];
    this.render();
    this.persistWidget();
    this.message("Saved project loaded");
  }
  build() {
    this.status = el("output", {
      class: "h3ts-status",
      text: "Ready · source timestamps and output frames are separate clocks",
    });
    this.title = el("input", {
      value: this.p.name,
      "aria-label": "Project name",
      onchange: (e) => this.edit((p) => (p.name = e.target.value)),
    });
    const file = el("input", {
      type: "file",
      multiple: "",
      accept: "video/*,audio/*,image/*,.mkv",
      onchange: (e) => this.guard(() => this.importFiles(e.target.files)),
    });
    file.hidden = true;
    this.root.append(
      el(
        "header",
        {},
        el("strong", { text: "CONTINUUM / TIMELINE STUDIO" }),
        this.title,
        button("Import media", () => file.click()),
        file,
        button("Save", () => this.guard(() => this.save())),
        button("Reload", () => this.guard(() => this.reload())),
        button("Close", () => this.close()),
      ),
    );
    this.assets = el("aside", { class: "h3ts-assets" });
    this.inspector = el("aside", { class: "h3ts-inspector" });
    this.video = el("video", {
      preload: "metadata",
      playsinline: "",
    });
    this.image = el("img", { class: "h3ts-image", alt: "Timeline source preview" });
    this.image.hidden = true;
    // A fast cached filmstrip-sized still is shown only while the independent
    // high-detail viewer request is pending; it is never used for sampling,
    // export, or as a substitute for full-resolution delivery pixels.
    this.previewPlaceholder = el("img", {
      class: "h3ts-preview-placeholder", alt: "Temporary low-detail source preview",
    });
    this.previewPlaceholder.hidden = true;
    this.previewStatus = el("div", {
      class:"h3ts-preview-state",text:"",role:"status","aria-live":"polite",
    });
    this.previewStatus.hidden = true;
    this.video.addEventListener("error", () => {
      if (this.closed) return;
      if (this.playWanted) {
        this.stopPlayback();
        this.message("Playback segment could not be decoded", true);
        return;
      }
      if (!this.video.hidden && this.previewAsset && !this.proxy)
        this.guard(() => this.preview(true));
    });
    this.video.addEventListener("ended", () => {
      if (!this.playWanted || this.closed || this.compositionStart == null)
        return;
      if (this.compositionEnd >= this.p.duration_frames) {
        this.stopPlayback();
        this.message("Reached end of timeline");
        return;
      }
      this.playhead = this.compositionEnd;
      this.guard(() => this.playSegment(this.playhead, this.playEpoch));
    });
    this.video.addEventListener("timeupdate", () => {
      if (this.compositionStart != null) {
        this.playhead = Math.min(
          this.p.duration_frames - 1,
          this.compositionStart + Math.floor(this.video.currentTime * 24),
        );
        this.drawPlayhead();
        if (this.playWanted && this.compositionEnd < this.p.duration_frames &&
            this.compositionEnd - this.playhead <= 60) {
          this.prefetchSegment(this.compositionEnd);
        }
        return;
      }
      if (!this.video.paused && this.previewClip) {
        const c = this.previewClip;
        const src = this.video.currentTime + (this.proxyOffset || 0);
        this.playhead = Math.round(
          c.start +
            ((src - ratio(c.source_in || 0)) / ratio(c.speed || 1)) * 24,
        );
        this.drawPlayhead();
      }
    });
    this.counter = el("output", { text: time(0) });
    this.compare = el(
      "select",
      { "aria-label": "Preview mode" },
      el("option", { value: "composed", text: "Composed cut" }),
      el("option", { value: "source", text: "A · original" }),
      el("option", { value: "generated", text: "B · generated" }),
    );
    this.compare.value = this.previewMode;
    this.compare.onchange = () => {
      this.stopPlayback();
      this.guard(() => this.preview());
    };
    const center = el(
      "section",
      { class: "h3ts-viewer" },
      el("div", { class: "h3ts-preview" },
        this.video, this.previewPlaceholder, this.image, this.previewStatus),
      el(
        "nav",
        {},
        button("−1 frame", () => this.seek(this.playhead - 1)),
        button("Play / pause", () => this.guard(() => this.playComposition())),
        button("+1 frame", () => this.seek(this.playhead + 1)),
        this.counter,
        this.compare,
        button("Source-window proxy", () =>
          this.guard(() => this.preview(true)),
        ),
      ),
    );
    this.splitters={
      left:this.splitter("vertical","left","Resize media library width"),
      right:this.splitter("vertical","right","Resize inspector width"),
    };
    this.main=el("main", {},this.assets,this.splitters.left,
      center,this.splitters.right,this.inspector);
    this.root.append(this.main);
    this.previewDivider=this.splitter("horizontal","preview",
      "Resize media preview and upper workspace height");
    this.root.append(this.previewDivider);
    const role = el(
      "select",
      { "aria-label": "New track kind" },
      ...ROLES.filter((r) => !["generated","image_reference"].includes(r)).map((r) =>
        el("option", { value: r, text: LABELS[r] }),
      ),
    );
    this.ripple = el("input", {
      type: "checkbox",
      "aria-label": "Ripple edits",
    });
    this.snapping = el("input", {
      type: "checkbox",
      checked: "",
      "aria-label": "Snap",
    });
    this.rangeA = el("input", {
      type: "number",
      min: "0",
      value: this.p.selection[0],
      "aria-label": "Render in frame",
    });
    this.rangeB = el("input", {
      type: "number",
      min: "1",
      value: this.p.selection[1],
      "aria-label": "Render out frame",
    });
    for (const input of [this.rangeA, this.rangeB])
      input.onchange = () =>
        this.edit(
          (p) => (p.selection = [+this.rangeA.value, +this.rangeB.value]),
        );
    this.root.append(
      el(
        "nav",
        { class: "h3ts-tools" },
        role,
        button("+ Track", () =>
          this.edit((p) =>
            p.tracks.push(makeTrack(role.value, LABELS[role.value])),
          ),
        ),
        button("Split", () =>
          this.edit((p) => split(p, this.selected, this.playhead)),
        ),
        button("Merge adjacent", () => this.guard(() =>
          this.edit((p) => mergeAdjacentClips(p, this.selected))),
          "Rejoin only source-continuous adjacent pieces with identical settings"),
        button("Duplicate", () => this.duplicate()),
        button("Delete", () =>
          this.edit((p) => remove(p, this.selected, this.ripple.checked)),
        ),
        el("label", {}, this.ripple, "Ripple"),
        el("label", {}, this.snapping, "Snap"),
        button("Undo", () => this.undo()),
        button("Redo", () => this.redo()),
        button("← Pan", () => {
          this.scroller.scrollLeft-=Math.max(120,this.scroller.clientWidth*.75);
        },"Move timeline left without changing source/playhead"),
        button("Pan →", () => {
          this.scroller.scrollLeft+=Math.max(120,this.scroller.clientWidth*.75);
        },"Move timeline right without changing source/playhead"),
        // Scene start and reset belong to the H3 chunk workspace, not the
        // crowded general-purpose timeline editing toolbar.
        button("Fit selection", () => this.fitSelectedRange(),
          "Zoom to the selected H3 render window"),
        button("Fit movie", () => this.fitFullMovie(),
          "Zoom out to see the entire original movie"),
        button("− Zoom", () => this.setZoom(this.zoom / 1.5)),
        button("+ Zoom", () => this.setZoom(this.zoom * 1.5)),
        button("Marker", () =>
          this.edit((p) =>
            p.markers.push({ id: uid(), frame: this.playhead, name: "Marker" }),
          ),
        ),
        el("span", { text: "Render [in,out)" }),
        this.rangeA,
        this.rangeB,
        button("Inspect inputs", () => this.guard(() => this.inspectInputs())),
        button("Guide H3 with selected clip", () => this.guard(() => this.guideSelectedClip())),
        button("Queue selection", () => this.guard(() => this.queue())),
        button("Reroll selection", () => this.guard(() => this.queue(true))),
        button("Cancel sampling", () =>
          this.guard(() => this.cancelSampling()),
        ),
      ),
    );
    this.inputRoute = el("p", { class: "h3ts-input-route" });
    this.root.append(this.inputRoute);
    this.chunkBoard = el("section", {class:"h3ts-chunk-workspace"});
    this.root.append(this.chunkBoard);
    this.chunkDivider=this.splitter("horizontal","chunk",
      "Resize chunk editor and track timeline height");
    this.root.append(this.chunkDivider);
    this.scroller = el("div", { class: "h3ts-scroll" });
    this.timeline = el("div", { class: "h3ts-timeline" });
    this.scroller.append(this.timeline);
    this.root.append(this.scroller);
    this.scroller.addEventListener("scroll", () => {
      this.scheduleDraw();
      this.persistView();
    });
    // A video timeline navigates horizontally with the ordinary wheel.
    // Preserve native horizontal trackpad movement and explicit vertical
    // scrolling when Shift is held. Never capture browser zoom shortcuts.
    this.scroller.addEventListener("wheel",(event)=>{
      // Shift+vertical-wheel explicitly scrolls stacked tracks when present.
      if(event.shiftKey && !event.deltaX &&
          this.scroller.scrollHeight>this.scroller.clientHeight)return;
      const pan=timelineWheelPan(event.deltaX,event.deltaY,event);
      if(!pan || this.scroller.scrollWidth<=this.scroller.clientWidth)return;
      event.preventDefault();
      this.scroller.scrollLeft+=pan;
    },{passive:false});
    this.root.addEventListener("dragover", (e) => e.preventDefault());
    this.root.addEventListener("drop", (e) => {
      if (e.dataTransfer.files.length) {
        e.preventDefault();
        this.guard(() => this.importFiles(e.dataTransfer.files));
      }
    });
    this.diagnostics = el("pre", { class: "h3ts-diagnostics" });
    const details = el(
      "details",
      {},
      el("summary", { text: "Model inputs and temporal diagnostics" }),
      this.diagnostics,
    );
    this.exportStatus = el("output", {
      class: "h3ts-export-progress", text: "",
      "aria-live": "polite",
    });
    this.exportResolution = el("select", {
      "aria-label": "Export pixel dimensions",
    },
      el("option", { value: "source", text: "Original source resolution" }),
      el("option", { value: "timeline", text: "H3 sampling canvas" }),
    );
    this.exportResolution.value = this.exportResolutionChoice;
    this.exportResolution.onchange = () => {
      this.exportResolutionChoice = this.exportResolution.value;
      this.persistView();
    };
    this.exportLink = el("a", {
      class: "h3ts-export-download",
      text: "", hidden: "",
    });
    this.root.append(
      el(
        "footer",
        {},
        this.status,
        this.exportStatus,
        this.exportLink,
        button("Review takes", () => this.guard(() => this.review())),
        el("label", {}, "Export pixels", this.exportResolution),
        button("Export clip/range · FFV1", () =>
          this.guard(() => this.export("lossless")),
        ),
        button("Export clip/range · H.264", () =>
          this.guard(() => this.export("h264")),
        ),
        button("Cancel export", () =>
          this.guard(
            () =>
              this.exportId &&
              request(this.api, "/export/" + this.exportId + "/cancel", {
                method: "POST",
              }),
          ),
        ),
      ),
      details,
    );
    this.resize = new ResizeObserver(() => this.scheduleDraw());
    this.resize.observe(this.scroller);
    this.layoutObserver=new ResizeObserver(() => this.applyLayout());
    this.layoutObserver.observe(this.root);
    // Wrapping toolbars/footer can change fixed-height chrome while dialog
    // width/height stays constant; remeasure available pane height then.
    for (const child of this.root.children)
      if (![this.main,this.chunkBoard,this.scroller,
            this.previewDivider,this.chunkDivider].includes(child))
        this.layoutObserver.observe(child);
  }
  render() {
    this.renderAssets();
    this.renderInspector();
    this.compiledWindows = null;
    renderChunkWorkspace(this);
    this.scheduleChunkCompile();
    this.inputRoute.textContent =
      "H3 INPUT CONTRACT · Chunk cards select Image Conveyor references through "+
      "the existing sampler IMAGE connections. Chunk prompts, video guidance, "+
      "and audio cuts are edited alongside them. Model/sampler connections "+
      "and overrides remain inspectable." +
      (this.p.operation === "recreate"
        ? " Recreate also derives guidance from retained footage when no explicit video guide exists."
        : " Add explicit video guidance to chunks that should condition H3.") +
      " Queue selection passes this project to the Continuum Timeline Sampler; " +
      "Export only writes a separate delivery file.";
    this.drawTimeline();
    this.rangeA.value = this.p.selection[0];
    this.rangeB.value = this.p.selection[1];
    if (!this.playWanted) this.guard(() => this.preview());
  }
  renderAssets() {
    this.assets.replaceChildren(
      el("h3", { text: "Timeline media" }),
      el("p", {
        text: "Drag video/audio onto tracks. Per-chunk reference images come from Image Conveyor's connected outputs; choose them in each chunk card.",
      }),
    );
    for (const a of this.p.assets) {
      const card = el(
        "div",
        { class: "h3ts-asset", draggable: "true" },
        el("strong", { text: a.name }),
        el("small", {
          text: `${a.kind} · ${Number(ratio(a.duration || 0)).toFixed(2)} s · ${a.storage === "linked" ? "linked source" : "managed copy"}`,
        }),
      );
      card.ondragstart = (e) =>
        e.dataTransfer.setData("application/x-h3-asset", a.id);
      const alias = el("input", {
        value: a.alias || "",
        placeholder: "Semantic alias, e.g. Alice",
        "aria-label": "Reference alias",
        onchange: (e) =>
          this.edit(
            (p) => (p.assets.find((x) => x.id === a.id).alias = e.target.value),
          ),
      });
      card.append(
        alias,
        button("Place at playhead", () => this.place(a)),
        button("Relink", () => {
          const f = el("input", {
            type: "file",
            onchange: (e) =>
              this.guard(() => this.relink(a, e.target.files[0])),
          });
          f.click();
        }),
      );
      this.assets.append(card);
    }
  }
  place(a, trackId = null, frame = this.playhead) {
    this.edit((p) => {
      let track = p.tracks.find((t) => t.id === trackId);
      if (!track) {
        track = makeTrack(
          a.kind === "image"
            ? "image_reference"
            : a.kind === "audio"
              ? "audio"
              : "retained",
          a.kind === "image" ? "Reference set" : a.name,
        );
        p.tracks.push(track);
      }
      const duration =
        a.kind === "image"
          ? p.chunk_frames
          : Math.max(1, Math.round(ratio(a.duration) * 24));
      const c = placeAsset(p, a, track, frame, duration);
      this.selected = [c.id];
    });
  }
  async resolveOriginal(file) {
    if (this.abort.signal.aborted) throw this.abort.signal.reason;
    const native = globalThis.h3NativeMedia;
    if (!native || typeof native.linkFile !== "function") {
      throw new Error(
        "Zero-copy drag requires Timeline Studio Desktop for Windows. " +
        "An ordinary browser does not expose the file path; " +
        "Studio will not scan drives or upload/copy the movie.",
      );
    }
    this.message("Linking the selected original in place · no scan or upload…");
    const transaction = uid();
    const cancel = () => native.cancelLink?.(transaction);
    this.abort.signal.addEventListener("abort", cancel, { once: true });
    let asset;
    try {
      asset = await native.linkFile(file, transaction);
    } finally {
      this.abort.signal.removeEventListener("abort", cancel);
    }
    if (this.abort.signal.aborted) throw this.abort.signal.reason;
    if (asset.storage !== "linked" && asset.storage !== "managed") {
      throw new Error("Native media bridge did not return a registered asset");
    }
    return asset;
  }
  async importFiles(files) {
    for (const file of files) {
      const asset = await this.resolveOriginal(file);
      this.addImportedAsset(asset);
      this.message(
        asset.storage === "linked"
          ? "Ready · original linked in place · no movie copy"
          : "Ready · identical asset already registered in the managed store",
      );
    }
  }
  addImportedAsset(a) {
    this.edit((p) => {
      if (!p.assets.some((x) => x.id === a.id)) p.assets.push(a);
    });
    this.place(a);
    // Embedded audio is attached to its retained video clip (both H3 guide
    // and final output). Never add a duplicate audio track for a new import.
  }
  async relink(old, file) {
    if (!file) return;
    const a = await this.resolveOriginal(file);
    this.edit((p) => {
      if (!p.assets.some((x) => x.id === a.id))
        p.assets.push({ ...a, alias: old.alias });
      for (const t of p.tracks)
        for (const c of t.clips) if (c.asset_id === old.id) c.asset_id = a.id;
    });
    this.message("Source relinked without uploading movie bytes");
  }
  renderInspector() {
    const pairs = selectedClips(
      {
        ...this.p,
        tracks: this.p.tracks.map((t) => ({ ...t, locked: false })),
      },
      this.selected,
    );
    this.inspector.replaceChildren(
      el("h3", { text: pairs.length ? `${pairs.length} selected` : "Project" }),
    );
    const field = (label, value, fn, type = "text") => {
      const i = el(type === "textarea" ? "textarea" : "input", {
        ...(type === "textarea" ? {} : { type }),
        value,
        "aria-label": label,
      });
      if (type === "textarea") i.value = value;
      i.onchange = () => this.edit(fn.bind(null, i.value));
      this.inspector.append(el("label", {}, label, i));
    };
    if (pairs.length === 1) {
      const [track, c] = pairs[0];
      const active=el("input",{
        type:"checkbox","aria-label":"Enable entire selected clip",
      });
      active.checked=!c.muted&&!track.muted;
      active.disabled=!!track.locked||!!track.muted;
      active.onchange=()=>this.edit(project=>{
        const item=project.tracks.find(t=>t.id===track.id)
          ?.clips.find(clip=>clip.id===c.id);
        if(item)item.muted=!active.checked;
      });
      this.inspector.append(el("label",{},
        active,
        "Active for this clip's entire timeline span"+
          (track.muted?" · track globally muted":"")+
          (track.locked?" · track locked":"")));
      field(
        "Name",
        c.name || "",
        (v, p) =>
          (p.tracks
            .find((t) => t.id === track.id)
            .clips.find((x) => x.id === c.id).name = v),
      );
      for (const [name, key] of [
        ["Timeline in (frame)", "start"],
        ["Timeline out (frame)", "end"],
        ["Source in (absolute PTS seconds)", "source_in"],
        ["Speed (negative = reverse)", "speed"],
        ["Alias", "alias"],
        ["Volume (linear)", "gain"],
        ["Fade in (seconds)", "fade_in"],
        ["Fade out (seconds)", "fade_out"],
      ])
        field(name, c[key] ?? (key === "gain" ? 1 : 0), (v, p) => {
          const clip = p.tracks
            .find((t) => t.id === track.id)
            .clips.find((x) => x.id === c.id);
          clip[key] = ["start", "end"].includes(key) ? Math.round(+v) : v;
        });
      for (const [label, key, values] of [
        ["End policy", "padding", ["error", "hold", "silence"]],
        [
          "Audio route",
          "audio_role",
          ["neither", "guide", "final", "both", "reference"],
        ],
      ]) {
        const select = el(
          "select",
          { "aria-label": label },
          ...values.map((v) => el("option", { value: v, text: v })),
        );
        select.value = c[key] || values[0];
        select.onchange = () =>
          this.edit(
            (p) =>
              (p.tracks
                .find((t) => t.id === track.id)
                .clips.find((x) => x.id === c.id)[key] = select.value),
          );
        this.inspector.append(el("label", {}, label, select));
      }
      field("Source out (absolute PTS seconds)", sourceAt(c, c.end), (v, p) => {
        const target = p.tracks
          .find((t) => t.id === track.id)
          .clips.find((x) => x.id === c.id);
        target.end =
          target.start +
          Math.max(
            1,
            Math.round(
              ((ratio(v) - ratio(target.source_in)) * 24) / ratio(target.speed),
            ),
          );
        p.duration_frames = Math.max(p.duration_frames, target.end);
      });
      if (track.role === "prompt")
        field(
          "Prompt",
          c.text || "",
          (v, p) =>
            (p.tracks
              .find((t) => t.id === track.id)
              .clips.find((x) => x.id === c.id).text = v),
          "textarea",
        );
      if (track.role === "prompt") {
        const override = el("input", {
          type: "checkbox",
          "aria-label": "Override inherited prompt",
        });
        override.checked = !!c.override;
        override.onchange = () =>
          this.edit(
            (p) =>
              (p.tracks
                .find((t) => t.id === track.id)
                .clips.find((x) => x.id === c.id).override = override.checked),
          );
        this.inspector.append(
          el(
            "label",
            {},
            override,
            "Override inherited prompt (global remains)",
          ),
        );
      }
      if (track.role === "retained") {
        const disabled = el("input", {
          type: "checkbox",
          "aria-label": "Disable video in Recreate",
        });
        disabled.checked = !!c.guide_disabled;
        disabled.onchange = () =>
          this.edit(
            (p) =>
              (p.tracks
                .find((t) => t.id === track.id)
                .clips.find((x) => x.id === c.id).guide_disabled =
                disabled.checked),
          );
        this.inspector.append(
          el(
            "label",
            {},
            disabled,
            "Disable this video in Recreate (audio independent)",
          ),
        );
      }
      if (track.role === "retained")
        this.inspector.append(
          button("Guide H3 with this clip", () =>
            this.guard(() => this.guideSelectedClip()),
          ),
        );
    } else {
      for (const [label, key] of [
        ["Duration (frames)", "duration_frames"],
        ["Chunk length (frames)", "chunk_frames"],
        ["Output width", "width"],
        ["Output height", "height"],
      ])
        field(
          label,
          this.p[key],
          (v, p) => (p[key] = Math.round(+v)),
          "number",
        );
      const mode = el(
        "select",
        { "aria-label": "Generation operation" },
        ...["generate", "recreate", "replace"].map((v) =>
          el("option", { value: v, text: v }),
        ),
      );
      mode.value = this.p.operation;
      mode.onchange = () => this.edit((p) => (p.operation = mode.value));
      this.inspector.append(el("label", {}, "Operation", mode));
      field(
        "Global prompt",
        this.p.prompts.global,
        (v, p) => (p.prompts.global = v),
        "textarea",
      );
      const format = el(
        "select",
        { "aria-label": "Prompt format" },
        ...["Auto", "Fixed", "List", "Timeline"].map((v) =>
          el("option", { value: v, text: v }),
        ),
      );
      format.value = this.p.prompts.mode;
      format.onchange = () => this.edit((p) => (p.prompts.mode = format.value));
      this.inspector.append(el("label", {}, "Prompt format", format));
      field(
        "Prompt script",
        this.p.prompts.script || "",
        (v, p) => (p.prompts.script = v),
        "textarea",
      );
      this.inspector.append(
        el("p", {
          text: "General references apply to the whole physical invocation. Timed internal H3 keyframes and hot LoRA switching are not supported.",
        }),
      );
    }
    if (this.p.markers.length) {
      this.inspector.append(el("h4", { text: "Markers / named regions" }));
      for (const marker of this.p.markers) {
        const name = el("input", {
          value: marker.name,
          "aria-label": "Marker name",
          onchange: (e) =>
            this.edit(
              (p) =>
                (p.markers.find((m) => m.id === marker.id).name =
                  e.target.value),
            ),
        });
        const end = el("input", {
          type: "number",
          min: String(marker.frame + 1),
          max: String(this.p.duration_frames),
          value: marker.end || marker.frame + 1,
          "aria-label": "Region end frame",
          onchange: (e) =>
            this.edit(
              (p) =>
                (p.markers.find((m) => m.id === marker.id).end =
                  +e.target.value),
            ),
        });
        this.inspector.append(
          el(
            "div",
            {},
            button(time(marker.frame), () => this.seek(marker.frame)),
            name,
            end,
            button("Render region", () =>
              this.edit(
                (p) =>
                  (p.selection = [
                    marker.frame,
                    marker.end || marker.frame + 1,
                  ]),
              ),
            ),
            button("×", () =>
              this.edit(
                (p) =>
                  (p.markers = p.markers.filter((m) => m.id !== marker.id)),
              ),
            ),
          ),
        );
      }
    }
  }
  focusOrigin() {
    // Rendering may be localized to selected chunks; navigation never is.
    return 0;
  }
  jumpToFrame(frame) {
    this.scroller.scrollLeft=scrollToFrame(frame,this.zoom,
      this.scroller.clientWidth,this.p.duration_frames);
    this.seek(frame);
    this.scheduleDraw();
  }
  fitSelectedRange() {
    this.zoom=fitRangeZoom(...this.p.selection,this.scroller.clientWidth);
    this.chunkFocus=true;
    this.drawTimeline();
    this.scroller.scrollLeft=scrollToFrame(this.p.selection[0],this.zoom,
      this.scroller.clientWidth,this.p.duration_frames);
    this.persistView();
  }
  fitFullMovie() {
    this.zoom=fitMovieZoom(this.p.duration_frames,this.scroller.clientWidth);
    this.chunkFocus=false;
    this.drawTimeline();
    this.scroller.scrollLeft=0;
    this.persistView();
  }
  frameX(f) {
    return 180 + ((f - this.focusOrigin()) * this.zoom) / 24;
  }
  fromX(clientX) {
    return Math.round(this.focusOrigin() +
      ((clientX - this.timeline.getBoundingClientRect().left - 180) * 24) /
        this.zoom);
  }
  points() {
    return [
      0,
      ...this.p.markers.map((m) => m.frame),
      ...this.p.tracks.flatMap((t) =>
        t.clips.flatMap((c) =>
          this.selected.includes(c.id) ? [] : [c.start, c.end],
        ),
      ),
      ...Array.from(
        { length: Math.ceil((this.p.duration_frames -
          (this.p.chunk_origin_frame||0)) / this.p.chunk_frames) + 1 },
        (_, i) => (this.p.chunk_origin_frame||0)+i*this.p.chunk_frames,
      ),
    ];
  }
  snapped(f) {
    return this.snapping.checked
      ? snap(f, this.points(), (8 * 24) / this.zoom)
      : Math.max(0, Math.round(f));
  }
  drawTimeline() {
    if (!this.timeline) return;
    const left = this.scroller.scrollLeft,
      right = left + this.scroller.clientWidth;
    const origin = this.focusOrigin();
    const focusEnd = this.p.duration_frames;
    const width = movieTimelineWidth(
      this.p.duration_frames,this.zoom,this.scroller.clientWidth,
    );
    this.timeline.style.width = width + "px";
    this.timeline.replaceChildren();
    const ruler = el("div", { class: "h3ts-ruler" });
    ruler.onpointerdown = (e) => this.seek(Math.max(0, this.fromX(e.clientX)));
    this.timeline.append(ruler);
    const stride = Math.max(24, Math.ceil(80 / this.zoom) * 24);
    for (
      let f = Math.max(
        0,
        Math.floor((origin + ((left - 180) * 24) / this.zoom) / stride) * stride,
      );
      f <= focusEnd && this.frameX(f) < right + 100;
      f += stride
    ) {
      ruler.append(
        el("span", {
          class: "h3ts-tick",
          style: `left:${this.frameX(f)}px`,
          text: time(f),
        }),
      );
    }
    for (
      let f = Math.max(
        0,
        (this.p.chunk_origin_frame||0) +
        Math.max(0,Math.floor((origin+((left-180)*24)/this.zoom-
          (this.p.chunk_origin_frame||0))/this.p.chunk_frames)) *
          this.p.chunk_frames,
      );
      f <= focusEnd && this.frameX(f) < right + 100;
      f += this.p.chunk_frames
    )
      this.timeline.append(
        el("div", {
          class: "h3ts-boundary",
          style: `left:${this.frameX(f)}px`,
          title: `Scene chunk ${Math.floor((f-(this.p.chunk_origin_frame||0)) /
            this.p.chunk_frames) + 1}`,
        }),
      );
    this.timeline.append(
      el("div", {
        class: "h3ts-render-range",
        style: `left:${this.frameX(this.p.selection[0])}px;width:${((this.p.selection[1] - this.p.selection[0]) * this.zoom) / 24}px`,
      }),
    );
    for (const m of this.p.markers)
      if (this.frameX(m.frame) >= left && this.frameX(m.frame) < right)
        this.timeline.append(
          el("button", {
            class: "h3ts-marker",
            style: `left:${this.frameX(m.frame)}px`,
            text: "◆ " + m.name,
            onclick: () => this.seek(m.frame),
          }),
        );
    for (const [index, t] of this.p.tracks.entries()) {
      const row = el("div", {
        class: "h3ts-track" + (t.hidden ? " h3ts-hidden" : ""),
      });
      const header = el(
        "div",
        { class: "h3ts-track-name" },
        el("input", {
          value: t.name || LABELS[t.role],
          "aria-label": "Track name",
          onchange: (e) =>
            this.edit(
              (p) =>
                (p.tracks.find((x) => x.id === t.id).name = e.target.value),
            ),
        }),
      );
      for (const [key, label] of [
        ["locked", "L"],
        ["muted", "M"],
        ["hidden", "H"],
      ])
        header.append(
          button(
            (t[key] ? "●" : "○") + label,
            () =>
              this.edit(
                (p) => (p.tracks.find((x) => x.id === t.id)[key] = !t[key]),
              ),
            { locked: "Lock track", muted: "Mute track", hidden: "Hide track" }[
              key
            ],
          ),
        );
      header.append(
        button("↑", () =>
          this.edit((p) => {
            if (index > 0)
              [p.tracks[index - 1], p.tracks[index]] = [
                p.tracks[index],
                p.tracks[index - 1],
              ];
          }),
        ),
        button("↓", () =>
          this.edit((p) => {
            if (index < p.tracks.length - 1)
              [p.tracks[index + 1], p.tracks[index]] = [
                p.tracks[index],
                p.tracks[index + 1],
              ];
          }),
        ),
      );
      row.append(header);
      row.ondragover = (e) => e.preventDefault();
      row.ondrop = (e) => {
        const assetId = e.dataTransfer.getData("application/x-h3-asset");
        if (assetId) {
          e.preventDefault();
          e.stopPropagation();
          const asset = this.p.assets.find((a) => a.id === assetId);
          this.place(asset, t.id, this.snapped(this.fromX(e.clientX)));
        }
      };
      row.ondblclick = (e) => {
        if (t.role === "prompt" && !t.locked) {
          const f = this.snapped(this.fromX(e.clientX));
          this.edit((p) => {
            const c = {
              id: uid(),
              start: f,
              end: Math.min(p.duration_frames, f + p.chunk_frames),
              source_in: "0",
              speed: "1",
              padding: "error",
              audio_role: "neither",
              text: "",
            };
            p.tracks.find((x) => x.id === t.id).clips.push(c);
            this.selected = [c.id];
          });
        }
      };
      if (!t.hidden)
        for (const c of t.clips) {
          const x = this.frameX(c.start),
            w = ((c.end - c.start) * this.zoom) / 24;
          const geometry = visibleClipGeometry(x, w, left, right);
          if (!geometry) continue;
          const {left: visibleLeft, width: visibleWidth, offset} = geometry;
          const clip = el(
            "div",
            {
              class: `h3ts-clip ${t.role}${this.selected.includes(c.id) ? " selected" : ""}`,
              style: `left:${visibleLeft}px;width:${visibleWidth}px`,
              tabindex: "0",
              role: "button",
              "aria-label": `${c.name || c.text || t.role} ${time(c.start)} to ${time(c.end)}`,
            },
            el("span", {
              text:
                c.name ||
                c.text ||
                this.p.assets.find((a) => a.id === c.asset_id)?.name ||
                "Prompt",
            }),
          );
          clip.onpointerdown = (e) => this.drag(e, t, c, clip);
          clip.oncontextmenu = (e) => this.context(e, t, c);
          const trimLeft = el("i", { class: "trim-left" });
          const trimRight = el("i", { class: "trim-right" });
          trimLeft.hidden = !geometry.showStartTrim;
          trimRight.hidden = !geometry.showEndTrim;
          clip.append(trimLeft, trimRight);
          if (t.role === "audio") this.waveform(clip, c, offset);
          else if (c.asset_id && w > 80)
            this.filmstrip(clip, c, left, right, offset);
          row.append(clip);
        }
      this.timeline.append(row);
    }
    this.playheadLine = el("div", {
      class: "h3ts-playhead",
      style: `left:${this.frameX(this.playhead)}px`,
    });
    this.timeline.append(this.playheadLine);
    this.drawPlayhead();
  }
  drag(e, t, c, element) {
    if (t.locked || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.shiftKey) {
      this.selected = this.selected.includes(c.id)
        ? this.selected.filter((id) => id !== c.id)
        : [...this.selected, c.id];
    } else if (!this.selected.includes(c.id)) this.selected = [c.id];
    const initial = clone(this.p),
      startX = e.clientX,
      edge = e.target.classList.contains("trim-left")
        ? "start"
        : e.target.classList.contains("trim-right")
          ? "end"
          : null;
    const target = e.currentTarget;
    target.setPointerCapture(e.pointerId);
    let delta = 0;
    const onmove = (event) => {
      delta =
        this.snapped(c.start + ((event.clientX - startX) * 24) / this.zoom) -
        c.start;
      element.style.transform = `translateX(${(delta * this.zoom) / 24}px)`;
    };
    const finish = () => {
      target.removeEventListener("pointermove", onmove);
      target.removeEventListener("pointerup", finish);
      target.removeEventListener("pointercancel", cancel);
      this.history.value = initial;
      if (delta)
        this.edit((p) =>
          edge
            ? trim(
                p,
                [c.id],
                edge,
                (edge === "start" ? c.start : c.end) + delta,
                this.ripple.checked,
              )
            : move(p, this.selected, delta, e.altKey),
        );
      else {
        this.renderInspector();
        this.drawTimeline();
      }
    };
    const cancel = () => {
      delta = 0;
      finish();
    };
    target.addEventListener("pointermove", onmove);
    target.addEventListener("pointerup", finish);
    target.addEventListener("pointercancel", cancel);
  }
  context(e, t, c) {
    e.preventDefault();
    this.selected = [c.id];
    const menu = el("div", {
      class: "h3ts-context",
      style: `left:${e.clientX}px;top:${e.clientY}px`,
    });
    for (const [label, fn] of [
      [
        "Split here",
        () => this.edit((p) => split(p, this.selected, this.playhead)),
      ],
      ["Merge adjacent", () =>
        this.guard(() => this.edit(p=>mergeAdjacentClips(p,this.selected)))],
      ["Duplicate", () => this.duplicate()],
      [
        "Delete",
        () => this.edit((p) => remove(p, this.selected, this.ripple.checked)),
      ],
      [
        "Set render range",
        () => this.edit((p) => (p.selection = [c.start, c.end])),
      ],
    ])
      menu.append(
        button(label, () => {
          menu.remove();
          fn();
        }),
      );
    this.root.append(menu);
    menu.addEventListener("mouseleave", () => menu.remove());
  }
  // Thumbnails and waveforms are requested on fixed grids so scrolling only
  // adds new tiles and every URL stays cacheable.
  filmstrip(element, c, left, right, offset = 0) {
    const step = 2 ** Math.max(0, Math.ceil(Math.log2((80 * 24) / this.zoom)));
    const visibleStart = Math.max(
      c.start,
      Math.floor((this.focusOrigin() + ((left - 180) * 24) / this.zoom) / step) * step,
    );
    const visibleEnd = Math.min(
      c.end,
      Math.ceil(this.focusOrigin() + ((right - 180) * 24) / this.zoom),
    );
    for (let f = visibleStart; f < visibleEnd; f += step) {
      const tile = Math.max(f - (f % step), c.start);
      const x = ((tile - c.start) * this.zoom) / 24 - offset;
      const src = BASE + "/thumbnail/" + c.asset_id + "?pts=" +
        sourceAt(c, tile).toFixed(6);
      const key = c.id + ":" + tile + ":" + step + ":" + src;
      let image = this.filmTileCache?.get(key);
      if (!image) {
        image = el("img", {
          class: "h3ts-filmstrip", src, loading: "lazy", alt: "",
        });
        this.filmTileCache?.set(key, image);
        while (this.filmTileCache?.size > 192)
          this.filmTileCache.delete(this.filmTileCache.keys().next().value);
      }
      image.style.left = x + "px";
      image.style.width = (step * this.zoom) / 24 + "px";
      element.append(image);
      f = tile;
    }
  }
  waveTile(asset, start, end) {
    const key = `${asset}:${start}:${end}`;
    if (!this.waveCache.has(key)) {
      const pending = request(
        this.api,
        `/waveform/${asset}?start=${start}&end=${end}`,
        { signal: this.abort.signal },
      ).catch((e) => {
        this.waveCache.delete(key);
        throw e;
      });
      this.waveCache.set(key, pending);
      if (this.waveCache.size > 256)
        this.waveCache.delete(this.waveCache.keys().next().value);
    }
    return this.waveCache.get(key);
  }
  waveform(element, c, offset = 0) {
    const speed = ratio(c.speed || 1);
    const pxPerSource = this.zoom / Math.abs(speed);
    const span =
      2 ** Math.min(5, Math.max(-3, Math.round(Math.log2(512 / pxPerSource))));
    const left = Math.max(
      c.start,
      Math.floor(this.focusOrigin() + ((this.scroller.scrollLeft - 180) * 24) / this.zoom),
    );
    const right = Math.min(
      c.end,
      Math.ceil(
        this.focusOrigin() + ((this.scroller.scrollLeft + this.scroller.clientWidth - 180) * 24) /
          this.zoom,
      ),
    );
    if (right <= left) return;
    const sa = Math.min(sourceAt(c, left), sourceAt(c, right));
    const sb = Math.max(sourceAt(c, left), sourceAt(c, right));
    for (let k = Math.max(0, Math.floor(sa / span)); k * span < sb; k++) {
      const start = k * span,
        end = start + span;
      // Timeline x of a source time inside this clip.
      const at = (t) => ((t - ratio(c.source_in || 0)) / speed) * this.zoom;
      const x = Math.min(at(start), at(end)) - offset;
      this.waveTile(c.asset_id, start, end)
        .then((data) => {
          if (!element.isConnected) return;
          const canvas = el("canvas", {
            width: String(data.peaks.length),
            height: "25",
            class: "h3ts-waveform",
            style: `left:${x}px;width:${span * pxPerSource}px`,
          });
          const ctx = canvas.getContext("2d");
          ctx.fillStyle = "#9ee8c5";
          const peaks = speed < 0 ? [...data.peaks].reverse() : data.peaks;
          for (let i = 0; i < peaks.length; i++)
            ctx.fillRect(i, 12 - peaks[i] * 12, 1, Math.max(1, peaks[i] * 24));
          element.append(canvas);
        })
        .catch(() => {});
    }
  }
  scheduleDraw() {
    if (this.drawPending) return;
    this.drawPending = requestAnimationFrame(() => {
      this.drawPending = 0;
      if (!this.closed) this.drawTimeline();
    });
  }
  stopPlayback() {
    this.playWanted = false;
    ++this.playEpoch;
    this.playController?.abort();
    this.playController = null;
    this.nextPreview?.controller.abort();
    this.nextPreview = null;
    this.video.pause();
  }
  async fetchSegment(start, length, controller) {
    return request(this.api, "/preview", {
      method: "POST",
      body: JSON.stringify({
        project: clone(this.p), start,
        length_frames: length, mode: this.compare.value,
      }),
      signal: controller.signal,
      timeoutMs: 15000,
    });
  }
  prefetchSegment(start) {
    if (!this.playWanted || this.closed || start >= this.p.duration_frames ||
        this.nextPreview?.start === start) return;
    this.nextPreview?.controller.abort();
    const controller = new AbortController();
    const next = {
      start, controller,
      promise: this.fetchSegment(start, 144, controller),
    };
    next.promise.catch(() => {}); // Pausing may discard the pending prefetch.
    this.nextPreview = next;
  }
  async playSegment(start, epoch) {
    if (!this.playWanted || epoch !== this.playEpoch || this.closed) return;
    if (start >= this.p.duration_frames) {
      this.stopPlayback();
      this.message("Reached end of timeline");
      return;
    }
    const cached = this.nextPreview?.start === start ? this.nextPreview : null;
    if (cached) this.nextPreview = null;
    const controller = cached?.controller || new AbortController();
    this.playController = controller;
    const cancel = () => controller.abort();
    this.abort.signal.addEventListener("abort", cancel, { once: true });
    try {
      this.message("Preparing short playback segment…");
      const result = await (cached
        ? cached.promise : this.fetchSegment(start, 48, controller));
      if (!this.playWanted || this.closed || epoch !== this.playEpoch ||
          controller.signal.aborted) return;
      this.video.pause();
      this.previewClip = null;
      this.video.dataset.source = result.url;
      this.video.src = result.url;
      this.video.hidden = false;
      this.image.hidden = true;
      this.compositionStart = result.start;
      this.compositionEnd = result.end;
      await this.video.play();
      if (this.playWanted && epoch === this.playEpoch) {
        this.message(
          "Playing · segment ready in " +
          ((result.prepare_ms || 0) / 1000).toFixed(2) +
          "s" + (result.strategy ? " · " + result.strategy : "") +
          (result.cache_hit ? " (cached)" : "") +
          " · next segment prefetching"
        );
        this.prefetchSegment(this.compositionEnd);
      }
    } catch (error) {
      if (this.playWanted && epoch === this.playEpoch &&
          error?.name !== "AbortError") {
        this.stopPlayback();
        this.message("Playback unavailable: " + error.message, true);
      }
    } finally {
      this.abort.signal.removeEventListener("abort", cancel);
      if (this.playController === controller) this.playController = null;
    }
  }
  async playComposition() {
    // Pause must be synchronous even when FFmpeg has not returned yet.
    if (this.playWanted || !this.video.paused) {
      this.stopPlayback();
      this.message("Paused");
      return;
    }
    if (!this.p.tracks.some(t =>
      !t.muted && t.clips?.some(c =>
        ["retained", "generated"].includes(t.role) ||
        (t.role === "audio" && ["final", "both"].includes(c.audio_role))
      )
    )) {
      this.message("No playable footage or final audio · import or place media first");
      return;
    }
    this.playWanted = true;
    const epoch = ++this.playEpoch;
    // Resume an already-buffered segment without a new render or HTTP call.
    if (this.compositionStart != null &&
        this.playhead >= this.compositionStart &&
        this.playhead < this.compositionEnd - 1 &&
        this.video.dataset.source) {
      try {
        await this.video.play();
        if (this.playWanted && this.playEpoch === epoch) {
          this.message("Playing");
          this.prefetchSegment(this.compositionEnd);
        }
      } catch (error) {
        if (this.playWanted && this.playEpoch === epoch) {
          this.stopPlayback();
          this.message("Playback unavailable: " + error.message, true);
        }
      }
      return;
    }
    await this.playSegment(this.playhead, epoch);
  }
  cachedFilmstripStill(clip, frame) {
    // Reuse already decoded filmstrip bytes instead of launching a second
    // low-res FFmpeg seek while the display-resolution seek is in flight.
    // This nearest-frame placeholder is explicitly temporary and never a
    // source for model tensors or exported pixels.
    let best=null;
    for (const [key,img] of this.filmTileCache || []) {
      if (!key.startsWith(clip.id+":") || !img.complete || !img.naturalWidth) continue;
      const tile=Number(key.split(":")[1]);
      const distance=Math.abs(tile-frame);
      if (Number.isFinite(tile) && distance<=48 &&
          (!best || distance<best.distance))
        best={distance,src:img.src};
    }
    return best?.src || null;
  }
  async preview(forceProxy = false) {
    this.compositionStart = null;
    const mode = this.compare.value;
    this.previewMode = mode;
    const roles =
      mode === "source"
        ? ["retained"]
        : mode === "generated"
          ? ["generated"]
          : ["retained", "generated"];
    let chosen;
    for (const t of this.p.tracks)
      if (!t.muted && roles.includes(t.role))
        for (const c of t.clips)
          if (c.start <= this.playhead && this.playhead < c.end) chosen = c;
    this.counter.textContent =
      time(this.playhead) +
      ` · scene chunk ${this.playhead<(this.p.chunk_origin_frame||0)?
        "before start":Math.floor((this.playhead-(this.p.chunk_origin_frame||0)) /
        this.p.chunk_frames)+1}`;
    if (!chosen) {
      this.previewStillUrl=null;
      this.video.pause();
      this.video.hidden = true;
      this.image.hidden = true;
      this.previewPlaceholder.hidden = true;
      this.previewStatus.hidden = false;
      this.previewStatus.textContent = "No visible footage at this timeline frame";
      return;
    }
    const asset = this.p.assets.find((a) => a.id === chosen.asset_id);
    if (!asset) {
      this.previewStillUrl=null;
      this.previewPlaceholder.hidden=true;
      this.image.hidden=true;
      this.previewStatus.hidden=false;
      this.previewStatus.textContent="Preview source is missing; relink the imported media";
      return;
    }
    this.previewClip = chosen;
    this.previewAsset = asset;
    const source = sourceAt(chosen, this.playhead),
      origin = ratio(asset.video?.start_time || 0);
    const [stillWidth, stillHeight] = viewerFrameDimensions(
      asset, this.image.parentElement?.clientWidth,
      this.image.parentElement?.clientHeight,
      globalThis.devicePixelRatio || 1,
    );
    const stillUrl = BASE + "/thumbnail/" + asset.id + "?pts=" +
      source.toFixed(6) + "&w=" + stillWidth + "&h=" + stillHeight;
    if (asset.kind === "image" || !forceProxy) {
      this.video.pause();
      this.video.hidden = true;
      const cached=asset.kind==="image" ? null :
        this.cachedFilmstripStill(chosen,this.playhead);
      const lowUrl=cached || BASE + "/thumbnail/" + asset.id + "?pts=" +
        source.toFixed(6);
      this.previewStillUrl=stillUrl;
      this.image.hidden=false;
      this.previewPlaceholder.hidden=false;
      this.previewPlaceholder.src=lowUrl;
      this.previewStatus.hidden=false;
      this.previewStatus.textContent=cached
        ? "Loading exact source frame… showing nearby cached filmstrip temporarily"
        : "Loading original-source display preview…";
      this.image.onload=()=>{
        if (this.closed || this.previewStillUrl!==stillUrl) return;
        this.previewPlaceholder.hidden=true;
        this.previewStatus.hidden=true;
      };
      this.image.onerror=()=>{
        if (this.closed || this.previewStillUrl!==stillUrl) return;
        this.image.hidden=true;
        this.previewStatus.hidden=false;
        this.previewStatus.textContent=
          "High-detail preview unavailable; showing temporary low-detail still. "+
          "Try Source-window proxy or seek again.";
      };
      this.previewPlaceholder.onerror=()=>{
        if (this.closed || this.previewStillUrl!==stillUrl) return;
        this.previewStatus.hidden=false;
        this.previewStatus.textContent=
          "Source frame could not be decoded; try Source-window proxy. "+
          "Check the media link if it persists.";
      };
      this.image.src=stillUrl;
      return;
    }
    this.previewStillUrl=null;
    this.previewPlaceholder.hidden=true;
    this.previewStatus.hidden=true;
    this.video.hidden = false;
    this.image.hidden = true;
    // A seek preview needs only four seconds, not a 30-second transcode.
    // All transport remains bounded to the inspected source window.
    const useProxy =
      forceProxy ||
      asset.video?.codec_name === "ffv1" ||
      /\.mkv$/i.test(asset.name);
    const offset = useProxy
      ? Math.max(0, Math.floor((source - origin) / 3) * 3)
      : 0;
    const url = useProxy
      ? `${BASE}/proxy/${asset.id}?start=${offset}&duration=4`
      : `${BASE}/media/${asset.id}?type=video/mp4`;
    this.proxy = useProxy;
    this.proxyOffset = offset + origin;
    if (this.video.dataset.source !== url) {
      this.video.dataset.source = url;
      this.video.src = url;
      this.video.addEventListener(
        "loadedmetadata",
        () => {
          if (!this.closed && this.video.dataset.source === url)
            this.video.currentTime = Math.max(0, source - origin - offset);
        },
        { once: true },
      );
    } else if (this.video.paused)
      this.video.currentTime = Math.max(0, source - origin - offset);
  }
  seek(f) {
    this.stopPlayback();
    this.compositionStart = null;
    clearTimeout(this.seekTimer);
    this.playhead = Math.min(
      this.p.duration_frames - 1,
      Math.max(0, Math.round(f)),
    );
    this.drawPlayhead();
    // Seek updates immediately, but only the last position in a rapid scrub
    // should submit a decoder request. Keep editing/playback paths unchanged.
    this.seekTimer = setTimeout(() => {
      this.seekTimer = null;
      if (!this.closed) this.guard(() => this.preview());
    }, 80);
    this.persistView();
  }
  drawPlayhead() {
    if (this.playheadLine)
      this.playheadLine.style.left = this.frameX(this.playhead) + "px";
    if (this.counter)
      this.counter.textContent =
        time(this.playhead) +
        ` · scene chunk ${this.playhead<(this.p.chunk_origin_frame||0)?
        "before start":Math.floor((this.playhead-(this.p.chunk_origin_frame||0)) /
        this.p.chunk_frames)+1}`;
  }
  setZoom(z) {
    this.zoom = Math.max(0.1, Math.min(1000, z));
    this.drawTimeline();
    this.persistView();
  }
  copy() {
    this.clipboard = selectedClips(this.p, this.selected).map(([t, c]) => [
      t.id,
      clone(c),
    ]);
  }
  duplicate() {
    this.copy();
    const end = Math.max(
      this.playhead,
      ...this.clipboard.map(([, c]) => c.end),
    );
    this.edit((p) => (this.selected = paste(p, this.clipboard, end)));
  }
  undo() {
    this.stopPlayback();
    this.editVersion++;
    this.history.undo();
    this.persistWidget();
    this.render();
  }
  redo() {
    this.stopPlayback();
    this.editVersion++;
    this.history.redo();
    this.persistWidget();
    this.render();
  }
  key(e) {
    // The editor is modal: no ComfyUI shortcut (queue, delete node, ...) may
    // act on the graph behind it, including while typing in its fields.
    e.stopPropagation();
    if (e.defaultPrevented ||
        e.target.closest("input,textarea,select,button,[role=separator],[contenteditable=true]"))
      return;
    let handled = true;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z")
      e.shiftKey ? this.redo() : this.undo();
    else if (mod && e.key.toLowerCase() === "y") this.redo();
    else if (mod && e.key.toLowerCase() === "c") this.copy();
    else if (mod && e.key.toLowerCase() === "v")
      this.edit(
        (p) => (this.selected = paste(p, this.clipboard, this.playhead)),
      );
    else if (mod && e.key.toLowerCase() === "s") this.guard(() => this.save());
    else if (e.key === "Delete" || e.key === "Backspace")
      this.edit((p) => remove(p, this.selected, this.ripple.checked));
    else if (e.key.toLowerCase() === "s")
      this.edit((p) => split(p, this.selected, this.playhead));
    else if (e.key.toLowerCase() === "j")
      this.guard(() => this.edit(p=>mergeAdjacentClips(p,this.selected)));
    else if (e.key === "ArrowLeft") this.seek(this.playhead - 1);
    else if (e.key === "ArrowRight") this.seek(this.playhead + 1);
    else if (e.key === " ") {
      this.guard(() => this.playComposition());
    } else if (e.key.toLowerCase() === "i")
      this.edit((p) => (p.selection[0] = this.playhead));
    else if (e.key.toLowerCase() === "o")
      this.edit((p) => (p.selection[1] = this.playhead + 1));
    else handled = false;
    if (handled) {
      e.preventDefault();
      e.stopPropagation();
    }
  }
  startScene(frame) {
    const count=Math.max(1,Math.ceil((this.p.selection[1]-
      this.p.selection[0])/this.p.chunk_frames));
    const hasScopedEdits=this.p.tracks.some(t=>t.clips.some(c=>
      c.chunk_prompt||c.chunk_workspace)) ||
      !!this.p.accepted_windows?.length ||
      !!Object.keys(this.p.accepted||{}).length;
    if(hasScopedEdits && !globalThis.confirm?.(
      "Start a new scene? Existing scene-specific inputs and accepted bindings " +
      "are cleared. Original media and stored take files remain. Undo restores " +
      "the previous editor state."))return;
    this.edit(p=>{
      setSceneStart(p,frame,count);
      const sources=p.tracks.flatMap(t=>t.role==="retained" &&
        !t.muted ? t.clips.filter(c=>!c.muted&&c.start<=frame&&frame<c.end &&
        p.assets.some(a=>a.id===c.asset_id&&a.video)) : []);
      if(sources.length>1)
        throw Error("Multiple original movies cover this scene; mute or trim the others before choosing the source");
      if(sources.length===1) {
        const source=sources[0];
        if(source.end<p.selection[1])
          throw Error("The original video does not cover all requested chunks; choose fewer chunks");
        const asset=p.assets.find(a=>a.id===source.asset_id);
        if(asset?.audio && !p.tracks.some(t=>t.role==="audio" && !t.muted &&
          t.clips.some(c=>!c.muted && c.audio_role==="guide" &&
            c.start<=frame && c.end>=p.selection[1]))) {
          const legacyFinal=p.tracks.some(t=>t.role==="audio"&&!t.muted &&
            t.clips.some(c=>!c.muted&&c.asset_id===source.asset_id &&
              ["final","both"].includes(c.audio_role)));
          source.audio_role=legacyFinal ? "guide" : "both";
          source.audio_padding="silence";
        }
        if(p.operation!=="recreate" && !p.tracks.some(t=>
          t.role==="video_guide"&&!t.muted&&t.clips.some(c=>!c.muted &&
          c.start<=frame && c.end>=p.selection[1]))) {
          routeClipToGuidance(p,[source.id]);
          p.selection=[frame,frame+count*p.chunk_frames];
        }
      }
      this.selected=[];
    });
    this.jumpToFrame(frame);
    this.message("Scene starts at "+time(frame)+
      ". The source movie and soundtrack remain whole; subsequent chunks are automatic.");
  }
  restoreMovie() {
    const clips=selectedClips(this.p,this.selected)
      .filter(([track])=>track.role==="retained");
    const assetId=clips.length===1 ? clips[0][1].asset_id : null;
    if(!globalThis.confirm?.(
      "Restore original movie? This resets timeline clips, cuts, overrides " +
      "and accepted placements. Registered assets and saved take files stay " +
      "available. Undo restores the previous editor state."))return;
    this.edit(p=>{restoreOriginalMovie(p,assetId);this.selected=[];});
    this.jumpToFrame(0);
    this.message("Original movie restored with synchronized source audio.");
  }
  guideSelectedClip() {
    this.edit((p) => routeClipToGuidance(p, this.selected));
    this.message(
      "H3 guidance linked to the original asset at its authored source PTS. " +
      "Render range set to this clip. Queue selection to generate; no export needed."
    );
  }
  refreshSamplerInputs() {
    if (this.closed || !this.chunkBoard) return;
    // Preserve a currently edited prompt until the field's change event
    // commits the value to its chunk track.
    if (this.chunkBoard.contains(document.activeElement) &&
        ["TEXTAREA","INPUT"].includes(document.activeElement?.tagName)) return;
    renderChunkWorkspace(this);
  }
  scheduleChunkCompile() {
    clearTimeout(this.chunkCompileTimer);
    const epoch = ++this.chunkInspectionEpoch;
    this.chunkCompileTimer = setTimeout(async () => {
      if (this.closed || epoch !== this.chunkInspectionEpoch) return;
      try {
        // For a multi-hour selected job, inspect only the visible chunk page.
        // This endpoint is for authored input summaries, not the physical
        // per-job audio encoder contract or final queue identity.
        const page = selectedChunkPage(this.p, this.chunkPage || 0, 8);
        const previewProject = page.total > 16 ? {
          ...this.p,
          selection: [page.chunks[0].start, page.chunks.at(-1).end],
          accepted_windows: [],
        } : this.p;
        const plan = await request(this.api, "/compile", {
          method: "POST", body: JSON.stringify(previewProject),
          signal: this.abort.signal,
        });
        if (this.closed || epoch !== this.chunkInspectionEpoch) return;
        this.compiledWindows = new Map(plan.jobs.flatMap((job)=>
          job.windows.map((w)=>[w.index,w])));
        updateChunkCompiledLines(this);
      } catch (error) {
        if (this.closed || epoch !== this.chunkInspectionEpoch) return;
        for (const node of this.chunkBoard.querySelectorAll("[data-compiled-chunk]"))
          node.textContent = "Cannot compile chunk inputs: " + error.message;
      }
    },120);
  }
  async inspectInputs() {
    const plan = await request(this.api, "/compile", {
      method: "POST",
      body: JSON.stringify(this.p),
    });
    this.diagnostics.textContent = JSON.stringify(
      { jobs: plan.jobs, capabilities: plan.capabilities },
      null,
      2,
    );
    this.diagnostics.parentElement.open = true;
    this.message(
      "Compiled logical inputs. Actual physical descriptors are recorded with sampled takes.",
    );
    return plan;
  }
  async queue(reroll = false) {
    await this.save();
    const snapshot = clone(this.p);
    snapshot.queued_batch_id = uid();
    const plan = await request(this.api, "/compile", {
      method: "POST",
      body: JSON.stringify(snapshot),
    });
    const samplers = this.app.graph._nodes
      .filter(
        (n) =>
          n.comfyClass === "H3ContinuumTimelineSampler" &&
          n.inputs?.find((i) => i.name === "timeline")?.link != null,
      )
      .filter((n) => {
        const link =
          this.app.graph.links[
            n.inputs.find((i) => i.name === "timeline").link
          ];
        return link?.origin_id === this.node.id;
      });
    if (samplers.length !== 1)
      throw new Error(
        "Connect this project to one Timeline Sampler before queueing",
      );
    const sampler = samplers[0];
    // Core's seed controls run around app.queuePrompt. This editor submits
    // its own frozen batch through api.queuePrompt, so run the same widget
    // lifecycle once for the entire batch, keeping its causal jobs coherent.
    const queuedNodes = [...this.app.graph._nodes];
    for (const node of queuedNodes)
      for (const widget of node.widgets || [])
        widget.beforeQueued?.({ isPartialExecution: false });
    const frozen = await this.app.graphToPrompt();
    frozen.output[String(this.node.id)].inputs.project_json =
      JSON.stringify(snapshot);
    const graphNode = frozen.workflow?.nodes?.find(
      (n) => n.id === this.node.id,
    );
    if (graphNode?.widgets_values)
      graphNode.widgets_values[0] = JSON.stringify(snapshot);
    let submitted = 0;
    try {
      for (let i = 0; i < plan.jobs.length; i++) {
        const payload = clone(frozen);
        const actual = payload.output[String(sampler.id)].inputs;
        const job = plan.jobs[i];
        actual.job_index = i;
        if (reroll) {
          // A job continuing a stored parent state can only restart at its
          // first chunk.
          const resumes = job.requires_previous_job || job.resume_parent_take;
          actual.regenerate_from =
            i === 0 && !resumes
              ? Math.max(
                  1,
                  Math.floor(snapshot.selection[0] / snapshot.chunk_frames) -
                    job.start_chunk +
                    1,
                )
              : 1;
          // Native storage allocates the next nonce under its run lock. A
          // restored or concurrently used widget counter cannot safely do so.
          actual.reroll_nonce = 0;
        }
        const serialized = payload.workflow?.nodes?.find(
          (n) => n.id === sampler.id,
        );
        if (serialized?.widgets_values) {
          for (const key of ["job_index", "regenerate_from", "reroll_nonce"]) {
            const index = sampler.widgets
              .filter((w) => w.serialize !== false)
              .findIndex((w) => w.name === key);
            if (index >= 0 && key in actual)
              serialized.widgets_values[index] = actual[key];
            if (serialized.widgets_values_named && key in actual)
              serialized.widgets_values_named[key] = actual[key];
          }
        }
        // Number 0 appends in submission order; -1 would queue each later job
        // ahead of its predecessor.
        let queued;
        try {
          // State Manager patches api.queuePrompt to flush persistent writes
          // and reject unsaved managed prompt text. Never bypass that guard.
          queued = await this.api.queuePrompt(0, payload);
        } catch (error) {
          const reason = error?.code === "DSM_UNSAVED_MANAGED_TEXT"
            ? "State Manager has unsaved connected Text Box text. Close Timeline Studio, open the State Manager, select the intended persistent prompt preset, click Save connected and wait for the save to finish. Then reopen Studio and queue again."
            : error?.message || "Queue submission failed";
          throw new Error(
            "Queue stopped: " + submitted + " of " + plan.jobs.length +
            " job(s) confirmed. " + reason,
            { cause: error },
          );
        }
        submitted++;
        this.queuedIds.add(queued.prompt_id);
        this.persistView();
      }
    } finally {
      if (submitted) {
        for (const node of queuedNodes)
          if (String(node.id) in frozen.output)
            for (const widget of node.widgets || [])
              widget.afterQueued?.({ isPartialExecution: false });
        this.app.graph.setDirtyCanvas?.(true, true);
      }
    }
    this.message(
      `${plan.jobs.length} immutable job snapshot(s) queued. Connect decoded assembly to Save Take for review.`,
    );
  }
  async cancelSampling() {
    const response = await this.api.fetchApi("/queue");
    if (!response.ok) throw new Error("Cannot read the sampling queue");
    let queue = await response.json();
    const live = new Set(
      [...(queue.queue_pending || []), ...(queue.queue_running || [])].map(
        (row) => row[1],
      ),
    );
    this.queuedIds = new Set([...this.queuedIds].filter((id) => live.has(id)));
    const pending = (queue.queue_pending || [])
      .filter((row) => this.queuedIds.has(row[1]))
      .map((row) => row[1]);
    if (pending.length) {
      const removed = await this.api.fetchApi("/queue", {
        method: "POST",
        body: JSON.stringify({ delete: pending }),
      });
      if (!removed.ok)
        throw new Error("Could not cancel pending timeline jobs");
      // A pending job can start while DELETE is in flight. Re-read before
      // deciding whether its execution needs an interrupt.
      const refreshed = await this.api.fetchApi("/queue");
      if (!refreshed.ok) throw new Error("Cannot verify sampling cancellation");
      queue = await refreshed.json();
    }
    const active = (queue.queue_running || []).some((row) =>
      this.queuedIds.has(row[1]),
    );
    if (active) await this.api.interrupt();
    const remaining = new Set(
      [...(queue.queue_pending || []), ...(queue.queue_running || [])].map(
        (row) => row[1],
      ),
    );
    this.queuedIds = new Set(
      [...this.queuedIds].filter((id) => remaining.has(id)),
    );
    this.persistView();
    this.message(
      active
        ? "Timeline sampling cancellation requested"
        : pending.length
          ? "Pending timeline jobs cancelled"
          : "No active job queued by this editor",
    );
  }
  async review() {
    await this.save();
    const records = await request(this.api, "/takes/" + this.p.project_id);
    this.inspector.replaceChildren(
      el("h3", { text: "Generated alternatives" }),
    );
    for (const r of records) {
      const rejected = this.p.rejected?.includes(r.id);
      const card = el(
        "section",
        { class: "h3ts-take" },
        el("strong", { text: r.id.slice(0, 8) }),
        el("p", {
          text: `${time(r.range[0])}–${time(r.range[1])} · ${r.stale ? "STALE" : r.status}${rejected ? " · rejected" : ""}`,
        }),
      );
      if (r.rendition) {
        card.append(
          button("Preview B", () => {
            this.video.src =
              BASE + "/proxy/" + r.rendition.asset.id + "?duration=4";
            this.video.hidden = false;
            this.image.hidden = true;
            this.previewClip = null;
            this.video.play().catch(() => {});
          }),
        );
        if (!r.stale)
          card.append(
            button("Accept / select", () =>
              this.guard(() => this.selectTake(r, "accept")),
            ),
          );
        card.append(
          button("Reject", () =>
            this.guard(() => this.selectTake(r, "reject")),
          ),
        );
      }
      card.append(
        button("Inspect actual inputs", () => {
          this.diagnostics.textContent = JSON.stringify(
            r.model_inputs || [],
            null,
            2,
          );
          this.diagnostics.parentElement.open = true;
        }),
      );
      this.inspector.append(card);
    }
  }
  async selectTake(record, action) {
    await this.save();
    const p = await request(this.api, "/take/select", {
      method: "POST",
      body: JSON.stringify({
        project_id: this.p.project_id,
        take_id: record.id,
        action,
        expected_revision: this.serverRevision,
      }),
    });
    this.history = new History(p);
    this.serverRevision = p.revision;
    this.persistWidget();
    this.render();
    this.message(
      action === "accept"
        ? "Take selected; dependent later choices invalidated"
        : "Take rejected",
    );
  }
  async export(codec) {
    if (this.exportPending) {
      this.message("An export is already running; use Cancel export if needed");
      return;
    }
    this.exportPending = true;
    const requestedResolution = this.exportResolutionChoice || "timeline";
    this.exportLink.hidden = true;
    this.exportLink.textContent = "";
    this.exportStatus.textContent = "Preparing export…";
    try {
      // The yellow-highlighted clip is the requested snippet. 'Render in/out'
      // describes a separate range and must not override the highlighted
      // clip without the user's knowledge.
      const target = exportTarget(this.p, this.selected);
      const {start, stop} = target;
      if (target.kind === "clip" &&
          (this.p.selection[0] !== start || this.p.selection[1] !== stop)) {
        // Commit the actual output range so both the input fields and backend
        // agree with what was highlighted at export time.
        this.edit((p) => { p.selection = [start, stop]; });
      }
      await this.save();
      // Close during an asynchronous save must not start a background export
      // after the user has dismissed the editor.
      if (this.closed) return;
      // An explicit clip source is still mapped through source_in and speed,
      // never confused with project time zero.
      if (!Number.isSafeInteger(start) || !Number.isSafeInteger(stop) ||
          start < 0 || stop <= start || stop > this.p.duration_frames) {
        throw new Error("Invalid Render [in,out) selection for export");
      }
      const frames = stop - start;
      const descriptor = (target.kind === "clip"
        ? "highlighted clip · " : "render range · ") +
        "frames " + start + "–" + stop +
        " (" + (frames / 24).toFixed(2) + " seconds)" +
        (target.kind === "clip"
          ? " · source in " + (target.sourceIn ?? "0") + "s"
          : "");
      this.exportStatus.textContent = "Exporting selection · " + descriptor;
      const result = await request(this.api, "/export", {
        method: "POST",
        body: JSON.stringify({
          project: this.p, codec, resolution: requestedResolution,
          render_range: [start, stop],
          selected_clip_ids: target.clipIds,
        }),
      });
      this.exportId = result.id;
      const visual = target.clipIds.flatMap((id) =>
        this.p.tracks.flatMap((t) =>
          ["retained", "generated"].includes(t.role)
            ? t.clips.filter((c) => c.id === id) : [],
        ),
      );
      const nativeAsset = visual.length === 1
        ? this.p.assets?.find((a) => a.id === visual[0].asset_id)
        : null;
      const wrongDimensions =
        requestedResolution === "source" &&
        nativeAsset?.video && (
          result.width !== nativeAsset.video.width ||
          result.height !== nativeAsset.video.height
        );
      if (result.resolution !== requestedResolution ||
          !Number.isSafeInteger(result.width) ||
          !Number.isSafeInteger(result.height) ||
          result.width <= 0 || result.height <= 0 || wrongDimensions) {
        await request(this.api, "/export/" + result.id + "/cancel", {
          method: "POST",
        }).catch(() => {});
        throw new Error(
          "Server ignored export resolution or source pixel dimensions; " +
          "export cancelled. Update the Patcher overlay and restart ComfyUI."
        );
      }
      const pixels = result.width + "×" + result.height;
      const sizeDescription = (result.resolution === "source"
        ? "source-size" : "timeline-size") + " " + pixels;
      if (this.closed) {
        // The server may accept the job while the editor closes; cancel the
        // newly created export even if close() happened before its ID arrived.
        await request(this.api, "/export/" + result.id + "/cancel", {
          method: "POST",
        }).catch(() => {});
        return;
      }
      if (result.total_frames !== frames ||
          result.render_range?.[0] !== start ||
          result.render_range?.[1] !== stop) {
        // The server is stale or wrong. Cancel it rather than leaving a
        // potentially hour-long export running after frontend rejection.
        await request(this.api, "/export/" + result.id + "/cancel", {
          method: "POST",
        }).catch(() => {});
        throw new Error(
          "Server ignored selected in/out frames; export cancelled. " +
          "Update the Patcher overlay and restart ComfyUI."
        );
      }
      const poll = async () => {
        if (this.closed) return;
        const status = await request(this.api, "/export/" + result.id);
        if (status.status === "running") {
          const done = Number(status.frames_done) || 0;
          const total = Number(status.total_frames);
          if (total !== frames ||
              status.render_range?.[0] !== start ||
              status.render_range?.[1] !== stop ||
              status.resolution !== result.resolution ||
              status.width !== result.width ||
              status.height !== result.height) {
            await request(this.api, "/export/" + result.id + "/cancel", {
              method: "POST",
            }).catch(() => {});
            throw new Error(
              "Export status changed its selected frame range; job cancelled"
            );
          }
          const percent = Math.min(100, Math.round(100 * done / frames));
          this.exportStatus.textContent =
            "Export " + sizeDescription + " · " + descriptor + " · " +
            (status.stage || "running") + " · " + percent +
            "% · " + ((status.elapsed_seconds || 0)).toFixed(1) + " s";
          this.exportTimer = setTimeout(() => this.guard(poll), 1000);
          return;
        }
        this.exportPending = false;
        if (status.status === "complete") {
          const url = BASE + "/export/" + result.id + "/file";
          this.exportLink.href = url;
          this.exportLink.download = status.filename;
          this.exportLink.textContent = "Download " + status.filename;
          this.exportLink.hidden = false;
          this.exportStatus.textContent = "Exported " + sizeDescription + " · " + descriptor +
            " · " + ((status.elapsed_seconds || 0)).toFixed(1) + " s";
          this.message("Export saved in ComfyUI output: " + status.filename +
            ". This file is not a model input; use Guide H3 with selected clip or Recreate.");
        } else {
          this.exportStatus.textContent =
            "Export " + (status.status || "failed") + ": " +
            (status.error || "Unknown error");
          this.message(this.exportStatus.textContent, true);
        }
      };
      await poll();
    } catch (error) {
      this.exportPending = false;
      this.exportStatus.textContent = "Export failed: " + error.message;
      throw error;
    }
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    // Remove the modal FIRST. No video decoder, canvas refresh, workflow
    // serialization, or hanging media request may prevent Close from working.
    try { this.root.close?.(); } catch { /* Already dismissed. */ }
    this.root.remove();
    try { this.onClose?.(); } catch (e) { console.warn(e); }

    // Most edits already persist synchronously. Closing only stores view
    // state; persistWidget() also refreshes a large sampler graph and can
    // freeze a busy ComfyUI canvas, so never call it on Close.
    try { this.persistView(); } catch (e) { console.warn(e); }
    const activeExport = this.exportPending ? this.exportId : null;
    this.exportPending = false;
    clearTimeout(this.exportTimer);
    clearTimeout(this.seekTimer);
    clearTimeout(this.chunkCompileTimer);
    clearInterval(this.conveyorPoll);
    this.chunkInspectionEpoch++;
    this.filmTileCache?.clear();
    try { this.abort.abort(); } catch { /* no pending requests */ }
    try { this.resize?.disconnect(); } catch {}
    try { this.layoutObserver?.disconnect(); } catch {}
    try { cancelAnimationFrame(this.drawPending); } catch {}
    try {
      this.stopPlayback();
      this.video.removeAttribute("src");
      // Do not call HTMLMediaElement.load() synchronously during Close:
      // tearing down a large remote video resource can stall this callback.
    } catch (e) { console.warn(e); }
    if (activeExport) {
      // The user is closing an unusable editor, not electing to keep a large
      // background export saturating WSL. Cancel it best-effort.
      void request(this.api, "/export/" + activeExport + "/cancel", {
        method: "POST",
      }).catch((e) => console.warn("Export cancellation:", e));
    }
  }
}
