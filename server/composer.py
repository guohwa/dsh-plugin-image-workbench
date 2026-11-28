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

# 官方技能包（handdraw-painter/SKILL.md「Prompt modes」）的图文固定后缀，一个字都不许改。
# 它同时规定了三件事：文案可以由模型升华、概念主题要模型自己设计画面隐喻、文字必须参与构图。
# 工作台以前用自己的「文字清晰可读，不要出现多余文字」代替它，结果画面退化成图示卡片
# （用户 2026-11-26：同一句主题、同一个图型，官方技能出的图明显更像小林漫画）。
GRAPHIC_SUFFIX = (
    "【如果主题直白包含画面元素那就按主题出图，文案由你来升华，但是不要直接描述画面。 "
    "如果主题比较概念化，那么文案和主题尽量保持一致，如果文案较长由你提炼，"
    "由你先设计画面隐喻（人类和非人类都行）再出图   。    文字参与构图，图文一体】"
)

# 缩微摄影跨媒介融合：角色小、场景真、同光源同空间
MICRO_FUSION = (
    "缩微摄影跨媒介融合：角色尺寸异常小、场景正常尺寸。"
    "场景视觉语言＝暖调写实生活摄影（真实材质、可信光影）。"
    "角色与场景同空间同光源，有可信接触、投影、遮挡，角色真正活在场景里而不是贴纸。"
)

# 线稿 / 白底 / 单色类画风（小林漫画、社论漫画、极简线描这一批）跟上面那条直接冲突：
# 它要求「暖调写实生活摄影」，模型就照做，线稿被改成带光影的暖色插画，用户选了风格却出成另一种画风。
# 同一套融合只保留「微缩比例 + 同光同源」这两件事，画风以所选风格为准。
MICRO_FUSION_INK = (
    "缩微比例融合：角色尺寸异常小、场景正常尺寸。"
    "画风以所选风格为准，保持它的白底、线稿、单色与大面积留白，"
    "不要改成写实摄影，不要加摄影质感、暖色滤镜和环境光渲染。"
    "角色与场景同空间同光源，有可信接触与遮挡，线条与场景是同一支笔画的。"
)

# 图型自己要求「纯色背景 / 大面积留白」时，上面那条「暖调写实生活摄影」就自相矛盾了：
# 版式要留白卡片，融合句却要求真实场景与实景光影，模型只能二选一，结果出成一张实拍照片
# （用户 2026-11-26：小红书图文卡变成桌面笔记本实拍）。这类图型只保留微缩比例与同光同源。
MICRO_FUSION_FLAT = (
    "缩微比例融合：角色尺寸异常小、道具正常尺寸。"
    "背景严格按图型要求保持纯色或留白，不铺实景环境，不要摄影质感与暖色滤镜。"
    "角色与道具同光源、有可信接触与投影，真正处在同一个空间里，而不是贴上去的贴纸。"
)

# 判定线稿类画风：features/name 里出现了线稿特征词，且不是摄影/胶片那一挂
_INK_HINTS = ("线稿", "白底", "黑白", "单色", "水墨", "毛笔", "简笔", "墨线",
              "勾线", "素描", "版画", "剪影", "线描")
_INK_EXCLUDE = ("摄影", "写真", "实拍", "照片", "胶片", "镜头")

# 图型文案里出现这些词，说明这张卡要的是干净的纯色留白底，不能套实景摄影
_FLAT_LAYOUT_HINTS = ("纯色背景", "纯白背景", "纯黑背景", "纯色底", "留白背景",
                      "大面积留白", "较多留白", "大量留白")


def is_flat_layout(layout: dict | None) -> bool:
    """这张图型是不是要求纯色 / 留白底。"""
    text = str((layout or {}).get("prompt") or "")
    return any(k in text for k in _FLAT_LAYOUT_HINTS)


def is_three_layer(layout: dict | None) -> bool:
    """三层版式（上层主标题 / 中层小辅助图 / 下层副标题）单独走自己的槽位写法。"""
    return bool(layout) and "三层" in str((layout or {}).get("prompt") or "")


def is_ink_style(style: dict | None) -> bool:
    """这条风格是不是「白底线稿」那一类，需要把融合从写实摄影改成同笔触。"""
    if not style:
        return False
    # 摄影组的「黑白」是黑白照片，不是黑白线稿（如 FI-015 黑白肖像杂志）
    group = str(style.get("group_name") or "")
    if "摄影" in group or "写实" in group:
        return False
    text = (str(style.get("features") or "") + " " + str(style.get("name_en") or "")).lower()
    return any(k in text for k in _INK_HINTS) and not any(k in text for k in _INK_EXCLUDE)


# 纸白类主题色：白底线稿画风要的是「旧纸的暖白」，不是印刷白
_PAPER_COLORS = ("奶油白", "象牙白", "米驼色")


def default_color(style: dict | None, layout: dict | None, colors: list[dict],
                  image_mode: str = "graphic-text") -> dict | None:
    """没选主题色时的兜底推荐，只救一种场面：白底线稿 + 纯色留白卡。

    图型写着「使用纯色背景」，画风又要求「白底」，但没给颜色时模型只能自己挑，
    一句治愈文案被铺成蓝底（用户 2026-11-26）。这里补一个同调的纸白，
    其余组合一律不擅自替用户选色。
    """
    if image_mode != "graphic-text" or not is_ink_style(style) or not is_flat_layout(layout):
        return None
    for want in _PAPER_COLORS:
        for c in colors or []:
            if want in str(c.get("name") or ""):
                return c
    return None

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
# 纯图模式：只出画面，不排任何文字，也不套图型里的标题/标签版式（用户 2026-11-26）
PURE_LEAD = "纯图模式。"
PURE_NO_TEXT = "画面里不要出现任何文字、标题、标签、标语、水印或对白气泡。"
# 合法模式按「纯图 / 图文 / 摄影」排列；PHOTO_MODES 保留旧名给老调用方，语义等于全部模式
IMAGE_MODES = ("pure-image", "graphic-text", "photo")
PHOTO_MODES = IMAGE_MODES

# 主图（hero / 首图）的硬约束：scene 层有黑名单拦截，但主图提示词里没有禁杯规则，
# 模型自由发挥时把杯子画进了主图（截图里就出现过 NVIDIA 马克杯）。这条补在主图尾部，
# 纯图 / 图文 / 摄影三种模式都生效，和 BANNED_PROP_TOKENS 形成双层保险。
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
SCENE_FALLBACK = "一个涂鸦小人和一件具体实物道具，动作直接对应文案的意思，造型简单、线条少"

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



# 上游偶发把 ESC 键渲染成字面标签（<escape> / </escape>），JSON 解析失败走按行兜底时，
# 这些残片会连着被切断的引号、方括号一起当成道具名，最后原样印进用户能看到的提示词。
# 拼词这一层再兜一次：不管字段从哪儿来，进提示词前都不许带标签。
_MARKUP_TAG_RE = re.compile(r"</?\s*esc(?:ape)?\b[^<>]{0,40}>", re.I)
_MARKUP_ENT_RE = re.compile(r"&lt;\s*/?\s*esc(?:ape)?\b[^&<>]{0,40}&gt;", re.I)
# 首尾孤立的分隔符：多半是被切断的 JSON（如 `["一本旧书" ]`）
_STRAY_EDGE_RE = re.compile(
    r"^[\s\"'“”‘’\[\]{}<>，,、；;：:]+|[\s\"'“”‘’\[\]{}<>，,、；;：:]+$")


def clean_markup(text: str) -> str:
    """去掉 ESC 残片标签与终端控制字符，保留文字本身。"""
    s = str(text or "")
    s = _MARKUP_TAG_RE.sub("", s)
    s = _MARKUP_ENT_RE.sub("", s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    return s.strip()


def tidy_phrase(text: str) -> str:
    """道具名 / 画面描述收尾：剥掉标签残片，再反复削掉两边的引号和方括号。"""
    s = clean_markup(text)
    prev = None
    while s and s != prev:
        prev = s
        s = _STRAY_EDGE_RE.sub("", s).strip()
    return s


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
    prop = tidy_phrase(prop)
    if not prop:
        prop = "一件具体实物道具"
    return f"{who}和{prop}，小人的动作直接呼应文案的意思，造型简单、线条少"


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
        c = tidy_phrase(str(x))
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
        scene = tidy_phrase(it.get("scene") or "")
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


def _looks_like_person(name: str) -> bool:
    """ref 是不是「具体人名」（相对「手绘漫画」这类泛化风格描述）。

    命中才从 name_en / features 里剔除，避免误伤本来就是画风词的中文 ref。
    """
    s = str(name or "").strip()
    if not s:
        return False
    # 拉丁人名：两词以上（Gemma Correll / Jean-Jacques Sempé），或带连字符
    if re.search(r"[A-Za-z]", s):
        return len(re.findall(r"[A-Za-z][A-Za-z\-']*", s)) >= 2
    # 日文假名人名：なっくす / きくちあつこ
    if re.search(r"[\u3040-\u30ff]", s):
        return True
    # 中文人名：2–4 字、不含常见画风词
    generic = ("风格", "插画", "漫画", "绘本", "手绘", "风", "画", "卡通", "写实", "扁平",
               "线稿", "水墨", "油画", "水彩", "涂鸦", "简笔", "像素", "国潮", "复古")
    return 2 <= len(s) <= 4 and not any(g in s for g in generic)


def strip_ref_name(text: str, ref: str) -> str:
    """从给模型看的文本里剔除具体作者名，保留其余画风描述。

    catalog 有些 style 的 name_en / features 自带作者名（如 FB-035
    "Charlie Mackesy Expressive Ink & Watercolor"），只去掉 ref 字段还不够。
    """
    s = str(text or "")
    if not _looks_like_person(ref):
        return s
    s = re.sub(re.escape(ref), "", s, flags=re.I)
    # 删干净后可能留下重复空格 / 句首冒号 / 悬空括号
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"^[：:、，,\s]+", "", s)
    s = re.sub(r"（\s*）|\(\s*\)", "", s).strip()
    return s


def clean_traits(features: str, limit: int | None = None) -> str:
    """只留正向特征，砍掉含否定词的分句；limit 为空表示不截断。

    以前默认截到 110 字，小林漫画那种排在后面的「脸颊、衣服或小道具加入少量红黄绿局部平涂」
    和「中国写意人物画与老式讽刺漫画的味道」正好被砍掉，风格辨识度就没了（用户 2026-11-26）。
    官方技能包是整段照用的，这里也默认整段保留。
    """
    segs = re.split(r"[；;。]", features or "")
    keep = []
    for seg in segs:
        seg = seg.strip()
        if not seg or any(m in seg for m in NEG_MARKERS):
            continue
        keep.append(seg)
    if limit is None:
        return "；".join(keep)
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
    """风格名（视觉特征）。

    绝不写 style.ref：那是 catalog 里的具体画家 / 作品名（Quentin Blake、Gemma
    Correll、三体……），喂给上游会触发「相似内容 / 版权」审核返回 400。
    1.1.126 的更新说明写过「统一改成不点名的风格描述」，但这里一直没改，
    说明与代码对不上（2026-11-29 发现）。ref 只留在 catalog 给界面展示用。
    """
    if not style:
        return ""
    ref = str(style.get("ref") or "").strip()
    name = strip_ref_name((style.get("name_en") or "").strip(), ref)
    traits = strip_ref_name(clean_traits(style.get("features", "")), ref)
    if not name and not traits:
        # 名字整个就是作者名时（如 FB-035），退回用风格 ID 当泛化标识
        name = str(style.get("id") or "").strip()
    if not name and not traits:
        return ""
    if name and traits:
        return f"{name}（{traits}）。"
    return f"{name or traits}。"


def color_line(color: dict | None) -> str:
    if not color:
        return ""
    return (color.get("prompt") or "").strip()


# 少数图型的 prompt 是「提示词生成模板 + 用户交互规范 + 画面指令」混写的，
# 后半段写给写词的人 / 前端 UI 看（类型枚举、点选序号、反引号代码示例），
# 不是画面指令。喂给生图模型只会干扰出图（用户 2026-11-28 反馈 SC-021 提示词过长）。
# 170 个图型里目前只有 SC-021 含这些标记，命中即从标记处截断，只留画面指令。
_LAYOUT_NON_VISUAL_MARKS = (
    "交互点选输出规范",
    "七大核心转译类型体系",
    "每次输出单义明确，并在文末附带编号清单供用户点选",
)


def layout_line(layout: dict | None) -> str:
    if not layout:
        return ""
    text = (layout.get("prompt") or "").strip()
    cuts = [text.find(k) for k in _LAYOUT_NON_VISUAL_MARKS]
    cuts = [c for c in cuts if c > 0]
    if cuts:
        text = text[:min(cuts)]
        # 砍点常落在「3. 七大…」的序号前，收掉悬空的序号，别留半截「3.」
        text = re.sub(r"(?:\s*\d+[.．、]\s*)+$", "", text).rstrip()
    if text and not text.endswith(("。", "！", "》", "」")):
        text += "。"
    return text


def _as_label(value: str) -> str:
    """标签值收尾：去掉自带的句号，避免拼出「。。」。"""
    return tidy_phrase(str(value or "")).rstrip("。．.；;，, ")


def graphic_head(style: dict | None, color: dict | None, ratio: str,
                 layout: dict | None, moods: list[str] | None = None) -> list[str]:
    """图文模式的固定开头，字段与顺序照官方技能包逐项对齐：

    风格名称 → 参考作者/风格名称 → 主题色 → 画幅比例 → 排版要求 → 核心风格特征 → 情绪基调

    顺序不是随便排的。官方那套提示词按这个顺序喂给模型，同一图型、同一句主题，
    工作台以前把风格名和特征揉进一个括号、把画幅和情绪挤到最后一行、特征还被截到 110 字，
    出来的图就和官方差一截（用户 2026-11-26 对比截图）。
    """
    out: list[str] = []
    traits = ""
    if style:
        ref = str(style.get("ref") or "").strip()
        name_en = _as_label(strip_ref_name(style.get("name_en") or style.get("name") or "", ref))
        traits = strip_ref_name(clean_traits(style.get("features", "")), ref)
        if not name_en:
            name_en = str(style.get("id") or "").strip()
        if name_en:
            out.append(f"风格名称：{name_en}。")
        # 不再输出「参考作者/风格名称：{ref}」：catalog 的 ref 是真实画家 / 作品名，
        # 发上游会撞内容审核 400（2026-11-29）。画风由 name_en + features 完整表达。
        # 字段顺序里少了这一项没关系，核心风格特征在下面照样输出。
    if color:
        line = color_line(color)
        if line:
            out.append(line)
    if str(ratio or "").strip():
        out.append(f"画幅比例：{str(ratio).strip()}。")
    lay = layout_line(layout)
    if lay:
        out.append(f"排版要求：{lay}")
    if traits:
        out.append(f"核心风格特征：{traits}。")
    if moods:
        out.append("情绪基调：" + "、".join(moods) + "。")
    return out


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
    three = is_three_layer(layout)
    sub = short_body(body)
    bits = []
    if three:
        if title:
            bits.append(f"上层主标题文字：「{title}」")
        bits.append("中层小辅助图：" + (tidy_phrase(scene) or SCENE_FALLBACK))
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
    mode_name = {"photo": "摄影写真模式", "pure-image": "纯图模式"}.get(image_mode, "图文模式")
    bits = [mode_name]
    if ref:
        bits.append("参考图：已上传")
    layout = sel.get("layout")
    style = sel.get("style")
    color = sel.get("color")
    # 纯图不排字、也不套版式，所以不带图型；摄影带图型——官方 skill 的摄影指令同样写
    # 「图型：SC-008」，漏了等于选了没用（用户 2026-11-27 对比官方截图发现）
    if layout and image_mode != "pure-image":
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


def _photo_layout_line(layout: dict | None) -> str:
    """摄影模式下的图型：只留构图分区，把所有排字要求剥掉。

    摄影不排字，但图型仍要影响画面（上/下分区、留白多少、主体占比）。
    直接搬 layout.prompt 会要求画标题/标签，跟「画面里不出现任何文字」打架，
    所以按逗号切句、丢掉含标题/文案/标签/排版的分句（用户 2026-11-27 对比官方 skill）。
    """
    if not layout:
        return ""
    name = str(layout.get("title") or layout.get("id") or "").strip()
    raw = str(layout.get("prompt") or "").strip()
    parts = [p.strip() for p in re.split(r"[，。；,;]", raw) if p.strip()]
    drop = ("标题", "文案", "文字", "标签", "字号", "一行", "排版", "标注", "对白")
    keep = [p for p in parts if not any(k in p for k in drop)]
    body = "，".join(keep)
    # 有的图型描述通篇都在讲排什么字（三层标题卡之类），剥完一句不剩：
    # 那就只留版式名，让模型按这个名字的分区意识构图，别把整行吞掉
    if body:
        return f"构图版式：{name}。{body}。画面里不排任何文字。"
    return f"构图版式：{name}。按这个版式的分区组织画面，画面里不排任何文字。" if name else ""


def build_prompt(item: dict, sel: dict, ratio: str, moods: list[str],
                 fusion: bool, whitespace: str, note: str = "", ref: bool = False,
                 image_mode: str = "graphic-text", shoot: str = "", instr: str = "",
                 ref_mode: str = "style", visual: str = "", scene_manual: bool = True) -> str:
    """单条完整提示词。item = {title, body, scene}

    scene_manual=False 表示画面描述不是用户填的，而是系统按文案自动挑的道具。
    纯色留白的社媒卡上，这种自动道具会把画面钉死在道具上，所以不写进提示词。
    """
    style = sel.get("style")
    layout = sel.get("layout")
    color = sel.get("color")
    title = clean_title(clean_markup(item.get("title") or ""))
    body = clean_markup(item.get("body") or "").strip()
    # 画面描述可能来自 AI 挑道具、用户手填或旧草稿，进去前统一清掉标签残片
    scene = tidy_phrase(item.get("scene") or "")

    blocks: list[str] = []

    # 注意：参考图约束（REF_FUSION / REF_FUSION_FULL）是「内部指令」，**不**写进这里返回的
    # 展示用提示词。它只在真正出图（app.build_image_prompt）时才拼到发给模型的文本前面，
    # 避免用户可编辑的提示词里混进一条像「系统代码」的指令，也避免用户手改时误删约束。

    # 手改过的出图指令原样进提示词（编号已换成自然语言名），用户改了什么就生效什么
    if instr:
        blocks.append(_ids_to_names(instr, sel))

    if image_mode in ("photo", "pure-image"):
        # 纯图也按官方那套字段顺序写（风格名称 / 参考作者 / 主题色 / 画幅 / 核心特征 / 情绪），
        # 只是不写「排版要求」、不加图文后缀。以前它单独走 style_line 把三者揉进一个括号，
        # 同一句主题在纯图下拼出来的文本和官方差一整段（用户 2026-11-26 截图）。
        if image_mode == "pure-image":
            blocks += graphic_head(style, color, ratio, None, moods)
        else:
            head = style_line(style) + color_line(color)
            if head:
                blocks.append(head)

    # 缩微比例融合：开关开着就统一带同一条，不再按图型 / 风格挑文案（用户 2026-11-28 明确）。
    # 系统不替用户判断「这张图该不该带融合、该带哪条」——效果好不好用户自己看，
    # 不满意他把前端那个融合勾去掉即可。以前按 is_ink_style / is_flat_layout 分流，
    # SC-021 被判成留白卡套上「不要摄影质感」，和它自己的超写实摄影要求打架。
    fusion_block = MICRO_FUSION_FLAT if fusion else ""

    if image_mode in ("photo", "pure-image"):
        if fusion_block:
            blocks.append(fusion_block)
        if note.strip():
            blocks.append(f"补充要求：{note.strip()}。")
        # 摄影 / 纯图：上游提示词里没有文字槽位，分镜的标题/正文只当画面内容
        if image_mode == "photo":
            blocks.append(PHOTO_LEAD)
            if shoot:
                blocks.append(f"拍摄类型：{shoot}。")
            # 图型选了就得在画面里生效：给构图分区，但不许排字（用户 2026-11-27）
            layout_line = _photo_layout_line(layout)
            if layout_line:
                blocks.append(layout_line)
            blocks.append(PHOTO_KEYWORDS)
        # 纯图：「纯图模式。」这种系统术语模型读不懂，直接由下面那条「不要出现任何文字」说话
        subject = re.sub(r"^0*\d{1,3}[\s.．、,，:：｜|\-—]+", "", title).strip() or title
        # 分镜标题/正文/小辅助图都当画面内容；各自收掉句末标点，避免「。。」
        scene_bits = [re.sub(r"[。！？\.!?，,；;]+$", "", x).strip() for x in (body, scene)]
        scene_bits = [x for x in scene_bits if x]
        if image_mode == "pure-image":
            # 纯图不排字，没有「主题 / 正文」之分，标题和正文都是画面内容，合成一条才完整
            bits = [x for x in [subject] + scene_bits if x]
            paint = "。".join(dict.fromkeys(bits))
            if paint:
                blocks.append("画面：" + paint + "。")
            elif ref:
                blocks.append("画面：沿用参考图里的主体与场景，重新组织构图，"
                              "画一张完整的画面，画面里不排文字。")
            blocks.append(PURE_NO_TEXT)
        else:
            paint = "。".join(scene_bits)
            if paint:
                blocks.append("画面：" + paint + "。")
            elif ref:
                # 只传了参考图、没写任何文案：不给主体的话模型只能照抄原图
                blocks.append("画面：沿用参考图里的主体与场景，重新组织构图，"
                              "画一张完整的画面，画面里不排文字。")
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
        # 实拍里道具穿帮最扎眼（出现过 NVIDIA 马克杯），只在摄影模式兜这一条
        if image_mode == "photo":
            blocks.append(NO_CUP_RULE)
        blocks.append(TAIL_INSTRUCTION)
        return "\n".join(b for b in blocks if b)

    # ---- 图文：字段与顺序和官方技能包逐项对齐 ----
    blocks += graphic_head(style, color, ratio, layout, moods)

    three = is_three_layer(layout)
    subject = re.sub(r"^0*\d{1,3}[\s.．、,，:：｜|\-—]+", "", title).strip()
    if three:
        # 三层版式要精确控制三行字，仍走槽位写法，不再加「文案由你升华」的后缀
        if scene:
            pass        # 中层小辅助图已经在槽位里，不重复加画面行
        slots = build_slots(title, body, scene, layout)
        if slots:
            blocks.append(slots)
            blocks.append("文字清晰可读，文字参与构图，不要出现多余文字。")
        elif ref:
            blocks.append(
                "没有文案：画面主体与画风都取自参考图，重新组织构图、环境与动作，"
                "画一张完整的插画，画面里不排文字。")
    else:
        # 主题：官方写法是「主题：」+ 用户原话，文案的取舍交给固定后缀
        theme = subject or body
        # 单图快出只有一个 keyword 框、没有独立标题：这句 keyword 既是画面主体，
        # 也是要排进图里的字。官方图文写法必须两行都给：
        #   「主题：」告诉模型画什么，「正文文字：」告诉模型把哪句话排进图里。
        # 2026-11-28 曾把这两行当「同一句重复」删成只剩主题，结果出图全是画面没有字
        # ——缺的正是「正文文字」这条排字指令。内容即使一样，两行也不能省。
        if body and body != theme:
            blocks.append(f"主题：{theme.rstrip('。')}。正文文字：{body.rstrip('。')}。")
        elif theme:
            blocks.append(f"主题：{theme.rstrip('。')}。正文文字：{theme.rstrip('。')}。")
        # 纯色留白的社媒卡不硬塞「画面：一个小人和一件道具」——
        # 那句话会把模型钉在道具上（一句关于忘记的话被画成一页日历），
        # 隐喻交给固定后缀里的「由你先设计画面隐喻」来处理（用户 2026-11-26 对比官方效果）。
        # 用户自己填的画面描述仍然照用，那是明确指令。
        inject_scene = bool(scene) and (scene_manual or not is_flat_layout(layout))
        if inject_scene:
            blocks.append("画面：" + scene.rstrip("。") + "。")
        if not theme and not scene and ref:
            blocks.append(
                "没有文案：画面主体与画风都取自参考图，重新组织构图、环境与动作，"
                "画一张完整的插画，画面里不排文字。")

    if fusion_block:
        blocks.append(fusion_block)
    if note.strip():
        blocks.append(f"补充要求：{note.strip()}。")

    # 小说拆分给出的画面表演要点：只描述画面，不进文字槽位。
    v = str(visual or "").strip()
    if v:
        blocks.append("画面表演（只描述画面，不要排成文字）：" + v.rstrip("。！？.!?") + "。")

    if str(ratio or "").strip() == WIDE_CROP_RATIO:
        blocks.append(WIDE_CROP_HINT)

    if whitespace in WHITESPACE:
        blocks.append(WHITESPACE[whitespace])

    if not three:
        blocks.append(GRAPHIC_SUFFIX)
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
    # 出图模式：默认图文，和以前完全一样；显式传 pure-image / photo 才走对应分支
    image_mode = (payload.get("image_mode") or "graphic-text").strip()
    if image_mode not in IMAGE_MODES:
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
        scene = tidy_phrase(raw.get("scene") or "")
        visual = (raw.get("visual") or "").strip()
        if not title and not body and not allow_blank:
            continue
        items.append({"title": title, "body": body, "scene": scene,
                      "scene_manual": bool(scene), "visual": visual})

    if not items:
        topic = (payload.get("topic") or "").strip()
        if topic:
            lines = topic.split("\n", 1)
            items.append({"title": lines[0].strip(), "body": lines[1].strip() if len(lines) > 1 else "",
                          "scene": "", "scene_manual": False, "visual": ""})

    # 没选主题色时，纯色留白的线稿卡补一个同调的纸白。
    # 不补的话「纯色背景」这一条只能让模型自己挑颜色，一句治愈文案被铺成蓝底（用户 2026-11-26）。
    auto_color = False
    if not sel["color"]:
        guess = default_color(sel["style"], sel["layout"], colors, image_mode)
        if guess:
            sel["color"] = guess
            auto_color = True

    instruction = custom_instr or build_instruction(
        sel, ratio, moods, fusion, whitespace, ref, image_mode, shoot)
    out_items = []
    # 纯色留白的社媒卡不给自动道具：那张卡要的是「一个小人和大片空白」，
    # 塞一件实物反而把模型钉在道具上（一句关于忘记的话被画成一页日历）。
    # 单图快出（只有一条）也一样，一句主题本来就该由模型自己设计画面，
    # 既然用不上，就别再花十几秒调大模型挑道具（用户 2026-11-26 反馈优化提示词等太久）。
    single = len(items) == 1
    if image_mode == "photo":
        want_scene = True
    elif image_mode == "pure-image":
        # 多图贴图才需要逐条配道具；单图只给一句主题时交给模型自由发挥
        want_scene = (not single) or not (items[0]["title"] or items[0]["body"])
    else:
        want_scene = is_three_layer(sel["layout"]) or not is_flat_layout(sel["layout"])
    if want_scene:
        # scene 留空的条目统一交给 fill_scenes：大模型批量挑道具，失败静默回退关键词规则，
        # 保证永远有元素、绝不像以前那样一律画杯子。拆分镜 / 生成提示词都复用同一份逻辑。
        scenes = await fill_scenes(items, fusion)
    else:
        scenes = [tidy_phrase(it.get("scene") or "") for it in items]
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
                                   visual=item["visual"], scene_manual=item["scene_manual"]),
        })
    return {
        "instruction": instruction,
        "items": out_items,
        "auto_color": auto_color,
        # 没有风格＝提示词里一个画风字都没有，出图只能靠模型自由发挥。
        # 前端据此提示并自动补风格，其它入口也能从响应里看出来（用户 2026-11-26）。
        "style_missing": not bool(sel["style"]),
        "selection": {
            "style": sel["style"]["id"] if sel["style"] else None,
            "layout": sel["layout"]["id"] if sel["layout"] else None,
            "color": sel["color"]["id"] if sel["color"] else None,
        },
    }
