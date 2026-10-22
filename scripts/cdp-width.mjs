// Evaluate a measurement expression on one URL over CDP.
// Usage: node cdp-width.mjs <wsUrl> <expression> <json paths>
const [wsUrl, expression, pathsJson] = process.argv.slice(2);
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
await send("Page.enable");
const report = {};
for (const path of paths) {
  await send("Page.navigate", { url: base + path });
  await sleep(2200);
  const res = await send("Runtime.evaluate", { expression, returnByValue: true });
  report[path] = res.result?.result?.value ?? {};
}
console.log(JSON.stringify(report));
ws.close();
process.exit(0);
