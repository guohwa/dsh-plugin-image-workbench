"""把 handraw-style 的「风格 + 主题色 + 图型 + 融合 + 文字槽位 + 情绪 + 比例」
拼成两层产物：

1. 出图指令（instruction）：一行短指令，人能看懂、也能直接丢给 AI 再加工
2. 完整提示词（prompt）：真正提交给号池生图的那段文本

规则来源：handdraw-painter/SKILL.md
- 提示词内不出现任何编号（FA-001 / SC-002 / C-16 一律换成自然语言名），
  否则生图模型会把编号当文字画进画面
- 风格特征只保留正向描述，含「避免/不要/不准/禁止」的分句一律剔除
- 图型 prompt 原文照用，它是排版权威描述
"""

from __future__ import annotations

import json
import random
import re

NEG_MARKERS = ("避免", "不要", "不准", "禁止")

# 缩微摄影跨媒介融合：角色小、场景真、同光源同空间
MICRO_FUSION = (
    "缩微摄影跨媒介融合：角色尺寸异常小、场景正常尺寸。"
    "场景视觉语言＝暖调写实生活摄影（真实材质、可信光影）。"
    "角色与场景同空间同光源，有可信接触、投影、遮挡，角色真正活在场景里而不是贴纸。"
)

WHITESPACE = {
    "moderate": "【大量留白】",
    "high": "【大量留白，场景只显示必要部分，不要显示全】",
}

# 2.35:1 的公众号首图是「3:2 出图 + 上下各裁一截」得来的，不提前约束构图，
# 裁完就会砍掉标题或者削掉人物头顶。这里把安全区写死进提示词里。
WIDE_CROP_RATIO = "2.35:1"
WIDE_CROP_HINT = (
    "横幅构图：主体、标题和所有关键元素都压在同一块居中的横向安全区里，"
    "画面上下各留一段可以整块裁掉的次要背景（纯色、天空、地面或虚化），"
    "文字和人物头顶都不要贴到画面上沿或下沿"
)

# 摄影模式：文案全部照搬 handraw-style 上游，不另写一套
# - 「摄影写真模式」见 handraw-style/gallery/tutorials.html 的 photo 模式前缀
# - 「高级摄影师风格，创意构图，强烈的戏剧张力」见
#   handraw-style/skills/couple-photo-orchestrator/SKILL.md「通用摄影关键词」
# 上游那句「请先出3个方案供我挑选」是让 AI 先给方案的指令，进生图会被当成画面要求，
# 所以这里只取模式标识本身。
PHOTO_LEAD = "摄影写真模式。"
PHOTO_KEYWORDS = "通用摄影关键词：高级摄影师风格，创意构图，强烈的戏剧张力。"
PHOTO_MODES = ("graphic-text", "photo")

# 主图（hero / 首图）的硬约束：scene 层有黑名单拦截，但主图提示词里没有禁杯规则，
# 模型自由发挥时把杯子画进了主图（截图里就出现过 NVIDIA 马克杯）。这条补在主图尾部，
# 图文 / 摄影两种模式都生效，和 BANNED_PROP_TOKENS 形成双层保险。
NO_CUP_RULE = (
    "画面里禁止出现任何饮用容器：杯子、马克杯、水杯、茶杯、咖啡杯、玻璃杯、酒杯、"
    "保温杯、纸杯都不要画；若需要手持道具，改用与主题相关的其它实物。"
)

TAIL_INSTRUCTION = ""

# 参考图两种用法（对齐 Midjourney 的 --sref / --cref 解耦思路）：
# - style（默认）：参考图只作「视觉基准」，提取画风/笔触/配色/光影/质感，
#   但严禁 1:1 复刻原图的具体元素/人物姿态/构图布局/文字。这是用户要的「融合」，
#   不是「重画同一张」。
# - full：尽量一比一还原参考图（保留给确实需要复刻的人）。
# 注意：无论哪种，参考图都会作为 /images/edits 的底图传入，edits 接口会过度保留底图，
# 所以 style 模式必须靠下面的强约束把「只借风格、不抄内容」钉死。
REF_FUSION = (
    "参考我上传的这张图，仅把它当作「视觉基准」：提取它的画风、笔触、配色、光影与整体质感，"
    "但不要照搬它的具体元素、人物姿态、构图布局或任何文字。"
    "本图的画面内容、主体、排版与构图完全由下面这些要求决定，你是在「借鉴风格」，不是「复制这张图」。"
)
REF_FUSION_FULL = (
    "参考我上传的这张图，尽可能一比一还原它的画面内容、构图、人物姿态、配色与画风，"
    "把它当作可直接参照的模板；下面的版式与文字要求叠加在这张图上。"
)

# 中层小辅助图没填时的兜底描述，保证三层版式不会空一层
SCENE_FALLBACK = "一个涂鸦小人和一件具体实物道具，动作直接对应上层标题的意思，造型简单、线条少"

# 元素库：按文案关键词匹配「一件具体实物道具」，解决"具体实物道具"被生图模型
# 一律脑补成杯子的问题。命中类别里挑一个，且单次出图内相邻分镜不重复。
# 刻意不放杯子/马克杯，且每个类别给足候选，保证画面元素多样、不撞车。
PROP_LIB = [
    (["钱", "工资", "存款", "理财", "消费", "攒", "借款", "花呗", "信用卡", "账单", "物价", "涨价",
      "基金", "股票", "钱包", "省", "花钱", "还款", "退税", "奖金"],
     ["一枚硬币", "一只存钱罐", "一张工资条", "一张购物小票", "一张银行卡", "一只钱包",
      "一叠现金", "一只计算器", "一张彩票", "一只记账本", "一只收银秤", "一沓发票"]),
    (["健康", "病", "医院", "身体", "体检", "运动", "减肥", "睡眠", "失眠", "熬夜", "颈椎", "腰",
      "锻炼", "跑步", "健身", "吃药", "感冒", "养生", "复查", "挂号"],
     ["一只体温计", "一盒药", "一只运动鞋", "一只枕头", "一卷瑜伽垫", "一对哑铃",
      "一条护腰", "一只护膝", "一盒创可贴", "一只计步器", "一副耳机", "一只保温壶"]),
    (["工作", "开会", "加班", "老板", "同事", "面试", "邮件", "办公", "打卡", "工位", "KPI", "绩效",
      "辞职", "离职", "摸鱼", "汇报", "项目", "打工人", "offer", "裁员"],
     ["一台笔记本电脑", "一盏台灯", "一张工牌", "一叠便利贴", "一个文件筐", "一台打印机",
      "一只订书机", "一盒回形针", "一只马克笔", "一只咖啡豆罐", "一块白板", "一摞文件夹"]),
    (["学习", "考试", "读书", "笔记", "上课", "课本", "学生", "考研", "刷题", "复习", "知识", "课堂", "老师"],
     ["一本书", "一支铅笔", "一本笔记本", "一个书包", "一盏台灯", "一个地球仪",
      "一块橡皮", "一只钢笔", "一张试卷", "一只书签", "一副圆规", "一只笔袋"]),
    (["爱情", "约会", "心动", "分手", "对象", "情侣", "结婚", "恋爱", "暧昧", "表白", "异地", "脱单"],
     ["一封手写信", "一支玫瑰", "两张电影票", "一部手机", "一张双人沙发", "一条围巾",
      "一只戒指盒", "一条手链", "一张拍立得", "一只心形挂锁", "一盒巧克力", "一只心形摆件"]),
    (["美食", "吃", "外卖", "做饭", "咖啡", "茶", "餐厅", "饭", "饿了", "火锅", "奶茶", "早餐",
      "夜宵", "下厨", "甜点", "聚餐"],
     ["一只餐盘", "一口锅", "一只咖啡壶", "一只水果盘", "一双筷子", "一台微波炉",
      "一只汤碗", "一盒寿司", "一只打蛋器", "一只调料瓶", "一块砧板", "一只烘焙模具"]),
    (["旅行", "出门", "行李", "风景", "机票", "酒店", "度假", "出游", "拍照", "攻略", "打卡"],
     ["一只行李箱", "一本护照", "一台相机", "一张地图", "一顶遮阳帽", "一双旅游鞋",
      "一只望远镜", "一张登机牌", "一只防晒霜", "一只充电宝", "一只攻略本", "一台自拍杆"]),
    (["科技", "AI", "代码", "芯片", "数据", "程序", "算法", "智能", "机器人", "互联网", "软件",
      "硬件", "显卡", "电脑", "大模型", "算力"],
     ["一台手机", "一块电路板", "一个机械键盘", "一只机器人玩偶", "一块显示屏", "一块芯片",
      "一只无人机", "一只智能音箱", "一只手写板", "一只显卡模型", "一只数据线", "一只路由器"]),
    (["宠物", "猫", "狗", "毛孩子", "撸猫", "遛狗", "喵", "汪"],
     ["一团毛线球", "一根狗绳", "一只食盆", "一个猫爬架", "一根逗猫棒",
      "一只宠物窝", "一张宠物垫", "一只宠物梳", "一块猫薄荷", "一只小铃铛"]),
    (["天气", "雨", "冷", "热", "太阳", "降温", "下雪", "台风", "刮风", "空调"],
     ["一把雨伞", "一副太阳镜", "一台小风扇", "一件外套", "一条围巾", "一顶草帽",
      "一双雨靴", "一只暖手宝", "一罐驱蚊液", "一件雨衣"]),
    (["时间", "拖延", "懒", "周末", "假期", "放假", "躺平", "无聊", "焦虑", "压力", "emo", "内卷", "摆烂"],
     ["一个闹钟", "一张沙发", "一本日历", "一只沙漏", "一个抱枕", "一台游戏机",
      "一只解压捏捏乐", "一条毛毯", "一只泡泡机", "一本漫画书"]),
    (["家", "搬家", "装修", "妈妈", "爸爸", "孩子", "家人", "家务", "打扫", "带娃"],
     ["一个收纳箱", "一只扫把", "一束花", "一只玩具熊", "一个相框", "一套餐具",
      "一只吸尘器", "一只洗衣篮", "一只积木", "一只奶瓶", "一块地毯", "一只台历"]),
    (["节日", "生日", "春节", "圣诞", "礼物", "跨年", "元宵", "端午", "中秋"],
     ["一只礼物盒", "一根蜡烛", "一只灯笼", "一串彩灯", "一只红包", "一块月饼",
      "一只气球", "一棵小盆栽", "一只皇冠", "一张贺卡"]),
    (["美妆", "护肤", "化妆", "穿搭", "发型", "口红", "香水"],
     ["一支口红", "一瓶香水", "一面小镜子", "一只化妆刷", "一盒眼影", "一条丝巾",
      "一只首饰盒", "一只美甲贴", "一只卷发棒", "一只粉饼"]),
    (["运动", "球", "篮球", "足球", "游泳", "骑行", "登山", "户外"],
     ["一只篮球", "一只足球", "一只泳镜", "一辆自行车", "一只登山杖", "一只跳绳",
      "一只哑铃", "一只飞盘", "一只滑板", "一只水壶"]),
]
# 通用兜底池：不命中任何类别时随机挑，刻意不含杯子，保证多样
PROP_GENERIC = [
    "一枚邮票", "一只风筝", "一盒火柴", "一盏灯笼", "一串风铃", "一块肥皂", "一只陀螺", "一本杂志",
    "一个放大镜", "一只蝴蝶标本", "一盒彩色铅笔", "一只小船模型", "一个沙漏", "一只口琴",
    "一本日记", "一个地球仪", "一只拨浪鼓", "一束干花", "一只陶瓷摆件", "一只指南针",
    "一只万花筒", "一块拼图", "一只捕梦网", "一串糖葫芦", "一只纸灯笼", "一块七巧板",
    "一只竹蜻蜓", "一本手账", "一只八音盒", "一只钥匙扣", "一只怀表", "一只小风车",
    "一盒积木", "一只木偶", "一只铃铛", "一只望远镜模型",
]

# 硬黑名单：无论大模型还是关键词规则，只要元素里出现这些饮用容器字样就一律剔除并重新挑选。
# 这是「绝不画杯子」的兜底闸，比靠大模型自觉遵守更可靠（截图里就出现过「一个杯子」漏网）。
BANNED_PROP_TOKENS = ("杯子", "马克杯", "水杯", "茶杯", "咖啡杯", "玻璃杯", "酒杯", "保温杯", "纸杯")



def _prop_is_banned(prop: str) -> bool:
    """元素里出现饮用容器字样就判为违规，绝不进画面。

    除了完整词（杯子/马克杯…），只要含「杯」字也一并拦截——否则「情侣杯」这种
    带「杯」的道具会漏过黑名单、又被模型画成真杯子。
    """
    p = (prop or "").strip()
    if "杯" in p:
        return True
    return any(tok in p for tok in BANNED_PROP_TOKENS)


def _pick_prop(title: str, body: str, used: list) -> str:
    text = (title or "") + "\n" + (body or "")
    matched = []
    for kws, props in PROP_LIB:
        if any(kw in text for kw in kws):
            matched += list(props)
    pool = matched if matched else PROP_GENERIC
    fresh = [p for p in pool if p not in used]
    if not fresh:
        used.clear()
        fresh = pool
    chosen = random.choice(fresh)
    used.append(chosen)
    return chosen


def _wrap_scene(prop: str, fusion: bool) -> str:
    """把「具体道具」包成中层小辅助图描述，区分微缩融合 / 涂鸦两种小人。"""
    who = "一个微缩尺寸的小人" if fusion else "一个涂鸦小人"
    return f"{who}和{prop}，小人的动作直接呼应上层标题的意思，造型简单、线条少"


def _auto_scene(title: str, body: str, used: list, fusion: bool) -> str:
    """scene 没填时的规则兜底：按文案关键词挑一件具体实物道具，避免模型一律画杯子。"""
    return _wrap_scene(_pick_prop(title, body, used), fusion)


def _parse_props(raw: str, n: int) -> list[str]:
    """把模型返回的道具清单解析成干净的短语列表。

    兼容三种形态：纯 JSON 数组、JSON 字符串里套数组（"[\"a\"]"）、以及退化成逐行文本。
    清洗每个元素（剥引号/方括号/逗号、丢无中文的垃圾），最终数量必须严格等于 n，
    否则抛异常让调用方回退规则，绝不带着错位继续。
    """
    raw = raw.strip()
    # 防御性兜底：即便调用方没 sanitize（理论上是 chat_once 已清过），
    # 这里也清掉 ESC 等控制字符，避免污染 json.loads 造成错位
    raw = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", raw).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        arr = json.loads(raw)
    except Exception:
        arr = None
    if isinstance(arr, str):
        try:
            arr = json.loads(arr)
        except Exception:
            arr = None
    if not isinstance(arr, list) or not arr:
        arr = [ln.strip().lstrip("0123456789.、，, ") for ln in raw.splitlines() if ln.strip()]
    if not arr:
        raise RuntimeError("AI 返回的道具清单无法解析")
    clean = []
    for x in arr:
        c = str(x).strip().strip("[]\"'，, \t").strip()
        if c and re.search(r"[一-鿿]", c):   # 必须含至少一个汉字，否则视为垃圾
            clean.append(c)
    if len(clean) != n:
        raise RuntimeError(f"AI 道具数量不符：期望 {n} 实际 {len(clean)} -> {clean}")
    return clean


async def _ai_scene_batch(auto_items: list[dict], fusion: bool) -> list[str]:
    """一次 LLM 调用，给所有空 scene 的分镜各挑一件具体实物道具，返回与 auto_items 等长的列表。

    大模型读完每条文案后自行权衡「相关性 + 整体多样 + 不重复」，比关键词规则更准。
    解析失败或返回不合法时直接抛异常，由调用方回退到关键词规则。
    """
    import pool as _pool
    lines = []
    for i, it in enumerate(auto_items, 1):
        title = (it.get("title") or "").strip()
        body = (it.get("body") or "").strip()
        lines.append(f"{i}. 标题：{title} 正文：{body}")
    prompt = (
        "你是贴图分镜的「小道具搭配师」。下面是一组分镜的标题与正文（按编号顺序）。\n"
        "请为每一条挑一件【具体的、可握在手里的小实物道具】，要求：\n"
        "- 道具要和该条文案的情境或主题强相关（例如讲工资就挑硬币/存钱罐/工资条，讲体检就挑体温计/药盒/瑜伽垫）；\n"
        "- 必须是看得见摸得着的实物，不要抽象概念；\n"
        "- 整体尽量多样，相邻条目不要重复使用同一种东西；\n"
        "- 绝对不要选杯子、马克杯、水杯这类（用户已经看腻了）。\n"
        "只输出一个 JSON 数组，数组长度等于下面条目的数量，每个元素是对应条目的道具短名"
        "（带量词，如「一枚硬币」「一台笔记本电脑」「一卷瑜伽垫」），不要任何解释，也不要 markdown 代码块标记。\n"
        "条目：\n" + "\n".join(lines)
    )
    raw = await _pool.chat_once(prompt, max_tokens=400, temperature=0.5)
    return _parse_props(raw, len(auto_items))


async def fill_scenes(items: list[dict], fusion: bool) -> list[str]:
    """给所有空 scene 的条目挑一件具体实物道具，返回与 items 等长的 scene 列表。

    大模型一次性批量挑（更准、更省调用），不可用（没配号池 / 限流 / 返回非法 / 重试仍失败）
    时静默回退关键词规则。无论哪条路径，返回列表长度一定等于 items 长度，调用方无需再判空。
    """
    used_props: list[str] = []        # 单次出图内已用过的元素，保证相邻分镜不重复
    auto_items = [it for it in items if not (it.get("scene") or "").strip()]
    ai_props: list[str] = []
    if auto_items:
        try:
            ai_props = await _ai_scene_batch(auto_items, fusion)
        except Exception:
            # LLM 不可用（没配号池 / 超时 / 返回非法 / 限流重试仍失败）：静默回退规则匹配
            ai_props = []
    ai_idx = 0
    out: list[str] = []
    for it in items:
        scene = (it.get("scene") or "").strip()
        if not scene:
            # scene 留空：先用大模型挑的道具，挑不到或 LLM 没覆盖这一条时回退关键词规则
            prop = None
            if ai_idx < len(ai_props) and ai_props[ai_idx].strip():
                prop = ai_props[ai_idx].strip()
                ai_idx += 1
            # 硬闸：大模型偶尔仍会漏出「杯子」，命中黑名单直接丢弃，改走关键词规则重挑
            if prop and _prop_is_banned(prop):
                prop = None
            if prop:
                scene = _wrap_scene(prop, fusion)
                used_props.append(prop)
            else:
                scene = _auto_scene(it["title"], it["body"], used_props, fusion)
        out.append(scene)
    return out


def clean_traits(features: str, limit: int = 110) -> str:
    """只留正向特征，砍掉含否定词的分句，过长截断。"""
    segs = re.split(r"[；;。]", features or "")
    keep = []
    for seg in segs:
        seg = seg.strip()
        if not seg or any(m in seg for m in NEG_MARKERS):
            continue
        keep.append(seg)
    # 按分句累加，超长就整句舍弃，不留半句、不加省略号
    out: list[str] = []
    total = 0
    for seg in keep:
        add = len(seg) + (1 if out else 0)
        if out and total + add > limit:
            break
        if not out and len(seg) > limit:
            seg = seg[:limit].rstrip("、，；")
        out.append(seg)
        total += add
    return "；".join(out)


def style_line(style: dict | None) -> str:
    """Loose Ink-and-Wash Storybook（参考 Quentin Blake：抖动墨线、松散水彩…）。"""
    if not style:
        return ""
    name = (style.get("name_en") or "").strip()
    ref = (style.get("ref") or "").strip()
    traits = clean_traits(style.get("features", ""))
    inner = ""
    if ref and traits:
        inner = f"参考 {ref}：{traits}"
    elif ref:
        inner = f"参考 {ref}"
    elif traits:
        inner = traits
    if not name and not inner:
        return ""
    if name and inner:
        return f"{name}（{inner}）。"
    return f"{name or inner}。"


def color_line(color: dict | None) -> str:
    if not color:
        return ""
    return (color.get("prompt") or "").strip()


def layout_line(layout: dict | None) -> str:
    if not layout:
        return ""
    text = (layout.get("prompt") or "").strip()
    if text and not text.endswith(("。", "！", "》", "」")):
        text += "。"
    return text


def clean_title(raw: str) -> str:
    """01｜先存后花 / **01｜先存后花** / 【01｜先存后花】 → 01 先存后花"""
    t = (raw or "").strip()
    t = t.strip("*＊【】[] ")
    t = re.sub(r"[｜|]", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def short_body(body: str, limit: int = 62) -> str:
    """副标题槽位只用正文第一句；太长就在句内标点处收掉，别让图里塞一整段。"""
    t = re.sub(r"\s+", "", body or "")
    if not t:
        return ""
    parts = re.split(r"(?<=[。！？!?])", t)
    first = (parts[0] if parts and parts[0] else t).strip()
    if len(first) <= limit:
        return first
    cut = first[:limit]
    for mark in ("，", "、", "；", "："):
        i = cut.rfind(mark)
        if i > limit * 0.45:
            return cut[:i] + "。"
    return cut + "。"


def build_slots(title: str, body: str, scene: str, layout: dict | None) -> str:
    """文字槽位。三层版式按「上层主标题 / 中层小辅助图 / 下层副标题」写，
    其他图型退回「主标题 / 正文」，免得槽位和图型打架。"""
    three = bool(layout) and "三层" in (layout.get("prompt") or "")
    sub = short_body(body)
    bits = []
    if three:
        if title:
            bits.append(f"上层主标题文字：「{title}」")
        bits.append("中层小辅助图：" + (scene.strip() or SCENE_FALLBACK))
        if sub:
            bits.append(f"下层副标题文字：「{sub}」")
    else:
        if title:
            bits.append(f"主标题文字：「{title}」")
        if sub:
            bits.append(f"正文文字：「{sub}」")
    if not bits:
        return ""
    out = "。".join(bits)
    if not out.endswith(("」", "。")):
        out += "。"
    return out


def parse_moods(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        tokens = []
        for part in raw:
            tokens += re.split(r"[、,，/\s]+", str(part))
    else:
        tokens = re.split(r"[、,，/\s]+", str(raw))
    seen, out = set(), []
    for t in tokens:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def build_instruction(sel: dict, ratio: str, moods: list[str], fusion: bool, whitespace: str,
                      ref: bool = False, image_mode: str = "graphic-text", shoot: str = "") -> str:
    """一行短指令，形如：图文模式，图型：SC-002，风格：FB-001，主题色：C-26，画幅比例：3:4，情绪：治愈、幽默"""
    bits = ["摄影写真模式" if image_mode == "photo" else "图文模式"]
    if ref:
        bits.append("参考图：已上传")
    layout = sel.get("layout")
    style = sel.get("style")
    color = sel.get("color")
    # 摄影模式不套图型版式：图型模板写的是「主标题/小辅助图/副标题」的位置，和写真不排字冲突
    if layout and image_mode != "photo":
        bits.append(f"图型：{layout['id']}")
    if style:
        bits.append(f"风格：{style['id']}")
    if color:
        bits.append(f"主题色：{color['id']}")
    # 三样都可以不选：说明这一轮靠什么出图，避免指令读起来像漏了参数
    if not layout and not style and not color:
        bits.append("图型 / 风格 / 主题色：跟参考图" if ref else "图型 / 风格 / 主题色：不限")
    if ratio:
        bits.append(f"画幅比例：{ratio}")
    if moods:
        bits.append("情绪：" + "、".join(moods))
    if image_mode == "photo" and shoot:
        bits.append(f"拍摄类型：{shoot}")
    if fusion:
        bits.append("缩微摄影融合")
    if whitespace == "moderate":
        bits.append("留白：大量")
    elif whitespace == "high":
        bits.append("留白：极简")
    return "，".join(bits)


def _ids_to_names(instr: str, sel: dict) -> str:
    """手改指令里的编号换成自然语言名，遵守「提示词不出现编号」的规矩。"""
    layout = sel.get("layout")
    style = sel.get("style")
    color = sel.get("color")
    if layout:
        instr = instr.replace(f"图型：{layout['id']}", f"图型：{(layout.get('title') or '').strip()}")
    if style:
        instr = instr.replace(f"风格：{style['id']}",
                              f"风格：{(style.get('name_en') or style.get('name') or '').strip()}")
    if color:
        instr = instr.replace(f"主题色：{color['id']}", f"主题色：{(color.get('name') or '').strip()}")
    return instr


def build_prompt(item: dict, sel: dict, ratio: str, moods: list[str],
                 fusion: bool, whitespace: str, note: str = "", ref: bool = False,
                 image_mode: str = "graphic-text", shoot: str = "", instr: str = "",
                 ref_mode: str = "style", visual: str = "") -> str:
    """单条完整提示词。item = {title, body}"""
    style = sel.get("style")
    layout = sel.get("layout")
    color = sel.get("color")
    title = clean_title(item.get("title") or "")
    body = (item.get("body") or "").strip()
    scene = (item.get("scene") or "").strip()

    blocks: list[str] = []

    # 注意：参考图约束（REF_FUSION / REF_FUSION_FULL）是「内部指令」，**不**写进这里返回的
    # 展示用提示词。它只在真正出图（app.build_image_prompt）时才拼到发给模型的文本前面，
    # 避免用户可编辑的提示词里混进一条像「系统代码」的指令，也避免用户手改时误删约束。

    # 手改过的出图指令原样进提示词（编号已换成自然语言名），用户改了什么就生效什么
    if instr:
        blocks.append(_ids_to_names(instr, sel))

    head = style_line(style) + color_line(color)
    if head:
        blocks.append(head)

    layout_text = "" if image_mode == "photo" else layout_line(layout)
    if layout_text:
        blocks.append(layout_text)

    # 缩微摄影融合在图文 / 摄影两种模式下都生效（用户 2026-11-05 定）
    if fusion:
        blocks.append(MICRO_FUSION)

    if note.strip():
        blocks.append(f"补充要求：{note.strip()}。")

    if image_mode == "photo":
        # 摄影模式：上游提示词里没有文字槽位，分镜的标题/正文只当画面内容
        blocks.append(PHOTO_LEAD)
        if shoot:
            blocks.append(f"拍摄类型：{shoot}。")
        blocks.append(PHOTO_KEYWORDS)
        subject = re.sub(r"^0*\d{1,3}[\s.．、,，:：｜|\-—]+", "", title).strip() or title
        # 分镜标题/正文/小辅助图都当画面内容；各自收掉句末标点，避免「。。」
        scene_bits = [re.sub(r"[。！？\.!?，,；;]+$", "", x).strip() for x in (subject, body, scene)]
        scene_bits = [x for x in scene_bits if x]
        if scene_bits:
            blocks.append("画面：" + "。".join(scene_bits) + "。")
        elif ref:
            # 只传了参考图、没写任何文案：不给主体的话模型只能照抄原图
            blocks.append("画面：沿用参考图里的主体与场景，重新组织构图拍一张。")
    else:
        slots = build_slots(title, body, scene, layout)
        if slots:
            blocks.append(slots)
            blocks.append("文字清晰可读，文字参与构图，不要出现多余文字。")
        elif ref:
            # 只上传参考图、不选图型风格主题色（用户 2026-11-08 要求）：没有文案可排，
            # 就让参考图当唯一依据，重新组织构图出一张完整画面，别把原图原样复刻
            blocks.append(
                "没有文案：画面主体与画风都取自参考图，重新组织构图、环境与动作，"
                "画一张完整的插画，画面里不排文字。"
            )

    # 小说拆分给出的画面表演要点：只描述画面，不进文字槽位。
    # 老链路不带 visual，这一块不会出现，提示词逐字保持原样。
    v = str(visual or "").strip()
    if v:
        blocks.append("画面表演（只描述画面，不要排成文字）：" + v.rstrip("。！？.!?") + "。")

    tail = []
    if moods:
        tail.append("情绪：" + "、".join(moods))
    if color:
        tail.append(f"{color.get('name') or ''}作统一色调点缀")
    if ratio:
        tail.append(f"画幅比例 {ratio}")
    tail = [t for t in tail if t and t.strip("：")]
    if tail:
        blocks.append("。".join(tail) + "。")

    if str(ratio or "").strip() == WIDE_CROP_RATIO:
        blocks.append(WIDE_CROP_HINT)

    if whitespace in WHITESPACE:
        blocks.append(WHITESPACE[whitespace])

    blocks.append(NO_CUP_RULE)
    blocks.append(TAIL_INSTRUCTION)
    return "\n".join(b for b in blocks if b)


async def compose(payload: dict, styles: list[dict], layouts: list[dict], colors: list[dict]) -> dict:
    """组装全部条目。payload 里 items 为空时用 topic 兜底生成一条。

    scene 留空时优先让大模型读懂文案挑一件实物道具（一次批量调用、整体多样），
    调用失败则回退关键词规则，保证永远有元素、且绝不像以前那样一律画杯子。
    """

    def pick(items, item_id):
        if not item_id:
            return None
        return next((x for x in items if x["id"] == item_id), None)

    sel = {
        "style": pick(styles, payload.get("style_id")),
        "layout": pick(layouts, payload.get("layout_id")),
        "color": pick(colors, payload.get("color_id")),
    }
    ratio = (payload.get("ratio") or "").strip()
    moods = parse_moods(payload.get("mood"))
    fusion = bool(payload.get("fusion"))
    whitespace = (payload.get("whitespace") or "normal").strip()
    note = (payload.get("note") or "").strip()
    ref = bool(payload.get("ref"))
    ref_mode = (payload.get("ref_mode") or "style").strip()  # style=只借画风 | full=完整复刻
    # 出图模式：默认图文，和以前完全一样；只有显式传 photo 才走摄影分支
    image_mode = (payload.get("image_mode") or "graphic-text").strip()
    if image_mode not in PHOTO_MODES:
        image_mode = "graphic-text"
    shoot = (payload.get("shoot_type") or "").strip()[:40] if image_mode == "photo" else ""
    # allow_blank：左侧没写标题/正文时也允许出提示词（纯指令出图）；instruction：手改指令
    allow_blank = bool(payload.get("allow_blank"))
    custom_instr = (payload.get("instruction") or "").strip()

    raw_items = payload.get("items") or []
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        title = (raw.get("title") or "").strip()
        body = (raw.get("body") or "").strip()
        scene = (raw.get("scene") or "").strip()
        visual = (raw.get("visual") or "").strip()
        if not title and not body and not allow_blank:
            continue
        items.append({"title": title, "body": body, "scene": scene, "visual": visual})

    if not items:
        topic = (payload.get("topic") or "").strip()
        if topic:
            lines = topic.split("\n", 1)
            items.append({"title": lines[0].strip(), "body": lines[1].strip() if len(lines) > 1 else "", "scene": ""})

    instruction = custom_instr or build_instruction(
        sel, ratio, moods, fusion, whitespace, ref, image_mode, shoot)
    out_items = []
    # scene 留空的条目统一交给 fill_scenes：大模型批量挑道具，失败静默回退关键词规则，
    # 保证永远有元素、绝不像以前那样一律画杯子。拆分镜 / 生成提示词都复用同一份逻辑。
    scenes = await fill_scenes(items, fusion)
    for idx, item in enumerate(items, 1):
        scene = scenes[idx - 1]
        item["scene"] = scene
        out_items.append({
            "index": idx,
            "title": item["title"],
            "body": item["body"],
            "scene": scene,
            "visual": item["visual"],
            "instruction": instruction,
            "prompt": build_prompt(item, sel, ratio, moods, fusion, whitespace, note, ref,
                                   image_mode, shoot, instr=custom_instr, ref_mode=ref_mode,
                                   visual=item["visual"]),
        })
    return {
        "instruction": instruction,
        "items": out_items,
        "selection": {
            "style": sel["style"]["id"] if sel["style"] else None,
            "layout": sel["layout"]["id"] if sel["layout"] else None,
            "color": sel["color"]["id"] if sel["color"] else None,
        },
    }
