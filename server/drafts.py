"""暂存草稿：工作台左侧的选题 / 标题 / 正文 / 分镜清单。

为什么要放到服务端而不是 localStorage：这个工作台会在手机和电脑之间来回用，
本地存一份等于换台设备就没了。文件很小（几十条），整体读写最省事。
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

MAX_DRAFTS = 60  # 超出后按最后修改时间淘汰最旧的

_SENT_SPLIT = re.compile(r'[。！？!?；;\n]')

def first_sentence(text, max_len=48):
    """取正文第一句话，超长截断加省略号。用于草稿列表的预览小字。"""
    text = (text or '').strip()
    if not text:
        return ''
    seg = ''
    for part in _SENT_SPLIT.split(text):
        part = part.strip()
        if part:
            seg = part
            break
    if not seg:
        seg = text
    if len(seg) > max_len:
        seg = seg[:max_len].rstrip() + '…'
    return seg



class Drafts:
    def __init__(self, path: Path) -> None:
        self.path = path
        # 覆盖前的旧版本往这里追加一行，纯保险：真被覆盖了还能手工捞回来
        self.hist_path = path.with_name("drafts_history.jsonl")
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._load()

    def _archive(self, did: str, before: dict) -> None:
        """把即将被覆盖的内容追加到历史文件。写失败不影响主流程。"""
        try:
            line = json.dumps({"id": did, "at": int(time.time()),
                               "topic": before.get("topic") or "",
                               "title": before.get("title") or "",
                               "body": before.get("body") or "",
                               "items": before.get("items") or []}, ensure_ascii=False)
            self.hist_path.parent.mkdir(parents=True, exist_ok=True)
            with self.hist_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if self.hist_path.stat().st_size > 2_000_000:  # 约 2MB 时只留最近 200 条
                keep = self.hist_path.read_text(encoding="utf-8").splitlines()[-200:]
                self.hist_path.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(d, list):
            self._items = [x for x in d if isinstance(x, dict) and x.get("id")]

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _clean(v, depth: int = 0):
        """只留基础类型，前端多传的东西一律丢掉。"""
        if depth > 3:
            return None
        if isinstance(v, str):
            return v[:20000]
        if isinstance(v, (int, float, bool)) or v is None:
            return v
        if isinstance(v, list):
            return [Drafts._clean(x, depth + 1) for x in v[:200]]
        if isinstance(v, dict):
            return {str(k)[:40]: Drafts._clean(x, depth + 1) for k, x in list(v.items())[:40]}
        return None

    def save(self, body: dict) -> dict:
        now = int(time.time())
        did = str(body.get("id") or "").strip() or ("d" + uuid.uuid4().hex[:10])
        with self._lock:
            rec = next((x for x in self._items if x["id"] == did), None)
            if rec is None:
                rec = {"id": did, "created": now}
                self._items.append(rec)
            else:
                # 内容真变了才归档，避免无脑点暂存把历史刷满
                new_body = self._clean(body.get("body") or "")
                new_title = self._clean(body.get("title") or "")
                new_items = self._clean(body.get("items") or []) or []
                if (rec.get("body") or "") != new_body \
                        or (rec.get("title") or "") != new_title \
                        or len(rec.get("items") or []) != len(new_items):
                    self._archive(did, {k: rec.get(k) for k in ("topic", "title", "body", "items")})
            rec.update({
                "updated": now,
                "topic": self._clean(body.get("topic") or ""),
                "title": self._clean(body.get("title") or ""),
                "body": self._clean(body.get("body") or ""),
                "group": self._clean(body.get("group") or ""),
                "note": self._clean(body.get("note") or ""),
                "mood": self._clean(body.get("mood") or ""),
                "ws": self._clean(body.get("ws") or "normal"),
                "ratio": self._clean(body.get("ratio") or "3:4"),
                "mode": self._clean(body.get("mode") or "image"),
                "sel": self._clean(body.get("sel") or {}),
                "ref": self._clean(body.get("ref") or None),
                "items": self._clean(body.get("items") or []),
            })
            rec["count"] = len(rec.get("items") or [])
            if len(self._items) > MAX_DRAFTS:
                self._items.sort(key=lambda x: int(x.get("updated") or 0), reverse=True)
                del self._items[MAX_DRAFTS:]
            self._flush()
            return dict(rec)

    def get(self, did: str) -> dict | None:
        with self._lock:
            rec = next((x for x in self._items if x["id"] == did), None)
            return dict(rec) if rec else None

    def delete(self, did: str) -> bool:
        with self._lock:
            n = len(self._items)
            self._items = [x for x in self._items if x["id"] != did]
            if len(self._items) == n:
                return False
            self._flush()
            return True

    def rename(self, did: str, name: str) -> dict | None:
        """只改草稿自定义名称，不动内容，不改 updated（列表顺序保持不变）。"""
        name = str(name or "").strip()[:60]
        with self._lock:
            rec = next((x for x in self._items if x["id"] == did), None)
            if rec is None:
                return None
            rec["name"] = name
            self._flush()
            return dict(rec)

    def list(self) -> dict:
        with self._lock:
            rows = sorted(self._items, key=lambda x: int(x.get("updated") or 0), reverse=True)
            out = []
            for r in rows:
                items = r.get("items") or []
                # 草稿预览：优先文档正文；图片型草稿正文常为空，退而取第一张分镜的正文
                body_text = r.get("body") or ""
                if not body_text.strip():
                    for it in items:
                        b = (it.get("body") or "").strip()
                        if b:
                            body_text = b
                            break
                preview = first_sentence(body_text)
                out.append({
                    "id": r["id"],
                    "updated": r.get("updated") or r.get("created"),
                    "created": r.get("created"),
                    "topic": r.get("topic") or "",
                    "title": r.get("title") or "",
                    "name": r.get("name") or "",
                    "group": r.get("group") or "",
                    "body": (r.get("body") or "")[:120],
                    "preview": preview,
                    "count": len(items),
                    "titles": [(i.get("title") or "").strip() for i in items[:6]],
                    "mode": r.get("mode") or "image",
                })
        return {"total": len(out), "items": out}
