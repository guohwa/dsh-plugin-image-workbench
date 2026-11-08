/**
 * 把插件挂进当前 DSH desktop profile。
 *
 * 用法：
 *   node install.mjs              安装（幂等）
 *   node install.mjs --uninstall  从 profile 配置里摘掉插件
 *
 * 改动范围：
 *   1. profile/package.json 的 dependencies 与 dsh.profile.bundles（改前备份）；
 *   2. pnpm-lock.yaml 里直接补/删 importer 记录，不联网、不动 node_modules；
 *   3. profile/node_modules 下建目录联接到插件源码目录。
 * 不会执行完整 pnpm install（避免重拉已装的两个 git 插件），也不会重启 DSH。
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

if (!fs.existsSync(PKG_FILE)) {
  console.error('找不到 DSH profile：' + PKG_FILE);
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

let changed = false;
let removedLegacy = false;
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

if (changed) {
  const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
  const backup = PKG_FILE + '.bak_' + stamp;
  fs.writeFileSync(backup, raw, 'utf8');
  fs.writeFileSync(PKG_FILE, JSON.stringify(pkg, null, 2) + '\n', 'utf8');
  console.log('已备份原配置 -> ' + backup);
  console.log('已更新 -> ' + PKG_FILE);
} else {
  console.log('profile 配置已经是目标状态，无需修改。');
}
if (removedLegacy) {
  console.log('已摘除旧独立号池插件 ' + LEGACY + '（源码与账号数据保留，未删除）。');
}

/**
 * 直接补/删 lock 的 importer 记录，不调用 pnpm install。
 * link 依赖在 lock v9 里的标准写法（已实测，跨盘符时 specifier 与 version 都是绝对路径）。
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
syncLock('link:' + PLUGIN_DIR.replace(/\\/g, '/'));

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

if (!uninstall) {
  syncServer();
  if (fs.existsSync(LINK_PATH)) {
    console.log('插件目录已存在 -> ' + LINK_PATH);
  } else {
    fs.symlinkSync(PLUGIN_DIR, LINK_PATH, 'junction');
    console.log('已建立目录联接 -> ' + LINK_PATH);
  }
  const ok = fs.existsSync(path.join(LINK_PATH, 'host.js')) && fs.existsSync(path.join(LINK_PATH, 'client.js'));
  console.log(ok ? '插件文件校验通过。' : '警告：联接内未找到 host.js / client.js。');
  console.log('工作台后端与内置号池由 host.js / pool_service.py 在 DSH 启动时拉起。');
  console.log('请重启 DeepSeek Harness 使 bundle 生效（旧号池插件届时会从侧栏消失）。');
} else {
  console.log('已从 profile 配置摘除插件；node_modules 里的目录联接保留，不影响 DSH。');
  console.log('请重启 DeepSeek Harness 使变更生效。');
}
