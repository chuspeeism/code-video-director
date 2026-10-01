#!/usr/bin/env node
// render.mjs —— 把「每一帧都由时间 t 算出来」的网页逐帧截图，再用 ffmpeg 编码成 mp4。
// 部分思路来自 howseen-ai/claude-motion-design（MIT）
//
// 用法：node render.mjs <page.html> --out <目录> [--w 1920 --h 1080 --fps 30 --dur 秒]
//         [--stills 1,5.5,9] [--from 0 --to 60] [--workers 4] [--sub 1] [--audio mix.wav]
//         [--name video.mp4] [--force]
// 页面约定：
//   window.seek = async (t) => {...}   t 单位秒，画面只由 t 决定（不许用定时器、CSS 过渡/动画）
//   window.ready = true                字体、图片等资源加载完后再设
//   window.DURATION = 6                可选，默认时长（秒）
// 依赖：在「当前工作目录」npm i playwright-core；优先用系统 Chrome，没有再用 Playwright 自带 chromium；
//       PATH 里要有 ffmpeg（或用环境变量 FFMPEG 指定）。
// 断点续渲：已存在的帧会跳过；页面 html/同目录 js/css、尺寸、帧率、子帧数变了会自动整段重渲；
//           只改了子目录里的资源时请加 --force。
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';

const FF = process.env.FFMPEG || 'ffmpeg';
const USAGE = `用法：node render.mjs <page.html> --out <目录> [--w 1920 --h 1080 --fps 30 --dur 秒]
        [--stills 1,5.5,9] [--from 0 --to 60] [--workers 4] [--sub 1] [--audio mix.wav]
        [--name video.mp4] [--force]
  --stills  只渲这几个时刻的静帧（秒，逗号分隔），拼成 <out>/stills/sheet.jpg，不出视频
  --from/--to  只渲这段时间（秒）    --workers  并行页面数    --sub K  每帧 K 个子帧做动态模糊
  --audio   把音频合进成片（以视频时长为准，音频短了补静音）    --force  已有的帧也重渲`;
const VALUE_KEYS = ['out', 'w', 'h', 'fps', 'dur', 'stills', 'from', 'to', 'workers', 'sub', 'audio', 'name'];

function die(msg) { console.error('[错误] ' + msg); process.exit(1); }
const fmt = (s) => (s < 90 ? `${s.toFixed(1)} 秒` : `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`);
const pad = (n, k) => String(n).padStart(k, '0');

function parseArgs(argv) {
  const o = { w: 1920, h: 1080, fps: 30, sub: 1, name: 'video.mp4', force: false,
    workers: Math.max(1, Math.min(4, Math.floor(os.cpus().length / 2))) };
  const pos = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--force') o.force = true;
    else if (a === '-h' || a === '--help') o.help = true;
    else if (a.startsWith('--')) {
      const k = a.slice(2);
      if (!VALUE_KEYS.includes(k)) die(`不认识的参数 ${a}\n${USAGE}`);
      if (argv[i + 1] === undefined) die(`参数 ${a} 后面缺值`);
      o[k] = argv[++i];
    } else pos.push(a);
  }
  o.page = pos[0];
  for (const k of ['w', 'h', 'fps', 'workers', 'sub']) {
    o[k] = Math.round(Number(o[k]));
    if (!(o[k] >= 1)) die(`--${k} 要是正整数`);
  }
  for (const k of ['dur', 'from', 'to']) {
    if (o[k] === undefined) continue;
    o[k] = Number(o[k]);
    if (!Number.isFinite(o[k]) || o[k] < 0) die(`--${k} 要是不小于 0 的秒数`);
  }
  if (o.stills !== undefined) {
    o.stills = String(o.stills).split(',').map((s) => s.trim()).filter(Boolean).map(Number);
    if (!o.stills.length || o.stills.some((t) => !Number.isFinite(t) || t < 0)) die('--stills 要写成 1,5.5,9 这样的秒数列表');
  }
  return o;
}

// 从「当前工作目录」解析 playwright-core / playwright，脚本本身不带 node_modules
function loadPlaywright() {
  const req = createRequire(path.join(process.cwd(), 'x.js'));
  for (const name of ['playwright-core', 'playwright']) {
    try { return { pw: req(name), name }; } catch (e) { if (e.code !== 'MODULE_NOT_FOUND') throw e; }
  }
  die('当前工作目录找不到 playwright-core。请在工作目录先运行 npm i playwright-core（只装库，不会下载浏览器）');
}

async function launchBrowser(pw) {
  const args = ['--hide-scrollbars', '--force-color-profile=srgb', '--autoplay-policy=no-user-gesture-required'];
  const first = (e) => String(e && e.message || e).split('\n')[0];
  try { return { browser: await pw.chromium.launch({ channel: 'chrome', args }), kind: '系统 Chrome' }; } catch (e1) {
    try { return { browser: await pw.chromium.launch({ args }), kind: 'Playwright 自带 chromium' }; } catch (e2) {
      die(`启动浏览器失败。请安装 Google Chrome，或在工作目录运行 npx playwright-core install chromium。
  系统 Chrome：${first(e1)}\n  自带 chromium：${first(e2)}`);
    }
  }
}

// 用 Node 自带 http 模块托管页面所在文件夹（随机端口），避免 file:// 下字体、ES 模块加载出错。支持 Range（<video> 拖动要用）。
const MIME = { '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8', '.js': 'text/javascript',
  '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp',
  '.avif': 'image/avif', '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.otf': 'font/otf',
  '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4', '.mp4': 'video/mp4', '.webm': 'video/webm',
  '.wasm': 'application/wasm', '.txt': 'text/plain; charset=utf-8', '.glsl': 'text/plain; charset=utf-8' };
function startServer(root) {
  const srv = http.createServer((req, res) => {
    let rel = '/';
    try { rel = decodeURIComponent(new URL(req.url, 'http://x').pathname); } catch {}
    let file = path.join(root, rel);
    if (path.relative(root, file).startsWith('..')) { res.writeHead(403); return res.end(); }
    try { if (fs.statSync(file).isDirectory()) file = path.join(file, 'index.html'); } catch {}
    fs.stat(file, (err, st) => {
      if (err || !st.isFile()) { res.writeHead(404); return res.end('404'); }
      const head = { 'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream',
        'Cache-Control': 'no-store', 'Accept-Ranges': 'bytes', 'Access-Control-Allow-Origin': '*' };
      const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range || '');
      if (m && st.size > 0) {
        const s = m[1] ? Number(m[1]) : Math.max(0, st.size - Number(m[2]));
        const e = Math.min(m[1] && m[2] ? Number(m[2]) : st.size - 1, st.size - 1);
        if (s > e) { res.writeHead(416, { 'Content-Range': `bytes */${st.size}` }); return res.end(); }
        res.writeHead(206, { ...head, 'Content-Range': `bytes ${s}-${e}/${st.size}`, 'Content-Length': e - s + 1 });
        return fs.createReadStream(file, { start: s, end: e }).pipe(res);
      }
      res.writeHead(200, { ...head, 'Content-Length': st.size });
      fs.createReadStream(file).pipe(res);
    });
  });
  return new Promise((ok, bad) => {
    srv.once('error', (e) => bad(e.code === 'EPERM' || e.code === 'EACCES'
      ? new Error('没有权限监听本机端口（多半是沙盒禁止了本地端口）：请在沙盒外运行，或在沙盒设置里允许本地端口绑定') : e));
    srv.listen(0, '127.0.0.1', () => ok(srv));
  });
}

// 页面指纹：html 本身 + 同目录的 js/css。变了就说明旧帧过期。
function pageHash(pagePath) {
  const dir = path.dirname(pagePath), h = crypto.createHash('sha1');
  const files = [pagePath, ...fs.readdirSync(dir).filter((f) => /\.(m?js|css)$/i.test(f)).sort().map((f) => path.join(dir, f))];
  for (const f of files) { try { h.update(path.basename(f)); h.update(fs.readFileSync(f)); } catch {} }
  return h.digest('hex').slice(0, 12);
}

async function openPage(browser, url, o, errs) {
  const ctx = await browser.newContext({ viewport: { width: o.w, height: o.h }, deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => errs.add('页面异常：' + String(e && e.message || e).split('\n')[0]));
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const loc = m.location() && m.location().url;
    if (!/favicon/.test(loc || '')) errs.add('console.error：' + m.text() + (loc ? `（${loc}）` : ''));
  });
  page.on('requestfailed', (r) => errs.add('资源加载失败：' + r.url()));
  await page.goto(url, { waitUntil: 'load', timeout: 60000 });
  try {
    await page.waitForFunction(() => window.ready === true && typeof window.seek === 'function', null, { timeout: 120000 });
  } catch {
    throw new Error('等了 120 秒页面还没就绪：确认页面定义了 window.seek，并在资源加载完后设置 window.ready = true');
  }
  await page.evaluate(() => document.fonts && document.fonts.ready.then(() => 0));
  const info = await page.evaluate(() => {
    const el = document.querySelector('#stage'), d = Number(window.DURATION);
    const r = el && el.getBoundingClientRect();
    return { duration: d > 0 ? d : null, clip: r ? { x: Math.round(r.left + scrollX), y: Math.round(r.top + scrollY),
      width: Math.round(r.width), height: Math.round(r.height) } : null };
  });
  return { ctx, page, ...info };
}

async function shoot(page, clip, t, file) {
  try { await page.evaluate((tt) => window.seek(tt), t); } catch (e) {
    const msg = String(e && e.message || e).replace(/^page\.evaluate:\s*/, '').split('\n')[0];
    throw new Error(`seek(${t.toFixed(3)}) 出错（第 ${t.toFixed(3)} 秒）：${msg}`);
  }
  const buf = await page.screenshot({ type: 'jpeg', quality: 92, ...(clip ? { clip } : {}) });
  fs.writeFileSync(file + '.tmp', buf);
  fs.renameSync(file + '.tmp', file); // 先写临时文件再改名：中途被打断也不会留下半张图
}

// 把任务切成 N 段连续区间，每段开一个页面并行渲染；每 5% 打印一次进度和预计剩余时间
async function runJobs(browser, url, o, jobs, errs, label) {
  if (!jobs.length) return;
  const n = Math.max(1, Math.min(o.workers, jobs.length)), per = Math.ceil(jobs.length / n);
  const t0 = Date.now();
  let done = 0, nextPct = 5;
  const tick = () => {
    done++;
    const pct = (done / jobs.length) * 100;
    if (pct + 1e-9 < nextPct && done < jobs.length) return;
    while (nextPct <= pct + 1e-9) nextPct += 5;
    const used = (Date.now() - t0) / 1000;
    console.log(`  ${label} ${Math.floor(pct)}%（${done}/${jobs.length}）已用 ${fmt(used)}，预计还要 ${fmt((used / done) * (jobs.length - done))}`);
  };
  await Promise.all(Array.from({ length: n }, async (_, w) => {
    const mine = jobs.slice(w * per, (w + 1) * per);
    if (!mine.length) return;
    const { ctx, page, clip } = await openPage(browser, url, o, errs);
    try { for (const j of mine) { await shoot(page, clip, j.t, j.file); tick(); } } finally { await ctx.close().catch(() => {}); }
  }));
}

function ffmpeg(args, what) {
  const r = spawnSync(FF, ['-hide_banner', '-y', '-v', 'error', ...args], { stdio: ['ignore', 'inherit', 'inherit'] });
  if (r.error || r.status !== 0) throw new Error(`ffmpeg ${what}失败（退出码 ${r.status}）`);
}

function makeSheet(dir, n) {
  const cols = Math.min(4, n), rows = Math.ceil(n / cols), sheet = path.join(dir, 'sheet.jpg');
  ffmpeg(['-start_number', '1', '-i', path.join(dir, 'still_%03d.jpg'), '-vf',
    `scale=640:-2,tile=${cols}x${rows}:padding=8:margin=8:color=0x202020`, '-frames:v', '1', '-q:v', '3', sheet], '拼静帧');
  return sheet;
}

// 编码：libx264 / yuv420p / crf 18 / faststart / BT.709；子帧 > 1 时先用 tmix 把每 K 张混成一帧（动态模糊）
function encode(o, out, frameDir, i0, i1, K) {
  const N = i1 - i0, secs = N / o.fps, file = path.join(out, o.name);
  const vf = [`scale=${o.w}:${o.h}:flags=lanczos:in_range=pc:out_range=tv:out_color_matrix=bt709`, 'format=yuv420p',
    'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv'];
  const args = K === 1
    ? ['-framerate', String(o.fps), '-start_number', String(i0 + 1), '-i', path.join(frameDir, 'f_%06d.jpg')]
    : ['-framerate', String(o.fps * K), '-start_number', String(i0 * K + 1), '-i', path.join(frameDir, 's_%07d.jpg')];
  if (K > 1) vf.unshift(`tmix=frames=${K}`, `select='eq(mod(n\\,${K})\\,${K - 1})'`, `setpts=N/(${o.fps}*TB)`);
  if (o.audio) args.push(...(i0 > 0 ? ['-ss', (i0 / o.fps).toFixed(3)] : []), '-i', path.resolve(o.audio));
  args.push('-map', '0:v:0', '-vf', vf.join(','), '-frames:v', String(N), '-r', String(o.fps),
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
    '-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709', '-color_range', 'tv');
  if (o.audio) args.push('-map', '1:a:0', '-af', 'apad', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000');
  args.push('-t', secs.toFixed(3), '-movflags', '+faststart', file);
  ffmpeg(args, '编码');
  return { file, secs, N };
}

async function main() {
  const o = parseArgs(process.argv.slice(2));
  if (o.help || !o.page) { console.log(USAGE); process.exit(o.help ? 0 : 1); }
  const pagePath = path.resolve(o.page);
  if (!fs.existsSync(pagePath)) die('找不到页面文件：' + pagePath);
  if (o.audio && !fs.existsSync(o.audio)) die('找不到音频文件：' + path.resolve(o.audio));
  if (spawnSync(FF, ['-version']).error) die('找不到 ffmpeg：请先安装并加入 PATH（或用环境变量 FFMPEG 指定路径）');
  const out = path.resolve(o.out || path.join(path.dirname(pagePath), 'out'));
  fs.mkdirSync(out, { recursive: true });
  const { pw, name: pwName } = loadPlaywright();
  const srv = await startServer(path.dirname(pagePath)).catch((e) => die(e.message));
  const url = `http://127.0.0.1:${srv.address().port}/${encodeURIComponent(path.basename(pagePath))}?render=1`;
  const { browser, kind } = await launchBrowser(pw);
  const errs = new Set();
  let code = 0;
  try {
    const probe = await openPage(browser, url, o, errs);
    await probe.ctx.close();
    if (probe.clip && (probe.clip.width !== o.w || probe.clip.height !== o.h)) {
      console.log(`[警告] #stage 实际是 ${probe.clip.width}×${probe.clip.height}，和视频尺寸 ${o.w}×${o.h} 不一致，编码时会被拉伸；请用 --w/--h 对齐或改页面`);
    }
    console.log(`页面：${pagePath}\n输出：${out}\n浏览器：${kind}（${pwName}）；截图区域：${probe.clip ? '#stage' : '整个视口'}`);

    if (o.stills) { // ---- 只出静帧 + 拼图 ----
      const dir = path.join(out, 'stills');
      fs.mkdirSync(dir, { recursive: true });
      for (const f of fs.readdirSync(dir)) if (/^still_\d+\.jpg$/.test(f)) fs.unlinkSync(path.join(dir, f));
      const jobs = o.stills.map((t, k) => ({ t, file: path.join(dir, `still_${pad(k + 1, 3)}.jpg`) }));
      await runJobs(browser, url, o, jobs, errs, '静帧');
      const sheet = makeSheet(dir, jobs.length);
      console.log(`静帧 ${jobs.length} 张（顺序：${o.stills.map((t, k) => `${k + 1}=${t}s`).join('，')}）\n拼图：${sheet}`);
    } else {        // ---- 出视频 ----
      const dur = o.dur ?? probe.duration;
      if (!(dur > 0)) throw new Error('不知道视频多长：用 --dur 指定秒数，或在页面里设 window.DURATION');
      const from = o.from ?? 0, to = o.to ?? dur, K = o.sub;
      const i0 = Math.round(from * o.fps), i1 = Math.round(to * o.fps);
      if (i1 <= i0) throw new Error(`时间区间不对：--from ${from} --to ${to}`);
      const frameDir = path.join(out, K === 1 ? 'frames.noindex' : `frames_sub${K}.noindex`);  // .noindex：macOS 的 Spotlight 不去索引成千上万张帧
      fs.mkdirSync(frameDir, { recursive: true });
      const metaFile = path.join(frameDir, 'meta.json');
      const meta = { w: o.w, h: o.h, fps: o.fps, sub: K, page: path.basename(pagePath), hash: pageHash(pagePath) };
      let force = o.force;
      try {
        const old = JSON.parse(fs.readFileSync(metaFile, 'utf8'));
        if (!force && JSON.stringify(old) !== JSON.stringify(meta)) { force = true; console.log('[提示] 页面或参数和上次不同，旧帧作废，整段重渲'); }
      } catch {}
      fs.writeFileSync(metaFile, JSON.stringify(meta));
      const jobs = [];
      for (let i = i0; i < i1; i++) for (let j = 0; j < K; j++) {
        const t = K === 1 ? i / o.fps : Math.max(0, i / o.fps + (j - (K - 1) / 2) / (o.fps * K));
        const file = K === 1 ? path.join(frameDir, `f_${pad(i + 1, 6)}.jpg`) : path.join(frameDir, `s_${pad(i * K + j + 1, 7)}.jpg`);
        if (force || !fs.existsSync(file) || fs.statSync(file).size === 0) jobs.push({ t, file });
      }
      const total = (i1 - i0) * K;
      console.log(`规格：${o.w}×${o.h} @${o.fps}fps，渲 ${from.toFixed(2)}–${to.toFixed(2)} 秒共 ${i1 - i0} 帧` +
        `${K > 1 ? `（每帧 ${K} 个子帧做动态模糊）` : ''}，${Math.min(o.workers, Math.max(1, jobs.length))} 个页面并行`);
      if (jobs.length < total) console.log(`[提示] 跳过已存在的 ${total - jobs.length} 张截图（要全部重渲加 --force）`);
      const t0 = Date.now();
      await runJobs(browser, url, o, jobs, errs, '渲染');
      if (jobs.length) console.log(`截图完成，用时 ${fmt((Date.now() - t0) / 1000)}`);
      const r = encode(o, out, frameDir, i0, i1, K);
      const mb = (fs.statSync(r.file).size / 1048576).toFixed(1);
      console.log(`成片：${r.file}（${r.N} 帧，${r.secs.toFixed(2)} 秒，${mb} MB${o.audio ? '，含音轨' : ''}）`);
    }
  } catch (e) {
    console.error('[错误] ' + (e && e.message || e));
    code = 1;
  } finally {
    await browser.close().catch(() => {});
    srv.close();
  }
  if (errs.size) {
    console.log(`[警告] 页面报了 ${errs.size} 条错误（最多列 20 条）：`);
    [...errs].slice(0, 20).forEach((e) => console.log('  - ' + e));
  }
  process.exit(code);
}

main().catch((e) => die(e && e.stack || String(e)));
