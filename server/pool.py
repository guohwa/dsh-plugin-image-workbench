"""本地版执行层：出图 / 文案统一走 OpenAI 兼容接口（号池或任意第三方均可）。

工作台不绑定任何特定号池：接口地址与密钥完全由 `config.local.json` 的
`api_base_url` / `api_key` 决定，指向本插件内置的 38651 号池、用户已有的
ChatGPT2API 号池（如 38650）、或任意 OpenAI 兼容服务皆可。PoolManager 的
公开接口、job 状态机、result 数据契约保持稳定，因此 app.py / history.py /
imaging.py / 前端代码无需随底层接口变化而改动，历史、单张重做、压缩图、
批量下载全部沿用。

VPS 原件备份在 pool.py.vps_original。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "images"          # 默认保存目录（未配置 output_dir 时用它）
CONFIG_FILE = BASE / "config.local.json"
LOG_FILE = BASE / "pool.log"
# 未完成任务落盘：后端重启/崩溃后把队列捞回来，页面刷新也不会白丢图
STATE_FILE = BASE / "workbench" / "pending_jobs.json"
DEFAULT_POOL_DIR = Path(os.environ.get(
    "DSH_IMAGE_POOL_DIR", str(Path.home() / ".dsh" / "chatgpt2api-image")))

# 并行数（worker 个数）现在由用户自己定：默认 3，写进 config.local.json 的 workers。
# 上限 = 有效账号数（可用账号），一个号同时只出一张图；账号明细读不到时退回默认值，
# 不会因为一次探测失败就把用户设置判成非法。绝对硬顶防止手抖填出四位数。
WORKERS_DEFAULT = 3
WORKERS_ABS_MAX = 40
# 兼容旧引用（环境变量仍可给个初值）
WORKERS = max(1, min(WORKERS_ABS_MAX,
                     int(os.environ.get("WORKBENCH_WORKERS", WORKERS_DEFAULT) or WORKERS_DEFAULT)))
# 队列里的「提前下班」令牌：并行上限下调时塞进队列，worker 取到它就退出。
# 用令牌而不是 cancel 任务，是为了不打断正在出图的那张（cancel 会连带杀掉在途请求）
RETIRE_TOKEN = "__wb_retire__"
IMAGE_TIMEOUT = 300      # 实测单张 45~62 秒，留足余量
TEXT_TIMEOUT = 180
# 文本模型（选道具）被单号限流时换号重试：号池内部严格轮转，重试即换下一个号。
# 只重试 429 / 5xx / 超时这三类「临时不可用」；401/403/404 是配置错，重试没用。
CHAT_RETRY_MAX = 3
CHAT_RETRY_BACKOFF = [4, 8, 15]
# 失败不中断任务：重新排队就是换号（号池按 round-robin 取号），最多换 3 轮。
# 每轮之间留出时间让号池把刚失败的号标掉，避免立刻又撞同一个号。
RETRY_MAX = 3
RETRY_DELAYS = [15, 40, 90]

# 失败归因：号池每次调用都会写一条 /api/logs（detail.account_email 就是那次用的号），
# 成功响应里的 _account_email 被它主动 pop 掉了，所以只能回读日志来对号。
CALL_LOG_TTL = 3.0        # 多个 worker 同时失败时共用一次日志请求
CALL_LOG_WINDOW = 1800.0  # 只认半小时内的调用记录，旧记录不参与匹配

# 账号失败账本：哪个号今天失败过几次，界面上如实显示
FAIL_LEDGER = BASE / "workbench" / "account_fails.json"
FAIL_KEEP_DAYS = 14

RATIO_SIZE = {
    "3:4": "1024x1536", "2:3": "1024x1536", "9:16": "1024x1536",
    "1:1": "1024x1024",
    "4:3": "1536x1024", "3:2": "1536x1024", "16:9": "1536x1024",
    # 公众号首图 2.35:1 上游没有原生尺寸，先按最宽的 3:2 出，
    # 再由 imaging.crop_to 居中裁成 1536x654（比例精确、分辨率够发微信）
    "2.35:1": "1536x1024",
}


def mask(s) -> str:
    s = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+",
               lambda m: m.group(0)[:2] + "***@***", str(s))
    return re.sub(r"sk-[A-Za-z0-9_\-]{12,}", "sk-***", s)


def mask_email(s) -> str:
    """掩码但保留可辨识度：号池的邮箱前缀是随机串，只留 2 位很容易两个号撞成一样，
    真出事时反而看不出是谁。留前 4 位 + 域名。"""
    s = str(s or "").strip()
    if "@" not in s:
        return (s[:3] + "***") if s else "—"
    local, domain = s.split("@", 1)
    return (local[:4] if len(local) > 4 else local[:2]) + "***@" + domain


# ---------- 账号失败账本 ----------
_LEDGER_LOCK = threading.Lock()


def _ledger_load() -> dict:
    try:
        data = json.loads(FAIL_LEDGER.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("days", {})
            data.setdefault("last", {})
            return data
    except Exception:
        pass
    return {"days": {}, "last": {}}


def _ledger_save(data: dict) -> None:
    """原子写：账本是给用户看的凭据，不能因为进程被中断留下半个文件。"""
    try:
        FAIL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        tmp = FAIL_LEDGER.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, FAIL_LEDGER)
    except Exception as e:
        log(f"失败账本写入失败：{mask(e)[:120]}")


def record_account_fail(email: str, err: str) -> int:
    """记一次失败：某某账号在某天失败 1 次。返回该号今天累计次数。"""
    key = str(email or "").strip().lower() or "unknown"
    day = time.strftime("%Y-%m-%d")
    with _LEDGER_LOCK:
        data = _ledger_load()
        days = data["days"]
        bucket = days.setdefault(day, {})
        bucket[key] = int(bucket.get(key) or 0) + 1
        data["last"][key] = {"error": mask(err)[:200], "at": int(time.time())}
        # 只留最近两周，别让账本无限长
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - FAIL_KEEP_DAYS * 86400))
        for d in [d for d in days if d < cutoff]:
            days.pop(d, None)
        _ledger_save(data)
        return bucket[key]


def fails_today() -> dict:
    """今天各账号的失败次数：{email: n}（email 为明文，展示时再掩码）。"""
    with _LEDGER_LOCK:
        data = _ledger_load()
    return dict(data.get("days", {}).get(time.strftime("%Y-%m-%d"), {}))


def fails_summary() -> list[dict]:
    """给监控面板用：按失败次数倒序，带最后一次的错误。"""
    today = fails_today()
    with _LEDGER_LOCK:
        data = _ledger_load()
    last = data.get("last") or {}
    rows = []
    for email, n in today.items():
        info = last.get(email) or {}
        rows.append({"account": mask_email(email) if "@" in email else "未知账号",
                     "account_email": email, "count": int(n),
                     "error": str(info.get("error") or ""), "at": int(info.get("at") or 0)})
    rows.sort(key=lambda x: (-x["count"], -x["at"]))
    return rows


# 号池账号 status（正常/限流/禁用/异常）-> 工作台监控用的状态键
STATUS_MAP = {"正常": "ready", "限流": "limited", "禁用": "unhealthy", "异常": "error"}


def map_status(raw) -> str:
    return STATUS_MAP.get(str(raw or "").strip(), "error")


# 本机内置号池：端口 → DSH 里的目录名。换接口时账号明细要跟着新接口走，
# 不能还拿上一个号池的 admin-key 去问新号池（会 401，界面就留着旧账号不放）。
LOCAL_POOL_PORTS = {"38650": "chatgpt2api-native", "38651": "chatgpt2api-image"}


def _read_admin_key(d: Path) -> str:
    try:
        return (d / "admin-key").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _dsh_home() -> Path:
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def pool_home(cfg: dict | None = None) -> Path:
    """当前接口对应的号池数据目录（admin-key / user-api-keys.json 都在里面）。

    先按 Base URL 的端口映射到 ~/.dsh 下的号池目录，对不上再退回配置里的 pool_dir。
    本机同时开着两个号池（38650 写作、38651 生图），切换接口后必须跟着换目录，
    否则会拿错 admin-key 和用户 Key——以前就是这么串的。
    """
    cfg = cfg or load_config()
    base = str(cfg.get("api_base_url") or cfg.get("pool_base_url") or "")
    try:
        port = str(urlparse(base).port or "")
    except Exception:
        port = ""
    name = LOCAL_POOL_PORTS.get(port)
    if name:
        d = _dsh_home() / name
        if (d / "admin-key").exists() or (d / "user-api-keys.json").exists():
            return d
    return Path(str(cfg.get("pool_dir") or DEFAULT_POOL_DIR))


def pool_admin_key(cfg: dict | None = None) -> str:
    """拉真实账号列表用的号池 admin key（/api/accounts），绝不随便给浏览器。

    先看当前接口对应的号池目录，没有 admin-key 再退回配置里的 pool_dir：
    以前只认 pool_dir，把 Base URL 从 38651 换成 38650 之后仍在用 38651 的
    admin-key，/api/accounts 直接 401，界面只好继续显示上一个号池的账号。
    """
    cfg = cfg or load_config()
    key = _read_admin_key(pool_home(cfg))
    if key:
        return key
    return _read_admin_key(Path(str(cfg.get("pool_dir") or DEFAULT_POOL_DIR)))


def _parse_dt(s) -> float:
    if not s or str(s).strip() in ("None", "null", ""):
        return 0.0
    s = str(s).strip().replace("Z", "").replace("T", " ")
    m = re.match(r"^(.*?)([+-]\d{2}:?\d{2})?\s*$", s)
    base = (m.group(1) if m else s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(base, fmt).timestamp()
        except Exception:
            continue
    return 0.0


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {mask(msg)}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# 版本段：v1 / v2 / v1beta / v1beta1 都算，结尾有它就不再补 /v1
_VER_TAIL = re.compile(r"/v\d+[a-z0-9]*$", re.I)
# 用户直接粘完整端点时顺手剥掉，只留根
_ENDPOINT_TAIL = re.compile(
    r"/(images/generations|images/edits|images/variations|chat/completions|completions|models)$", re.I)


def norm_base_url(raw: str) -> str:
    """把 Base URL 归一化成「带版本段」的形式。

    界面上显示的必须是 http://127.0.0.1:38651/v1 这种可读地址：
    大多数人是从 OpenAI 文档照抄的，少写 /v1 会让人怀疑是不是填错了。
    请求侧本来就统一发 /v1/... 绝对路径，所以带不带都能通，这里只是不让它看起来像错的。
    """
    b = str(raw or "").strip().rstrip("/")
    if not b:
        return b
    if not re.match(r"^https?://", b, re.I):
        # 本机默认 http，外部域名默认 https，省得用户少打协议头就报连不上
        local = re.match(r"^(127\.0\.0\.1|localhost|\[::1\])(:|\/|$)", b, re.I)
        b = ("http://" if local else "https://") + b
    # 粘了整条端点（.../v1/images/generations）时截到版本段为止
    m = _ENDPOINT_TAIL.search(b)
    if m:
        b = b[:m.start()].rstrip("/")
    if not _VER_TAIL.search(urlparse(b).path.rstrip("/")):
        b = b + "/v1"
    return b


def out_dir(cfg: dict | None = None) -> Path:
    """当前生效的图片保存目录。

    config.local.json 的 output_dir 可写绝对路径，也可写相对路径（相对工作台目录）；
    留空就用默认的 images/。目录不可用时退回默认目录，绝不让存图这一步把出图弄挂。
    """
    cfg = cfg if cfg is not None else load_config()
    raw = str(cfg.get("output_dir") or "").strip()
    if not raw:
        return OUT_DIR
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not p.is_absolute():
        p = BASE / p
    try:
        p.mkdir(parents=True, exist_ok=True)
        if not os.access(p, os.W_OK):
            raise OSError("目录不可写")
        return p
    except Exception as e:
        log(f"保存目录 {p} 不可用（{e}），本次退回 {OUT_DIR}")
        return OUT_DIR


def image_dirs() -> list[Path]:
    """找图时依次尝试的目录：当前目录 → 用过的旧目录 → 默认目录。

    用户把保存目录从 D:\\a 换到 D:\\b 之后，D:\\a 里那些图在历史里还得能打开，
    否则一改设置，之前的作品全变成破图。
    """
    cfg = load_config()
    dirs = [out_dir(cfg)]
    for s in reversed(cfg.get("output_dirs_seen") or []):
        try:
            p = Path(str(s))
        except Exception:
            continue
        if p.is_dir() and p not in dirs:
            dirs.append(p)
    if OUT_DIR not in dirs:
        dirs.append(OUT_DIR)
    return dirs


def image_path(name: str) -> Path | None:
    """按文件名找图：当前保存目录优先，其次历史目录，最后默认目录。"""
    name = str(name or "").strip()
    if not name or "/" in name or "\\" in name:
        return None
    for d in image_dirs():
        p = d / name
        if p.is_file():
            return p
    return None


def load_config() -> dict:
    cfg = {
        # 旧字段（保留兼容）：pool_base_url / pool_dir / model_image / model_text
        "pool_base_url": "http://127.0.0.1:38651/v1",
        "pool_dir": str(DEFAULT_POOL_DIR),
        "model_image": "gpt-image-2",
        "model_text": "auto",
        # 生成图片保存目录：绝对路径，或相对工作台目录的路径；空 = 默认 images/
        "output_dir": "",
        # 工作台显示名（页头标题 / 浏览器标签）；空 = 用内置默认名
        "app_name": "",
        # —— 通用接口（Provider）扩展字段 ——
        "provider_name": "本地生图号池",
        "supports_edits": True,           # 是否支持参考图（/v1/images/edits）
        "edits_style": "auto",            # auto | json | multipart | off
        "image_response_format": "auto",  # auto | b64_json | url
        "monitor_mode": "auto",           # auto | pool | single
        # 并行出图数（worker 个数）：默认 3，上限由「可用账号数」决定
        "workers": WORKERS,
    }
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"config.local.json 读取失败: {e}")
    return cfg


def save_config(updates: dict) -> dict:
    """把若干键合并写回 config.local.json（原子替换），返回写后的完整配置。

    只做浅合并：别的键原样留着，不会因为存一个并行数就把接口配置抹掉。
    """
    cfg: dict = {}
    if CONFIG_FILE.exists():
        try:
            got = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                cfg = got
        except Exception as e:
            log(f"config.local.json 读取失败（准备覆盖写）: {e}")
    cfg.update(updates or {})
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except Exception as e:
        log(f"config.local.json 写入失败: {mask(e)[:160]}")
    return cfg


def workers_from_cfg() -> int:
    """启动时的并行数：配置里存的值优先，非法值一律退回默认 3。"""
    try:
        n = int(load_config().get("workers") or WORKERS_DEFAULT)
    except Exception:
        n = WORKERS_DEFAULT
    return max(1, min(WORKERS_ABS_MAX, n))


def _read_pool_user_key(cfg: dict) -> str:
    """从号池目录里的 user-api-keys.json 读第一个 sk- 用户 Key（仅号池模式用）。"""
    p = pool_home(cfg) / "user-api-keys.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        for v in (d.values() if isinstance(d, dict) else d):
            if isinstance(v, str) and v.strip().startswith("sk-"):
                return v.strip()
    except Exception as e:
        log(f"号池用户 Key 读取失败: {e}")
    return ""


def pool_api_key(cfg: dict | None = None) -> str:
    """兼容旧名。用户 Key 只在服务端读取，绝不返回给浏览器。"""
    cfg = cfg or load_config()
    key = str(cfg.get("api_key") or cfg.get("pool_api_key") or "").strip()
    return key or _read_pool_user_key(cfg)


def provider_cfg(cfg: dict | None = None) -> dict:
    """把配置归一化成通用 Provider 视图（兼容旧字段）。

    这样无论接的是本地号池，还是任意 OpenAI 兼容接口，执行层只认这一份结构。
    """
    cfg = cfg or load_config()
    base = norm_base_url(str(cfg.get("api_base_url") or cfg.get("pool_base_url") or ""))
    admin = str(cfg.get("pool_dir") or DEFAULT_POOL_DIR)
    netloc = urlparse(base).netloc if base else ""
    is_pool = (netloc.startswith("127.0.0.1:") or netloc.startswith("localhost:")
               or str(cfg.get("monitor_mode") or "") == "pool")
    return {
        "name": str(cfg.get("provider_name") or "当前接口"),
        "base_url": base,
        # 版本段之前的根（http://host:port 或带前置路径的那种），号池管理端点用
        "api_root": _VER_TAIL.sub("", base.rstrip("/")) or base,
        "api_key": pool_api_key(cfg),
        "model_image": str(cfg.get("model_image") or "gpt-image-2"),
        "model_text": str(cfg.get("model_text") or "auto"),
        "supports_edits": bool(cfg.get("supports_edits", True)),
        "edits_style": str(cfg.get("edits_style") or "auto"),
        "image_format": str(cfg.get("image_response_format") or "auto"),
        "monitor_mode": str(cfg.get("monitor_mode") or "auto"),
        "pool_dir": admin,
        "output_dir": str(out_dir(cfg)),
        "is_pool": is_pool,
    }


def api_url(p: dict, path: str) -> str:
    """拼接 /v1 下的接口地址。base_url 已经带版本段，所以只拼后半截。

    不能用 httpx 的 base_url 自动合并：base 带路径时它是「追加」而不是「替换」，
    base=http://h/v1 + /v1/models 会变成 http://h/v1/v1/models，直接 404。
    """
    return str(p["base_url"]).rstrip("/") + "/" + path.lstrip("/")


def root_url(p: dict, path: str) -> str:
    """拼接版本段之外的管理端点（/health、/api/accounts）。"""
    return str(p.get("api_root") or p["base_url"]).rstrip("/") + "/" + path.lstrip("/")


def size_for_ratio(ratio) -> str | None:
    return RATIO_SIZE.get(str(ratio or "").strip())


async def chat_once(prompt: str, *, max_tokens: int = 600, temperature: float = 0.5,
                   timeout: float | None = None) -> str:
    """轻量级单轮文本调用：直接走已配置的号池 / OpenAI 兼容接口，不建 job、不写历史。

    用于「让模型读懂文案再挑元素」这类轻量辅助决策。被单号限流（429）时自动换号重试：
    号池内部严格轮转，每次请求都带唯一 user 强制穿透缓存、落到下一个号，重试即换号。
    重试耗尽仍失败才抛异常，由调用方回退到关键词规则。
    """
    p = provider_cfg()
    key = p["api_key"]
    if not key:
        raise RuntimeError("未配置接口 API Key，无法用 AI 挑选元素（请在「接口与号池」面板填写）")
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    payload = {
        "model": p["model_text"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # 唯一 user：穿透号池 60s 聊天缓存，保证每次重试真的换号轮转，而不是命中缓存的同一份 429
        "user": f"wb-scene-{int(time.time() * 1000)}",
    }
    last_err = ""
    for attempt in range(CHAT_RETRY_MAX):
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=timeout or TEXT_TIMEOUT) as c:
                r = await c.post(api_url(p, "chat/completions"), json=payload, headers=headers)
            if r.status_code == 200:
                data = _json_of(r)
                try:
                    text = str(data["choices"][0]["message"]["content"] or "").strip()
                except Exception:
                    text = str(data.get("text") or "").strip()
                text = _sanitize_text(text)
                if not text:
                    raise RuntimeError("AI 挑选元素返回为空")
                return text
            # 4xx 配置错误重试没用；只有 429 / 5xx 才重试（号池会换号）
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}"
                if attempt < CHAT_RETRY_MAX - 1:
                    await asyncio.sleep(CHAT_RETRY_BACKOFF[min(attempt, len(CHAT_RETRY_BACKOFF) - 1)])
                    continue
            raise_for_response(r)
        except httpx.TimeoutException as e:
            last_err = f"超时：{e}"
            if attempt < CHAT_RETRY_MAX - 1:
                await asyncio.sleep(CHAT_RETRY_BACKOFF[min(attempt, len(CHAT_RETRY_BACKOFF) - 1)])
                continue
            raise RuntimeError(f"AI 挑选元素失败（重试 {CHAT_RETRY_MAX} 次仍超时）") from e
    raise RuntimeError(f"AI 挑选元素失败（重试 {CHAT_RETRY_MAX} 次仍失败：{last_err or '未知错误'}）")


def _json_of(r: httpx.Response) -> dict:
    try:
        j = r.json()
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


# 模型偶发会把内部协议碎片带进正文，典型形态是「英文词/长串字母数字」如 turn/5u8495ea07e7、
# turn/5B4095ea0ew9，也可能是 unicode 替换符。这类垃圾只在不含中文的孤立片段里出现，
# 所以清洗时只动这些片段，正常的简体中文正文一个字都不会被碰。
# 注意后缀是「字母数字混合串」（含 u/e/a 这类非纯十六进制的字母），所以字符类要放宽到 [0-9a-zA-Z]；
# 用 (?<!:) 排除 http(s):// 这类正常链接的协议段，避免误伤。
_GARBAGE_RE = re.compile(r"(?<!:)\b[a-z]{2,}/[0-9a-zA-Z]{6,}\b")


# 零宽与不可见字符：模型偶发会把 ZWSP/BOM/替换符带进正文，肉眼看不见但会污染复制与分镜解析
_ZEROWIDTH_RE = re.compile(
    "[" + "".join([
        "\u200b",  # 零宽空格
        "\u200c",  # 零宽不连字
        "\u200d",  # 零宽连字
        "\ufeff",  # BOM / 零宽不换行空格
        "\ufffd",  # 替换符（乱码占位）
    ]) + "]"
)


def _sanitize_text(text: str) -> str:
    s = str(text or "")
    s = _GARBAGE_RE.sub("", s)              # 清掉 turn/5u8495ea07e7 这类协议碎片
    s = _ZEROWIDTH_RE.sub("", s)            # 清掉零宽/BOM/替换符
    # 清掉 ESC(\x1b) 等终端控制字符：模型偶发把这类不可见字符带进 JSON，
    # 会污染 json.loads 造成解析错位（如把数组前缀变成垃圾元素）
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    s = s.replace("<escape>", "")           # 个别接口把 ESC 渲染成字面 <escape>
    s = re.sub(r"[ \t]{2,}", " ", s)        # 清掉删词后留下的多余空格
    s = re.sub(r"\n{3,}", "\n\n", s)        # 连续空行压成两段
    return s.strip()


def _err_text(j: dict) -> str:
    e = j.get("error") or j.get("detail") or j
    if isinstance(e, dict):
        return str(e.get("message") or e.get("error") or e)
    return str(e)


def raise_for_response(r: httpx.Response) -> None:
    """把接口的各种失败翻译成用户能看懂、能自己动手解决的提示。"""
    j = _json_of(r)
    msg = _err_text(j)[:300]
    low = msg.lower()
    if r.status_code == 401:
        raise RuntimeError("接口 API Key 无效或未配置，请打开「接口设置」检查密钥")
    if r.status_code == 403:
        raise RuntimeError("接口拒绝访问（403）：密钥无权限或该模型未开通")
    if r.status_code == 404:
        raise RuntimeError(
            "接口地址不存在（404）：确认 Base URL 是否以 /v1 结尾、路径是否为 OpenAI 兼容接口")
    if r.status_code == 429 or "no available image quota" in low or "quota" in low and "exceed" in low:
        raise RuntimeError("接口额度已用完或触发限流，等配额恢复或换一个接口 / 密钥")
    if r.status_code == 502 or "proxy" in low or "failed to connect" in low or "timed out" in low or "socks" in low:
        raise RuntimeError("接口不可达：确认 Base URL、本机网络或出口代理（号池需能访问 OpenAI / ChatGPT）")
    if "unsupported image model" in low or "model_not_found" in low or "does not exist" in low:
        raise RuntimeError(f"接口不支持该出图模型：{msg[:120]}，请在「接口设置」里换一个模型名")
    if "content_policy_violation" in low or "text reply" in low:
        raise RuntimeError("上游没返回图片（只回了文字），把画面描述写得更具体后重试")
    raise RuntimeError(f"接口 HTTP {r.status_code}: {msg}")


async def test_connection(overrides: dict | None = None) -> dict:
    """按给定（或已保存）配置试连一次，返回 {ok,status,message,models}。

    Base URL 会被归一化成带 /v1 的形式，请求地址按 base_url + 尾段拼，带不带 /v1 都能通。
    """
    cfg = load_config()
    if overrides:
        # 前端提交的字段名是 base_url，存量配置里叫 api_base_url / pool_base_url
        if overrides.get("base_url"):
            cfg["api_base_url"] = overrides["base_url"]
        for k in ("api_base_url", "pool_base_url", "api_key", "pool_api_key",
                  "model_image", "model_text", "supports_edits", "edits_style",
                  "image_response_format", "provider_name", "monitor_mode"):
            if k in overrides and overrides[k] not in (None, ""):
                cfg[k] = overrides[k]
    p = provider_cfg(cfg)
    if not p["base_url"]:
        return {"ok": False, "status": 0, "message": "未填写 Base URL", "models": []}
    hdr = {"Authorization": "Bearer " + p["api_key"]} if p["api_key"] else {}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as c:
            r = await c.get(api_url(p, "models"), headers=hdr)
        if r.status_code == 200:
            data = _json_of(r).get("data") or []
            models = [str(m.get("id")) for m in data
                      if isinstance(m, dict) and m.get("id")][:60]
            return {"ok": True, "status": 200,
                    "message": f"连接成功，发现 {len(models)} 个模型",
                    "models": models}
        if r.status_code in (401, 403):
            return {"ok": False, "status": r.status_code,
                    "message": "密钥无效或无权限（401/403），请检查 API Key", "models": []}
        if r.status_code == 404:
            return {"ok": False, "status": 404,
                    "message": "地址可达但没有 /v1/models：确认 Base URL 是否为 OpenAI 兼容根（可带或不带 /v1）",
                    "models": []}
        return {"ok": False, "status": r.status_code,
                "message": f"接口返回 HTTP {r.status_code}：{_err_text(_json_of(r))[:160]}",
                "models": []}
    except Exception as e:
        return {"ok": False, "status": 0,
                "message": f"连接失败：{mask(e)[:160]}", "models": []}


class PoolManager:
    """保持线上版接口：accounts / jobs / running / on_finish / check_state /
    start / stop / submit / snapshot / refresh_env_ids / reset_statuses / check_all。
    """

    def __init__(self) -> None:
        # 真实账号列表：来自号池 /api/accounts（admin key），数量 = 号池实际账号数。
        # 选号与严格轮转由号池内部负责；这里只把真实账号铺进监控，不再用占位单号。
        self.accounts: list[dict] = []
        # 工作台侧 Job 记账用的端点占位（不进监控列表，仅用于 running/历史归属）
        self.endpoint: dict = {
            # 这只是本地状态槽位的显示名，不是接口类型：出图和文案共用同一套配置
            "id": 0, "masked": "当前接口", "env_name": "api",
            "env_id": 0, "status": "ready", "last_used": 0.0, "last_ok": 0.0,
            "last_error_at": 0.0, "uses": 0, "fails": 0, "error": "",
            "busy_since": 0.0, "cooldown": 0, "check_ok": None, "check_msg": "", "check_at": 0.0,
        }
        self._rr_index = 0
        self.jobs: dict[str, dict] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.running: dict[str, dict] = {}
        self._workers: list[asyncio.Task] = []
        # 并行数：用户可调，持久化在 config.local.json 的 workers
        self.workers: int = workers_from_cfg()
        self._wid_seq: int = 0          # worker 编号只增不减，日志里对得上
        self._retire_pending: int = 0   # 队列里还没被取走的「提前下班」令牌数
        self._workers_lock = asyncio.Lock()
        # 正在执行的单个任务：手动停止时只取消它，不杀 worker，队列才能继续接新活
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._check_task: asyncio.Task | None = None
        self._acct_task: asyncio.Task | None = None
        self._acct_cache_at: float = 0.0
        # 当前账号列表是为哪个接口拉的：接口一换，旧列表立刻作废，别让它接着显示
        self._prov_sig: tuple | None = None
        # 账号列表的「上一次长什么样」：只在变化时写日志，空转时同一份内容不重复记
        self._acct_sig: tuple | None = None
        self._acct_beat_at: float = 0.0
        # 上次接口探测时间：离线状态下要能自动重探，否则一次失败会把「离线」钉死
        self._probe_at: float = 0.0
        # 号池调用日志缓存：失败时按 prompt + 起始时间回查，定位这次是哪个号干的
        self._log_cache: tuple[float, list[dict]] = (0.0, [])
        self.check_state: dict = {"running": False, "total": 1, "done": 0,
                                  "started": 0, "finished": 0, "items": {}}
        self.pool_stat: dict = {"online": False, "total": 0, "active": 0,
                                "limited": 0, "quota_total": 0, "message": "尚未探测",
                                "mode": "single", "name": "当前接口"}
        self.on_finish = None

    # ---------- lifecycle ----------
    async def start(self) -> None:
        await self.refresh_env_ids()
        await self.refresh_accounts(force=True)
        self._restore()
        self.workers = workers_from_cfg()
        for _ in range(self.workers):
            self._spawn_worker()
        self._acct_task = asyncio.create_task(self._account_refresh_loop())
        p = provider_cfg()
        log(f"本地执行层启动：{self.workers} 个 worker，接口 {p['base_url']}"
            f"（{p['name']}），已同步 {len(self.accounts)} 个账号")

    def _spawn_worker(self) -> asyncio.Task:
        self._wid_seq += 1
        t = asyncio.create_task(self._worker(self._wid_seq))
        self._workers.append(t)
        return t

    # ---------- 并行数（用户可调） ----------
    def valid_accounts(self) -> int:
        """有效账号数 = 能出图的号（可用 + 正在出图中）。

        「正在出图」也是有效账号，不能因为它在忙就从上限里扣掉：否则一边出图
        一边上限往下掉，用户刚设好的并行数会被自己压回去。
        限流 / 已下线 / 未绑定 / 待验证 / 异常这些才算无效。
        """
        if self.accounts:
            return sum(1 for a in self.accounts
                       if a.get("status") in ("ready", "busy"))
        # 账号明细拿不到时退回号池自报的可用数
        try:
            return int(self.pool_stat.get("active") or 0)
        except Exception:
            return 0

    def workers_cap(self) -> int:
        """并行上限：不超过有效账号数（一个号同时只出一张图）。"""
        n = self.valid_accounts()
        if n <= 0:
            # 账号明细暂时读不到时不收紧，免得一次探测失败就把用户的设置判成非法
            return max(WORKERS_DEFAULT, self.workers)
        return max(1, min(n, WORKERS_ABS_MAX))

    async def set_workers(self, n: int) -> dict:
        """改并行数：多的立刻拉起来，少的发「下班令牌」等它把手上的图出完再退。"""
        async with self._workers_lock:
            cap = self.workers_cap()
            try:
                want = int(n)
            except Exception:
                return {"ok": False, "workers": self.workers, "max": cap,
                        "message": "并行数必须是整数"}
            if want < 1:
                return {"ok": False, "workers": self.workers, "max": cap,
                        "message": "并行数至少 1"}
            target = min(want, cap)
            old = self.workers
            # 已经退出的 worker 任务从列表里清掉，别让列表越堆越长
            self._workers = [t for t in self._workers if not t.done()]
            if target > old:
                for _ in range(target - old):
                    self._spawn_worker()
            elif target < old:
                for _ in range(old - target):
                    self._retire_pending += 1
                    await self.queue.put(RETIRE_TOKEN)
            self.workers = target
            save_config({"workers": target})
            if target != old:
                log(f"并行数 {old} → {target}"
                    + (f"（请求 {want}，按有效账号 {cap} 收紧）" if target != want else ""))
        msg = f"并行上限已设为 {target}"
        if want > cap:
            msg = f"最多 {cap} 个：并行数不能超过有效账号数，已按 {cap} 执行"
        return {"ok": True, "workers": target, "max": cap, "requested": want,
                "prev": old, "clamped": want > cap, "message": msg}


    # ---------- 任务落盘 / 恢复 ----------
    def _persist(self) -> None:
        """把「还没结束」的任务写进磁盘。写完原子替换，进程被杀也不会写出半个文件。"""
        try:
            live = [j for j in self.jobs.values()
                    if j.get("status") not in ("done", "error", "cancelled")]
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
            tmp.replace(STATE_FILE)
        except Exception as e:
            log(f"任务状态落盘失败：{mask(e)[:120]}")

    def _restore(self) -> int:
        """启动时把上次没跑完的任务重新入队。图片已出的任务不会再存盘，所以不会重出。"""
        if not STATE_FILE.exists():
            return 0
        try:
            rows = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"未完成任务读取失败：{mask(e)[:120]}")
            return 0
        n = 0
        for job in rows if isinstance(rows, list) else []:
            if not isinstance(job, dict) or not job.get("id") or job["id"] in self.jobs:
                continue
            job["status"] = "queued"
            job["auto_retry"] = int(job.get("auto_retry") or 0)
            job.pop("retry_at", None)
            job["restored"] = 1
            self.jobs[job["id"]] = job
            self.queue.put_nowait(job["id"])
            n += 1
        if n:
            log(f"重启恢复：{n} 个未完成任务重新入队")
        return n

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        if self._check_task:
            self._check_task.cancel()
        if self._acct_task:
            self._acct_task.cancel()

    # ---------- 号池状态 ----------
    async def refresh_env_ids(self) -> None:
        """探测当前接口。

        1) 先试号池专有 /health?format=json（含 accounts 汇总）→ mode=pool，可展开账号明细；
        2) 失败则降级为通用接口探测（/v1/models）→ mode=single，在线但没有账号明细。
        这样换成任意第三方兼容接口都不会因「没有 /health」被误判为离线。
        """
        p = provider_cfg()
        st = {"online": False, "total": 0, "active": 0, "limited": 0,
              "quota_total": 0, "status": "", "message": "尚未探测",
              "mode": "single", "name": p["name"]}
        # --- 1) 号池专有探测 ---
        if p["monitor_mode"] != "single":
            try:
                async with httpx.AsyncClient(trust_env=False, timeout=8) as c:
                    r = await c.get(root_url(p, "health?format=json"))
                    j = _json_of(r) if r.status_code == 200 else {}
                a = j.get("accounts") or {}
                total = int(a.get("total") or 0)
                if total > 0:
                    st.update({
                        "mode": "pool",
                        "total": total,
                        "active": int(a.get("active") or 0),
                        "limited": int(a.get("limited") or 0),
                        "quota_total": int(a.get("total_quota") or 0),
                        "status": str(j.get("status") or ""),
                        "online": True,
                    })
                    st["message"] = f"{total} 个账号 / 配额 {st['quota_total']}"
                    self.pool_stat = st
                    self._probe_at = time.time()   # 命中号池分支也要记，否则每次轮询都重探
                    return
            except Exception:
                pass
        # --- 2) 通用接口探测 ---
        try:
            hdr = {"Authorization": "Bearer " + p["api_key"]} if p["api_key"] else {}
            async with httpx.AsyncClient(trust_env=False, timeout=8) as c:
                r = await c.get(api_url(p, "models"), headers=hdr)
            ok = r.status_code < 500
            st.update({
                "mode": "single",
                "online": ok,
                "total": 1,
                "active": 1 if ok else 0,
                "message": (f"通用接口可用 · {p['name']}" if ok
                            else f"接口返回 HTTP {r.status_code} · {p['name']}"),
            })
        except Exception as e:
            st.update({"mode": "single", "online": False,
                       "message": f"接口不可达：{mask(e)[:120]}"})
        self.pool_stat = st
        self._probe_at = time.time()
        # 自动重探会反复失败，同一条错误只记一次，别把日志刷满
        if not st["online"] and st["message"] != getattr(self, "_probe_msg", ""):
            log(f"接口探测失败：{st['message']}")
        self._probe_msg = "" if st["online"] else st["message"]

    async def ensure_online(self, min_interval: float = 15.0,
                           recheck_interval: float = 60.0) -> None:
        """离线时自动重探（节流），在线时也隔一分钟对一次账。

        号池是真异步起的：工作台后端先就绪、号池还在加载账号库，启动那一次探测
        必然失败，然后 pool_stat.online 就一直钉在 False，界面永远显示离线，
        得手动点「刷新状态」才好。所以只要当前是离线态，就隔一段时间自己再探一次。

        在线也不能一直不探：以前上线之后直接 return，号池增减账号时 /health 报的
        数字永远停在第一次成功的那一刻（导入 33 个号之后仍显示 19 个账号）。
        """
        online = bool(self.pool_stat.get("online"))
        gap = recheck_interval if online else min_interval
        if time.time() - self._probe_at < gap:
            return
        await self.refresh_env_ids()

    def _drop_accounts(self) -> None:
        self.accounts = []
        self._acct_sig = None

    def _accounts_unavailable(self, msg: str) -> None:
        """账号明细拿不到时的收尾。

        只有「旧列表条数还和号池自报的总数对得上」才敢继续用，否则一律清空：
        换接口后 admin-key 常对不上（401），以前这种情况会沿用旧列表，监控里就一直
        挂着上一个号池的账号，右上角也照旧显示 32/33。
        """
        total = int(self.pool_stat.get("total") or 0)
        if self.accounts and len(self.accounts) == total:
            self.pool_stat.pop("accounts_error", None)
            log(msg + "，沿用上次账号列表")
            return
        self._drop_accounts()
        self.pool_stat["accounts_error"] = msg
        log(msg + "，已清空账号列表")

    async def refresh_accounts(self, force: bool = False) -> None:
        """从号池 /api/accounts 拉真实账号；非号池接口没有此端点时静默跳过。"""
        now = time.time()
        p = provider_cfg()
        sig = (p["base_url"], p["monitor_mode"], str(p["pool_dir"]))
        if sig != self._prov_sig:
            # 换了接口（地址 / 监控模式 / 号池目录任一变化）：上一个接口的账号一个都不留
            self._prov_sig = sig
            self._drop_accounts()
            self.pool_stat.pop("accounts_error", None)
            self._acct_cache_at = 0
        if not force and self.accounts and now - self._acct_cache_at < 20:
            return
        if p["monitor_mode"] == "single":
            self._drop_accounts()
            self._acct_cache_at = now
            return
        admin = pool_admin_key()
        if not admin:
            self._accounts_unavailable("号池模式下没找到 admin-key，账号明细不可用")
            self._acct_cache_at = now
            return
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=15) as c:
                r = await c.get(root_url(p, "api/accounts"),
                                headers={"Authorization": "Bearer " + admin})
            if r.status_code != 200:
                # 通用接口没有 /api/accounts 属正常；但号池模式下 401 多半是 admin-key
                # 和当前接口对不上，这时必须清空，否则旧号池的账号会一直挂在监控里。
                self._accounts_unavailable(f"账号明细拉取失败（HTTP {r.status_code}）")
                self._acct_cache_at = now
                return
            items = (_json_of(r).get("items") or [])
        except Exception as e:
            self._accounts_unavailable(f"账号明细拉取失败：{mask(e)[:80]}")
            self._acct_cache_at = now
            return
        self.pool_stat.pop("accounts_error", None)
        accounts = []
        for idx, a in enumerate(items, start=1):
            st = map_status(a.get("status"))
            last_used = _parse_dt(a.get("last_used_at"))
            # image_inflight 是号池里「这个号正在出几张图」，唯一可信的占用信号
            try:
                inflight = int(a.get("image_inflight") or 0)
            except (TypeError, ValueError):
                inflight = 0
            if inflight > 0 and st == "ready":
                st = "busy"
            cooldown = 0
            if st == "limited":
                ts = _parse_dt(a.get("restore_at"))
                cd = int(ts - time.time())
                cooldown = cd if cd > 0 else 0
            note = a.get("last_refresh_error") or ""
            if st == "limited":
                note = note or "限流中"
            elif st == "busy":
                note = f"出图中 ×{inflight}"
            accounts.append({
                "id": idx,
                "env_id": idx,
                "env_name": "cpa-image-pool",
                "masked": mask_email(a.get("email")),
                "label": mask_email(a.get("email")),
                "status": st,
                "uses": int(a.get("success") or 0),
                "fails": int(a.get("fail") or 0),
                "hist_done": int(a.get("success") or 0),
                "hist_error": int(a.get("fail") or 0),
                "hist_last_ok": int(last_used or 0),
                "last_used": last_used,
                "last_ok": last_used,
                "cooldown": cooldown,
                "error": note or "",
                "busy_since": (last_used or now) if st == "busy" else 0.0,
                "inflight": inflight,
                "check_ok": None,
                "check_msg": note or "",
                "check_at": 0,
                "quota": int(a.get("quota") or 0),
            })
        self.accounts = accounts
        self._acct_cache_at = now
        # 账号明细就是真相。号池刚导入/删号时它自己的 /health 还是旧的（health 由号池
        # 内部定时重算），页头「号池 X/Y 可用」和监控里的配额会跟着错一截。
        if self.pool_stat.get("mode") == "pool":
            self.pool_stat.update({
                "total": len(accounts),
                "active": sum(1 for x in accounts if x["status"] in ("ready", "busy")),
                "limited": sum(1 for x in accounts if x["status"] == "limited"),
                "quota_total": sum(int(x.get("quota") or 0) for x in accounts),
            })
            self.pool_stat["message"] = (f"{len(accounts)} 个账号 / "
                                         f"配额 {self.pool_stat['quota_total']}")
        sig = (len(accounts),
               sum(1 for x in accounts if x["status"] == "ready"),
               sum(1 for x in accounts if x["status"] == "limited"),
               sum(1 for x in accounts if x["status"] in ("error", "unhealthy")),
               sum(int(x.get("quota") or 0) for x in accounts))
        # 号池的内存列表只在真的出图 / 手动刷新时才会变，空转期间拉到的永远是同一份。
        # 以前每次拉都写一行「账号列表已同步」，一天能刷出四千多行噪声日志；
        # 现在只在状态真的变了才记，另外每半小时留一条心跳，方便确认同步没死。
        if sig != self._acct_sig or now - self._acct_beat_at >= 1800:
            self._acct_sig = sig
            self._acct_beat_at = now
            log(f"账号列表已同步：{sig[0]} 个（可用 {sig[1]} / 限流 {sig[2]} / 异常 {sig[3]}，"
                f"配图额度 {sig[4]}）")

    async def _call_logs(self) -> list[dict]:
        """读号池最近的调用日志。3 秒缓存，三个 worker 同时失败也只打一次。

        只对本地号池生效：通用第三方接口没有这个端点，失败就记「未知账号」，
        不影响出图本身。
        """
        now = time.time()
        ts, items = self._log_cache
        if now - ts < CALL_LOG_TTL:
            return items
        p = provider_cfg()
        admin = pool_admin_key()
        if not admin or not p["is_pool"]:
            self._log_cache = (now, [])
            return []
        got: list[dict] = []
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=6) as c:
                # type=call 只要调用日志：号池每 5 分钟会把 30 多个账号的刷新记录也写进去，
                # 不过滤的话 200 条窗口只够覆盖二十几分钟
                r = await c.get(root_url(p, "api/logs") + "?type=call",
                                headers={"Authorization": "Bearer " + admin})
            if r.status_code == 200:
                got = [i for i in (_json_of(r).get("items") or [])
                       if str(i.get("type") or "") == "call"]
        except Exception:
            got = []
        self._log_cache = (now, got)
        return got

    async def _attribute_call(self, job: dict, since: float) -> str:
        """这次调用落到哪个号上：按 prompt 片段 + 起始时间在号池日志里对上号。

        号池是内部轮转选号，请求里指定不了账号，所以只能事后回查。
        对不上就返回空串，界面上显示「未知账号」，绝不编造。
        """
        logs = await self._call_logs()
        if not logs:
            return ""
        head = re.sub(r"\s+", " ", str(job.get("prompt") or "")).strip()[:48]
        if not head:
            # 没有指纹就无从对号：宁可显示「未知账号」，也不能随便认一个坑
            return ""
        floor, ceil = since - 5, time.time() + 120
        # 出图看 /v1/images，文案 / 润色看 /v1/chat，别把另一条链路的日志混进来。
        # 已知限制：号池的 /v1/chat/completions 日志只有 key_name / model，
        # 没有 account_email（实测 39 条全空），而 /v1/images/* 日志才有。
        # 因此文本任务无法归因到具体账号，界面保持「文案任务」占位，不编造邮箱。
        want = "/v1/images" if str(job.get("mode") or "image") == "image" else "/v1/chat"
        best_at, best_email = 0.0, ""
        for it in logs:
            det = it.get("detail") or {}
            if want not in str(det.get("endpoint") or ""):
                continue
            if head:
                body = re.sub(r"\s+", " ", str(det.get("request_text") or ""))
                if head not in body:
                    continue
            at = _parse_dt(det.get("started_at"))
            # 日志时间只到秒，且 clock 有微差；给 ±5 秒容差，同时排除半小时前的旧调用
            if at and (at < floor or at > ceil):
                continue
            if at and at < time.time() - CALL_LOG_WINDOW:
                continue
            if at >= best_at:
                best_at = at
                best_email = str(det.get("account_email") or "")
        return best_email

    def is_busy(self) -> bool:
        """现在有没有生图请求：有图在跑，或者还有排队的。

        账号状态只在这时候才会变，监控和后台同步据此决定刷多快。
        """
        return bool(self.running) or (self.queue.qsize() - self._retire_pending) > 0

    async def refresh_accounts_throttled(self, busy_interval: float = 3.0,
                                         idle_interval: float = 20.0) -> None:
        """监控页轮询：出图中才高频，空闲时放慢。

        以前固定 2 秒一次，监控开着的时候等于每秒半次去打号池；而号池空闲时
        那份列表根本不会变，纯属空转。现在没有生图请求就按 idle_interval 走。
        """
        gap = busy_interval if self.is_busy() else idle_interval
        if time.time() - self._acct_cache_at >= gap:
            await self.ensure_online()
            await self.refresh_accounts(force=True)

    async def _account_refresh_loop(self) -> None:
        """后台同步账号状态：出图时 10 秒一轮，空转时 5 分钟一轮就够。"""
        try:
            while True:
                await asyncio.sleep(10 if self.is_busy() else 300)
                await self.ensure_online(60.0)
                await self.refresh_accounts(force=True)
        except asyncio.CancelledError:
            raise

    def reset_statuses(self) -> None:
        self.endpoint["fails"] = 0
        self.endpoint["error"] = ""
        if self.endpoint["status"] in ("error", "unhealthy"):
            self.endpoint["status"] = "ready"

    async def check_all(self) -> dict:
        if self._check_task and not self._check_task.done():
            return self.check_state
        self.check_state = {"running": True, "total": 1, "done": 0,
                            "started": int(time.time()), "finished": 0, "items": {}}
        self._check_task = asyncio.create_task(self._check_runner())
        return self.check_state

    async def _check_runner(self) -> None:
        try:
            await self.refresh_env_ids()
            p = provider_cfg()
            key = p["api_key"]
            ok, msg = False, "未配置接口 API Key"
            if key:
                try:
                    async with httpx.AsyncClient(trust_env=False, timeout=15) as c:
                        r = await c.get(api_url(p, "models"),
                                        headers={"Authorization": "Bearer " + key})
                        ok = r.status_code == 200
                        msg = "正常" if ok else f"HTTP {r.status_code}"
                except Exception as e:
                    msg = mask(e)[:120]
            if self.pool_stat.get("mode") == "pool":
                detail = (f"{msg} · {self.pool_stat.get('total')} 个号 / "
                          f"配额 {self.pool_stat.get('quota_total')}")
            else:
                detail = f"{msg} · {p['base_url']}"
            self.check_state["items"][p["name"]] = {"ok": ok, "msg": detail}
            self.check_state["done"] = 1
            self.endpoint.update({"check_ok": ok, "check_msg": detail,
                                  "check_at": time.time()})
        finally:
            self.check_state["running"] = False
            self.check_state["finished"] = int(time.time())

    # ---------- public ops ----------
    def _single_endpoint(self) -> dict:
        """通用接口模式下的单端点伪账号，供监控表展示（无账号明细）。"""
        p = provider_cfg()
        online = bool(self.pool_stat.get("online"))
        ep = self.endpoint
        return {
            "id": 1, "masked": p["name"], "label": p["name"],
            "env_name": "api", "env_id": 0,
            "status": "ready" if online else "error",
            "last_used": int(ep.get("last_used") or 0),
            "uses": int(ep.get("uses") or 0), "fails": int(ep.get("fails") or 0),
            "error": "" if online else str(self.pool_stat.get("message") or ""),
            "last_ok": int(ep.get("last_ok") or 0), "busy_since": int(ep.get("busy_since") or 0),
            "cooldown": 0,
            "check_ok": ep.get("check_ok"), "check_msg": str(ep.get("check_msg") or ""),
            "check_at": int(ep.get("check_at") or 0),
            "hist_done": int(ep.get("uses") or 0), "hist_error": int(ep.get("fails") or 0),
            "hist_last_ok": int(ep.get("last_ok") or 0), "quota": None,
        }

    def _snapshot_accounts(self) -> list[dict]:
        mode = self.pool_stat.get("mode") or ("pool" if self.accounts else "single")
        if mode == "single":
            return [self._single_endpoint()]
        return [
            {
                "id": a["id"], "masked": a["masked"], "label": a["label"],
                "env_name": a["env_name"], "env_id": a["env_id"], "status": a["status"],
                "last_used": int(a["last_used"]), "uses": a["uses"], "fails": a["fails"],
                "error": a["error"], "last_ok": int(a["last_ok"]),
                "busy_since": int(a["busy_since"]), "cooldown": a["cooldown"],
                "check_ok": a["check_ok"], "check_msg": a["check_msg"],
                "check_at": int(a["check_at"]), "hist_done": a["hist_done"],
                "hist_error": a["hist_error"], "hist_last_ok": int(a["hist_last_ok"]),
                "quota": a.get("quota", 0),
                "inflight": a.get("inflight", 0),
            }
            for a in self.accounts
        ]

    def snapshot(self) -> dict:
        now = time.time()
        accounts = self._snapshot_accounts()
        # 限流号不能再算「可用」：它已经不参与轮转，算进去会让人以为 33 个号都能出图
        ready_n = sum(1 for a in accounts if a["status"] == "ready")
        limited_n = sum(1 for a in accounts if a["status"] == "limited")
        # 号池自己选号，工作台只知道「哪几个号 inflight>0」。数量对得上时按序对应，
        # 对不上就统一显示号池里正在出图的号，避免张冠李戴。
        busy_masks = [a["masked"] for a in accounts if a["status"] == "busy"]
        running = []
        for i, (k, v) in enumerate(self.running.items()):
            acc_name = v.get("account") or ""
            if busy_masks and len(busy_masks) == len(self.running):
                acc_name = busy_masks[i]
            elif busy_masks:
                acc_name = "号池 · " + "、".join(busy_masks[:2])
            running.append({"job_id": k, "account": acc_name, "env_id": v.get("env_id"),
                            "started": v.get("started", 0),
                            "elapsed": int(now - v.get("started", now))})
        current = running[0]["account"] if running else ""
        return {
            "total": len(accounts),
            "ready": ready_n,
            "limited": limited_n,
            "bound": len(accounts),
            "busy": len(self.running),
            # 「现在有活干」：正在出图或还有排队。别和 pool_stat 里的 active（可用账号数）搞混
            "running_now": self.is_busy(),
            "workers": self.workers,
            "workers_max": self.workers_cap(),
            "workers_default": WORKERS_DEFAULT,
            "valid_accounts": self.valid_accounts(),
            # 队列里混着「下班令牌」，排队数只算真任务
            "queued": max(0, self.queue.qsize() - self._retire_pending),
            "running": running,
            "current": current,
            "rr_index": self._rr_index,
            "rr_env": self.endpoint["env_name"],
            "rr_env_id": self.endpoint["env_id"],
            "accounts": accounts,
            # 今天哪个号失败过几次：失败会自动换号重试，但账要如实记着
            "fails_today": sum(int(v) for v in fails_today().values()),
            "fail_accounts": fails_summary()[:8],
            # 账号明细拉不到时给前端的原话（换接口后 admin-key 对不上就会走到这里）
            "accounts_error": str(self.pool_stat.get("accounts_error") or ""),
            # 真实号池 / 通用接口探测结果，插件面板和 /api/pool 据此展示
            "pool": dict(self.pool_stat),
        }

    async def submit(self, job: dict) -> str:
        self.jobs[job["id"]] = job
        await self.queue.put(job["id"])
        self._persist()
        return job["id"]

    async def cancel_all(self, reason: str = "已手动停止") -> dict:
        '''停止当前生图任务：清空排队、中断在途请求、阻止换号重试。

        已出图和已写入历史的任务不受影响；被停止的任务不写历史、不落盘，
        重启后也不会被 _restore 捞回。
        '''
        stopped: list[str] = []
        dropped = 0
        retire_lost = 0
        while True:
            try:
                jid = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if jid == RETIRE_TOKEN:
                # 下班令牌不是任务，别当洪水清掉：并行数下调的意图还得兑现
                retire_lost += 1
                self.queue.task_done()
                continue
            job = self.jobs.get(jid)
            if job and job.get("status") in ("queued", "running"):
                job.update({"status": "cancelled", "error": reason,
                            "will_retry": 0, "finished": int(time.time())})
                stopped.append(jid)
                dropped += 1
            self.queue.task_done()
        for _ in range(retire_lost):
            await self.queue.put(RETIRE_TOKEN)
        # 先改状态再 cancel：_run_job 的 finally 才不会写历史或安排重试
        running = []
        for jid, task in list(self._job_tasks.items()):
            job = self.jobs.get(jid)
            if job:
                job.update({"status": "cancelled", "error": reason, "will_retry": 0})
            running.append(jid)
            stopped.append(jid)
            if not task.done():
                task.cancel()
        # 换号重试等待中的任务没有执行体，也要一并停掉
        for jid, job in self.jobs.items():
            if job.get("status") in ("queued", "running") and jid not in stopped:
                job.update({"status": "cancelled", "error": reason,
                            "will_retry": 0, "finished": int(time.time())})
                stopped.append(jid)
        self.running.clear()
        self._job_tasks.clear()
        self._persist()
        log(f"手动停止生图：中断执行中 {len(running)} 个 / 丢弃排队 {dropped} 个")
        return {"ok": True, "stopped": len(stopped), "running": len(running),
                "queued": dropped,
                "message": f"已停止 {len(stopped)} 个任务（执行中 {len(running)}、排队 {dropped}）"}

    def _finish(self, job: dict) -> None:
        job.setdefault("finished", int(time.time()))
        if self.on_finish:
            try:
                self.on_finish(job)
            except Exception as e:
                log(f"history record failed: {mask(e)[:120]}")

    # ---------- workers ----------
    async def _worker(self, wid: int) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                if job_id == RETIRE_TOKEN:
                    # 并行上限被下调：把手上的活干完再走，不打断在途请求
                    self._retire_pending = max(0, self._retire_pending - 1)
                    log(f"worker {wid}: 退出（并行数下调）")
                    return
                job = self.jobs.get(job_id)
                if not job or job.get("status") == "cancelled":
                    continue
                acc = await self._claim()
                task = asyncio.create_task(self._run_job(job, acc, wid))
                self._job_tasks[job_id] = task
                try:
                    await task
                except asyncio.CancelledError:
                    # 单个任务被手动停止：worker 继续取下一条，不能让整个 worker 死掉
                    log(f"job {job_id}: 已手动停止")
                finally:
                    self._job_tasks.pop(job_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # worker 绝不能死
                log(f"worker {wid}: unexpected {mask(e)[:160]}")
            finally:
                self.queue.task_done()

    async def _claim(self, avoid_env=None) -> dict:
        """号池自己选号与轮转，这里只占一个本地槽位做状态展示。"""
        async with self._lock:
            acc = self.endpoint
            acc["busy_since"] = time.time()
            return acc

    async def _run_job(self, job: dict, acc: dict, wid: int) -> None:
        job_id = job["id"]
        # 状态栏里的「生成中 · X」先给按任务类型区分的占位，别用接口名冒充账号。
        # 出图任务成功后会由 _attribute_call 回查号池日志、替换成真实邮箱；
        # 文案任务走 chat/completions，号池这类日志不带 account_email，
        # 所以会一直停在「文案任务」，这是如实显示，不是走了出图接口。
        label = "出图任务" if job["mode"] == "image" else "文案任务"
        self.running[job_id] = {"account": label, "env_id": acc["env_id"],
                                "started": int(time.time())}
        job.update({"status": "running", "account": label, "env_id": acc["env_id"],
                    "last_env": acc["env_id"], "started": int(time.time()), "worker": wid})
        log(f"job {job_id}: start mode={job['mode']} worker={wid} "
            f"prompt_chars={len(job.get('prompt') or '')}")
        t_call = time.time()
        try:
            try:
                result = await self._execute(acc, job)
                if job.get("status") == "cancelled":
                    return
                job["result"] = result
                job["status"] = "done"
                acc["uses"] += 1
                acc["fails"] = 0
                acc["last_ok"] = time.time()
                acc["error"] = ""
                # 成功也回查一次账号：用户能看见「这次是谁出的图」，
                # 重试时也能直观看出确实换了号
                email = await self._attribute_call(job, t_call)
                if email:
                    job["account_email"] = email
                    job["account"] = mask_email(email)
                    job["result"]["account"] = mask_email(email)
                    job["result"]["account_email"] = email
                log(f"job {job_id}: done ({(job.get('result') or {}).get('bytes') or 'text'})"
                    + (f" by {mask_email(email)}" if email else "")
                    + (f" after {job['auto_retry']} retry" if job.get("auto_retry") else ""))
            except Exception as e:
                if job.get("status") == "cancelled":
                    return
                err = mask(f"{type(e).__name__}: {e}")[:300]
                acc["fails"] += 1
                acc["last_error_at"] = time.time()
                acc["error"] = err[:160]
                # 先查清楚是哪个号失败的，再排队重试（重新入队＝号池换下一个号）
                email = await self._attribute_call(job, t_call)
                attempt = {
                    "n": len(job.get("attempts") or []) + 1,
                    "account": mask_email(email) if email else "未知账号",
                    "account_email": email,
                    "error": err[:200],
                    "at": int(time.time()),
                }
                job.setdefault("attempts", []).append(attempt)
                if email:
                    attempt["count_today"] = record_account_fail(email, err)
                log(f"job {job_id}: attempt #{attempt['n']} failed on {attempt['account']}: "
                    f"{err[:120]}")
                self._schedule_retry(job, err)
        finally:
            job["finished"] = int(time.time())
            self.running.pop(job_id, None)
            acc["last_used"] = time.time()
            acc["busy_since"] = 0.0
            if job["status"] in ("done", "error"):
                self._finish(job)
            self._persist()

    def _schedule_retry(self, job: dict, err: str) -> None:
        if job.get("status") == "cancelled":
            return
        n = int(job.get("auto_retry") or 0)
        if n >= RETRY_MAX:
            job["status"] = "error"
            job["will_retry"] = 0
            # 失败原因里带上「换过哪几个号」，用户一眼能看出不是同一个号反复撞
            names = list(dict.fromkeys(
                a.get("account") for a in (job.get("attempts") or []) if a.get("account")))
            job["error"] = (f"{err}（已换 {len(names)} 个号重试 {n} 次仍未成功："
                            f"{'、'.join(names)}）") if names else err
            log(f"job {job['id']}: FAILED for good after {n} retries "
                f"on {len(names)} accounts: {err}")
            return
        delay = RETRY_DELAYS[min(n, len(RETRY_DELAYS) - 1)]
        job["auto_retry"] = n + 1
        job["will_retry"] = 1
        job["status"] = "queued"
        job["error"] = err
        job["retry_at"] = int(time.time() + delay)
        log(f"job {job['id']}: {err} -> retry #{n + 1} in {delay}s")
        self._persist()
        asyncio.create_task(self._requeue_later(job["id"], delay))

    async def _requeue_later(self, job_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            job = self.jobs.get(job_id)
            if not job or job.get("status") != "queued":
                return
            self.queue.put_nowait(job_id)
        except Exception as e:
            log(f"requeue {job_id}: {mask(e)[:120]}")

    # ---------- 一次任务：号池 API ----------
    async def _post_edits(self, c, p, auth, payload, ref_bytes):
        """参考图：先按配置方式试，auto 时先 json（号池）失败再退标准 multipart。"""
        if p["edits_style"] == "json":
            styles = ["json"]
        elif p["edits_style"] == "multipart":
            styles = ["multipart"]
        else:
            styles = ["json", "multipart"]
        last = None
        for st in styles:
            try:
                if st == "json":
                    body = dict(payload)
                    body.pop("response_format", None)
                    body["images"] = ["data:image/png;base64,"
                                      + base64.b64encode(ref_bytes).decode("ascii")]
                    r = await c.post(api_url(p, "images/edits"), json=body, headers=auth)
                else:
                    data = {"model": payload["model"], "prompt": payload["prompt"], "n": "1"}
                    if payload.get("size"):
                        data["size"] = payload["size"]
                    r = await c.post(api_url(p, "images/edits"), data=data,
                                     files={"image": ("ref.png", ref_bytes, "image/png")},
                                     headers={"Authorization": auth["Authorization"]})
                last = r
                if r.status_code < 400:
                    return r, "api:edits"
            except Exception:
                continue
        return last, "api:edits"

    def _dl_headers(self, url, p):
        """下载图片：仅当图片与接口同源时才带密钥，避免把 key 送给外部 CDN。"""
        if not url.lower().startswith("http"):
            return {}
        try:
            same = urlparse(url).netloc == urlparse(p["base_url"]).netloc
        except Exception:
            same = False
        return {"Authorization": "Bearer " + p["api_key"]} if same else {}

    async def _execute(self, acc: dict, job: dict) -> dict:
        p = provider_cfg()
        key = p["api_key"]
        if not key:
            raise RuntimeError("未配置接口 API Key，请打开「接口设置」填写 Base URL 与密钥")
        headers = {"Authorization": "Bearer " + key,
                   "Content-Type": "application/json"}
        timeout = IMAGE_TIMEOUT if job["mode"] == "image" else TEXT_TIMEOUT
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as c:
            if job["mode"] == "image":
                payload = {"model": p["model_image"], "prompt": job["prompt"],
                           "n": 1, "quality": "auto"}
                # 返回格式：显式配置优先；auto 时号池默认 b64，第三方交给服务端默认
                if p["image_format"] in ("b64_json", "url"):
                    payload["response_format"] = p["image_format"]
                elif p["is_pool"]:
                    payload["response_format"] = "b64_json"
                sz = size_for_ratio(job.get("ratio"))
                if sz:
                    payload["size"] = sz
                ref = job.get("ref_path") or ""
                ref_bytes = Path(ref).read_bytes() if (ref and Path(ref).is_file()) else b""
                r, via = None, "api:generations"
                if ref_bytes and p["supports_edits"] and p["edits_style"] != "off":
                    r, via = await self._post_edits(c, p, headers, payload, ref_bytes)
                    if r is None or r.status_code >= 400:
                        # 参考图走 edits 失败：不静默降级，明确告诉用户怎么改
                        if r is not None and r.status_code in (400, 404, 405, 415, 422, 501):
                            raise RuntimeError(
                                "当前接口不支持参考图（/v1/images/edits 不可用）。"
                                "请到「接口设置」关闭「支持参考图」，或换成支持 edits 的接口后重试。")
                        raise_for_response(r)
                else:
                    r = await c.post(api_url(p, "images/generations"), json=payload, headers=headers)
                    via = "api:generations"
                if r.status_code != 200:
                    raise_for_response(r)
                items = (_json_of(r).get("data") or [])
                if not items:
                    raise RuntimeError("接口返回成功但没有图片数据（确认该地址是 OpenAI 兼容的 images 接口）")
                item = items[0] or {}
                raw = b""
                if item.get("b64_json"):
                    raw = base64.b64decode(item["b64_json"])
                elif item.get("url"):
                    u = str(item["url"])
                    if u.startswith("data:"):
                        raw = base64.b64decode(u.split(",", 1)[1])
                    else:
                        rr = await c.get(u, headers=self._dl_headers(u, p))
                        if rr.status_code != 200:
                            raise RuntimeError(f"下载图片失败 HTTP {rr.status_code}")
                        raw = rr.content
                if not raw:
                    raise RuntimeError("图片数据为空")
                # 存到用户配置的保存目录（默认 images/），目录不可用时 out_dir 会退回默认
                save_to = out_dir()
                save_to.mkdir(parents=True, exist_ok=True)
                final = save_to / f"{job['id']}.png"
                with Image.open(io.BytesIO(raw)) as im:
                    w, h = im.size
                    im.convert("RGB").save(final, "PNG")
                return {"image": final.name, "bytes": final.stat().st_size,
                        "width": w, "height": h, "via": via,
                        "saved_to": str(save_to)}

            payload = {
                "model": p["model_text"],
                "messages": [{"role": "user", "content": job["prompt"]}],
                "stream": False,
                # 唯一 user 强制穿透号池 60 秒聊天缓存，保证每次真的去取号轮转
                "user": f"wb-{job['id']}-{int(time.time())}",
            }
            r = await c.post(api_url(p, "chat/completions"), json=payload, headers=headers)
            if r.status_code != 200:
                raise_for_response(r)
            data = _json_of(r)
            try:
                text = str(data["choices"][0]["message"]["content"] or "").strip()
            except Exception:
                text = str(data.get("text") or "").strip()
            text = _sanitize_text(text)
            if not text:
                raise RuntimeError("文案返回为空")
            return {"text": text}
