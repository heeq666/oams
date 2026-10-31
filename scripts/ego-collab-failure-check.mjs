// 断网重发验收（ego 驱动）：真浏览器断网，验「红色感叹号 → 重发 → 就地变正常气泡」。
//
// 这条路径替身测不出来：气泡是脚本在 htmx 失败事件里现挂的，重发是把正文与幂等键
// 搬回输入框再提交一次。所以这里起一个真浏览器，点真按钮，收工后把自己起的实例和
// 写进库的那条话一起收走。
//
// 验的是**前端路径**：断网那一下气泡出不出来、重发能不能把这一条发成功。
// **幂等不在这里验**：断网时那条 POST 根本没到服务端，服务端一行都没写，所以
// 「重发行数=1」证明不了去重——那是 pytest 的活（tests/test_collab_rules.py
// ::test_resending_with_the_same_key_lands_the_message_once，真插两次同一 id）。
// 这里只把行数当信息打出来，不作断言。
//
// 用法：ego-browser nodejs < scripts/ego-collab-failure-check.mjs
// 退出码：0 通过 / 1 断言不成立 / 2 环境起不来（浏览器、服务、静态资源、数据库）
import { spawn, execFileSync } from "node:child_process";
import { createServer } from "node:net";
import { readFile } from "node:fs/promises";

const ROOT = "~/work/company-os";
const PYTHON = ROOT + "/.venv/bin/python";
const SPACE_NAME = "oams 断网重发验收";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 演示站占 8000，这里另起一个，别撞。端口上要是已经有别人的实例（本机上很常见），
// 我们起的那个会悄悄起不来、然后对着别人的库跑完一整轮——所以先挑一个空端口。
function portFree(port) {
  return new Promise((done) => {
    const probe = createServer();
    probe.once("error", () => done(false));
    probe.once("listening", () => probe.close(() => done(true)));
    probe.listen(port, "127.0.0.1");
  });
}

async function pickPort() {
  const wanted = Number((process.env.OAMS_CDP_APP_PORT || "").trim() || 0);
  if (wanted) {
    if (!(await portFree(wanted))) {
      console.error("端口 " + wanted + " 已经被占用（本机上可能还有别的实例在跑）。换一个端口，或者先把那个停掉。");
      process.exit(2);
    }
    return wanted;
  }
  for (let port = 8011; port <= 8020; port += 1) {
    if (await portFree(port)) return port;
  }
  console.error("8011-8020 都被占了，找不到能起实例的端口。");
  process.exit(2);
}

// 库地址取自 .env（跟演示站同一条），不为测试另建库。
async function databaseUrl() {
  const fromEnv = (process.env.OAMS_DATABASE_URL || "").trim();
  if (fromEnv) return fromEnv;
  const text = await readFile(ROOT + "/.env", "utf8");
  const line = text.split("\n").find((l) => l.trim().startsWith("OAMS_DATABASE_URL="));
  if (!line) throw new Error(".env 里没有 OAMS_DATABASE_URL");
  return line.slice(line.indexOf("=") + 1).trim();
}

// 顺序有讲究：ego 的 nodejs 运行器里，模块的**第一个** await 必须是文件/浏览器这类
// 由它自己驱动的操作；开口子探测端口如果排在最前面，listening 事件不会派发，
// 脚本会一声不响地退出去（查了很久才定位到）。所以先把 .env 读出来。
const dbUrl = await databaseUrl();
const appPort = await pickPort();
const base = "http://127.0.0.1:" + appPort;
const body = "断网重发验收 " + Date.now().toString(36);

// 查库/清理都走 venv 里的 psycopg——不引第三方依赖，也不假设本机装了 psql。
function sql(statement, params = []) {
  const code = `
import json, sys, psycopg
sql = sys.argv[1]
params = json.loads(sys.argv[2])
with psycopg.connect(sys.argv[3], row_factory=psycopg.rows.dict_row) as conn:
    rows = conn.execute(sql, tuple(params)).fetchall() if params else conn.execute(sql).fetchall()
    print(json.dumps(rows, default=str))
`;
  const out = execFileSync(PYTHON, ["-c", code, statement, JSON.stringify(params), dbUrl], {
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: ROOT + "/app" },
  });
  return JSON.parse(out.trim() || "[]");
}

const countRows = () =>
  sql("SELECT count(*) AS n FROM oams.collab_messages WHERE body = %s", [body])[0].n;
const dropRows = () =>
  sql("DELETE FROM oams.collab_messages WHERE body = %s RETURNING message_id", [body]).length;

const problems = [];
const check = (ok, message) => {
  if (!ok) problems.push(message);
  return ok;
};

let server = null;
let page = null;

try {
  // ------------------------------------------------------------------ 服务
  server = spawn(
    PYTHON,
    ["-m", "uvicorn", "oams.api:app", "--host", "127.0.0.1", "--port", String(appPort)],
    {
      cwd: ROOT,
      stdio: "ignore",
      env: {
        ...process.env,
        PYTHONPATH: ROOT + "/app",
        OAMS_DATABASE_URL: dbUrl,
        OAMS_DEV_AUTOLOGIN: "1", // 本机演示站的口径；只决定「我是谁」，权限一条不少
      },
    },
  );

  let up = false;
  for (let i = 0; i < 120 && !up; i += 1) {
    await sleep(250);
    try {
      up = (await fetch(base + "/")).ok;
    } catch {}
  }
  if (!up) {
    console.error("服务没起来：" + base);
    process.exit(2);
  }

  // 静态资源先在 node 这侧确认一次：冷启动时它 404/超时，htmx 就没得加载，
  // 后面所有步骤都会以「元素找不到」的形式假失败，很难一眼看出是这里。
  const htmxUrl = base + "/static/vendor/htmx.min.js";
  const htmxRes = await fetch(htmxUrl).catch(() => null);
  if (!htmxRes || !htmxRes.ok) {
    console.error("htmx 静态资源取不到：" + htmxUrl + "（" + (htmxRes ? htmxRes.status : "连接失败") + "）");
    process.exit(2);
  }

  // ------------------------------------------------------------- 开会话、写字
  // /collab 是片段：直接打开它拿不到外壳、样式和 htmx。走人走的路——先开首页，
  // 再点右下角那个入口，让面板被 htmx 拉进来。
  const task = await taskSpace(SPACE_NAME);
  // 页面不复用：上次收尾 close 掉之后 p1 这个名字还挂着，拿回来是个死页面。
  page = await task.newPage();
  await page.goto(base + "/");
  try {
    await page.waitForFunction(() => !!window.htmx && !!document.getElementById("collab-entry"), null, { timeout: 15000 });
  } catch {
    console.error("外壳没起来，或者 htmx 没加载");
    process.exit(2);
  }

  await page.click("#collab-entry", { label: "打开协作室" });
  try {
    await page.waitForFunction(
      () => !!document.querySelector("#collab-host .collab-panel")
        && !!document.querySelector("#collab-thread-list a.collab-row"),
      null,
      { timeout: 15000 },
    );
  } catch {
    console.error("协作室面板没打开，或者会话列表是空的（这个库还没有种子数据？）");
    process.exit(2);
  }

  // 断网时也要能开这个问题不存在；先把会话开出来，再断网。
  // 会话行有好几条，ego 的 click 不接受「匹配到多个」的模糊选择器；按第一行的
  // hx-get 收窄到唯一那一条（比派生意外的 nth-child 稳）。
  const firstRow = await page.evaluate(() => {
    const row = document.querySelector("#collab-thread-list a.collab-row");
    return row ? row.getAttribute("hx-get") : null;
  });
  if (!firstRow) {
    console.error("会话列表里没有可点的行（这个库还没有种子数据？）");
    process.exit(2);
  }
  await page.click('#collab-thread-list a.collab-row[hx-get="' + firstRow + '"]', { label: "打开第一个会话" });
  try {
    await page.waitForSelector(".collab-composer textarea[name=body]", { timeout: 15000 });
  } catch {
    console.error("会话没打开：找不到输入框");
    process.exit(1);
  }
  // 失败气泡全靠这段脚本挂着；它没执行，后面的红感叹号根本不会出现。
  try {
    await page.waitForFunction(() => !!window.__whCollabSendHook, null, { timeout: 5000 });
  } catch {
    console.error("输入区的失败处理脚本没有执行（气泡不会被挂出来）");
    process.exit(1);
  }

  await page.fill(".collab-composer textarea[name=body]", body);
  await page.evaluate(() => {
    window.__whProbe = "alive";
    return true;
  });

  // ---------------------------------------------------------------- 断网发送
  await page.cdp("Network.emulateNetworkConditions", {
    offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0,
  });
  await page.click(".collab-composer .collab-send", { label: "断网下发送" });

  let bubbleCame = true;
  try {
    await page.waitForSelector("#collab-msgs .collab-msg-failed", { timeout: 8000 });
  } catch {
    bubbleCame = false;
  }
  check(bubbleCame, "断网发送后没有出现失败气泡");

  if (bubbleCame) {
    const seen = await page.evaluate(() => {
      const node = document.querySelector("#collab-msgs .collab-msg-failed");
      const mark = node.querySelector(".collab-msg-fail-mark");
      const resend = node.querySelector("[data-resend]");
      return {
        text: node.querySelector("[data-failed-body]").textContent,
        markText: mark ? mark.textContent.trim() : null,
        markHidden: mark ? mark.getAttribute("aria-hidden") : null,
        resendText: resend ? resend.textContent.trim() : null,
        resendHidden: resend ? !!resend.hidden : null,
        clientMsgId: node.getAttribute("data-client-msg-id") || "",
        probe: window.__whProbe || null,
      };
    });
    check(seen.markText === "!", "气泡上没有红色感叹号（拿到：" + seen.markText + "）");
    check(seen.markHidden === "true", "感叹号没对读屏器说明是装饰");
    check(seen.resendText === "重发", "没有重发按钮（拿到：" + seen.resendText + "）");
    check(seen.resendHidden === false, "重发按钮被隐藏了；断网这种情况应当能重发");
    check(seen.text === body, "气泡里的正文跟输入的那句不一致");
    // 重发之所以不会变成第二条，全靠这个键；它空了，去重就无从谈起。
    check(!!seen.clientMsgId, "气泡上没有幂等键（data-client-msg-id 是空的）");
    // 整页刷新会把 window 上的探针抹掉：断网应当只改这一块，不重载整页。
    check(seen.probe === "alive", "页面整页刷新了（探针丢失）");
  }

  // 断网期间库里不该有这条。
  check(countRows() === 0, "断网期间消息竟然落库了");

  // ---------------------------------------------------------------- 恢复重发
  // 气泡都没出来就没有「重发」可点：这时候直接带问题收尾，别在 null 上崩，
  // 否则报出来的是一句「元素找不到」，看不出真正坏的是哪一步。
  let becameReal = false;
  if (bubbleCame) {
    await page.cdp("Network.emulateNetworkConditions", {
      offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1,
    });
    await page.click("#collab-msgs .collab-msg-failed [data-resend]", { label: "点重发" });
    try {
      await page.waitForFunction(
        (text) => {
          if (document.querySelector("#collab-msgs .collab-msg-failed")) return false;
          return [...document.querySelectorAll("#collab-msgs article.collab-msg .collab-msg-text")]
            .some((node) => node.textContent.trim() === text);
        },
        body,
        { timeout: 10000 },
      );
      becameReal = true;
    } catch {
      becameReal = false;
    }
    check(becameReal, "点了重发之后，那条话没有变成正常气泡");
  }

  // 只当信息：断网那条没到服务端，行数天然是 1，它证明不了幂等（见文件头）。
  const rows = countRows();

  // ------------------------------------------------------------------ 收尾
  dropRows(); // 共享库不留测试消息
  const left = countRows();

  console.log([
    "[断网重发]",
    "气泡=" + (bubbleCame ? "有" : "无"),
    "变正常气泡=" + (becameReal ? "是" : "否"),
    "库里行数=" + rows,
    "清理后剩=" + left,
  ].join(" "));

  if (problems.length) {
    for (const problem of problems) console.error("✗ " + problem);
    process.exitCode = 1;
  } else {
    console.log("✓ 断网重发：红感叹号出现、重发成功、就地变成正常气泡");
    console.log("  幂等不在这条里验（断网那条没到服务端），归 pytest："
      + "tests/test_collab_rules.py::test_resending_with_the_same_key_lands_the_message_once");
    process.exitCode = 0;
  }
} catch (error) {
  console.error("跑挂了：" + (error && error.message ? error.message : error));
  try { dropRows(); } catch {}
  process.exitCode = 1;
} finally {
  if (page) { try { await page.close(); } catch {} }
  if (server) server.kill("SIGTERM");
  await sleep(300);
}
