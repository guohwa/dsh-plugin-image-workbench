"""小说推文漫画：出图完成后把旁白叠到画面顶部。

中文字让生图模型直接画进图里，字形和位置都不可控；这里改成出图后用 Pillow
排版，引号里的对话标红，正文走微软雅黑。叠完直接覆盖原图，历史、下载、
公众号推送拿到的都是带字的成品。

Pillow 是号池 worker 的既有依赖；字体优先用 Windows 自带的微软雅黑，
Linux 上退到文泉驿 / Noto CJK，都没有就用 Pillow 默认字体（中文可能缺字）。
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_CANDIDATES = (
    r"C:/Windows/Fonts/msyh.ttc",
    r"C:/Windows/Fonts/msyhbd.ttc",
    r"C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# 引号里的词按对话标红；“初次见面，姐姐好。”这种整句都红，和示例一致
_QUOTED = re.compile(r"(「[^」]{1,80}」|“[^”]{1,80}”|\"[^\"]{1,80}\")")
_INK = (40, 34, 32, 255)
_RED = (176, 34, 30, 255)
_STROKE = (255, 255, 255, 225)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _colored_chars(text: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    at = 0
    for m in _QUOTED.finditer(text):
        out.extend((c, False) for c in text[at:m.start()])
        out.extend((c, True) for c in m.group(0))
        at = m.end()
    tail = text[at:]
    # 兜底资料被句号切开时可能只剩半截引号，别让它变成一整句黑字
    if "「" in tail and "」" not in tail:
        i = tail.index("「")
        out.extend((c, False) for c in tail[:i])
        out.extend((c, True) for c in tail[i:])
    elif "」" in tail and "「" not in tail:
        i = tail.index("」")
        out.extend((c, True) for c in tail[:i + 1])
        out.extend((c, False) for c in tail[i + 1:])
    else:
        out.extend((c, False) for c in tail)
    return out


def _wrap(draw: ImageDraw.ImageDraw, font, chars: list[tuple[str, bool]],
          max_w: int) -> list[list[tuple[str, bool]]]:
    lines: list[list[tuple[str, bool]]] = [[]]
    used = 0.0
    for ch, red in chars:
        if ch == "\n":
            lines.append([])
            used = 0.0
            continue
        w = draw.textlength(ch, font=font)
        if used + w > max_w and lines[-1]:
            lines.append([])
            used = 0.0
        lines[-1].append((ch, red))
        used += w
    return [ln for ln in lines if ln]


def apply(path: str | Path, text: str, *, max_lines: int = 7) -> dict:
    """把旁白叠到图片顶部并覆盖原文件。返回叠字信息，失败时抛异常由调用方兜底。"""
    text = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not text:
        return {"overlay": False, "reason": "empty"}

    p = Path(path)
    with Image.open(p) as src:
        im = src.convert("RGB")
    w, h = im.size
    draw = ImageDraw.Draw(im)

    size = max(24, min(72, int(w * 0.052)))
    chars = _colored_chars(text)
    while True:
        font = _font(size)
        lines = _wrap(draw, font, chars, max(80, w - int(w * 0.13)))
        if len(lines) <= max_lines or size <= 20:
            break
        size -= 2

    line_h = int(size * 1.5)
    top = max(12, int(h * 0.035))
    band_h = min(h, top + len(lines) * line_h + size)
    veil = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vd = ImageDraw.Draw(veil)
    for y in range(band_h):
        # 顶部白纱渐隐，保证黑字/红字压在深色画面上也读得清，又不至于盖成一块白板
        a = int(205 * (1 - y / band_h) ** 1.6)
        if a > 0:
            vd.line([(0, y), (w, y)], fill=(255, 255, 255, a))
    im = Image.alpha_composite(im.convert("RGBA"), veil).convert("RGB")
    draw = ImageDraw.Draw(im)

    x0 = int(w * 0.065)
    y = top
    for line in lines:
        # 同一颜色的连续字一次画完，中文字距正常，英文也不会被拆成一个个字母
        runs: list[tuple[str, bool]] = []
        for ch, red in line:
            if runs and runs[-1][1] == red:
                runs[-1] = (runs[-1][0] + ch, red)
            else:
                runs.append((ch, red))
        x = x0
        for run, red in runs:
            draw.text((x, y), run, font=font, fill=(_RED if red else _INK),
                      stroke_width=1, stroke_fill=_STROKE)
            x += draw.textlength(run, font=font)
        y += line_h

    im.save(p, "PNG")
    return {"overlay": True, "lines": len(lines), "font_size": size,
            "bytes": p.stat().st_size, "width": w, "height": h}
