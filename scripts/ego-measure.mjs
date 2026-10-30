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
//   { out, base, login?, paths: [...], expression, viewport?: {width, height},
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
// 先清一次 cookie：这个浏览器配置是和本机使用者共用的，残留的 127.0.0.1
// 会话 cookie 会让登录那一步变成空操作，之后每一页量到的都是登录页（没有
// 侧栏、没有 frame），报出来是「missing shell elements」——看着像布局坏了。
await page.cdp("Network.clearBrowserCookies").catch(() => {});
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

// 登录段：/login?token= 是 302 到落地页，会话 cookie 是在那条 302 响应上种下的。
// 浏览器把 cookie 提交进 jar 是异步的，紧接着 goto 下一页偶尔会赶在它前面 —— 服务
// 端看到的是未登录，于是量到的是一张登录页（没有侧栏、没有 frame），报出来是
// 「missing shell elements」，看着像布局坏了。所以每次登录都等 jar 里真的出现那个
// 主机上的 cookie、并且落地页确实有外壳，才算登录完成。
const hasShell = () =>
  page
    .evaluate("!!(document.querySelector('.app-frame') && document.querySelector('header.topbar'))")
    .catch(() => false);

const waitForCookie = async () => {
  const host = new URL(cfg.base).hostname;
  for (let wait = 0; wait < 25; wait += 1) {
    const jar = await page.cdp("Network.getAllCookies").catch(() => ({ cookies: [] }));
    const cookies = (jar && jar.cookies) || [];
    // cookie 名字由服务端定，这里只认「要访问的这个主机已经有会话 cookie」。
    if (cookies.some((c) => String(c.domain).includes(host) && c.value)) return true;
    await sleep(100);
  }
  return false;
};

const signIn = async () => {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.goto(cfg.base + cfg.login);
    await page.waitForLoadState("load").catch(() => {});
    await page.waitForFunction("document.readyState === 'complete'").catch(() => {});
    await sleep(300);
    await waitForCookie();
    if (await hasShell()) return true;
    await sleep(300);
  }
  return false;
};

const report = {};
try {
  if (cfg.login && !(await signIn())) {
    throw new Error(`登录没有落地：${cfg.login.slice(0, 40)}`);
  }
  for (const path of cfg.paths) {
    let landed = false;
    for (let attempt = 0; attempt < 3 && !landed; attempt += 1) {
      await page.goto(cfg.base + path);
      await page.waitForLoadState("load").catch(() => {});
      await page.waitForFunction("document.readyState === 'complete'").catch(() => {});
      await sleep(settle);
      // 不带登录时无从判断「该不该有外壳」，第一条就量。
      landed = !cfg.login || (await hasShell());
      if (!landed) {
        // 这一页被弹回登录页：会话没被带上，重新登录一次再走这一页。
        if (attempt === 2 || !(await signIn())) {
          throw new Error(`每一页都要登录：${path}`);
        }
      }
    }
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
