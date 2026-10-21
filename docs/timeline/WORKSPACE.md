# Timeline workspace (v26)

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

## Generate from reference images and prompts

Keep Timeline Studio connected to supply duration and chunk timing. A project
can contain no source media. Connect the desired reference images and prompt
to the sampler, or write prompts in Studio.

To ignore existing source footage and its soundtrack, use Scene settings:

- **H3 model video conditioning → Off**.
- **H3 audio generation → Generated · no driving audio**.
- **Final audio for new takes → Generated H3 audio**.

These controls disable source-video conditioning, driving-audio conditioning
and original-soundtrack replacement independently. Registered media can remain
in the project for preview/editing. Image references and independent voice
references retain their authored selections. H3 still generates its own audio.
Queue a new take after changing conditioning; saved takes retain their recorded
settings and soundtrack.

Source Audio Locked requires source audio and controls audio denoising; it does
not constrain head/body pose, camera motion or shot boundaries.
