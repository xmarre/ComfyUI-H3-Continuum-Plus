// Run against tools/check_timeline_comfy.py's real ComfyUI server.
const assert = require("node:assert/strict");
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.H3_TIMELINE_CHROMIUM || undefined,
    args: ["--no-sandbox", "--disable-gpu"],
    headless: true,
  });
  const page = await browser.newPage({
    viewport: { width: 1600, height: 1000 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") console.error("BROWSER", m.text());
  });
  try {
    await page.goto(process.env.H3_TIMELINE_TEST_URL, {
      waitUntil: "domcontentloaded",
    });
    await page.waitForFunction(
      () =>
        !!window.comfyAPI?.app?.app?.graph &&
        !!globalThis.LiteGraph?.registered_node_types
          ?.H3ContinuumTimelineStudio,
      null,
      { timeout: 30000 },
    );
    await page.waitForTimeout(1500);
    await page.keyboard.press("Escape");
    await page.evaluate(() => {
      for (const dialog of document.querySelectorAll("dialog[open]"))
        dialog.close();
    });
    await page.evaluate(async (project) => {
      const app = window.comfyAPI.app.app;
      const node = LiteGraph.createNode("H3ContinuumTimelineStudio");
      app.graph.add(node);
      node.widgets.find((w) => w.name === "project_json").value =
        JSON.stringify(project);
      globalThis.h3TestNode = node;
      node.widgets.find((w) => w.name === "Open Timeline Studio").callback();
    }, JSON.parse(process.env.H3_TIMELINE_TEST_PROJECT));
    await page.locator(".h3ts").waitFor({ state: "visible" });
    assert.equal(await page.locator(".h3ts-clip").count(), 1);
    await page
      .getByRole("button", { name: "Inspect inputs", exact: true })
      .click();
    await page.waitForFunction(() =>
      document
        .querySelector(".h3ts-diagnostics")
        .textContent.includes("generation_hash"),
    );
    const beforeView = await page.evaluate(
      () => h3TestNode.widgets.find((w) => w.name === "project_json").value,
    );
    // Verify the real browser layout: the preview is not crushed by the
    // chunk workspace, and each interactive splitter persists view-only size.
    const firstLayout=await page.evaluate(() => ({
      main:document.querySelector(".h3ts > main").getBoundingClientRect().height,
      chunk:document.querySelector(".h3ts-chunk-workspace").getBoundingClientRect().height,
      timeline:document.querySelector(".h3ts-scroll").getBoundingClientRect().height,
    }));
    assert.ok(firstLayout.main>=150,JSON.stringify(firstLayout));
    assert.ok(firstLayout.chunk>=150,JSON.stringify(firstLayout));
    assert.ok(firstLayout.timeline>=85,JSON.stringify(firstLayout));
    const previewGrip=page.getByRole("separator",{
      name:"Resize media preview and upper workspace height",
    });
    const gripBox=await previewGrip.boundingBox();
    await page.mouse.move(gripBox.x+gripBox.width/2,gripBox.y+gripBox.height/2);
    await page.mouse.down();
    await page.mouse.move(gripBox.x+gripBox.width/2,
      gripBox.y+gripBox.height/2+65,{steps:5});
    await page.mouse.up();
    const afterGrip=await page.evaluate(() => ({
      preview:document.querySelector(".h3ts > main").getBoundingClientRect().height,
      timeline:document.querySelector(".h3ts-scroll").getBoundingClientRect().height,
      saved:h3TestNode.properties.h3_timeline_view?.layout?.preview,
    }));
    assert.ok(afterGrip.preview>=firstLayout.main+20,JSON.stringify(afterGrip));
    assert.ok(afterGrip.timeline>=85,JSON.stringify(afterGrip));
    assert.ok(afterGrip.saved>firstLayout.main,JSON.stringify(afterGrip));
    const leftGrip=page.getByRole("separator",{name:"Resize media library width"});
    await leftGrip.focus();
    await page.keyboard.press("ArrowRight");
    assert.ok(
      await page.evaluate(() =>
        h3TestNode.properties.h3_timeline_view.layout.left>190),
    );
    await page.getByRole("slider",{name:"Reference thumbnail size"})
      .fill("150");
    assert.equal(
      await page.evaluate(() =>
        h3TestNode.properties.h3_timeline_view.layout.thumb),
      150,
    );
    assert.equal(
      await page.evaluate(() =>
        h3TestNode.widgets.find(w=>w.name==="project_json").value),
      beforeView,
    );
    await page.getByRole("button", { name: "+ Zoom", exact: true }).click();
    await page.evaluate(() => {
      document.querySelector(".h3ts-scroll").scrollLeft = 40;
      document.querySelector(".h3ts-scroll").dispatchEvent(new Event("scroll"));
    });
    // View state must not change the serialized prompt input.
    assert.equal(
      await page.evaluate(
        () => h3TestNode.widgets.find((w) => w.name === "project_json").value,
      ),
      beforeView,
    );
    assert.ok(
      await page.evaluate(
        () => h3TestNode.properties.h3_timeline_view?.zoom > 0,
      ),
    );
    // Select source clip, split at frame 24 with the keyboard, undo and redo.
    await page.locator(".h3ts-clip").first().click();
    await page.evaluate(() => {
      const root = document.querySelector(".h3ts");
      root.focus();
    });
    for (let i = 0; i < 24; i++) await page.keyboard.press("ArrowRight");
    await page.keyboard.press("s");
    assert.equal(await page.locator(".h3ts-clip").count(), 2);
    await page.keyboard.press("Control+z");
    assert.equal(await page.locator(".h3ts-clip").count(), 1);
    await page.keyboard.press("Control+Shift+z");
    assert.equal(await page.locator(".h3ts-clip").count(), 2);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await page.waitForFunction(() =>
      document
        .querySelector(".h3ts-status")
        .textContent.includes("Project saved"),
    );
    await page.getByRole("button", { name: "Reload", exact: true }).click();
    await page.waitForFunction(() =>
      document
        .querySelector(".h3ts-status")
        .textContent.includes("Saved project loaded"),
    );
    assert.equal(await page.locator(".h3ts-clip").count(), 2);
    // Editing text must not trigger split shortcuts.
    await page
      .getByRole("textbox", { name: "Project name", exact: true })
      .fill("Studio acceptance");
    await page.keyboard.press("s");
    assert.equal(await page.locator(".h3ts-clip").count(), 2);
    await page.evaluate(() => {
      document.querySelector(".h3ts details").open = false;
    });
    await page
      .getByRole("textbox", { name: "Project name", exact: true })
      .fill("Timeline Studio · source editing");
    const layout = await page.evaluate(() => ({
      video: document.querySelector(".h3ts video").getBoundingClientRect()
        .bottom,
      nav: document.querySelector(".h3ts-viewer>nav").getBoundingClientRect()
        .top,
    }));
    assert.ok(
      layout.video <= layout.nav + 2,
      "Preview must leave its transport controls visible",
    );
    await page.locator(".h3ts").screenshot({
      path: process.env.H3_TIMELINE_SCREENSHOT || "/tmp/timeline-studio.png",
    });
    await page
      .locator(".h3ts")
      .getByRole("button", { name: "Close", exact: true })
      .click();
    assert.equal(await page.locator(".h3ts").count(), 0);
    const clonedIds = await page.evaluate(() => {
      const copy = h3TestNode.clone();
      return [
        JSON.parse(
          h3TestNode.widgets.find((w) => w.name === "project_json").value,
        ).project_id,
        JSON.parse(copy.widgets.find((w) => w.name === "project_json").value)
          .project_id,
      ];
    });
    assert.notEqual(...clonedIds);
    const serialized = await page.evaluate(async () => {
      const app = window.comfyAPI.app.app;
      const out = LiteGraph.createNode("H3ContinuumTimelineExport");
      app.graph.add(out);
      h3TestNode.connect(0, out, 0);
      const prompt = await app.graphToPrompt();
      return JSON.parse(
        prompt.output[String(h3TestNode.id)].inputs.project_json,
      );
    });
    assert.ok(serialized.project_id);
    // Reopen from the serialized node, then workflow switch must dispose it.
    await page.evaluate(() =>
      h3TestNode.widgets
        .find((w) => w.name === "Open Timeline Studio")
        .callback(),
    );
    await page.locator(".h3ts").waitFor({ state: "visible" });
    const workflow = JSON.parse(
      require("node:fs").readFileSync(
        require("node:path").join(
          __dirname,
          "../examples/timeline/Timeline_Studio_Generate.json",
        ),
        "utf8",
      ),
    );
    const samplerInputs = await page.evaluate(async (workflow) => {
      const app = window.comfyAPI.app.app;
      await app.loadGraphData(workflow);
      return (await app.graphToPrompt()).output["8"].inputs;
    }, workflow);
    assert.equal(samplerInputs.job_index, 0);
    assert.equal(samplerInputs.base_seed, 20261017);
    assert.equal(samplerInputs.width, 768);
    assert.equal(samplerInputs.height, 512);
    assert.equal(samplerInputs.audio_continuity, true);
    assert.equal(samplerInputs.run_storage, "Save + Auto Resume");
    assert.equal(samplerInputs.reference_size, "Match Output");
    assert.equal(samplerInputs.video_reference_size, "Efficient - 0.4 MP");
    assert.equal(samplerInputs.conditioning_lookahead, true);
    assert.equal(samplerInputs.continuity, "Strong — 39 frames");
    assert.equal(
      samplerInputs.continuation_method,
      "Native Masked — exact continuation (Recommended)",
    );
    assert.equal(await page.locator(".h3ts").count(), 0);
    const parity = await page.evaluate(async (workflow) => {
      const app = window.comfyAPI.app.app;
      const sampler = app.graph.getNodeById(8);
      const names = sampler.inputs.map((input) => input.name);
      const outputNames = sampler.outputs.map((output) => output.name);
      const widgetNames = sampler.widgets.map((widget) => widget.name);
      for (const [name, value] of [
        ["chunks", 2],
        ["chunk_seconds", 5],
        ["width", 960],
        ["height", 704],
      ]) {
        const widget = sampler.widgets.find((w) => w.name === name);
        widget.value = value;
        widget.callback(value);
      }
      const studio = app.graph._nodes.find(
        (n) => n.comfyClass === "H3ContinuumTimelineStudio",
      );
      const project = JSON.parse(
        studio.widgets.find((w) => w.name === "project_json").value,
      );
      const compiled = await (
        await window.comfyAPI.api.api.fetchApi(
          "/h3_continuum/timeline/compile",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(project),
          },
        )
      ).json();
      const set = (name, value) => {
        sampler.widgets.find((w) => w.name === name).value = value;
      };
      set("audio_continuity", false);
      set("reference_size", "Max Identity");
      set("video_reference_size", "Balanced - 0.6 MP");
      set("conditioning_lookahead", false);
      set("run_storage", "Off");
      const serialized = app.graph.serialize();
      await app.loadGraphData(serialized);
      const roundtrip = (await app.graphToPrompt()).output["8"].inputs;
      // The first released PR schema stored just seven sampler widgets.
      const old = structuredClone(workflow);
      delete old.nodes.find((n) => n.id === 8).widgets_values_named;
      old.nodes.find((n) => n.id === 8).widgets_values = [
        0,
        123,
        "Strong — 39 frames",
        "Native Masked — exact continuation (Recommended)",
        2,
        17,
        false,
      ];
      await app.loadGraphData(old);
      const migrated = (await app.graphToPrompt()).output["8"].inputs;
      await app.loadGraphData(workflow);
      return {
        names,
        outputNames,
        widgetNames,
        project,
        compiled,
        roundtrip,
        migrated,
      };
    }, workflow);
    for (const name of [
      "reference_image_1",
      "reference_image_2",
      "reference_image_3",
      "reference_video_1",
      "driving_audio",
      "sequence_prompt",
    ])
      assert.ok(parity.names.includes(name), name);
    assert.ok(parity.outputNames.includes("driving_audio"));
    assert.deepEqual(parity.widgetNames.slice(0, 8), [
      "prompt_mode",
      "chunks",
      "chunk_seconds",
      "width",
      "height",
      "continuity",
      "base_seed",
      "control_after_generate",
    ]);
    assert.equal(parity.project.width, 960);
    assert.equal(parity.project.height, 704);
    assert.equal(parity.project.chunk_frames, 120);
    assert.deepEqual(parity.project.selection, [0, 240]);
    assert.equal(parity.compiled.jobs[0].windows.length, 2);
    assert.equal(parity.roundtrip.width, 960);
    assert.equal(parity.roundtrip.height, 704);
    assert.equal(parity.roundtrip.audio_continuity, false);
    assert.equal(parity.roundtrip.reference_size, "Max Identity");
    assert.equal(parity.roundtrip.video_reference_size, "Balanced - 0.6 MP");
    assert.equal(parity.roundtrip.conditioning_lookahead, false);
    assert.equal(parity.roundtrip.run_storage, "Off");
    assert.equal(parity.migrated.base_seed, 123);
    assert.equal(parity.migrated.regenerate_from, 2);
    assert.equal(parity.migrated.reroll_nonce, 17);
    assert.equal(parity.migrated.width, 768);
    assert.equal(parity.migrated.height, 512);
    const references = await page.evaluate(async () => {
      const app = window.comfyAPI.app.app;
      const sampler = app.graph.getNodeById(8);
      const image = LiteGraph.createNode("EmptyImage");
      app.graph.add(image);
      for (let i = 3; i <= 8; i++) {
        const slot = sampler.inputs.findIndex(
          (input) => input.name === `reference_image_${i}`,
        );
        if (slot < 0) throw new Error(`Missing dynamic reference socket ${i}`);
        image.connect(0, sampler, slot);
        await new Promise(queueMicrotask);
      }
      const dimensionNodes = {};
      for (const [name, value] of [
        ["width", 1024],
        ["height", 768],
        ["base_seed", 123],
      ]) {
        const integer = LiteGraph.createNode("PrimitiveInt");
        app.graph.add(integer);
        integer.widgets.find((w) => w.name === "value").value = value;
        const slot = sampler.inputs.findIndex((input) => input.name === name);
        if (slot < 0) throw new Error(`Missing numeric input ${name}`);
        integer.connect(0, sampler, slot);
        dimensionNodes[name] = String(integer.id);
        await new Promise(queueMicrotask);
      }
      const serialized = app.graph.serialize();
      await app.loadGraphData(serialized);
      const restored = app.graph.getNodeById(8);
      const prompt = await app.graphToPrompt();
      return {
        references: restored.inputs
          .filter((input) => input.name.startsWith("reference_image_"))
          .map((input) => [input.name, input.link != null]),
        names: restored.inputs.map((input) => input.name),
        dimensionNodes,
        inputs: prompt.output["8"].inputs,
        slotTargetsValid: restored.inputs.every(
          (input, slot) =>
            input.link == null ||
            app.graph.links[input.link].target_slot === slot,
        ),
      };
    });
    assert.equal(references.references.length, 8);
    assert.deepEqual(
      references.references.slice(2).map(([, connected]) => connected),
      Array(6).fill(true),
    );
    assert.deepEqual(references.names.slice(7, 20), [
      "first_frame",
      "last_frame",
      ...Array.from({ length: 8 }, (_, i) => `reference_image_${i + 1}`),
      "reference_video_1",
      "driving_audio",
      "audio_vae",
    ]);
    assert.ok(references.slotTargetsValid);
    for (const name of ["width", "height", "base_seed"])
      assert.deepEqual(references.inputs[name], [
        references.dimensionNodes[name],
        0,
      ]);
    await page.evaluate(() => {
      const app = window.comfyAPI.app.app;
      app.canvas.ds.scale = 0.85;
      app.canvas.ds.offset = [-380, 70];
      app.canvas.setDirty(true, true);
    });
    await page.waitForTimeout(100);
    await page.screenshot({
      path: "/tmp/timeline-sampler-layout.png",
      clip: { x: 320, y: 30, width: 450, height: 940 },
    });
    await page.evaluate(async (workflow) => {
      const app = window.comfyAPI.app.app;
      await app.loadGraphData(workflow);
      app.canvas.ds.scale = 0.9;
      app.canvas.ds.offset = [-650, 60];
      app.canvas.setDirty(true, true);
    }, workflow);
    await page.waitForTimeout(100);
    await page.screenshot({ path: "/tmp/timeline-sampler-parity.png" });
    // Pasting one clipboard twice must still give independent projects.
    const pastedIds = await page.evaluate(() => {
      const app = window.comfyAPI.app.app;
      const studio = app.graph._nodes.find(
        (n) => n.comfyClass === "H3ContinuumTimelineStudio",
      );
      app.canvas.copyToClipboard([studio]);
      app.canvas.pasteFromClipboard();
      app.canvas.pasteFromClipboard();
      return app.graph._nodes
        .filter((n) => n.comfyClass === "H3ContinuumTimelineStudio")
        .map(
          (n) =>
            JSON.parse(n.widgets.find((w) => w.name === "project_json").value)
              .project_id,
        );
    });
    assert.equal(pastedIds.length, 3);
    assert.equal(new Set(pastedIds).size, 3);
    // Multi-job reroll: append in order and let native storage allocate nonces.
    await page.evaluate(() => {
      const app = window.comfyAPI.app.app;
      const api = window.comfyAPI.api.api;
      const sampler = app.graph._nodes.find(
        (n) => n.comfyClass === "H3ContinuumTimelineSampler",
      );
      sampler.widgets.find((w) => w.name === "control_after_generate").value =
        "increment";
      const link = sampler.inputs.find((i) => i.name === "timeline").link;
      const studio = app.graph.getNodeById(app.graph.links[link].origin_id);
      for (const n of app.graph._nodes.filter(
        (n) => n.comfyClass === "H3ContinuumTimelineStudio" && n !== studio,
      ))
        app.graph.remove(n);
      const widget = studio.widgets.find((w) => w.name === "project_json");
      const p = JSON.parse(widget.value);
      p.duration_frames = 168 * 17;
      p.selection = [0, 168 * 17];
      widget.value = JSON.stringify(p);
      window.__h3Queued = [];
      api.queuePrompt = async (number, payload) => {
        const inputs = payload.output[String(sampler.id)].inputs;
        window.__h3Queued.push([
          number,
          inputs.job_index,
          inputs.reroll_nonce,
          inputs.base_seed,
        ]);
        return { prompt_id: "test-" + window.__h3Queued.length };
      };
      studio.widgets.find((w) => w.name === "Open Timeline Studio").callback();
    });
    await page.locator(".h3ts").waitFor({ state: "visible" });
    await page
      .getByRole("button", { name: "Reroll selection", exact: true })
      .click();
    await page.waitForFunction(() =>
      document.querySelector(".h3ts-status").textContent.includes("queued"),
    );
    assert.deepEqual(await page.evaluate(() => window.__h3Queued), [
      [0, 0, 0, 20261017],
      [0, 1, 0, 20261017],
    ]);
    assert.equal(
      await page.evaluate(
        () =>
          window.comfyAPI.app.app.graph._nodes
            .find((n) => n.comfyClass === "H3ContinuumTimelineSampler")
            .widgets.find((w) => w.name === "base_seed").value,
      ),
      20261018,
    );
    // A long queued cut may exceed 64 jobs; reopening must retain ownership
    // of its earliest pending job so Cancel cannot orphan it.
    await page
      .locator(".h3ts")
      .getByRole("button", { name: "Close", exact: true })
      .click();
    await page.evaluate(() => {
      const app = window.comfyAPI.app.app,
        api = window.comfyAPI.api.api;
      const studio = app.graph._nodes.find(
        (n) => n.comfyClass === "H3ContinuumTimelineStudio",
      );
      const ids = Array.from({ length: 70 }, (_, i) => "owned-" + i);
      studio.properties.h3_timeline_view.queuedPrompts = ids;
      const original = api.fetchApi.bind(api);
      window.__h3Interrupted = 0;
      api.interrupt = async () => {
        window.__h3Interrupted++;
      };
      api.fetchApi = async (path, options) => {
        if (path !== "/queue") return original(path, options);
        if (options?.method === "POST") {
          window.__h3Cancelled = JSON.parse(options.body).delete;
          return new Response("{}");
        }
        return new Response(
          JSON.stringify({
            queue_pending: window.__h3Cancelled
              ? []
              : ids.map((id, i) => [i, id]),
            queue_running: window.__h3Cancelled ? [[0, ids[0]]] : [],
          }),
        );
      };
      studio.widgets.find((w) => w.name === "Open Timeline Studio").callback();
    });
    await page
      .locator(".h3ts")
      .getByRole("button", { name: "Cancel sampling", exact: true })
      .click();
    await page.waitForFunction(() => window.__h3Cancelled?.length === 70);
    assert.equal(await page.evaluate(() => window.__h3Cancelled[0]), "owned-0");
    await page.waitForFunction(() => window.__h3Interrupted === 1);
    // Long-duration clip surfaces must stay viewport-bounded even at an
    // interior point several thousand source seconds from frame zero.
    const geometry = await page.evaluate(async () => {
      const app = window.comfyAPI.app.app;
      const studio = app.graph._nodes.find(
        (n) => n.comfyClass === "H3ContinuumTimelineStudio",
      );
      studio.__timelineEditProject((p) => {
        p.duration_frames = 7200 * 24;
        p.selection = [86400, 86568];
        p.tracks = [{
          id:"virtual",role:"prompt",name:"Long prompt",
          clips:[{
            id:"virtual-clip",start:0,end:p.duration_frames,
            source_in:"0",speed:"1",padding:"error",
            audio_role:"neither",text:"Spanning prompt",
          }],
        }];
      });
      const scroller = document.querySelector(".h3ts-scroll");
      const zoom = studio.properties.h3_timeline_view.zoom;
      scroller.scrollLeft = Math.round(3600 * zoom);
      scroller.dispatchEvent(new Event("scroll"));
      await new Promise((resolve) => requestAnimationFrame(
        () => requestAnimationFrame(resolve),
      ));
      const clip = document.querySelector(".h3ts-clip");
      return {
        width: clip.getBoundingClientRect().width,
        viewport: scroller.clientWidth,
        trimLeft: clip.querySelector(".trim-left").hidden,
        trimRight: clip.querySelector(".trim-right").hidden,
      };
    });
    assert.ok(geometry.width <= geometry.viewport + 24, JSON.stringify(geometry));
    assert.equal(geometry.trimLeft, true);
    assert.equal(geometry.trimRight, true);
    // The selected chunk editor is a real project authoring surface, not
    // placeholder text: typed prompts must become exact chunk prompt clips.
    await page.locator(".h3ts-chunk-card").first().waitFor();
    const chunkPrompt = page.locator(
      '.h3ts-chunk-card textarea[aria-label^="Prompt for chunk"]',
    ).first();
    await chunkPrompt.fill("A precise authored H3 chunk prompt");
    await chunkPrompt.press("Tab");
    const authored = await page.evaluate(() => {
      const studio = window.comfyAPI.app.app.graph._nodes.find((n) =>
        n.comfyClass === "H3ContinuumTimelineStudio");
      const p = JSON.parse(studio.widgets.find((w)=>w.name==="project_json").value);
      const prompt = p.tracks.flatMap((t)=>t.role==="prompt" ? t.clips : [])
        .find((c)=>c.chunk_prompt && c.text==="A precise authored H3 chunk prompt");
      return prompt && {start:prompt.start,end:prompt.end,
        frames:p.chunk_frames,override:prompt.override};
    });
    assert.ok(authored, "Chunk prompt text did not persist into project");
    assert.equal(authored.end-authored.start,authored.frames);
    assert.equal(authored.override,true);
    const dropSource = await page.evaluate(() => {
      const app=window.comfyAPI.app.app;
      const studio=app.graph._nodes.find((n)=>
        n.comfyClass==="H3ContinuumTimelineStudio");
      const project=JSON.parse(studio.widgets.find((w)=>
        w.name==="project_json").value);
      const source=project.assets.find((a)=>a.video);
      if (!source) return null;
      const transfer=new DataTransfer();
      transfer.setData("application/x-h3-asset",source.id);
      document.querySelector(".h3ts-chunk-card").dispatchEvent(new DragEvent(
        "drop",{bubbles:true,cancelable:true,dataTransfer:transfer},
      ));
      return source.id;
    });
    if (dropSource) {
      await page.waitForFunction((id) => {
        const studio=window.comfyAPI.app.app.graph._nodes.find((n)=>
          n.comfyClass==="H3ContinuumTimelineStudio");
        const project=JSON.parse(studio.widgets.find((w)=>
          w.name==="project_json").value);
        return project.tracks.some((track)=>track.role==="video_guide" &&
          track.clips.some((c)=>c.asset_id===id && c.chunk_workspace));
      },dropSource);
    }
    const own = errors.filter((e) => /Timeline|timeline|h3ts/.test(e));
    assert.deepEqual(own, []);
    console.log(
      JSON.stringify({
        browser: "real ComfyUI",
        editor:
          "open/split/undo/redo/save/reload/typing/view-state/close/paste identity/queue order/long queue cancellation/workflow disposal passed",
        pageErrors: errors,
      }),
    );
  } catch (e) {
    await page.screenshot({ path: "/tmp/timeline-failure.png" });
    console.error("PAGE ERRORS", errors);
    console.error(
      await page.evaluate(() =>
        [...document.querySelectorAll("dialog")].map((d) => ({
          open: d.open,
          modal: d.matches(":modal"),
          rect: d.getBoundingClientRect().toJSON(),
          css: getComputedStyle(d).pointerEvents,
        })),
      ),
    );
    console.error((await page.locator("body").innerText()).slice(-2000));
    throw e;
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
