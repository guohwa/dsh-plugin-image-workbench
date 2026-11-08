"""本地号池管理：账号导入 / 删除、用户 Key 签发、对外连接信息。

号池本身是独立服务（38650 写作 / 38651 生图），以前想往里加号，要么改
accounts.json，要么去号池自带的前端。这个模块把「加号 / 删号 / 停用 / 拿 Key」
做进工作台的「接口与号池」面板，好处是用户不用碰代码、不用开别的窗口。

三条硬约束：

1. 只碰本机号池。Base URL 不是 127.0.0.1 / localhost，或者找不到 admin-key，
   一律拒绝写操作——把第三方接口当成号池去改账号，是真会出事的。
2. 浏览器拿不到 access_token。列表只回 sha1 摘要当 id，删除 / 停用时服务端用
   内存表换回真 token。access_token 等于账号全部权限，不该进前端。
3. 报错说人话。号池回的是英文 JSON，这里翻成「这个 token 校验没过」这类话，
   同时把号池自己返回的 errors 原样带上，方便对账。

号池对账号的约束：access_token 是 JWT（eyJ 开头三段），导入后号池会立刻拿它
去 OpenAI 换一次额度，所以「导入成功」= 号池认了、上游也没拒；上游连不上
（比如本机代理没开）会进 errors，账号仍然留在池子里，等下次刷新再试。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

import pool as pool_mod
import pool_service

# ChatGPT 的 access_token 一定是 JWT：三段 base64url，第一段解出来是 {"alg":...}
TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]+")
MAX_IMPORT = 50          # 一次最多导入 50 个：每加一个都要上游校验，再多就该分批发
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# id -> access_token。账号 id 是 token 的摘要，浏览器只认这个，真 token 留在这
_TOKENS: dict[str, str] = {}
# 复用的连接池（见 _client()）
_CLIENT: httpx.AsyncClient | None = None
_CLIENT_LOOP = None


# ---------- 小工具 ----------
def _id_of(token: str) -> str:
    return hashlib.sha1(token.encode("utf-8")).hexdigest()[:12]


def _remember(tokens) -> None:
    for t in tokens:
        if t:
            _TOKENS[_id_of(t)] = t


def _is_local(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in {"127.0.0.1", "localhost", "::1"}
    except Exception:
        return False


def _fmt_ts(ts) -> str:
    if not ts:
        return ""
    try:
        f = float(ts)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(f))


def mask_key(key: str) -> str:
    key = str(key or "")
    if len(key) <= 12:
        return "***" if key else ""
    return key[:6] + "***" + key[-4:]


def _err_text(r: httpx.Response) -> str:
    try:
        j = r.json()
    except Exception:
        return (r.text or "")[:200] or f"HTTP {r.status_code}"
    if isinstance(j, dict):
        e = j.get("detail") or j.get("error") or j
        if isinstance(e, dict):
            return str(e.get("error") or e.get("message") or e)[:240]
        return str(e)[:240]
    return str(j)[:240]


def target(cfg: dict | None = None) -> dict:
    """当前接口对应的号池：地址、数据目录、admin-key。

    local=False 表示「当前接口不是本机号池」，面板据此把写操作全部关掉。
    """
    cfg = cfg or pool_mod.load_config()
    p = pool_mod.provider_cfg(cfg)
    home = pool_mod.pool_home(cfg)
    admin = pool_mod.pool_admin_key(cfg)
    # 全新安装时号池目录里还没有 admin-key：工作台自己签一份，别让面板
    # 误判成「不是本机号池」而把导入 / 建 Key 全禁掉。
    if not admin and _is_local(p["api_root"]) and pool_service.view(cfg)["manageable"]:
        admin = pool_service.ensure_admin_key(pool_service.view(cfg)["home"])
    local = bool(p["api_root"]) and _is_local(p["api_root"]) and bool(admin)
    return {
        "local": local,
        "root": p["api_root"],
        "base_url": p["base_url"],
        "home": str(home),
        "admin_key": admin,
        "name": p["name"],
    }


async def _client() -> httpx.AsyncClient:
    """复用同一个 httpx 连接池。

    Windows 上新建一个 AsyncClient 要 0.7~0.8 秒（SSL/线程池初始化），原来每个请求
    都新建一个，面板串起来要 2 秒多才出数。池子绑在事件循环上，换循环就重建。
    """
    global _CLIENT, _CLIENT_LOOP
    loop = asyncio.get_running_loop()
    if _CLIENT is None or _CLIENT_LOOP is not loop or _CLIENT.is_closed:
        _CLIENT = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(60.0, connect=8.0))
        _CLIENT_LOOP = loop
    return _CLIENT


async def _req(method: str, tgt: dict, path: str, body=None, timeout: float = 30.0,
               client: httpx.AsyncClient | None = None):
    url = str(tgt["root"]).rstrip("/") + "/" + path.lstrip("/")
    hdr = {"Authorization": "Bearer " + tgt["admin_key"]}
    c = client or await _client()
    return await c.request(method, url, json=body, headers=hdr, timeout=timeout)


# ---------- 号池出口代理（受限网络必填，默认直连） ----------
async def get_pool_proxy() -> dict:
    """读取本机号池的出口代理配置。admin 接口，浏览器不碰 key。

    号池 agent 需要访问 OpenAI / ChatGPT；不受限网络可留空直连，受限网络
    由用户在这里填自己的出口代理。默认不写死任何地址，公网用户各填各的。
    """
    tgt = target()
    if not tgt["local"]:
        return {"ok": False, "reason": "non_local",
                "msg": "当前接口不是本机号池，无法读取代理配置"}
    try:
        r = await _req("GET", tgt, "api/proxy/runtime", timeout=20)
    except Exception as e:
        return {"ok": False, "reason": "net", "msg": str(e)[:160]}
    if r.status_code != 200:
        return {"ok": False, "reason": "http", "status": r.status_code, "msg": _err_text(r)}
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "reason": "json", "msg": "号池返回非 JSON"}
    return {"ok": True, "runtime": j.get("runtime", {}), "status": j.get("status", {})}


async def set_pool_proxy(payload: dict) -> dict:
    """把用户填的出口代理写进本机号池（默认直连；受限网络填自己的代理）。

    payload: {enabled, egress_mode, proxy_url, resource_proxy_url}
    号池接口要求扁平字段，会自动合并进 proxy_runtime，不会覆盖 clearance 等其它键。
    """
    tgt = target()
    if not tgt["local"]:
        return {"ok": False, "reason": "non_local",
                "msg": "当前接口不是本机号池，无法修改代理配置"}
    body = {
        "enabled": bool(payload.get("enabled", True)),
        "egress_mode": str(payload.get("egress_mode") or "single_proxy").strip().lower() or "single_proxy",
        "proxy_url": str(payload.get("proxy_url") or "").strip(),
        "resource_proxy_url": str(payload.get("resource_proxy_url") or payload.get("proxy_url") or "").strip(),
    }
    try:
        r = await _req("POST", tgt, "api/proxy/runtime", body=body, timeout=20)
    except Exception as e:
        return {"ok": False, "reason": "net", "msg": str(e)[:160]}
    if r.status_code not in (200, 201, 204):
        return {"ok": False, "reason": "http", "status": r.status_code, "msg": _err_text(r)}
    try:
        j = r.json()
    except Exception:
        j = {}
    return {"ok": True, "runtime": (j.get("runtime") if isinstance(j, dict) else None),
            "msg": "代理已保存，号池将使用新出口"}


async def test_pool_proxy(url: str = "") -> dict:
    """用号池自己已配置的代理测连通性（测的是已保存配置，不是临时填的）。

    返回号池 test_proxy 的原始结果：{ok, status, latency_ms, error, ...}
    """
    tgt = target()
    if not tgt["local"]:
        return {"ok": False, "reason": "non_local",
                "msg": "当前接口不是本机号池，无法测试代理"}
    try:
        r = await _req("POST", tgt, "api/proxy/test", body={"url": str(url or "")},
                       timeout=30)
    except Exception as e:
        return {"ok": False, "reason": "net", "msg": str(e)[:160]}
    if r.status_code != 200:
        return {"ok": False, "reason": "http", "status": r.status_code, "msg": _err_text(r)}
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "reason": "json", "msg": "号池返回非 JSON"}
    return {"ok": True, **(j.get("result") if isinstance(j.get("result"), dict) else {})}


# ---------- 用户 Key（对外提供 API 就靠它） ----------
def _key_file(home: str | Path) -> Path:
    return Path(home) / "user-api-keys.json"


def read_plain_keys(home: str | Path) -> dict[str, str]:
    """读号池目录里的「keyId -> sk-明文」表。

    号池后端只存哈希，明文只落在本机这个文件里（其它插件也写它，所以只改自己那条）。
    """
    p = _key_file(home)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(d, dict):
        return {str(k): str(v) for k, v in d.items() if isinstance(v, str) and v.strip()}
    if isinstance(d, list):
        return {str(i): str(v) for i, v in enumerate(d) if isinstance(v, str) and v.strip()}
    return {}


def _save_plain_key(home: str | Path, key_id: str, key: str) -> None:
    p = _key_file(home)
    data = read_plain_keys(home)
    data[str(key_id)] = key
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        pool_mod.log(f"用户 Key 明文写入失败：{pool_mod.mask(e)[:120]}")


async def _list_keys(tgt: dict, client=None) -> list[dict]:
    try:
        r = await _req("GET", tgt, "api/auth/users", timeout=10, client=client)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    try:
        items = r.json().get("items") or []
    except Exception:
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append({
            "id": str(it.get("id") or ""),
            "name": str(it.get("name") or "默认"),
            "enabled": it.get("enabled") is not False,
            "last_used_at": _fmt_ts(pool_mod._parse_dt(it.get("last_used_at"))),
        })
    return out


async def ensure_key(force_new: bool = False, name: str = "生图工作台") -> dict:
    """保证号池里有一把可用的用户 Key，返回明文供面板展示 / 复制。

    force_new=True 时另起一把新的（老 Key 不删，避免正在跑的脚本突然断掉）。
    """
    tgt = target()
    if not tgt["local"]:
        return {"ok": False, "message": "当前接口不是本机号池"}
    keys = await _list_keys(tgt)
    plain = read_plain_keys(tgt["home"])
    if not force_new:
        for k in keys:
            if k["enabled"] and plain.get(k["id"]):
                return {"ok": True, "created": False, "key": plain[k["id"]],
                        "id": k["id"], "home": tgt["home"]}
        # 后端列表里没有、但本地文件有明文：仍然可用，别硬造新 Key
        for kid, val in plain.items():
            if val:
                return {"ok": True, "created": False, "key": val, "id": kid,
                        "home": tgt["home"]}
    try:
        r = await _req("POST", tgt, "api/auth/users", {"name": name}, timeout=20)
    except Exception as e:
        return {"ok": False, "message": f"号池连不上：{pool_mod.mask(e)[:120]}"}
    if r.status_code != 200:
        return {"ok": False, "message": f"号池拒绝建 Key：{_err_text(r)}"}
    j = r.json()
    kid = str((j.get("item") or {}).get("id") or "")
    key = str(j.get("key") or "")
    if not key:
        return {"ok": False, "message": "号池没返回密钥，稍后重试"}
    if kid:
        _save_plain_key(tgt["home"], kid, key)
    return {"ok": True, "created": True, "key": key, "id": kid, "home": tgt["home"]}


# ---------- 面板数据 ----------
_STATUS_CLS = {"正常": "ok", "限流": "warn", "禁用": "off", "异常": "err"}


async def panel(cfg: dict | None = None) -> dict:
    tgt = target(cfg)
    out = {
        "local": tgt["local"],
        "online": False,
        "root": tgt["root"],
        "base_url": tgt["base_url"],
        "home": tgt["home"],
        "message": "",
        "total": 0, "active": 0, "limited": 0, "disabled": 0, "quota": 0,
        "api_key": "", "api_key_masked": "", "api_key_id": "", "keys": [],
        "accounts": [], "accounts_error": "", "blacklist_total": 0,
        # 工作台自带号池：进程是否在线、能否启动 / 停止、运行时是否就绪
        "service": pool_service.status(cfg),
    }
    out["blacklist_total"] = len(load_blacklist(tgt["home"]))
    if not tgt["local"]:
        out["message"] = "当前接口不是本机号池（Base URL 不是 127.0.0.1，或找不到 admin-key）"
        return out

    # 三个请求互不依赖，一起发出去。串行在 Windows 上要 2 秒多，面板会先闪一下「读取中」
    c = await _client()

    async def grab(path, timeout):
        return await _req("GET", tgt, path, timeout=timeout, client=c)

    h_res, a_res, k_res = await asyncio.gather(
        grab("health?format=json", 6), grab("api/accounts", 12),
        _list_keys(tgt, client=c), return_exceptions=True)

    # 1) 号池自报状态：有 /health 才算真在线
    if isinstance(h_res, Exception):
        if isinstance(h_res, (httpx.ConnectError, httpx.ConnectTimeout)):
            out["message"] = out["service"]["message"] or "号池没在跑：端口上连不上"
        else:
            out["message"] = f"号池连不上：{pool_mod.mask(h_res)[:110]}"
        # 号池停着的时候，本地明文 Key 依然在 user-api-keys.json 里。
        # 以前这里直接 return，面板会把当前密钥显示成「还没有用户 Key」，
        # 用户以为密钥丢了又去点「重新生成」，白白多出一把。
        plain = read_plain_keys(tgt["home"])
        if plain:
            kid, val = next(iter(plain.items()))
            out.update({"api_key": val, "api_key_masked": mask_key(val), "api_key_id": kid,
                        "keys": [{"id": kid, "name": "本地保存", "enabled": True,
                                  "last_used_at": "", "has_plain": True, "current": True}]})
        # 刚启动的那一瞬间进程状态可能还没翻过来，回给前端最新的服务快照
        out["service"] = pool_service.status(cfg)
        return out
    if h_res.status_code == 200:
        j = h_res.json()
        a = j.get("accounts") or {}
        out.update({
            "online": True,
            "total": int(a.get("total") or 0),
            "active": int(a.get("active") or 0),
            "limited": int(a.get("limited") or 0),
            "quota": int(a.get("total_quota") or 0),
            "message": f"{int(a.get('total') or 0)} 个账号 · 可用 {int(a.get('active') or 0)}"
                       f" · 配额 {int(a.get('total_quota') or 0)}",
        })
    else:
        out["message"] = f"号池返回 HTTP {h_res.status_code}"
        return out

    # 2) 账号明细：顺手把 id -> token 表填上，后面删号/停用才换得回来
    if isinstance(a_res, Exception):
        out["accounts_error"] = f"账号明细拉取失败：{pool_mod.mask(a_res)[:90]}"
    elif a_res.status_code != 200:
        out["accounts_error"] = f"账号明细拉取失败（HTTP {a_res.status_code}）"
    else:
        try:
            items = a_res.json().get("items") or []
        except Exception:
            items = []
        rows = []
        disabled = 0
        for a in items:
            if not isinstance(a, dict):
                continue
            tok = str(a.get("access_token") or "").strip()
            _remember([tok])
            st = str(a.get("status") or "正常").strip()
            if st == "禁用":
                disabled += 1
            rows.append({
                "id": _id_of(tok) if tok else "",
                "email": pool_mod.mask_email(str(a.get("email") or "")),
                "status": st,
                "cls": _STATUS_CLS.get(st, "warn"),
                "quota": int(a.get("quota") or 0),
                "success": int(a.get("success") or 0),
                "fail": int(a.get("fail") or 0),
                "type": str(a.get("type") or ""),
                "last_used": _fmt_ts(pool_mod._parse_dt(a.get("last_used_at"))),
                "error": str(a.get("last_refresh_error") or "")[:120],
            })
        # quota=0 却挂「正常」的号不算可用：显示与页头统计一起纠正
        normalize_zero_quota(rows, out)
        rows.sort(key=lambda x: (x["cls"] != "ok", -x["quota"], x["email"]))
        out["accounts"] = rows
        out["disabled"] = disabled

    # 3) 用户 Key：面板上要能直接复制出去接别的工具
    #    「当前」优先认接口里正在用的那把，其次才是第一个有明文的启用 Key
    keys = [] if isinstance(k_res, Exception) else k_res
    plain = read_plain_keys(tgt["home"])
    active = str(pool_mod.provider_cfg(cfg)["api_key"] or "")
    cur = active if active else ""
    for k in keys:
        k["has_plain"] = bool(plain.get(k["id"]))
        if active and plain.get(k["id"]) == active:
            out["api_key_id"] = k["id"]
    if cur:
        for kid, val in plain.items():
            if val == cur:
                out["api_key_id"] = kid
                break
    if not cur:
        for k in keys:
            if k["enabled"] and k["has_plain"]:
                cur = plain[k["id"]]
                out["api_key_id"] = k["id"]
                break
    if not cur and plain:
        # 后端列表读不到（老号池没这个接口）就退一步用本地明文
        kid, cur = next(iter(plain.items()))
        out["api_key_id"] = kid
    for k in keys:
        k["current"] = k["id"] == out["api_key_id"]
    out["keys"] = keys
    out["api_key"] = cur
    out["api_key_masked"] = mask_key(cur)
    return out


# ---------- 人工剔除名单 ----------
# 删除 / 停用过的 token 记一份摘要，再导入同一份 txt 时自动跳过。
# 只存 sha256 前 24 位，明文 token 不落盘；文件随号池数据目录走，换池互不干扰。
BLACKLIST_FILE = "import-blacklist.json"


def blacklist_id(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:24]


def _blacklist_path(home: str | Path) -> Path:
    return Path(home) / BLACKLIST_FILE


def load_blacklist(home: str | Path) -> set[str]:
    try:
        d = json.loads(_blacklist_path(home).read_text(encoding="utf-8"))
        ids = d.get("ids") if isinstance(d, dict) else d
        return {str(x) for x in (ids or []) if isinstance(x, str) and x}
    except Exception:
        return set()


def _save_blacklist(home: str | Path, ids: set[str]) -> None:
    p = _blacklist_path(home)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": 1, "ids": sorted(ids)},
                                  ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        pool_mod.log(f"剔除名单写入失败：{pool_mod.mask(e)[:120]}")


def blacklist_add(home, tokens) -> int:
    ids = load_blacklist(home)
    before = len(ids)
    ids.update(blacklist_id(t) for t in tokens if t)
    if len(ids) != before:
        _save_blacklist(home, ids)
    return len(ids) - before


def blacklist_remove(home, tokens) -> int:
    ids = load_blacklist(home)
    before = len(ids)
    ids.difference_update(blacklist_id(t) for t in tokens if t)
    if len(ids) != before:
        _save_blacklist(home, ids)
    return before - len(ids)


def filter_blacklisted(tokens: list[str], items: list[dict], blacklist: set[str]):
    """导入前过滤人工剔除过的 token，返回 (tokens, items, 跳过数)。"""
    if not blacklist:
        return tokens, items, 0
    kept_t = [t for t in tokens if blacklist_id(t) not in blacklist]
    kept_i = [d for d in items
              if blacklist_id(d.get("access_token") or "") not in blacklist]
    return kept_t, kept_i, (len(tokens) - len(kept_t)) + (len(items) - len(kept_i))


def normalize_zero_quota(rows: list[dict], out: dict) -> int:
    """号池把 quota=0 的新号写成「正常」，这里纠正显示与统计，不误导成可用。"""
    n = 0
    for r in rows:
        if r.get("status") == "正常" and int(r.get("quota") or 0) == 0:
            r["status"] = "限流"
            r["cls"] = _STATUS_CLS.get("限流", "warn")
            n += 1
    if n:
        out["active"] = max(0, int(out.get("active") or 0) - n)
        out["limited"] = int(out.get("limited") or 0) + n
        out["message"] = (f'{int(out.get("total") or 0)} 个账号 · 可用 {out["active"]}'
                          f' · 配额 {int(out.get("quota") or 0)}')
    return n


async def _mark_zero_quota(tgt: dict, submitted: list[str], client) -> int:
    """把本次新增里额度为 0 的号从「正常」改成「限流」。

    只在号池明确完成额度校验后调用；quota>0 或已有刷新错误的号一律不动，
    避免把还能用的号锁死。改失败不影响导入结果。
    """
    if not submitted:
        return 0
    try:
        r = await _req("GET", tgt, "api/accounts", timeout=15, client=client)
        if r.status_code != 200:
            return 0
        items = (r.json() or {}).get("items") or []
    except Exception:
        return 0
    want = set(submitted)
    todo = [str(a.get("access_token") or "") for a in items
            if isinstance(a, dict)
            and str(a.get("access_token") or "") in want
            and str(a.get("status") or "正常") == "正常"
            and int(a.get("quota") or 0) == 0
            and not str(a.get("last_refresh_error") or "").strip()]
    if not todo:
        return 0

    async def one(tok: str) -> bool:
        try:
            rr = await _req("POST", tgt, "api/accounts/update",
                            {"access_token": tok, "status": "限流"},
                            timeout=15, client=client)
            return rr.status_code < 400
        except Exception:
            return False

    res = await asyncio.gather(*(one(t) for t in todo[:MAX_IMPORT]))
    return sum(1 for x in res if x)


# ---------- 导入 ----------
# 账号资料要原样带过去。web 类型的号没有 OpenAI refresh_token，
# access_token 过期后靠 password 重新登录；以前只把 token 抠出来提交，
# 号池里就会多一批「密码为空」的号，几天后 token 一过期就全变死号。
#
# 特别小心 mail_refresh_token：市面上「首登料」的发料格式是
#   邮箱----邮箱密码----邮箱Client ID----邮箱Refresh Token----AT
# 第 4 段是微软 MSA 的邮箱刷新令牌（M.C5xx...0.U.MsaArtifacts...），给接码用的，
# 不是 OpenAI 的 OAuth refresh_token。号池的 refresh_token 只会被 POST 到
# auth.openai.com/oauth/token，把邮箱令牌填进去必然刷新失败，还会触发
# 自动密码重登和 keepalive 定时刷新。所以邮箱料一律用 mail_* 独立字段留档，
# 号池会原样保存未知字段（account_service._normalize_account 是 dict(item)），
# 重启 / 导出时资料都还在，同时不会被当成 OpenAI 令牌使用。
IMPORT_FIELDS = (
    "access_token", "accessToken", "email", "password", "totp_secret",
    "source_type", "type", "user_id", "proxy", "default_model_slug",
    "refresh_token", "id_token", "session_token",
    "mail_client_id", "mail_refresh_token",
)
# 微软 MSA 邮箱刷新令牌，形如 M.C535_SN1.0.U.MsaArtifacts.xxxxx
MAIL_TOKEN_RE = re.compile(r"^M\.[A-Za-z0-9_-]+\.[0-9]+\.U\.", re.I)
# 发料文件末尾常带一行中文说明（含「发货格式」），不能算成没认出来的账号
MATERIAL_NOTE_RE = re.compile(r"发货格式|质保|售后|不会用|适用于", re.I)


def _clean_import_item(it: dict) -> dict | None:
    """校验一条账号 JSON，只保留号池重登需要的字段，别把运行态数据也灌进去。"""
    tok = str(it.get("access_token") or it.get("accessToken") or "").strip()
    if not TOKEN_RE.fullmatch(tok):
        return None
    out: dict = {"access_token": tok}
    for k in IMPORT_FIELDS:
        v = it.get(k)
        if v is None or v == "" or k in ("access_token", "accessToken"):
            continue
        # 有人把邮箱 Refresh Token 填到 refresh_token 字段时，改归档别误伤号池
        if k == "refresh_token" and isinstance(v, str) and MAIL_TOKEN_RE.match(v.strip()):
            out.setdefault("mail_refresh_token", v)
            continue
        out[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
    return out


def _parse_dash_row(line: str) -> dict | None:
    """解析发料行：邮箱----密码----[邮箱Client ID----邮箱Refresh Token]----AT。

    先整行锁定 JWT，再切前面的资料：JWT 签名本身可能含连续横线，按
    分隔符直接 split 整行会把一个 token 劈成两半（真实发料里就有一行这样），
    那条账号就会退化成「只有 token、没有邮箱密码」。
    只认第一段是邮箱的行；中间资料按位置留档，段数不明时不硬猜字段语义。
    """
    m = TOKEN_RE.search(line)
    if m is None:
        return None
    head = line[:m.start()].strip()
    parts = [p.strip() for p in re.split(r"-{4,}", head) if p.strip()]
    if len(parts) < 2 or "@" not in parts[0]:
        return None
    out: dict = {"access_token": m.group(0), "email": parts[0],
                 "password": parts[1], "source_type": "web"}
    if len(parts) == 4:
        out["mail_client_id"] = parts[2]
        out["mail_refresh_token"] = parts[3]
    return out


def parse_import(text: str) -> tuple[list[str], list[dict], int]:
    """解析粘贴内容，返回 (裸 token 列表, 完整账号对象, 没认出来的行数)。

    支持四种常见格式（txt / json / csv 上传后走同一条解析）：
    - 每行一个 token（最常见）
    - 号池 / 第三方工具导出的 JSON：数组、{accounts:[...]}、或单个对象
    - 五段式首登料（邮箱----密码----Client ID----邮箱RT----AT）：整条资料一起入池
    - 老式导出行（邮箱,密码,token / 邮箱----密码----token）：按行扫 JWT
    """
    # Windows 记事本存的 UTF-8 txt 常带 BOM，不清掉会把整段 JSON 判成纯文本
    raw = str(text or "").lstrip("\ufeff")
    tokens: list[str] = []
    items: list[dict] = []
    bad = 0
    s = raw.strip()
    data = None
    if s.startswith("[") or s.startswith("{"):
        try:
            data = json.loads(s)
        except Exception:
            data = None
    if data is not None:
        entries = data
        if isinstance(data, dict):
            for key in ("accounts", "items", "tokens"):
                if isinstance(data.get(key), list):
                    entries = data[key]
                    break
            else:
                entries = [data]
        if isinstance(entries, list):
            for it in entries:
                if isinstance(it, dict):
                    one = _clean_import_item(it)
                    if one:
                        items.append(one)
                    else:
                        bad += 1
                elif isinstance(it, str) and TOKEN_RE.fullmatch(it.strip()):
                    tokens.append(it.strip())
                else:
                    bad += 1
            items = list({d["access_token"]: d for d in items}.values())
            have = {d["access_token"] for d in items}
            tokens = [t for t in dict.fromkeys(tokens) if t not in have]
            return tokens, items, bad

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        one = _parse_dash_row(line)
        if one is not None:
            items.append(one)
            continue
        found = TOKEN_RE.findall(line)
        if found:
            tokens.extend(found)
        elif MATERIAL_NOTE_RE.search(line) and not TOKEN_RE.search(line):
            continue      # 发料文件末尾的中文说明 / 格式示例行
        elif re.match(r"^\s*(email|user|account|access_token|token)\b", line, re.I) \
                and re.search(r"[,;\t|]", line):
            continue      # CSV / TSV 的表头行，不算没认出的账号
        else:
            bad += 1
    items = list({d["access_token"]: d for d in items}.values())
    have = {d["access_token"] for d in items}
    tokens = [t for t in dict.fromkeys(tokens) if t not in have]
    if bad and not tokens and not items:
        bad = max(1, bad)
    return tokens, items, bad


def parse_tokens(text: str) -> tuple[list[str], int]:
    """兼容旧调用：只关心 token 列表（面板/冒烟用它做格式校验）。"""
    tokens, items, bad = parse_import(text)
    merged = list(dict.fromkeys([*tokens, *[d["access_token"] for d in items]]))
    return merged, bad


async def import_accounts(text: str) -> dict:
    tgt = target()
    if not tgt["local"]:
        return {"ok": False, "message": "当前接口不是本机号池，无法导入账号"}
    tokens, items, bad = parse_import(text)
    # 人工停用 / 删除过的号不允许靠重导同一份文件复活
    blacklisted = 0
    if tokens or items:
        tokens, items, blacklisted = filter_blacklisted(
            tokens, items, load_blacklist(tgt["home"]))
    all_tokens = list(dict.fromkeys([*tokens, *[d["access_token"] for d in items]]))
    if not all_tokens:
        if blacklisted:
            return {"ok": False, "blacklisted": blacklisted,
                    "message": f"{blacklisted} 个账号都在剔除名单里；先在列表里启用即可解封"}
        # 邮箱 / 密码 / 2FA 料（email----password----secret）里没有 token，
        # 本号池版本不会用它现场登录换 token，别让用户以为导入成功了。
        material = bool(re.search(r"[\w.+-]+@[\w.-]+\s*[-]{3,4}\s*\S", text))
        return {"ok": False,
                "message": ("文件里是邮箱 / 密码料，没有 access_token"
                            if material else
                            "没读到 access_token：每行贴一个，或粘贴导出的账号 JSON")}
    note = ""
    if len(all_tokens) > MAX_IMPORT:
        items = items[:MAX_IMPORT]
        tokens = tokens[:max(0, MAX_IMPORT - len(items))]
        note = f"一次最多导 {MAX_IMPORT} 个，已取前 {len(items) + len(tokens)} 个"
    # 号池的 POST /api/accounts 同时收 accounts（完整对象）和 tokens（裸 token）。
    # 五段式发料 / JSON 粘贴走前者，邮箱密码、2FA、来源类型和邮箱料都能留下；
    # 裸 token 只能走后者。号池对已存在的账号是「合并更新」，重导同一份文件
    # 还能把以前漏掉的密码补回去，不必删库重来。
    body: dict = {}
    if items:
        body["accounts"] = items
    if tokens:
        body["tokens"] = tokens
    try:
        r = await _req("POST", tgt, "api/accounts", body, timeout=240)
    except Exception as e:
        return {"ok": False, "message": f"导入请求失败：{pool_mod.mask(e)[:120]}"}
    if r.status_code >= 400:
        return {"ok": False, "message": f"号池拒绝导入：{_err_text(r)}"}
    try:
        j = r.json()
    except Exception:
        j = {}
    _remember(all_tokens)
    if items:
        note = (note + " · " if note else "") + f"{len(items)} 个账号连邮箱密码和来源一起导入"
    added = int(j.get("added") or 0)
    skipped = int(j.get("skipped") or 0)
    refreshed = int(j.get("refreshed") or 0)
    errors = [str(e)[:160] for e in (j.get("errors") or [])][:6]
    # 号池报告「已完成校验」且确实有新增，才把额度为 0 的新号从「正常」改成「限流」
    limited = 0
    if added > 0 and refreshed > 0:
        try:
            limited = await _mark_zero_quota(tgt, all_tokens, await _client())
        except Exception:
            limited = 0
    return {"ok": True, "count": len(all_tokens), "added": added, "skipped": skipped,
            "refreshed": refreshed, "limited": limited, "errors": errors,
            "blacklisted": blacklisted,
            "blacklist_total": len(load_blacklist(tgt["home"])),
            "bad": bad, "note": note}


# ---------- 删除 / 停用 ----------
async def _ids_to_tokens(ids: list[str]) -> tuple[list[str], int]:
    ids = [str(i) for i in ids if i]
    tokens = [_TOKENS[i] for i in ids if i in _TOKENS]
    if len(tokens) == len(ids):
        return tokens, 0
    # id 表可能是上一轮进程留下的，先刷一次账号列表再换
    await panel()
    tokens = [_TOKENS[i] for i in ids if i in _TOKENS]
    return tokens, len(ids) - len(tokens)


async def delete_accounts(ids: list[str]) -> dict:
    tgt = target()
    if not tgt["local"]:
        return {"ok": False, "message": "当前接口不是本机号池，无法删除账号"}
    tokens, missing = await _ids_to_tokens(ids)
    if not tokens:
        return {"ok": False, "message": "账号已变化，刷新面板后重试"}
    try:
        r = await _req("DELETE", tgt, "api/accounts", {"tokens": tokens}, timeout=30)
    except Exception as e:
        return {"ok": False, "message": f"删除请求失败：{pool_mod.mask(e)[:120]}"}
    if r.status_code >= 400:
        return {"ok": False, "message": f"号池拒绝删除：{_err_text(r)}"}
    try:
        removed = int((r.json() or {}).get("removed") or 0)
    except Exception:
        removed = 0
    for i in ids:
        _TOKENS.pop(str(i), None)
    bl = blacklist_add(tgt["home"], tokens)
    pool_mod.log(f"号池删除账号 {removed} 个，剔除名单 +{bl}")
    return {"ok": True, "removed": removed, "missing": missing, "blacklisted": bl}


async def set_status(ids: list[str], disabled: bool) -> dict:
    tgt = target()
    if not tgt["local"]:
        return {"ok": False, "message": "当前接口不是本机号池，无法修改账号"}
    tokens, missing = await _ids_to_tokens(ids)
    if not tokens:
        return {"ok": False, "message": "账号已变化，刷新面板后重试"}
    status = "禁用" if disabled else "正常"
    done = 0
    for tok in tokens:
        try:
            r = await _req("POST", tgt, "api/accounts/update",
                           {"access_token": tok, "status": status}, timeout=20)
        except Exception as e:
            return {"ok": False, "message": f"请求失败：{pool_mod.mask(e)[:120]}"}
        if r.status_code >= 400:
            return {"ok": False, "message": f"号池拒绝修改：{_err_text(r)}"}
        done += 1
    # 停用即入剔除名单（以后重导也回不来），启用即解封
    bl = (blacklist_add(tgt["home"], tokens) if disabled
          else blacklist_remove(tgt["home"], tokens))
    return {"ok": True, "changed": done, "status": status, "missing": missing,
            "blacklisted": bl}
