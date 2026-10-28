// 12 栅格验收：三档宽度 × 若干页面，量行内 top、跨度宽度、横向溢出，并落盘截图。
// 用法：node scripts/cdp-grid-check.mjs <outDir> <widths csv> <paths csv> [shot]
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";

const [outDir = "assets/screenshots/grid", widthsCsv = "1440,1024,768", pathsCsv = "/settings", shot = "1"] = process.argv.slice(2);
const base = process.env.WORKHUB_BASE || "http://127.0.0.1:8000";
const widths = widthsCsv.split(",").map(Number);
const paths = pathsCsv.split(",");
const chromeBin = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const port = 9600 + Math.floor(Math.random() * 200);

const chrome = spawn(chromeBin, [
  "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-crash-reporter", "--disable-crashpad",
  "--hide-scrollbars", "--no-first-run",
  "--user-data-dir=/tmp/workhub-cdp-" + port, "--remote-debugging-port=" + String(port), "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let wsUrl = null;
for (let i = 0; i < 60 && !wsUrl; i++) {
  await sleep(250);
  try {
    const res = await fetch("http://127.0.0.1:" + port + "/json/list");
    const list = await res.json();
    const page = list.find((t) => t.type === "page");
    if (page) wsUrl = page.webSocketDebuggerUrl;
  } catch {}
}
if (!wsUrl) { console.error("chrome did not expose a page"); process.exit(2); }

const ws = new WebSocket(wsUrl);
await new Promise((r) => (ws.onopen = r));
let seq = 0;
const pending = new Map();
ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
const send = (method, params = {}) => new Promise((res) => { const id = ++seq; pending.set(id, res); ws.send(JSON.stringify({ id, method, params })); });

await send("Page.enable");
await send("Runtime.enable");
await mkdir(outDir, { recursive: true });

const MEASURE = `(() => {
  const main = document.querySelector('.app-frame > main');
  const rect = (n) => { const r = n.getBoundingClientRect(); return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) }; };
  const grids = [...document.querySelectorAll('.grid-12')].map((g) => {
    const kids = [...g.querySelectorAll(':scope > *')].map((k) => ({ tag: k.tagName.toLowerCase(), cls: k.className.split(' ').slice(0, 3).join('.'), span: getComputedStyle(k).gridColumnEnd, align: getComputedStyle(k).alignSelf, ...rect(k) }));
    const rows = {};
    kids.forEach((k) => { const key = Math.abs(k.y) + ''; (rows[key] ||= []).push(k); });
    const rowList = Object.entries(rows).map(([y, list]) => ({ y: +y, count: list.length, widths: list.map((c) => c.w), heights: list.map((c) => c.h), equalHeight: list.length > 1 ? new Set(list.map((c) => c.h)).size === 1 : null, spans: list.map((c) => c.span) }));
    const gRect = rect(g);
    const inner = gRect.w;
    const sum = rowList[0] ? rowList[0].widths.reduce((a, b) => a + b, 0) + (rowList[0].count - 1) * 16 : 0;
    return { rows: rowList, width: inner, firstRowSum: sum, firstRowSlack: rowList[0] ? inner - sum : null, kids };
  });
  return {
    viewport: document.documentElement.clientWidth,
    overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    mainWidth: main ? Math.round(main.getBoundingClientRect().width) : null,
    grids,
    // 全站横向溢出元素排查：右边界超出视口的节点
    offenders: [...document.querySelectorAll('body *')].filter((n) => n.getBoundingClientRect().right > document.documentElement.clientWidth + 1).slice(0, 5).map((n) => n.tagName + '.' + String(n.className).split(' ')[0]),
  };
})()`;

const report = {};
for (const w of widths) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: 900, deviceScaleFactor: 1, mobile: false });
  for (const p of paths) {
    await send("Page.navigate", { url: base + p });
    await sleep(1400);
    const r = await send("Runtime.evaluate", { expression: MEASURE, returnByValue: true });
    const value = r.result?.result?.value ?? { error: r.result?.exceptionDetails?.text };
    report[w + " " + p] = value;
    if (shot === "1") {
      const cap = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      if (cap.result?.data) {
        const name = outDir + "/" + String(w) + (p === "/" ? "-home" : p.replace(/\//g, "-")) + ".png";
        await writeFile(name, Buffer.from(cap.result.data, "base64"));
      }
    }
  }
}
console.log(JSON.stringify(report, null, 1));
ws.close();
chrome.kill();
process.exit(0);
