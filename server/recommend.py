"""按内容推荐「图型 + 风格 + 主题色」组合。

不调模型、不联网：内容 → 关键词命中 → 语义标签权重 → 给 170 图型 / 372 风格 / 36 配色打分，
再从同档候选里按权重抽一个。同一个 seed 结果稳定（方便复现、测试）；换个 seed 就是「换一组」。

设计上的三个取舍：
1. 只做词表加权，不做 embedding。词表可读、可改、可测；命中不了就退到「通用」档，
   靠 seed 抖动出不同组合，绝不返回空。
2. 标签只影响排序的「倾向」，不锁死唯一答案：同一条内容每次点都能换到同档的其他搭配，
   但不会换到气质相反的组合（写实摄影不会被推给纯色留白卡）。
3. 兼容性单独一层 compat：图型定的是「这张卡长什么样」，风格不能和它打架。
"""

from __future__ import annotations

import math
import random
import re

import catalog

# ---------------------------------------------------------------- 语义标签表
# kw       命中词（2 字以上，尽量具体；ascii 词按整词匹配）
# layout_group / layout_id / layout_lex   图型倾向：分组权重、单卡权重、文案里的词
# style_group / style_id / style_lex      风格倾向
# color_group / color_id                  配色倾向（配色没有文案，只按分组和具体色号）
TAGS: list[dict] = [
    {
        "key": "heal", "name": "治愈松弛",
        "kw": ["治愈", "松弛", "别着急", "慢慢", "放下", "焦虑", "难过", "累", "疲惫", "孤独",
               "安慰", "温柔", "温暖", "晚安", "自愈", "情绪", "心事", "想不通", "忘记", "释然",
               "接受", "躺平", "喘息", "放空", "安静", "不着急", "没关系", "算了", "歇一歇"],
        "layout_group": {"社媒卡": 1.0},
        "layout_id": {"SC-008": 2.2, "SC-024": 1.8, "SC-001": 1.4, "SC-002": 1.4, "SC-014": 1.2, "SC-016": 1.0},
        "layout_lex": ["留白", "简洁", "图上文下", "图下文上", "极简"],
        "style_group": {"经典绘本叙事": 1.8, "日本当代插画": 1.4, "新国风与传统工艺": 1.2},
        "style_id": {"FE-060": 1.6, "FD-003": 0.8, "FB-002": 0.7, "FE-005": 0.7},
        "style_lex": ["留白", "温柔", "治愈", "墨线", "手绘", "轻", "安静", "白底"],
        "color_group": {"中性色系": 1.0, "暖阳大地": 0.8, "清新绿系": 0.6},
        "color_id": {"C-34": 1.4, "C-30": 1.2, "C-33": 0.8},
    },
    {
        "key": "humor", "name": "吐槽幽默",
        "kw": ["搞笑", "吐槽", "离谱", "沙雕", "尴尬", "翻车", "笑死", "摸鱼", "上班", "老板",
               "加班", "周一", "打工人", "同事", "开会", "裁员", "内卷", "社死", "摆烂", "崩溃",
               "无语", "救命", "离谱", "干饭", "emo"],
        "layout_group": {"社媒卡": 0.8, "漫画分镜": 0.8},
        "layout_id": {"SC-005": 2.0, "SC-025": 1.8, "SC-007": 1.4, "SC-003": 1.0, "SB-001": 1.0},
        "layout_lex": ["气泡", "漫画", "双格", "对话"],
        "style_group": {"国际社论幽默": 1.8, "现代平面艺术": 1.2, "经典绘本叙事": 1.0},
        "style_id": {"FA-001": 1.0, "FE-002": 1.0, "FB-001": 0.8},
        "style_lex": ["幽默", "冷幽默", "夸张", "怪", "吐槽", "讽刺", "尴尬"],
        "color_group": {"暖阳大地": 1.0, "古典红绿": 0.6},
        "color_id": {"C-27": 1.0, "C-26": 0.9, "C-28": 0.7},
    },
    {
        "key": "work", "name": "职场成长",
        "kw": ["职场", "升职", "面试", "简历", "汇报", "绩效", "跳槽", "副业", "效率", "会议",
               "工作", "打工", "辞职", "涨薪", "转行", "实习", "管理", "带团队", "述职"],
        "layout_group": {"信息图": 1.6, "社媒卡": 0.6},
        "layout_id": {"SC-010": 1.4, "SC-020": 1.0, "SC-011": 1.0, "IG-012": 1.0},
        "layout_lex": ["步骤", "层级", "对比", "清单", "标签", "流程"],
        "style_group": {"现代平面艺术": 1.6, "国际社论幽默": 1.0, "写实摄影与视觉大片": 0.6},
        "style_id": {"FC-003": 0.8, "FC-005": 0.8},
        "style_lex": ["几何", "图标", "简洁", "社论", "平涂"],
        "color_group": {"经典蓝系": 1.2, "中性色系": 0.8},
        "color_id": {"C-31": 0.8, "C-04": 0.6},
    },
    {
        "key": "money", "name": "理财搞钱",
        "kw": ["理财", "基金", "工资", "存钱", "省钱", "消费", "投资", "收入", "负债", "预算",
               "搞钱", "股票", "攒钱", "攒下", "存下", "花钱", "利息", "定投", "房贷", "记账",
               "月薪", "年薪", "存款", "省钱", "块钱", "万元"],
        "layout_group": {"信息图": 1.6, "社媒卡": 0.6},
        "layout_id": {"IG-011": 1.4, "IG-004": 1.2, "SC-006": 1.0, "IG-010": 1.0},
        "layout_lex": ["对比", "步骤", "层级", "数据", "标注"],
        "style_group": {"现代平面艺术": 1.6, "国际社论幽默": 1.0, "3D黏土毛毡与纸雕": 0.6},
        "style_id": {"FC-005": 0.8, "FC-006": 0.8},
        "style_lex": ["几何", "符号", "负空间", "高对比", "极简"],
        "color_group": {"古典红绿": 1.0, "暖阳大地": 0.8, "经典蓝系": 0.8},
        "color_id": {"C-16": 0.9, "C-15": 0.6},
    },
    {
        "key": "love", "name": "情感关系",
        "kw": ["恋爱", "分手", "结婚", "婚姻", "男朋友", "女朋友", "老公", "老婆", "父母", "家人",
               "朋友", "喜欢", "暗恋", "相亲", "异地", "失恋", "暧昧", "陪伴", "吵架", "想念"],
        "layout_group": {"社媒卡": 1.0},
        "layout_id": {"SC-008": 1.6, "SC-015": 1.4, "SC-001": 1.0, "SC-024": 1.2, "SC-003": 1.0},
        "layout_lex": ["图上文下", "两段", "递进", "双格"],
        "style_group": {"日本当代插画": 1.6, "经典绘本叙事": 1.4, "新国风与传统工艺": 0.6},
        "style_id": {"FD-002": 0.8, "FB-002": 0.8, "FD-006": 0.8},
        "style_lex": ["温柔", "轻", "诗意", "低饱和", "留白", "水彩"],
        "color_group": {"浪漫粉紫": 1.4, "中性色系": 0.8},
        "color_id": {"C-19": 1.0, "C-22": 1.0, "C-21": 0.8},
    },
    {
        "key": "parent", "name": "育儿教育",
        "kw": ["孩子", "宝宝", "育儿", "教育", "学校", "作业", "老师", "家长", "启蒙", "亲子",
               "幼儿园", "小学生", "带娃", "成长", "绘本共读", "辅导"],
        "layout_group": {"社媒卡": 1.0},
        "layout_id": {"SC-003": 1.6, "SC-008": 1.4, "SC-006": 1.0, "SC-011": 1.0},
        "layout_lex": ["双格", "图上文下", "三层", "标注"],
        "style_group": {"经典绘本叙事": 1.8, "3D黏土毛毡与纸雕": 1.0, "日本当代插画": 1.0},
        "style_id": {"FB-003": 0.8, "FB-007": 0.8},
        "style_lex": ["绘本", "拟人", "圆润", "温暖", "水彩"],
        "color_group": {"清新绿系": 1.0, "暖阳大地": 1.0},
        "color_id": {"C-27": 0.8, "C-09": 0.7},
    },
    {
        "key": "food", "name": "美食生活",
        "kw": ["美食", "做饭", "菜谱", "咖啡", "奶茶", "早餐", "餐厅", "食谱", "烘焙", "下厨",
               "零食", "宵夜", "火锅", "面条", "甜品", "好吃", "探店", "厨房", "食材", "做法",
               "配方", "番茄", "牛腩", "炖", "炒", "下饭", "味道"],
        "layout_group": {"社媒卡": 1.0, "电商": 0.6},
        "layout_id": {"SC-008": 1.4, "SC-021": 1.4, "SC-001": 1.0, "SC-018": 0.8},
        "layout_lex": ["主图", "双拼", "照片", "标注"],
        "style_group": {"写实摄影与视觉大片": 1.6, "日本当代插画": 1.0, "经典绘本叙事": 0.8},
        "style_lex": ["摄影", "质感", "暖调", "食", "静物"],
        "color_group": {"暖阳大地": 1.4},
        "color_id": {"C-25": 1.0, "C-26": 0.9, "C-29": 0.7},
    },
    {
        "key": "travel", "name": "旅行户外",
        "kw": ["旅行", "旅游", "风景", "出发", "海边", "露营", "徒步", "城市", "散步", "日出",
               "日落", "远方", "周末", "山川", "海边", "自驾", "机票", "行李"],
        "layout_group": {"社媒卡": 1.0},
        "layout_id": {"SC-014": 1.8, "SC-001": 1.4, "SC-018": 1.0, "SC-021": 1.0},
        "layout_lex": ["留白", "主视觉", "主图", "照片"],
        "style_group": {"写实摄影与视觉大片": 1.6, "日本当代插画": 1.2, "新国风与传统工艺": 0.8},
        "style_id": {"FE-005": 0.8, "FD-002": 0.6},
        "style_lex": ["旅行", "风景", "自然", "光影", "宽阔"],
        "color_group": {"清新绿系": 1.0, "经典蓝系": 1.0},
        "color_id": {"C-08": 0.8, "C-10": 0.7},
    },
    {
        "key": "ai", "name": "AI 科技",
        "kw": ["ai", "人工智能", "大模型", "提示词", "工具", "效率", "自动化", "代码", "程序员",
               "软件", "系统", "工作流", "agent", "skill", "模型", "算法", "数据", "插件", "开源"],
        "layout_group": {"信息图": 1.6, "社媒卡": 0.8},
        "layout_id": {"SC-020": 1.2, "SC-019": 1.2, "IG-012": 1.0, "IG-008": 1.0},
        "layout_lex": ["层级", "流程", "标注", "结构", "标签"],
        "style_group": {"现代平面艺术": 1.6, "3D黏土毛毡与纸雕": 1.0, "先锋实验与综合媒介": 0.8},
        "style_id": {"FC-005": 0.8, "FC-008": 0.7},
        "style_lex": ["几何", "极简", "未来", "科技", "高对比"],
        "color_group": {"经典蓝系": 1.6, "中性色系": 0.8},
        "color_id": {"C-01": 0.9, "C-03": 0.7, "C-31": 0.6},
    },
    {
        "key": "know", "name": "干货教程",
        "kw": ["教程", "步骤", "方法", "清单", "攻略", "入门", "怎么", "如何", "指南",
               "技巧", "流程", "模板", "避坑", "原理", "盘点", "攻略", "手把手"],
        "layout_group": {"信息图": 2.0, "社媒卡": 0.8},
        "layout_id": {"IG-008": 1.4, "IG-009": 1.2, "IG-001": 1.2, "IG-004": 1.2, "SC-010": 1.2},
        "layout_lex": ["步骤", "流程", "时间轴", "对比", "列表", "层级", "图鉴"],
        "style_group": {"现代平面艺术": 1.4, "国际社论幽默": 1.0, "3D黏土毛毡与纸雕": 0.8},
        "style_id": {"FC-008": 0.7, "FC-009": 0.7},
        "style_lex": ["几何", "图标", "符号", "扁平", "信息"],
        "color_group": {"经典蓝系": 1.0, "中性色系": 1.0},
        "color_id": {"C-31": 0.7, "C-02": 0.6},
    },
    {
        "key": "biz", "name": "商业营销",
        "kw": ["增长", "转化", "品牌", "营销", "用户", "数据", "复盘", "案例", "运营", "流量",
               "私域", "电商", "销量", "投放", "获客", "留存", "复购", "定价"],
        "layout_group": {"信息图": 1.8, "电商": 1.2, "社媒卡": 0.6},
        "layout_id": {"SC-011": 1.2, "SC-018": 1.0, "IG-006": 1.0, "IG-004": 1.0},
        "layout_lex": ["对比", "数据", "结构", "多行", "标注"],
        "style_group": {"现代平面艺术": 1.6, "写实摄影与视觉大片": 1.0},
        "style_id": {"FC-005": 0.8, "FC-009": 0.8},
        "style_lex": ["高对比", "几何", "符号", "商业", "极简"],
        "color_group": {"经典蓝系": 1.2, "中性色系": 0.8},
        "color_id": {"C-04": 0.7, "C-32": 0.6},
    },
    {
        "key": "guofeng", "name": "国风东方",
        "kw": ["古诗", "书法", "节气", "茶", "禅", "水墨", "东方", "国潮", "传统", "江湖",
               "山水", "汉服", "中医", "养生", "国学", "留白画", "宋", "唐"],
        "layout_group": {"社媒卡": 1.0},
        "layout_id": {"SC-014": 1.6, "SC-002": 1.2, "SC-006": 1.2, "SC-008": 1.0},
        "layout_lex": ["留白", "极简", "大字", "三层"],
        "style_group": {"新国风与传统工艺": 2.6},
        "style_id": {"FE-060": 0.8, "FE-003": 0.8},
        "style_lex": ["新国风", "国潮", "东方", "水墨", "传统", "古典", "留白"],
        "color_group": {"古典红绿": 1.4, "中性色系": 0.8},
        "color_id": {"C-15": 1.0, "C-14": 0.8, "C-34": 0.6},
    },
    {
        "key": "pet", "name": "萌宠动物",
        "kw": ["猫", "狗", "宠物", "橘猫", "小猫", "小狗", "毛孩子", "铲屎官", "喵", "汪", "动物"],
        "layout_group": {"社媒卡": 1.0},
        "layout_id": {"SC-008": 2.0, "SC-024": 1.6, "SC-003": 1.0, "SC-001": 0.8},
        "layout_lex": ["图上文下", "图下文上", "角色", "双格"],
        "style_group": {"经典绘本叙事": 1.6, "日本当代插画": 1.4, "3D黏土毛毡与纸雕": 1.0},
        "style_id": {"FD-004": 0.8, "FB-007": 0.8},
        "style_lex": ["动物", "拟人", "圆润", "柔软", "可爱"],
        "color_group": {"暖阳大地": 1.2, "中性色系": 0.8},
        "color_id": {"C-30": 1.0, "C-26": 0.6},
    },
    {
        "key": "health", "name": "健康运动",
        "kw": ["健身", "跑步", "睡眠", "熬夜", "减肥", "健康", "运动", "瑜伽", "体重", "早睡",
               "体检", "拉伸", "增肌", "自律", "作息"],
        "layout_group": {"信息图": 1.4, "社媒卡": 0.8},
        "layout_id": {"IG-010": 1.2, "IG-011": 1.0, "SC-006": 1.0, "SC-008": 0.8},
        "layout_lex": ["阶段", "对比", "步骤", "层级"],
        "style_group": {"现代平面艺术": 1.2, "写实摄影与视觉大片": 1.0, "日本当代插画": 0.8},
        "style_lex": ["简洁", "几何", "自然", "清爽"],
        "color_group": {"清新绿系": 1.4},
        "color_id": {"C-09": 1.0, "C-10": 0.8},
    },
    {
        "key": "story", "name": "故事回忆",
        "kw": ["那年", "那天", "后来", "小时候", "故事", "曾经", "记得", "爷爷", "奶奶", "第一次",
               "回忆", "从前", "长大", "旧", "老照片", "很多年"],
        "layout_group": {"漫画分镜": 1.6, "社媒卡": 0.8},
        "layout_id": {"SC-015": 1.2, "SC-025": 1.2, "SB-001": 1.0},
        "layout_lex": ["分镜", "两段", "递进", "漫画"],
        "style_group": {"经典绘本叙事": 1.8, "日本当代插画": 1.0, "新国风与传统工艺": 1.0},
        "style_id": {"FB-004": 0.8, "FB-008": 0.7},
        "style_lex": ["旧书", "水彩", "安静", "怀旧", "速写"],
        "color_group": {"暖阳大地": 1.0, "中性色系": 0.8},
        "color_id": {"C-29": 0.8, "C-36": 0.7},
    },
]

# 什么都没命中时的兜底倾向：一组稳妥、不挑题材的搭配
GENERAL = {
    "key": "general", "name": "通用",
    "layout_group": {"社媒卡": 1.0},
    "layout_id": {"SC-008": 1.2, "SC-001": 0.8},
    "layout_lex": [],
    "style_group": {"经典绘本叙事": 1.0, "日本当代插画": 0.8},
    "style_id": {"FE-060": 0.8},
    "style_lex": [],
    "color_group": {"中性色系": 1.0, "暖阳大地": 0.6},
    "color_id": {"C-34": 0.8},
}

# 图型分组 → 一句话说明，用在推荐理由里
LAYOUT_WHY = {
    "社媒卡": "单张卡片，适合一句话配一张图直接发",
    "信息图": "有结构，适合把一件事讲清楚",
    "漫画分镜": "有场景推进，适合讲过程和故事",
    "电商": "商品展示位，主体要抢眼",
    "IP设计": "以角色为主，适合立人设",
}

FLAT_HINTS = ["纯色背景", "纯白背景", "纯黑背景", "纯色底", "留白背景",
              "大面积留白", "较多留白", "大量留白"]

INK_HINTS = ["白底", "黑白", "墨线", "线稿", "白纸", "单色线", "毛笔"]

_ASCII = re.compile(r"^[a-z0-9 ]+$")


def _has(text: str, kw: str) -> bool:
    """中文直接子串；ascii 词按整词匹配，避免 ai 命中 said 里的 ai。"""
    if _ASCII.match(kw):
        return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None
    return kw in text


def detect_tags(text: str) -> list[tuple[dict, int]]:
    t = (text or "").lower()
    out: list[tuple[dict, int]] = []
    for tag in TAGS:
        hits = sum(1 for k in tag["kw"] if _has(t, k))
        if hits:
            out.append((tag, min(hits, 3)))
    out.sort(key=lambda x: -x[1])
    return out


def _lex_hits(blob: str, words: list[str]) -> int:
    return min(sum(1 for w in words if w in blob), 3)


def _is_flat(layout: dict) -> bool:
    blob = (layout.get("prompt") or "") + (layout.get("title") or "")
    return any(h in blob for h in FLAT_HINTS)


def _is_ink(style: dict) -> bool:
    blob = (style.get("features") or "") + " " + (style.get("name_en") or "")
    if "摄影" in (style.get("group_name") or ""):
        return False
    return any(h in blob for h in INK_HINTS)


def _compat_style(style: dict, layout: dict) -> float:
    """图型和风格的相容系数：摄影风不能往纯色留白卡上推，信息图也别用大片写真。"""
    k = 1.0
    group = style.get("group_name") or ""
    lay_group = layout.get("group_name") or ""
    if _is_flat(layout) and group == "写实摄影与视觉大片":
        k *= 0.2
    if lay_group == "信息图" and group == "写实摄影与视觉大片":
        k *= 0.55
    if lay_group == "漫画分镜" and group == "写实摄影与视觉大片":
        k *= 0.45
    if lay_group == "信息图" and "先锋实验" in group:
        k *= 0.7
    if lay_group == "漫画分镜" and group in ("经典绘本叙事", "动漫和赛璐璐", "日本当代插画"):
        k *= 1.25
    return k


def _compat_color(color: dict, style: dict) -> float:
    """白底线稿配高饱和主题色会直接毁掉画风，往纸白／低饱和那边压。"""
    k = 1.0
    group = color.get("group") or ""
    if _is_ink(style):
        if group == "中性色系":
            k *= 1.5
        elif group == "暖阳大地":
            k *= 1.15
        elif group in ("经典蓝系", "浪漫粉紫"):
            k *= 0.45
    if (style.get("group_name") or "") == "3D黏土毛毡与纸雕" and group == "暖阳大地":
        k *= 1.25
    return k


def _score(items: list[dict], tagged: list[tuple[dict, int]], kind: str) -> list[tuple[float, dict]]:
    total = sum(w for _, w in tagged) or 1
    rows: list[tuple[float, dict]] = []
    for it in items:
        if kind == "layout":
            blob = (it.get("title") or "") + (it.get("prompt") or "")
        elif kind == "style":
            blob = " ".join([it.get("ref") or "", it.get("features") or "",
                             it.get("name_en") or "", it.get("group_name") or ""])
        else:
            blob = (it.get("name") or "") + (it.get("group") or "")
        s = 0.0
        for tag, w in tagged:
            p = float(tag.get(kind + "_group", {}).get(
                it.get("group_name") if kind != "color" else it.get("group"), 0.0))
            p += float(tag.get(kind + "_id", {}).get(it.get("id"), 0.0))
            if kind != "color":
                p += 0.30 * _lex_hits(blob, tag.get(kind + "_lex", []))
            s += w * p
        rows.append((s / total, it))
    return rows


def _pick(rows: list[tuple[float, dict]], rng: random.Random, avoid: set[str],
          k: int = 6, temp: float = 0.5) -> dict:
    """同档候选里按 softmax 权重抽一个；avoid 里的组合压到很低但保留可能。"""
    rows = sorted(rows, key=lambda x: -x[0])[:k]
    if not rows:
        raise ValueError("empty candidates")
    top = rows[0][0]
    weights = []
    for s, it in rows:
        w = math.exp((s - top) / temp)
        if it.get("id") in avoid:
            w *= 0.12
        weights.append(max(w, 1e-6))
    pick = rng.choices(range(len(rows)), weights=weights, k=1)[0]
    return rows[pick][1]


PHOTO_GROUP = "写实摄影与视觉大片"

# 这几个是「有明确使用场景」的画风：监控截屏、淘宝主图、随手拍、航拍、游戏实机。
# 内容没提到对应场景时压到很低，否则一句治愈文案会被配成安防监控录像。
ODD_STYLE_HINTS = {
    "FI-031": ["淘宝", "电商", "商品", "带货", "主图", "详情页"],
    "FI-032": ["随手拍", "手机拍", "自拍", "vlog", "日常记录"],
    "FI-033": ["监控", "摄像头", "安防", "录像", "门禁"],
    "FI-034": ["航拍", "无人机", "俯瞰", "高空", "鸟瞰"],
    "FI-035": ["游戏", "实机", "开放世界", "3a", "主机"],
}


def _odd_adjust(style: dict, text: str) -> float:
    """用加减不乘除：这些画风在多数题材里基础分本来就是 0，乘任何系数还是 0，
    只有扣一个固定分才压得下去；内容真的提到对应场景时再加回来。"""
    kws = ODD_STYLE_HINTS.get(style.get("id") or "")
    if not kws:
        return 0.0
    low = (text or "").lower()
    return 0.9 if any(k in low for k in kws) else -1.0


def recommend(text: str, seed: int | None = None, mode: str = "graphic-text",
              avoid: dict | None = None) -> dict:
    """返回一组组合。seed 固定则结果固定；avoid 传上次的 id，可让「换一组」真的换。"""
    data = catalog.build()
    rng = random.Random(0 if seed is None else int(seed) % (2 ** 31 - 1))
    tagged = detect_tags(text)
    note = ""
    if not tagged:
        tagged = [(GENERAL, 1)]
        note = "没读到明确题材，先给一组通用搭配"
    # 纯图 / 摄影都不套图型，别让人以为换了图型却没变化
    if mode in ("pure-image", "photo"):
        tip = "摄影模式不套图型，只配了画风和主题色" if mode == "photo" \
            else "纯图模式不套图型，只配了画风和主题色"
        note = (note + "；" + tip) if note else tip

    avoid = avoid or {}
    layouts = _score(data["layouts"], tagged, "layout")
    # 抖动量要小于一个「倾向」的差距，否则随机压过内容
    layouts = [(s + rng.uniform(0, 0.8), it) for s, it in layouts]
    layout = _pick(layouts, rng, set(avoid.get("layout") or []), k=6)

    # 摄影模式就是写真管线，线稿那一路推过去必然出不了片：直接在写实组里挑，不放宽
    style_pool = data["styles"]
    if mode == "photo":
        style_pool = [x for x in style_pool if (x.get("group_name") or "") == PHOTO_GROUP] or style_pool
    # 其余模式才需要「风格和图型合不合得来」这一层；摄影不套图型，也就没有这条
    styles = _score(style_pool, tagged, "style")
    styles = [(s * (1.0 if mode == "photo" else _compat_style(it, layout))
               + _odd_adjust(it, text) + rng.uniform(0, 0.75), it)
              for s, it in styles]
    style = _pick(styles, rng, set(avoid.get("style") or []), k=7)

    colors = _score(data["colors"], tagged, "color")
    colors = [(s * _compat_color(it, style) + rng.uniform(0, 0.6), it) for s, it in colors]
    color = _pick(colors, rng, set(avoid.get("color") or []), k=4)

    tag_names = [t["name"] for t, _ in tagged if t["key"] != "general"]
    lay_group = layout.get("group_name") or ""
    reason_bits = {
        "layout": LAYOUT_WHY.get(lay_group, "版式和内容对得上"),
        "style": (style.get("ref") or style.get("name_en") or style.get("id", "")).strip(),
        "color": (color.get("name") or "") + "（" + (color.get("group") or "") + "）",
    }
    summary = ("内容是" + " · ".join(tag_names[:3]) + "，" if tag_names else "")
    summary += f"图型 {layout['id']} + 风格 {style['id']} + 主题色 {color['id']}"

    return {
        "seed": 0 if seed is None else int(seed),
        "mode": mode,
        "tags": [{"key": t["key"], "name": t["name"], "hits": w}
                 for t, w in tagged if t["key"] != "general"],
        "note": note,
        "layout": {
            "id": layout["id"], "title": layout.get("title", ""),
            "group": lay_group, "reason": reason_bits["layout"],
        },
        "style": {
            "id": style["id"], "ref": style.get("ref", ""), "name_en": style.get("name_en", ""),
            "group": style.get("group_name", ""),
            "reason": ("参考 " + (style.get("ref") or style.get("name_en") or "")).strip(),
        },
        "color": {
            "id": color["id"], "name": color.get("name", ""), "group": color.get("group", ""),
            "reason": reason_bits["color"],
        },
        "summary": summary,
    }
