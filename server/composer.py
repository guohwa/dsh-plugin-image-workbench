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

TAIL_INSTRUCTION = ""

# 带了参考图时插在最前面：先定画风基准，再谈版式和文字，避免模型把参考图
# 原样复刻（用户要的是「融合」，不是「重画同一张」）
REF_FUSION = (
    "参考我上传的这张图：沿用它的画风、笔触、配色和整体质感，把它当作视觉基准。"
    "画面内容按下文重新绘制，下面的版式与文字要求优先，不要照抄参考图里的文字。"
)

# 中层小辅助图没填时的兜底描述，保证三层版式不会空一层
SCENE_FALLBACK = "一个涂鸦小人和一件具体实物道具，动作直接对应上层标题的意思，造型简单、线条少"


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
                 image_mode: str = "graphic-text", shoot: str = "", instr: str = "") -> str:
    """单条完整提示词。item = {title, body}"""
    style = sel.get("style")
    layout = sel.get("layout")
    color = sel.get("color")
    title = clean_title(item.get("title") or "")
    body = (item.get("body") or "").strip()
    scene = (item.get("scene") or "").strip()

    blocks: list[str] = []

    if ref:
        blocks.append(REF_FUSION)

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

    blocks.append(TAIL_INSTRUCTION)
    return "\n".join(b for b in blocks if b)


def compose(payload: dict, styles: list[dict], layouts: list[dict], colors: list[dict]) -> dict:
    """组装全部条目。payload 里 items 为空时用 topic 兜底生成一条。"""

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
        if not title and not body and not allow_blank:
            continue
        items.append({"title": title, "body": body, "scene": scene})

    if not items:
        topic = (payload.get("topic") or "").strip()
        if topic:
            lines = topic.split("\n", 1)
            items.append({"title": lines[0].strip(), "body": lines[1].strip() if len(lines) > 1 else "", "scene": ""})

    instruction = custom_instr or build_instruction(
        sel, ratio, moods, fusion, whitespace, ref, image_mode, shoot)
    out_items = []
    for idx, item in enumerate(items, 1):
        out_items.append({
            "index": idx,
            "title": item["title"],
            "body": item["body"],
            "scene": item.get("scene", ""),
            "instruction": instruction,
            "prompt": build_prompt(item, sel, ratio, moods, fusion, whitespace, note, ref,
                                   image_mode, shoot, instr=custom_instr),
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
