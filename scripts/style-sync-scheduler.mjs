/**
 * 风格库定时同步（插件自带，不依赖 WorkBuddy 或系统计划任务）。
 *
 * 由 host.js 在 DSH 启动时挂起：每 60 秒看一次时钟，本地时间到点且当天没跑过就
 * 拉起 scripts/sync-handraw-style.mjs。DSH 在 6 点之后才启动也算「补跑」，
 * 因为判断依据是「今天有没有成功跑过」，不是「有没有恰好撞上 6 点」。
 *
 * 运行状态落在 RUNTIME_DIR/style-sync-state.json，重启 DSH 不会同一天重复跑；
 * 失败保留重试窗口（默认 1 小时），不会每分钟刷一次网络。
 *
 * 这个模块不 import host.js，可以单独用 node 跑测试。
 */
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';

export const DEFAULT_HOUR = 6;
export const TICK_MS = 60_000;
export const TIMEOUT_MS = 5 * 60_000;
export const RETRY_MS = 60 * 60_000;

const pad = n => String(n).padStart(2, '0');

/** 本地时区的日期键。不能用 toISOString()，那是 UTC，会把早上 6 点算成前一天。 */
export function localDay(d = new Date()) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/**
 * 纯函数：这一刻该不该跑。抽出来是为了能直接测 5:59 / 6:00 / 跨天 / 退避等边界。
 * @param {{now:Date,lastRun?:string,nextRetryAt?:number,hour?:number,busy?:boolean}} o
 */
export function shouldRun({ now, lastRun = '', nextRetryAt = 0, hour = DEFAULT_HOUR, busy = false }) {
  if (busy) return false;
  if (lastRun === localDay(now)) return false;          // 今天已成功跑过
  if (nextRetryAt && now.getTime() < nextRetryAt) return false; // 失败退避中
  return now.getHours() >= hour;                       // 过点启动则补跑
}

/** 默认执行器：用宿主自己的 Node 跑同步脚本，收集最后一行 JSON 结果。 */
export function spawnSyncScript({ script, cwd, env, timeoutMs = TIMEOUT_MS }) {
  return new Promise(resolve => {
    let child;
    try {
      child = spawn(process.execPath, [script], {
        cwd, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (e) {
      return resolve({ code: -1, out: '', err: String(e && e.message || e), timedOut: false });
    }
    let out = '', err = '', settled = false;
    const finish = (code, timedOut = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ code, out, err, timedOut });
    };
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* 已退出 */ }
      finish(-1, true);
    }, timeoutMs);
    if (child.stdout) child.stdout.on('data', c => { out += c; });
    if (child.stderr) child.stderr.on('data', c => { err += c; });
    child.on('error', e => { err += String(e && e.message || e); finish(-1); });
    child.on('close', code => finish(code));
  });
}

/**
 * 启动调度器。
 * @param {object} o
 * @param {string} o.pluginDir   插件包目录（脚本所在处）
 * @param {string} o.serverDir   运行时 server 目录（可能已被复制到 ~/.dsh）
 * @param {string} o.runtimeDir  状态与日志目录
 * @param {number} o.port        后端端口
 * @param {string} [o.python]    解释器路径
 * @param {number} [o.hour]      每天几点跑，默认 6
 * @param {boolean} [o.enabled]  设为 false 关闭
 * @param {Function} [o.runOnce] 注入执行器，仅供测试
 * @param {Function} [o.log]     注入日志，仅供测试
 * @returns {{state:object, tick:Function, triggerNow:Function, stop:Function}}
 */
export function startStyleSync(o = {}) {
  const {
    pluginDir, serverDir, runtimeDir, port = 8092, python = '',
    hour = Number(process.env.DSH_IMAGE_STYLE_SYNC_HOUR || DEFAULT_HOUR),
    enabled = process.env.DSH_IMAGE_STYLE_SYNC !== '0',
    runOnce = spawnSyncScript, log = null,
  } = o;

  const stateFile = path.join(runtimeDir, 'style-sync-state.json');
  const logFile = path.join(runtimeDir, 'style-sync.log');
  const script = path.join(pluginDir, 'scripts', 'sync-handraw-style.mjs');
  const h = Number.isFinite(hour) ? Math.max(0, Math.min(23, Math.trunc(hour))) : DEFAULT_HOUR;

  const state = {
    enabled: !!enabled && !!serverDir,
    hour: h,
    running: false,
    lastRun: '',
    lastAt: '',
    lastOk: null,
    lastUpdated: false,
    lastMessage: '',
    lastError: '',
    nextRetryAt: 0,
    runs: 0,
    stateFile,
    logFile,
  };

  const writeLog = text => {
    try {
      fs.mkdirSync(runtimeDir, { recursive: true });
      fs.appendFileSync(logFile, `[${new Date().toISOString()}] ${text}\n`);
    } catch { /* 日志写不了不影响同步 */ }
  };
  const save = () => {
    try {
      fs.mkdirSync(runtimeDir, { recursive: true });
      const { stateFile: _f, logFile: _l, ...rest } = state;
      fs.writeFileSync(stateFile, JSON.stringify(rest, null, 2));
    } catch { /* 状态写不了最多是重启后多跑一次，无副作用 */ }
  };
  try {
    const prev = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
    for (const k of ['lastRun', 'lastAt', 'lastOk', 'lastUpdated', 'lastMessage',
                     'lastError', 'nextRetryAt', 'runs']) {
      if (prev[k] !== undefined) state[k] = prev[k];
    }
  } catch { /* 首次运行或状态损坏，从空开始 */ }

  const emit = (level, text) => {
    if (log) return log(level, text);
    if (level === 'error') console.error('[image-workbench] 风格库同步 ' + text);
    else if (level === 'warn') console.warn('[image-workbench] 风格库同步 ' + text);
    else console.log('[image-workbench] 风格库同步 ' + text);
  };

  const tick = async (force = false) => {
    const now = new Date();
    if (!state.enabled || state.running) return state;
    if (!force && !shouldRun({ now, lastRun: state.lastRun,
      nextRetryAt: state.nextRetryAt, hour: h, busy: state.running })) return state;

    state.running = true;
    const started = new Date();
    try {
      const env = { ...process.env, WB_PORT: String(port) };
      if (serverDir) env.WB_SERVER_DIR = serverDir;
      if (python) env.PYTHON = python;
      const r = await runOnce({
        script, cwd: pluginDir, env,
        timeoutMs: TIMEOUT_MS,
        serverDir, port, python,
      });
      const lines = String(r.out || '').trim().split('\n').filter(Boolean);
      let parsed = null;
      try { parsed = JSON.parse(lines[lines.length - 1] || ''); } catch { /* 非 JSON */ }
      const ok = r.code === 0 && parsed && parsed.ok === true;
      state.runs += 1;
      state.lastAt = started.toISOString();
      state.lastOk = !!ok;
      state.lastUpdated = !!(parsed && parsed.updated);

      if (ok) {
        state.lastRun = localDay(started);
        state.nextRetryAt = 0;
        state.lastError = '';
        state.lastMessage = parsed.updated
          ? `已更新：${parsed.counts ? parsed.counts.styles + ' 个风格' : '见日志'}`
          : '上游无变化';
        const detail = JSON.stringify(parsed).slice(0, 1200);
        writeLog(`OK updated=${!!parsed.updated} ${detail}`);
        if (parsed.updated) emit('info', state.lastMessage + '，样图与运行时目录已同步');
      } else {
        const reason = (parsed && parsed.error)
          || (r.timedOut && '同步超时（5 分钟）')
          || String(r.err || '').trim().slice(0, 200)
          || `退出码 ${r.code}，输出非 JSON：${String(r.out || '').trim().slice(0, 160)}`;
        state.lastError = reason;
        state.lastMessage = '同步失败';
        state.nextRetryAt = Date.now() + RETRY_MS;
        writeLog(`FAIL ${reason}`);
        emit('warn', '失败：' + reason + '，1 小时后重试');
      }
    } catch (e) {
      state.runs += 1;
      state.lastAt = started.toISOString();
      state.lastOk = false;
      state.lastUpdated = false;
      state.lastError = String(e && e.message || e);
      state.lastMessage = '同步异常';
      state.nextRetryAt = Date.now() + RETRY_MS;
      writeLog('ERROR ' + state.lastError);
      emit('warn', '异常：' + state.lastError + '，1 小时后重试');
    } finally {
      state.running = false;
      save();
    }
    return state;
  };

  let kick = null, timer = null;
  if (state.enabled) {
    // 启动 5 秒后先看一眼：昨晚 6 点至今没跑过就立刻补跑，不用干等第二天。
    kick = setTimeout(() => { void tick(); }, 5000);
    if (typeof kick.unref === 'function') kick.unref();
    timer = setInterval(() => { void tick(); }, TICK_MS);
    if (typeof timer.unref === 'function') timer.unref();
    emit('info', `已启用，每天 ${pad(h)}:00 自动检查上游更新`);
  } else {
    emit('info', '已停用（配置或环境变量关闭）');
  }

  return {
    state,
    tick,
    triggerNow: () => tick(true),
    stop: () => {
      if (kick) clearTimeout(kick);
      if (timer) clearInterval(timer);
      kick = timer = null;
      state.enabled = false;
    },
  };
}
