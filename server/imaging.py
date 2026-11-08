"""派生图：给历史列表和右侧预览用的压缩副本，原图 PNG 永远不动。

两档：
  card    360px 宽 WebP，历史小卡片，约 40KB
  preview 960px 宽 WebP，右侧排版预览，约 280KB
原图 1.7~3.5MB，只在「双击看大图」和所有下载通道里出现。

生成一次落盘长期复用；按 (job_id, 档位) 加锁防重复计算；先写临时文件
再 os.replace，避免并发请求读到半张图；原图变了按 mtime 自动重建。
"""
from __future__ import annotations

import os
import io
import threading
from pathlib import Path

from PIL import Image, ImageOps

VARIANTS = {
    "card": {"box": (360, 1200), "quality": 78, "method": 4},
    "preview": {"box": (960, 1400), "quality": 84, "method": 4},
}

# 上游号池只出 1:1 / 3:2 / 2:3 三档，公众号首图 2.35:1 这种宽横幅没有原生尺寸，
# 只能是「按 3:2 出图 + 本地居中裁切」。原图始终不裁，裁切只发生在派生图和导出
# 通道里，用户觉得框偏了还能换一处逻辑重裁，不用重新烧配额。
CROP_RATIO = {"2.35:1": 2.35}
CROP_TOL = 0.006          # 宽高比误差在这个范围内就当已经对了，不再裁

_guard = threading.Lock()
_locks: dict[tuple[str, str], threading.Lock] = {}


def _lock_for(key: tuple[str, str]) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _ar(ratio) -> float | None:
    """'2.35:1' -> 2.35。不需要裁切的比例（含空值）返回 None。"""
    return CROP_RATIO.get(str(ratio or "").strip())


def needs_crop(ratio) -> bool:
    """这条记录出图后是不是要额外裁一刀才成品（公众号首图 2.35:1）。"""
    return _ar(ratio) is not None


def _tag(ratio) -> str:
    """裁切版派生图换个文件名，别把没裁的缓存盖掉。"""
    ar = _ar(ratio)
    return "" if ar is None else f"_r{str(ar).replace('.', '')}"


def crop_to(im: Image.Image, ratio) -> Image.Image:
    """居中裁到目标比例。已经够接近就原样返回，避免无谓重编码。"""
    ar = _ar(ratio)
    if not ar:
        return im
    w, h = im.size
    if not w or not h or abs(w / h - ar) < CROP_TOL:
        return im
    if w / h > ar:                       # 比目标宽：裁左右
        nw = int(round(h * ar))
        x = (w - nw) // 2
        return im.crop((x, 0, x + nw, h))
    nh = int(round(w / ar))              # 比目标高：裁上下
    y = (h - nh) // 2
    return im.crop((0, y, w, y + nh))


def variant_path(thumb_dir: Path, job_id: str, kind: str, ratio=None) -> Path:
    """旧 {id}.jpg 是 520px JPEG，新两档用 _c / _p 后缀，互不覆盖。"""
    if kind not in VARIANTS:
        raise ValueError(f"unknown variant: {kind}")
    return thumb_dir / f"{job_id}_{'c' if kind == 'card' else 'p'}{_tag(ratio)}.webp"


def ensure(thumb_dir: Path, job_id: str, kind: str, src: Path, ratio=None) -> Path:
    if kind not in VARIANTS:
        raise ValueError(f"unknown variant: {kind}")
    cfg = VARIANTS[kind]
    dst = variant_path(thumb_dir, job_id, kind, ratio)
    with _lock_for((job_id, kind + _tag(ratio))):
        try:
            if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                return dst
        except OSError:
            pass
        thumb_dir.mkdir(parents=True, exist_ok=True)
        tmp = thumb_dir / f".{job_id}_{kind}_{os.getpid()}_{threading.get_ident()}.tmp"
        try:
            with Image.open(src) as raw:
                im = ImageOps.exif_transpose(raw)
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                if im.mode == "RGBA":
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.getchannel("A"))
                    im = bg
                else:
                    im = im.convert("RGB")
                im = crop_to(im, ratio)
                im.thumbnail(cfg["box"], Image.LANCZOS)
                im.save(tmp, "WEBP", quality=cfg["quality"], method=cfg["method"])
            os.replace(tmp, dst)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return dst


def ensure_all(thumb_dir: Path, job_id: str, src: Path, ratio=None) -> None:
    """出图完成后后台调用：两档一次备齐，点开历史时不用现场等。"""
    for kind in ("card", "preview"):
        try:
            ensure(thumb_dir, job_id, kind, src, ratio)
        except Exception:
            pass


def _flatten(src: Path):
    """打开 → 摆正 → 去 alpha，返回可直接裁切的 RGB 图（调用方负责 close）。"""
    raw = Image.open(src)
    try:
        im = ImageOps.exif_transpose(raw)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            return bg
        return im.convert("RGB")
    finally:
        raw.close()


def export_path(thumb_dir: Path, job_id: str, src: Path, ratio=None) -> Path:
    """导出成品：需要裁切就生成一份缓存 PNG 返回，不需要就原图直接用。

    缓存文件按 (job_id, 比例) 落盘，大图弹窗和下载共用同一份，
    不会每点一次就重新解码一遍 3MB PNG。
    """
    if not _ar(ratio) or not job_id:
        return src
    dst = thumb_dir / f"{job_id}_f{_tag(ratio)}.png"
    with _lock_for((job_id, "export" + _tag(ratio))):
        try:
            if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
                return dst
        except OSError:
            pass
        thumb_dir.mkdir(parents=True, exist_ok=True)
        tmp = thumb_dir / f".{job_id}_export_{os.getpid()}_{threading.get_ident()}.tmp"
        try:
            im = _flatten(src)
            crop_to(im, ratio).save(tmp, "PNG")
            os.replace(tmp, dst)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return dst


def export_bytes(thumb_dir: Path, job_id: str, src: Path, ratio=None) -> bytes:
    """下载 / 打包走这里：拿到的就是成品字节，原图文件本身不动。"""
    return export_path(thumb_dir, job_id, src, ratio).read_bytes()
