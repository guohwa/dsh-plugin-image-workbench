"""每日热榜条目的「事实资料」抓取。

为什么要有这个文件：热榜快照里一行只有标题、平台、排名、热度和一个链接，
没有摘要。用户点「用作选题」后如果直接拿标题去写，模型只能靠训练记忆补
日期、数字和人名，事件型稿子几乎必错。这里补一条取证链路：

  L1 抓原文    有 url 就先试抓正文（复用 trends/wechat-article.js 的提取器）。
               公众号、部分新闻站可抓；头条/抖音/微博的榜单页是 JS 渲染，抓不到。
  L2 标题检索  抓不到就把标题丢去必应 RSS，取 3-5 条不同来源的新闻摘要拼成资料块。
  L3 显式降级  两级都空时返回 level=none，由提示词明令只写框架、不许编数字。

三者按顺序降级，L1 与 L2 并发跑，谁先给出足够内容用谁，L1 优先。
取到的结果在进程内缓存 2 小时，写稿时按 id 复用，不必让前端回传正文。
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

_ROOT = Path(__file__).resolve().parent
_ARTICLE_JS = _ROOT / "trends" / "wechat-article.js"

CACHE_TTL = 2 * 3600
_MAX_CACHE = 240

# 这些域名下的链接基本是 JS 单页或强反爬，抓正文只会白等超时，直接跳 L2
_SKIP_HOSTS = (
    "toutiao.com", "douyin.com", "weibo.com", "weibo.cn", "baidu.com",
    "zhihu.com", "bilibili.com", "x.com", "twitter.com", "youtube.com",
    "xiaohongshu.com", "kuaishou.com",
)

# 必应 RSS 里混进来的门户首页、下载页、导航条，摘要全是站点简介，不是新闻
_JUNK_URL_HOSTS = (
    "baidu.com", "chatgpt.com", "openai.com", "smapply.org", "bing.com",
    "microsoft.com", "google.com", "apple.com",
)
_JUNK_DESC = (
    "致力于让网民更便捷", "使用百度前必读", "下载适用于", "全球领先的中文搜索引擎",
    "互联网药品信息服务", "京公网安备", "京ICP证", "登录后", "请开启JavaScript",
)

_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")

_cache: dict[str, dict] = {}


# ---------------- 对外主入口 ----------------

def key_of(title: str, url: str = "") -> str:
    """资料缓存键：同一标题换了链接也算同一条，热榜每次快照的 url 参数会变。"""
    import hashlib
    base = (str(title or "").strip() + "|" + _host(url)).encode("utf-8", "replace")
    return hashlib.sha1(base).hexdigest()[:16]


def cached(key: str) -> dict | None:
    hit = _cache.get(key)
    if not hit:
        return None
    if time.time() - hit["at"] > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return hit["data"]


def remember(key: str, data: dict) -> None:
    if len(_cache) >= _MAX_CACHE:
        # 满了就丢最旧的一批，不做精确 LRU：这份缓存只是省一次请求
        for k in sorted(_cache, key=lambda x: _cache[x]["at"])[:40]:
            _cache.pop(k, None)
    _cache[key] = {"at": time.time(), "data": data}


async def build(title: str, url: str = "", platform: str = "",
                timeout: float = 12.0) -> dict:
    """抓一条热点的资料。返回 {level, digest, sources, title, platform, chars}。"""
    title = _one_line(title)[:200]
    url = str(url or "").strip()
    platform = _one_line(platform)[:40]
    if not title:
        return _empty(title, platform)

    key = key_of(title, url)
    hit = cached(key)
    if hit:
        return hit

    # L1 与 L2 并发：抓原文通常 1-2s，检索 1-2s；串行会白等一倍
    arts, snips = await asyncio.gather(
        _fetch_article(url) if url and not _skip(url) else _none(),
        _search(title),
        return_exceptions=True,
    )
    art = arts if isinstance(arts, dict) else None
    rows = [x for x in (snips if isinstance(snips, list) else []) if x]

    data = _pack(title, platform, url, art, rows)
    remember(key, data)
    return data


def _empty(title: str, platform: str) -> dict:
    return {"level": "none", "title": title, "platform": platform,
            "digest": "", "sources": [], "chars": 0}


def _pack(title: str, platform: str, url: str, art: dict | None,
          rows: list[dict]) -> dict:
    """先看原文够不够长，再退回检索摘要。两个都空就是 L3。"""
    if art and len(str(art.get("text") or "")) >= 400:
        body = _trim_article(str(art.get("text") or ""))
        src = [{"n": 1, "title": _one_line(art.get("title") or title)[:80],
                "site": _host(art.get("url") or url) or platform or "原文",
                "date": "", "url": str(art.get("url") or url)[:400]}]
        digest = "原文正文（已截取关键段落）：\n" + body
        return {"level": "article", "title": title, "platform": platform,
                "digest": digest, "sources": src, "chars": len(body)}

    packed = _pack_snippets(rows, title)
    if packed:
        return {"level": "search", "title": title, "platform": platform,
                "digest": packed["digest"], "sources": packed["sources"],
                "chars": packed["chars"]}
    return _empty(title, platform)


# ---------------- L1 原文 ----------------

async def _none():
    return None


def _skip(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS) if host else True


async def _fetch_article(url: str) -> dict | None:
    """调 node 跑 trends/wechat-article.js 的 fetchArticle，失败返回 None。"""
    node = shutil.which("node")
    if not node or not _ARTICLE_JS.exists():
        return None
    script = (
        "import { pathToFileURL } from 'node:url';\n"
        "const m = await import(pathToFileURL(" + json.dumps(_ARTICLE_JS.as_posix()) + ").href);\n"
        "try {\n"
        "  const r = await m.fetchArticle(" + json.dumps(url) + ", { timeout: 9000 });\n"
        "  process.stdout.write(JSON.stringify({title: r.title || '', url: r.url || " + json.dumps(url) + ", text: r.text || ''}));\n"
        "} catch (e) { process.stdout.write(JSON.stringify({error: String(e && e.message || e).slice(0, 120)})); }\n"
    )
    try:
        out = await asyncio.to_thread(
            subprocess.run,
            [node, "--input-type=module", "-e", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=14,
        )
    except Exception:
        return None
    raw = (out.stdout or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if data.get("error") or not str(data.get("text") or "").strip():
        return None
    return data


def _trim_article(text: str, limit: int = 1800) -> str:
    """留前 1800 字，尽量切在句号上，别把一句话劈成两半。"""
    t = re.sub(r"[ \t]+", " ", str(text or ""))
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    stop = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("\n"))
    if stop >= limit * 0.6:
        cut = cut[:stop + 1]
    return cut.strip()


# ---------------- L2 标题检索 ----------------

async def _search(title: str) -> list[dict]:
    """必应 RSS 检索。

    线路按「直连 cn → 直连 www → 走系统代理」逐个试：本机实测经代理要 6s 起，
    直连只要 2s。直连偶尔被网络环境挡住时，再退回代理那条。
    """
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0 Safari/537.36"}
    params = {"q": title, "format": "rss", "count": "12", "setlang": "zh-CN"}
    routes = (
        ("https://cn.bing.com/search", False),
        ("https://www.bing.com/search", False),
        ("https://www.bing.com/search", True),
    )
    for url, use_proxy in routes:
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                         headers=headers,
                                         trust_env=use_proxy) as cli:
                r = await cli.get(url, params=params)
                body = r.text
        except Exception:
            continue
        rows = filter_snippets(parse_rss(body), title)
        if rows:
            return rows
    return []


def parse_rss(body: str) -> list[dict]:
    """必应 RSS → [{title, desc, date, link}]。不引第三方 XML 库，格式很固定。"""
    out = []
    for raw in re.findall(r"<item>(.*?)</item>", str(body or ""), re.S):
        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}>(.*?)</{tag}>", raw, re.S)
            if not m:
                return ""
            txt = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
            return html.unescape(re.sub(r"<.*?>", "", txt)).strip()

        link = grab("link")
        out.append({"title": grab("title"), "desc": grab("description"),
                    "date": grab("pubDate"), "link": link})
    return out


def filter_snippets(rows: list[dict], title: str) -> list[dict]:
    """去掉门户首页与站点简介，按「与标题重合的字面量」排序，一个站点只要一条。"""
    probe = _probe(title)
    picked: list[dict] = []
    seen_host: set[str] = set()
    scored: list[tuple[int, dict]] = []
    for r in rows:
        desc = _one_line(r.get("desc") or "")
        link = str(r.get("link") or "").strip()
        head = _one_line(r.get("title") or "")
        if len(desc) < 45 or not head or not link:
            continue
        if any(j in desc for j in _JUNK_DESC) or any(j in head for j in _JUNK_DESC):
            continue
        host = _host(link)
        if not host or any(host == h or host.endswith("." + h) for h in _JUNK_URL_HOSTS):
            continue
        path = urlparse(link).path.strip("/")
        if not path:
            continue
        score = _overlap(head + desc, probe)
        # 只按「和标题重合 2 字片段」判相关性。原先留了个「摘要里有数字就放行」的口子，
        # 结果一个瞎编的标题也能召回到带「300 首」「4 分钟」的网易云问答，那种资料
        # 比没有资料更糟：模型会把它当事实写进稿子。宁可降级，也不喂垃圾。
        need = 4 if len(probe) >= 10 else 3
        if score < need:
            continue
        scored.append((score, {"title": head[:90], "desc": desc[:260],
                               "date": _date_of(r.get("date"), desc),
                               "link": link[:400], "host": host}))
    scored.sort(key=lambda x: -x[0])
    for _, row in scored:
        if row["host"] in seen_host:
            continue
        seen_host.add(row["host"])
        picked.append(row)
        if len(picked) >= 5:
            break
    for i, row in enumerate(picked, 1):
        row["n"] = i
        row["site"] = _site_name(row["host"])
    return picked


def _pack_snippets(rows: list[dict], title: str) -> dict | None:
    if len(rows) < 2:
        return None
    lines = ["公开新闻报道的检索摘要（同一事件的不同来源，按相关度排序）："]
    used = 0
    for r in rows[:5]:
        head = r["title"]
        meta = "，".join(x for x in (r.get("site") or "", r.get("date") or "") if x)
        one = f"[{r['n']}] {head}" + (f"（{meta}）" if meta else "")
        desc = r["desc"]
        if used + len(one) + len(desc) > 2600:
            desc = desc[: max(0, 2600 - used - len(one))]
        if not desc:
            break
        lines.append(one)
        lines.append(desc)
        used += len(one) + len(desc)
    if len(lines) <= 2:
        return None
    digest = "\n".join(lines)
    srcs = [{"n": r["n"], "title": r["title"], "site": r.get("site") or "",
             "date": r.get("date") or "", "url": r["link"]} for r in rows[:5]]
    return {"digest": digest, "sources": srcs, "chars": len(digest)}


# ---------------- 小工具 ----------------

def _host(url: str) -> str:
    try:
        h = (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _site_name(host: str) -> str:
    """news.sina.com.cn → sina，m.163.com → 163。取到域名主体，不显示成 com/net。"""
    parts = [p for p in str(host or "").split(".") if p]
    if len(parts) >= 3 and ".".join(parts[-2:]) in (
            "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
            "com.hk", "com.tw", "co.uk", "co.jp"):
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return host or ""


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _probe(title: str) -> set[str]:
    """把标题切成 2 字片段，用来衡量检索结果和标题沾不沾边。"""
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(title or ""))
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else set()


def _overlap(text: str, probe: set[str]) -> int:
    if not probe:
        return 0
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", str(text or ""))
    return len(probe & {t[i:i + 2] for i in range(len(t) - 1)})


def _date_of(raw: str, desc: str) -> str:
    """优先用 RSS 的 pubDate，解析不出来再从摘要正文里找 2026-11-25 这种写法。"""
    text = str(raw or "")
    m = re.search(r"(\d{1,2}) (\w{3}) (\d{4})", text)
    if m:
        months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                  "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
        mon = months.get(m.group(2)[:3].title(), 0)
        if mon:
            return f"{int(m.group(3))}-{mon:02d}-{int(m.group(1)):02d}"
    hit = _DATE_RE.search(text) or _DATE_RE.search(str(desc or ""))
    if hit:
        return f"{int(hit.group(1))}-{int(hit.group(2)):02d}-{int(hit.group(3)):02d}"
    return ""
