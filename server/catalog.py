"""Parse the handraw-style markdown catalogs into a single JSON used by the workbench.

Source: https://github.com/yang0/handraw-style (STYLES.md / LAYOUTS.md / COLORS.md)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("HANDRAW_REPO", str(_BASE / "handraw_repo")))
OUT = Path(os.environ.get("CATALOG_OUT", str(_BASE / "catalog.json")))

STYLE_GROUP_NAMES = {
    "FA": "国际社论幽默",
    "FB": "经典绘本叙事",
    "FC": "现代平面艺术",
    "FD": "日本当代插画",
    "FE": "新国风与传统工艺",
    "FF": "3D黏土毛毡与纸雕",
    "FG": "动漫和赛璐璐",
    "FH": "先锋实验与综合媒介",
    "FI": "写实摄影与视觉大片",
}

LAYOUT_GROUP_NAMES = {
    "SC": "社媒卡",
    "IG": "信息图",
    "SB": "漫画分镜",
    "IP": "IP设计",
    "EC": "电商",
}

COLOR_GROUPS = [
    (1, 6, "经典蓝系"),
    (7, 12, "清新绿系"),
    (13, 18, "古典红绿"),
    (19, 24, "浪漫粉紫"),
    (25, 30, "暖阳大地"),
    (31, 36, "中性色系"),
]

CELL_RE = re.compile(r"<img src='([^']+)'[^>]*alt='([^']*)'[^>]*>(.*?)</details>", re.S)


def _clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", text).strip()


def _read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8", errors="replace")


def parse_styles() -> list[dict]:
    txt = _read("STYLES.md")
    out: list[dict] = []
    hits = list(re.finditer(r"\*\*([A-Z]{2}-\d{3})\*\*", txt))
    for i, m in enumerate(hits):
        code = m.group(1)
        seg = txt[m.start():(hits[i + 1].start() if i + 1 < len(hits) else len(txt))]
        head = txt[max(0, m.start() - 420):m.start()]
        imgs = list(re.finditer(r"<img src='([^']+)'[^>]*alt='([^']*)'", head))
        img = imgs[-1].group(1) if imgs else ""
        alt = imgs[-1].group(2) if imgs else code
        num = re.search(r"<small>\(#(\d{3})\)</small>", seg)
        name = re.search(r"</small>\s*·\s*([^<\n]+)", seg)
        ref = re.search(r"<sub>参考:\s*([^<]+)</sub>", seg)
        feat = re.search(r"查看核心特征</summary><br>(.*?)(?:</details>|$)", seg, re.S)
        out.append({
            "id": code,
            "num": num.group(1) if num else code.split("-")[1],
            "name_en": _clean(name.group(1)) if name else _clean(alt),
            "ref": _clean(ref.group(1)) if ref else "",
            "features": _clean(feat.group(1)) if feat else "",
            "group": code[:2],
            "group_name": STYLE_GROUP_NAMES.get(code[:2], code[:2]),
            "img": img,
        })
    return out


def parse_layouts() -> list[dict]:
    txt = _read("LAYOUTS.md")
    out: list[dict] = []
    for img, alt, tail in CELL_RE.findall(txt):
        m = re.search(r"\*\*([A-Z]{2}-\d{3})\*\*", tail)
        if not m:
            continue
        code = m.group(1)
        prompt = re.search(r"查看排版提示词</summary><br>(.*)", tail, re.S)
        title = _clean(alt.split(" ", 1)[1]) if " " in alt else _clean(alt)
        out.append({
            "id": code,
            "title": title,
            "prompt": _clean(prompt.group(1)) if prompt else "",
            "group": code[:2],
            "group_name": LAYOUT_GROUP_NAMES.get(code[:2], code[:2]),
            "img": img,
        })
    return out


def parse_colors() -> list[dict]:
    txt = _read("COLORS.md")
    out: list[dict] = []
    for img, alt, tail in CELL_RE.findall(txt):
        m = re.search(r"\*\*(C-\d{2})\*\*", tail)
        if not m:
            continue
        code = m.group(1)
        idx = int(code.split("-")[1])
        name_cn = alt.split(" ", 1)[1] if " " in alt else alt
        prompt = re.search(r"`([^`]+)`", tail)
        group = next((g for lo, hi, g in COLOR_GROUPS if lo <= idx <= hi), "")
        out.append({
            "id": code,
            "name": _clean(name_cn),
            "prompt": _clean(prompt.group(1)) if prompt else f"主题色：{name_cn}。",
            "group": group,
            "img": img,
        })
    return out


def build(force: bool = False) -> dict:
    if OUT.exists() and not force:
        return json.loads(OUT.read_text(encoding="utf-8"))
    data = {
        "styles": parse_styles(),
        "layouts": parse_layouts(),
        "colors": parse_colors(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


if __name__ == "__main__":
    d = build(force="--force" in sys.argv)
    print(f"styles={len(d['styles'])} layouts={len(d['layouts'])} colors={len(d['colors'])}")
