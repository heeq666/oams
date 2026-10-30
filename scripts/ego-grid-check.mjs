// 12 栅格验收（ego 驱动）：三档宽度 × 若干页面，量行内排布、跨度宽度、横向溢出，并落盘截图。
//
// 旧的自研 CDP 版已随这次迁移删除；
// 而是 ego 浏览器（AGENTS.md「浏览器自动化」）。服务仍自己起一个——不占演示站 8000，
// 也不假设本机已经有服务在跑；跑完关掉。
//
// 用法：ego-browser nodejs < scripts/ego-grid-check.mjs
// 退出码：0 通过 / 1 有页面不合规 / 2 环境起不来
//
// 口径（docs/design/layout-grid.md）：三档 1440 / 1024 / 768，页面不许横向滚动
// （documentElement.scrollWidth === clientWidth），不许有右边界超出视口的元素。
// 退出码只看这两条 + 外壳在不在；行内数据只进报告，留给人工看。
//
// 注意：ego 脚本拿不到 argv（process.argv 是空的），也不继承 shell 环境变量，
// 所以配置只能是下面的常量；要临时改，写 /tmp/workhub-ego-grid.json 覆盖即可。
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { readFile } from "node:fs/promises";

const ROOT = "/Volumes/工作/aiworkspace/company-workhub";
const PYTHON = ROOT + "/.venv/bin/python";
const CONFIG_PATH = "/tmp/workhub-ego-grid.json";

const DEFAULTS = {
  widths: [1440, 1024, 768],
  // /settings 是唯一用 .grid-12 并排的页；其余几页跟着走一遍，防回归（它们本来就单列）。
  paths: ["/settings", "/", "/progress", "/board", "/reviews"],
  outDir: ROOT + "/assets/screenshots/grid",
  shot: true,
  spaceName: "workhub 12 栅格验收",
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function loadConfig() {
  try {
    const text = await readFile(CONFIG_PATH, "utf8");
    return { ...DEFAULTS, ...JSON.parse(text) };
  } catch (error) {
    if (error.code === "ENOENT") return { ...DEFAULTS };
    throw error;
  }
}

// 服务要连真库（跟演示站同一条，取自 .env），不为测试另建库。
async function databaseUrl() {
  const fromEnv = (process.env.WORKHUB_DATABASE_URL || "").trim();
  if (fromEnv) return fromEnv;
  const text = await readFile(ROOT + "/.env", "utf8");
  const line = text.split("\n").find((l) => l.trim().startsWith("WORKHUB_DATABASE_URL="));
  if (!line) throw new Error(".env 里没有 WORKHUB_DATABASE_URL");
  return line.slice(line.indexOf("=") + 1).trim();
}

function portFree(port) {
  return new Promise((done) => {
    const probe = createServer();
    probe.once("error", () => done(false));
    probe.once("listening", () => probe.close(() => done(true)));
    probe.listen(port, "127.0.0.1");
  });
}

async function pickPort() {
  for (let port = 8021; port <= 8030; port += 1) {
    if (await portFree(port)) return port;
  }
  console.error("8021-8030 都被占了，找不到能起实例的端口。");
  process.exit(2);
}

// 量什么：外壳在不在、整页横向溢出多少、超出视口右边界的节点（前 5 个）、
// 以及每个 .grid-12 里按 y 分组的行——行内是否等高、宽度加 gap 后是否填满。
// 回调整段在页内跑，不能引用 Node 侧的变量。
function measure() {
  const main = document.querySelector(".app-frame > main");
  const rect = (node) => {
    const r = node.getBoundingClientRect();
    return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
  };
  const grids = [...document.querySelectorAll(".grid-12")].map((grid) => {
    const kids = [...grid.querySelectorAll(":scope > *")].map((kid) => ({
      tag: kid.tagName.toLowerCase(),
      cls: String(kid.className).split(" ").slice(0, 3).join("."),
      span: getComputedStyle(kid).gridColumnEnd,
      ...rect(kid),
    }));
    const rows = {};
    for (const kid of kids) (rows[Math.abs(kid.y)] ||= []).push(kid);
    const rowList = Object.entries(rows).map(([y, list]) => ({
      y: Number(y),
      count: list.length,
      widths: list.map((c) => c.w),
      heights: list.map((c) => c.h),
      equalHeight: list.length > 1 ? new Set(list.map((c) => c.h)).size === 1 : null,
      spans: list.map((c) => c.span),
    }));
    const width = Math.round(grid.getBoundingClientRect().width);
    const first = rowList[0];
    return {
      width,
      rows: rowList,
      firstRowSlack: first ? width - (first.widths.reduce((a, b) => a + b, 0) + (first.count - 1) * 16) : null,
    };
  });
  const clientWidth = document.documentElement.clientWidth;
  return {
    href: location.pathname + location.search,
    title: document.title,
    hasShell: !!main,
    viewport: clientWidth,
    overflowX: document.documentElement.scrollWidth - clientWidth,
    mainWidth: main ? Math.round(main.getBoundingClientRect().width) : null,
    grids,
    offenders: [...document.querySelectorAll("body *")]
      .filter((node) => node.getBoundingClientRect().right > clientWidth + 1)
      .slice(0, 5)
      .map((node) => node.tagName + "." + String(node.className).split(" ")[0]),
  };
}

const config = await loadConfig();
const appPort = await pickPort();
const base = "http://127.0.0.1:" + appPort;

let server = null;
const problems = [];

try {
  server = spawn(
    PYTHON,
    ["-m", "uvicorn", "workhub.api:app", "--host", "127.0.0.1", "--port", String(appPort)],
    {
      cwd: ROOT,
      stdio: "ignore",
      env: {
        ...process.env,
        PYTHONPATH: ROOT + "/app",
        WORKHUB_DATABASE_URL: await databaseUrl(),
        WORKHUB_DEV_AUTOLOGIN: "1",
      },
    },
  );

  let up = false;
  for (let i = 0; i < 120 && !up; i += 1) {
    await sleep(250);
    try {
      const res = await fetch(base + "/");
      up = res.ok;
    } catch {}
  }
  if (!up) {
    console.error("服务没起来：" + base);
    process.exit(2);
  }

  // 任务空间可以复用（省启动），页面不要复用：上次收尾 close 掉之后，p1 这个
  // 名字还挂在空间里，task.page("p1") 会把已经关掉的死页面还回来。
  const task = await taskSpace(config.spaceName);
  const page = await task.newPage();

  const report = {};
  for (const width of config.widths) {
    await page.cdp("Emulation.setDeviceMetricsOverride", {
      width, height: 900, deviceScaleFactor: 1, mobile: false,
    });
    for (const path of config.paths) {
      await page.goto(base + path);
      await page.waitForFunction(() => document.readyState === "complete");
      const value = await page.evaluate(measure);
      const key = width + " " + path;
      report[key] = value;

      // 量到登录页就当失败：没外壳时 grids 一定是空的，静默通过比报错更危险。
      if (!value.hasShell) {
        problems.push(key + "：没找到 .app-frame > main（当前在 " + value.href + "），大概率被踢回登录页");
      }
      if (value.overflowX !== 0) {
        problems.push(key + "：横向溢出 " + value.overflowX + "px");
      }
      if (value.offenders.length) {
        problems.push(key + "：有元素超出视口右边界 → " + value.offenders.join(", "));
      }

      if (config.shot) {
        const name = config.outDir + "/" + width + (path === "/" ? "-home" : path.replaceAll("/", "-")) + ".png";
        await page.screenshot({ path: name });
      }
    }
  }

  console.log(JSON.stringify(report, null, 1));
  const pages = config.widths.length * config.paths.length;
  if (problems.length) {
    for (const problem of problems) console.error("✗ " + problem);
    console.error("✗ 12 栅格：" + problems.length + " 处不合规（共 " + pages + " 页次）");
    process.exitCode = 1;
  } else {
    console.log("✓ 12 栅格：" + pages + " 页次三档都不横向滚动、无越界元素");
    console.log("  截图在 " + config.outDir);
    process.exitCode = 0;
  }
  await page.close();
} catch (error) {
  console.error("跑挂了：" + (error && error.message ? error.message : error));
  process.exitCode = 1;
} finally {
  if (server) server.kill("SIGTERM");
  await sleep(300);
}
