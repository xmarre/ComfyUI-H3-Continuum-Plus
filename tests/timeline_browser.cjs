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
    assert.equal(samplerInputs.continuity, "Strong — 39 frames");
    assert.equal(
      samplerInputs.continuation_method,
      "Native Masked — exact continuation (Recommended)",
    );
    assert.equal(await page.locator(".h3ts").count(), 0);
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
    // Multi-job reroll: jobs queue in order, appended, with a fresh nonce.
    await page.evaluate(() => {
      const app = window.comfyAPI.app.app;
      const api = window.comfyAPI.api.api;
      const sampler = app.graph._nodes.find(
        (n) => n.comfyClass === "H3ContinuumTimelineSampler",
      );
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
        window.__h3Queued.push([number, inputs.job_index, inputs.reroll_nonce]);
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
      [0, 0, 1],
      [0, 1, 1],
    ]);
    const own = errors.filter((e) => /Timeline|timeline|h3ts/.test(e));
    assert.deepEqual(own, []);
    console.log(
      JSON.stringify({
        browser: "real ComfyUI",
        editor:
          "open/split/undo/redo/save/reload/typing/view-state/close/paste identity/queue order/workflow disposal passed",
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
