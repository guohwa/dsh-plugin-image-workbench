#!/usr/bin/env python3
"""从上游 handraw-style 仓库拉取真实风格样图到 repo_images/。

仓库本身只含代码，真实样图不进 git（避免仓库膨胀）。安装时或手动点击
「下载风格样例图」时按需拉取。网络不可用则静默跳过，前端自动回退到 SVG 占位。

映射关系：catalog 里每个条目的 img 字段形如 `images/individual/FA/FA-001.webp`，
上游仓库根目录正是 `images/...`，因此把上游 `images/` 整体拷到本插件 `repo_images/`
即可，路径天然对齐，`/img/individual/FA/FA-001.webp` 就能命中真实样图。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UPSTREAM = "https://github.com/yang0/handraw-style"
DEST = Path(__file__).resolve().parent.parent / "repo_images"


def _clone(td: str) -> bool:
    """优先只拉 images/（sparse），失败则退回完整浅克隆。带超时，网络不通时快速失败。"""
    # 1) sparse 克隆，仅取 images 树，省流量
    r = subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", UPSTREAM, td],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode == 0:
        r2 = subprocess.run(["git", "-C", td, "sparse-checkout", "set", "images"],
                            capture_output=True, text=True, timeout=30)
        if r2.returncode == 0:
            return True
    # 2) 回退：完整浅克隆
    r = subprocess.run(["git", "clone", "--depth", "1", UPSTREAM, td],
                       capture_output=True, text=True, timeout=90)
    return r.returncode == 0


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        if not _clone(td):
            print("CLONE_FAIL")
            return 2
        src = Path(td) / "images"
        if not src.is_dir():
            print("NO_IMAGES_DIR")
            return 3
        copied = 0
        for p in src.rglob("*"):
            if p.is_file():
                rel = p.relative_to(src)
                dst = DEST / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                copied += 1
        print(f"OK copied={copied}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
