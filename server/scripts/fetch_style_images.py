#!/usr/bin/env python3
"""下载 handraw-style 真实风格样图到本插件 repo_images/。

设计要点
--------
- 真实样图来自上游 handraw-style 的图床 CDN：`https://img.handraw-style.com`
  该域名在国内可直连，且路径与 catalog 的 `img` 字段完全一致
  （`images/individual/FA/FA-001.webp`），无需任何改写即可命中。
- 仓库本身只含代码，真实样图**不进 git**（避免仓库膨胀）。安装时或手动点击
  「下载风格样例图」时按需下载，网络不可用则静默跳过，前端自动回退到 SVG 占位。
- 下载清单直接来自 `catalog.build()`，始终与画廊当前数据保持一致；已存在的文件
  跳过，支持断点续传；gitee / github raw 仅作兜底来源。
"""
from __future__ import annotations

import concurrent.futures as cf
import sys
from pathlib import Path

import httpx

# 来源优先级：CDN（国内直连、路径精确）> gitee 镜像 raw > github raw
CDN = "https://img.handraw-style.com"
GITEE = "https://gitee.com/yang02010/handraw-style/raw/main"
GITHUB = "https://raw.githubusercontent.com/yang0/handraw-style/main"

SERVER_DIR = Path(__file__).resolve().parent.parent
DEST = SERVER_DIR / "repo_images"
# 把 server/ 加入 sys.path，便于 import catalog
sys.path.insert(0, str(SERVER_DIR))

WORKERS = 12
TIMEOUT = 25.0


def _list_images():
    """从 catalog 取出全部需要下载的相对路径（images/individual/...webp）。"""
    try:
        import catalog
        styles = catalog.build().get("styles", [])
    except Exception as e:  # catalog 缺失则退回保守清单
        print("CATALOG_FAIL:" + str(e)[:120])
        styles = []
    rels = [s.get("img") for s in styles if s.get("img")]
    # catalog 为空时，用线上画廊已知的前缀兜底（FA-FI 九类）
    if not rels:
        for pre in "FA FB FC FD FE FF FG FH FI".split():
            for n in range(1, 80):
                rels.append(f"images/individual/{pre}/{pre}-{n:03d}.webp")
    # 去重并保持顺序
    seen, out = set(), []
    for r in rels:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _sources(rel: str):
    return [f"{CDN}/{rel}", f"{GITEE}/{rel}", f"{GITHUB}/{rel}"]


def _fetch_one(rel: str):
    # catalog 的 img 形如 `images/individual/FA/FA-001.webp`；前端 imgUrl() 会去掉
    # 开头的 `images/`，路由也在 repo_images/ 下按 `individual/...` 查找，故本地落盘
    # 时同步去掉该前缀，保证 /img/individual/FA/FA-001.webp 能命中。
    rel_local = rel[len("images/"):] if rel.startswith("images/") else rel
    dst = DEST / rel_local
    if dst.exists() and dst.stat().st_size > 0:
        return ("skip", rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for url in _sources(rel):
        try:
            # 图床在国内可直连，默认不走系统代理（避免代理异常时整体卡死）；
            # 若直连失败，下面 gitee / github raw 兜底来源才可能需要代理——但那两个
            # 在受限网络下通常也不可达，仅作「尽力而为」的兜底，失败即跳过。
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True, trust_env=False) as c:
                r = c.get(url)
            if r.status_code == 200 and len(r.content) > 200:
                dst.write_bytes(r.content)
                return ("ok", rel)
            last_err = f"{r.status_code}"
        except Exception as e:
            last_err = type(e).__name__
    return ("fail", rel + " :: " + last_err)


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    rels = _list_images()
    total = len(rels)
    if not total:
        print("NO_IMAGES")
        return 3
    done = skipped = failed = 0
    fails = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for status, rel in ex.map(_fetch_one, rels):
            if status == "ok":
                done += 1
            elif status == "skip":
                skipped += 1
            else:
                failed += 1
                fails.append(rel)
    print(f"DONE total={total} downloaded={done} skipped={skipped} failed={failed}")
    if fails:
        print("FAILED_FIRST:" + fails[0])
        for f in fails[:15]:
            print("  " + f)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
