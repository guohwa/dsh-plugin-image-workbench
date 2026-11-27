"""升级文件的单一真理：哪些文件跟随版本更新、内容指纹怎么算。

增量升级（app.py）与发版清单生成（scripts/make_manifest.py）都必须从这里取，
两边各写一份白名单迟早对不上：少一个 → 升级漏更新；多一个 → 白下载甚至覆盖数据。

指纹一律按 LF 行尾算：本仓库 core.autocrlf=true，工作区是 CRLF，npm 包里是 LF，
不规范化就会永远判「内容不一致」，增量直接退化成全量。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# 顶层这几个是插件外壳，跟随版本一起更新
TOP_CODE = {"host.js", "client.js", "cordis.patch.yml", "install.mjs", "package.json"}
# 这些目录整棵不动：虚拟环境、运行态数据、号池内核（几十 MB，升级用不到）
KEEP_ANY = {"vendor", "node_modules", ".venv", "__pycache__", "workbench", "images",
            "thumbs", "refs", "repo_images", ".pytest_cache", ".git"}
KEEP_FILES = {"config.local.json", "app.db", "history.db"}
# 备份 / 编辑器临时文件：npm 按 .gitignore 打包，这些根本不在包里。一旦被收进清单，
# 客户端每次都判「本地缺这个文件」→ 去 CDN 拉 → 404 → 整个增量升级退回整包下载。
# 1.1.120 就是这样：server/static/index.html.bak_xxx 混进清单，增量形同虚设。
JUNK_MARKS = (".bak", ".orig", ".rej", ".swp", ".tmp", ".upnew", ".crdownload")
# server/ 下一层里允许跟随升级的非 py 文件
SERVER_EXTRA = {"catalog.json", "changelog.json", "requirements.txt", "update-manifest.json"}
TEXT_EXT = {".py", ".html", ".css", ".js", ".mjs", ".json", ".txt", ".yml", ".yaml", ".md"}
# 清单文件自己不进清单：内容里要装自己的 sha1，收进去就成了自指
MANIFEST_REL = "server/update-manifest.json"


def upgrade_rel(rel: str) -> str | None:
    """包内的相对路径 -> 该不该写盘。不该写返回 None。"""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    name = parts[-1]
    if any(p in KEEP_ANY for p in parts[:-1]):
        return None
    if name.startswith(".") or any(m in name for m in JUNK_MARKS):
        return None
    if name in KEEP_FILES or name.endswith((".db", ".log", ".pyc", ".upnew")):
        return None
    if len(parts) == 1:
        return rel if name in TOP_CODE else None
    top = parts[0]
    if top == "server":
        if len(parts) == 2:
            ok = name.endswith(".py") or name in SERVER_EXTRA
            return rel if ok else None
        return rel if parts[1] == "static" else None
    if top == "scripts":
        return rel if name.endswith((".mjs", ".py")) else None
    return None


def sha_norm(raw: bytes, rel: str) -> str:
    """内容指纹：文本文件统一按 LF 算，绕开 CRLF / LF 的平台差异。"""
    if Path(rel).suffix.lower() in TEXT_EXT:
        raw = raw.replace(b"\r\n", b"\n")
    return hashlib.sha1(raw).hexdigest()


def manifest_files(root: Path) -> dict:
    """扫出全部该跟随升级的文件，返回 {相对路径: {sha1, size}}。

    只收磁盘上真实存在的文件；清单自身跳过（见 MANIFEST_REL 注释）。
    """
    out: dict = {}
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if rel == MANIFEST_REL:
            continue
        if not upgrade_rel(rel):
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        out[rel] = {"sha1": sha_norm(raw, rel), "size": len(raw)}
    return out


def manifest_diff(files: dict, root: Path) -> list[dict]:
    """远端清单 vs 本地磁盘：返回真正需要下载的文件（新文件或指纹不同）。"""
    out: list[dict] = []
    root = Path(root)
    for rel, meta in (files or {}).items():
        rel = upgrade_rel(str(rel)) or ""
        if not rel:
            continue
        if not isinstance(meta, dict):
            continue
        want = str(meta.get("sha1") or "").strip().lower()
        if not want:
            continue
        target = root / rel
        try:
            raw = target.read_bytes()
        except OSError:
            raw = b""
        if raw and sha_norm(raw, rel) == want:
            continue
        out.append({"rel": rel, "sha1": want, "size": int(meta.get("size") or 0)})
    return out
