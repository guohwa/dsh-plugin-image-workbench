"""本地生图工作台冒烟：符号断言 + 全量读接口 + 一次真实端到端出图。

用法：
  .venv/Scripts/python.exe smoke_check.py           # 全量（含真实出图，约 1 分钟）
  .venv/Scripts/python.exe smoke_check.py --quick   # 只跑符号与读接口
"""
from __future__ import annotations

import json, pathlib, sys, time
import httpx

BASE = 'http://127.0.0.1:8092'
QUICK = '--quick' in sys.argv
ROOT = pathlib.Path(__file__).resolve().parent
passed = failed = 0


def ok(name, cond, detail=''):
    global passed, failed
    if cond:
        passed += 1
        print('PASS  ' + name + (('  ' + str(detail)) if detail else ''))
    else:
        failed += 1
        print('FAIL  ' + name + (('  ' + str(detail)) if detail else ''))


# ---- 1) 离线符号断言：抓「语法合法但符号丢失」 ----
print('=== 1. 关键符号 ===')
sys.path.insert(0, str(ROOT))
import app, pool, pool_admin, pool_service, composer, history, imaging, catalog, access, drafts
for mod, names in [
    (app, ['app', 'POOL', 'HISTORY', 'DRAFTS', 'CATALOG', 'api_generate',
           'api_compose', '_finish_with_replace']),
    (pool, ['PoolManager', 'load_config', 'pool_api_key', 'raise_for_response',
            'size_for_ratio', 'pool_home', 'pool_admin_key']),
    # 号池面板：导入账号 / 删号 / 签发 Key 全靠这几个入口，缺一个面板就是死的
    (pool_admin, ['panel', 'import_accounts', 'delete_accounts', 'set_status',
                  'ensure_key', 'parse_tokens', 'target']),
    # 工作台自带号池：进程生命周期全在这里，不再有独立号池插件
    (pool_service, ['status', 'start', 'stop', 'restart', 'bootstrap',
                    'ensure_admin_key', 'set_auto_start', 'start_watchdog', 'view']),
    (composer, ['compose']),
    (history, ['History']),
    (imaging, ['ensure', 'ensure_all', 'variant_path']),
    (catalog, ['build']),
    (drafts, ['Drafts']),
]:
    for n in names:
        ok(f'{mod.__name__}.{n}', hasattr(mod, n))
ok('PoolManager._execute', callable(getattr(pool.PoolManager, '_execute', None)))
ok('PoolManager.snapshot', callable(getattr(pool.PoolManager, 'snapshot', None)))
ok('PoolManager.check_all', callable(getattr(pool.PoolManager, 'check_all', None)))
ok('access 无 fcntl 依赖', not hasattr(access, 'fcntl'))
ok('LAB 指向本地', str(app.LAB).replace('\\', '/').endswith('image-local'))
ok('AUTH 为空(本地全放行)', app.AUTH is None)
ok('catalog 三类齐全', all(len(app.CATALOG[k]) > 0 for k in ('styles', 'layouts', 'colors')),
   {k: len(v) for k, v in app.CATALOG.items()})
ok('config.local.json 存在', (ROOT / 'config.local.json').is_file())
cfg = pool.load_config()
# 接口地址允许换（38651 生图号池 / 38650 写作号池 / 任意 OpenAI 兼容地址），
# 这里只校验「配置能读出来且是合法 URL」，端口写死会让冒烟在换接口后假失败
ok('接口地址可读', bool(cfg['pool_base_url']) and cfg['pool_base_url'].startswith('http'),
   cfg['pool_base_url'])
ok('号池用户 Key 可读', bool(pool.pool_api_key(cfg)))
ok('出图模型 = gpt-image-2', cfg['model_image'] == 'gpt-image-2')

# 公众号首图 2.35:1：上游没有这个尺寸，靠 3:2 出图 + 本地居中裁切；
# 提示词里必须先说清「上下留可裁背景」，否则裁完砍标题
ok('2.35:1 按 1536x1024 出图', pool.size_for_ratio('2.35:1') == '1536x1024',
   pool.size_for_ratio('2.35:1'))
ok('认得出要裁切的比例', imaging.needs_crop('2.35:1') and not imaging.needs_crop('3:4'))
ok('提示词带横向安全区约束',
   bool(composer.WIDE_CROP_HINT) and composer.WIDE_CROP_HINT[:6] in
   composer.build_prompt({'title': '选题', 'body': ''}, {}, '2.35:1', [],
                         False, 'normal', '', False, 'graphic-text', ''))
try:
    from PIL import Image as _Img
    _src = _Img.new('RGB', (1536, 1024), (250, 245, 255))
    _cut = imaging.crop_to(_src, '2.35:1')
    ok('居中裁切比例正确', _cut.size[0] == 1536 and abs(_cut.size[0] / _cut.size[1] - 2.35) < 0.01,
       _cut.size)
    ok('不用裁的比例原样返回', imaging.crop_to(_src, '3:4').size == (1536, 1024))
except Exception as e:
    ok('裁切函数可用', False, str(e)[:100])

# ---- 2) 全量读接口 ----
print('\n=== 2. 读接口 ===')
client = httpx.Client(base_url=BASE, trust_env=False, timeout=30)
routes = [
    ('/', 200), ('/health', 200), ('/api/catalog', 200), ('/api/pool', 200),
    ('/api/monitor', 200), ('/api/recent', 200), ('/api/history', 200),
    ('/api/tasks', 200), ('/api/drafts', 200), ('/api/pool/check', 200),
    ('/api/pool/fails', 200), ('/api/pool/panel', 200),
    ('/api/pool/service', 200),
    ('/api/config', 200),
    ('/img/individual/FA/FA-001.webp', 200),
]
for path, expect in routes:
    try:
        r = client.get(path)
        ok(f'GET {path}', r.status_code == expect, f'HTTP {r.status_code} / {len(r.content)}B')
    except Exception as e:
        ok(f'GET {path}', False, type(e).__name__ + ': ' + str(e)[:100])

try:
    p = client.get('/api/pool').json()
    ok('号池 snapshot 有 pool 统计', isinstance(p.get('pool'), dict),
       f"{p.get('pool', {}).get('total')} 号 / 配额 {p.get('pool', {}).get('quota_total')}")
    ok('号池在线', bool(p.get('pool', {}).get('online')))
    # 失败换号：snapshot 要能给出「今天谁失败过几次」
    ok('号池 snapshot 有今日失败统计', isinstance(p.get('fails_today'), int),
       f"今日失败 {p.get('fails_today')} 次 / 涉及 {len(p.get('fail_accounts') or [])} 个号")
    f = client.get('/api/pool/fails').json()
    ok('GET /api/pool/fails 有结构', isinstance(f.get('items'), list) and 'total_today' in f,
       f"{f.get('total_today')} 次")
except Exception as e:
    ok('号池 snapshot 可解析', False, str(e)[:120])

# 「接口与号池」面板：账号导入 / 删号全靠它，字段缺一个前端就是空白
try:
    pn = client.get('/api/pool/panel').json()
    ok('panel 有 local / online 标志',
       isinstance(pn.get('local'), bool) and isinstance(pn.get('online'), bool),
       f"local={pn.get('local')} online={pn.get('online')}")
    ok('panel 给出对外接口地址', str(pn.get('base_url', '')).startswith('http'),
       pn.get('base_url'))
    sv = pn.get('service') or {}
    ok('panel 带内置号池服务快照',
       isinstance(sv.get('manageable'), bool) and isinstance(sv.get('runtime_ready'), bool)
       and 'auto_start' in sv, f"port={sv.get('port')} runtime={sv.get('runtime_ready')}")
    ok('内置号池端口可管理（不是写作台 38650）', sv.get('manageable') is True,
       sv.get('message'))
    ok('号池运行时就绪（uv + source + venv）', sv.get('runtime_ready') is True,
       sv.get('runtime_reason'))
    if pn.get('local') and pn.get('online'):
        accs = pn.get('accounts') or []
        ok('panel 账号带掩码邮箱与状态',
           bool(accs) and all(a.get('email') and a.get('status') for a in accs),
           f"{accs[0]['email'] if accs else '—'} / 共 {pn.get('total')} 个")
        # 账号 id 必须是 12 位摘要：access_token 不能出现在前端
        ok('panel 账号 id 是摘要不是 token',
           all(len(str(a.get('id') or '')) == 12 for a in accs),
           accs[0].get('id') if accs else '')
        ok('panel 能拿到用户 Key 明文（供复制）', bool(pn.get('api_key')),
           pn.get('api_key_masked'))
    else:
        print('      当前接口不是本机号池，跳过账号字段断言')
except Exception as e:
    ok('GET /api/pool/panel 可用', False, str(e)[:120])

# 内置号池服务：状态可读、自动拉起开关可写（只写回原值，不改变用户设置）
try:
    svc = client.get('/api/pool/service').json()
    ok('服务状态字段齐全',
       all(k in svc for k in ('port', 'home', 'running', 'online', 'auto_start',
                              'runtime_ready', 'manageable', 'message')),
       f"port={svc.get('port')} online={svc.get('online')} total={svc.get('total')}")
    r = client.post('/api/pool/service/auto', json={'enabled': bool(svc.get('auto_start'))})
    d = r.json()
    ok('POST /api/pool/service/auto 可写', r.status_code == 200 and d.get('ok') is True
       and d.get('auto_start') == bool(svc.get('auto_start')), f'HTTP {r.status_code}')
except Exception as e:
    ok('GET /api/pool/service 可用', False, str(e)[:120])

# 导入解析：三种常见粘贴格式都要认，认不出时不能瞎编 token
try:
    _tk = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.abc12345'
    _a, _ = pool_admin.parse_tokens(_tk)
    _b, _ = pool_admin.parse_tokens(json.dumps([{'access_token': _tk}]))
    _c, _bad = pool_admin.parse_tokens('a@b.com----pw----' + _tk + '\n乱七八糟')
    ok('导入解析：裸 token / JSON / 老导出格式',
       _a == [_tk] and _b == [_tk] and _c == [_tk] and _bad == 1,
       f'{len(_a)}/{len(_b)}/{len(_c)} bad={_bad}')
    ok('导入解析：没有 token 时不编造', pool_admin.parse_tokens('这不是 token')[0] == [])
    _row = {'access_token': _tk, 'email': 'a@x.com', 'password': 'pw1',
            'source_type': 'web', 'quota': 25, 'status': '正常'}
    _t, _it, _b = pool_admin.parse_import(json.dumps([_row]))
    ok('JSON 导入保留密码与来源类型',
       _t == [] and len(_it) == 1 and _it[0].get('password') == 'pw1'
       and _it[0].get('source_type') == 'web' and _b == 0,
       f'{len(_it)} 条 / {sorted(_it[0]) if _it else []}')
    ok('JSON 导入不灌运行态数据',
       'quota' not in (_it[0] if _it else {}) and 'status' not in (_it[0] if _it else {}))
    _tk2 = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ5In0.bbb67890'
    _t2, _it2, _ = pool_admin.parse_import(json.dumps({'items': [_row, _tk2]}))
    ok('导入混合格式：对象走 accounts、纯串走 tokens',
       len(_it2) == 1 and _t2 == [_tk2], f'obj={len(_it2)} raw={len(_t2)}')
    ok('导入解析：accessToken 别名归一',
       pool_admin.parse_import(json.dumps({'accessToken': _tk, 'password': 'pw'}))[1][0]['access_token'] == _tk)
except Exception as e:
    ok('导入解析可用', False, str(e)[:120])

# 成品图路由：不裁切的比例直连原图，裁切的给裁好的 PNG
try:
    _items = (client.get('/api/history').json() or {}).get('items') or []
    if _items:
        _jid = _items[0]['id']
        r = client.get(f'/api/history/{_jid}/final')
        ok('GET /api/history/{id}/final', r.status_code == 200 and
           r.headers.get('content-type', '').startswith('image/'),
           f'HTTP {r.status_code} / {len(r.content)}B')
    else:
        ok('历史里有可用记录', False, 'items 为空，跳过 final 路由')
except Exception as e:
    ok('GET /api/history/{id}/final', False, str(e)[:120])

# ---- 3) 纯本地拼装（不消耗额度）----print('\n=== 3. 本地拼装 ===')
compose_body = {
    'style_id': app.CATALOG['styles'][0]['id'],
    'layout_id': app.CATALOG['layouts'][0]['id'],
    'color_id': app.CATALOG['colors'][0]['id'],
    'ratio': '3:4', 'mood': '治愈、松弛、幽默', 'fusion': True,
    'topic': '如何停止内耗', 'image_mode': 'graphic-text',
    'items': [{'title': '先把事情写下来', 'body': '写下来，脑子就空了', 'scene': '书桌前'}],
}
try:
    r = client.post('/api/compose', json=compose_body)
    d = r.json()
    ok('POST /api/compose', r.status_code == 200 and bool(d), f'HTTP {r.status_code}')
    print('      返回键:', list(d.keys())[:8])
except Exception as e:
    ok('POST /api/compose', False, str(e)[:120])
try:
    r = client.post('/api/preview', json={'mode': 'image', 'topic': '测试',
                                          'prompt': '一只橘猫在旧书店门口晒太阳'})
    ok('POST /api/preview', r.status_code == 200 and bool(r.json().get('prompt')))
except Exception as e:
    ok('POST /api/preview', False, str(e)[:120])

# ---- 4) 端到端真实出图 ----
if not QUICK:
    print('\n=== 4. 端到端出图（会消耗 1 次号池配额）===')
    gen_body = {
        'mode': 'image', 'topic': '冒烟测试', 'title': '冒烟测试',
        'body': '一只橘猫蜷在旧书店门槛上打盹', 'ratio': '3:4',
        'image_mode': 'graphic-text', 'seq': 0, 'group': '__smoke__',
        'prompt': ('请生成一张插画：清晨的旧书店门口，木质门框挂着一串黄铜风铃，'
                   '一只橘猫蜷在门槛上打盹，阳光斜射进来，空气里浮着细小尘埃。'
                   '手绘水彩质感，柔和暖色调，画面干净，不要文字，不要水印，竖版构图。'),
    }
    try:
        r = client.post('/api/generate', json=gen_body, timeout=30)
        jid = r.json().get('job_id') if r.status_code == 200 else ''
        ok('POST /api/generate', bool(jid), f'HTTP {r.status_code} job={jid}')
    except Exception as e:
        jid = ''
        ok('POST /api/generate', False, str(e)[:120])
    if jid:
        deadline = time.time() + 300
        status, job = '', {}
        while time.time() < deadline:
            time.sleep(3)
            try:
                job = client.get('/api/jobs/' + jid, timeout=20).json()
                status = str(job.get('status') or '')
                if status in ('done', 'error'):
                    break
            except Exception:
                pass
        ok('任务走到终态', status in ('done', 'error'), f'status={status}')
        if status == 'done':
            img = (job.get('result') or {}).get('image')
            f = ROOT / 'images' / str(img)
            ok('原图已落盘', bool(img) and f.is_file(),
               f'{img} / {f.stat().st_size if f.is_file() else 0} B')
            if f.is_file():
                ok('原图是 PNG', f.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n')
                time.sleep(2)
                ok('card 压缩图已生成', (ROOT / 'thumbs' / f'{jid}_c.webp').is_file())
                ok('preview 压缩图已生成', (ROOT / 'thumbs' / f'{jid}_p.webp').is_file())
                try:
                    dl = client.get('/api/history/' + jid + '/download')
                    ok('下载返回原图', dl.status_code == 200 and len(dl.content) == f.stat().st_size,
                       f'{len(dl.content)} B')
                except Exception as e:
                    ok('下载返回原图', False, str(e)[:100])
                ok('历史记录已写入', any(x.get('id') == jid for x in
                                    client.get('/api/history').json().get('items', [])))
        else:
            print('      错误信息:', str(job.get('error') or '')[:250])

client.close()
print(f'\n结果：{passed} 通过 / {failed} 失败')
sys.exit(0 if failed == 0 else 1)
