/**
 * DSH Host 插件：生图工作台（图文 / 摄影双模式），一个插件跑全套。
 *
 * 号池不再有独立插件：专用生图号池（38651）由工作台后端自己拉起、守护、初始化
 * （pool_service.py / /api/pool/service/* 路由）。号池服务端代码 + uv 随本插件
 * 放在 vendor/chatgpt2api/，工作台后端代码放在 server/（开发机上优先用旁边的
 * image-local/）。整包拷到新机器就能自己建虚拟环境、起号池，不依赖任何第三方插件。
 * 本插件只负责拉起并守护 8092，顺手把号池状态转给侧栏顶部的指示灯。
 * 所以工作台脱离 DSH、直接在浏览器打开 8092 也能独立运行。
 * 只探端口、只重启自己拉起的进程，绝不重载 client 的 iframe。
 */
import { existsSync, appendFileSync, mkdirSync, openSync, readdirSync, readFileSync,
         copyFileSync, statSync } from 'node:fs';
import { spawn } from 'node:child_process';
import http from 'node:http';
import net from 'node:net';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { fetchStyleImages } from './scripts/fetch-style-images.mjs';
import { startStyleSync } from './scripts/style-sync-scheduler.mjs';

export const inject = ['connection'];

const PLUGIN_DIR = fileURLToPath(new URL('.', import.meta.url));
const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh');
/** 插件运行时根目录：日志、后端代码副本、号池源码快照都放这里，与插件包本身解耦。 */
const RUNTIME_DIR = join(DSH_HOME, 'image-workbench');
const PLUGIN_NAME = 'dsh-plugin-image-workbench';

const sleep = ms => new Promise(r => setTimeout(r, ms));

function firstFile(list) {
  for (const p of list) if (p && existsSync(p)) return p;
  return '';
}

/** 一个目录是否构成可运行的工作台后端。 */
function isServerDir(dir) {
  return !!dir && existsSync(join(dir, 'app.py')) && existsSync(join(dir, 'static', 'index.html'));
}

/** 这些是运行时产物，不参与代码同步，升级时保留。 */
const SERVER_RUNTIME = new Set([
  '.venv', 'images', 'thumbs', 'refs', 'repo_images', 'workbench', 'data',
  '__pycache__', 'config.local.json', 'views.json',
]);
const VENDOR_SKIP = new Set(['.venv', '__pycache__', 'node_modules']);

/** 把代码目录覆盖到运行时目录：变了才复制，只碰代码，不动 venv、图片和账号数据。 */
function syncTree(src, dst, skip) {
  mkdirSync(dst, { recursive: true });
  for (const ent of readdirSync(src, { withFileTypes: true })) {
    if (skip && skip.has(ent.name)) continue;
    const s = join(src, ent.name);
    const d = join(dst, ent.name);
    if (ent.isDirectory()) { syncTree(s, d, skip); continue; }
    const st = statSync(s);
    if (existsSync(d)) {
      const dt = statSync(d);
      if (dt.size === st.size && dt.mtimeMs >= st.mtimeMs - 1000) continue;
    }
    copyFileSync(s, d);
  }
}

/**
 * 装在 node_modules 里的插件目录会被包管理器整体替换，运行时不能留在里面：
 * 一是升级会把 .venv、生成的图片、账号数据一起冲掉；二是后端进程以该目录为
 * 工作目录运行时 Windows 会锁住整个目录，pnpm 重装时报 ERR_PNPM_EPERM rename。
 * 这类安装把代码复制到 ~/.dsh/image-workbench/ 再跑，node_modules 保持只读。
 */
function isolatedServer(bundled) {
  const dst = join(RUNTIME_DIR, 'server');
  try {
    syncTree(bundled, dst, SERVER_RUNTIME);
    // 号池源码快照放到 server 的同级 vendor/，pool_service 会从那里找
    const vendSrc = join(PLUGIN_DIR, 'vendor', 'chatgpt2api');
    if (existsSync(join(vendSrc, 'source', 'main.py'))) {
      syncTree(vendSrc, join(RUNTIME_DIR, 'vendor', 'chatgpt2api'), VENDOR_SKIP);
    }
    // 1.1.3 及更早版本把运行时直接写在插件目录里，搬一次，别让用户丢图
    for (const name of ['images', 'data', 'workbench', 'refs', 'config.local.json']) {
      const s = join(bundled, name);
      const d = join(dst, name);
      if (!existsSync(s) || existsSync(d)) continue;
      if (statSync(s).isDirectory()) syncTree(s, d);
      else copyFileSync(s, d);
    }
  } catch (e) {
    console.error('[image-workbench] 同步后端代码失败，改用插件目录：' + e.message);
    return bundled;
  }
  return isServerDir(dst) ? dst : bundled;
}

/** 工作台目录：环境变量 > patch 配置 > 插件同级 image-local > 插件自带 server/。 */
export function locateServer(config = {}) {
  const candidates = [
    process.env.DSH_IMAGE_SERVER_DIR,
    config.serverDir,
    resolve(PLUGIN_DIR, '..', 'image-local'),
  ].filter(Boolean);
  for (const dir of candidates) if (isServerDir(dir)) return dir;

  const bundled = resolve(PLUGIN_DIR, 'server');
  if (!isServerDir(bundled)) return '';
  // 插件被装进 node_modules 时，目录归包管理器管，运行时要搬出去
  if (bundled.split(/[\\/]/).includes('node_modules')) return isolatedServer(bundled);
  return bundled;
}

/** 工作台 Python：自带 venv 优先，其次系统 Python。 */
export function locatePython(serverDir, config = {}) {
  return firstFile([
    process.env.DSH_IMAGE_PYTHON,
    config.python,
    serverDir && join(serverDir, '.venv', 'Scripts', 'python.exe'),
    serverDir && join(serverDir, '.venv', 'bin', 'python'),
    join(homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe'),
  ].filter(Boolean)) || 'python';
}

function probe(port, timeout = 700) {
  return new Promise(done => {
    const socket = net.connect({ host: '127.0.0.1', port });
    let settled = false;
    const finish = value => { if (!settled) { settled = true; socket.destroy(); done(value); } };
    socket.setTimeout(timeout);
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
    socket.once('timeout', () => finish(false));
  });
}

/** node:http 直连回环，绕开宿主可能挂上的全局代理 dispatcher。 */
function localGetJson(port, pathname, timeout = 2500) {
  return new Promise((done, fail) => {
    const req = http.get({ host: '127.0.0.1', port, path: pathname,
                           headers: { Accept: 'application/json' }, timeout }, res => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', c => { body += c; if (body.length > 500_000) req.destroy(new Error('响应过大')); });
      res.on('end', () => {
        try { done({ status: res.statusCode, data: JSON.parse(body) }); }
        catch { done({ status: res.statusCode, data: {} }); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('连接超时')));
    req.on('error', fail);
  });
}

/**
 * 用系统默认浏览器打开 URL。
 *
 * DSH 宿主不保证是 Electron（也可能跑在纯 node 里），所以不能只靠 shell.openExternal。
 * 顺序：electron.shell → 平台原生命令（win: cmd /c start、mac: open、linux: xdg-open）。
 * 一律返回 { ok, via, error }，让 client 能把成功/失败如实告诉用户 —— 以前只返回 false
 * 且 client 不看返回值，点了像没反应。
 */
async function openExternal(url) {
  let electronError = '';
  if (process.versions.electron) {
    try {
      const { shell } = await import('electron');
      if (typeof shell?.openExternal === 'function') {
        await shell.openExternal(url);
        return { ok: true, via: 'electron.shell' };
      }
      electronError = 'electron.shell 不可用';
    } catch (error) {
      electronError = 'electron 模块加载失败：' + error.message;
    }
  }

  const [command, args] = process.platform === 'win32'
    // 第二个空参数是 start 的窗口标题占位，少了它 URL 会被当成标题
    ? ['cmd', ['/c', 'start', '', url]]
    : process.platform === 'darwin'
      ? ['open', [url]]
      : ['xdg-open', [url]];

  try {
    const child = spawn(command, args, { detached: true, stdio: 'ignore', windowsHide: true });
    child.unref();
    // detached 后进程已脱离，这里只收 ENOENT 之类的启动错误，避免 unhandled
    child.on('error', error => {
      console.warn('[image-workbench] 打开浏览器失败：' + error.message);
    });
    return { ok: true, via: command, note: electronError };
  } catch (error) {
    return { ok: false, error: [electronError, error.message].filter(Boolean).join(' / ') };
  }
}

export function apply(ctx, config = {}) {
  const port = Number(config.port || process.env.DSH_IMAGE_PORT || 8092);
  const serverDir = locateServer(config);
  const state = {
    port,
    url: 'http://127.0.0.1:' + port + '/',
    serverDir,
    python: locatePython(serverDir, config),
    autoStart: config.autoStart !== false,
    serverChild: null,
    lastServerStart: '',
  };

  const poolPort = Number(config.poolPort || process.env.DSH_IMAGE_POOL_PORT || 38651);
  console.log('[image-workbench] 工作台=' + (serverDir || '未找到') + ':' + port
    + '，内置号池=' + poolPort + '（由工作台后端自己拉起和守护）');

  /**
   * 样图补齐（后台，不阻塞启动）。
   *
   * npm 包里不带 574 张样例图，工作台后端在文件缺失时会退化成渐变 SVG 占位，
   * 「风格库 / 配色库」就整片色块，看着像装坏了。所以插件自己启动后补一次：
   * 已存在的跳过，升级也不会重下（repo_images 在运行时目录，同步时被排除）。
   */
  const samples = { state: 'idle', total: 0, done: 0, skip: 0, fail: 0,
                    message: '', at: '', log: join(RUNTIME_DIR, 'samples.log') };
  let samplesRunning = null;
  function ensureSamples() {
    if (samplesRunning) return samplesRunning;
    if (!state.serverDir) { samples.state = 'skipped'; return Promise.resolve(samples); }
    if (process.env.DSH_IMAGE_SKIP_SAMPLES === '1') {
      samples.state = 'skipped'; samples.message = '已按 DSH_IMAGE_SKIP_SAMPLES 跳过'; return Promise.resolve(samples);
    }
    const log = text => {
      console.log('[image-workbench] ' + text);
      try {
        mkdirSync(RUNTIME_DIR, { recursive: true });
        appendFileSync(samples.log, `[${new Date().toISOString()}] ${text}\n`);
      } catch { /* 日志而已 */ }
    };
    samples.state = 'running'; samples.at = new Date().toISOString();
    samplesRunning = fetchStyleImages({ serverDir: state.serverDir, log })
      .then(r => {
        samples.total = r.total; samples.done = r.done;
        samples.skip = r.skip; samples.fail = r.fail;
        samples.message = r.error || '';
        samples.state = r.ok ? 'done' : (r.error ? 'error' : 'partial');
        samples.at = new Date().toISOString();
        return samples;
      })
      .catch(e => {
        samples.state = 'error'; samples.message = String(e && e.message || e);
        samples.at = new Date().toISOString();
        return samples;
      })
      .finally(() => { samplesRunning = null; });
    return samplesRunning;
  }
  // 先让后端环境初始化抢到网络，再补样图（3 秒后开跑，纯后台）
  {
    const t = setTimeout(() => { void ensureSamples(); }, 3000);
    if (typeof t.unref === 'function') t.unref();
  }

  const watchdog = {
    enabled: config.watchdog !== false && state.autoStart,
    intervalMs: Math.max(3000, Number(config.watchdogIntervalMs || 15000)),
    timer: null, busy: false, failures: 0, nextAllowedAt: 0,
    restarts: 0, lastRestart: '',
  };

  /**
   * 上游 handraw-style 风格库同步：插件自带定时器，不依赖 WorkBuddy。
   * 默认每天本地 6:00 跑一次；6 点之后才开 DSH 会补跑，失败一小时后再试。
   * 可在 profile 的 config 里用 styleSync:false 关掉、styleSyncHour 改时间。
   */
  const styleSync = startStyleSync({
    pluginDir: PLUGIN_DIR,
    serverDir: state.serverDir,
    runtimeDir: RUNTIME_DIR,
    port: state.port,
    python: state.python,
    hour: Number(config.styleSyncHour ?? process.env.DSH_IMAGE_STYLE_SYNC_HOUR ?? 6),
    enabled: config.styleSync !== false && process.env.DSH_IMAGE_STYLE_SYNC !== '0',
  });

  // 只回收自己 spawn 的子进程；守护定时器属于插件生命周期，不被「手动启动」清掉。
  const stopChild = () => {
    const child = state.serverChild;
    if (child && !child.killed) { try { child.kill(); } catch { /* 已退出 */ } }
    state.serverChild = null;
  };
  const cleanup = () => {
    if (watchdog.timer) { clearInterval(watchdog.timer); watchdog.timer = null; }
    styleSync.stop();
    stopChild();
  };
  if (typeof ctx.effect === 'function') ctx.effect(() => cleanup, PLUGIN_NAME + ': backend lifecycle');

  function logFd(name) {
    mkdirSync(RUNTIME_DIR, { recursive: true });
    return openSync(join(RUNTIME_DIR, name), 'a');
  }

  /** 读日志末尾若干字符，排障时直接看，不用去翻文件。读不到就返回空串。 */
  function tailOf(file, limit = 800) {
    try {
      const text = readFileSync(file, 'utf8');
      const rows = text.trimEnd().split('\n');
      // 同一行反复出现（后端每次退出都写一条）时只留一条，否则日志等于噪音
      const out = [];
      for (const r of rows) if (out[out.length - 1] !== r) out.push(r);
      return out.slice(-12).join('\n').slice(-limit);
    } catch { return ''; }
  }

  /** 整包拷到新机器时，server/ 里没有 .venv，用插件自带的 uv 现建一个。 */
  function bundledUv() {
    return firstFile([
      process.env.DSH_IMAGE_UV,
      join(PLUGIN_DIR, 'vendor', 'chatgpt2api', 'runtime', 'uv.exe'),
      join(PLUGIN_DIR, 'vendor', 'chatgpt2api', 'runtime', 'uv'),
    ]);
  }
  function venvPython(dir) {
    return firstFile([
      join(dir, '.venv', 'Scripts', 'python.exe'),
      join(dir, '.venv', 'bin', 'python'),
    ]);
  }

  /**
   * 这个解释器能不能真跑起后端。
   *
   * 只看 python.exe 存在是不够的：上一轮装依赖失败时 venv 目录已经建好，
   * 里面却没有 uvicorn，于是每次启动都报 "No module named uvicorn"，
   * 而代码以为环境已就绪，永远不会去补装。
   */
  async function venvUsable(py, cwd) {
    if (!py) return false;
    const r = await runCmd(py, ['-c', 'import fastapi, uvicorn'], cwd, 30000);
    return r.ok;
  }

  /**
   * 依赖源候选，按「国内镜像直连 → 官方 PyPI 走代理 → 官方直连」排列。
   *
   * 关键点：镜像站不能走代理。走代理访问清华镜像会拿到 403，
   * uv 看到的就是 "fastapi was not found in the package registry"，
   * 报错完全指不到真正原因（实测第三次装机就是这么卡住的）。
   */
  function indexCandidates(proxy) {
    const custom = (process.env.DSH_IMAGE_PYPI_INDEX || '').trim();
    const list = [];
    if (custom) list.push({ url: custom, label: '自定义源', proxy: false });
    list.push({ url: 'https://pypi.tuna.tsinghua.edu.cn/simple', label: '清华镜像', proxy: false });
    list.push({ url: 'https://mirrors.aliyun.com/pypi/simple', label: '阿里云镜像', proxy: false });
    if (proxy) list.push({ url: 'https://pypi.org/simple', label: '官方 PyPI', proxy: true });
    list.push({ url: 'https://pypi.org/simple', label: '官方 PyPI', proxy: false });
    return list;
  }

  /** 按候选源准备 env：镜像直连（清掉代理变量），官方源才挂代理。 */
  function envForIndex(base, cand, proxy) {
    const env = { ...base };
    env.UV_DEFAULT_INDEX_URL = cand.url;   // 覆盖宿主可能带进来同名变量
    const keys = ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy'];
    if (cand.proxy && proxy) {
      env.HTTPS_PROXY = proxy; env.HTTP_PROXY = proxy;
    } else {
      for (const k of keys) delete env[k];
    }
    env.NO_PROXY = env.no_proxy =
      'pypi.tuna.tsinghua.edu.cn,mirrors.aliyun.com,pypi.org,files.pythonhosted.org,127.0.0.1,localhost';
    return env;
  }
  function runCmd(command, args, cwd, timeoutMs = 240000, env = null) {
    return new Promise(done => {
      const child = spawn(command, args, { cwd, windowsHide: true,
                                           stdio: ['ignore', 'pipe', 'pipe'],
                                           ...(env ? { env } : {}) });
      let tail = '';
      const collect = c => { tail = (tail + c.toString()).slice(-4000); };
      child.stdout?.on('data', collect);
      child.stderr?.on('data', collect);
      const timer = setTimeout(() => child.kill(), timeoutMs);
      child.once('error', e => { clearTimeout(timer); done({ ok: false, tail: e.message }); });
      child.once('exit', code => { clearTimeout(timer);
        done({ ok: code === 0, tail: code === 0 ? '' : tail.slice(-600) }); });
    });
  }

  /** 本机常见代理端口：uv 要出网下 Python 运行时，国内不开代理基本下不动。 */
  const PROXY_SPECS = [['http', 7890], ['socks5', 1080], ['http', 7891],
                       ['http', 8118], ['socks5', 1087], ['http', 10809]];
  async function egressProxy() {
    for (const k of ['HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy',
                     'HTTP_PROXY', 'http_proxy']) {
      const v = (process.env[k] || '').trim();
      if (v) return v;
    }
    for (const [scheme, port] of PROXY_SPECS) {
      if (await probe(port, 250)) return scheme + '://127.0.0.1:' + port;
    }
    return '';
  }

  /** 判断某个解释器能不能用（版本 >= 3.10）。py 启动器要带 -3，返回的版本号同此。 */
  async function pythonVersion(exe, prefix = []) {
    const r = await runCmd(exe, [...prefix, '-c',
      'import sys;print(sys.version_info[0]*100+sys.version_info[1])'], homedir(), 12000);
    if (!r.ok) return 0;
    const n = Number(String(r.tail || '').trim().split('\n').pop());
    return Number.isFinite(n) ? n : 0;
  }

  /**
   * 找一台机器上现成的 Python 3.10+。
   *
   * 全新电脑如果没有任何 Python，uv 会去 GitHub 下载 python-build-standalone，
   * 国内网络经常下不动，于是整个后端起不来。所以先找现成的，找不到才交给 uv。
   */
  async function findSystemPython() {
    const seeds = [];
    const push = (exe, prefix = []) => { if (exe) seeds.push([exe, prefix]); };
    push(process.env.DSH_IMAGE_PYTHON);
    push(config.python);
    if (process.platform === 'win32') {
      push(join(process.env.ProgramFiles || 'C:\\Program Files', 'Python313', 'python.exe'));
      push(join(process.env.ProgramFiles || 'C:\\Program Files', 'Python312', 'python.exe'));
      push(join(process.env.ProgramFiles || 'C:\\Program Files', 'Python311', 'python.exe'));
      const roots = [
        join(process.env.LOCALAPPDATA || '', 'Programs', 'Python'),
        join(homedir(), '.workbuddy', 'binaries', 'python', 'versions'),  // 宿主自带运行时
        'C:\\Python313', 'C:\\Python312', 'C:\\Python311', 'C:\\Python310',
      ];
      for (const root of roots) {
        let names = [];
        try { names = readdirSync(root).sort().reverse(); } catch { /* 目录不存在 */ }
        for (const n of names) push(join(root, n, 'python.exe'));
        push(join(root, 'python.exe'));
      }
      // py 启动器放到最后：它可能指向 3.12，而上面已排过 3.13 的绝对路径
      push('py', ['-3']);
    }
    push('python3');
    push('python');
    push(join(homedir(), 'AppData', 'Local', 'Programs', 'Python', 'Python312', 'python.exe'));

    const seen = new Set();
    for (const [exe, prefix] of seeds) {
      const key = exe + ' ' + prefix.join(' ');
      if (seen.has(key)) continue;
      seen.add(key);
      const v = await pythonVersion(exe, prefix);
      if (v >= 310) return { exe, prefix, version: v };
    }
    return null;
  }

  const venvState = { tried: false, ok: false, message: '', error: '', attempts: 0, nextRetryAt: 0 };
  let venvTask = null;   // 同一时刻只允许一次初始化，宿主自启 / 守护 / 用户点按钮会撞车

  /** 初始化后端虚拟环境。失败后 60 秒可再试，用户点「立即启动并重试」则立刻重试。 */
  async function ensureServerVenv(force = false) {
    if (!state.serverDir || venvState.ok) return;
    if (!force && venvState.tried && Date.now() < venvState.nextRetryAt) return;
    if (venvTask) return venvTask;
    venvTask = runVenvInit().finally(() => { venvTask = null; });
    return venvTask;
  }

  async function runVenvInit() {
    venvState.tried = true;
    venvState.attempts += 1;

    const existing = venvPython(state.serverDir);
    if (existing && await venvUsable(existing, state.serverDir)) {
      venvState.ok = true; venvState.message = ''; venvState.error = ''; state.python = existing;
      return;
    }
    if (process.env.DSH_IMAGE_SKIP_VENV === '1') {
      venvState.message = '已按 DSH_IMAGE_SKIP_VENV 跳过环境初始化';
      return;
    }
    const uv = bundledUv();
    const req = join(state.serverDir, 'requirements.txt');
    const fail = msg => {
      venvState.message = msg;
      venvState.nextRetryAt = Date.now() + 60000;
    };
    if (!uv || !existsSync(req)) { fail('插件里缺 uv 或 server/requirements.txt，无法自动初始化环境'); return; }

    const venvLog = join(RUNTIME_DIR, 'venv.log');
    mkdirSync(RUNTIME_DIR, { recursive: true });
    const logLine = s => { try { appendFileSync(venvLog, s + '\n'); } catch { /* 日志而已 */ } };

    const sys = await findSystemPython();
    const proxy = await egressProxy();
    const uvEnv = { ...process.env };
    uvEnv.UV_HTTP_TIMEOUT = uvEnv.UV_HTTP_TIMEOUT || '120';
    if (proxy) { uvEnv.HTTPS_PROXY = proxy; uvEnv.HTTP_PROXY = proxy; }
    if (process.env.DSH_IMAGE_PYTHON_MIRROR) {
      uvEnv.UV_PYTHON_INSTALL_MIRROR = process.env.DSH_IMAGE_PYTHON_MIRROR;
    }
    logLine(`[${new Date().toISOString()}] 初始化后端环境（第 ${venvState.attempts} 次）`
      + `，系统 Python=${sys ? sys.exe + ' (' + sys.version + ')' : '未找到'}`
      + `，代理=${proxy || '无'}`);

    // 已有 python 就直接补装依赖：最常见的失败是 venv 建好了、包没装上，
    // 这时重建多余，还可能被 uv 的「目录已存在」拦下来。
    let py = existing;
    if (!py) {
      const venvDir = join(state.serverDir, '.venv');
      const vArgs = ['venv', venvDir];
      if (existsSync(venvDir)) vArgs.push('--clear');   // 目录在却没有 python：清掉重建
      if (sys) vArgs.push('--python', sys.exe);
      const v = await runCmd(uv, vArgs, state.serverDir, 180000, uvEnv);
      if (!v.ok) {
        logLine('uv venv 失败：' + v.tail);
        venvState.error = (v.tail || '').slice(-400);
        fail('创建 Python 环境失败' + (sys ? '' : '：本机没找到 Python 3.10+，自动下载也没成功')
             + '（详见 venv.log）');
        return;
      }
      py = venvPython(state.serverDir);
    }
    if (!py) {
      venvState.error = 'venv 目录里没有 python 可执行文件';
      fail('环境目录建好了但里面没有 python（详见 venv.log）');
      return;
    }
    state.python = py;

    // 依赖安装：国内镜像直连 → 官方源（有代理就经代理）→ 官方直连。
    // 逐个试；装完必须真能 import 才算成功，uv 返回 0 却没装上的情况见过。
    let lastTail = '', used = '';
    for (const cand of indexCandidates(proxy)) {
      logLine('安装后端依赖：' + cand.label + '（' + (cand.proxy ? '经代理 ' + proxy : '直连') + '）');
      const r = await runCmd(uv, ['pip', 'install', '--python', py, '-r', req,
                                  '--index-url', cand.url],
                             state.serverDir, 300000, envForIndex(uvEnv, cand, proxy));
      if (r.ok && await venvUsable(py, state.serverDir)) { used = cand.label; break; }
      lastTail = r.ok ? '依赖装完仍无法 import fastapi/uvicorn' : (r.tail || '').slice(-400);
      logLine('  ' + cand.label + ' 失败：'
        + (r.tail || '装完仍无法 import uvicorn').replace(/\s+/g, ' ').slice(-200));
      if (/403|forbidden/i.test(r.tail || '')) {
        logLine('  ↑ 镜像站被代理拦了（403），下一个源改用直连');
      }
    }
    if (used) {
      logLine('工作台后端依赖已装好（' + used + '）');
      venvState.ok = true; venvState.message = ''; venvState.error = ''; state.python = py;
      return;
    }
    venvState.error = lastTail;
    fail(/403|forbidden/i.test(lastTail)
      ? '镜像站被代理拦住（403）导致依赖装不上。可先关掉代理重试，'
        + '或用 DSH_IMAGE_PYPI_INDEX 指定一个能直连的源（详见 venv.log）'
      : '安装后端依赖失败（网络不通或依赖源不可用，详见 venv.log）');
  }

  /** 拉起生图工作台后端（8092）。 */
  async function ensureServer(force = false) {
    if (await probe(state.port)) return { running: true, started: false };
    if (!state.autoStart) return { running: false, started: false, reason: 'autoStart 已关闭' };
    if (!state.serverDir) return { running: false, started: false, reason: '未找到 image-local 工作台目录' };
    if (state.serverChild && !state.serverChild.killed) return { running: false, started: true, reason: '工作台正在启动中' };
    await ensureServerVenv(force);
    if (!venvState.ok) {
      // 环境没建好时只能用现成解释器兜底，但它必须真有 fastapi/uvicorn，
      // 否则 spawn 出来只会得到 "No module named uvicorn"，用户看不出根因。
      const ver = await pythonVersion(state.python);
      const pkg = ver ? await venvUsable(state.python, state.serverDir) : false;
      if (!pkg) {
        return { running: false, started: false,
                 reason: (venvState.message || '本机没有可用的 Python 3.10+') + '，后端无法启动' };
      }
      venvState.ok = true;
    }
    if (force) stopChild();
    try {
      const fd = logFd('backend.log');
      const child = spawn(state.python, [
        '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1',
        '--port', String(state.port), '--log-level', 'warning',
      ], {
        cwd: state.serverDir, windowsHide: true, stdio: ['ignore', fd, fd],
        env: { ...process.env, DSH_IMAGE_POOL_PORT: String(poolPort) },
      });
      state.serverChild = child;
      const forget = message => {
        if (state.serverChild !== child) return;
        state.lastServerStart = message; state.serverChild = null;
      };
      child.once('exit', code => forget('工作台进程退出 code=' + code));
      child.once('error', error => forget('工作台启动失败：' + error.message));
    } catch (error) {
      return { running: false, started: false, reason: '工作台启动失败：' + error.message };
    }
    for (let i = 0; i < 24; i++) {
      await sleep(500);
      if (await probe(state.port)) return { running: true, started: true };
      if (!state.serverChild) break;
    }
    return { running: false, started: true, reason: state.lastServerStart || '工作台启动超时，请看 ' + join(RUNTIME_DIR, 'backend.log') };
  }

  if (state.autoStart) void ensureServer().then(r => {
    if (!r.running) console.warn('[image-workbench] 工作台未就绪：' + (r.reason || ''));
  });

  const watchdogTick = async () => {
    if (!watchdog.enabled || watchdog.busy || Date.now() < watchdog.nextAllowedAt) return;
    watchdog.busy = true;
    try {
      const r = await ensureServer();
      if (r.running) {
        watchdog.failures = 0; watchdog.nextAllowedAt = 0;
        if (r.started) { watchdog.restarts += 1; watchdog.lastRestart = new Date().toISOString();
          console.warn('[image-workbench] 守护已自动拉起掉线的工作台'); }
      } else {
        watchdog.failures += 1;
        const backoff = Math.min(watchdog.intervalMs * 2 ** Math.min(watchdog.failures - 1, 5), 300000);
        watchdog.nextAllowedAt = Date.now() + backoff;
        console.warn('[image-workbench] 守护拉起失败（第 ' + watchdog.failures + ' 次），'
          + Math.round(backoff / 1000) + ' 秒后重试：' + (r.reason || '未知原因'));
      }
    } finally { watchdog.busy = false; }
  };
  if (watchdog.enabled) {
    watchdog.timer = setInterval(() => { void watchdogTick(); }, watchdog.intervalMs);
    if (typeof watchdog.timer.unref === 'function') watchdog.timer.unref();
  }

  // 最近一次主动动作的结果：client 据此给出「打开了 / 失败了」的明确反馈
  const lastAction = { action: '', ok: null, via: '', error: '', at: '' };
  const note = (action, result) => {
    lastAction.action = action;
    lastAction.ok = result.ok;
    lastAction.via = result.via || '';
    lastAction.error = result.error || '';
    lastAction.at = new Date().toISOString();
  };

  const route = {
    path: '/api/image-workbench.status',
    methods: ['GET', 'POST'],
    requestBody: 'buffered',
    async fetch(request) {
      let action = 'status';
      if (request.method === 'POST') {
        try { action = String((await request.json()).action || 'status'); } catch { /* 空 body */ }
      }
      if (action === 'start') await ensureServer(true);
      else if (action === 'samples') void ensureSamples();
      else if (action === 'style-sync') void styleSync.triggerNow();
      else if (action === 'open') {
        const result = await openExternal(state.url);
        note('open', result);
        if (result.ok) console.log('[image-workbench] 已通过 ' + result.via + ' 打开 ' + state.url);
        else console.warn('[image-workbench] 打开浏览器失败：' + result.error);
      }
      const running = await probe(state.port);
      // 号池状态由工作台后端上报（它才是号池的 owner），宿主只读不算数
      let pool = null;
      if (running) {
        try {
          const r = await localGetJson(state.port, '/api/pool/service');
          if (r.status === 200) pool = r.data;
        } catch { /* 后端正在启动，下次刷新就有了 */ }
      }
      return Response.json({
        ok: true,
        name: '生图工作台（图文 / 摄影）',
        url: state.url,
        port: state.port,
        running,
        pool,
        poolPort,
        serverDir: state.serverDir,
        python: state.python,
        ownedProcess: Boolean(state.serverChild),
        venv: {
          ready: venvState.ok,
          tried: venvState.tried,
          attempts: venvState.attempts,
          message: venvState.message,
          error: venvState.error,
          log: join(RUNTIME_DIR, 'venv.log'),
          retryInMs: Math.max(0, venvState.nextRetryAt - Date.now()),
        },
        lastError: state.lastServerStart,
        // 样例图（风格 / 图型 / 配色）下载进度：没下完时画廊显示的是占位色块
        samples,
        // 上游风格库定时同步（每天 6:00，插件自带，不依赖 WorkBuddy）
        styleSync: {
          ...styleSync.state,
          retryInMs: Math.max(0, styleSync.state.nextRetryAt - Date.now()),
        },
        // 进程起来又退出时，真正的原因在 backend.log 末尾（缺依赖、端口被占…）
        backendTail: tailOf(join(RUNTIME_DIR, 'backend.log'), 900),
        lastAction,
        engine: '内置专用生图号池 /v1/images/generations（运行时随插件自带，全新机器自动初始化；工作台自拉起、自守护）',
        watchdog: {
          enabled: watchdog.enabled,
          intervalMs: watchdog.intervalMs,
          restarts: watchdog.restarts,
          consecutiveFailures: watchdog.failures,
          retryInMs: Math.max(0, watchdog.nextAllowedAt - Date.now()),
          lastRestart: watchdog.lastRestart,
        },
        log: join(RUNTIME_DIR, 'backend.log'),
      }, { headers: { 'Cache-Control': 'no-store' } });
    },
  };
  if (typeof ctx.effect === 'function') ctx.effect(() => ctx.connection.fetch.register(route));
  else ctx.connection.fetch.register(route);
}
