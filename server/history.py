"""生成历史：一条任务一行 JSONL，落盘后可跨重启查询。

图片本身已经在 workbench/images/{job_id}.png，这里只存索引与元信息，
所以历史列表是纯读文件，不会因为图片多而变慢。

两层结构：
- 一行 = 一次出图（job），是历史的原子单位；
- 一次点击「批量出图」会提交 N 个 job，它们共用同一个 task_id。
  前端历史面板按 task 分组展示，默认折叠，展开才看这一批的图。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import imaging

MAX_SCAN = 4000  # 最多回读多少行，防止文件无限增长后启动变慢

# 标题开头的序号：「03 24小时冷冻」→ 3
_TITLE_NO = re.compile(r"^\s*0*(\d{1,3})(?=[\s、.．:：\-—_|/）)]|$)")


def _title_no(s: str) -> int | None:
    m = _TITLE_NO.match(s or "")
    return int(m.group(1)) if m else None


def _order_items(items: list[dict]) -> list[dict]:
    """组内排序：seq 优先，其次标题里的序号，最后才按时间。

    created 只有秒级精度，一次批量出图 8 条基本全落在同一两秒里，只按时间
    排会退化成按随机 id 排，载入历史后顺序就成了 03 01 02 06 04 08 05 07。
    """
    if not items:
        return items
    if all(isinstance(x.get("seq"), int) for x in items):
        return sorted(items, key=lambda x: (x["seq"], int(x.get("created") or 0)))
    nums = [_title_no(x.get("title") or x.get("topic") or "") for x in items]
    if all(v is not None for v in nums) and len(set(nums)) == len(nums):
        return [x for _, x in sorted(zip(nums, items), key=lambda p: p[0])]
    return sorted(items, key=lambda x: (int(x.get("created") or 0), str(x["id"])))


_DROP = object()  # 「这个值没法写进 JSON，丢掉」的哨兵，和合法的 None 区分开


def _clean(v):
    """递归洗成纯 JSON 基础类型：保留 list/dict（失败轨迹就是这么存的），丢掉异常对象。"""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            c = _clean(x)
            if c is not _DROP:
                out[str(k)] = c
        return out
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            c = _clean(x)
            if c is not _DROP:
                out.append(c)
        return out
    return _DROP


def _safe(d: dict) -> dict:
    """只保留可 JSON 序列化的内容，避免 job 里混进 Task / 异常对象。

    以前只认 str/int/float/bool/None，顺手把 list/dict 也丢了，
    导致 attempts（每次失败用的哪个号）压根存不进历史。
    """
    out = {}
    for k, v in d.items():
        c = _clean(v)
        if c is not _DROP:
            out[k] = c
    return out


class History:
    def __init__(self, path: Path, images_dir, thumbs_dir: Path | None = None,
                 finder=None) -> None:
        self.path = path
        # images_dir 可以是 Path，也可以是返回 Path 的函数：
        # 用户在工作台里改了「生成图片保存目录」后，这里要立刻跟着走，不能等重启。
        self._images = images_dir
        # finder(name) 负责多目录兜底（换过保存目录后老图仍可读）
        self._finder = finder
        self.thumbs_dir = Path(thumbs_dir) if thumbs_dir else (
            self.images_dir.parent / "thumbs")
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._ids: set[str] = set()
        self._load()

    @property
    def images_dir(self) -> Path:
        d = self._images
        return Path(d() if callable(d) else d)

    # ---------- load / write ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return
        rows: list[dict] = []
        for ln in lines[-MAX_SCAN:]:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if isinstance(d, dict) and d.get("id"):
                rows.append(d)
        self._items = rows
        self._ids = {r["id"] for r in rows}

    def _append(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ---------- write ----------
    def record(self, job: dict) -> None:
        """任务结束时调用一次。同一个 id 只记一次，重试不产生两条。"""
        jid = str(job.get("id") or "")
        if not jid:
            return
        result = job.get("result") or {}
        rec = _safe({
            "id": jid,
            "task_id": job.get("task_id") or "",
            "created": job.get("created") or int(time.time()),
            "finished": job.get("finished") or int(time.time()),
            "mode": job.get("mode") or "image",
            "status": job.get("status") or "done",
            "topic": job.get("topic") or "",
            "title": job.get("title") or job.get("topic") or "",
            "body": job.get("body") or "",
            # 整篇正文（左侧「正文」框），载入历史时还原用；老记录没有这个字段
            "doc_body": job.get("doc_body") or "",
            # 这一批里的提交序号，0 起。并行出图完成顺序是乱的，靠它还原清单顺序
            "seq": job.get("seq"),
            "group": job.get("group") or "",
            "account": job.get("account") or "",
            "env_id": job.get("env_id"),
            "style_id": job.get("style_id"),
            "layout_id": job.get("layout_id"),
            "color_id": job.get("color_id"),
            "ratio": job.get("ratio"),
            "ref_id": job.get("ref_id") or "",
            # 出图模式（图文 / 摄影）与拍摄类型，载入历史时一起还原
            "image_mode": job.get("image_mode") or "graphic-text",
            "shoot_type": job.get("shoot_type") or "",
            "prompt": job.get("prompt") or "",
            # 失败/换号的完整轨迹：第几次、哪个号、什么错。成功那次也在里面，
            # 前端据此显示「换 2 个号后成功」或「换了 3 个号仍未成功」
            "attempts": job.get("attempts") or [],
            "account_email": job.get("account_email") or "",
            "image": result.get("image"),
            "width": result.get("width"),
            "height": result.get("height"),
            "bytes": result.get("bytes"),
            "error": job.get("error") or "",
        })
        with self._lock:
            if jid in self._ids:
                return
            self._ids.add(jid)
            self._items.append(rec)
            try:
                self._append(rec)
            except Exception:
                pass

    # ---------- read ----------
    def get(self, job_id: str) -> dict | None:
        with self._lock:
            return next((r for r in reversed(self._items) if r["id"] == job_id), None)

    def image_path(self, job_id: str) -> Path | None:
        rec = self.get(job_id)
        if not rec or not rec.get("image"):
            return None
        name = str(rec["image"])
        if self._finder:
            return self._finder(name)
        for d in (self.images_dir, self.images_dir.parent / "images"):
            p = d / name
            if p.is_file():
                return p
        return None

    def _any_image(self, name: str) -> Path:
        """按文件名定位原图：交给 finder 做多目录兜底，没注入就按当前目录找。"""
        if self._finder:
            hit = self._finder(name)
            if hit:
                return hit
        return self.images_dir / name

    def list(self, limit: int = 24, offset: int = 0, q: str = "", status: str = "") -> dict:
        q = (q or "").strip().lower()
        status = (status or "").strip()
        with self._lock:
            rows = list(reversed(self._items))
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if q:
            rows = [r for r in rows if q in " ".join(
                str(r.get(k) or "") for k in ("title", "topic", "group", "account", "prompt")
            ).lower()]
        total = len(rows)
        limit = max(1, min(int(limit or 24), 100))
        offset = max(0, int(offset or 0))
        page = rows[offset:offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < total,
            "items": [self._public(r) for r in page],
        }

    # ---------- 按任务分组 ----------
    def _task_brief(self, r: dict) -> dict:
        """任务里的单张记录：去掉提示词全文，列表才不会一次传几百 KB。"""
        img = r.get("image")
        return {
            "id": r["id"],
            "title": r.get("title") or r.get("topic") or "",
            "status": r.get("status") or "done",
            "mode": r.get("mode") or "image",
            "account": r.get("account") or "",
            "finished": r.get("finished"),
            "width": r.get("width"),
            "height": r.get("height"),
            "bytes": r.get("bytes"),
            "error": (r.get("error") or "")[:200],
            "body": (r.get("body") or "")[:160],
            "has_prompt": bool((r.get("prompt") or "").strip()),
            "image_url": f"/files/{img}" if img else None,
            "thumb_url": f"/api/history/{r['id']}/thumb" if img else None,
            "download_url": f"/api/history/{r['id']}/download" if img else None,
        }

    @staticmethod
    def _group_rows(rows: list[dict]) -> tuple[list[str], dict[str, dict]]:
        """把行聚成任务。列表、载入、删除都必须用这同一套口径，否则删错行。"""
        groups: dict[str, dict] = {}
        order: list[str] = []
        prev_key: str | None = None
        prev_g = ""
        prev_ts = 0

        for r in rows:
            tid = str(r.get("task_id") or "")
            g = str(r.get("group") or "").strip()
            ts = int(r.get("created") or 0)
            if tid:
                key = "t:" + tid
                prev_key = None
            else:
                merged = bool(prev_key and g and g == prev_g and ts and prev_ts
                              and 0 <= ts - prev_ts <= 300)
                if not merged:
                    prev_key = ("s:" + r["id"]) if g else None
                key = prev_key or ("s:" + r["id"])
                prev_g = g
                prev_ts = ts

            t = groups.get(key)
            if t is None:
                t = {
                    "task_id": key.split(":", 1)[1],
                    "title": g or r.get("title") or r.get("topic") or "未命名",
                    "group": g,
                    "created": ts or int(r.get("finished") or 0),
                    "finished": 0,
                    "total": 0,
                    "done": 0,
                    "error": 0,
                    "modes": {},
                    "items": [],
                }
                groups[key] = t
                order.append(key)
            t["total"] += 1
            fin = int(r.get("finished") or 0)
            t["finished"] = max(t["finished"], fin)
            if r.get("status") == "done":
                t["done"] += 1
            elif r.get("status") == "error":
                t["error"] += 1
            m = r.get("mode") or "image"
            t["modes"][m] = t["modes"].get(m, 0) + 1
            t["items"].append(r)

        # 组内按提交顺序（01 02 03 的顺序感）
        for t in groups.values():
            t["items"] = _order_items(t["items"])
        return order, groups

    def _task_rows(self, task_id: str) -> list[dict]:
        """按任务号取整组行。老记录的合成任务号就是组内第一条记录的 id。"""
        with self._lock:
            rows = list(self._items)
        _, groups = self._group_rows(rows)
        g = groups.get("t:" + task_id) or groups.get("s:" + task_id)
        if g:
            return g["items"]
        return [r for r in rows if str(r.get("id")) == task_id]

    def task_detail(self, task_id: str) -> dict | None:
        """载入历史任务用：带提示词全文，外加当时选的图型/风格/主题色/比例。"""
        rows = self._task_rows(task_id)
        if not rows:
            return None
        first = rows[0]

        def pick(k: str):
            for r in rows:
                v = r.get(k)
                if v not in (None, ""):
                    return v
            return None

        items = [{
            "id": r["id"],
            "title": r.get("title") or r.get("topic") or "",
            "body": r.get("body") or "",
            "prompt": r.get("prompt") or "",
            "status": r.get("status") or "done",
            "mode": r.get("mode") or "image",
            "image_url": f"/files/{r['image']}" if r.get("image") else None,
        } for r in rows]
        return {
            "task_id": task_id,
            "title": first.get("group") or first.get("title") or first.get("topic") or "未命名任务",
            "group": first.get("group") or "",
            "topic": first.get("topic") or "",
            # 整篇正文：同一任务内每条都一样，取最长的那条（防止个别记录没写进去）
            "doc_body": max((r.get("doc_body") or "" for r in rows), key=len),
            "created": first.get("created"),
            "style_id": pick("style_id"),
            "layout_id": pick("layout_id"),
            "color_id": pick("color_id"),
            "ratio": pick("ratio"),
            "ref_id": pick("ref_id"),
            "image_mode": pick("image_mode") or "graphic-text",
            "shoot_type": pick("shoot_type") or "",
            "items": items,
        }

    def bundle_data(self, task_id: str) -> dict | None:
        """打包下载用：整组行（已按提交顺序排好）+ 任务标题 + 整篇正文。"""
        rows = self._task_rows(task_id)
        if not rows:
            return None
        first = rows[0]
        return {
            "title": first.get("group") or first.get("title") or first.get("topic") or "未命名任务",
            "doc_body": max((r.get("doc_body") or "" for r in rows), key=len),
            "rows": rows,
        }

    def _rewrite(self, rows: list[dict]) -> None:
        """整文件重写：JSONL 删行只能这么干。先写 tmp 再 rename，中途断电也不会剩半截。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(self.path)

    def drop_record(self, job_id: str, task_id: str | None = None) -> dict:
        """单张重做：删掉一条旧记录（索引 + 原图 + 缩略图）。

        task_id 给了就要求旧记录属于同一个任务，防止前端传错 id 删到别处。
        只在新图成功后调用；失败时旧图必须原样留着。
        """
        jid = str(job_id or "")
        if not jid:
            return {"ok": False, "deleted": 0, "images": 0}
        with self._lock:
            hit = next((r for r in self._items if str(r.get("id")) == jid), None)
            if not hit:
                return {"ok": False, "deleted": 0, "images": 0}
            if task_id and str(hit.get("task_id") or "") != str(task_id):
                return {"ok": False, "deleted": 0, "images": 0}
            keep = [r for r in self._items if str(r.get("id")) != jid]
            self._items = keep
            self._ids.discard(jid)
            self._rewrite(keep)
        thumbs = self.thumbs_dir
        gone = 0
        paths = ((self._any_image(str(hit["image"]))) if hit.get("image") else None,
                 thumbs / (jid + ".jpg"),
                 thumbs / (jid + "_c.webp"), thumbs / (jid + "_p.webp"))
        for p in paths:
            if p is None:
                continue
            try:
                if p.is_file():
                    p.unlink()
                    gone += 1
            except Exception:
                pass
        return {"ok": True, "deleted": 1, "images": gone}

    def delete_task(self, task_id: str) -> dict:
        """删掉整个任务：索引、图片原图、缩略图一起清。

        图片文件名带 job id，不存在两条记录共用一张图，所以可以直接删文件。
        """
        rows = self._task_rows(task_id)
        if not rows:
            return {"ok": False, "deleted": 0, "images": 0}
        ids = {str(r["id"]) for r in rows}
        with self._lock:
            dropped = [r for r in self._items if str(r["id"]) in ids]
            keep = [r for r in self._items if str(r["id"]) not in ids]
            self._items = keep
            self._ids -= ids
            self._rewrite(keep)
        # 文件删除放在锁外：磁盘慢的时候别把出图线程堵住
        thumbs = self.thumbs_dir
        gone = 0
        for r in dropped:
            for p in ((self._any_image(str(r["image"]))) if r.get("image") else None,
                      thumbs / (str(r["id"]) + ".jpg"),
                      thumbs / (str(r["id"]) + "_c.webp"),
                      thumbs / (str(r["id"]) + "_p.webp")):
                if p is None:
                    continue
                try:
                    if p.is_file():
                        p.unlink()
                        gone += 1
                except Exception:
                    pass
        return {"ok": True, "deleted": len(dropped), "images": gone}

    def tasks(self, limit: int = 20, offset: int = 0, q: str = "") -> dict:
        """按 task 聚合。老记录没有 task_id，用「同 group + 5 分钟内」近似归并。"""
        q = (q or "").strip().lower()
        with self._lock:
            rows = list(self._items)  # 写入顺序 ≈ 时间顺序

        order, groups = self._group_rows(rows)
        tasks = [groups[k] for k in order]
        tasks.sort(key=lambda t: (t["finished"] or t["created"], t["task_id"]), reverse=True)

        if q:
            def hit(t: dict) -> bool:
                blob = " ".join([str(t.get("title") or ""), str(t.get("group") or "")] +
                                [str(i.get("title") or "") for i in t["items"]])
                return q in blob.lower()
            tasks = [t for t in tasks if hit(t)]

        total = len(tasks)
        limit = max(1, min(int(limit or 20), 100))
        offset = max(0, int(offset or 0))
        page = tasks[offset:offset + limit]
        out = []
        for t in page:
            d = {
                "task_id": t["task_id"],
                "title": t["title"],
                "group": t["group"],
                "created": t["created"],
                "finished": t["finished"],
                "total": t["total"],
                "done": t["done"],
                "error": t["error"],
                "modes": t["modes"],
                "items": [self._task_brief(i) for i in t["items"]],
            }
            out.append(d)
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(page) < total,
            "page": offset // limit + 1,
            "pages": max(1, (total + limit - 1) // limit),
            "items": out,
        }

    def _public(self, r: dict) -> dict:
        d = dict(r)
        img = r.get("image")
        d["raw_url"] = f"/files/{img}" if img else None
        # 公众号首图 2.35:1 是裁出来的成品：列表大图、下载都给裁好的那张，
        # 未裁原图留在 raw_url，需要回头看完整构图时还能拿
        d["image_url"] = (f"/api/history/{r['id']}/final"
                          if img and imaging.needs_crop(r.get("ratio")) else d["raw_url"])
        d["thumb_url"] = f"/api/history/{r['id']}/thumb" if img else None
        d["download_url"] = f"/api/history/{r['id']}/download" if img else None
        return d

    def stats(self) -> dict:
        today = time.strftime("%Y-%m-%d")
        with self._lock:
            rows = list(self._items)
        done = sum(1 for r in rows if r.get("status") == "done")
        err = sum(1 for r in rows if r.get("status") == "error")
        t_done = t_err = 0
        for r in rows:
            if time.strftime("%Y-%m-%d", time.localtime(int(r.get("finished") or 0))) != today:
                continue
            if r.get("status") == "done":
                t_done += 1
            elif r.get("status") == "error":
                t_err += 1
        return {
            "total": len(rows),
            "done": done,
            "error": err,
            "today_done": t_done,
            "today_error": t_err,
        }

    def account_stats(self) -> tuple[dict, dict]:
        """（按 env_id, 按掩码）两种账号统计。

        掩码只留前两位，Hou*** 这种会撞；新记录都带 env_id，按 env 匹配才准。
        没有 env_id 的老记录退回按掩码匹配。
        """
        with self._lock:
            rows = list(self._items)
        by_env: dict = {}
        by_mask: dict = {}
        for r in rows:
            ok = r.get("status") == "done"
            fin = int(r.get("finished") or 0)
            env = r.get("env_id")
            if env not in (None, ""):
                d = by_env.setdefault(str(env), {"done": 0, "error": 0, "last_ok": 0})
                if ok:
                    d["done"] += 1
                    d["last_ok"] = max(d["last_ok"], fin)
                else:
                    d["error"] += 1
            else:
                k = str(r.get("account") or "")
                if not k:
                    continue
                d = by_mask.setdefault(k, {"done": 0, "error": 0, "last_ok": 0})
                if ok:
                    d["done"] += 1
                    d["last_ok"] = max(d["last_ok"], fin)
                else:
                    d["error"] += 1
        return by_env, by_mask

    def recent_errors(self, n: int = 6) -> list[dict]:
        with self._lock:
            rows = [r for r in reversed(self._items) if r.get("status") == "error"]
        return [{"id": r["id"], "title": r.get("title") or r.get("topic"),
                 "error": r.get("error"), "finished": r.get("finished"),
                 "account": r.get("account")} for r in rows[:n]]
