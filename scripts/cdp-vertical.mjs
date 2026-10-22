// Verify the app shell: content pane scrolls, rail and top bar stay put.
const [wsUrl, pathsJson] = process.argv.slice(2);
const paths = JSON.parse(pathsJson);
const base = process.env.WORKHUB_BASE || "http://127.0.0.1:8000";
const ws = new WebSocket(wsUrl);
await new Promise((resolve) => (ws.onopen = resolve));
let id = 0;
const pending = new Map();
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
};
const send = (method, params = {}) => new Promise((resolve) => {
  const myId = ++id; pending.set(myId, resolve);
  ws.send(JSON.stringify({ id: myId, method, params }));
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const probe = `(() => {
  const rail = document.querySelector('.app-rail');
  const main = document.querySelector('.app-frame > main');
  const bar = document.querySelector('header.topbar');
  if (!rail || !main) return { error: 'missing shell elements' };
  const r = rail.getBoundingClientRect();
  const b = bar ? bar.getBoundingClientRect() : null;
  return {
    railTop: Math.round(r.top), railBottom: Math.round(r.bottom), railHeight: Math.round(r.height),
    barBottom: b ? Math.round(b.bottom) : null,
    viewport: window.innerHeight,
    mainScrollTop: Math.round(main.scrollTop),
    mainScrollable: main.scrollHeight - main.clientHeight,
    railFullyVisible: r.top >= -1 && r.bottom <= window.innerHeight + 1,
    railBelowBar: b ? r.top >= b.bottom - 1 : true,
  };
})()`;

await send("Page.enable");
const report = {};
for (const path of paths) {
  await send("Page.navigate", { url: base + path });
  await sleep(2200);
  const before = await send("Runtime.evaluate", { expression: probe, returnByValue: true });
  // Scroll the content pane, which is what a user actually scrolls now.
  await send("Runtime.evaluate", { expression: "document.querySelector('.app-frame > main').scrollTop = 600" });
  await sleep(500);
  const after = await send("Runtime.evaluate", { expression: probe, returnByValue: true });
  report[path] = {
    before: before.result?.result?.value ?? {},
    afterScroll: after.result?.result?.value ?? {},
  };
}
console.log(JSON.stringify(report));
ws.close();
process.exit(0);
