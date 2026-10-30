// Measure served pages in a real browser (ego), one expression, two readings each.
//
// The three layout checks in tests/ share this driver: they differ only in the
// expression they evaluate, the viewport, and what they scroll between the two
// readings. The measurement itself stays in the test file, so what a check
// claims is readable next to its assertions.
//
// Run:  ego-browser nodejs < scripts/ego-measure.mjs
//
// ego hands the script neither argv nor the caller's environment, and its cwd is
// "/" — so the inputs come from a JSON file:
//
//   { out, base, paths: [...], expression, viewport?: {width, height},
//     scroll?: "<js to run between the two readings>", settle?: ms, space?: name }
//
// Every run gets its own config path, so parallel runs (pytest-xdist) cannot read
// each other's settings: the caller copies this file, replaces the placeholder
// below with the path it picked, and hands that copy to ego on stdin. Never
// hard-code a shared path here — a collision measures the wrong page silently.
//
// The first await below has to be a file or browser operation. When it was a
// net.createServer().listen port probe, ego's listening/error events never fired
// and the whole script exited 0 in silence.
const CONFIG = "__EGO_MEASURE_CONFIG__";

const fs = await import("node:fs/promises");
const cfg = JSON.parse(await fs.readFile(CONFIG, "utf8"));
const settle = cfg.settle ?? 2200;

// Reuse the task space (cheap) but never a page: a reused page label can point at
// a tab an earlier run closed, and every goto then fails with "page p1 was closed".
const space = await taskSpace(cfg.space || "workhub 布局量测");
const page = await space.newPage();
if (cfg.viewport) {
  await page.cdp("Emulation.setDeviceMetricsOverride", {
    width: cfg.viewport.width,
    height: cfg.viewport.height,
    deviceScaleFactor: 1,
    mobile: false,
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// A reading of `undefined` means the evaluation did not reach a document yet: the
// navigation had not committed, so the expression ran in a discarded context.
// Retry rather than report a page with no measurements — that failure looks like
// "every box is None" and tells nobody what happened.
const read = async () => {
  for (let attempt = 0; attempt < 25; attempt += 1) {
    const value = await page.evaluate(cfg.expression);
    if (value !== undefined && value !== null) return value;
    await sleep(200);
  }
  throw new Error(`measurement kept returning nothing: ${cfg.expression.slice(0, 60)}`);
};

const report = {};
try {
  for (const path of cfg.paths) {
    await page.goto(cfg.base + path);
    await page.waitForLoadState("load").catch(() => {});
    await page.waitForFunction("document.readyState === 'complete'").catch(() => {});
    await sleep(settle);
    const before = await read();
    if (cfg.scroll) {
      await page.evaluate(cfg.scroll).catch(() => {});
      await sleep(400);
    }
    report[path] = { before, afterScroll: await read() };
  }
  await fs.writeFile(cfg.out, JSON.stringify(report));
} finally {
  // Always close: a page left open here burns the space's page budget, and the
  // next run then measures a tab that never committed. (Seen as "every box is
  // None" on the first check of a suite, with the page count creeping up.)
  await page.close().catch(() => {});
}

console.log(`[measure] ${cfg.paths.length} 页次 → ${cfg.out}`);
