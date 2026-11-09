"""工作台自带的号池进程管理：启动 / 停止 / 重启 / 掉线守护 / 首次初始化。

以前生图号池是另一个 DSH 插件在管，工作台面板只能写「去号池插件里点启动」；
一旦那个插件没装、没启、或守护失效，工作台就只剩一个空壳。现在号池归工作台自己：

* 号池进程由本模块 spawn，后台线程每 15 秒探端口，掉了自动拉起；
* 号池目录、admin-key、用户 Key、账号库全部由本模块按当前 Base URL 定位；
* 号池服务端代码随工作台插件自带（vendor/chatgpt2api/），首次启动释放到本机
  数据目录并用自带 uv 建独立虚拟环境，**不需要另装任何第三方号池插件**；
* 不依赖 DSH：浏览器直接打开 8092 也能用，DSH 插件只负责把页面嵌进侧栏。

安全边界：只管本机号池。第三方接口、以及 38650 写作台号池都不代管，
避免「停止」按钮把别人的号池关掉。
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

import pool as pool_mod

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_PORT = 38651
# 38650 是写作台号池，本工作台只读不碰
FOREIGN_PORTS = {38650}
STOP_FLAG = ".workbench-pool-stop"
STATE_FILE = ".workbench-pool-state.json"
RR_MARK = "# [dsh-patch:rr-persist]"

_LOCK = threading.RLock()   # 可重入：start() 内部会递归调用自身来完成重启
_WATCHDOG_STARTED = False
_LAST = {"action": "", "ok": True, "message": "", "at": 0.0}
# 号池启动成功后通知工作台刷新账号统计（由 app.py 在 lifespan 里挂上）。
# 号池是异步后起的，不主动刷一次，页头会先停在「1 号 / 配额 0」最多一分钟。
ON_STARTED = None


def _notify_started() -> None:
    cb = ON_STARTED
    if not callable(cb):
        return
    try:
        cb()
    except Exception as e:
        log("启动后同步回调失败：" + pool_mod.mask(e)[:100])


# ---------- 基础工具 ----------
def log(msg: str) -> None:
    pool_mod.log("[内置号池] " + str(msg))


def _dsh_home() -> Path:
    return pool_mod._dsh_home()


def vendor_dir() -> Path | None:
    """工作台插件自带的号池运行时快照。

    工作台后端可能在两种位置：工作区里的 image-local/，或插件包里的 server/；
    插件也可能通过 profile/node_modules 的目录联接加载。这里全部覆盖，
    唯独不去找本机任何第三方号池插件的源码 —— 那是「能独立安装」的前提。
    """
    here = Path(__file__).resolve().parent
    candidates = [
        os.environ.get("DSH_IMAGE_POOL_VENDOR"),
        here / "vendor" / "chatgpt2api",
        here.parent / "vendor" / "chatgpt2api",
        here.parent / "dsh-plugin-image-workbench" / "vendor" / "chatgpt2api",
        _dsh_home() / "profiles" / "desktop" / "node_modules"
        / "dsh-plugin-image-workbench" / "vendor" / "chatgpt2api",
    ]
    for c in candidates:
        if c and Path(c, "source", "main.py").is_file():
            return Path(c)
    return None


def _read_state(home: Path) -> dict:
    try:
        d = json.loads((home / STATE_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_state(home: Path, data: dict) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        tmp = home / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, home / STATE_FILE)
    except Exception as e:
        log("状态写入失败：" + pool_mod.mask(e)[:100])


def auto_start(home: Path) -> bool:
    return _read_state(home).get("auto_start") is not False


def set_auto_start(home: Path, enabled: bool) -> dict:
    st = _read_state(home)
    st["auto_start"] = bool(enabled)
    _write_state(home, st)
    return {"ok": True, "auto_start": bool(enabled),
            "message": "已开启掉线自动拉起" if enabled else "已关闭掉线自动拉起"}


def _record(action: str, ok: bool, message: str) -> None:
    _LAST.update({"action": action, "ok": bool(ok), "message": str(message), "at": time.time()})


def view(cfg: dict | None = None) -> dict:
    """从当前接口配置推出：号池端口、数据目录、是否本机、是否归本工作台管。"""
    cfg = cfg or pool_mod.load_config()
    p = pool_mod.provider_cfg(cfg)
    root = str(p["api_root"] or "")
    u = urlparse(root)
    host = (u.hostname or "").lower()
    try:
        port = int(u.port or 0)
    except Exception:
        port = 0
    if not port:
        port = int(os.environ.get("DSH_IMAGE_POOL_PORT") or DEFAULT_PORT)
    local = host in LOCAL_HOSTS
    home = pool_mod.pool_home(cfg)
    # 自定义端口不该复用 38650/38651 的数据目录，否则会串号
    if local and port not in (38650, 38651) and home.name in ("chatgpt2api-native", "chatgpt2api-image"):
        home = _dsh_home() / "chatgpt2api-workbench"
    # 兼容老安装：如果本机已经有一份带账号的号池目录，继续沿用它的「数据」。
    # 这只是不丢用户已有账号，不是运行时依赖 —— 源码与环境一律来自插件 vendor。
    if local and port == 38651 and home.name != "chatgpt2api-image":
        candidate = _dsh_home() / "chatgpt2api-image"
        if (candidate / "admin-key").exists() or (candidate / "source" / "main.py").exists():
            home = candidate
    return {
        "cfg": cfg, "root": root, "host": host, "port": port, "local": local,
        "home": Path(home), "base_url": str(p["base_url"] or ""),
        "manageable": bool(local) and port not in FOREIGN_PORTS,
    }


def ensure_admin_key(home: Path) -> str:
    """号池后端用 CHATGPT2API_AUTH_KEY 校验 /api/accounts。缺了就生成一份。"""
    p = Path(home) / "admin-key"
    try:
        k = p.read_text(encoding="utf-8").strip()
        if k:
            return k
    except Exception:
        pass
    k = "sk-admin-" + secrets.token_urlsafe(32)
    try:
        Path(home).mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(k, encoding="utf-8")
        os.replace(tmp, p)
        log("已生成 admin-key：" + str(p))
        return k
    except Exception as e:
        log("admin-key 写入失败：" + pool_mod.mask(e)[:100])
        return ""


def runtime(home: Path) -> dict:
    """号池运行时：uv + source/main.py + source/.venv。"""
    home = Path(home)
    vend = vendor_dir()
    uv = ""
    for c in (os.environ.get("DSH_IMAGE_POOL_UV"),
              home / "runtime" / "uv.exe", home / "runtime" / "uv",
              (vend / "runtime" / "uv.exe") if vend else "",
              (vend / "runtime" / "uv") if vend else "",
              shutil.which("uv") or ""):
        if c and Path(c).is_file():
            uv = str(c)
            break
    source = home / "source"
    has_main = (source / "main.py").is_file()
    has_venv = (source / ".venv").is_dir()
    ready = bool(uv) and has_main and has_venv
    if not uv:
        reason = "缺 uv 运行时"
    elif not has_main:
        reason = "缺号池源码 main.py"
    elif not has_venv:
        reason = "缺号池虚拟环境 .venv"
    else:
        reason = ""
    return {"uv": uv, "source": str(source), "ready": ready, "reason": reason}


def probe(port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def health(port: int) -> dict:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health?format=json",
                      timeout=httpx.Timeout(2.5), trust_env=False)
        if r.status_code != 200:
            return {"online": False, "message": f"号池返回 HTTP {r.status_code}"}
        a = r.json().get("accounts") or {}
        return {"online": True, "message": "",
                "total": int(a.get("total") or 0), "active": int(a.get("active") or 0),
                "limited": int(a.get("limited") or 0), "quota": int(a.get("total_quota") or 0)}
    except Exception as e:
        return {"online": False, "message": "号池端口连不上：" + pool_mod.mask(e)[:80]}


# ---------- 出口代理零配置自动探测 ----------
# 常见本机出口代理候选（端口 + 首选协议），按出现频率排序。
_LOCAL_PROXY_SPECS = [
    ("socks5", 1080),   # Shadowsocks / 本机 SOCKS5 最常见
    ("http",   7890),   # Clash HTTP
    ("http",   7891),   # Clash HTTP 备选
    ("http",   8118),   # privoxy
    ("socks5", 1087),   # 部分 SS 客户端
    ("http",   1087),
]


_COMMON_PROXY_PORTS = {1080, 1087, 7890, 7891, 7897, 8118, 8080, 8888,
                       10808, 10809, 20171, 33210}


def _env_egress_proxy(relaxed: bool = False) -> str:
    """用户环境显式设了出口代理就直接用，优先级最高。

    宿主和各类工具链会往环境里塞临时回环代理（随机高端口），那玩意只能通
    它自己的链路，拿去跑号池只会全盘超时。所以回环地址只认常见代理端口。
    """
    for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
              "HTTP_PROXY", "http_proxy"):
        v = (os.environ.get(k) or "").strip()
        if not v:
            continue
        if not relaxed:
            host, port = proxy_host_port(v)
            if host in ("127.0.0.1", "localhost", "::1") and port not in _COMMON_PROXY_PORTS:
                continue
        return v
    return ""


def _local_proxy_candidates() -> list[str]:
    """本机代理端口在监听的，生成候选 URL。

    同一个端口两种协议都试一遍：1080 既可能是 SOCKS5（Shadowsocks），
    也可能是 HTTP（部分客户端），只按首选协议生成会漏掉一半用户。
    """
    out: list[str] = []
    seen: set[int] = set()
    for scheme, port in _LOCAL_PROXY_SPECS:
        if port in seen or not probe(port, 0.4):
            continue
        seen.add(port)
        out.append(f"{scheme}://127.0.0.1:{port}")
        out.append(f"{'http' if scheme == 'socks5' else 'socks5'}://127.0.0.1:{port}")
    return out


def _pool_proxy_get(home: Path, admin: str, port: int):
    """读号池当前 proxy_url；读取失败返回 None（不确定状态：不探测也不清除）。"""
    try:
        with httpx.Client(timeout=httpx.Timeout(12), trust_env=False) as c:
            r = c.get(f"http://127.0.0.1:{port}/api/proxy/runtime",
                      headers={"Authorization": "Bearer " + admin})
        if r.status_code == 200:
            rt = (r.json() or {}).get("runtime") or {}
            return str(rt.get("proxy_url") or "")
    except Exception:
        pass
    return None


def _pool_proxy_set(home: Path, admin: str, port: int, url: str) -> bool:
    body = {"enabled": True, "egress_mode": "single_proxy",
            "proxy_url": url, "resource_proxy_url": url}
    try:
        with httpx.Client(timeout=httpx.Timeout(20), trust_env=False) as c:
            r = c.post(f"http://127.0.0.1:{port}/api/proxy/runtime",
                       json=body, headers={"Authorization": "Bearer " + admin})
        return r.status_code in (200, 201, 204)
    except Exception:
        return False


def _pool_proxy_test(home: Path, admin: str, port: int) -> bool:
    try:
        with httpx.Client(timeout=httpx.Timeout(30), trust_env=False) as c:
            r = c.post(f"http://127.0.0.1:{port}/api/proxy/test",
                       json={"url": ""}, headers={"Authorization": "Bearer " + admin})
        if r.status_code != 200:
            return False
        res = (r.json() or {}).get("result") or {}
        return bool(res.get("ok"))
    except Exception:
        return False


_EGRESS_CACHE: dict = {"at": 0.0, "data": {}}
_EGRESS_LOCK = threading.Lock()


def proxy_host_port(url: str) -> tuple[str, int]:
    """解析代理地址；端口解析不出来返回 0。"""
    try:
        u = urlparse(url if "://" in str(url) else "http://" + str(url))
        host = (u.hostname or "").lower()
        return host, int(u.port or (443 if u.scheme == "https" else 80))
    except Exception:
        return "", 0


def proxy_alive(url: str) -> bool:
    """这个代理在本机还活着吗。生图没有出口代理就出不了图，所以要能判断。

    只对回环地址做端口探测：远程代理本机判断不了，一律当作可用，不擅自替换。
    """
    host, port = proxy_host_port(url)
    if not host:
        return False
    if host not in ("127.0.0.1", "localhost", "::1"):
        return True
    return bool(port) and probe(port, 0.35)


def detect_egress(cache_seconds: float = 20.0) -> dict:
    """探测当前设备实际可用的出口代理，只读，不改号池配置。

    面板里那一栏永远显示本机真实情况，而不是写死某个地址：换一台电脑、
    换了代理端口，显示会跟着变。顺序：环境变量优先，其次扫本机常见端口。
    """
    now = time.time()
    with _EGRESS_LOCK:
        cached = _EGRESS_CACHE["data"]
        if cached and now - float(_EGRESS_CACHE["at"]) < cache_seconds:
            return dict(cached)
    envp = _env_egress_proxy()
    env_any = _env_egress_proxy(relaxed=True)
    ports: list[dict] = []
    seen: set[int] = set()
    for scheme, port in _LOCAL_PROXY_SPECS:
        if port in seen or not probe(port, 0.3):
            continue
        seen.add(port)
        alt = "http" if scheme == "socks5" else "socks5"
        ports.append({"port": port, "scheme": scheme,
                      "url": f"{scheme}://127.0.0.1:{port}",
                      "alt": f"{alt}://127.0.0.1:{port}"})
    url = envp or (ports[0]["url"] if ports else "")
    data = {"ok": True, "url": url,
            "source": "env" if envp else ("port" if ports else "none"),
            "env": envp,
            "env_ignored": env_any if env_any and env_any != envp else "",
            "ports": ports, "checked_at": int(now)}
    with _EGRESS_LOCK:
        _EGRESS_CACHE["at"] = now
        _EGRESS_CACHE["data"] = data
    return dict(data)


def auto_detect_proxy(home: Path, port: int, recheck: bool = True) -> dict:
    """号池上线后校准一次出口代理，命中即持久化，否则保持直连。

    recheck=True 时，如果已保存的地址在本机已经连不上（换了电脑、换了代理
    端口、客户端关了），会重新探测并覆盖；本机还在监听的原值一律保留，
    不会覆盖用户手填的地址。
    """
    home = Path(home)
    admin = ensure_admin_key(home)
    if not admin:
        return {"ok": False, "reason": "no_admin"}
    cur = _pool_proxy_get(home, admin, port)
    if cur is None:
        return {"ok": True, "skipped": True, "reason": "read_failed"}
    if cur:
        if not recheck or proxy_alive(cur):
            return {"ok": True, "skipped": True, "reason": "already_set", "proxy_url": cur}
        log("已保存的出口代理 " + cur + " 在本机已连不上，重新探测")
    cands: list[str] = []
    envp = _env_egress_proxy()
    if envp:
        cands.append(envp)
    cands += _local_proxy_candidates()
    if not cands:
        log("出口代理自动探测：无候选（无本地代理端口、无 env 代理），保持直连")
        return {"ok": True, "skipped": True, "reason": "no_candidate"}
    for url in cands:
        try:
            if not _pool_proxy_set(home, admin, port, url):
                continue
            time.sleep(0.3)
            if _pool_proxy_test(home, admin, port):
                st = _read_state(home)
                st["proxy_auto_url"] = url
                _write_state(home, st)
                log("出口代理自动探测命中并持久化：" + url)
                return {"ok": True, "proxy_url": url, "auto": True}
        except Exception as e:
            log("出口代理候选 " + url + " 验证失败：" + pool_mod.mask(e)[:120])
    # 候选都不通：确保回到直连，不残留错误代理
    try:
        _pool_proxy_set(home, admin, port, "")
    except Exception:
        pass
    log("出口代理自动探测：候选均不通，保持直连")
    return {"ok": True, "proxy_url": "", "auto": False}


def status(cfg: dict | None = None) -> dict:
    """面板与 DSH 外壳共用的状态快照（不写文件、不启动进程）。"""
    v = view(cfg)
    home = v["home"]
    rt = runtime(home)
    admin = ""
    if v["manageable"]:
        admin = ensure_admin_key(home)
    running = probe(v["port"])
    h = health(v["port"]) if running else {"online": False, "message": "号池端口未监听"}
    stopped = (home / STOP_FLAG).exists()
    msg = h.get("message") or ""
    if v["manageable"] and not running:
        msg = ("已手动停止，点「启动号池」或打开自动拉起" if stopped
               else ("号池没在跑，点「启动号池」由本工作台拉起"
                     if rt["ready"] else
                     "号池没在跑，首次启动会从插件自带运行时初始化并装依赖（约 1–3 分钟）"))
    if not v["local"]:
        msg = "当前接口不是本机号池，工作台不代管第三方接口"
    elif not v["manageable"]:
        msg = "38650 是写作台号池，本工作台只读，不代管它的进程"
    vend = vendor_dir()
    return {
        "local": v["local"], "manageable": v["manageable"],
        "port": v["port"], "home": str(home), "base_url": v["base_url"],
        "vendor_bundled": bool(vend), "vendor_path": str(vend) if vend else "",
        "running": running, "online": bool(h.get("online")),
        "total": int(h.get("total") or 0), "active": int(h.get("active") or 0),
        "limited": int(h.get("limited") or 0), "quota": int(h.get("quota") or 0),
        "runtime_ready": rt["ready"], "runtime_reason": rt["reason"], "runtime": rt,
        "admin_key_ready": bool(admin), "auto_start": auto_start(home),
        "stopped_by_user": stopped,
        "log": str(home / "pool-service.log"),
        "last": dict(_LAST),
        "message": msg,
    }


# ---------- 首次初始化 ----------
def _rr_patch(source: Path) -> str:
    """号池 round-robin 游标持久化补丁（幂等），移植自旧号池插件。"""
    f = source / "services" / "account_service.py"
    if not f.is_file():
        return "号池源码缺 account_service.py，跳过轮转补丁"
    try:
        src = f.read_text(encoding="utf-8")
    except Exception:
        return "轮转补丁读取失败，已跳过"
    if RR_MARK in src:
        return ""
    pairs = [
        ("        self._index = 0\n        self._accounts = self._load_accounts()",
         "        self._index = 0\n"
         f"        {RR_MARK} 图片/文本各一个游标，持久化到 data/.rr_index.json\n"
         '        _rr_state = self._load_rr_state()\n'
         '        self._image_index = int(_rr_state.get("image") or 0)\n'
         '        self._text_index = int(_rr_state.get("text") or 0)\n'
         "        self._accounts = self._load_accounts()"),
        ("    def _get_cumulative_file(self) -> Path:",
         f"    {RR_MARK} begin 轮转游标读写\n"
         "    def _get_rr_state_file(self) -> Path:\n"
         "        from services.config import DATA_DIR\n"
         '        return DATA_DIR / ".rr_index.json"\n\n'
         "    def _load_rr_state(self) -> dict:\n"
         "        try:\n"
         "            f = self._get_rr_state_file()\n"
         "            if f.exists():\n"
         '                data = json.loads(f.read_text(encoding="utf-8") or "{}")\n'
         "                if isinstance(data, dict):\n"
         '                    return {"image": int(data.get("image") or 0),\n'
         '                            "text": int(data.get("text") or 0)}\n'
         "        except Exception:\n"
         "            pass\n"
         '        return {"image": 0, "text": 0}\n\n'
         "    def _save_rr_state(self) -> None:\n"
         "        try:\n"
         "            f = self._get_rr_state_file()\n"
         "            f.parent.mkdir(parents=True, exist_ok=True)\n"
         '            tmp = f.with_suffix(".json.tmp")\n'
         '            tmp.write_text(json.dumps({"image": int(getattr(self, "_image_index", 0)),\n'
         '                                    "text": int(getattr(self, "_text_index", 0))}),\n'
         '                         encoding="utf-8")\n'
         "            tmp.replace(f)\n"
         "        except Exception:\n"
         "            pass\n"
         f"    {RR_MARK} end\n\n"
         "    def _get_cumulative_file(self) -> Path:"),
        ("                if tokens:\n"
         "                    access_token = tokens[self._index % len(tokens)]\n"
         "                    self._index += 1",
         "                if tokens:\n"
         "                    access_token = tokens[self._image_index % len(tokens)]\n"
         "                    self._image_index += 1\n"
         "                    self._save_rr_state()"),
        ("            access_token = candidates[self._index % len(candidates)]\n"
         "            self._index += 1",
         "            access_token = candidates[self._text_index % len(candidates)]\n"
         "            self._text_index += 1\n"
         "            self._save_rr_state()"),
    ]
    out = src
    for old, new in pairs:
        if out.count(old) != 1:
            return "轮转补丁锚点不匹配（号池源码版本可能变了），已跳过，不影响出图"
        out = out.replace(old, new)
    try:
        bak = f.with_name(f.name + ".bak_rr_" + time.strftime("%Y%m%d"))
        if not bak.exists():
            shutil.copy2(f, bak)
        f.write_text(out, encoding="utf-8")
    except Exception as e:
        return "轮转补丁写入失败：" + pool_mod.mask(e)[:80]
    return ""


PROXY_MARK = "# workbench:upstream-proxy"

_PROXY_PATCHES = (
    (
        "services/openai_backend_api.py",
        '            impersonate=self.fp["impersonate"],\n'
        '            verify=True,\n'
        '        ))',
        '            impersonate=self.fp["impersonate"],\n'
        '            verify=True,\n'
        '            upstream=True,  ' + PROXY_MARK + '\n'
        '        ))',
    ),
    (
        "services/account_service.py",
        'build_session_kwargs(account=account, impersonate="chrome110", verify=True))',
        'build_session_kwargs(account=account, impersonate="chrome110", verify=True, '
        'upstream=True))  ' + PROXY_MARK,
    ),
    (
        "services/oauth_login_service.py",
        'build_session_kwargs(impersonate="chrome", verify=False)',
        'build_session_kwargs(impersonate="chrome", verify=False, upstream=True)  ' + PROXY_MARK,
    ),
)


def _proxy_patch(source: Path) -> str:
    """让界面里配置的出口代理在真实上游请求中生效（幂等，可反复执行）。

    上游 proxy_service.get_profile() 只有在 upstream=True 时才启用
    proxy_runtime 的 single_proxy；而访问 ChatGPT 的调用点全都没传这个参数，
    于是「接口与号池 → 号池出口代理」填好地址、点测试也通过，真实请求却
    一律直连。受限网络下就是超时，额度永远刷不回来，账号被显示成「已用完」。

    只补三处真正访问上游的调用：
      openai_backend_api   拉额度与出图的核心客户端
      account_service      refresh_token 换 access_token
      oauth_login_service  登录码换 token
    内容审核、CPA、用户图片下载走的是第三方地址，保持原样，
    避免把本来能直连的请求也塞进出网代理。

    补丁写回前先做语法校验，校验不过就放弃该文件，不影响号池启动。
    返回空串表示无需改动或未应用；返回 applied:N 表示本次真的改了 N 个文件。
    """
    applied, skipped = [], []
    for rel, old, new in _PROXY_PATCHES:
        f = source / rel
        if not f.is_file():
            skipped.append(rel + " 缺文件")
            continue
        try:
            text = f.read_bytes().decode("utf-8")
        except Exception:
            skipped.append(rel + " 读取失败")
            continue
        # 上游文件混用 LF 与 CRLF，跨行锚点必须按归一化后的文本匹配，
        # 写回时再还原原有行尾，避免整个文件被改写。
        crlf = "\r\n" in text
        norm = text.replace("\r\n", "\n")
        if PROXY_MARK in norm:
            continue
        if norm.count(old) != 1:
            skipped.append(rel + " 锚点不匹配")
            continue
        patched = norm.replace(old, new)
        try:
            compile(patched, str(f), "exec")
        except SyntaxError:
            skipped.append(rel + " 补丁后语法异常")
            continue
        out = patched.replace("\n", "\r\n") if crlf else patched
        try:
            # 留一份原件，需要回滚时直接覆盖回去即可
            bak = f.with_name(f.name + ".bak_proxy")
            if not bak.exists():
                shutil.copy2(f, bak)
            f.write_bytes(out.encode("utf-8"))
            applied.append(rel)
        except Exception:
            skipped.append(rel + " 写入失败")
    if not applied and not skipped:
        return ""
    if applied:
        log("出口代理补丁已应用 %d 处：%s" % (len(applied), "、".join(applied)))
    if skipped:
        log("出口代理补丁跳过 %d 处：%s" % (len(skipped), "、".join(skipped)))
    return ("applied:%d" % len(applied)) if applied else ("skipped:%d" % len(skipped))


def _venv_python(src: Path) -> str:
    for p in (src / ".venv" / "Scripts" / "python.exe",
              src / ".venv" / "bin" / "python"):
        if p.is_file():
            return str(p)
    return ""


def _find_python313() -> str:
    """找一台机器上现成的 Python 3.13+；找不到就交给 uv 自己下载。"""
    cands = [sys.executable, shutil.which("python") or ""]
    wb = Path.home() / ".workbuddy" / "binaries" / "python" / "versions"
    if wb.is_dir():
        cands += [str(p / ("python.exe" if os.name == "nt" else "python"))
                  for p in sorted(wb.iterdir(), reverse=True)]
    for c in cands:
        if not c or not Path(c).is_file():
            continue
        try:
            r = subprocess.run([c, "-c", "import sys;print(sys.version_info[0],sys.version_info[1])"],
                               capture_output=True, text=True, timeout=6,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            a = (r.stdout or "").split()
            if len(a) == 2 and (int(a[0]), int(a[1])) >= (3, 13):
                return c
        except Exception:
            continue
    return ""


def _venv_ready(src: Path) -> bool:
    """虚拟环境是否真的能跑号池。

    只看 python.exe 存在不够：上一轮装依赖失败时目录已经建好、包却没装上，
    之后每次启动都是 "No module named uvicorn"，而代码以为环境已就绪。
    """
    py = _venv_python(src)
    if not py:
        return False
    try:
        r = subprocess.run([py, "-c", "import fastapi, uvicorn"], cwd=str(src),
                           capture_output=True, text=True, timeout=90,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def _index_candidates(proxy: str) -> list[tuple[str, str, bool]]:
    """依赖源候选：(标签, 索引地址, 是否走代理)。

    镜像站必须直连。经代理访问清华镜像会返回 403，uv 只会说
    「fastapi 不在 registry 里」，报错完全指不到真正原因。
    """
    out: list[tuple[str, str, bool]] = []
    custom = (os.environ.get("DSH_IMAGE_PYPI_INDEX") or "").strip()
    if custom:
        out.append(("自定义源", custom, False))
    out.append(("清华镜像", "https://pypi.tuna.tsinghua.edu.cn/simple", False))
    out.append(("阿里云镜像", "https://mirrors.aliyun.com/pypi/simple", False))
    if proxy:
        out.append(("官方 PyPI", "https://pypi.org/simple", True))
    out.append(("官方 PyPI", "https://pypi.org/simple", False))
    return out


def _ensure_venv(home: Path, src: Path, uv: str) -> dict:
    """用插件自带的 uv 按 lock 建独立虚拟环境，全程不需要第三方插件。"""
    if _venv_ready(src):
        return {"ok": True, "message": ""}
    if not uv:
        return {"ok": False, "message": "号池运行时缺 uv，且系统 PATH 里也找不到 uv。"
                                       "请重新完整安装工作台插件（vendor/chatgpt2api/runtime/）。"}
    base = os.environ.copy()
    base["UV_CACHE_DIR"] = str(home / "uv-cache")
    base["UV_PYTHON_DOWNLOADS"] = "automatic"   # 全新机器允许 uv 自己取 Python 3.13
    base.setdefault("UV_HTTP_TIMEOUT", "120")
    # 允许用户指定 Python 运行时的下载镜像（UV_PYTHON_INSTALL_MIRROR）。
    # 国内直连 GitHub Releases 经常下不动 3.13，这里跟 host.js 用同一个开关。
    mirror = (os.environ.get("DSH_IMAGE_PYTHON_MIRROR") or "").strip()
    if mirror:
        base["UV_PYTHON_INSTALL_MIRROR"] = mirror
    # 系统未设出口代理时，探测本机常见代理（Shadowsocks/Clash/privoxy 等）并注入 uv，
    # 让 uv 能经代理下载 Python 3.13 运行时，覆盖「开了代理但没设环境变量」的典型场景。
    proxy = _env_egress_proxy()
    if not proxy:
        for cand in _local_proxy_candidates():
            proxy = cand
            break

    cmd = [uv, "sync", "--frozen", "--no-dev"]
    py = _find_python313()
    if py:
        cmd += ["--python", py]
    log("开始建号池虚拟环境：" + " ".join(cmd[1:]))
    proxy_keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                  "ALL_PROXY", "all_proxy")
    no_proxy = ("pypi.tuna.tsinghua.edu.cn,mirrors.aliyun.com,pypi.org,"
                "files.pythonhosted.org,127.0.0.1,localhost")
    last = ""
    for label, index, via_proxy in _index_candidates(proxy):
        env = dict(base)
        env["UV_DEFAULT_INDEX_URL"] = index
        if via_proxy and proxy:
            env["HTTPS_PROXY"] = proxy
            env["HTTP_PROXY"] = proxy
        else:
            for k in proxy_keys:
                env.pop(k, None)
        env["NO_PROXY"] = env["no_proxy"] = no_proxy
        log("号池依赖源：%s（%s）" % (label, ("经代理 " + proxy) if (via_proxy and proxy) else "直连"))
        try:
            r = subprocess.run(cmd, cwd=str(src), env=env, capture_output=True,
                               text=True, timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            last = pool_mod.mask(e)[:160]
            log("号池依赖安装异常：" + last)
            continue
        if _venv_ready(src):
            log("号池虚拟环境已就绪：" + str(_venv_python(src)) + "（" + label + "）")
            return {"ok": True, "message": ""}
        last = (((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-400:]
                or "装完仍缺 fastapi/uvicorn")
        log("  " + label + " 失败：" + last.replace("\n", " ")[:280])
    return {"ok": False,
            "message": "号池虚拟环境没建起来（依赖源被拒或网络不通）。"
                       "日志见 pool-service.log。原始输出：" + last}


def bootstrap(home: Path) -> dict:
    """从插件自带 vendor 释放一份独立号池：代码自带、环境自建、账号库不共享。"""
    home = Path(home)
    src = home / "source"
    vend = vendor_dir()
    if not vend:
        return {"ok": False,
                "message": "工作台插件自带的号池运行时缺失（vendor/chatgpt2api）。"
                           "安装包不完整，请重新完整安装本工作台插件；"
                           "不需要也不应该另装任何第三方号池插件。"}
    template = vend / "source"
    if template.resolve() == src.resolve():
        return {"ok": False, "message": "号池源码目录异常，请检查号池数据目录"}
    log("开始初始化独立号池，代码来自插件自带 " + str(template))
    try:
        home.mkdir(parents=True, exist_ok=True)

        def _ignore(directory, names):
            skip = {"data", "logs", "__pycache__", ".git", "tmp", "temp", ".venv"}
            return [n for n in names
                    if n in skip or n.endswith(".log") or n.endswith(".pyc")]

        shutil.copytree(template, src, dirs_exist_ok=True, ignore=_ignore)
        # 出厂默认配置：账号、密钥、代理一律不带
        cfg = src / "config.json"
        if not cfg.is_file() and (template / "config.default.json").is_file():
            shutil.copy2(template / "config.default.json", cfg)
        (src / "data").mkdir(parents=True, exist_ok=True)
        (home / "uv-cache").mkdir(parents=True, exist_ok=True)
        # uv 可执行文件随插件分发，复制到数据目录后与源码同生命周期
        uv_src = ""
        for c in (vend / "runtime" / "uv.exe", vend / "runtime" / "uv"):
            if c.is_file():
                uv_src = str(c)
                break
        if uv_src:
            dst_dir = home / "runtime"
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / Path(uv_src).name
            if not dst.is_file():
                shutil.copy2(uv_src, dst)
        keys = home / "user-api-keys.json"
        if not keys.exists():
            keys.write_text("{}\n", encoding="utf-8")
        note = _rr_patch(src)
        if note:
            log(note)
        proxy_note = _proxy_patch(src)
        if proxy_note:
            log(proxy_note)
    except Exception as e:
        return {"ok": False, "message": "初始化号池失败：" + pool_mod.mask(e)[:140]}

    rt = runtime(home)
    v = _ensure_venv(home, src, rt["uv"])
    if not v["ok"]:
        return {"ok": False, "message": v["message"]}
    rt = runtime(home)
    if not rt["ready"]:
        return {"ok": False, "message": "初始化完成但运行时仍不完整：" + rt["reason"]}
    log("独立号池初始化完成：" + str(src))
    return {"ok": True, "message": "已从插件自带运行时初始化独立号池（无需另装号池插件，账号库不共享）"}


# ---------- 进程控制 ----------
def _port_pids(port: int) -> list[int]:
    """只找「本机这个端口正在 LISTENING」的 PID，避免误杀外部连接。"""
    pids: set[int] = set()
    try:
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", f"netstat -ano | findstr :{port}"],
                               capture_output=True, text=True, timeout=8,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in (r.stdout or "").splitlines():
                if "LISTENING" not in line.upper():
                    continue
                parts = line.split()
                if len(parts) < 5 or not parts[1].endswith(":" + str(port)):
                    continue
                if parts[-1].isdigit():
                    pids.add(int(parts[-1]))
        else:
            r = subprocess.run(["bash", "-lc", f"lsof -nP -iTCP:{port} -sTCP:LISTEN -t"],
                               capture_output=True, text=True, timeout=8)
            for s in (r.stdout or "").split():
                if s.isdigit():
                    pids.add(int(s))
    except Exception as e:
        log("查端口占用失败：" + pool_mod.mask(e)[:90])
    return sorted(pids)


def stop(mark_user_stop: bool = True) -> dict:
    v = view()
    if not v["manageable"]:
        msg = "当前接口不是本工作台管理的号池，未做任何操作"
        _record("stop", False, msg)
        return {"ok": False, "message": msg}
    home = v["home"]
    if mark_user_stop:
        try:
            (home / STOP_FLAG).write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except Exception:
            pass
    pids = _port_pids(v["port"])
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.kill(pid, 9)
        except Exception as e:
            log("停止 PID " + str(pid) + " 失败：" + pool_mod.mask(e)[:80])
    for _ in range(20):
        if not probe(v["port"], 0.3):
            break
        time.sleep(0.3)
    ok = not probe(v["port"], 0.3)
    msg = ("号池已停止" if ok else "发送了停止指令，但端口仍被占用（可能是别的进程），请看日志")
    _record("stop", ok, msg)
    log(msg)
    return {"ok": ok, "message": msg, "pids": pids}


def start(force: bool = False) -> dict:
    with _LOCK:
        v = view()
        if not v["local"]:
            msg = "当前接口不是本机号池，工作台不代管第三方接口"
            _record("start", False, msg)
            return {"ok": False, "message": msg}
        if not v["manageable"]:
            msg = "38650 是写作台号池，本工作台只读，不代管它的进程"
            _record("start", False, msg)
            return {"ok": False, "message": msg}
        home, port = v["home"], v["port"]
        if probe(port) and not force:
            msg = "号池已在运行，无需重复启动"
            _record("start", True, msg)
            # 号池已在跑（如后端重启但号池没掉）：补一次零配置代理探测
            try:
                auto_detect_proxy(home, port)
            except Exception as e:
                log("出口代理自动探测异常：" + pool_mod.mask(e)[:120])
            # 运行中的进程加载的是打补丁之前的代码。刚打完补丁就重启一次，
            # 否则界面里配好的出口代理要等用户手动重启才真正生效。
            if _proxy_patch(home / "source").startswith("applied:"):
                log("出口代理补丁首次应用，重启号池使其生效")
                stop(mark_user_stop=False)
                time.sleep(0.6)
                return start()
            return {"ok": True, "message": msg, "already": True}
        if force and probe(port):
            stop(mark_user_stop=False)

        rt = runtime(home)
        # 目录存在不等于依赖齐了：半装的 venv 会让号池一起来就报 No module named uvicorn
        if rt["ready"] and not _venv_ready(Path(rt["source"])):
            rt = dict(rt, ready=False, reason="号池虚拟环境缺依赖，正在补齐")
        if not rt["ready"]:
            b = bootstrap(home)
            if not b["ok"]:
                _record("start", False, b["message"])
                return {"ok": False, "message": b["message"]}
            rt = runtime(home)
        # 已装好的号池不会重跑 bootstrap，这里再补一次幂等补丁
        _proxy_patch(Path(rt["source"]))
        admin = ensure_admin_key(home)
        if not admin:
            msg = "admin-key 读不到也写不进，检查号池目录权限：" + str(home)
            _record("start", False, msg)
            return {"ok": False, "message": msg}

        env = os.environ.copy()
        env.update({
            "CHATGPT2API_AUTH_KEY": admin,
            "STORAGE_BACKEND": "sqlite",
            "UV_CACHE_DIR": str(home / "uv-cache"),
            "UV_PYTHON_DOWNLOADS": "never",
        })
        src = Path(rt["source"])
        bindir = src / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        env["VIRTUAL_ENV"] = str(src / ".venv")
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        # 直接执行号池 venv 里的 uvicorn，不再经 uv.exe 中转。
        # 旧写法 `uv run uvicorn ...` 会形成「uv.exe -> uvicorn.exe -> python.exe」三层，
        # 而 DETACHED_PROCESS 只让直接子进程 uv.exe 脱离控制台，Windows 会给孙进程
        # uvicorn.exe 新建一个可见控制台，屏幕上就一直挂着一个黑框，关掉它还会杀掉号池。
        launcher = ""
        for name in ("uvicorn.exe", "uvicorn"):
            f = bindir / name
            if f.is_file():
                launcher = str(f)
                break
        args = ["main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
        if launcher:
            cmd = [launcher] + args
        elif _venv_python(src):
            cmd = [_venv_python(src), "-m", "uvicorn"] + args
        else:
            cmd = [rt["uv"], "run", "--no-sync", "uvicorn"] + args
        log_file = home / "pool-service.log"
        flags = 0
        startupinfo = None
        if os.name == "nt":
            # CREATE_NO_WINDOW 明确禁止为控制台程序创建窗口；STARTF_USESHOWWINDOW
            # 是第二重保险。注意 DETACHED_PROCESS 与 CREATE_NO_WINDOW 不能混用。
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
        try:
            home.mkdir(parents=True, exist_ok=True)
            fd = open(log_file, "a", encoding="utf-8", errors="replace")
            p = subprocess.Popen(cmd, cwd=str(src), env=env,
                                 stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                                 creationflags=flags, startupinfo=startupinfo,
                                 close_fds=True)
            fd.close()
        except Exception as e:
            msg = "号池启动失败：" + pool_mod.mask(e)[:140]
            _record("start", False, msg)
            return {"ok": False, "message": msg}

        for _ in range(80):           # 首次启动要装依赖，最多等 40 秒
            time.sleep(0.5)
            if probe(port, 0.4):
                try:
                    (home / STOP_FLAG).unlink(missing_ok=True)
                except Exception:
                    pass
                msg = f"号池已启动：127.0.0.1:{port}（PID {p.pid}）"
                _record("start", True, msg)
                log(msg)
                # 号池上线后零配置探测出口代理（仅当前为空时），不阻塞前台返回
                try:
                    auto_detect_proxy(home, port)
                except Exception as e:
                    log("出口代理自动探测异常（不影响号池运行）：" + pool_mod.mask(e)[:120])
                _notify_started()
                return {"ok": True, "message": msg, "pid": p.pid}
            if p.poll() is not None:
                break
        msg = "号池启动超时，请看日志：" + str(log_file)
        _record("start", False, msg)
        return {"ok": False, "message": msg}


def restart() -> dict:
    # 整个停-等-启放在同一把锁里，避免守护线程在这 0.6 秒的窗口里插一脚
    with _LOCK:
        v = view()
        if not v["manageable"]:
            msg = "当前接口不是本工作台管理的号池，未做任何操作"
            _record("restart", False, msg)
            return {"ok": False, "message": msg}
        stop(mark_user_stop=False)
        time.sleep(0.6)
        return start()


# ---------- 掉线守护 ----------
def _watchdog() -> None:
    # 启动后台先立刻补一次，之后每 15 秒探一次
    first = True
    while True:
        if not first:
            time.sleep(15)
        first = False
        try:
            v = view()
            if not v["manageable"] or not auto_start(v["home"]):
                continue
            if (v["home"] / STOP_FLAG).exists():
                continue
            if probe(v["port"], 0.4):
                continue
            log("守护发现号池掉线，自动拉起")
            r = start()
            if not r.get("ok"):
                log("守护拉起失败：" + str(r.get("message") or "")[:120])
        except Exception as e:
            log("守护异常：" + pool_mod.mask(e)[:100])


def start_watchdog() -> None:
    global _WATCHDOG_STARTED
    if _WATCHDOG_STARTED:
        return
    _WATCHDOG_STARTED = True
    t = threading.Thread(target=_watchdog, name="builtin-pool-watchdog", daemon=True)
    t.start()
    log("内置号池守护已启动（15 秒探一次）")


# 命令行手动用：python pool_service.py status|start|stop|restart
if __name__ == "__main__":
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if action == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif action == "start":
        print(json.dumps(start(), ensure_ascii=False, indent=2))
    elif action == "stop":
        print(json.dumps(stop(), ensure_ascii=False, indent=2))
    elif action == "restart":
        print(json.dumps(restart(), ensure_ascii=False, indent=2))
    else:
        print("用法：python pool_service.py status|start|stop|restart")
