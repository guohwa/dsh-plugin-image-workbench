"""生成 server/update-manifest.json —— 增量升级比对用的文件指纹清单。

发版前必跑（不跑的话新包里带的还是上一版指纹，客户端会漏掉改过的文件）：

    python scripts/make_manifest.py

指纹按 LF 行尾算（见 server/upgrade_files.py）：工作区是 CRLF、包里是 LF，
不规范化就会永远判「不一致」。脚本自己写 LF，免得和包内内容差一个 \\r。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "server") not in sys.path:
    sys.path.insert(0, str(ROOT / "server"))

from upgrade_files import MANIFEST_REL, manifest_files   # noqa: E402


def main() -> int:
    ver = str(json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("version") or "").strip()
    if not ver:
        print("package.json 里读不到 version")
        return 1
    files = manifest_files(ROOT)
    if not files:
        print("没扫到任何可升级文件，路径不对？")
        return 1
    dest = ROOT / MANIFEST_REL
    dest.write_bytes(json.dumps({"version": ver, "files": files},
                                ensure_ascii=False, sort_keys=True, indent=0).encode("utf-8"))
    total = sum(v["size"] for v in files.values())
    print(f"{MANIFEST_REL} -> {ver}: {len(files)} 个文件, {total / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
