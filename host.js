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
import { existsSync, appendFileSync, mkdirSync, openSync } from 'node:fs';
import { spawn } from 'node:child_process';
import http from 'node:http';
import net from 'node:net';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const inject = ['connection'];

const PLUGIN_DIR = fileURLToPath(new URL('.', import.meta.url));
const DSH_HOME = process.env.DSH_HOME || join(homedir(), '.dsh');
const LOG_DIR = join(DSH_HOME, 'image-workbench');
const PLUGIN_NAME = 'dsh-plugin-image-workbench';

const sleep = ms => new Promise(r => setTimeout(r, ms));

function firstFile(list) {
  for (const p of list) if (p && existsSync(p)) return p;
  return '';
}

/** 工作台目录：环境变量 > patch 配置 > 插件同级 image-local > 插件自带 server/。 */
export function locateServer(config = {}) {
  const candidates = [
    process.env.DSH_IMAGE_SERVER_DIR,
    config.serverDir,
    resolve(PLUGIN_DIR, '..', 'image-local'),
    resolve(PLUGIN_DIR, 'server'),
  ].filter(Boolean);
  for (const dir of candidates) {
    if (existsSync(join(dir, 'app.py')) && existsSync(join(dir, 'static', 'index.html'))) return dir;
  }
  return '';
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

  const watchdog = {
    enabled: config.watchdog !== false && state.autoStart,
    intervalMs: Math.max(3000, Number(config.watchdogIntervalMs || 15000)),
    timer: null, busy: false, failures: 0, nextAllowedAt: 0,
    restarts: 0, lastRestart: '',
  };

  // 只回收自己 spawn 的子进程；守护定时器属于插件生命周期，不被「手动启动」清掉。
  const stopChild = () => {
    const child = state.serverChild;
    if (child && !child.killed) { try { child.kill(); } catch { /* 已退出 */ } }
    state.serverChild = null;
  };
  const cleanup = () => {
    if (watchdog.timer) { clearInterval(watchdog.timer); watchdog.timer = null; }
    stopChild();
  };
  if (typeof ctx.effect === 'function') ctx.effect(() => cleanup, PLUGIN_NAME + ': backend lifecycle');

  function logFd(name) {
    mkdirSync(LOG_DIR, { recursive: true });
    return openSync(join(LOG_DIR, name), 'a');
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
  function runCmd(command, args, cwd, timeoutMs = 240000) {
    return new Promise(done => {
      const child = spawn(command, args, { cwd, windowsHide: true,
                                           stdio: ['ignore', 'pipe', 'pipe'] });
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
  const venvState = { tried: false, ok: false, message: '' };
  async function ensureServerVenv() {
    if (venvState.tried || !state.serverDir) return;
    venvState.tried = true;
    if (venvPython(state.serverDir)) { venvState.ok = true; return; }
    if (process.env.DSH_IMAGE_SKIP_VENV === '1') return;
    const uv = bundledUv();
    const req = join(state.serverDir, 'requirements.txt');
    if (!uv || !existsSync(req)) {
      venvState.message = '插件里缺 uv 或 server/requirements.txt，退回系统 Python';
      return;
    }
    const venvLog = join(LOG_DIR, 'venv.log');
    mkdirSync(LOG_DIR, { recursive: true });
    const logLine = s => { try { appendFileSync(venvLog, s + '\n'); } catch { /* 日志而已 */ } };
    logLine('用插件自带 uv 初始化工作台后端环境：' + state.serverDir);
    const v = await runCmd(uv, ['venv', join(state.serverDir, '.venv')], state.serverDir, 120000);
    if (!v.ok) { logLine('uv venv 失败：' + v.tail); venvState.message = '建虚拟环境失败，退回系统 Python'; return; }
    const py = venvPython(state.serverDir);
    if (!py) { venvState.message = 'venv 里没找到 python，退回系统 Python'; return; }
    const i = await runCmd(uv, ['pip', 'install', '--python', py, '-r', req,
                                '--index-url', 'https://pypi.tuna.tsinghua.edu.cn/simple'],
                           state.serverDir);
    logLine(i.ok ? '工作台后端依赖已装好' : ('依赖安装失败：' + i.tail));
    if (i.ok) { state.python = py; venvState.ok = true; }
    else venvState.message = '依赖安装失败，退回系统 Python（详见 venv.log）';
  }

  /** 拉起生图工作台后端（8092）。 */
  async function ensureServer(force = false) {
    if (await probe(state.port)) return { running: true, started: false };
    if (!state.autoStart) return { running: false, started: false, reason: 'autoStart 已关闭' };
    if (!state.serverDir) return { running: false, started: false, reason: '未找到 image-local 工作台目录' };
    if (state.serverChild && !state.serverChild.killed) return { running: false, started: true, reason: '工作台正在启动中' };
    await ensureServerVenv();
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
    return { running: false, started: true, reason: state.lastServerStart || '工作台启动超时，请看 ' + join(LOG_DIR, 'backend.log') };
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
        venv: { ready: venvState.ok, tried: venvState.tried, message: venvState.message },
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
        log: join(LOG_DIR, 'backend.log'),
      }, { headers: { 'Cache-Control': 'no-store' } });
    },
  };
  if (typeof ctx.effect === 'function') ctx.effect(() => ctx.connection.fetch.register(route));
  else ctx.connection.fetch.register(route);
}
