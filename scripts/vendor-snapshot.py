"""把本机一份 ChatGPT2API 号池源码，做成插件自带的 vendor 快照。

用途：工作台插件发布到别的机器时，不再要求对方先装任何第三方号池插件。
这个脚本只在「升级号池源码」时手动跑一次，日常运行完全不依赖它。

用法：
    python vendor-snapshot.py [源号池 home 目录]

默认源：~/.dsh/chatgpt2api-image
输出：  <插件目录>/vendor/chatgpt2api/

严格排除：账号库 data/、虚拟环境 .venv/、日志、备份、缓存、明文密钥与代理配置。
config.json 会被清洗成 config.default.json（auth-key / proxy / 云存储凭据一律置空）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
OUT = PLUGIN_DIR / "vendor" / "chatgpt2api"

# 只带运行需要的代码与静态资源，不带文档 / 测试 / 容器配置
DIRS = ["api", "services", "utils", "web", "assets", "scripts"]
FILES = ["main.py", "pyproject.toml", "uv.lock", ".python-version",
         ".env.example", "LICENSE", "README.md", "VERSION"]

EXCLUDE_NAMES = {"__pycache__", ".git", ".codex", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log", ".db", ".db-shm", ".db-wal", ".enc")


def keep(p: Path) -> bool:
    n = p.name
    if n in EXCLUDE_NAMES or n.startswith(".dsh-"):
        return False
    if n.endswith(EXCLUDE_SUFFIX) or ".bak" in n or n.endswith(".tmp"):
        return False
    return True


def clean_config(raw: dict) -> dict:
    """出厂默认配置：任何凭据、代理、本机状态都不带进插件包。"""
    d = json.loads(json.dumps(raw))          # 深拷贝
    d["auth-key"] = ""
    d["proxy"] = ""
    pr = d.get("proxy_runtime") or {}
    pr["enabled"] = False
    pr["proxy_url"] = ""
    pr["resource_proxy_url"] = ""
    cl = pr.get("clearance") or {}
    cl["cf_cookies"] = ""
    cl["cf_clearance"] = ""
    d["backup"] = {**(d.get("backup") or {}),
                   "enabled": False, "access_key_id": "",
                   "secret_access_key": "", "passphrase": ""}
    d["ai_review"] = {**(d.get("ai_review") or {}),
                      "enabled": False, "base_url": "", "api_key": ""}
    img = d.get("image_storage") or {}
    img.update({"enabled": False, "webdav_username": "", "webdav_password": ""})
    return d


def main() -> int:
    home = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "chatgpt2api-image")
    src = home / "source"
    if not (src / "main.py").is_file():
        print("找不到号池源码：" + str(src))
        return 1
    if OUT.resolve() == src.resolve():
        print("源目录不能是 vendor 输出目录")
        return 1

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "source").mkdir(parents=True)
    (OUT / "runtime").mkdir(parents=True)

    copied = 0
    for d in DIRS:
        s = src / d
        if not s.is_dir():
            continue
        shutil.copytree(s, OUT / "source" / d, ignore=lambda _, names: [
            n for n in names if not keep(Path(n))], dirs_exist_ok=True)
        copied += sum(1 for _ in (OUT / "source" / d).rglob("*") if _.is_file())
    for f in FILES:
        s = src / f
        if s.is_file() and keep(s):
            shutil.copy2(s, OUT / "source" / f)
            copied += 1

    cfg = src / "config.json"
    if cfg.is_file():
        try:
            data = clean_config(json.loads(cfg.read_text(encoding="utf-8")))
            (OUT / "source" / "config.default.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            copied += 1
        except Exception as e:
            print("警告：config.json 清洗失败，未生成默认配置：" + str(e))

    uv = ""
    for c in (home / "runtime" / "uv.exe", home / "runtime" / "uv",
              shutil.which("uv") or ""):
        if c and Path(c).is_file():
            uv = str(c)
            break
    if uv:
        shutil.copy2(uv, OUT / "runtime" / Path(uv).name)
        copied += 1
    else:
        print("警告：没找到 uv.exe，全新机器初始化时会退回系统 Python + pip")

    note = """# 内置号池运行时

本目录是 ChatGPT2API 号池服务端的**代码快照**，随「Bluxe手绘工坊 · 生图工作台」插件一起分发，
安装后由工作台后端（pool_service.py）释放到本机数据目录并自建虚拟环境。

* 上游：MIT License，版权归原作者 kunkun，LICENSE 原文见 `source/LICENSE`。
* 已包含本工作台需要的两处补丁：round-robin 游标持久化、限流号排除；首次初始化时还会幂等复打一次。
* **不含**任何账号、token、密钥、代理配置或图片数据。`source/config.default.json` 是清洗后的出厂默认配置。
* 升级方式：改完本机号池源码后，重跑 `scripts/vendor-snapshot.py`，再重新发布插件。

分工：工作台插件负责「拉起并守护」号池进程；本目录只提供「能跑起来的代码」。
两者都不需要安装任何第三方 DSH 插件。
"""
    (OUT / "README.md").write_text(note, encoding="utf-8")
    (OUT / "SOURCE.json").write_text(json.dumps({
        "name": "chatgpt2api", "license": "MIT",
        "version": (src / "VERSION").read_text(encoding="utf-8").strip()
        if (src / "VERSION").is_file() else "",
        "python": (src / ".python-version").read_text(encoding="utf-8").strip()
        if (src / ".python-version").is_file() else "",
        "files": copied,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print("vendor 已生成：" + str(OUT))
    print("文件数 %d，体积 %.1f MB" % (copied, size / 1024 / 1024))
    print("含 uv 运行时：" + ("是" if uv else "否"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
