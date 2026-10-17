import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { TimelineEditor } from "./timeline/editor.js";
import { uid } from "./timeline/core.js";
const editors = new Map();
function independent(node) {
  const w = node.widgets?.find((x) => x.name === "project_json");
  if (!w) return;
  const p = JSON.parse(w.value);
  p.project_id = uid();
  delete p.revision;
  delete p.accepted;
  delete p.accepted_windows;
  delete p.rejected;
  p.takes = [];
  for (const track of p.tracks)
    if (track.role === "generated") {
      track.role = "retained";
      track.name = "Copied footage";
      for (const clip of track.clips) delete clip.take_id;
    }
  w.value = JSON.stringify(p);
}
const style = document.createElement("link");
style.rel = "stylesheet";
style.href = new URL("./timeline/studio.css", import.meta.url).href;
document.head.append(style);
app.registerExtension({
  name: "H3Continuum.TimelineStudio",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "H3ContinuumTimelineStudio") return;
    const created = nodeType.prototype.onNodeCreated,
      removed = nodeType.prototype.onRemoved,
      cloning = nodeType.prototype.clone;
    nodeType.prototype.onNodeCreated = function () {
      created?.apply(this, arguments);
      const w = this.widgets.find((x) => x.name === "project_json");
      if (w) {
        w.hidden = true;
        w.type = "converted-widget";
        w.computeSize = () => [0, -4];
        if (w.inputEl) w.inputEl.style.display = "none";
      }
      independent(this);
      this.addWidget("button", "Open Timeline Studio", null, () => {
        if (editors.has(this)) {
          editors.get(this).root.focus();
          return;
        }
        try {
          const editor = new TimelineEditor({
            node: this,
            widget: w,
            api,
            app,
          });
          editors.set(this, editor);
          editor.onClose = () => editors.delete(this);
        } catch (e) {
          console.error("Timeline Studio editor failed", e);
          app.extensionManager?.toast?.add({
            severity: "error",
            summary: "Timeline Studio",
            detail: e.message,
          });
        }
      });
      this.setSize([330, 100]);
    };
    nodeType.prototype.onRemoved = function () {
      editors.get(this)?.close();
      return removed?.apply(this, arguments);
    };
    nodeType.prototype.clone = function () {
      const copy = cloning?.apply(this, arguments);
      if (copy) independent(copy);
      return copy;
    };
  },
  afterConfigureGraph() {
    const seen = new Set();
    for (const node of app.graph?._nodes || []) {
      if (node.comfyClass !== "H3ContinuumTimelineStudio") continue;
      const w = node.widgets?.find((x) => x.name === "project_json");
      if (!w) continue;
      const p = JSON.parse(w.value);
      if (seen.has(p.project_id)) independent(node);
      seen.add(JSON.parse(w.value).project_id);
    }
  },
  beforeConfigureGraph() {
    for (const editor of editors.values()) editor.close();
    editors.clear();
  },
});
