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
    await page.getByRole("button", { name: "+ Zoom", exact: true }).click();
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
    await page
      .locator(".h3ts")
      .screenshot({
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
    await page.evaluate(async () => {
      const app = window.comfyAPI.app.app;
      await app.loadGraphData({
        version: 0.4,
        nodes: [],
        links: [],
        groups: [],
        config: {},
        extra: {},
        last_node_id: 0,
        last_link_id: 0,
      });
    });
    assert.equal(await page.locator(".h3ts").count(), 0);
    const own = errors.filter((e) => /Timeline|timeline|h3ts/.test(e));
    assert.deepEqual(own, []);
    console.log(
      JSON.stringify({
        browser: "real ComfyUI",
        editor:
          "open/split/undo/redo/save/reload/typing/close/workflow disposal passed",
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
