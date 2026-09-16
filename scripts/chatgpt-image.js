// ChatGPT 出图桥（Playwright 驱动网页版，用订阅额度，不花 API 钱）
//
// 用法：
//   node chatgpt-image.js --prompt "描述" --out D:\path\out.png [--size 1024x1024] [--headless]
//
// 首次运行需在弹出的浏览器里登录 ChatGPT（唯一的人工步骤），登录态存在 data/chatgpt-profile。
// 依赖：playwright-core（已 npm 装在本项目；用系统 Chrome，不下载浏览器）
// 网络：ChatGPT 必须走代理（默认 http://127.0.0.1:7890，可用 CHATGPT_PROXY 覆盖）
//
// 输出：成功打印 JSON {"ok":true,"file":"..."}；失败 {"ok":false,"msg":"..."}
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const PROFILE = path.join(ROOT, "data", "chatgpt-profile");
const PROXY = process.env.CHATGPT_PROXY || "http://127.0.0.1:7890";

function arg(name, def = "") {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : def;
}
const hasFlag = (name) => process.argv.includes(`--${name}`);

function out(obj) {
  console.log(JSON.stringify(obj));
}

async function main() {
  const prompt = arg("prompt");
  const outFile = arg("out");

  // --login：只打开浏览器让用户登录（一次性人工步骤），登录态存进 profile
  if (hasFlag("login")) {
    fs.mkdirSync(PROFILE, { recursive: true });
    const ctx = await chromium.launchPersistentContext(PROFILE, {
      headless: false, channel: "chrome", locale: "zh-CN",
      viewport: { width: 1200, height: 820 },
      proxy: { server: PROXY },
      args: ["--disable-blink-features=AutomationControlled"],
    });
    const pg = ctx.pages()[0] || (await ctx.newPage());
    await pg.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded", timeout: 90000 }).catch(() => {});
    console.log("请在弹出的浏览器里登录 ChatGPT（登录完这个窗口会自动关）…");
    // 等输入框出现 = 登录成功（最多 10 分钟）
    const until = Date.now() + 600000;
    while (Date.now() < until) {
      await new Promise((r) => setTimeout(r, 5000));
      const ok = await pg.evaluate(() =>
        !!(document.querySelector("#prompt-textarea") || document.querySelector('div[contenteditable="true"]'))
      ).catch(() => false);
      if (ok) break;
    }
    const logged = await pg.evaluate(() =>
      !!(document.querySelector("#prompt-textarea") || document.querySelector('div[contenteditable="true"]'))
    ).catch(() => false);
    await ctx.close().catch(() => {});
    out({ ok: logged, msg: logged ? "登录成功，profile 已保存" : "未检测到登录态（超时）" });
    return;
  }
  if (!prompt || !outFile) {
    out({ ok: false, msg: "缺 --prompt 或 --out" });
    process.exit(2);
  }
  fs.mkdirSync(PROFILE, { recursive: true });
  fs.mkdirSync(path.dirname(outFile), { recursive: true });

  // ⚠️ 必须真实 Chrome + 有头：无头/自带 Chromium 会被 Cloudflare 403
  const launchOpts = {
    headless: false,
    channel: "chrome",
    viewport: { width: 1400, height: 950 },
    locale: "zh-CN",
    proxy: { server: PROXY },              // ChatGPT 必须走代理
    args: ["--disable-blink-features=AutomationControlled"],
  };
  const context = await chromium.launchPersistentContext(PROFILE, launchOpts);

  try {
    const page = context.pages()[0] || (await context.newPage());
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "webdriver", { get: () => undefined });
    });

    // 拦截响应，收集生成的图片 URL（files.oaiusercontent / blob）
    const found = [];
    page.on("response", async (res) => {
      try {
        const u = res.url();
        if (/\.(png|jpe?g|webp)(\?|$)/i.test(u) && /oaiusercontent|openai|blob|backend-api\/files/i.test(u)) {
          found.push(u);
        }
      } catch { /* ignore */ }
    });

    await page.goto("https://chatgpt.com/", { waitUntil: "domcontentloaded", timeout: 90000 });
    await page.waitForLoadState("networkidle", { timeout: 30000 }).catch(() => {});
    await new Promise((r) => setTimeout(r, 2500));

    // 登录判断：出现输入框即视为已登录
    const editors = ['#prompt-textarea', 'div[contenteditable="true"]', 'textarea'];
    let editor = null;
    for (const sel of editors) {
      const loc = page.locator(sel).first();
      if ((await loc.count()) > 0) { editor = loc; break; }
    }
    if (!editor) {
      out({ ok: false, msg: "未找到输入框——可能未登录，请先在弹出的浏览器里登录 ChatGPT" });
      return;
    }

    // 记录基线（避免抓到历史图片）
    const baseline = new Set(
      await page.evaluate(() =>
        [...document.querySelectorAll("img")].map((i) => i.src).filter(Boolean)
      )
    );

    await editor.click();
    await editor.fill(`请画一张图：${prompt}。只出图，不要多余说明。`);
    await new Promise((r) => setTimeout(r, 600));
    await page.keyboard.press("Enter");

    // 等待新图片出现（ChatGPT 出图通常 20~90 秒）
    const deadline = Date.now() + 300000;
    let imgUrl = "";
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 4000));
      const urls = await page.evaluate(() =>
        [...document.querySelectorAll("img")]
          .map((i) => i.src || "")
          .filter((s) => s && (s.startsWith("blob:") || /oaiusercontent|openai|backend-api/i.test(s)))
      );
      const fresh = urls.filter((u) => !baseline.has(u));
      if (fresh.length) { imgUrl = fresh[fresh.length - 1]; break; }
      const hit = found.filter((u) => !baseline.has(u));
      if (hit.length) { imgUrl = hit[hit.length - 1]; break; }
      const txt = (await page.evaluate(() => document.body.innerText.slice(-800))) || "";
      if (/无法|不能|不符合|violat|can't|unable/i.test(txt) && /图片|image/i.test(txt)) {
        out({ ok: false, msg: "ChatGPT 拒绝了该请求：" + txt.slice(-120) });
        return;
      }
    }
    if (!imgUrl) { out({ ok: false, msg: "超时未拿到图片" }); return; }

    // 页面内取图（带 cookie）→ base64 落盘
    const b64 = await page.evaluate(async (u) => {
      const r = await fetch(u, { credentials: "include" });
      const buf = await r.arrayBuffer();
      let bin = "";
      const bytes = new Uint8Array(buf);
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return btoa(bin);
    }, imgUrl);
    fs.writeFileSync(outFile, Buffer.from(b64, "base64"));
    out({ ok: true, file: outFile, bytes: fs.statSync(outFile).size });
  } catch (e) {
    out({ ok: false, msg: String(e.message || e) });
  } finally {
    await context.close().catch(() => {});
  }
}

main();
