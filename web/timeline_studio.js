import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { TimelineEditor } from "./timeline/editor.js";
import { uid } from "./timeline/core.js";
const editors = new Map();
function summary(node) {
  const w = node.widgets?.find((x) => x.name === "project_json");
  if (!w) return;
  try {
    const p = JSON.parse(w.value);
    node.title = "Timeline Studio · " + p.name;
    node.__timelineSummary = [
      `${p.width}×${p.height} · ${(p.duration_frames / 24).toFixed(2)} s · 24 fps`,
      `${p.chunk_frames} f chunks · render ${(p.selection[0] / 24).toFixed(2)}–${(p.selection[1] / 24).toFixed(2)} s`,
    ];
  } catch {
    node.__timelineSummary = ["Open Timeline Studio to inspect project data"];
  }
}
function projectId(node) {
  try {
    return JSON.parse(
      node.widgets?.find((x) => x.name === "project_json")?.value,
    ).project_id;
  } catch {
    return null;
  }
}
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
  delete p.ui;
  if (node.properties) delete node.properties.h3_timeline_view;
  for (const track of p.tracks)
    if (track.role === "generated") {
      track.role = "retained";
      track.name = "Copied footage";
      for (const clip of track.clips) delete clip.take_id;
    }
  w.value = JSON.stringify(p);
  summary(node);
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
      cloning = nodeType.prototype.clone,
      draw = nodeType.prototype.onDrawForeground;
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
      this.setSize([330, 145]);
    };
    nodeType.prototype.onDrawForeground = function (ctx) {
      draw?.apply(this, arguments);
      if (this.flags?.collapsed) return;
      ctx.save();
      ctx.fillStyle = "#b7c6d6";
      ctx.font = "11px sans-serif";
      for (const [i, line] of (this.__timelineSummary || []).entries())
        ctx.fillText(line, 10, this.size[1] - 30 + i * 16, this.size[0] - 20);
      ctx.restore();
    };
    nodeType.prototype.onRemoved = function () {
      editors.get(this)?.close();
      return removed?.apply(this, arguments);
    };
    const configured = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = configured?.apply(this, arguments);
      // Pasting one clipboard twice restores the same serialized project.
      const mine = projectId(this);
      if (
        mine &&
        (this.graph?._nodes || []).some(
          (n) =>
            n !== this &&
            n.comfyClass === "H3ContinuumTimelineStudio" &&
            projectId(n) === mine,
        )
      )
        independent(this);
      summary(this);
      return result;
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
      summary(node);
    }
  },
  loadedGraphNode(node) {
    if (node.comfyClass === "H3ContinuumTimelineStudio") summary(node);
  },
  beforeConfigureGraph(graphData) {
    for (const editor of editors.values()) editor.close();
    editors.clear();
    // The first Timeline schema had no seed control widget. Insert its
    // fixed default before LiteGraph assigns positional serialized values.
    for (const node of graphData?.nodes || []) {
      if (
        node.type === "H3ContinuumTimelineSampler" &&
        node.widgets_values?.length === 7
      )
        node.widgets_values.splice(2, 0, "fixed");
    }
  },
});
