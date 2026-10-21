# Timeline workspace (v25)

The editor has two main work areas: preview on the left and one selected H3
chunk on the right. Previous/Next chunk and the chunk-number field navigate the
selected scene without changing its inputs. Each chunk keeps its exact source
clock, prompt, Conveyor reference selections and media routes.

**Settings** opens an independently scrolling panel with four tabs:

- **Scene**: source start, chunk count/duration, video/audio conditioning,
  final soundtrack choice, connected sampler settings and view options.
- **Global**: prompt ownership and the shared prompt.
- **Media**: registered originals and relinking.
- **Inspect**: project/clip controls, advanced edits, manual ranges and input
  diagnostics.

Drag the vertical divider between preview and chunks or the horizontal divider
above the track toolbar. Arrow keys work on focused dividers. **Focus chunks**
and **Focus preview** use the workspace width for one task. **Reset layout**
restores defaults. **Timeline** hides the lower tracks to reclaim workspace height. Old three-band sizes are discarded on migration; project
inputs are unchanged. View state remains in node properties, outside sampling
hashes. The source movie remains horizontally navigable below the workspace.

## Capture

**Screenshot** in the header saves a PNG of the visible editor. In the matching
v25 Windows Desktop it captures only the application content via Electron's
`capturePage`; it cannot capture other apps or enumerate displays. An ordinary
browser opens its screen/tab chooser and immediately stops the selected stream
after taking one frame. The browser needs screen-sharing support and a secure
context. Nothing is uploaded or sent automatically; attach the downloaded PNG
to a report yourself.

**Save frame** under the preview saves the currently displayed video/still as
PNG at its displayed resource dimensions. This is a preview capture, not a
lossless source-frame extraction or a new Image Conveyor reference. It does not
change the project or model inputs.

## Mary's reported motion

The supplied October 21 output has working lip sync according to the user, but
additional head/gaze movement. Side-by-side frame inspection confirms different
pose/framing from the original; the screenshot has model video conditioning
Off. Source Audio Locked constrains audio targets and downstream audio masks,
not video pose, expression, camera or shot boundaries. This is a plausible
explanation for free motion, not proof of the specific cause of head shaking.
No sampler or prompt rewrite is presented as a motion fix. A controlled GPU
comparison needs the completed workflow, exact prompts/references/seed and
conditioning settings, plus source/output video. This host has no CUDA GPU.

## Verification boundaries

`tests/timeline_workspace_browser.cjs` runs the actual editor modules/CSS in
Chromium at 1920×1200, 1366×768 and 1000×700, with mocked backend responses. It
checks independent chunk scrolling, focus and settings, view-only resizing,
chunk navigation/prompt editing, and PNG downloads. CI uploads its screenshots.
This establishes browser layout/interaction behavior, not installed ComfyUI,
physical Windows capture quality or trained H3 motion/lip-sync acceptance.
