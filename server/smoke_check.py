"""本地生图工作台冒烟：符号断言 + 全量读接口 + 一次真实端到端出图。

用法：
  .venv/Scripts/python.exe smoke_check.py           # 全量（含真实出图，约 1 分钟）
  .venv/Scripts/python.exe smoke_check.py --quick   # 只跑符号与读接口
"""
from __future__ import annotations

import asyncio, json, pathlib, sys, time
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
                  'ensure_key', 'parse_tokens', 'parse_import', '_parse_dash_row',
                  'IMPORT_FIELDS', 'target']),
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

# 参考图模式（style=只借画风 / full=完整复刻，对齐漫画工作台的身份/姿势解耦思路）
_src_index = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
ok('GenRequest 默认 ref_mode=style', app.GenRequest(mode='image', topic='t').ref_mode == 'style')
ok('ComposeRequest 默认 ref_mode=style', app.ComposeRequest().ref_mode == 'style')
ok('REF_FUSION 只借风格不抄内容',
   '借鉴风格' in composer.REF_FUSION and '复制这张图' in composer.REF_FUSION)
ok('REF_FUSION_FULL 是一比一复刻', '一比一还原' in composer.REF_FUSION_FULL)
_r1 = app.GenRequest(mode='image', topic='t'); _r1.ref_id = 'x'; _r1.ref_mode = 'style'; _r1.prompt = None
_r2 = app.GenRequest(mode='image', topic='t'); _r2.ref_id = 'x'; _r2.ref_mode = 'full'; _r2.prompt = None
ok('build_image_prompt 按 ref_mode 走不同指令',
   '借鉴风格' in app.build_image_prompt(_r1)
   and '一比一还原' in app.build_image_prompt(_r2)
   and app.build_image_prompt(_r1) != app.build_image_prompt(_r2))
ok('build_image_prompt 已有 prompt 也把参考约束拼在最前（不丢约束）',
   app.build_image_prompt(app.GenRequest(mode='image', topic='t', ref_id='x',
                                        ref_mode='style', prompt='用户自己的提示词正文')).startswith('参考我上传的这张图'))
ok('build_image_prompt 无参考图时不塞入约束',
   '参考我上传的这张图' not in app.build_image_prompt(app.GenRequest(mode='image', topic='t', prompt='纯文案')))
_sel = {'style': None, 'layout': None, 'color': None}
ok('build_prompt 不把参考约束写进展示用提示词（避免像「代码」混进用户文案）',
   '借鉴风格' not in composer.build_prompt({'title': 't', 'body': 'b'}, _sel, '3:4', [], False, 'normal', ref=True, ref_mode='style')
   and '复制这张图' not in composer.build_prompt({'title': 't', 'body': 'b'}, _sel, '3:4', [], False, 'normal', ref=True, ref_mode='full'))
ok('前端参考图卡片带「参考图模式」下拉',
   'id="refMode"' in _src_index and '只借画风' in _src_index and '完整复刻参考图' in _src_index)
ok('前端 payload 与 composeBody 透传 ref_mode', 'ref_mode: state.refMode' in _src_index)
ok('PoolManager._execute', callable(getattr(pool.PoolManager, '_execute', None)))
ok('PoolManager.snapshot', callable(getattr(pool.PoolManager, 'snapshot', None)))
ok('PoolManager.check_all', callable(getattr(pool.PoolManager, 'check_all', None)))
ok('PoolManager.cancel_all', callable(getattr(pool.PoolManager, 'cancel_all', None)))
ok('app.api_generate_stop', hasattr(app, 'api_generate_stop'))
ok('POST /api/generate/stop 已注册',
   any(getattr(r, 'path', '') == '/api/generate/stop'
       and 'POST' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('POST /api/upgrade/apply 已注册',
   any(getattr(r, 'path', '') == '/api/upgrade/apply'
       and 'POST' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('POST /api/upgrade/restart 已注册',
   any(getattr(r, 'path', '') == '/api/upgrade/restart'
       and 'POST' in getattr(r, 'methods', set()) for r in app.app.routes))
# AI 热点选题：左栏那块资讯靠这两个路由，路由没了就是一块死内容
ok('GET /api/aihot/topics 已注册',
   any(getattr(r, 'path', '') == '/api/aihot/topics'
       and 'GET' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('GET /api/aihot/story 已注册',
   any(getattr(r, 'path', '') == '/api/aihot/story'
       and 'GET' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('aihot 事件 id 解析：只认 /story/<id>',
   app._aihot_story_id('https://aihot.news/story/760e0591-91c2-4b7a-aea8-5a33ba5d86b1')
   == '760e0591-91c2-4b7a-aea8-5a33ba5d86b1'
   and app._aihot_story_id('') == ''
   and app._aihot_story_id('https://aihot.news/items/abc') == '')
_tmp_hot = app._aihot_slim({"items": [
    {"rank": 1, "title": "测试事件", "sourceCount": 7,
     "links": {"story": "https://aihot.news/story/abc-1", "aihot": "https://aihot.news/items/x"}},
    {"rank": 2, "title": "   "},          # 没标题的丢掉，别渲染成一行空白
    "not a dict",
]})
ok('热点榜瘦身：丢空标题、字段齐全',
   len(_tmp_hot) == 1 and _tmp_hot[0]['sourceCount'] == 7 and _tmp_hot[0]['storyId'] == 'abc-1')
ok('热点榜瘦身：坏输入不抛异常', app._aihot_slim({}) == [] and app._aihot_slim({"items": None}) == [])
# 版本比较：必须按数字段比，字符串比会把 1.1.9 判成比 1.1.10 新
_ver_cases = [('1.1.9', '1.1.10', False), ('1.1.17', '1.1.16', True), ('1.1', '1.1.0', False),
              ('1.1.17', '1.1.17', False), ('1.1.17-beta.1', '1.1.17', False),
              ('1.1.17-beta.1', '1.1.16', True), ('1.10.0', '1.9.9', True)]
ok('版本比较：按数字段比，预发布版低于同号正式版',
   all((app._ver_key(a) > app._ver_key(b)) == exp for a, b, exp in _ver_cases))

_src_pool = (ROOT / 'pool.py').read_text(encoding='utf-8')
_src_html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
_src_app = (ROOT / 'app.py').read_text(encoding='utf-8')
ok('已停止状态不落盘', 'not in ("done", "error", "cancelled")' in _src_pool)
ok('取消后不再换号重试', 'if job.get("status") == "cancelled":' in _src_pool)
ok('前端两个停止按钮齐全', 'id="stopGen"' in _src_html and 'id="monStop"' in _src_html)
ok('前端停止链路完整',
   all(x in _src_html for x in ('async function stopAllJobs', '/api/generate/stop',
                                'clearBatch();', "d.status === 'cancelled'")))
# AI 热点选题：卡片必须真的落在「选题」输入框上方，节点顺序错了会跑到下面去
ok('前端 AI 热点选题节点齐全',
   all(x in _src_html for x in ('id="aihotList"', 'id="aihotDlg"', 'id="aihotDList"',
                                'id="aihotMore"', 'id="aihotRefresh"')))
ok('前端选题卡片排在选题输入框之前',
   0 < _src_html.find('id="aihotList"') < _src_html.find('label for="topic"'))
ok('前端标注了数据来源 AIHOT', 'AIHOT（aihot.news）' in _src_html)
ok('前端「用作选题」只填框、不立刻存草稿',
   all(x in _src_html for x in ('function useAsTopic', 'bindDraft(null, \'\')',
                                'markContentClean()', 'ai-use')),
   '选题应当只填空、不立刻存草稿；漏了会退化成每选一次存一版')
ok('前端自动暂存：只有选题、还没成文时不存草稿',
   '只有选题、还没成文' in _src_html and 'hasArticleContent(d)' in _src_html,
   '漏了会让「用作选题」又变成每次都存一版')
ok('前端草稿标题可点击载入',
   all(x in _src_html for x in ('b[data-role="label"]', 'labelEl.onclick = loadDraft',
                                '点击载入这份草稿')),
   '漏了草稿标题就点不动，只能去瞄那个小按钮')
ok('草稿列表小字 = 时间 · 正文首句，不拼张数/选题片段',
   all(x in _src_html for x in ('const preview = (it.preview ||',
                                'fmtTime(it.updated)', '${esc(dsTxt)}</span>',
                                "fmtTime(it.updated) + ' · ' + preview"))
   and 'const sub = clipText([it.count' not in _src_html,
   '漏了会让草稿卡片又冒出「N张图·选题片段」那行脏内容')
ok('后端草稿列表返回 preview 首句字段',
   'preview' in (drafts.Drafts(ROOT / 'workbench' / 'drafts.json').list().get('items', [{}])[0]
                if drafts.Drafts(ROOT / 'workbench' / 'drafts.json').list().get('items') else {}),
   '前端靠 it.preview 渲染，后端不返回就会整片空白')
ok('AI 生成文章带「条数」输入框，默认 7，范围 1-20',
   all(x in _src_html for x in ('id="genCount"', 'type="number"', 'value="7"',
                                'min="1"', 'max="20"')),
   '漏了条数框，正文就不知道要拆成几条贴图')
ok('payload 把条数带进请求（num_items）',
   "num_items: Number.isFinite(n)" in _src_html and "$('#genCount')" in _src_html,
   '前端不传 num_items，后端就用不了条数框')
ok('后端：num_items 优先控制编号清单条数并带标签',
   'req.num_items and req.num_items >= 1' in _src_app
   and '严格输出 {n} 条贴图文案' in _src_app
   and '3-5 个 #话题标签' in _src_app,
   '漏了会让条数框失效、或生成的正文没有标签')
ok('编号清单模板先写概述段落、再列 01，且不禁止开场导语',
   '先写一段 2-4 句的概述段落' in _src_app
   and '作为整组内容的总起' in _src_app
   and '不要开场导语' not in _src_app,
   '漏了「概述」会导致正文直接 01 开头、很突兀')
ok('编号清单模板要求编号行与正文紧挨、无中间空行',
   '编号行和正文之间不要有空行' in _src_app,
   '漏了会让「01｜标题」和正文之间多出一行空行')
ok('文本提示词要求简体中文、禁止乱码/英文碎片/字母数字组合',
   '全程使用流畅的简体中文写作' in _src_app
   and '字母数字组合' in _src_app,
   '漏了反乱码约束，模型偶发会吐 turn/5u8495ea07e7 这类碎片')

# ---- 热点事件综述进文本提示词：别让模型只凭一句标题脑补日期和名单 ----
ok('GenRequest 收 story_id 且默认不带',
   app.GenRequest(mode='text', topic='t').story_id is None)
_req_hot = app.GenRequest(mode='text', topic='黄仁勋辞任英伟达CEO并退出董事会', num_items=7)
_story_mock = {'title': '黄仁勋辞任英伟达CEO并退出董事会',
               'digest': '11月12日董事会宣布人事变动，董事会启动继任者遴选。',
               'latest': '', 'sourceCount': 17,
               'firstReportAt': '2026-10-29T13:42:38', 'latestAt': '2026-11-12T18:39:23'}
_p_old = app.build_text_prompt(_req_hot)
_p_new = app.build_text_prompt(_req_hot, _story_mock)
ok('无热点资料时提示词不含事实红线', '事实红线' not in _p_old)
ok('带热点资料时插入综述与事实红线',
   '综述：11月12日董事会宣布人事变动' in _p_new and '事实红线' in _p_new)
ok('资料块不改原有模板一个字', _p_new.endswith(_p_old))
ok('资料块带信源数与时间，减少模型编日期',
   '17 个信源' in _p_new and '首次报道 2026-10-29' in _p_new)
ok('空综述不插入资料块',
   app.build_text_prompt(_req_hot, {'title': 'x', 'digest': ''}) == _p_old)
ok('preview 与 generate 都接了资料取值', _src_app.count('await _text_story(req)') == 2)


async def _probe_text_story():
    async def _fake(_sid):
        return _story_mock
    orig = app._aihot_story_data
    app._aihot_story_data = _fake
    try:
        return (
            await app._text_story(app.GenRequest(
                mode='text', topic=_story_mock['title'], story_id='x')),
            await app._text_story(app.GenRequest(
                mode='text', topic='完全不相关的选题', story_id='x')),
            await app._text_story(app.GenRequest(
                mode='text', topic=_story_mock['title'])),
            await app._text_story(app.GenRequest(
                mode='image', topic=_story_mock['title'], story_id='x')),
        )
    finally:
        app._aihot_story_data = orig


_a, _b, _c, _d = asyncio.run(_probe_text_story())
ok('选题匹配热点才带资料', _a is _story_mock)
ok('选题与热点不一致不带资料', _b is None)
ok('没有 story_id 不取资料', _c is None)
ok('出图模式不取热点资料', _d is None)
ok('前端记住热点 storyId 并随生成请求传',
   all(x in _src_html for x in ('hotStory:null', 'story_id: story_id || undefined',
                                'useAsTopic(hit)')),
   '漏了就退回只给模型一句标题')
ok('选题文字改动即失效、会话快照带上 hot_story',
   'text === hs.title' in _src_html and 'snap.hot_story = state.hotStory' in _src_html)
ok('后端 sanitizer 清掉 word/hex 垃圾串、保留中文与 http 链接',
   '_sanitize_text' in _src_pool
   and '_GARBAGE_RE' in _src_pool
   and '5u8495ea07e7' not in pool._sanitize_text('正文里有 turn/5u8495ea07e7 也该清')
   and 'http://5u8495ea07e7' in pool._sanitize_text('链接 http://5u8495ea07e7 保留')
   and '(?<!:)' in _src_pool,
   '漏了清洗，正文会出现 turn/5u8495ea07e7 这类乱码')
ok('选择器单击放大预览：弹窗 / 确认按钮 / 双击直选都在',
   all(x in _src_html for x in ('id="pickPrev"', 'id="ppPick"', 'function openPickPrev',
                                'function confirmPick', 'b.ondblclick = ',
                                'function fitPrevWidth')),
   '缺一个就会出现「点了没反应」或「选不中」')
ok('选择器弹窗固定高度 + 焦点进框 + 滚轮接管',
   all(x in _src_html for x in ('height:min(86vh,880px)', 'dlg.focus()',
                                'function wirePickerWheel', 'wirePickerWheel()')),
   '高度改回内容自适应会随分组抽动；少了滚轮接管，鼠标停在标题上滚不动')

# 更新说明随包发布，升级面板从新版本的包里读它
_log = json.loads((ROOT / 'changelog.json').read_text(encoding='utf-8'))
_vers = [x.get('version') for x in _log if isinstance(x, dict)]
ok('更新说明文件可用', isinstance(_log, list) and _vers
   and all(x.get('items') for x in _log), f'{len(_log)} 个版本：{_vers[:3]}')
ok('更新说明按版本区间取，最新在前，同版本为空',
   len(_vers) >= 2
   and [r['version'] for r in app._notes_between(_vers[-1], _vers[0], _log)][0] == _vers[0]
   and app._notes_between(_vers[0], _vers[0], _log) == [],
   '跨版本升级要把漏掉的几条一次补全，否则用户只看到最后一版')
ok('更新说明不落后于当前版本', app._ver_key(_vers[0]) >= app._ver_key(app.VERSION),
   f'changelog {_vers[0]} / 当前 {app.VERSION} —— 发了版忘记写说明，面板就空了')
ok('升级白名单带上 changelog.json',
   app._upgrade_rel('server/changelog.json') == 'server/changelog.json'
   and app._upgrade_rel('server/other.json') is None)
ok('升级面板有更新说明区块',
   all(x in _src_html for x in ('id="upNotes"', 'id="upNotesBody"', 'function renderNotes')))
ok('access 无 fcntl 依赖', not hasattr(access, 'fcntl'))
ok('工作台目录可用', (ROOT / 'app.py').is_file())
ok('AUTH 为空(本地全放行)', app.AUTH is None)
ok('catalog 三类齐全', all(len(app.CATALOG[k]) > 0 for k in ('styles', 'layouts', 'colors')),
   {k: len(v) for k, v in app.CATALOG.items()})
ok('配置可缺省（整包首次运行）', isinstance(pool.load_config(), dict))
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
# 元素库自动选：空 scene 不再一律画杯子，且相邻不重复、按文案匹配
ok('元素库自动选：PROP_LIB/_pick_prop/_auto_scene 就位',
   hasattr(composer, 'PROP_LIB') and hasattr(composer, '_pick_prop') and hasattr(composer, '_auto_scene'),
   '缺元素库，空 scene 会退回固定的「具体实物道具」被模型画成杯子')
_L = [{'id': 'SC-002', 'prompt': '三层版式：上层主标题、中层小辅助图、下层副标题'}]
_c = asyncio.run(composer.compose({'ratio': '3:4', 'mood': ['治愈'], 'fusion': True, 'layout_id': 'SC-002', 'items': [
    {'title': '工资到账', 'body': '还完花呗钱包轻松'},
    {'title': '熬夜体检', 'body': '颈椎睡眠亮红灯养生该提上日程'},
    {'title': '周末躺平', 'body': '闹钟沙发日历一起休息'},
]}, [], _L, []))
ok('元素库自动选：空 scene 不再默认画杯子且进了提示词',
   all('杯' not in it['scene'] for it in _c['items']) and
   all('中层小辅助图' in it['prompt'] for it in _c['items']),
   '仍出现杯子或没进提示词：' + str([it['scene'] for it in _c['items']]))
ok('元素库自动选：相邻分镜元素不重复且不再单一（无杯子）',
   len({it['scene'] for it in _c['items']}) == len(_c['items']) and
   all('杯' not in it['scene'] for it in _c['items']),
   '相邻重复或仍出现杯子：' + str([it['scene'] for it in _c['items']]))
_c2 = asyncio.run(composer.compose({'ratio': '3:4', 'mood': ['治愈'], 'fusion': True, 'layout_id': 'SC-002', 'items': [
    {'title': 't', 'body': '', 'scene': '我手填的：小人举着一杯奶茶'}]}, [], _L, []))
ok('元素库自动选：用户手填 scene 不被覆盖',
   _c2['items'][0]['scene'] == '我手填的：小人举着一杯奶茶',
   '手填被覆盖：' + _c2['items'][0]['scene'])

# _parse_props 健壮性：直接覆盖 LLM 返回变异形态（live 实测踩过的坑）
_pp = composer._parse_props
# 1) 纯 JSON 数组
_a1 = _pp('["一枚硬币","一卷瑜伽垫","一张工资条"]', 3)
ok('_parse_props：纯 JSON 数组解析正确',
   _a1 == ['一枚硬币', '一卷瑜伽垫', '一张工资条'], '解析结果：' + str(_a1))
# 2) JSON 字符串里套数组（模型把数组包成字符串返回：用 json.dumps 包两层，
#    保证外层是「字符串」而非数组，复刻模型真实返回形态）
_inner_json = json.dumps(['一枚硬币', '一卷瑜伽垫', '一张工资条'], ensure_ascii=False)
_a2 = _pp(json.dumps(_inner_json, ensure_ascii=False), 3)
ok('_parse_props：字符串套 JSON 双层解析正确',
   _a2 == ['一枚硬币', '一卷瑜伽垫', '一张工资条'], '解析结果：' + str(_a2))
# 3) ESC 等控制字符污染（实测出现 \x1b）
_a3 = _pp('[\x1b"一枚硬币", "一卷瑜伽垫", "一张工资条"]', 3)
ok('_parse_props：ESC/控制字符被清掉',
   _a3 == ['一枚硬币', '一卷瑜伽垫', '一张工资条'], '解析结果：' + str(_a3))
# 4) 夹带无中文的垃圾元素（如残留方括号/前缀符号）应被过滤，只留有效项
_a4 = _pp('["一枚硬币", "abc123", "一卷瑜伽垫"]', 2)
ok('_parse_props：无中文的垃圾元素被过滤、只留有效项',
   _a4 == ['一枚硬币', '一卷瑜伽垫'], '解析结果：' + str(_a4))
# 5) 返回数量与预期 n 不符时必须抛错，绝不带着错位继续
try:
    _bad = _pp('["一枚硬币", "一卷瑜伽垫"]', 3)
    _raise_ok = False
except RuntimeError:
    _raise_ok = True
ok('_parse_props：返回数量与预期不符时抛错（绝不带着错位继续）',
   _raise_ok, '期望抛 RuntimeError 却返回：' + (str(_bad) if not _raise_ok else ''))
# 5) 退化成逐行文本也能解析
_a5 = _pp('一枚硬币\n一卷瑜伽垫\n一张工资条', 3)
ok('_parse_props：逐行文本退化形态可解析',
   _a5 == ['一枚硬币', '一卷瑜伽垫', '一张工资条'], '解析结果：' + str(_a5))
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
    ('/', 200), ('/health', 200), ('/api/catalog', 200), ('/api/samples', 200), ('/api/pool', 200),
    ('/api/monitor', 200), ('/api/recent', 200), ('/api/history', 200),
    ('/api/tasks', 200), ('/api/drafts', 200), ('/api/pool/check', 200),
    ('/api/pool/fails', 200), ('/api/pool/panel', 200),
    ('/api/pool/service', 200),
    ('/api/pool/proxy', 200),
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

# 出口代理 GET 应回传 proxy_auto_url 字段（零配置自动探测标记）
try:
    pr = client.get('/api/pool/proxy').json()
    ok('GET /api/pool/proxy 回传 proxy_auto_url 字段',
       'proxy_auto_url' in pr, f"auto={pr.get('proxy_auto_url') or '—'}")
except Exception as e:
    ok('GET /api/pool/proxy 可解析', False, str(e)[:120])

# 出口代理那一栏按本机实时探测结果反显，不写死地址：换机器要能跟着变
try:
    eg = client.get('/api/pool/egress').json()
    ok('GET /api/pool/egress 给出本机探测结果',
       eg.get('ok') is True and 'url' in eg and isinstance(eg.get('ports'), list),
       f"{eg.get('url') or '未探测到'}（来源 {eg.get('source')}）")
    ok('探测结果带候选端口列表', isinstance(eg.get('ports'), list),
       f"{len(eg.get('ports') or [])} 个端口在监听")
except Exception as e:
    ok('GET /api/pool/egress 可解析', False, str(e)[:120])

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
    # 用户可能临时把接口切到 38650 写作池，那时 manageable=False 是正确行为，
    # 不能拿「当下选了哪个池」当回归标准；只校验常量与端口-自洽关系。
    ok('内置号池默认端口仍是 38651（不是写作台 38650）',
       pool_service.DEFAULT_PORT == 38651 and 38650 in pool_service.FOREIGN_PORTS,
       f'DEFAULT_PORT={pool_service.DEFAULT_PORT}')
    ok('manageable 与当前端口自洽',
       (sv.get('manageable') is True) ==
       (bool(sv.get('local')) and int(sv.get('port') or 0) not in pool_service.FOREIGN_PORTS),
       f'port={sv.get("port")} local={sv.get("local")} manageable={sv.get("manageable")}')
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
    ok('导入解析：带 BOM 的 txt 文件',
       pool_admin.parse_import('\ufeff' + _tk)[0] == [_tk])
    _csv = 'email,password,access_token\r\na@b.com,pw1,' + _tk + '\r\nc@d.com,pw2,' + _tk2
    _ct, _, _cb = pool_admin.parse_import(_csv)
    ok('导入解析：CSV 表头跳过、CRLF 兼容',
       _ct == [_tk, _tk2] and _cb == 0, f'{len(_ct)} 条 / bad={_cb}')
    # 五段式「首登料」：邮箱----密码----邮箱Client ID----邮箱Refresh Token----AT
    # 第 4 段是微软 MSA 邮箱令牌，绝不能被当成号池的 refresh_token 提交。
    _mail_rt = ('M.C535_SN1.0.U.MsaArtifacts.-CgyvaETosvszterVMMhyTAqQdRDx0FMPDuNxRjzP'
                '!mpzzeyPlYSeqOkMN9W0m9aWwvjO5DZtLl6jb3dKlIP7PHuCRV7$')
    _cid = '9e5f94bc-e8a4-4e73-b8be-63364c364c29d753'
    _five = 'a@b.com----pw1----' + _cid + '----' + _mail_rt + '----' + _tk
    _note = '不会用的别买，发货格式：邮箱----邮箱密码----邮箱Client ID----邮箱Refresh Token----AT'
    _t5, _i5, _b5 = pool_admin.parse_import(_five + '\n' + _note)
    _one = _i5[0] if _i5 else {}
    ok('导入解析：五段式发料保留完整资料',
       _t5 == [] and len(_i5) == 1 and _b5 == 0
       and _one.get('email') == 'a@b.com' and _one.get('password') == 'pw1'
       and _one.get('mail_client_id') == _cid
       and _one.get('mail_refresh_token') == _mail_rt
       and _one.get('access_token') == _tk and _one.get('source_type') == 'web',
       f'items={len(_i5)} bad={_b5} keys={sorted(_one)}')
    ok('导入解析：邮箱 Refresh Token 不写成号池 refresh_token',
       'refresh_token' not in _one and _one.get('mail_refresh_token') == _mail_rt)
    ok('导入解析：发料说明行不算坏行', _b5 == 0, f'bad={_b5}')
    _t3, _i3, _ = pool_admin.parse_import('c@d.com----pw2----' + _tk2)
    ok('导入解析：三段式发料也带邮箱密码',
       _t3 == [] and len(_i3) == 1 and _i3[0].get('password') == 'pw2'
       and _i3[0].get('access_token') == _tk2)
    _guarded = pool_admin.parse_import(
        json.dumps([{'access_token': _tk, 'refresh_token': _mail_rt}]))[1][0]
    ok('导入解析：误填到 refresh_token 的邮箱料自动归档',
       'refresh_token' not in _guarded and _guarded.get('mail_refresh_token') == _mail_rt)
    # 真实发料里出现过 JWT 签名含连续横线的行，按分隔符硬切会把资料劈丢
    _tk_dash = 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ6In0.abc----def12345'
    _dash_row = 'e@f.com----pw3----' + _cid + '----' + _mail_rt + '----' + _tk_dash
    _td, _idd, _bd = pool_admin.parse_import(_dash_row)
    ok('导入解析：JWT 内含连续横线也不丢资料',
       _td == [] and len(_idd) == 1 and _bd == 0
       and _idd[0].get('access_token') == _tk_dash
       and _idd[0].get('password') == 'pw3'
       and _idd[0].get('mail_refresh_token') == _mail_rt,
       f'items={len(_idd)} bad={_bd}')
    _mt, _mi, _mb = pool_admin.parse_import(_five + '\n' + _tk2)
    ok('导入解析：五段式与裸 token 混贴不重复',
       _mt == [_tk2] and len(_mi) == 1 and _mb == 0,
       f'raw={len(_mt)} obj={len(_mi)} bad={_mb}')
except Exception as e:
    ok('导入解析可用', False, str(e)[:120])

# 人工剔除名单：只存摘要、重导自动跳过、启用解封
try:
    import tempfile
    from pathlib import Path as _Path
    with tempfile.TemporaryDirectory() as _td:
        _bid = pool_admin.blacklist_id(_tk)
        ok('剔除名单：只存摘要不存明文',
           _bid != _tk and len(_bid) == 24 and pool_admin.load_blacklist(_td) == set())
        _n = pool_admin.blacklist_add(_td, [_tk, _tk2, _tk])
        ok('剔除名单：停用 / 删除后入名单且去重',
           _n == 2 and len(pool_admin.load_blacklist(_td)) == 2, f'added={_n}')
        _ft, _fi, _fb = pool_admin.filter_blacklisted(
            [_tk, _tk2], [{'access_token': _tk}], pool_admin.load_blacklist(_td))
        ok('剔除名单：重导时自动跳过',
           _ft == [] and _fi == [] and _fb == 3, f'skipped={_fb}')
        _raw = (_Path(_td) / pool_admin.BLACKLIST_FILE).read_text(encoding='utf-8')
        ok('剔除名单：磁盘上无明文 token', _tk not in _raw and _tk2 not in _raw)
        _n2 = pool_admin.blacklist_remove(_td, [_tk])
        ok('剔除名单：启用账号即解封',
           _n2 == 1 and len(pool_admin.load_blacklist(_td)) == 1, f'removed={_n2}')
except Exception as e:
    ok('剔除名单可用', False, str(e)[:120])

# 额度为 0 的号不再算「可用」
try:
    _rows = [{'status': '正常', 'quota': 0, 'cls': 'ok'},
             {'status': '正常', 'quota': 25, 'cls': 'ok'}]
    _o = {'active': 2, 'limited': 0, 'total': 2, 'quota': 25,
          'message': '2 个账号 · 可用 2 · 配额 25'}
    _z = pool_admin.normalize_zero_quota(_rows, _o)
    ok('面板：额度为 0 的号显示为限流且不计入可用',
       _z == 1 and _rows[0]['status'] == '限流' and _rows[1]['status'] == '正常'
       and _o['active'] == 1 and _o['limited'] == 1 and '可用 1' in _o['message'])
except Exception as e:
    ok('面板额度口径可用', False, str(e)[:120])

# 成品图路由：不裁切的比例直连原图，裁切的给裁好的 PNG
# 历史里混着「只生成文案」的 done 记录（image=None），拿它验图片路由必然 404，
# 所以先挑一条真正带图的记录，别被最新一条文案误报成回归。
try:
    _items = (client.get('/api/history').json() or {}).get('items') or []
    _with_img = [x for x in _items if x.get('image') or x.get('image_url')]
    if _with_img:
        _jid = _with_img[0]['id']
        r = client.get(f'/api/history/{_jid}/final')
        ok('GET /api/history/{id}/final', r.status_code == 200 and
           r.headers.get('content-type', '').startswith('image/'),
           f'HTTP {r.status_code} / {len(r.content)}B / 跳过 {len(_items)-len(_with_img)} 条无图记录')
    else:
        ok('历史里有可用记录', False, '没有带图记录，跳过 final 路由')
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

# ---------- 接口配置历史 + 失效号自动清理 ----------
_src_admin = (ROOT / 'pool_admin.py').read_text(encoding='utf-8')
ok('后端有档案存取 helper', all(x in _src_app for x in (
    'def _upsert_profile', 'def _seed_profiles', 'PROFILE_KEYS', 'provider_profiles')))
ok('档案存地址 / Key / 号池目录三件套',
   '"pool_dir", "api_key"' in _src_app and '"api_base_url"' in _src_app)
ok('档案视图只回脱敏 Key', 'api_key_masked' in _src_app and 'api_key_set' in _src_app)
ok('档案三个接口齐全', all(x in _src_app for x in (
    '@app.get("/api/config/profiles")',
    '@app.post("/api/config/profiles/switch")',
    '@app.post("/api/config/profiles/delete")')))
ok('切换整套恢复并重探号池', 'await POOL.refresh_env_ids()' in _src_app
   and 'cfg["provider_profiles"] = _upsert_profile(cfg)' in _src_app)
ok('删当前在用的档案有保护', '正在用的这份不能删' in _src_app)
ok('保存前后各存一次档案（换池不丢旧 Key）',
   _src_app.count('cfg["provider_profiles"] = _upsert_profile(cfg)') >= 3)
ok('首次启用补种本机两个号池', 'LOCAL_POOL_PORTS.items()' in _src_app
   and '_read_pool_user_key(seed)' in _src_app)
ok('档案按端口校正 pool_dir', 'LOCAL_POOL_PORTS.get(port)' in _src_app)
ok('前端有历史下拉、删除入口与清理开关', all(x in _src_html for x in (
    'id="cfgProfile"', 'id="cfgProfDel"', 'id="pPurgeDead"', 'loadProfiles(')))
ok('切换请求带档案 id', '/api/config/profiles/switch' in _src_html
   and 'JSON.stringify({id})' in _src_html)

ok('失效号判定三项同时命中', 'def is_hard_dead' in _src_admin
   and '!= "异常"' in _src_admin and '!= 0' in _src_admin)
ok('临时故障词被排除（代理/超时/限流）',
   'TEMP_ERR_RE' in _src_admin and 'proxy|connect|timeout' in _src_admin)
ok('限流号不会被删', pool_admin.is_hard_dead(
   {'status': '限流', 'quota': 0, 'error': 'token invalidated'}) is False)
ok('代理故障不会被删', pool_admin.is_hard_dead(
   {'status': '异常', 'quota': 0, 'error': 'proxy connect timeout'}) is False)
ok('人工停用不会被删', pool_admin.is_hard_dead(
   {'status': '禁用', 'quota': 0, 'error': 'token expired'}) is False)
ok('硬失效号命中', pool_admin.is_hard_dead(
   {'status': '异常', 'quota': 0, 'error': 'token invalidated (/backend-api/me)'}) is True)
ok('全池同时失效有跳过保护', 'len(dead) >= len(rows)' in _src_admin)
ok('清理有节流与防重入', '_PURGE_BUSY' in _src_admin and '_PURGE_INTERVAL' in _src_admin)
ok('清理可关且默认开', 'def purge_enabled' in _src_admin
   and 'auto_purge_dead' in _src_app)
ok('删除会进剔除名单', 'blacklist_add(tgt["home"], tokens)' in _src_admin)
ok('面板里接了自动清理', 'await _auto_purge_dead(rows, tgt, cfg)' in _src_admin)

# ---- 配置档案页：左右分栏 + 四态 + 三个新接口 ----
ok('第一项是独立的工作台重命名（全局项）',
   'id="cfgRenameInput"' in _src_html and 'JSON.stringify({app_name:v})' in _src_html)
ok('第二项左右分栏且列表末尾固定新增项',
   'class="prof-split"' in _src_html and 'data-padd="1"' in _src_html
   and '＋ 新增接口' in _src_html)
ok('详情区分查看 / 编辑 / 新增 / 空四态',
   all(x in _src_html for x in ("pChip('view')", "pChip('edit')",
                                "pChip('create')", "pChip('empty')")))
ok('未选中时是空态且不提供保存',
   '还没有选中号池' in _src_html and '未选中时不提供保存' in _src_html)
ok('新增必填三项、地址格式与唯一性校验齐全',
   all(x in _src_html for x in ('请填名称（用于列表展示）',
       '需以 http:// 或 https:// 开头', '新增接口必须填 API Key', '该地址已有档案')))
ok('保存失败保留表单与已填内容', '改动仍留在表单里' in _src_html)
ok('档案保存走新增 / 编辑独立接口', "'POST':'PATCH'" in _src_html)
ok('老全局控件收进折叠区且没被删',
   all(x in _src_html for x in ('id="cfgBase"', 'id="cfgWxGateway"',
       'id="cfgSave"', '<details class="cfg-more">')))
ok('折叠区默认收起', '<details class="cfg-more" open' not in _src_html)
ok('后端补齐单档案详情 / 新增 / 编辑三接口',
   all(x in _src_app for x in ('@app.get("/api/config/profiles/{pid}")',
       '@app.post("/api/config/profiles")', '@app.patch("/api/config/profiles/{pid}")')))
ok('详情接口只回脱敏 Key', 'def _profile_detail' in _src_app
   and 'masked = (key[:6]' in _src_app and 'api_key_masked' in _src_app)
ok('新增档案不等于启用（不重探、不冒充最近使用）',
   'x["last_used"] = 0' in _src_app and 'api_config_profile_create' in _src_app)
ok('编辑激活项才同步顶层配置并重探号池',
   'was_active = pid == _active_profile_id(cfg)' in _src_app
   and 'if was_active:' in _src_app and 'await POOL.refresh_env_ids()' in _src_app)
ok('编辑时留空 API Key 沿用、不清空',
   'if b.get("api_key"):' in _src_app and '留空 = 沿用' in _src_app)
ok('重复地址 / 重名返回 409 且激活档案禁删',
   'raise HTTPException(409' in _src_app and '正在用的这份不能删' in _src_app)

# 状态栏「生成中 · X」不能再把账号占位写成接口类型（用户曾因此以为写文章走了生图接口）
ok('任务状态占位按模式区分且中性',
   'label = "出图任务" if job["mode"] == "image" else "文案任务"' in _src_pool
   and 'id": 0, "masked": "当前接口"' in _src_pool)
ok('endpoint / pool_stat / provider_cfg 默认名都不再叫生图接口',
   _src_pool.count('"生图接口"') == 0
   and '"当前接口"' in _src_pool)
ok('号池 chat 日志无 account_email 的已知限制已写明',
   '没有 account_email' in _src_pool and '无法归因到具体账号' in _src_pool)
ok('号池未就绪报错不再写「生图号池 / 再出图」',
   '尚未配置生图号池' not in _src_app and '后再出图' not in _src_app
   and '尚未配置接口' in _src_app)
ok('档案名兼容新旧两版默认名',
   'n not in ("生图接口", "当前接口")' in _src_app)
ok('配置页签改为「接口配置」且不再出现「出图接口」',
   'data-tab="iface" role="tab">接口配置<' in _src_html
   and '>出图接口<' not in _src_html)
ok('完成态只跟真实脱敏账号，占位不当账号显示',
   "const who = /@/.test(d.account || '') ? d.account : ''" in _src_html
   and "statusEl.textContent = '完成' + (who ? ' · ' + who : '')" in _src_html)

# ---------- 配置页去重：折叠区只留全局项，接口参数归第 2 项档案区 ----------
import re as _re
_m = _re.search(r'function cfgPayload\(\)\{(.*?)\n\}', _src_html, _re.S)
_cfg_body = _m.group(1) if _m else ''
ok('旧接口表单保留 DOM 与全部 id 但整体隐藏',
   '<div id="cfgLegacyIface" style="display:none"' in _src_html
   and all(f'id="{x}"' in _src_html for x in
           ('cfgBase', 'cfgKey', 'cfgAppName', 'cfgModelImage', 'cfgEdits', 'cfgFormat', 'cfgEditsStyle')))
ok('「其他设置」标题不再写「当前接口参数」',
   '<summary>其他设置：图片保存目录 / 公众号推送</summary>' in _src_html
   and '其他设置：当前接口参数' not in _src_html)
ok('折叠区说明接口参数已上移到档案区', '这里不再重复放一套' in _src_html)
ok('图片目录与公众号推送仍在隐藏容器之外（全局项可见）',
   _re.search(r'</div>\s*<div class="span2"[^>]*>\s*<div[^>]*>生成图片保存目录', _src_html) is not None
   and 'id="cfgWxGateway"' in _src_html)
ok('底部保存只提交全局项，不再回写接口配置',
   bool(_cfg_body)
   and all(k in _cfg_body for k in ('output_dir', 'wx_gateway', 'wx_token', 'wx_push_mode'))
   and not any(k in _cfg_body for k in
               ('base_url', 'api_key', 'app_name', 'model_image', 'supports_edits',
                'edits_style', 'image_response_format')))
ok('试连改测当前生效配置（空 body），不依赖隐藏表单',
   "body: '{}'" in _src_html and '正在测试当前接口' in _src_html)
ok('底部按钮语义改为「保存全局设置 / 测试当前接口」',
   '>保存全局设置</button>' in _src_html and '>测试当前接口</button>' in _src_html)
ok('出图行为三项搬进档案编辑/新增表单',
   all(s in _src_html for s in (
       "pSelect('支持参考图','supports_edits'",
       "pSelect('返回格式','image_response_format'",
       "pSelect('参考图方式','edits_style'",
       'data-pf="${name}"'))
   and 'function pSelect(' in _src_html and 'pForm(pEmptyForm(), true)' in _src_html)
_b = _re.search(r'function pBody\(\)\{(.*?)\n\}', _src_html, _re.S)
_pbody = _b.group(1) if _b else ''
ok('pBody 不再硬编码 true/auto/auto 覆盖用户设置',
   bool(_pbody) and 'supports_edits: se !==' in _pbody
   and 'supports_edits:true' not in _pbody
   and "edits_style: pVal('edits_style')" in _pbody
   and "image_response_format: pVal('image_response_format')" in _pbody)
ok('select 改动也标脏，未保存离开会拦截',
   'i.onchange = ()=>{ PROF.dirty = true; }' in _src_html)
ok('查看态补上「参考图方式」只读行', "row('参考图方式'" in _src_html)

# ---------- 编辑态紧凑 + 出图/文案模型下拉 ----------
ok('紧凑只作用在编辑/新增态，且清掉全局 label 的 10px 顶部空白',
   all(s in _src_html for s in (
       'function pDense(on)', 'pDense(dense)', '.prof-split.dense{min-height:0}',
       '.prof-split.dense .prof-f label{margin:0 0 2px',
       '.prof-f label{display:block;font-size:11px;color:var(--ink-3);margin:0 0 3px}',
       "PROF.mode === 'create' || PROF.mode === 'edit'")))
ok('两个模型字段由手填改为下拉，仍保留手动输入兜底',
   all(s in _src_html for s in ("pModelField('出图模型'", "pModelField('文案模型'",
       'select data-pm="${name}"', '手动输入…', "const PM_CUSTOM = '__custom__'"))
   and "pField('出图模型'" not in _src_html and "pField('文案模型'" not in _src_html)
ok('下拉候选走后端模型接口，切态作废旧请求',
   all(s in _src_html for s in ("fetch('/api/config/models'", 'PM_TOKEN++',
       'if (token !== PM_TOKEN) return;', 'id="pmReload"'))
   and all(s in _src_app for s in ('@app.post("/api/config/models")',
       'await test_connection(ov)', '_is_image_model', '先填接口地址再读取模型')))
_model_fn = _re.search(r'async def api_config_models\(body: dict\):(.*?)\n@app\.post', _src_app, _re.S)
_model_body = _model_fn.group(1) if _model_fn else ''
ok('模型候选接口只在后端补 Key，不回明文',
   bool(_model_body) and 'str(v.get("api_key")' in _model_body
   and '"api_key":' not in _model_body and 'api_key_masked' not in _model_body)
ok('pVal 同时读下拉与手动输入，pBody 无需改动',
   'select[data-pm="${n}"]' in _src_html and 'input[data-pfi="${n}"]' in _src_html
   and "model_image:pVal('model_image')" in _src_html)
_r = client.post('/api/config/models', json={'base_url': ''})
_d = _r.json() if _r.status_code == 200 else {}
ok('没填地址时不去探当前在用的接口',
   _r.status_code == 200 and _d.get('source') == 'fallback'
   and '先填接口地址' in str(_d.get('message') or ''))
_rm = client.post('/api/config/models', json={})
_dm = _rm.json() if _rm.status_code == 200 else {}
ok('模型接口恒有 image_extra 字段', 'image_extra' in _dm, sorted(_dm.keys()))
ok('只列一个出图模型时补常用候选，前端分组标注来源',
   all(s in _src_app for s in ('"image_extra": image_extra', '"image_extra": []'))
   and all(s in _src_html for s in ('function pmGroups(', '接口提供 · 可直接用',
                                    '常用候选 · 接口未列出，需上游支持',
                                    '接口列出 ${PM_OPTS.image.length} 个出图模型')))
ok('重拉候选保留手动输入值与 custom 状态',
   'const wasCustom = s.value === PM_CUSTOM' in _src_html
   and 'inp.value = typed' in _src_html)

# ---- 推荐话题：知乎 / 小红书讨论方向加工（1.1.36）----
ok('GET /api/topics/recommend 已注册',
   any(getattr(r, 'path', '') == '/api/topics/recommend' for r in app.app.routes))
_rt = client.get('/api/topics/recommend')
_rd = _rt.json() if _rt.status_code == 200 else {}
_ritems = _rd.get('items') or []
ok('推荐话题接口返回成批可用选题',
   _rt.status_code == 200 and len(_ritems) >= 24
   and all(x.get('text') and x.get('tag') and x.get('why') for x in _ritems),
   '共 %d 条，来源 %s' % (len(_ritems), _rd.get('source')))
ok('话题池每条都过干货闸门', all(app._topic_ok(x['text']) for x in _ritems))
ok('话题池不含毒鸡汤空词',
   not any(app._TOPIC_BAD.search(x['text']) for x in _ritems))
ok('话题池不出现「你怎么看」式空问句',
   not any('你怎么看' in x['text'] for x in _ritems))
ok('鸡汤句被闸门拒绝', not app._topic_ok('只要努力坚持，人生一定会变好'))
ok('具体可验证的选题被接受',
   app._topic_ok('把 200 页 PDF 先标页码再问 AI，答案能翻回原文核对吗'))
ok('同一热点稳定落在同一个讨论角度',
   app._topic_angle('AI 工具进入团队周报之后怎么核数字')
   == app._topic_angle('AI 工具进入团队周报之后怎么核数字'))
_g = app._topics_from_titles(
    ['AI 工具进入团队周报之后怎么核数字',
     'AI 工具进入团队周报之后怎么核数字',
     '震惊！一定要看完的 3 个秘密'], '知乎热榜')
ok('热榜加工去重并跳过情绪标题',
   len(_g) == 1 and _g[0]['tag'] == '知乎热榜' and bool(_g[0]['why']), len(_g))
ok('热榜返回壳兼容 items / data / 数组',
   app._hot_titles_from_json({'items': [{'title': '甲'}]}) == ['甲']
   and app._hot_titles_from_json({'data': [{'title': '乙'}]}) == ['乙']
   and app._hot_titles_from_json(['丙']) == ['丙'])
_mix = app._rec_interleave([
    [{'text': 'a1'}, {'text': 'a2'}], [{'text': 'b1'}],
    [{'text': 'c1'}, {'text': 'c2'}]])
ok('三层话题交错，前三条来自不同层',
   [x['text'] for x in _mix[:3]] == ['a1', 'b1', 'c1'])
ok('常青题库够翻十批以上', len(app._evergreen_topics()) >= 24,
   len(app._evergreen_topics()))
ok('推荐话题卡片插在热点榜与选题框之间',
   all(x in _src_html for x in ('id="recTopics"', 'id="recList"', 'id="recNext"'))
   and 0 < _src_html.find('id="aihotList"') < _src_html.find('id="recTopics"')
        < _src_html.find('label for="topic"'))
ok('每批固定三条、翻批只走本地洗牌',
   'const PAGE = 3' in _src_html and 'function shuffle(' in _src_html
   and 'if(pool.length) render(); else load(1)' in _src_html)
ok('点选推荐话题填进选题框并断开旧热点综述',
   all(x in _src_html for x in ('function pick(it)', "t.value = String(it.text || '')",
                                'state.hotStory = null', "bindDraft(null, '')",
                                'saveSession()')))
ok('来源标记已移除，卡片不再显示实时/常青来源标注',
   'id="recSource"' not in _src_html
   and 'function sourceLabel' not in _src_html
   and 'id="recPage"' not in _src_html)

# ---- 多平台热点榜弹窗（1.1.37）----
ok('GET /api/topics/platforms 已注册',
   any(getattr(r, 'path', '') == '/api/topics/platforms' for r in app.app.routes))
_cp = client.get('/api/topics/platforms')
_cd = _cp.json() if _cp.status_code == 200 else {}
_cps = _cd.get('platforms') or []
ok('覆盖七个平台且每个页签都不空',
   _cp.status_code == 200 and len(_cps) == len(app._HB_PLATFORMS)
   and all(p.get('key') and p.get('name')
           and p.get('status') in ('live', 'curated')
           and p.get('items') and p.get('count') == len(p['items']) for p in _cps),
   '%d 个平台' % len(_cps))
ok('热点条目保留原标题并带可填进选题框的文本',
   all(x.get('title') and x.get('text') and x.get('why')
       for p in _cps for x in p['items']))
ok('加工题不编造热度和链接',
   all(not x.get('hot') and not x.get('url')
       for p in _cps for x in p['items'] if x['status'] == 'curated'))
ok('热榜返回壳兼容 NewsNow / 60s / 纯数组',
   app._hot_board_rows({'items': [{'title': '甲', 'url': 'u'}]})[0]['title'] == '甲'
   and app._hot_board_rows({'data': [{'title': '乙', 'hot_value': 11126038, 'link': 'l'}]})[0]['hot'] == '1112.6 万'
   and app._hot_board_rows(['丙'])[0]['title'] == '丙')
ok('热度值换算成万 / 亿',
   app._hb_hot(11126038) == '1112.6 万' and app._hb_hot('') == ''
   and app._hb_hot('12 万') == '12 万')
ok('缺源平台的加工题同样过干货闸门',
   all(app._topic_ok(x['text'])
       for k in ('xhs', 'kuaishou') for x in app._hb_curated(k, ''))
   and all(not app._TOPIC_BAD.search(x['text'])
           for k in ('xhs', 'kuaishou') for x in app._hb_curated(k, '')))
ok('弹窗角度通用，不把球赛油价套进 AI 办公话术',
   all(x not in ''.join(a[0] for a in app._HB_ANGLES)
       for x in ('团队', 'SOP', '重复劳动', '周报'))
   and '油价下调' in app._hb_angle('油价下调 加满一箱油少花约26元')[0]
   and app._topic_ok(app._hb_angle('油价下调 加满一箱油少花约26元')[0]))
ok('情绪化热榜标题只展示原题不加工',
   app._hb_angle('震惊！一定要看完的 3 个秘密') is None)
ok('可接自定义热榜源',
   'TOPIC_HOT_API_' in _src_app and 'custom' in _src_app)
ok('底部入口能打开多平台热点弹窗',
   all(x in _src_html for x in ('id="recOpen"', 'recOpen.onclick = hbOpen')))
ok('弹窗含平台页签、状态说明与两列卡片',
   all(x in _src_html for x in ('id="hotBoardDlg"', 'id="hbTabs"', 'id="hbNote"',
                                'id="hbList"', 'hb-grid', '.hb-dot.off')))
ok('切页签本地渲染，只有打开和刷新才打接口',
   all(x in _src_html for x in ("fetch('/api/topics/platforms'", 'hbLoad(0)',
                                'hbLoad(1)', 'if(!hbData) hbLoad(0)')))
ok('点条目填进选题并复用原有 pick 逻辑',
   'pick({text: val})' in _src_html
   and 'if(hbDlg && hbDlg.open) hbDlg.close()' in _src_html)
ok('界面把加工题和实时榜区分开',
   '平台向 · 加工题' in _src_html and '不冒充实时' in _src_app)

# ---- 图文首图下的「内容要点」----
print('=== 图文首图内容要点 ===')
_lead_raw = '["· 英伟达内网投票 97% 员工支持恢复黄仁勋职务","· 《硅谷》报道内部评论超 1600 条","· 两种表达指向同一问题"]'
ok('要点 JSON 解析并统一加「· 」前缀',
   app._wx_parse_points(_lead_raw) == [
       '· 英伟达内网投票 97% 员工支持恢复黄仁勋职务',
       '· 《硅谷》报道内部评论超 1600 条',
       '· 两种表达指向同一问题'], str(app._wx_parse_points(_lead_raw)))
ok('AI 返回带代码块也能解析',
   len(app._wx_parse_points('```json\n' + _lead_raw + '\n```')) == 3)
_lead_screens = [{"title": "97%员工支持！",
                  "caption": "一场来自英伟达内部的员工声援，正在引发关注。据内网科技报道，员工发起的内网投票中，超过97%的投票者希望恢复英伟达解信首席执行官。"}]
ok('AI 不可用兜底从正文第一句提炼要点',
   app._wx_lead_fallback(_lead_screens)[0].startswith('· 一场来自英伟达'),
   str(app._wx_lead_fallback(_lead_screens)))
ok('要点区块带「内容要点」标题且不含图片',
   '内容要点' in app._wx_lead_block(['· 要点一']) and '<img' not in app._wx_lead_block(['· 要点一']))
ok('首图区块能把要点插在图片之后',
   app._wx_news_section('标题', '正文', 'http://x/y.jpg',
                        tail_html=app._wx_lead_block(['· 要点一'])).count('内容要点') == 1)
ok('普通屏不带要点区块',
   app._wx_news_section('标题', '正文', 'http://x/y.jpg').count('内容要点') == 0)

client.close()
print(f'\n结果：{passed} 通过 / {failed} 失败')
sys.exit(0 if failed == 0 else 1)
