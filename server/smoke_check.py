"""本地生图工作台冒烟：符号断言 + 全量读接口 + 一次真实端到端出图。

用法：
  .venv/Scripts/python.exe smoke_check.py           # 全量（含真实出图，约 1 分钟）
  .venv/Scripts/python.exe smoke_check.py --quick   # 只跑符号与读接口
"""
from __future__ import annotations

import asyncio, json, pathlib, sys, time
from datetime import datetime
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
import app, pool, pool_admin, pool_service, composer, history, imaging, catalog, access, drafts, novel_overlay
import hot_facts
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
# 多频道：四个频道 + 两个新路由，菜单/瘦身逻辑别在升级中被碰丢
ok('GET /api/aihot/channels 已注册',
   any(getattr(r, 'path', '') == '/api/aihot/channels'
       and 'GET' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('GET /api/aihot/feed 已注册',
   any(getattr(r, 'path', '') == '/api/aihot/feed'
       and 'GET' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('GET /api/aihot/daily 已注册',
   any(getattr(r, 'path', '') == '/api/aihot/daily'
       and 'GET' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('GET /api/history/pick 已注册',
   any(getattr(r, 'path', '') == '/api/history/pick'
       and 'GET' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('POST /api/tasks/{task_id}/fill 已注册',
   any(getattr(r, 'path', '') == '/api/tasks/{task_id}/fill'
       and 'POST' in getattr(r, 'methods', set()) for r in app.app.routes))
_route_paths = [getattr(r, 'path', '') for r in app.app.routes]
ok('换图候选接口必须排在 /api/history/{job_id} 之前',
   _route_paths.index('/api/history/pick') < _route_paths.index('/api/history/{job_id}'),
   '顺序反了会把 pick 当成 job_id，候选列表永远 404')
ok('DELETE /api/history/{job_id} 已注册（删单张）',
   any(getattr(r, 'path', '') == '/api/history/{job_id}'
       and 'DELETE' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('AIHOT 四频道齐全（热点榜/精选/全部动态/AI 日报）',
   [c['key'] for c in app.AIHOT_CHANNELS] == ['hot', 'selected', 'all', 'daily']
   and all(len(c['cats']) == 7 for c in app.AIHOT_CHANNELS if c['key'] in ('selected', 'all')))
_tmp_feed = app._aihot_slim_feed({"items": [
    {"title": "模型甲", "category": "ai-models", "summary": "摘要一",
     "source": {"name": "信源甲"}, "links": {"aihot": "https://aihot.news/items/m1"}},
    {"title": "   "}, "bad",
]}, "selected")
ok('feed 瘦身：精选保留分类、丢空标题与非字典',
   len(_tmp_feed) == 1 and _tmp_feed[0]['category'] == 'ai-models'
   and _tmp_feed[0]['title'] == '模型甲' and _tmp_feed[0]['source'] == '信源甲')
ok('feed 瘦身：日报取 leadTitle、坏输入不抛异常',
   app._aihot_slim_feed({"items": [{"leadTitle": "今日日报", "date": "2026-11-14",
                                    "leadParagraph": "昨天发生了这些"}]}, "daily")[0]['title'] == '今日日报'
   and app._aihot_slim_feed({}, "all") == [])
# 单日日报明细：SSR 解析必须头条 + 明细都拿到，非法日期不抓（离线内联 HTML，不依赖网络）
ok('日报明细抓取：非法日期直接返回空',
   app._aihot_daily_scrape('') == [] and app._aihot_daily_scrape('not-a-date') == [])
_daily_html = '''
<script type="application/ld+json">{"@type":"NewsArticle",
 "headline":"AI 日报｜11月14日：今日头条事件","description":"11月14日 AI 新闻：头条摘要内容"}</script>
<article id="r-aaa111"><span class="truncate">信源甲</span>
 <h3><a href="/items/aaa111">明细标题甲</a></h3>
 <p class="mt-2 line-clamp-4 text-ink-2">明细摘要甲</p>
 <a href="https://example.com/a" aria-label="阅读信源甲原文：明细标题甲（新标签页）">原文</a></article>
<article id="r-bbb222"><span class="truncate">信源乙</span>
 <h3><a href="/items/bbb222">明细标题乙</a></h3>
 <p class="mt-2 line-clamp-4">明细摘要乙</p></article>
'''
_daily_parsed = app._aihot_daily_parse(_daily_html, '2026-11-14')
ok('日报解析：头条 + 2 条明细都抽到',
   len(_daily_parsed) == 3
   and _daily_parsed[0]['title'] == '今日头条事件'
   and _daily_parsed[0]['digest'] == '头条摘要内容')
ok('日报解析：明细字段齐全且原文/分类来源正确',
   _daily_parsed[1]['title'] == '明细标题甲'
   and _daily_parsed[1]['source'] == '信源甲'
   and _daily_parsed[1]['digest'] == '明细摘要甲'
   and _daily_parsed[1]['aihot'] == 'https://aihot.news/items/aaa111'
   and _daily_parsed[1]['original'] == 'https://example.com/a'
   and _daily_parsed[2]['original'] == '')
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
# 界面上不再出现平台名（用户 2026-11-26：内容工坊产出的是图文笔记，不绑定渠道）
ok('内容工坊与预览条不再标小红书',
   'id="modePill">图文笔记' in _src_html
   and '小红书笔记' not in _src_html
   and '小红书 · 预览' not in _src_html
   and "'图文 · 预览'" in _src_html)
ok('前端停止链路完整',
   all(x in _src_html for x in ('async function stopAllJobs', '/api/generate/stop',
                                'clearBatch();', "d.status === 'cancelled'")))
# AI 热点选题：卡片必须真的落在「选题」输入框上方，节点顺序错了会跑到下面去
ok('前端 AI 热点选题节点齐全',
   all(x in _src_html for x in ('id="aihotList"', 'id="aihotDlg"', 'id="aihotDList"',
                                'id="aihotMore"', 'id="aihotRefresh"')))
ok('前端热点两级 tab 节点与切换逻辑齐全',
   'id="ahTabs"' in _src_html and 'id="ahCats"' in _src_html
   and all(x in _src_html for x in ('function renderTabs', 'function selectChannel',
                                    'function selectCat', '/api/aihot/channels',
                                    "'/api/aihot/feed?channel='")))
ok('前端左栏热点数据与弹窗频道解耦（hotItems 已声明）',
   'let hotItems = []' in _src_html and 'hotItems.slice(0, 3)' in _src_html
   and 'items = hotItems' in _src_html)
ok('前端跨批次换图节点齐全',
   all(x in _src_html for x in ('id="pickDlg"', 'id="pickList"', 'id="pickConfirm"',
                                'id="pickFilter"', 'id="pickSelText"', 'data-act="fill"',
                                'function openPick', 'function confirmFill',
                                '/api/history/pick')))
ok('换图后只重建单个任务行，不整页重载',
   'function taskRowEl' in _src_html and 'function replaceTaskRow' in _src_html
   and 'row.dataset.taskId' in _src_html and 'old.replaceWith(el)' in _src_html)
ok('前端历史卡片有单张删除入口',
   'data-act="del"' in _src_html and 'function deleteHistoryOne' in _src_html
   and '/api/history/' in _src_html)
ok('删单张后同步头部计数，删空则移除任务行',
   'function removeFromTask' in _src_html and 'replaceTaskRow(task)' in _src_html
   and 'row.remove()' in _src_html)
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
ok('preview 与 generate 都接了资料取值', _src_app.count('await _text_facts(req)') == 2)


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

# ---- 每日热榜取证：热榜一行只有标题，别让模型凭标题编日期、数字和名单 ----
ok('取证模块提供原文抓取 / 检索 / 降级三级能力',
   hasattr(hot_facts, 'build') and hasattr(hot_facts, 'filter_snippets')
   and hasattr(hot_facts, 'parse_rss') and hasattr(hot_facts, 'cached'))

_RSS_SAMPLE = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>某公司发布新模型 X2</title>
  <description>11月26日，某公司在北京发布新模型 X2，参数规模 3200 亿，并宣布下月开放接口，同时公布了推理成本与首批合作方名单。</description>
  <pubDate>Wed, 26 Nov 2026 09:00:00 GMT</pubDate><link>https://news.example.com/a/1</link></item>
<item><title>百度一下，你就知道</title>
  <description>全球领先的中文搜索引擎、致力于让网民更便捷地获取信息，找到所求。</description>
  <link>https://www.baidu.com/</link></item>
<item><title>太短的摘要</title><description>太短</description>
  <link>https://short.example.org/p/1</link></item>
<item><title>某公司新模型 X2 细节曝光</title>
  <description>某公司新模型 X2 的细节曝光，参数规模 3200 亿，训练投入 2 万张卡，团队来自多个实验室。</description>
  <pubDate>Tue, 25 Nov 2026 18:00:00 GMT</pubDate><link>https://tech.example.org/a/2</link></item>
</channel></rss>"""
_rows = hot_facts.filter_snippets(hot_facts.parse_rss(_RSS_SAMPLE), '某公司发布新模型 X2')
ok('检索结果去掉门户首页与短摘要，同一站点只留一条',
   len(_rows) == 2 and all('baidu.com' not in r['link'] for r in _rows)
   and len({r['host'] for r in _rows}) == 2,
   str([(r['host'], r['title'][:12]) for r in _rows]))
ok('跟标题不沾边的检索结果一律丢掉，宁可没资料也不喂垃圾',
   hot_facts.filter_snippets(hot_facts.parse_rss(_RSS_SAMPLE), '南方多地暴雨预警升级') == [])
ok('检索结果带站点名与日期，来源看得见',
   _rows[0]['site'] == 'example' and _rows[0]['date'] == '2026-11-26',
   str((_rows[0]['site'], _rows[0]['date'])))
_pack = hot_facts._pack_snippets(_rows, '某公司发布新模型 X2')
ok('多个来源拼成带编号的资料块，并留来源清单',
   bool(_pack) and '[1]' in _pack['digest'] and len(_pack['sources']) == 2,
   str(_pack and _pack['chars']))
ok('同一标题的缓存键稳定，换了站点就算另一条',
   hot_facts.key_of('某公司发布新模型 X2', 'https://a.example.com/1')
   == hot_facts.key_of('某公司发布新模型 X2', 'https://a.example.com/1?x=2')
   and hot_facts.key_of('某公司发布新模型 X2', 'https://a.example.com/1')
   != hot_facts.key_of('某公司发布新模型 X2', 'https://b.example.com/1'))

_hb_story = {'title': '某公司发布新模型 X2', 'digest': _pack['digest'],
             'kind': 'search', 'sources': _pack['sources']}
_hb_p = app.build_text_prompt(
    app.GenRequest(mode='text', topic='某公司发布新模型 X2', num_items=7), _hb_story)
ok('热榜检索资料进提示词，标明多来源并列出出处',
   '公开新闻报道摘要' in _hb_p and '资料来源：' in _hb_p and '[1]' in _hb_p)
ok('热榜资料守同一条事实红线', '事实红线' in _hb_p)
_deg_p = app.build_text_prompt(
    app.GenRequest(mode='text', topic='某公司发布新模型 X2', num_items=7),
    {'title': '某公司发布新模型 X2', 'digest': '', 'degraded': True})
ok('一条资料都取不到时明确降级：只写框架，不许编日期和数字',
   '没有检索到可用的公开资料' in _deg_p and '不写具体日期' in _deg_p
   and '事实红线' in _deg_p)
ok('降级块不会混进一份空综述', '综述：' not in _deg_p)

ok('GenRequest 收热榜取证字段，默认都不带',
   all(getattr(app.GenRequest(mode='text', topic='t'), k) is None
       for k in ('hot_id', 'hot_title', 'hot_url', 'hot_platform')))
ok('后端提供 /api/hot/facts，并把热榜资料接进写稿',
   '"/api/hot/facts"' in _src_app and 'async def _hot_story' in _src_app
   and _src_app.count('await _text_facts(req)') == 2)


async def _probe_hot_story():
    orig = hot_facts.build

    async def _fake(title, url='', platform='', timeout=12.0):
        return {'level': 'search', 'title': title,
                'digest': '检索摘要', 'sources': [], 'chars': 4}

    hot_facts.build = _fake
    try:
        hit = await app._hot_story(app.GenRequest(
            mode='text', topic='甲事件', hot_title='甲事件', hot_url='https://a/1'))
        mism = await app._hot_story(app.GenRequest(
            mode='text', topic='乙事件', hot_title='甲事件', hot_url='https://a/1'))
        bare = await app._hot_story(app.GenRequest(mode='text', topic='乙事件'))
        img = await app._hot_story(app.GenRequest(
            mode='image', topic='甲事件', hot_title='甲事件'))
    finally:
        hot_facts.build = orig
    return hit, mism, bare, img


_h1, _h2, _h3, _h4 = asyncio.run(_probe_hot_story())
ok('热榜选题按标题+链接取回资料，并标成检索来源', _h1 and _h1['kind'] == 'search'
   and _h1['digest'] == '检索摘要')
ok('选题文字被改过就不再挂原热点的资料', _h2 is None)
ok('没带热榜字段的普通选题不触发取证', _h3 is None)
ok('出图模式不跑热榜取证', _h4 is None)

ok('前端点热榜「用作选题」就顺手取证：按钮变「取资料…」、结果进 state.hotRef',
   all(x in _src_html for x in ('applyHotTopic', "btn.textContent = '取资料…'",
                                'state.hotRef = ', "'/api/hot/facts'")),
   '漏了取证，点选题只会填一句标题')
ok('payload 只在标题没被改过时带热榜资料，生成前等取证回来',
   'hot_id: hotOk ? hr.id : undefined' in _src_html
   and 'text === hr.title' in _src_html
   and 'await hotFactsPending' in _src_html,
   '漏了比对，改了选题还会挂原事件资料')
ok('取证结果进会话快照，载入草稿不继承',
   'snap.hot_ref = state.hotRef' in _src_html and 'd.hot_ref' in _src_html
   and 'state.hotRef = null' in _src_html)
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

# 线稿类画风不能套「暖调写实生活摄影」的融合措辞：用户选了小林漫画，出的却是暖调写实插画
_cat_styles = {s['id']: s for s in catalog.build()['styles']}
_ink_style = _cat_styles.get('FE-060')      # 小林漫画：白底、毛笔线稿、大面积留白
_photo_style = _cat_styles.get('FI-015')    # 黑白肖像摄影：features 里有「黑白」但不是线稿
ok('线稿类画风被识别出来（小林漫画 / 极简线描）',
   composer.is_ink_style(_ink_style) is True
   and composer.is_ink_style(_cat_styles.get('FA-002')) is True
   and composer.is_ink_style(None) is False)
ok('摄影组的「黑白」不会被误判成线稿（FI-015 是黑白照片）',
   composer.is_ink_style(_photo_style) is False
   and composer.is_ink_style(_cat_styles.get('FE-001')) is False)
_ink_prompt = composer.build_prompt(
    {'title': '有很多事当时你想不通', 'body': '', 'scene': '一个微缩尺寸的小人和一本旧书'},
    {'style': _ink_style, 'layout': _L[0], 'color': None},
    '3:4', ['治愈'], True, 'normal', '', False, 'graphic-text', '')
ok('线稿风格 + 缩微融合：不把场景改写成暖调写实摄影',
   composer.MICRO_FUSION_INK in _ink_prompt
   and '暖调写实生活摄影' not in _ink_prompt
   and '画风以所选风格为准' in _ink_prompt)
ok('摄影 / 无风格时仍走原来的写实摄影融合',
   composer.MICRO_FUSION in composer.build_prompt(
       {'title': 't', 'body': '', 'scene': '小人和旧书'},
       {'style': _photo_style}, '3:4', [], True, 'normal', '', False, 'graphic-text', '')
   and composer.MICRO_FUSION in composer.build_prompt(
       {'title': 't', 'body': '', 'scene': '小人和旧书'},
       {'style': None}, '3:4', [], True, 'normal', '', False, 'graphic-text', ''))

# 图文卡的另一半毛病（用户 2026-11-26）：图型写着「纯色背景、留白」，融合句却要求暖调实景摄影，
# 两条要求打架，出来的不是卡片而是一张桌面实拍；而且「上方是角色或场景图」只说了位置、没说画什么，
# 挑好的画面内容被丢掉，模型只能自己编。
_cat_layouts = {l['id']: l for l in catalog.build()['layouts']}
_flat_layout = _cat_layouts['SC-008']      # 图上文下留白卡
_three_layout = _cat_layouts['SC-006']     # 三层标题辅助图卡
_flat_prompt = composer.build_prompt(
    {'title': '', 'body': '有很多事你当时想不通，别着急，过段时间你可能就忘了。',
     'scene': '一个微缩尺寸的小人和一本日记本'},
    {'style': None, 'layout': _flat_layout, 'color': None},
    '3:4', ['治愈'], True, 'normal', '', False, 'graphic-text', '')
ok('纯色留白图型不再要求暖调实景摄影，改纯色底融合',
   composer.MICRO_FUSION_FLAT in _flat_prompt
   and '暖调写实生活摄影' not in _flat_prompt,
   '图型要留白、融合句要实景，两条打架')
ok('图文卡把挑好的画面内容写进提示词',
   '画面：一个微缩尺寸的小人和一本日记本' in _flat_prompt,
   '只写了图块位置没写画什么，模型只能自己编')
ok('没有画面内容时不硬拼一个空的「画面：」',
   '画面：' not in composer.build_prompt(
       {'title': 't', 'body': 'b', 'scene': ''},
       {'layout': _flat_layout}, '3:4', [], True, 'normal', '', False, 'graphic-text', ''))
ok('三层版式仍走「中层小辅助图」，不重复加画面行',
   '中层小辅助图' in composer.build_prompt(
       {'title': '', 'body': '副标题', 'scene': '小人和日记本'},
       {'layout': _three_layout}, '3:4', [], True, 'normal', '', False, 'graphic-text', '')
   and '画面：' not in composer.build_prompt(
       {'title': '', 'body': '副标题', 'scene': '小人和日记本'},
       {'layout': _three_layout}, '3:4', [], True, 'normal', '', False, 'graphic-text', ''))
ok('画面描述不再指代不存在的「上层标题」',
   '上层标题' not in composer._wrap_scene('一本旧书', True)
   and '上层标题' not in composer.SCENE_FALLBACK)

# 图文提示词的字段与顺序必须和官方技能包一致（用户 2026-11-26：同一图型同一主题，
# 工作台出的图明显不如官方技能包像小林漫画，逐条比对后发现差在风格特征被截断、
# 编号标签缺失、画幅情绪挤在最后一行、还硬塞了一句自动挑的道具画面）
_traits_full = composer.clean_traits(_ink_style.get('features', ''))
ok('风格特征不再截到 110 字（丢掉的红黄绿点涂正是小林漫画的辨识度）',
   '红黄绿局部平涂' in _traits_full and '写意人物画' in _traits_full,
   '特征被截断：' + _traits_full[-30:])
_order_prompt = composer.build_prompt(
    {'title': '有很多事你当时想不通', 'body': '', 'scene': ''},
    {'style': _ink_style, 'layout': _flat_layout, 'color': {'prompt': '主题色：奶油白（Cream White）。',
                                                            'name': '奶油白'}},
    '3:4', ['治愈', '松弛'], False, 'normal', '', False, 'graphic-text', '')
_heads = ['风格名称：', '参考作者/风格名称：', '主题色：', '画幅比例：', '排版要求：',
          '核心风格特征：', '情绪基调：']
_pos = [_order_prompt.find(h) for h in _heads]
ok('图文提示词按官方顺序排列：风格名称→参考作者→主题色→画幅→排版→核心特征→情绪',
   all(p >= 0 for p in _pos) and _pos == sorted(_pos), '字段位置：' + str(dict(zip(_heads, _pos))))
ok('图文提示词带上官方固定图文后缀（文案升华 + 先设计隐喻 + 文字参与构图）',
   composer.GRAPHIC_SUFFIX in _order_prompt and '文案由你来升华' in _order_prompt
   and '由你先设计画面隐喻' in _order_prompt
   and '文字清晰可读' not in _order_prompt)
ok('图文提示词里没有「禁止饮用容器」这一大段噪声（和风格、画面都无关）',
   '禁止出现任何饮用容器' not in _order_prompt)
_auto_scene_prompt = composer.build_prompt(
    {'title': '有很多事你当时想不通', 'body': '', 'scene': '一个涂鸦小人和一本日历'},
    {'style': _ink_style, 'layout': _flat_layout, 'color': None},
    '3:4', ['治愈'], False, 'normal', '', False, 'graphic-text', '', scene_manual=False)
ok('纯色留白卡不把系统自动挑的道具写进提示词（一句忘记的话被画成一页日历）',
   '日历' not in _auto_scene_prompt and '画面：' not in _auto_scene_prompt)
_manual_scene_prompt = composer.build_prompt(
    {'title': '有很多事你当时想不通', 'body': '', 'scene': '一个涂鸦小人和一本日历'},
    {'style': _ink_style, 'layout': _flat_layout, 'color': None},
    '3:4', ['治愈'], False, 'normal', '', False, 'graphic-text', '', scene_manual=True)
ok('用户手填的画面描述在纯色留白卡上照样写进提示词',
   '画面：一个涂鸦小人和一本日历' in _manual_scene_prompt)
_cat_colors = catalog.build()['colors']
_cid_pick = {c['id']: c for c in _cat_colors}
ok('没选主题色时，白底线稿 + 纯色留白卡自动补纸白（否则纯色底被铺成蓝底）',
   (composer.default_color(_ink_style, _flat_layout, _cat_colors) or {}).get('name') in ('奶油白', '象牙白', '米驼色')
   and composer.default_color(_photo_style, _flat_layout, _cat_colors) is None
   and composer.default_color(_ink_style, None, _cat_colors) is None)
_auto_c = asyncio.run(composer.compose(
    {'ratio': '3:4', 'layout_id': 'SC-008', 'style_id': 'FE-060',
     'items': [{'title': '有很多事你当时想不通，别着急，过段时间你可能就忘了。', 'body': ''}]},
    catalog.build()['styles'], catalog.build()['layouts'], _cat_colors))
ok('拼词接口会自动补主题色并标出 auto_color',
   _auto_c.get('auto_color') is True and '主题色：' in _auto_c['items'][0]['prompt']
   and _auto_c['items'][0]['scene'] == '',
   'scene 不该再自动挑道具：' + str(_auto_c['items'][0]['scene']))

_single_pure = asyncio.run(composer.compose(
    {'image_mode': 'pure-image', 'ratio': '3:4', 'style_id': 'FE-060',
     'items': [{'title': '一只橘猫', 'body': '趴在窗台晒太阳'}]},
    catalog.build()['styles'], catalog.build()['layouts'], _cat_colors))
ok('单图快出不自动配道具：一句主题交给模型，不白等十几秒挑道具',
   _single_pure['items'][0]['scene'] == ''
   and '一个涂鸦小人' not in _single_pure['items'][0]['prompt'],
   '仍在自动挑道具：' + str(_single_pure['items'][0]['scene']))

# 纯图模式（用户 2026-11-26）：只出画面、不排文字，也不套图型版式
_pure = composer.build_prompt({'title': '一只橘猫', 'body': '趴在窗台晒太阳'},
                              {'layout': _L[0]}, '3:4', [], False, 'normal', '', False, 'pure-image', '')
ok('纯图模式不套图型版式，也不进文字槽位',
   all(x not in _pure for x in ('三层版式', '主标题文字', '正文文字', '文字清晰可读', '上层主标题')))
ok('纯图模式明令不出字，标题正文只当画面内容',
   composer.PURE_NO_TEXT in _pure and '画面：一只橘猫' in _pure and '趴在窗台晒太阳' in _pure)
# 纯图也要官方字段结构（用户 2026-11-26：选了 SC-008 点优化提示词，拿到一段没头没尾的纯图文本）
_pure_real = composer.build_prompt(
    {'title': '', 'body': '有很多事你当时想不通，别着急，过段时间你可能就忘了。', 'scene': ''},
    {'style': next((s for s in catalog.build()['styles'] if s['id'] == 'FE-060'), None),
     'layout': _cat_layouts.get('SC-008'),
     'color': next((c for c in catalog.build()['colors'] if c['id'] == 'C-34'), None)},
    '3:4', ['治愈'], False, 'normal', ref=False, image_mode='pure-image')
ok('纯图提示词同样是官方字段结构，不再把风格名和特征揉进括号',
   all(x in _pure_real for x in ('风格名称：', '参考作者/风格名称：', '主题色：', '画幅比例：3:4。',
                                 '核心风格特征：', '情绪基调：')),
   '纯图提示词：' + _pure_real[:60])
ok('纯图不再出现「纯图模式。」这种模型读不懂的系统术语，也不写排版要求',
   '纯图模式' not in _pure_real and '排版要求' not in _pure_real)
# 只选图型不选风格：提示词里一个画风字都没有，出图只能靠模型自由发挥（用户 2026-11-26 踩坑）
_no_style = asyncio.run(composer.compose({'layout_id': 'SC-008', 'ratio': '3:4',
                                          'mood': '治愈、松弛、幽默', 'image_mode': 'graphic-text',
                                          'topic': '有很多事你当时想不通，别着急，过段时间你可能就忘了。'},
                                         [], catalog.build()['layouts'], catalog.build()['colors']))
ok('没选风格时明确标记 style_missing，前端据此自动补画风',
   _no_style.get('style_missing') is True and _no_style['selection']['style'] is None,
   'selection：' + str(_no_style.get('selection')))
ok('没选风格时提示词确实没有风格字段（不是拼词偶发丢失）',
   '风格名称：' not in _no_style['items'][0]['prompt']
   and '核心风格特征：' not in _no_style['items'][0]['prompt'])
ok('纯图/图文/摄影三种模式指令各自独立',
   '纯图模式' in composer.build_instruction({'layout': _L[0]}, '3:4', [], False, 'normal', False, 'pure-image', '')
   and '图型：SC-002' not in composer.build_instruction({'layout': _L[0]}, '3:4', [], False, 'normal', False, 'pure-image', '')
   and '图文模式' in composer.build_instruction({}, '3:4', [], False, 'normal', False, 'graphic-text', '')
   and '摄影写真模式' in composer.build_instruction({}, '3:4', [], False, 'normal', False, 'photo', ''))
_pure_c = asyncio.run(composer.compose({'image_mode': 'pure-image', 'ratio': '3:4',
                                        'items': [{'title': '猫', 'body': '窗台'}]}, [], _L, []))
ok('拼词接口接受 pure-image，模式集合齐全',
   composer.IMAGE_MODES == ('pure-image', 'graphic-text', 'photo')
   and composer.PURE_NO_TEXT in _pure_c['items'][0]['prompt'])

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
# 6) ESC 被上游渲染成字面标签：`["一本旧书"]</escape>` 在 JSON 解析失败后会退化成
#    按行兜底，旧实现只剥首尾引号方括号，把 `" ]</escape>` 直接印进了提示词（v1.1.58 用户截图）
_a6 = _pp('["一本旧书"]</escape>', 1)
ok('_parse_props：ESC 残片 </escape> 不进道具名',
   _a6 == ['一本旧书'], '解析结果：' + str(_a6))
ok('道具名两边的孤立引号方括号一起削掉',
   composer.tidy_phrase('一本旧书" ]</escape>') == '一本旧书'
   and composer.tidy_phrase('["一枚硬币"') == '一枚硬币'
   and composer.tidy_phrase('&lt;/escape&gt;一卷瑜伽垫') == '一卷瑜伽垫',
   str([composer.tidy_phrase('一本旧书" ]</escape>'), composer.tidy_phrase('["一枚硬币"')]))
ok('号池清洗同时删开标签与闭标签，正文内容留住',
   pool._sanitize_text('正文<escape>[A</escape>结束') == '正文[A结束'
   and pool._sanitize_text('正文<ESC>B</ESC>结束') == '正文B结束'
   and pool._sanitize_text('a&lt;/escape&gt;b') == 'ab',
   repr(pool._sanitize_text('正文<escape>[A</escape>结束')))
_wrapped = composer._wrap_scene('一本旧书" ]</escape>', True)
ok('混进道具名的代码残片不会出现在小辅助图描述里',
   '</escape>' not in _wrapped and '" ]' not in _wrapped and '一本旧书' in _wrapped,
   _wrapped)
_dirty_prompt = composer.build_prompt(
    {'title': '猫', 'body': '窗台', 'scene': '一个微缩尺寸的小人和一本旧书" ]</escape>'},
    {}, '3:4', [], False, 'normal', '', False, 'pure-image', '')
ok('纯图提示词里不会有任何 ESC 标签残片',
   '</escape>' not in _dirty_prompt and '<escape>' not in _dirty_prompt
   and '一个微缩尺寸的小人和一本旧书。' in _dirty_prompt,
   _dirty_prompt[-260:])
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

# 跨批次换图：原位替换，图和标题/文案/提示词一起搬，位置与批次归属不动
try:
    import tempfile, json as _json
    from pathlib import Path as _P
    import history as _hist
    with tempfile.TemporaryDirectory() as _td:
        _root = _P(_td)
        _img = _root / 'images'; _img.mkdir()
        _th = _root / 'thumbs'; _th.mkdir()
        (_img / 'src.png').write_bytes(b'PNG' * 100)
        (_th / 'dst1_c.webp').write_bytes(b'old')
        _rows = [
            {'id': 'dst1', 'task_id': 'T1', 'seq': 0, 'created': 1, 'finished': 2,
             'group': '批次A', 'status': 'error', 'error': '高度重复',
             'title': '旧标题', 'body': '旧文案', 'prompt': '旧提示词', 'image': None},
            {'id': 'src1', 'task_id': 'T2', 'seq': 1, 'created': 3, 'finished': 4,
             'group': '批次B', 'status': 'done', 'title': '新标题', 'body': '新文案',
             'prompt': '新提示词', 'image': 'src.png', 'ratio': '1:1',
             'account': 'new@x.com'},
        ]
        _hp = _root / 'hist.jsonl'
        _hp.write_text('\n'.join(_json.dumps(r, ensure_ascii=False) for r in _rows) + '\n',
                       encoding='utf-8')
        _h = _hist.History(_hp, _img, _th)
        _r = _h.fill_slot('dst1', 'src1')
        _d = _h.get('dst1') or {}
        ok('换图：图 / 标题 / 文案 / 提示词一起搬过来',
           _r.get('ok') and _d.get('status') == 'done' and _d.get('title') == '新标题'
           and _d.get('body') == '新文案' and _d.get('prompt') == '新提示词',
           str(_d)[:120])
        ok('换图：位置、批次归属、排序字段不动',
           _d.get('id') == 'dst1' and _d.get('task_id') == 'T1'
           and _d.get('seq') == 0 and _d.get('group') == '批次A')
        ok('换图：图片独立复制，不和来源共用文件',
           bool(_d.get('image')) and (_img / _d['image']).is_file()
           and _d['image'] != 'src.png' and (_img / 'src.png').is_file())
        ok('换图：旧缩略图缓存清掉（不残留旧图）',
           not (_th / 'dst1_c.webp').exists())
        ok('换图：同一张拒绝自替换', _h.fill_slot('dst1', 'dst1').get('ok') is False)
        _pall = _h.pick()
        _pex = _h.pick(exclude_task='T1')
        ok('换图：候选只要成图，且能排除当前批次',
           _pall['total'] == 2 and _pex['total'] == 1
           and _pex['items'][0]['task_id'] == 'T2',
           f"all={_pall['total']} excl={_pex['total']}")
        ok('换图：原位替换不追加重复记录',
           len([x for x in _hp.read_text(encoding='utf-8').splitlines() if x.strip()]) == 2)
except Exception as e:
    ok('跨批次换图可用', False, str(e)[:120])

# 删单张：只删这一条，同批其他图不动；记录没了，推送里自然也不再有这一屏
try:
    import tempfile as _tf2, json as _json2
    from pathlib import Path as _P2
    import history as _hist2
    with _tf2.TemporaryDirectory() as _td2:
        _r2 = _P2(_td2)
        _img2 = _r2 / 'images'; _img2.mkdir()
        _th2 = _r2 / 'thumbs'; _th2.mkdir()
        for _n in ('a.png', 'b.png'):
            (_img2 / _n).write_bytes(b'PNG' * 100)
        for _n in ('a_c.webp', 'a_p.webp', 'b_c.webp'):
            (_th2 / _n).write_bytes(b'old')
        _rows2 = [
            {'id': 'a', 'task_id': 'T1', 'seq': 0, 'group': '批次A', 'status': 'done',
             'title': '要删的', 'body': '要删的文案', 'prompt': '要删的提示词', 'image': 'a.png'},
            {'id': 'b', 'task_id': 'T1', 'seq': 1, 'group': '批次A', 'status': 'done',
             'title': '要留的', 'body': '留下的文案', 'image': 'b.png'},
            {'id': 'c', 'task_id': 'T2', 'seq': 0, 'group': '批次B', 'status': 'error',
             'error': '高度重复', 'title': '别处的'},
        ]
        _hp2 = _r2 / 'hist.jsonl'
        _hp2.write_text('\n'.join(_json2.dumps(r, ensure_ascii=False) for r in _rows2) + '\n',
                        encoding='utf-8')
        _h2 = _hist2.History(_hp2, _img2, _th2)
        _dr = _h2.remove_one('a')
        ok('删单张：返回剩余条数与任务归属',
           _dr.get('ok') and _dr.get('left') == 1 and _dr.get('task_id') == 'T1',
           str(_dr))
        ok('删单张：记录、原图、缩略图一起清掉',
           _h2.get('a') is None and not (_img2 / 'a.png').exists()
           and not (_th2 / 'a_c.webp').exists() and _dr.get('images', 0) >= 3)
        ok('删单张：同批其他图和别的批次都不动',
           _h2.get('b') is not None and (_img2 / 'b.png').exists()
           and (_th2 / 'b_c.webp').exists() and _h2.get('c') is not None)
        ok('删单张：这条的标题/文案/提示词跟着消失（推送里也没了）',
           all(i.get('title') != '要删的' for i in (_h2.task_one('T1') or {}).get('items', [])))
        ok('删单张：删不存在的 id 安全返回',
           _h2.remove_one('nope').get('ok') is False)
except Exception as e:
    ok('删单张可用', False, str(e)[:120])

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
   and all(x.get('text') and x.get('why') for x in _ritems),
   '共 %d 条，来源 %s' % (len(_ritems), _rd.get('source')))
ok('推荐话题只来自每日热榜，徽标只可能是榜单平台名',
   _rd.get('source') == '每日热榜'
   and all(x.get('tag') in app._HOTV_PLATFORMS for x in _ritems)
   and not any(k in str(x.get('tag') or '') for x in _ritems
               for k in ('常青', '知乎热榜', 'AIHOT', '兜底')))
_hv = app._hotv_read() or {}
_hv_rows = (_hv.get('byDate') or {}).get(
    sorted(_hv.get('byDate') or {})[-1] if _hv.get('byDate') else '') or []
_hv_titles = set()
for _r in _hv_rows:
    _t = ' '.join(str(_r.get('title') or '').split())
    _t = app._TOPIC_FILLER.sub('', _t).strip().rstrip('，,、：:？? ')
    _hv_titles.add(_t[:80])
ok('推荐池每一条都能在热榜详情里找到（不掺外部来源）',
   bool(_hv_rows) and all(x['text'] in _hv_titles for x in _ritems),
   '热榜 %d 条 / 推荐 %d 条' % (len(_hv_rows), len(_ritems)))
ok('话题池不含毒鸡汤空词',
   not any(app._TOPIC_BAD.search(x['text']) for x in _ritems))
ok('话题池不出现「你怎么看」式空问句',
   not any('你怎么看' in x['text'] for x in _ritems))
ok('热榜原题的空问句在加工前被剥掉（单元级，不依赖实时数据）',
   '你怎么看' not in app._topic_clean('外媒曝某队收紧纹身限制，国脚需戴护臂，对此你怎么看，限制是否合理？')
   and '限制是否合理' in app._topic_clean('外媒曝某队收紧纹身限制，国脚需戴护臂，对此你怎么看，限制是否合理？'))
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
ok('兜底题库够翻十批以上', len(app._evergreen_topics()) >= 24,
   len(app._evergreen_topics()))
ok('兜底题不带任何来源标签（用户 2026-11-26 明确：不要出现常青题这种东西）',
   all('tag' not in x for x in app._evergreen_topics())
   and app._evergreen_topics()[0].get('kind') in ('知乎向', '小红书向'))
ok('缺源平台仍能按调性挑兜底题（小红书优先小红书向）',
   len(app._hb_curated('xhs', '小红书')) > 0
   and all('小红书' not in str(x.get('title') or '') + str(x.get('tag') or '')
           for x in app._hb_curated('xhs', '小红书')))
ok('推荐话题卡片插在热点榜与选题框之间',
   all(x in _src_html for x in ('id="recTopics"', 'id="recList"', 'id="recNext"'))
   and 0 < _src_html.find('id="aihotList"') < _src_html.find('id="recTopics"')
        < _src_html.find('label for="topic"'))
ok('每批固定三条、翻批只走本地洗牌',
   'const PAGE = 3' in _src_html and 'function shuffle(' in _src_html
   and 'if(pool.length) render(); else load(1)' in _src_html)
ok('点选推荐话题填进选题框、断开旧热点综述并顺手取证',
   all(x in _src_html for x in ('function pick(it)', 'applyHotTopic({title: String(it.text',
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
ok('底部入口能打开每日热榜弹窗',
   all(x in _src_html for x in ('id="recOpen"', 'recOpen.onclick = hbOpen', '每日热榜 ↗')))
ok('弹窗含八平台卡片、页签、日期、同步时间和完整榜单表格',
   all(x in _src_html for x in ('id="hotBoardDlg"', 'id="hbTabs"', 'id="hbNote"',
                                'id="hbList"', 'id="hbDay"', 'id="hbSynced"',
                                'id="hbRows"', 'hb-grid', '.hb-dot.off',
                                'hb-table', '抓一次热榜')))
ok('切页签本地渲染，打开和手动抓取才打接口',
   all(x in _src_html for x in ("fetch(force ? '/api/trends/hot/refresh' : '/api/trends/hot'",
                                'hbLoad(0)', 'hbLoad(1)', 'hbDayEl.onchange')))
ok('热榜详情行的「用作选题」也走同一套取证',
   'applyHotTopic({title: String(r.title)' in _src_html
   and 'if(hbDlg && hbDlg.open) hbDlg.close()' in _src_html)
ok('热榜桥接接口已注册（读取 / 手动抓取）',
   any(getattr(r, 'path', '') == '/api/trends/hot' for r in app.app.routes)
   and any(getattr(r, 'path', '') == '/api/trends/hot/refresh'
           and 'POST' in getattr(r, 'methods', set()) for r in app.app.routes))
ok('热榜数据层自带且写到本插件目录，不依赖成稿台插件',
   app._HOTV_JS.exists()
   and 'dsh-wechat-studio' not in str(app._HOTV_JS)
   and app._HOTV_ROOT.name == 'image-workbench'
   and app._HOTV_FILE.name == 'hot.json'
   and all((app._HOTV_JS.parent / f).exists()
           for f in ('trends.js', 'fs-json.js', 'wechat-article.js')))
_pkg_json = json.loads((pathlib.Path(app.__file__).resolve().parent.parent / 'package.json').read_text(encoding='utf-8'))
ok('发布白名单包含自带热榜脚本', 'server/trends' in (_pkg_json.get('files') or []))
ok('热榜视图保留同步时间、八个平台、按日快照和来源状态',
   all(k in app._hotv_view({'v': 2, 'updatedAt': '2026-11-26 10:00',
                            'byDate': {'2026-11-26': []}, 'sources': []})
       for k in ('fetchedAt', 'today', 'dates', 'platforms', 'byDate', 'sources'))
   and len(app._hotv_view({})['platforms']) == 8)
ok('热榜视图带快照年龄，供前端判断是否偏旧',
   all(k in app._hotv_view({'updatedAt': '2026-11-26 10:00',
                            'byDate': {'2026-11-26': [{'platform': p} for p in app._HOTV_PLATFORMS]}})
       for k in ('fetchedAt', 'ageSeconds', 'old'))
   and app._hotv_view({})['old'] is True
   and not hasattr(app, '_hotv_need_refresh'))

_hotv_calls = {'n': 0}
_orig_hotv_refresh = app._hotv_refresh
async def _fake_hotv_refresh(force):
    _hotv_calls['n'] += 1
    return app._hotv_view({'updatedAt': datetime.now().strftime('%Y-%m-%d %H:%M'),
                           'byDate': {datetime.now().strftime('%Y-%m-%d'):
                                      [{'platform': p} for p in app._HOTV_PLATFORMS]}})
app._hotv_refresh = _fake_hotv_refresh
try:
    _snap0 = app._hotv_read()
    _usable = bool(_snap0) and not app._hotv_empty(app._hotv_view(_snap0))
    _hv = asyncio.run(app._hotv_get(False))
finally:
    app._hotv_refresh = _orig_hotv_refresh
ok('打开弹窗只读本地快照不联网；仅本地完全没快照时兜底抓一次',
   (_hotv_calls['n'] == 0 and _hv.get('cached') is True) if _usable
   else (_hotv_calls['n'] == 1),
   'usable=%s calls=%s cached=%s' % (_usable, _hotv_calls['n'], _hv.get('cached')))
ok('热榜快照按文件 mtime 缓存，不重复读盘解析',
   _usable and app._HOTV_CACHE['key'] is not None and app._HOTV_CACHE['data'] is not None)
ok('前端复用已读榜单，再次打开弹窗不再请求接口',
   'if(hbData){ hbRender(); return; }' in _src_html
   and '正在读取本地快照…' in _src_html
   and '要最新榜单点「抓一次热榜」' in _src_html)

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

# ---- 带明确时间的重点信息必须放第一句，作为整篇中心思想 ----
print('=== 要点时间置顶 ===')
_pts_mid = ['· 采用稀疏混合专家架构',
            '· Qwen 团队 11 月 16 日正式发布 Qwen4-Max',
            '· 输入价格每百万词元 1.5 美元']
ok('要点里的时间句被提到第一句',
   app._wx_timed_first(_pts_mid, [])[0].startswith('· Qwen 团队 11 月 16 日'),
   str(app._wx_timed_first(_pts_mid, [])))
ok('「正式发布」压过「截止报名」这类次要日期',
   '发布' in app._wx_timed_first(['· 活动报名 11 月 20 日截止',
                                  '· 11 月 16 日正式发布上线'], [])[0])
ok('要点无时间时从正文补抽带日期的事实',
   '11 月 16 日' in app._wx_timed_first(
       ['· 参数达到 2.4 万亿'],
       [{'caption': '先铺垫一句。Qwen 团队 11 月 16 日正式发布 Qwen4-Max。'}])[0])
ok('全篇无日期时不虚构时间',
   app._wx_timed_first(['· 参数很大'], [{'caption': '这段没有日期。'}]) == ['· 参数很大'])
ok('「今天」这类相对时间也能置顶',
   app._wx_timed_first(['· 参数很大', '· 今天正式开放申请'], [])[0].startswith('· 今天'))
_app_src = pathlib.Path(app.__file__).read_text(encoding='utf-8')
ok('公众号摘要优先用要点第一条（中心思想）',
   'lead_points[0] if lead_points' in _app_src)

# ---- 热点原文第一句本身就是时间和事件，直接拿它当中心思想 ----
print('=== 热点原文第一句 ===')
_story_prompt = ('事件：阿里发布 Qwen4-Max，预览版先行接入多产品\n'
                 '综述：Qwen 团队 11 月 16 日正式发布旗舰模型 Qwen4-Max，'
                 '这是基于新的 Qwen4 架构，采用 2.4 万亿参数稀疏 MoE。\n')
_story_lead = app._wx_story_lead(_story_prompt)
ok('热点综述第一句抽成中心思想',
   _story_lead.startswith('Qwen 团队 11 月 16 日正式发布'), repr(_story_lead))
ok('抽出的中心思想不超过 44 字', 0 < len(_story_lead) <= 44, len(_story_lead))
ok('综述里只有促销日期时不硬凑',
   app._wx_story_lead('综述：开放第三方模型，11 月 11 日起限时优惠。') == '')
ok('没有综述块（手动选题）时返回空',
   app._wx_story_lead('主题：画一张公众号封面') == '')
_p3 = app._wx_timed_first(['· 采用 2.4 万亿参数稀疏 MoE',
                           '· Qwen 团队 11 月 16 日正式发布 Qwen4-Max',
                           '· 输入价格 1.5 美元'], [], _story_lead)
ok('热点原文第一句排在要点第一位', _p3[0] == '· ' + _story_lead, repr(_p3[0]))
ok('与提炼出的重复句只留一条', len(_p3) == 3, _p3)
ok('去掉「· 」和空格后仍能认出重复句',
   app._wx_pts_key('Qwen 团队 11 月 16 日正式发布 Qwen4-Max')
   == app._wx_pts_key('Qwen团队11月16日正式发布Qwen4-Max'))
ok('推送时从历史提示词里取热点第一句',
   "_wx_story_lead(rec.get('prompt')" in _app_src.replace('"', "'"))

# ---- 草稿标题按微信 64 字上限，不再按 32 字硬切 ----
print('=== 草稿标题 ===')
_t34 = 'OpenAI扩大科学家免费使用ChatGPT计划，发放5万个新名额！'
ok('34 字标题完整保留，不再被截断', app._wx_title(_t34) == _t34, app._wx_title(_t34))
_t71 = ('OpenAI宣布把面向科学家的免费ChatGPT计划再扩大一倍，新增五万个名额，'
        '覆盖全球四十多个国家的科研机构与高校实验室，申请通道已经开放。')
_r71 = app._wx_title(_t71)
ok('超长标题收到 64 字以内', 0 < len(_r71) <= 64, len(_r71))
ok('超长标题在标点处收尾，不切断词', _r71[-1] in '，。！？；：、,.!?;:', repr(_r71[-6:]))
ok('没有标点时硬切 64 字', len(app._wx_title('一二三四五六七八九十' * 8)) == 64)
_html = (ROOT / 'static' / 'index.html').read_text(encoding='utf-8')
ok('前端推送标题也放宽到 64 字',
   'slice(0, 32)' not in _html and _html.count('slice(0, 64)') >= 2,
   _html.count('slice(0, 64)'))

# ---- 手动选题：正文首句带日期时，内容要点第一条也要带 ----
print('=== 正文首句 lead ===')
_doc = ('OpenAI 于 11 月 18 日发布 GPT-6.2 Astra 和 Sol，两款模型即日起面向符合条件的用户开放，'
        '开发者也能通过 API 调用。这次升级不只是性能提升。\n\n01｜旗舰更能打\n如果你要搭一个复杂网站。')
_dlead = app._wx_doc_lead(_doc)
ok('正文首句抽到「日期 + 发布」', '11 月 18 日' in _dlead and '发布' in _dlead, repr(_dlead))
ok('lead 不把后面大段参数带进来', len(_dlead) <= 44 and '旗舰更能打' not in _dlead, repr(_dlead))
ok('正文没日期就不编', app._wx_doc_lead('今天聊聊模型的发展。继续往下看。') == '')
ok('只有促销日期不当 lead',
   app._wx_doc_lead('11 月 11 日起限时优惠，欢迎选购。') == '',
   repr(app._wx_doc_lead('11 月 11 日起限时优惠，欢迎选购。')))
ok('markdown 标题不干扰抽取',
   '11 月 18 日' in app._wx_doc_lead('# 新模型\n\nOpenAI 于 11 月 18 日发布 Astra，能力升级。'))
_dpts = app._wx_timed_first(
    ['· OpenAI 发布 Astra 与 Sol，两款模型面向复杂创作',
     '· Astra 价格比旧款低 20%'],
    [{'title': '01', 'caption': '如果你要搭一个复杂网站'}], _dlead)
ok('抽到的日期 lead 顶到要点第一条', '11 月 18 日' in _dpts[0], repr(_dpts))
_dup = app._wx_timed_first(
    ['· OpenAI 发布 GPT-6.2 Astra 和 Sol，两款模型面向复杂创作',
     '· Astra 价格低两成'], [], _dlead)
ok('lead 与无日期的同事件要点只留一条', len(_dup) == 2 and '面向复杂创作' not in ''.join(_dup), _dup)
_keep = app._wx_timed_first(
    ['· OpenAI 发布 GPT-5，另涨价', '· Astra 价格低两成'], [], _dlead)
ok('同公司不同产品的要点不被误删', len(_keep) == 3 and 'GPT-5' in ''.join(_keep), _keep)
ok('推送时正文首句是热点 lead 的兜底',
   "_wx_doc_lead(rec.get('doc_body')" in _app_src.replace('"', "'"))

# ---- 热点漫画：拆镜 + 逐格连续性约束 ----
print('=== 热点漫画 ===')
ok('出图请求带连续设定字段',
   'comic_bible' in app.GenRequest.model_fields and 'comic_link' in app.GenRequest.model_fields)
_cb = app.GenRequest(mode='image', topic='x', prompt='画面：小明看手机。',
                     comic_bible='角色：小明 短发蓝外套。', comic_link='上一格屏幕亮起')
_cb_prompt = app.build_image_prompt(_cb)
ok('固定设定拼在提示词最前', _cb_prompt.startswith('【这组连续漫画的固定设定'), _cb_prompt[:40])
ok('本格承接原样带上', '上一格屏幕亮起' in _cb_prompt)
ok('原来的提示词没丢', '画面：小明看手机。' in _cb_prompt)
ok('没有设定时提示词逐字不变',
   app.build_image_prompt(app.GenRequest(mode='image', topic='x', prompt='画面：只有一句。'))
   == '画面：只有一句。')
_sb_raw = json.dumps({
    'title': '测试热点漫画', 'background': '背景一句话',
    'chars': [{'name': '小明', 'anchor': '短发—戴眼镜——蓝外套“固定”'}],
    'scene_anchor': '傍晚室内暖光',
    'panels': [
        {'title': '开场', 'body': '小明站在门口看手机。', 'dialog': '来了', 'link': ''},
        {'title': '转折', 'body': '屏幕亮起。', 'dialog': '', 'link': '上一格还在等'},
        {'title': '结果', 'body': '众人围过来看。', 'dialog': '真的', 'link': '上一格屏幕亮了'},
    ]}, ensure_ascii=False)
_sb = app._comic_parse_storyboard(_sb_raw, 20)
ok('分镜脚本解析出角色与逐格内容',
   len(_sb['chars']) == 1 and len(_sb['panels']) == 3, str(len(_sb['panels'])))
ok('分镜里破折号与弯引号被清掉',
   '—' not in _sb['chars'][0]['anchor'] and '“' not in _sb['chars'][0]['anchor'],
   _sb['chars'][0]['anchor'])
_doc = app._comic_panels_to_doc(_sb['panels'])
ok('分镜正文是 01｜ 编号格式，可以直接拆分镜卡',
   _doc.startswith('01｜开场') and '气泡文字：来了' in _doc, _doc[:30])
_bible = app._comic_bible_text(_sb)
ok('视觉设定含角色锚点与单格约束',
   '小明' in _bible and '单格漫画' in _bible and '不要重新设计人物' in _bible)
try:
    app._comic_parse_storyboard(json.dumps({'panels': [{'body': '只有一格'}]}), 20)
except ValueError:
    ok('分镜少于 3 格直接拒绝', True)
else:
    ok('分镜少于 3 格直接拒绝', False)
_fb = app._comic_storyboard_fallback('第一句事实。第二句事实。第三句事实。', '测试', 20)
ok('AI 不可用时兜底不编角色和对白',
   _fb['chars'] == [] and all(not p['dialog'] for p in _fb['panels']), str(len(_fb['panels'])))
ok('分镜接口已注册', '/api/comic/storyboard' in _app_src)
ok('热点条目出现「做成漫画」按钮',
   '<button class="btn ghost mini ai-comic"' in _html)
ok('分镜预览弹窗与格数输入存在',
   'id="comicDlg"' in _html and 'id="comicN"' in _html)
ok('批量出图与单张重做都携带连续设定',
   _html.count('comic_bible: state.comicBible') >= 2
   and _html.count('comic_link: it.link') == 2,
   str(_html.count('comic_link: it.link')))
ok('点击做成漫画先弹等待态，不让界面看起来没反应',
   'showComicLoading();' in _html and '正在拆分镜' in _html
   and _html.index('showComicLoading();') < _html.index("fetch('/api/comic/storyboard'"))
ok('等待态禁用出图按钮，返回后再恢复',
   "use.disabled = true" in _html and "use.disabled = false" in _html
   and 'cb-spin' in _html)
ok('拆镜 AI 超时压到 20 秒内，不再干等近一分钟',
   'timeout=20' in _app_src and 'asyncio.wait_for(_ask(), timeout=22)' in _app_src)
ok('分镜接口返回 ok:true，避免 HTTP 200 被前端判失败',
   'data["ok"] = True' in _app_src and '!r.ok || !d.ok' in _html)
ok('漫画模式下拆成分镜按钮被拦，不重建清单不丢承接',
   'state.comicBible && state.items.length' in _html
   and '漫画分镜已拆好 ' in _html)
ok('一键清空连漫画角色锚点一起清',
   "state.comicBible = '';          // 角色锚点也要清" in _html)
ok('分镜写入后状态栏给出下一步指引',
   '不用再点「拆成分镜」，直接点右上「批量出图」' in _html)
ok('兜底分镜抽小标题，不再一律第N格',
   '_comic_fallback_title(sent, i + 1)' in _app_src
   and 'def _comic_fallback_title' in _app_src)
_fb2 = app._comic_storyboard_fallback(
    'Thinking Machines Lab 发布开源权重模型 Inking 2 Mini，回应部分用户认为参数过大。'
    '模型采用循环 Transformer 架构：24 层对每个 token 运行三次。', '测试', 20)
ok('兜底标题取自原句且有信息量',
   all('第%d格' % (i + 1) != p['title'] for i, p in enumerate(_fb2['panels'])),
   str([p['title'] for p in _fb2['panels']]))
ok('分镜写入与批量出图两处都回填中层小道具',
   _html.count('x.scene = d.items[k].scene') >= 1
   and _html.count('fillScenes();') >= 3,
   str(_html.count('fillScenes();')))
_long_title = 'Thinking Machines 开源 26.48 亿参数模型 Inking 2 Mini'
ok('兜底分镜标题不再截成 40 字',
   'topic[:40]' not in _app_src
   and app._comic_storyboard_fallback('这条热点正文足够长可以作为一格内容。',
                                       _long_title, 5)['title'] == _long_title,
   app._comic_storyboard_fallback('这条热点正文足够长可以作为一格内容。',
                                  _long_title, 5)['title'])
ok('AI 分镜标题上限放宽到 64 字',
   '_comic_clean(d.get("title"), 64)' in _app_src.replace("'", '"'))
ok('标题框换成自增高多行，长标题不再横向截断',
   '<textarea id="docTitle" rows="2"' in _html
   and '<input id="docTitle"' not in _html
   and 'field-sizing:content' in _html)

# ---- 小说推文叠字漫画 ----
_pool_src = pathlib.Path(pool.__file__).read_text(encoding='utf-8')
ok('出图请求支持叠字字段',
   'novel_overlay: bool = False' in _app_src and 'overlay_text: str' in _app_src
   and '"novel_overlay": bool(req.novel_overlay)' in _app_src)
ok('worker 出完图再叠字，且叠字失败不影响出图',
   'novel_overlay.apply(final, job["overlay_text"])' in _pool_src
   and 'overlay failed:' in _pool_src)
ok('叠字模式剥掉气泡文字并禁止模型画字',
   '气泡文字：' not in app._novel_no_text('画面描述 气泡文字：你好，世界')
   and '不要出现任何文字' in app._novel_no_text('画面描述'))
_fb3 = app._comic_storyboard_fallback('工作人员点头：“是的，我们会尽力满足。”她轻声说：好的。',
                                      '标题', 5)
ok('兜底分镜也带旁白，叠字不缺文案',
   all(p['caption'] for p in _fb3['panels']),
   str([p['caption'] for p in _fb3['panels']]))
ok('引号内对话会被标红解析',
   [r[0] for r in novel_overlay._colored_chars('他说：「初次见面，姐姐好。」后离开')
    if r[1]] == list('「初次见面，姐姐好。」'))
ok('前端提供叠字开关、旁白栏与 9:16 画幅',
   'id="comicNovel"' in _html and 'class="it-caption"' in _html
   and "$('#ratio').value = '9:16'" in _html)
ok('批量出图与单张出图都带叠字参数',
   _html.count('novel_overlay: true, overlay_text: it.caption.trim()') == 2,
   str(_html.count('novel_overlay: true, overlay_text: it.caption.trim()')))
ok('草稿快照保存旁白与叠字开关',
   'comic_novel: !!state.comicNovel' in _html
   and "...(it.caption ? {caption: it.caption} : {})" in _html)
# 2026-11-27 反馈：普通图文的分镜条目也挂出「旁白」框。根因是 state.comicNovel 默认 true，
# 加上拆分/草稿里给每条写了空串 caption，`typeof it.caption === 'string'` 恒真。
ok('旁白框只在漫画叠字模式或真有旁白文字时出现',
   'comicNovel:false};' in _html
   and "comicNovel || (typeof it.caption === 'string' && it.caption.trim())" in _html
   and "...(novel ? { caption: String(p.caption || p.dialog || '') } : {})" in _html
   and "state.comicNovel = _novCap && d.comic_novel !== false;" in _html)

# ---- 公众号推送网关持久化 ----
_secrets = pathlib.Path.home() / '.dsh' / 'image-workbench' / 'credentials.json'
ok('本机凭据备份存在且含网关地址',
   _secrets.exists() and 'wx.hyiplist.biz/api' in _secrets.read_text(encoding='utf-8'))
_saved_load = app.load_config
try:
    app.load_config = lambda: {'wx_gateway': '', 'wx_token': ''}
    _gw_back, _tk_back = app._wx_cfg()
finally:
    app.load_config = _saved_load
ok('网关配置被清空时仍能从备份取回',
   _gw_back == 'https://wx.hyiplist.biz/api' and bool(_tk_back),
   str((_gw_back, bool(_tk_back))))
ok('配置接口从 _wx_cfg 取网关值，不再只读可能被覆盖的字段',
   '\"wx_gateway\": _wx_cfg()[0]' in _app_src)
ok('保存配置时同步写本机凭据备份',
   '_persist_wx_secrets(cfg.get(\"wx_gateway\"), cfg.get(\"wx_token\"))' in _app_src)

# ---- 推送不再被号池 AI 润色拖死 ----
ok('推送要点/结尾润色各带硬超时，号池慢也不卡推送',
   _app_src.count('await asyncio.wait_for(_ask(), timeout=9)') == 2
   and 'timeout=8)' in _app_src)
ok('两处润色并发执行，不再串行叠加等待',
   'lead_points, footer_data = await asyncio.gather(' in _app_src)
ok('两处推送按钮都有 120s 硬超时兜底，不会永久停在推送中',
   _html.count('setTimeout(()=> ctrl.abort(), 120000)') == 2
   and _html.count('signal:ctrl.signal') == 2)

# 单图快出：参考图与前三张卡同排 + 关键字区（用户 2026-11-26）
ok('参考图卡与图型/风格/主题色四张同一行对齐（grid 4 列）',
   'grid-template-columns:repeat(4,minmax(0,1fr))' in _html
   and '#cardRef{max-width:none}' in _html)
ok('单图快出区存在：关键字框 + 优化提示词 / 直接出图按钮',
   all(x in _html for x in ('id="quickSingle"', 'id="qsKey"', 'id="qsPolish"', 'id="qsGen"')))
ok('单图快出不拆分镜：直接调 /api/generate 且带上 prompt，不写 state.items',
   'async function qsGenerate()' in _html
   and 'submitGenerate(p)' in _html
   and 'if (!_qsOptimized)' in _html)
ok('单图快出重拼时清空旧手改指令，以关键字为准',
   'body.instruction = \'\';' in _html and 'body.topic = keyword;' in _html)
# 2026-11-26 二次调整：优化结果原地覆盖输入框，不写下方出图指令；出图模式改名多图模式
ok('优化提示词原地覆盖关键字框，不占用下方出图指令',
   "$('#qsKey').value = prompt;" in _html
   and 'state.instrCustom = prompt; syncInstr();' not in _html
   and "$('#qsKey').addEventListener('input'" in _html)
ok('下方出图模式改名为「多图模式」，与单图快出成系列',
   'class="plabel mode-head"' in _html
   and '多图模式</span>' in _html
   and '<b>单图快出</b>' in _html)
_mh_css = _html.split('.mode-head{')[1].split('}')[0] if '.mode-head{' in _html else ''
ok('多图模式标题加粗、字号与单图快出一致，且带绿色图标',
   all(s in _mh_css for s in ('font-size:12.5px', 'font-weight:600', 'color:var(--ink)'))
   and '.mode-head svg{width:15px;height:15px;color:#1f9d6b}' in _html
   and 'class="plabel mode-head"><svg' in _html)
# 2026-11-26 三次调整：单图快出与上方四张卡关联，选型变了重拼；不碰多图模式
ok('单图快出用选型指纹和原始关键字保证与上方四张卡关联',
   'function qsCardFingerprint()' in _html
   and 'function qsCurrentKeyword()' in _html
   and '_qsFingerprint !== nowFp' in _html
   and 'qsCompose(qsCurrentKeyword())' in _html)
ok('清空框才重置关键字态，非空手改保留不重拼',
   "if (!v){ _qsOptimized = false; _qsFingerprint = ''; }" in _html
   and 'if (!_qsOptimized) _qsRawKey = v;' in _html)
# 2026-11-26 四次调整：直接出图给工作动效，持续到出图完成
ok('直接出图有完整工作动效：按钮转圈 + 卡片扫光 + 状态脉冲',
   '@keyframes qsSpin' in _html and '@keyframes qsSweep' in _html and '@keyframes qsPulse' in _html
   and '.qs-btn.loading' in _html and '.quick-single.working' in _html
   and '.qs-status.working' in _html and 'function qsWorking(on)' in _html
   and 'qsBusy(true); qsWorking(true)' in _html)
ok('出图动效持续到图片生成完成，poll 三种终态都回调收尾',
   'async function poll(id, statusEl, mode, waitTxt, onFinish)' in _html
   and _html.count('if (onFinish) onFinish(') == 3
   and "'单图排队中…', (result, d)=>{" in _html
   and 'qsWorking(false); qsBusy(false);' in _html)
ok('状态文字更新不冲掉工作动效类',
   "el.classList.contains('working') ? ' working' : ''" in _html)
# 2026-11-26 五次调整：内置示例，一键还原图型/风格/主题色和关键字
# 八次调整：示例换成纯图无字的真实记录，示例效果按钮整体去掉
ok('内置示例可一键还原图型/风格/主题色和关键字，且示例 prompt 是纯图无字版',
   'const QS_EXAMPLE = {' in _html
   and "layout:'IG-030', style:'FE-061', color:'C-27'" in _html
   and '93b2899443b2' in _html
   and '不要文字，不要水印' in _html
   and '小红书信息图' not in _html
   and 'function qsRestoreExample()' in _html
   and "$('#qsRestore').onclick = qsRestoreExample;" in _html)
ok('示例效果入口已去掉，只留还原示例按钮',
   'id="qsSample"' not in _html
   and 'qs-sample' not in _html
   and "$('#qsSample').onclick" not in _html
   and 'id="qsRestore"' in _html)
ok('还原示例记住原始关键字与选型指纹，换卡后仍能按新组合重拼',
   '_qsRawKey = QS_EXAMPLE.rawKey; _qsOptimized = true;' in _html
   and '_qsFingerprint = qsCardFingerprint();' in _html)
# 2026-11-27：示例提示词自己写着「不要文字」，还原后页签却被 qsAutoMode 顶到图文
_restore_body = _html.split('function qsRestoreExample(){')[1].split('function qsCardFingerprint')[0]
ok('还原示例按示例本身落到纯图，不再被图型自动顶成图文',
   "state.qsMode = 'pure-image';" in _restore_body
   and "state.qsModeAuto = false;" in _restore_body
   and 'qsPaintMode();' in _restore_body
   and _restore_body.index('syncPicks();') < _restore_body.index("state.qsMode = 'pure-image';")
   and _restore_body.index("state.qsMode = 'pure-image';") < _restore_body.index('_qsFingerprint ='))
# 2026-11-26 六次调整：单图快出成品登记进结果列表，修复右侧预览点不开
ok('单图出完登记进结果列表，右侧预览可点击',
   "if (onFinish) onFinish('done', d);" in _html
   and 'topic:text.slice(0, 60), seq:state.results.length,' in _html
   and "qsStatus('出图完成，点右侧预览图可放大', 'ok')" in _html)
# 2026-11-26 七次调整：单图快出默认折叠，只留标题；多图模式照常显示
ok('单图快出默认折叠，只留标题，下方多图模式正常显示',
   'class="quick-single collapsed" id="quickSingle"' in _html
   and 'id="qsHead"' in _html
   and 'id="qsBody" hidden' in _html
   and '.quick-single.collapsed .qs-tag,' in _html
   and '.quick-single.collapsed .qs-body{display:none}' in _html
   and 'class="params" id="omodeRow"' in _html)
ok('点标题可展开/收起单图快出，键盘也能操作，出图逻辑不受影响',
   'function qsSetOpen(open)' in _html
   and "head.addEventListener('click', toggle);" in _html
   and "head.addEventListener('keydown'" in _html
   and "qsSetOpen(false);" in _html
   and 'async function qsGenerate()' in _html)
# 2026-11-27 反馈：只有一个 14px 小箭头，用户根本看不见能收，改成文字开关 + 收起态摘要
ok('折叠开关是看得见文字的按钮，收起态还带内容摘要',
   'class="qs-fold" id="qsFold"' in _html
   and "<span id=\"qsFoldTxt\">展开</span>" in _html
   and "t.textContent = open ? '收起' : '展开';" in _html
   and 'function qsPaintMini()' in _html
   and 'class="qs-mini" id="qsMini"' in _html
   and '.quick-single:not(.collapsed) .qs-mini{display:none}' in _html)
# 2026-11-27 反馈：整张卡都能点开选择器；「更换」按钮去掉；X 放大且不占文字宽度
ok('四张选择卡整块可点，更换按钮已去掉，X 放大且浮在右上角',
   'id="chgLayout"' not in _html and 'id="chgStyle"' not in _html
   and 'id="chgColor"' not in _html and 'id="chgRef"' not in _html
   and 'class="pk-change"' not in _html
   and "if (e.target.closest('.pk-clear')) return;" in _html
   and "if (e.target.closest('.pk-btn')) return;" in _html
   and ".pk-acts{position:absolute;bottom:7px;right:7px" in _html
   and 'font-size:22px' in _html
   and "body.addEventListener('keydown'" in _html)
# 2026-11-26 八次调整：单图纯图/图文，多图纯图/图文/摄影
ok('单图默认纯图，多图三种模式齐全且彼此独立',
   'id="qsModeSeg"' in _html
   and 'data-qsmode="pure-image" class="on"' in _html
   and "state.qsMode = 'pure-image';" in _html
   and 'data-omode="pure-image"' in _html
   and 'data-omode="graphic-text" class="on"' in _html
   and 'data-omode="photo"' in _html)
ok('单图拼词用自己的模式，切换模式计入指纹自动重拼',
   'body.image_mode = state.qsMode;' in _html
   and 'b.image_mode = state.qsMode;' in _html
   and 'function qsSetMode(mode)' in _html
   and 'qsSetMode(b.dataset.qsmode);' in _html)
ok('多图切换直接取三模式值，历史与草稿还原兼容旧记录',
   "state.omode = b.dataset.omode || 'graphic-text';" in _html
   and _html.count("['pure-image','graphic-text','photo'].includes(") == 3)

print('=== 按内容推荐组合（图型 / 风格 / 主题色）===')
import recommend as rec
_cat = catalog.build()
_LAY_IDS = {x['id'] for x in _cat['layouts']}
_STY_IDS = {x['id'] for x in _cat['styles']}
_COL_IDS = {x['id'] for x in _cat['colors']}
HEAL = '有很多事你当时想不通，别着急，过段时间你可能就忘了。'
MONEY = '月薪八千怎么攒下第一个十万'
HUMOR = '周一开会被老板当众点名，我当场社死'


def _rec(text, seed, avoid=None):
    r = rec.recommend(text, seed=seed, avoid=avoid or {})
    return r, (r['layout']['id'], r['style']['id'], r['color']['id'])


_r0, _c0 = _rec(HEAL, 42)
ok('推荐的三个 id 都真实存在于库里',
   _c0[0] in _LAY_IDS and _c0[1] in _STY_IDS and _c0[2] in _COL_IDS, _c0)
ok('同一段内容 + 同一 seed 结果完全可复现', _rec(HEAL, 42)[1] == _c0)
ok('读得懂题材：治愈句命中治愈松弛，搞钱句命中理财搞钱',
   '治愈松弛' in [t['name'] for t in _r0['tags']]
   and '理财搞钱' in [t['name'] for t in _rec(MONEY, 3)[0]['tags']])
_heal = [_rec(HEAL, s)[0] for s in range(10)]
ok('治愈类内容只会配社媒卡，不会被推成信息图或分镜',
   all(x['layout']['group'] == '社媒卡' for x in _heal),
   {x['layout']['group'] for x in _heal})
ok('治愈类内容不会配写实摄影（和纯色留白卡冲突）',
   all(x['style']['group'] != '写实摄影与视觉大片' for x in _heal),
   {x['style']['group'] for x in _heal})
_money = [_rec(MONEY, s)[0] for s in range(10)]
ok('搞钱/教程类内容落到信息图，而不是情感卡',
   all(x['layout']['group'] == '信息图' for x in _money),
   {x['layout']['group'] for x in _money})
_humor = [_rec(HUMOR, s)[0] for s in range(10)]
ok('吐槽类内容配幽默画风（社论幽默组）',
   all(x['style']['group'] == '国际社论幽默' for x in _humor),
   {x['style']['group'] for x in _humor})
_cs = {_rec(HEAL, s)[1] for s in range(10)}
ok('换 seed 能换出不同组合，不是固定一套', len(_cs) >= 4, f'{len(_cs)}/10 种')
_a, _ = _rec(HEAL, 7)
_ca = (_a['layout']['id'], _a['style']['id'], _a['color']['id'])
_b, _cb = _rec(HEAL, 7, avoid={'layout': [_ca[0]], 'style': [_ca[1]], 'color': [_ca[2]]})
ok('「换一组」把上一组压下去，至少换掉一件', _ca != _cb, f'{_ca} → {_cb}')
_e, _ce = _rec('', 1)
ok('没内容也能给一组通用搭配并说明原因',
   _ce[0] in _LAY_IDS and _e['note'], _e['note'])
_photo = [rec.recommend(HEAL, seed=s, mode='photo') for s in range(6)]
ok('摄影模式只配写实那一路，并说明图型不参与',
   all(x['style']['group'] == '写实摄影与视觉大片' for x in _photo)
   and '摄影模式不套图型' in (_photo[0]['note'] or ''),
   {x['style']['group'] for x in _photo})
ok('纯图模式也说明图型不参与', '纯图模式不套图型' in rec.recommend(HEAL, seed=2, mode='pure-image')['note'])
ok('带使用场景的画风（监控/淘宝/随手拍/航拍）不会硬塞给无关内容',
   all(x['style']['id'] not in rec.ODD_STYLE_HINTS for x in _photo),
   {x['style']['id'] for x in _photo})
ok('内容真提到航拍时，航拍画风能回到候选里',
   any(rec.recommend('周末用无人机航拍了一段海岸线', seed=s, mode='photo')['style']['id'] == 'FI-034'
       for s in range(12)))
_rp = client.post('/api/recommend', json={'text': HEAL, 'seed': 9})
_rj = _rp.json() if _rp.status_code == 200 else {}
ok('推荐接口可用，返回三件套 / 题材 / 标签',
   _rp.status_code == 200 and (_rj.get('layout') or {}).get('id') in _LAY_IDS
   and isinstance(_rj.get('tags'), list))
# 2026-11-27：推荐组合只留配图工坊那条推荐带，顶部 / 单图下方的重复入口全撤
ok('推荐组合只剩推荐带一个入口，顶部与单图下方的重复按钮已撤',
   'id="comboRecBtn"' in _html and 'id="comboTop"' not in _html
   and 'id="qsRec"' not in _html and 'id="randomCombo"' not in _html
   and "if ($('#comboRecBtn')) $('#comboRecBtn').onclick = ()=> comboRecommend('auto');" in _html
   and "'/api/recommend'" in _html and 'avoid_layout' in _html)
ok('推荐前先读原始关键字，不拿九行提示词去匹配题材',
   "(_qsOptimized && _qsRawKey) ? _qsRawKey : val('#qsKey')" in _html)
ok('推荐后同步三张卡、记住这次风格，按钮变「换一组」',
   'state.sel.layout = L;' in _html and 'state.sel.color  = C;' in _html
   and "_comboLast = {layout: L.id, style: S.id, color: C.id};" in _html
   and "if (btn) btn.textContent = _comboLast ? '换一组' : '按内容推荐';" in _html)
# 2026-11-27：缩微摄影融合默认常开，只有用户自己关过才记住「关」
ok('缩微摄影融合默认常开，用户关过才记住',
   '<input type="checkbox" id="fusion" checked>' in _html
   and "sessionStorage.getItem('iw_fusion_off') === '1'" in _html
   and "sessionStorage.setItem('iw_fusion_off'" in _html
   and "iw_fusion_by_layout" not in _html.split('function syncFusionDefault')[1][:400])

client.close()
print(f'\n结果：{passed} 通过 / {failed} 失败')
sys.exit(0 if failed == 0 else 1)
