/**
 * DSH Client 插件：把 127.0.0.1:8092 的生图工作台嵌进侧栏面板。
 * 页面本体是 image-local/static/index.html，浏览器直连与插件内看到的是同一个工作台。
 * 号池是工作台自带的进程，所以侧栏只注册这一个面板，不再有独立号池入口。
 */
window.__ModuleLoader__.load({
  id: 'dsh-plugin-image-workbench',
  factory(require) {
    const { createElement: h, useState, useEffect, useCallback } = require('react');
    const PANEL = 'image-workbench';
    const STATUS_API = '/api/image-workbench.status';
    const CSS = `
.iw-root{display:flex;flex-direction:column;height:100%;min-height:0;width:100%;background:#e7eaee;color:#1c1e21;font:14px/1.5 "PingFang SC","Microsoft YaHei",system-ui,sans-serif;box-sizing:border-box}
.iw-root *{box-sizing:border-box}
.iw-bar{display:flex;align-items:center;justify-content:flex-start;gap:12px;flex:none;padding:10px 18px;background:#fff;border-bottom:1px solid #dee3e9;flex-wrap:wrap}
.iw-title{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:600;color:#0a1317;flex-wrap:wrap}
.iw-title small{font-weight:400;color:#8595a4;font-size:12px}
.iw-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-left:auto}
.iw-btn{border:1px solid #d7dde5;background:#fff;color:#1c1e21;border-radius:7px;padding:5px 11px;font-size:12px;cursor:pointer;text-decoration:none;font-family:inherit;line-height:1.5}
.iw-btn:hover{border-color:#0064e0;color:#0064e0}
.iw-btn.primary{background:#0064e0;border-color:#0064e0;color:#fff}
.iw-btn.primary:hover{background:#0053bb;color:#fff}
.iw-btn:disabled{opacity:.5;cursor:default}
.iw-chip{display:inline-flex;align-items:center;gap:6px;border-radius:99px;padding:3px 10px;font-size:11px;background:#eef1f5;color:#5d6c7b}
.iw-chip.on{background:#e6f6ec;color:#1d7a3c}
.iw-chip.off{background:#fff0ed;color:#b3402f}
.iw-chip.warn{background:#fff5e2;color:#9a6700}
.iw-dot{width:7px;height:7px;border-radius:50%;background:currentColor}
.iw-frame{flex:1;min-height:0;width:100%;border:0;background:#e7eaee;display:block}
.iw-empty{flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:32px;text-align:center;color:#5d6c7b}
.iw-empty h2{margin:0;font-size:18px;color:#1c1e21}
.iw-empty p{margin:0;max-width:560px;font-size:13px;line-height:1.8}
.iw-empty code{background:#dfe4ea;padding:2px 6px;border-radius:4px;font-size:12px}
.iw-error{margin:8px 18px 0;padding:9px 12px;border-radius:7px;background:#fff3ef;border:1px solid #f3cfc4;color:#a13c28;font-size:12px;flex:none}
.iw-note{flex:0 1 auto;min-width:0;max-width:min(420px,38vw);padding:4px 10px;border-radius:7px;
  font-size:12px;line-height:1.55;background:#eef4ff;border:1px solid #cddffb;color:#1b4d8f;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.iw-note.bad{background:#fff3ef;border-color:#f3cfc4;color:#a13c28}
/* 窄一点先把次要的自述说明收起来，优先保证提示条和按钮不换行 */
@media(max-width:1180px){.iw-title small{display:none}}
@media(max-width:820px){.iw-bar{padding:8px 12px}}
`;

    function Button(label, onClick, opts = {}) {
      return h('button', { type: 'button', className: 'iw-btn' + (opts.primary ? ' primary' : ''), onClick, disabled: !!opts.disabled, ...(opts.extra || {}) }, label);
    }

    function Workbench() {
      const [status, setStatus] = useState(null);
      const [error, setError] = useState('');
      const [notice, setNotice] = useState(null);
      const [busy, setBusy] = useState('');
      const [nonce, setNonce] = useState(0);

      const call = useCallback(async (action) => {
        setBusy(action || '刷新');
        setError('');
        try {
          const response = action
            ? await fetch(STATUS_API, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) })
            : await fetch(STATUS_API, { cache: 'no-store' });
          if (!response.ok) throw new Error('宿主接口返回 HTTP ' + response.status);
          const data = await response.json();
          setStatus(data);
          if (data.running && action === 'start') setNonce(n => n + 1);
          if (!data.running && action === 'start') setError('后端仍未就绪，可查看日志：' + (data.log || '~/.dsh/image-workbench/backend.log'));

          // 每个动作都要能看见结果：以前点了像没反应，就是因为什么都不回显
          // 文案压短，完整信息放 hint（渲染成 title），否则窄一点就把按钮挤到第二行
          if (action === 'open') {
            const done = data.lastAction || {};
            setNotice(done.ok
              ? { ok: true, text: '已用系统浏览器打开', hint: '已在系统默认浏览器打开 ' + (data.url || '') }
              : { ok: false, text: '打开浏览器失败', hint: '原因：' + (done.error || '未知') + '。也可以手动访问 ' + (data.url || '') });
          } else if (action === 'refresh') {
            setNotice(data.running
              ? { ok: true, text: '后端正常 · 端口 ' + (data.port || 8092), hint: '已检查：工作台后端在跑，端口正常响应' }
              : { ok: false, text: '端口 ' + (data.port || 8092) + ' 无响应', hint: '已检查：后端没在跑，点「启动后端」' });
          }
        } catch (e) {
          setError(e.message || '状态查询失败');
        } finally {
          setBusy('');
        }
      }, []);

      // 提示条自动消失，避免一直占着页面顶部
      useEffect(() => {
        if (!notice) return undefined;
        const timer = setTimeout(() => setNotice(null), notice.ok ? 6000 : 10000);
        return () => clearTimeout(timer);
      }, [notice]);

      const copyUrl = useCallback(async () => {
        const url = status?.url || 'http://127.0.0.1:8092/';
        try {
          await navigator.clipboard.writeText(url);
          setNotice({ ok: true, text: '地址已复制', hint: '已复制到剪贴板，可直接粘到浏览器打开：' + url });
        } catch {
          setNotice({ ok: false, text: '复制失败', hint: '复制被环境拦下了，手动复制这个地址：' + url });
        }
      }, [status]);

      // 不做定时轮询：iframe 一旦重载，正在编辑的工程会被打断。
      useEffect(() => { call(); }, [call]);

      const running = Boolean(status?.running);
      const appUrl = status?.url || 'http://127.0.0.1:8092/';
      const guard = status?.watchdog;
      const guardText = !status ? '检查中'
        : !guard ? '守护未加载 · 需重启 DSH'
          : !guard.enabled ? '守护已关闭'
            : '守护已开' + (guard.restarts ? ' · 已自动拉起 ' + guard.restarts + ' 次' : '')
              + (guard.consecutiveFailures ? ' · 拉起失败 ' + guard.consecutiveFailures + ' 次' : '');
      const guardClass = !status ? '' : !guard ? 'warn' : guard.enabled && !guard.consecutiveFailures ? 'on' : 'off';
      // 号池是工作台自带的第二个进程，状态由后端上报；不再有独立号池插件
      const bp = status?.pool;
      const poolText = !status ? '' : !bp ? '内置号池读取中'
        : bp.online ? `内置号池 ${bp.active}/${bp.total} · 剩 ${bp.quota} 次`
          : '内置号池未就绪';
      const poolClass = !bp ? 'warn' : bp.online ? 'on' : 'off';

      return h('div', { className: 'iw-root' },
        h('style', null, CSS),
        h('header', { className: 'iw-bar' },
          h('div', { className: 'iw-title' },
            'Bluxe手绘工坊 · 生图工作台',
            h('small', null, '号池运行时随插件自带 · 可独立运行'),
            h('span', { className: 'iw-chip ' + guardClass, title: '后端端口掉线时由 DSH 宿主自动拉起；修改插件后需重启 DSH 才会加载新代码' },
              h('span', { className: 'iw-dot' }), guardText),
            h('span', { className: 'iw-chip ' + poolClass, title: '号池服务端代码与 uv 随插件自带，全新机器首次启动自动初始化虚拟环境；由工作台后端自己启动与守护，不需要任何第三方号池插件' },
              h('span', { className: 'iw-dot' }), poolText)),
          // 提示放在标题右侧、按钮左侧这条空档里，不额外占一行高度
          notice && h('div', { className: 'iw-note' + (notice.ok ? '' : ' bad'), role: 'status', title: notice.hint || notice.text }, notice.text),
          h('div', { className: 'iw-actions' },
            Button(busy === 'refresh' ? '检查中…' : '刷新状态', () => call('refresh'), { disabled: !!busy }),
            // 只有在没跑的时候才给「启动后端」，跑着的时候这个按钮点了也没意义
            !running && Button(busy === 'start' ? '启动中…' : '启动后端', () => call('start'), { disabled: !!busy, primary: true }),
            Button(busy === 'open' ? '正在打开…' : '用系统浏览器打开', () => call('open'), { disabled: !!busy }),
            // 原来的「新窗口打开」是 <a target="_blank">，DSH 里既开不了新窗口又会静默失败，换成复制地址
            Button('复制地址', copyUrl))),
        error && h('div', { className: 'iw-error', role: 'alert' }, error),
        running
          ? h('iframe', {
              key: 'frame-' + nonce,
              className: 'iw-frame',
              src: appUrl + (appUrl.includes('?') ? '&' : '?') + 'dsh=' + nonce,
              title: '生图工作台（图文 / 摄影）',
              allow: 'clipboard-read; clipboard-write; fullscreen',
            })
          : h('div', { className: 'iw-empty' },
              h('h2', null, status ? '工作台后端未运行' : '正在连接工作台'),
              h('p', null,
                '插件会自动拉起本机 image-local 工作台（uvicorn，端口 ',
                h('code', null, String(status?.port || 8092)),
                '）。若这里一直是空白，请确认 DSH 已重启加载插件，或点「用系统浏览器打开」直接访问。'),
              status?.serverDir && h('p', null, '后端目录：', h('code', null, status.serverDir)),
              Button(busy ? '正在处理' : '立即启动并重试', () => call('start'), { primary: true, disabled: !!busy }),
            ),
      );
    }

    function HeaderIcon({ size = 20 }) {
      return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, 'aria-hidden': true },
        h('rect', { x: 3, y: 3, width: 18, height: 18, rx: 2 }),
        h('circle', { cx: 9, cy: 9, r: 2.2 }),
        h('path', { d: 'M21 15l-5-5L5 21' }));
    }

    function apply(ctx) {
      const slots = ctx?.slots;
      if (!slots?.inject) return;
      const register = () => {
        const disposers = [];
        disposers.push(slots.inject('sidebar.panellist', () => slots.register(
          { name: 'sidebar.panellist', id: PANEL, order: 73, label: () => 'Bluxe手绘工坊 · 生图工作台' },
          HeaderIcon)));
        disposers.push(slots.inject('main', () => slots.register(
          { name: 'main', key: PANEL },
          Workbench)));
        return () => { for (const fn of disposers.reverse()) if (typeof fn === 'function') fn(); };
      };
      if (typeof ctx.effect === 'function') ctx.effect(register, 'image-workbench: panel registrations');
      else register();
    }

    return { name: PANEL, inject: ['slots'], apply };
  },
});
