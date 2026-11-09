/**
 * 把插件挂进当前 DSH desktop profile。
 *
 * 用法：
 *   node install.mjs              安装（幂等，可在任意目录重复执行）
 *   node install.mjs --uninstall  从 profile 配置里摘掉插件并断开目录联接
 *
 * 改动范围：
 *   1. profile/node_modules 下建指向插件源码目录的目录联接（父目录自动创建）；
 *   2. profile/package.json 的 dependencies 与 dsh.profile.bundles（改前备份）；
 *   3. pnpm-lock.yaml 里直接补/删 importer 记录，不联网、不动 node_modules。
 * 不会执行完整 pnpm install（避免重拉已装的其他插件），也不会重启 DSH。
 *
 * 顺序是「先建联接、后写配置」：联接建不起来时 profile 保持原样，DSH 照常启动，
 * 不会出现「配置已注册、目录联接没建」的半装状态。这一点在全新 profile
 * （没有 node_modules、没有 pnpm-lock.yaml）上尤其重要。
 *
 * 号池已经并入工作台：安装时顺手把旧的独立号池插件 dsh-plugin-image-pool
 * 从 profile 摘掉（只改配置，不删它的源码目录和账号数据），侧栏就只剩一个
 * 工作台入口。旧插件下次 DSH 启动不再加载，号池改由工作台后端自己拉起。
 *
 * 整包自包含：插件自带 vendor/chatgpt2api（号池服务端源码 + uv）与 server/
 * （工作台后端代码）。开发机上仍优先用工作区里的 image-local/，把这个插件
 * 单独拷到另一台机器时会自动落到 server/，不要求对方装任何第三方插件。
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';

const NAME = 'dsh-plugin-image-workbench';
const LEGACY = 'dsh-plugin-image-pool';
const PLUGIN_DIR = path.resolve(fileURLToPath(new URL('.', import.meta.url)));
const PROFILE_DIR = path.join(process.env.DSH_HOME || path.join(os.homedir(), '.dsh'), 'profiles', 'desktop');
const PKG_FILE = path.join(PROFILE_DIR, 'package.json');
const LOCK_FILE = path.join(PROFILE_DIR, 'pnpm-lock.yaml');
const LINK_PATH = path.join(PROFILE_DIR, 'node_modules', NAME);
const uninstall = process.argv.includes('--uninstall');

/* Node 16 及更早没有全局 fetch，脚本会在改完配置之后才报错，留下半装状态。
   这里先拦一道，报错时 profile 一个字节都没动。 */
const nodeMajor = Number(String(process.versions.node).split('.')[0]);
if (nodeMajor < 18) {
  console.error('当前 Node 版本为 ' + process.versions.node + '，本安装器需要 Node 18 及以上（建议 20+）。');
  console.error('升级 Node 后重新执行；profile 未被修改。');
  process.exit(1);
}

if (!fs.existsSync(PKG_FILE)) {
  console.error('找不到 DSH profile：' + PKG_FILE);
  console.error('请确认 DSH 桌面端已至少启动过一次，或用 DSH_HOME 指定数据目录。');
  process.exit(1);
}

const raw = fs.readFileSync(PKG_FILE, 'utf8');
const pkg = JSON.parse(raw);
const deps = pkg.dependencies || (pkg.dependencies = {});
const bundles = pkg.dsh?.profile?.bundles;
if (!Array.isArray(bundles)) {
  console.error('profile/package.json 缺少 dsh.profile.bundles，已停止，不做任何修改。');
  process.exit(1);
}

/* ---------- 目录联接 ---------- */

/** 判定当前联接状态：missing 不存在 / dir 是真实目录 / ok 有效联接 / dead 失效联接 */
function linkState() {
  let st = null;
  try { st = fs.lstatSync(LINK_PATH); } catch { return 'missing'; }
  if (!st.isSymbolicLink()) return 'dir';
  try { fs.realpathSync(LINK_PATH); return 'ok'; } catch { return 'dead'; }
}

function removeLink() {
  try { fs.unlinkSync(LINK_PATH); return true; } catch (_) { /* 试下面的 */ }
  try { fs.rmdirSync(LINK_PATH); return true; } catch (_) { return false; }
}

/**
 * 建立（或修正）目录联接。
 * 三种非理想状态都必须处理，否则会出现「装上了但其实指向旧目录」的静默错误：
 *   - 父目录不存在（全新 profile）  -> 自动创建
 *   - 指向别的路径（换目录重装）    -> 删除后重建
 *   - 死链（旧目录已被删）          -> 删除后重建
 */
function ensureLink() {
  const parent = path.dirname(LINK_PATH);
  const createdParent = !fs.existsSync(parent);
  fs.mkdirSync(parent, { recursive: true });
  if (createdParent) console.log('已创建目录 -> ' + parent);

  const st = linkState();
  if (st === 'dir') {
    console.error('node_modules/' + NAME + ' 是一个真实目录，不是目录联接。');
    console.error('为避免误删你的文件，安装器不做改动。请先手动移走它，再重新执行。');
    process.exit(1);
  }
  if (st === 'ok') {
    const cur = fs.realpathSync(LINK_PATH);
    if (path.resolve(cur).toLowerCase() === path.resolve(PLUGIN_DIR).toLowerCase()) {
      console.log('目录联接已存在，指向本目录 -> ' + LINK_PATH);
      return;
    }
    console.log('目录联接原指向 ' + cur + '，与本次安装目录不同，正在重建。');
    if (!removeLink()) {
      console.error('无法删除旧联接：' + LINK_PATH);
      process.exit(1);
    }
  } else if (st === 'dead') {
    console.log('发现失效的目录联接（目标已不存在），重建中。');
    if (!removeLink()) {
      console.error('无法删除失效联接：' + LINK_PATH);
      process.exit(1);
    }
  }

  try {
    fs.symlinkSync(PLUGIN_DIR, LINK_PATH, 'junction');
  } catch (e) {
    console.error('建立目录联接失败：' + (e && e.message || e));
    console.error('常见原因：profile 目录无写入权限，或系统禁用了目录联接。profile 未被修改。');
    process.exit(1);
  }
  console.log('已建立目录联接 -> ' + LINK_PATH);
}

/* ---------- 包完整性 ---------- */

function checkAssets() {
  const critical = ['host.js', 'client.js', 'cordis.patch.yml',
                    'server/app.py', 'server/static/index.html', 'server/catalog.json'];
  const optional = ['vendor/chatgpt2api/runtime/uv.exe', 'server/requirements.txt'];
  const missing = critical.filter(f => !fs.existsSync(path.join(PLUGIN_DIR, f)));
  if (missing.length) {
    console.error('插件包不完整，缺少：' + missing.join('、'));
    console.error('这通常是解压不完整或下载中断。请重新获取完整包后重试。');
    process.exit(1);
  }
  for (const f of optional) {
    if (!fs.existsSync(path.join(PLUGIN_DIR, f)))
      console.warn('提醒：未找到 ' + f + '，首次启动时将改用系统 Python 或自动下载运行时。');
  }
}

/* ---------- profile 配置 ---------- */

let removedLegacy = false;

/** 写入 dependencies 与 bundles，仅在确有变化时备份并落盘。 */
function applyConfig() {
  let changed = false;
  if (uninstall) {
    if (deps[NAME]) { delete deps[NAME]; changed = true; }
    const i = bundles.indexOf(NAME);
    if (i >= 0) { bundles.splice(i, 1); changed = true; }
  } else {
    const linkSpec = 'link:' + PLUGIN_DIR.replace(/\\/g, '/');
    if (deps[NAME] !== linkSpec) { deps[NAME] = linkSpec; changed = true; }
    if (!bundles.includes(NAME)) { bundles.push(NAME); changed = true; }
    // 旧独立号池插件只从 bundle / 依赖里摘掉，文件与账号数据一律保留
    if (deps[LEGACY]) { delete deps[LEGACY]; changed = true; removedLegacy = true; }
    const j = bundles.indexOf(LEGACY);
    if (j >= 0) { bundles.splice(j, 1); changed = true; removedLegacy = true; }
  }

  if (!changed) { console.log('profile 配置已经是目标状态，无需修改。'); return; }

  // 写文件之前再确认一次目标文件没被别的进程改掉，降低覆盖别人改动的风险。
  if (fs.readFileSync(PKG_FILE, 'utf8') !== raw) {
    console.error('profile/package.json 在安装过程中被其他进程修改，已停止以免覆盖。');
    console.error('请关闭 DSH 后重新执行，或手动核对配置。');
    process.exit(1);
  }

  const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
  const backup = PKG_FILE + '.bak_' + stamp;
  fs.writeFileSync(backup, raw, 'utf8');
  fs.writeFileSync(PKG_FILE, JSON.stringify(pkg, null, 2) + '\n', 'utf8');
  console.log('已备份原配置 -> ' + backup);
  console.log('已更新 -> ' + PKG_FILE);
}

/**
 * 直接补/删 lock 的 importer 记录，不调用 pnpm install。
 * link 依赖在 lock v9 里的标准写法（已实测，跨盘符时 specifier 与 version 都是绝对路径）。
 * lock 不存在时跳过——DSH 下次执行 pnpm install 会自行生成。
 */
function dropLockEntry(text, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return text.replace(new RegExp('      ' + escaped + ':\\n(?:        [^\\n]*\\n){2}'), '');
}

function syncLock(linkSpec) {
  if (!fs.existsSync(LOCK_FILE)) return;
  const before = fs.readFileSync(LOCK_FILE, 'utf8');
  const entry = '      ' + NAME + ':\n        specifier: ' + linkSpec + '\n        version: ' + linkSpec + '\n';
  let after = before;
  if (uninstall) after = dropLockEntry(after, NAME);
  if (removedLegacy) after = dropLockEntry(after, LEGACY);
  if (!uninstall && !before.includes('      ' + NAME + ':')) {
    const marker = '\npackages:\n';
    const at = after.indexOf(marker);
    if (at < 0) { console.warn('lock 结构不符合预期，跳过（不影响插件加载）。'); return; }
    after = after.slice(0, at) + entry + after.slice(at);
  }
  if (after === before) return;
  const backup = LOCK_FILE + '.bak_' + new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
  fs.writeFileSync(backup, before, 'utf8');
  fs.writeFileSync(LOCK_FILE, after, 'utf8');
  console.log('已备份并同步 -> ' + LOCK_FILE);
}

/* ---------- 后端代码同步 ---------- */

/**
 * 把开发目录 image-local/ 的纯代码同步进插件 server/。
 * 只覆盖代码，绝不删目标里已有的图片、配置、数据库等运行态数据。
 * 发布后的插件包旁边没有 image-local/，直接跳过，用包内自带的 server/。
 */
function syncServer() {
  const src = path.resolve(PLUGIN_DIR, '..', 'image-local');
  const dst = path.join(PLUGIN_DIR, 'server');
  if (!fs.existsSync(path.join(src, 'app.py'))) {
    console.log('旁边没有 image-local/，使用插件包内自带的 server/。');
    return;
  }
  const skipDirs = new Set(['.venv', '__pycache__', 'images', 'thumbs', 'refs',
                            'repo_images', 'workbench', '.pytest_cache']);
  const skipFiles = new Set(['config.local.json', 'app.db', 'history.db', '.DS_Store']);
  const junk = n => skipFiles.has(n) || /\.(log|pyc|db|vps_original)$/.test(n)
                    || /\.bak_/.test(n);
  // 开发机上的源文件可能是 CRLF，仓库里存的是 LF。不归一的话每次同步
  // 都会把整个文件标成改动，commit 里几千行假 diff，历史没法看。
  const textExt = new Set(['.py', '.html', '.js', '.mjs', '.json', '.txt',
                           '.md', '.css', '.yml', '.yaml']);
  const syncFile = (from, to) => {
    if (!textExt.has(path.extname(from).toLowerCase())) {
      fs.copyFileSync(from, to);
      return;
    }
    const s = fs.readFileSync(from, 'utf8').replace(/\r\n?/g, '\n');
    fs.writeFileSync(to, s, 'utf8');
  };
  let copied = 0;
  const walk = (from, to) => {
    fs.mkdirSync(to, { recursive: true });
    for (const e of fs.readdirSync(from, { withFileTypes: true })) {
      const a = path.join(from, e.name), b = path.join(to, e.name);
      if (e.isDirectory()) { if (!skipDirs.has(e.name)) walk(a, b); continue; }
      if (!e.isFile() || junk(e.name)) continue;
      syncFile(a, b);
      copied += 1;
    }
  };
  walk(src, dst);
  fs.writeFileSync(path.join(dst, 'requirements.txt'),
                   'fastapi\nuvicorn\nhttpx\npydantic\npillow\npython-multipart\n', 'utf8');
  console.log('已同步工作台后端代码 -> server/（' + copied + ' 个文件，不含账号/图片/配置）。');
}

/* ---------- 样图下载 ---------- */

/**
 * 安装时自动下载真实样图（用 Node 直接拉，不依赖 Python 虚拟环境）。
 *
 * 分层来源：
 *   1) CDN 单文件  https://img.handraw-style.com  —— 国内国外都通，最小最快
 *   2) Gitee 镜像 raw  https://gitee.com/yang02010/handraw-style/raw/master
 *   3) GitHub raw     https://raw.githubusercontent.com/yang0/handraw-style/main
 * 失败不阻断安装，但会明确提示；重跑 `node install.mjs` 只补缺失项，已下的自动跳过。
 */
async function fetchStyleImages() {
  const repoImages = path.join(PLUGIN_DIR, 'server', 'repo_images');
  fs.mkdirSync(repoImages, { recursive: true });

  let rels = [];
  const catFile = path.join(PLUGIN_DIR, 'server', 'catalog.json');
  try {
    const cat = JSON.parse(fs.readFileSync(catFile, 'utf8'));
    for (const k of ['styles', 'layouts', 'colors'])
      for (const it of (cat[k] || [])) if (it.img) rels.push(it.img);
  } catch (e) {
    console.warn('读取 catalog.json 失败（' + (e && e.message || e) + '），跳过样图下载。');
    return;
  }
  rels = [...new Set(rels)];
  if (!rels.length) { console.log('catalog.json 中没有可下载的样图清单，跳过。'); return; }

  const total = rels.length;
  const CDN = 'https://img.handraw-style.com';
  const GITEE = 'https://gitee.com/yang02010/handraw-style/raw/master';
  const GITHUB = 'https://raw.githubusercontent.com/yang0/handraw-style/main';
  const sourcesFor = rel => [CDN + '/' + rel, GITEE + '/' + rel, GITHUB + '/' + rel];
  const localOf = rel => path.join(repoImages, rel.replace(/^images\//, ''));
  const exist = p => { try { return fs.statSync(p).size > 0; } catch { return false; } };

  let done = 0, skip = 0, fail = 0;
  const CONC = 12;
  const getOne = async (rel) => {
    const dst = localOf(rel);
    if (exist(dst)) { skip++; return; }
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    for (const url of sourcesFor(rel)) {
      try {
        const ac = new AbortController();
        const t = setTimeout(() => ac.abort(), 15000);
        const r = await fetch(url, { signal: ac.signal });
        clearTimeout(t);
        if (!r.ok) continue;
        // 只接受真正的图片响应：gitee raw 会 302 跳到登录页（text/html），
        // 若不拦截会把 HTML 写成 .webp 坏文件。
        const ct = (r.headers.get('content-type') || '').toLowerCase();
        if (!ct.startsWith('image/')) continue;
        const buf = Buffer.from(await r.arrayBuffer());
        if (buf.length < 200) continue;
        fs.writeFileSync(dst, buf);
        done++; return;
      } catch { /* 换下一个源 */ }
    }
    fail++;
  };

  console.log('正在自动下载真实样图（风格 / 图型 / 主题色，共 ' + total + ' 张）…');
  for (let i = 0; i < rels.length; i += CONC)
    await Promise.all(rels.slice(i, i + CONC).map(getOne));

  console.log(`样图下载完成：共 ${total} 张，已下 ${done}，跳过(已存在) ${skip}，失败 ${fail}。`);
  if (fail)
    console.log(`  ${fail} 张失败（多为网络受限）。重跑 \`node install.mjs\` 可补齐，已存在的会自动跳过。`);
}

/* ---------- 安装后自检 ---------- */

function verify() {
  const problems = [];
  if (!bundles.includes(NAME)) problems.push('bundles 中未包含 ' + NAME);
  if (!deps[NAME]) problems.push('dependencies 中未包含 ' + NAME);
  if (linkState() !== 'ok') problems.push('目录联接不有效：' + LINK_PATH);
  const viaLink = path.join(LINK_PATH, 'host.js');
  if (!fs.existsSync(viaLink)) problems.push('通过目录联接读不到 host.js');
  if (problems.length) {
    console.error('自检未通过：');
    for (const p of problems) console.error('  - ' + p);
    return false;
  }
  console.log('自检通过：配置已注册，目录联接有效，插件文件可读。');
  return true;
}

/* ---------- 主流程 ---------- */

async function install() {
  checkAssets();
  syncServer();
  ensureLink();          // 先建联接：失败则 profile 原样，不留半装
  applyConfig();         // 再改配置
  syncLock('link:' + PLUGIN_DIR.replace(/\\/g, '/'));
  const ok = verify();
  if (removedLegacy)
    console.log('已摘除旧独立号池插件 ' + LEGACY + '（源码与账号数据保留，未删除）。');
  await fetchStyleImages();
  if (!ok) {
    console.error('安装未完全成功，请根据上面的提示处理后重跑 node install.mjs。');
    process.exit(1);
  }
  console.log('安装完成。多数情况下 DSH 会直接加载新 bundle；若侧栏未出现入口，完全退出 DSH 再启动一次。');
  console.log('首次打开工作台会自动初始化后端与账号池，通常 1 分钟内就绪。');
}

if (!uninstall) {
  install().catch(e => {
    console.error('安装过程中发生异常：' + (e && e.stack || e));
    process.exit(1);
  });
} else {
  applyConfig();
  syncLock('link:' + PLUGIN_DIR.replace(/\\/g, '/'));
  // 断开联接：只删联接本身，不会碰插件源码目录与账号数据。
  const st = linkState();
  if (st === 'ok' || st === 'dead') {
    console.log(removeLink() ? '已断开目录联接 -> ' + LINK_PATH : '警告：目录联接未能删除，可手动移除。');
  } else if (st === 'dir') {
    console.log('node_modules/' + NAME + ' 是真实目录，非联接，未做改动。');
  }
  console.log('已从 profile 摘除插件；插件源码目录与 ~/.dsh/chatgpt2api-image 数据保留。');
  console.log('完全退出 DSH 再启动，侧栏入口即消失。确认不再需要时可自行删除源码目录。');
}
