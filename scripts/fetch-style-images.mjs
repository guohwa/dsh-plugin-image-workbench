/**
 * 样图下载器（风格 / 图型 / 主题色三类预览图）。
 *
 * 为什么要有这个文件：repo_images/ 里的 574 张样例图不进 npm 包（包体积会从
 * 几百 KB 涨到十几 MB，而且图片本来就该放 CDN）。但工作台后端的 /img/{path}
 * 在文件缺失时会返回一张渐变 SVG 占位图，于是全新安装打开「配色库 / 风格库」
 * 全是色块 —— 看起来像装坏了。安装包里必须自带一次「把样图拉下来」的动作，
 * 光靠 README 让用户手跑 `node install.mjs` 是不够的。
 *
 * 约定：
 *   - 由 host.js 在插件启动时后台调用（不阻塞工作台启动），失败只写日志；
 *   - install.mjs 也复用它，安装完顺手补齐，两处逻辑只有这一份；
 *   - 幂等：已存在且非空的文件直接跳过，重跑只补缺失项；
 *   - 只接受 image/* 响应，避免把 Gitee 的 302 登录页 HTML 写成 .webp 坏图。
 */
import fs from 'node:fs';
import path from 'node:path';

/** 样图源，按「CDN 单文件 → Gitee 镜像 → GitHub raw」顺序尝试。 */
export const IMAGE_SOURCES = [
  'https://img.handraw-style.com',
  'https://gitee.com/yang02010/handraw-style/raw/master',
  'https://raw.githubusercontent.com/yang0/handraw-style/main',
];

/** catalog.json 里 img 字段形如 images/individual/FA/FA-001.webp，落到 repo_images/ 时去掉 images/ 前缀。 */
export function localPathOf(repoImages, rel) {
  return path.join(repoImages, String(rel).replace(/^images\//, ''));
}

/** 从 catalog.json 收集需要下载的样图相对路径（去重）。 */
export function collectRels(serverDir) {
  const file = path.join(serverDir, 'catalog.json');
  const rels = [];
  const cat = JSON.parse(fs.readFileSync(file, 'utf8'));
  for (const key of ['styles', 'layouts', 'colors']) {
    for (const item of cat[key] || []) if (item && item.img) rels.push(item.img);
  }
  return [...new Set(rels)];
}

/**
 * 下载缺失样图。
 * @param {{serverDir:string, log?:Function, concurrency?:number, timeoutMs?:number, sources?:string[]}} opts
 * @returns {Promise<{ok:boolean, total:number, done:number, skip:number, fail:number, error?:string}>}
 */
export async function fetchStyleImages(opts = {}) {
  const serverDir = opts.serverDir;
  const log = opts.log || (() => {});
  const concurrency = Math.max(1, Math.min(24, opts.concurrency || 12));
  const timeoutMs = opts.timeoutMs || 15000;
  const sources = opts.sources || IMAGE_SOURCES;
  if (!serverDir) return { ok: false, total: 0, done: 0, skip: 0, fail: 0, error: '缺少 serverDir' };

  const repoImages = path.join(serverDir, 'repo_images');
  let rels = [];
  try {
    rels = collectRels(serverDir);
  } catch (e) {
    return { ok: false, total: 0, done: 0, skip: 0, fail: 0,
             error: '读取 catalog.json 失败：' + (e && e.message || e) };
  }
  if (!rels.length) return { ok: true, total: 0, done: 0, skip: 0, fail: 0 };

  fs.mkdirSync(repoImages, { recursive: true });
  const exists = p => { try { return fs.statSync(p).size > 0; } catch { return false; } };

  const missing = rels.filter(rel => !exists(localPathOf(repoImages, rel)));
  const skip = rels.length - missing.length;
  if (!missing.length) return { ok: true, total: rels.length, done: 0, skip, fail: 0 };

  log(`正在下载缺失样图 ${missing.length} 张（共 ${rels.length} 张，已存在 ${skip} 张）…`);
  let done = 0, fail = 0;

  const getOne = async rel => {
    const dst = localPathOf(repoImages, rel);
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    for (const base of sources) {
      const url = base.replace(/\/$/, '') + '/' + String(rel).replace(/^\/+/, '');
      try {
        const ac = new AbortController();
        const timer = setTimeout(() => ac.abort(), timeoutMs);
        const res = await fetch(url, { signal: ac.signal, redirect: 'follow' });
        clearTimeout(timer);
        if (!res.ok) continue;
        const type = (res.headers.get('content-type') || '').toLowerCase();
        if (!type.startsWith('image/')) continue;
        const buf = Buffer.from(await res.arrayBuffer());
        if (buf.length < 200) continue;
        fs.writeFileSync(dst, buf);
        done += 1;
        return;
      } catch { /* 换下一个源 */ }
    }
    fail += 1;
  };

  for (let i = 0; i < missing.length; i += concurrency) {
    await Promise.all(missing.slice(i, i + concurrency).map(getOne));
  }

  log(`样图下载结束：已下 ${done} 张，跳过 ${skip} 张，失败 ${fail} 张`);
  return { ok: fail === 0, total: rels.length, done, skip, fail };
}
