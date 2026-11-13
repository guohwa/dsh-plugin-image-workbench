/**
 * 同步上游 handraw-style 风格目录到工作台（供定时任务调用，幂等）。
 *
 * 流程：Gitee 国内镜像优先拉 STYLES.md / LAYOUTS.md / COLORS.md → 交给
 * server/catalog.py 生成目录 → 与本地 server/catalog.json 比对 → 只有真的
 * 有差异才写盘、补下载新增样图、调后端 /api/catalog/reload 热更新。
 *
 * 用法：node scripts/sync-handraw-style.mjs
 * 输出：单行 JSON，{ok, updated, diff, images, reload} 或 {ok:false, error}
 * 环境变量：
 *   PYTHON        指定解释器
 *   WB_PORT       后端端口（默认 8092）
 *   WB_SERVER_DIR 运行时 server 目录。宿主调度器必传：插件装在 node_modules 时，
 *                 代码会被复制到 ~/.dsh/image-workbench/server，不传就会写进只读副本。
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import https from 'node:https';
import http from 'node:http';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { fetchStyleImages } from './fetch-style-images.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const SERVER = process.env.WB_SERVER_DIR
  ? path.resolve(process.env.WB_SERVER_DIR)
  : path.join(ROOT, 'server');
const CATALOG_FILE = path.join(SERVER, 'catalog.json');
const PORT = Number(process.env.WB_PORT || 8092);

/** 国内镜像优先；GitHub raw 只作境外兜底。 */
const SOURCES = [
  'https://gitee.com/yang02010/handraw-style/raw/master',
  'https://raw.githubusercontent.com/yang0/handraw-style/main',
];
const FILES = ['STYLES.md', 'LAYOUTS.md', 'COLORS.md'];

function get(url, redirects = 0) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, {
      timeout: 20000,
      headers: { 'User-Agent': 'Mozilla/5.0 handraw-style-sync' },
    }, res => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location && redirects < 5) {
        res.resume();
        return resolve(get(new URL(res.headers.location, url).toString(), redirects + 1));
      }
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error(`HTTP ${res.statusCode} ${url}`));
      }
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => resolve(Buffer.concat(chunks)));
    });
    req.on('timeout', () => req.destroy(new Error('请求超时 ' + url)));
    req.on('error', reject);
  });
}

async function fetchMarkdowns(dir) {
  let lastErr;
  for (const base of SOURCES) {
    try {
      const got = [];
      for (const f of FILES) {
        const buf = await get(base + '/' + f);
        if (buf.length < 1000) throw new Error(`${f} 内容异常（${buf.length} 字节）`);
        got.push([f, buf]);
      }
      // 三个文件全部成功才落盘，避免半套目录混入
      for (const [f, buf] of got) fs.writeFileSync(path.join(dir, f), buf);
      return base;
    } catch (e) {
      lastErr = e;
      for (const f of FILES) {
        const p = path.join(dir, f);
        if (fs.existsSync(p)) fs.rmSync(p, { force: true });
      }
    }
  }
  throw new Error('所有源都拉不到 Markdown：' + (lastErr && lastErr.message || '未知错误'));
}

function pythonBin() {
  const candidates = [
    process.env.PYTHON,
    path.join(SERVER, '.venv', 'Scripts', 'python.exe'),
    path.join(SERVER, '.venv', 'bin', 'python'),
    'python3', 'python',
  ].filter(Boolean);
  for (const c of candidates) {
    if (c === 'python3' || c === 'python') return c;
    if (fs.existsSync(c)) return c;
  }
  return 'python';
}

function buildCatalog(repoDir, outFile) {
  const r = spawnSync(pythonBin(), [path.join(SERVER, 'catalog.py'), '--force'], {
    env: { ...process.env, HANDRAW_REPO: repoDir, CATALOG_OUT: outFile },
    encoding: 'utf8',
    timeout: 60000,
  });
  if (r.status !== 0) {
    throw new Error('catalog.py 执行失败：' + String(r.stderr || r.stdout || r.error || '').slice(0, 300));
  }
  return JSON.parse(fs.readFileSync(outFile, 'utf8'));
}

function diff(prev, next) {
  const out = {};
  for (const k of ['styles', 'layouts', 'colors']) {
    const o = new Map((prev[k] || []).map(x => [x.id, x]));
    const n = new Map((next[k] || []).map(x => [x.id, x]));
    out[k] = {
      before: o.size,
      after: n.size,
      added: [...n.keys()].filter(id => !o.has(id)),
      removed: [...o.keys()].filter(id => !n.has(id)),
      changed: [...n.keys()].filter(id => o.has(id)
        && JSON.stringify(o.get(id)) !== JSON.stringify(n.get(id))),
    };
  }
  return out;
}

/** 本机回环必须用 node:http：全局 fetch 会被代理 dispatcher 带偏。 */
function reloadCatalog() {
  return new Promise(resolve => {
    const req = http.request({
      host: '127.0.0.1', port: PORT, path: '/api/catalog/reload',
      method: 'POST', timeout: 5000,
    }, res => {
      let body = '';
      res.on('data', c => { body += c; });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); }
        catch { resolve({ ok: false, error: '响应非 JSON：' + body.slice(0, 120) }); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('reload 超时')));
    req.on('error', e => resolve({ ok: false, error: '后端未运行或不可达：' + e.message }));
    req.end();
  });
}

async function main() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'handraw-sync-'));
  try {
    const source = await fetchMarkdowns(tmp);
    const outFile = path.join(tmp, 'catalog.json');
    const next = buildCatalog(tmp, outFile);
    const prev = JSON.parse(fs.readFileSync(CATALOG_FILE, 'utf8'));

    // 上游格式若突变，解析出的条目会骤减；宁可不动本地，也不能把目录冲成空壳
    for (const k of ['styles', 'layouts', 'colors']) {
      if ((next[k] || []).length < (prev[k] || []).length * 0.8) {
        throw new Error(`${k} 条目骤减（${(prev[k] || []).length} → ${(next[k] || []).length}），拒绝覆盖`);
      }
    }

    const d = diff(prev, next);
    const updated = ['styles', 'layouts', 'colors'].some(k =>
      d[k].added.length || d[k].removed.length || d[k].changed.length);
    const counts = { styles: next.styles.length, layouts: next.layouts.length, colors: next.colors.length };
    if (!updated) {
      console.log(JSON.stringify({ ok: true, updated: false, source, counts, message: '上游无变化' }));
      return;
    }

    fs.copyFileSync(outFile, CATALOG_FILE);
    const images = await fetchStyleImages({ serverDir: SERVER, log: () => {} });
    const reload = await reloadCatalog();
    console.log(JSON.stringify({ ok: true, updated: true, source, counts, diff: d, images, reload }));
  } catch (e) {
    console.log(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
    process.exitCode = 1;
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

main();
