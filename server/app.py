"""Workbench backend: handraw-style catalogs + ChatGPT account pool as token source."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
import zipfile
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import access
import catalog
import composer
import imaging
import pool as pool_mod
import pool_admin
import pool_service
from starlette.concurrency import run_in_threadpool
from drafts import Drafts
from history import History
from pool import (PoolManager, load_config, provider_cfg, test_connection,
                  out_dir, image_path, norm_base_url,
                  CONFIG_FILE as POOL_CONFIG_FILE)

# 本地版：所有数据目录都在本文件所在目录，不再指向 VPS 的 /root/ai-pool-lab
LAB = Path(__file__).resolve().parent
REPO_IMAGES = LAB / "repo_images"
STATIC = LAB / "static"
OUT_DIR = LAB / "images"          # 默认保存目录；实际目录由 config 的 output_dir 决定
DEFAULT_APP_NAME = "Bluxe手绘工坊 · 生图工作台"   # 页头标题默认名，可被 config 的 app_name 覆盖
THUMB_DIR = LAB / "thumbs"
REFS_DIR = LAB / "refs"
# 出图完成后在后台生成压缩派生图，别堵住出图线程
VARIANT_EXEC = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wb-variant")
AUTH_FILE = LAB / ".auth"
for _d in (REPO_IMAGES, OUT_DIR, THUMB_DIR, REFS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

CATALOG = catalog.build()
POOL = PoolManager()
# 图片目录传函数：用户改「保存目录」后历史立刻按新目录找图，不用重启
HISTORY = History(LAB / "workbench" / "history.jsonl", out_dir, THUMB_DIR,
                  finder=image_path)
DRAFTS = Drafts(LAB / "workbench" / "drafts.json")
def _finish_with_replace(job: dict) -> None:
    """单张重做：新图成功后先删掉原任务里那一张旧记录，再写新记录。

    失败、没有图片、任务号对不上、id 格式不对，都保留旧图，避免把用户原来的图弄丢。
    """
    rid = str(job.get("replace_id") or "").strip()
    if (rid and re.fullmatch(r"[0-9a-z]{6,32}", rid)
            and job.get("status") == "done"
            and (job.get("result") or {}).get("image")):
        HISTORY.drop_record(rid, task_id=job.get("task_id"))
    HISTORY.record(job)
    # 图一落地就把 card/preview 两档备好，点开历史时零等待。
    # 派生图生成失败只影响预览速度，原图和历史记录不受影响。
    try:
        img = str((job.get("result") or {}).get("image") or "")
        jid = str(job.get("id") or "")
        src = image_path(img)
        if jid and img == f"{jid}.png" and src and src.is_file():
            VARIANT_EXEC.submit(imaging.ensure_all, THUMB_DIR, jid, src, job.get("ratio"))
    except Exception:
        pass


POOL.on_finish = _finish_with_replace
AUTH: tuple[str, str] | None = None
if AUTH_FILE.exists():
    u, _, p = AUTH_FILE.read_text(encoding="utf-8").strip().partition(":")
    if u and p:
        AUTH = (u, p)


@asynccontextmanager
async def lifespan(app: FastAPI):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_dir().mkdir(parents=True, exist_ok=True)
    # 号池由工作台自己拉起与守护，不依赖任何外部插件。
    # 它比后端晚几秒就绪，起来那一刻主动对一次账，页头/监控不会先显示「1 号」。
    loop = asyncio.get_running_loop()

    async def _sync_after_pool_start():
        # 全新机器首次启动时号池还没有任何用户 Key，工作台会 401。
        # 号池一起来就自动签发一把（幂等：已有可用 Key 时什么都不会新建）。
        try:
            await asyncio.sleep(1.0)
            k = await pool_admin.ensure_key()
            if k.get("ok") and k.get("created"):
                pool_mod.log("号池首次启动已自动签发用户 Key，工作台可直接出图")
        except Exception as e:
            pool_mod.log("自动签发号池用户 Key 失败：" + str(e)[:110])
        await POOL.refresh_env_ids()
        await POOL.refresh_accounts(force=True)

    def _on_pool_started():
        try:
            asyncio.run_coroutine_threadsafe(_sync_after_pool_start(), loop)
        except Exception:
            pass

    pool_service.ON_STARTED = _on_pool_started
    pool_service.start_watchdog()
    await POOL.start()
    yield
    await POOL.stop()


app = FastAPI(title="Handraw Workbench", lifespan=lifespan)
app.mount("/img", StaticFiles(directory=str(REPO_IMAGES)), name="img")
app.mount("/refs", StaticFiles(directory=str(REFS_DIR)), name="refs")


@app.get("/files/{name}")
async def files(name: str):
    """原图路由：按当前「保存目录」实时取文件。

    以前是 StaticFiles 固定挂到 images/，用户一改保存目录，改完得重启才生效，
    而且老图留在原目录就 404。现在每次请求现查，当前目录优先、默认目录兜底。
    """
    p = image_path(name)
    if not p:
        raise HTTPException(404, "image not found")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=604800"})

PUBLIC_ACCESS_PATHS = {
    "/access", "/api/access/request", "/api/access/verify", "/api/access/master",
}


def _client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf[:64]
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie("wb_sid", token, httponly=True, samesite="lax", secure=True,
                    max_age=access.SESSION_TTL, path="/")


class AccessRequest(BaseModel):
    email: str = ""
    user: str = ""          # 兼容浏览器缓存的旧页面


class AccessVerify(BaseModel):
    email: str = ""
    user: str = ""          # 兼容浏览器缓存的旧页面
    code: str = ""


class MasterRequest(BaseModel):
    password: str = ""


@app.middleware("http")
async def access_guard(request: Request, call_next):
    """旧的 ?k= 免密链接与 HTTP Basic 全部作废，只认服务端签发的随机 session token。

    页面路径 302 到申请页；/api/* 返回 401 JSON，避免前端 fetch 拿到一坨 HTML。
    /files /img /refs 三个图片挂载同样受保护，不对外裸奔。
    """
    path = request.url.path.rstrip("/") or "/"
    if not AUTH or path == "/health" or path in PUBLIC_ACCESS_PATHS:
        return await call_next(request)
    if access.check_session(request.cookies.get("wb_sid", "")):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "auth required", "redirect": "/access"}, status_code=401)
    return RedirectResponse("/access", status_code=302)


class GenRequest(BaseModel):
    mode: str = Field(default="image")
    topic: str = Field(min_length=1, max_length=2000)
    title: str | None = None
    body: str | None = None
    # 整篇正文（左侧「正文」框），只为历史「载入」还原，不参与拼提示词
    doc_body: str | None = Field(default=None, max_length=20000)
    # 这一批里的提交序号，0 起；并行出图完成顺序会乱，靠它还原清单顺序
    seq: int | None = None
    style_id: str | None = None
    layout_id: str | None = None
    color_id: str | None = None
    ratio: str = "3:4"
    note: str | None = None
    # 整组主题（左侧「标题」），落进历史方便回看
    group: str | None = None
    # 一次批量出图共用一个 task_id，历史按它分组
    task_id: str | None = None
    # 前端拼装器生成好的完整提示词；给了就直接用，不再走 build_image_prompt
    prompt: str | None = Field(default=None, max_length=8000)
    # 参考图（/api/ref 上传拿到的 id），给了就随提示词一起传给 ChatGPT
    ref_id: str | None = Field(default=None, max_length=32)
    # 出图模式与拍摄类型，只用于历史回看/载入还原，不参与拼提示词（提示词前端已拼好）
    image_mode: str | None = Field(default=None, max_length=16)
    shoot_type: str | None = Field(default=None, max_length=40)
    # 单张重做：新图成功后删掉这条旧记录，实现同一任务内原位替换
    replace_id: str | None = Field(default=None, max_length=32)


class ZipRequest(BaseModel):
    # 当前批次的 job id（展示顺序），服务端按这个顺序命名 01 02 03…
    ids: list[str] = Field(default_factory=list, max_length=100)


class RefRequest(BaseModel):
    # 前端已经压到 1400px 内，这里只做落盘；dataURL 或裸 base64 都收
    data: str = Field(max_length=24_000_000)
    name: str | None = Field(default=None, max_length=120)


class ComposeItem(BaseModel):
    title: str = ""
    body: str = ""
    scene: str = ""


class ComposeRequest(BaseModel):
    style_id: str | None = None
    layout_id: str | None = None
    color_id: str | None = None
    ratio: str = "3:4"
    mood: str | None = None
    fusion: bool = False
    whitespace: str = "normal"
    note: str | None = None
    topic: str | None = None
    ref: bool = False
    # 出图模式：graphic-text（默认，图文）| photo（摄影）；摄影可带拍摄类型
    image_mode: str | None = None
    shoot_type: str | None = None
    # 手改过的出图指令：给了就用它，不再自动拼；allow_blank：允许空标题/正文纯指令出图
    instruction: str | None = None
    allow_blank: bool = False
    items: list[ComposeItem] = Field(default_factory=list)


def _find(items: list[dict], item_id: str | None) -> dict | None:
    if not item_id:
        return None
    return next((x for x in items if x["id"] == item_id), None)


def build_image_prompt(req: GenRequest) -> str:
    if req.prompt and req.prompt.strip():
        return req.prompt.strip()
    style = _find(CATALOG["styles"], req.style_id)
    layout = _find(CATALOG["layouts"], req.layout_id)
    color = _find(CATALOG["colors"], req.color_id)
    parts: list[str] = []
    if req.ref_id:
        parts.append(composer.REF_FUSION)
    if layout:
        parts.append(f"排版图型：{layout['prompt']}")
    if color:
        parts.append(color["prompt"])
    if style:
        parts.append(f"画风：{style['name_en']}。核心视觉特征：{style['features']}")
    parts.append(f"主题：{req.topic.strip()}")
    # 2.35:1 是微信公众号首图（900×383），不是小红书图文的口径，单独说清楚
    if (req.ratio or "").strip() == "2.35:1":
        parts.append("画面比例：2.35:1 公众号首图横幅，主体与标题压在居中横向安全区内，"
                     "上下各留一段可裁的次要背景。")
    else:
        parts.append(f"画面比例：{req.ratio}，适合小红书图文排版。")
    if req.note:
        parts.append(f"补充要求：{req.note.strip()}")
    parts.append("请立即生成图片，不要反问，不要输出任何解释文字。")
    return "\n".join(parts)


_LIST_INTENT_RE = re.compile(
    r"(?:\d+\s*(?:条|张|个|则)\s*(?:适合)?(?:生成)?(?:贴图|分镜|卡片|清单|文案))"
    r"|(?:贴图|分镜|卡片|清单|文案)[^\n]{0,12}\d+\s*条"
    r"|01\s*[｜|]"
)


def build_text_prompt(req: GenRequest) -> str:
    """普通选题走小红书笔记模板；选题明确要「N 条贴图/分镜文案」或给了 01｜ 示例时，
    改走编号清单模板，避免 AI 输出「开场导语 + 编号条目 + 结尾标签」的混合格式
    （那种格式前端拆分只能识别成 1 条）。"""
    topic = req.topic.strip()
    if _LIST_INTENT_RE.search(topic):
        m = re.search(r"(\d+)\s*条", topic)
        n = min(max(int(m.group(1)) if m else 8, 2), 20)
        return (
            f"你是贴图分镜文案写手。围绕选题「{topic}」，直接输出成品，不要任何解释。\n"
            f"第一行：整组标题（20字以内，带情绪钩子，可用一个emoji）。\n"
            f"标题之后空一行，然后严格输出 {n} 条贴图文案，编号从 01 连续到 {n:02d}。\n"
            "每一条占两行：第一行是两位编号、一个竖线、再加小标题（小标题 8 字以内），"
            "第二行是这一条的正文（60到100字，口语化，写具体场景和细节，不要空话）。\n"
            f"条与条之间用一个空行隔开，全文一共 {n} 个编号行。\n"
            "硬性要求：不要开场导语、不要结尾总结、不要话题标签、不要 markdown 标记（不要 ** 和 #）、"
            "不要写「以下是」「最后想告诉你」这类客套话；正文里不要出现新的编号行，"
            "也不要把上面这些格式说明当成内容输出。"
        )
    return (
        f"你是小红书爆款笔记写手。围绕选题「{req.topic.strip()}」，直接输出成品，不要任何解释：\n"
        "第一行：标题（20字以内，带情绪钩子，可用一个emoji）\n"
        "空行后：正文 300-500 字，口语化、分段、适量emoji，有具体细节不要空话\n"
        "空行后：5 个 #话题标签\n"
        "不要写“以下是”“希望对你有帮助”这类客套话。"
    )


def build_polish_prompt(req: GenRequest) -> str:
    """Polish the user's edited title/body in place (no new facts)."""
    title = (req.title or "").strip()
    body = (req.body or "").strip()[:4000]
    return (
        "你是小红书文案编辑。请润色下面这篇笔记：保持原意和事实，不换主题，"
        "提升口语感、细节密度和情绪钩子，删掉空话。直接输出成品，不要任何解释：\n"
        "第一行：润色后的标题（20字以内，可用一个emoji）\n"
        "空行后：润色后的正文（保留分段，300-500字，适量emoji）\n"
        "空行后：5 个 #话题标签\n"
        f"原标题：{title or '（无）'}\n"
        f"原正文：\n{body or '（空，请围绕标题补写一篇完整笔记）'}"
    )


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/catalog")
async def api_catalog():
    return JSONResponse(CATALOG)


@app.get("/api/pool")
async def api_pool():
    # 前端每 10 秒轮询一次，顺手在这里做离线自愈：号池后起也能自己从「离线」翻成在线
    await POOL.ensure_online()
    return JSONResponse(POOL.snapshot())


@app.post("/api/pool/refresh")
async def api_pool_refresh():
    await POOL.refresh_env_ids()
    return JSONResponse(POOL.snapshot())


@app.get("/api/pool/fails")
async def api_pool_fails(days: int = 7):
    """账号失败账本：今天每个号失败几次、最近几天各失败几次。

    失败不会让任务中断（自动换号重试），但账必须留痕，用户要能核对。
    """
    days = max(1, min(30, int(days or 7)))
    try:
        ledger = json.loads(pool_mod.FAIL_LEDGER.read_text(encoding="utf-8"))
    except Exception:
        ledger = {}
    all_days = ledger.get("days") or {}
    last = ledger.get("last") or {}
    order = sorted(all_days.keys(), reverse=True)[:days]
    rows = []
    for email, n in sorted((all_days.get(time.strftime("%Y-%m-%d")) or {}).items(),
                           key=lambda kv: -int(kv[1])):
        info = last.get(email) or {}
        rows.append({"account": pool_mod.mask_email(email) if "@" in email else "未知账号",
                     "account_email": email, "today": int(n),
                     "error": str(info.get("error") or ""), "at": int(info.get("at") or 0)})
    return JSONResponse({
        "today": time.strftime("%Y-%m-%d"),
        "items": rows,
        "total_today": sum(int(v) for v in (all_days.get(time.strftime("%Y-%m-%d")) or {}).values()),
        "recent": [{"day": d, "total": sum(int(v) for v in (all_days.get(d) or {}).values())}
                   for d in order],
    })


@app.post("/api/pool/reset")
async def api_pool_reset():
    POOL.reset_statuses()
    await POOL.refresh_env_ids()
    return JSONResponse(POOL.snapshot())


# ---------- 号池管理（「接口与号池」面板里用） ----------
@app.get("/api/pool/panel")
async def api_pool_panel():
    return JSONResponse(await pool_admin.panel())


@app.post("/api/pool/accounts/import")
async def api_pool_import(body: dict):
    res = await pool_admin.import_accounts(str(body.get("text") or ""))
    if res.get("ok"):
        # 新号立刻进轮转：页头和监控不用等下一轮后台同步
        await POOL.refresh_accounts(force=True)
    pool_mod.log(f"号池导入：{res.get('added', 0)} 个新增 / {res.get('skipped', 0)} 个已存在")
    return JSONResponse(res)


@app.post("/api/pool/accounts/delete")
async def api_pool_delete(body: dict):
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "没选中账号")
    res = await pool_admin.delete_accounts([str(i) for i in ids])
    if res.get("ok"):
        await POOL.refresh_accounts(force=True)
    return JSONResponse(res)


@app.post("/api/pool/accounts/status")
async def api_pool_account_status(body: dict):
    ids = body.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "没选中账号")
    res = await pool_admin.set_status([str(i) for i in ids], bool(body.get("disabled")))
    if res.get("ok"):
        await POOL.refresh_accounts(force=True)
    return JSONResponse(res)


@app.post("/api/pool/key/ensure")
async def api_pool_key(body: dict | None = None):
    """取（或新建）一把号池用户 Key，供本页复制出去接别的工具。"""
    body = body or {}
    return JSONResponse(await pool_admin.ensure_key(force_new=bool(body.get("new"))))


# ---------- 工作台自带号池：进程生命周期 ----------
# 这些路由只在「当前接口指向本机号池」时生效；第三方接口与写作台 38650 一律不动。
# 启动 / 停止会等几十秒（首次还要复制运行时），全部丢到线程池，别堵住事件循环。
@app.get("/api/pool/service")
async def api_pool_service():
    return JSONResponse(await run_in_threadpool(pool_service.status))


@app.post("/api/pool/service/start")
async def api_pool_service_start(body: dict | None = None):
    body = body or {}
    res = await run_in_threadpool(pool_service.start, bool(body.get("force")))
    if res.get("ok"):
        await POOL.refresh_accounts(force=True)
    return JSONResponse(res)


@app.post("/api/pool/service/stop")
async def api_pool_service_stop():
    return JSONResponse(await run_in_threadpool(pool_service.stop))


@app.post("/api/pool/service/restart")
async def api_pool_service_restart():
    res = await run_in_threadpool(pool_service.restart)
    if res.get("ok"):
        await POOL.refresh_accounts(force=True)
    return JSONResponse(res)


@app.post("/api/pool/service/auto")
async def api_pool_service_auto(body: dict):
    home = pool_service.view()["home"]
    return JSONResponse(pool_service.set_auto_start(home, bool(body.get("enabled"))))


# ---------- 接口设置（通用 OpenAI 兼容接口） ----------
def _cfg_view() -> dict:
    """当前接口配置的脱敏视图，绝不回传完整 API Key。"""
    p = provider_cfg()
    pool = (POOL.snapshot().get("pool") or {})
    key = p["api_key"]
    masked = ""
    if key:
        masked = (key[:6] + "***" + key[-4:]) if len(key) > 12 else "***"
    return {
        "app_name": (load_config().get("app_name") or "").strip() or DEFAULT_APP_NAME,
        "app_name_default": DEFAULT_APP_NAME,
        "provider_name": p["name"],
        "base_url": p["base_url"],
        "output_dir": str(out_dir()),
        "output_dir_default": str(OUT_DIR),
        "output_dir_is_default": str(out_dir()) == str(OUT_DIR),
        "model_image": p["model_image"],
        "model_text": p["model_text"],
        "supports_edits": p["supports_edits"],
        "edits_style": p["edits_style"],
        "image_response_format": p["image_format"],
        "monitor_mode": p["monitor_mode"],
        "api_key_set": bool(key),
        "api_key_masked": masked,
        "is_pool": p["is_pool"],
        "mode": pool.get("mode") or ("pool" if p["is_pool"] else "single"),
        "pool_message": pool.get("message") or "",
        "port": load_config().get("port", 8092),
    }


@app.get("/api/config")
async def api_config_get():
    return JSONResponse(_cfg_view())


@app.post("/api/config")
async def api_config_save(body: dict):
    cfg: dict = {}
    if POOL_CONFIG_FILE.exists():
        try:
            cfg = json.loads(POOL_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    def put(k: str, v) -> None:
        cfg[k] = v

    if "app_name" in body:
        # 只影响显示名：留空 = 回到内置默认名
        put("app_name", str(body["app_name"] or "").strip()[:40])
    if "provider_name" in body:
        put("provider_name", str(body["provider_name"]).strip() or "生图接口")
    if "base_url" in body:
        b = norm_base_url(str(body["base_url"]))
        put("api_base_url", b)
        put("pool_base_url", b)   # 兼容旧字段
    if "pool_dir" in body:
        # 号池数据目录：决定 admin-key / 用户 Key 从哪读。只接受真实存在的目录
        raw = str(body["pool_dir"] or "").strip()
        if raw and Path(raw).is_dir():
            put("pool_dir", str(Path(raw)))
    if "output_dir" in body:
        raw = str(body["output_dir"] or "").strip()
        old = str(cfg.get("output_dir") or "")
        if raw:
            d = Path(os.path.expandvars(os.path.expanduser(raw)))
            if not d.is_absolute():
                d = LAB / d
            try:
                d.mkdir(parents=True, exist_ok=True)
                probe = d / ".wb_write_test"
                probe.write_bytes(b"ok")
                probe.unlink()
            except Exception as e:
                raise HTTPException(400, f"保存目录不可用：{d}（{e}）")
            put("output_dir", str(d))
        else:
            put("output_dir", "")     # 留空 = 回到默认 images/
        # 记下用过的旧目录：换走之后，老图在历史里还得打得开
        seen = [str(s) for s in (cfg.get("output_dirs_seen") or []) if str(s).strip()]
        if old and Path(old) != OUT_DIR and old not in seen:
            seen.append(old)
        new = str(cfg.get("output_dir") or "")
        if new and Path(new) != OUT_DIR and new not in seen:
            seen.append(new)
        put("output_dirs_seen", seen[-8:])
    if body.get("api_key"):       # 留空表示不改，保留原密钥
        put("api_key", str(body["api_key"]).strip())
    if "model_image" in body:
        put("model_image", str(body["model_image"]).strip() or "gpt-image-2")
    if "model_text" in body:
        put("model_text", str(body["model_text"]).strip() or "auto")
    if "supports_edits" in body:
        put("supports_edits", bool(body["supports_edits"]))
    if "edits_style" in body:
        put("edits_style", str(body["edits_style"]).strip() or "auto")
    if "image_response_format" in body:
        put("image_response_format", str(body["image_response_format"]).strip() or "auto")
    if "monitor_mode" in body:
        put("monitor_mode", str(body["monitor_mode"]).strip() or "auto")
    cfg.setdefault("port", 8092)
    POOL_CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 立即重探，让前端保存后马上看到新状态
    await POOL.refresh_env_ids()
    await POOL.refresh_accounts(force=True)
    return JSONResponse(_cfg_view())


@app.post("/api/config/test")
async def api_config_test(body: dict):
    """按提交的临时配置试连（也可空 body 用当前配置），不落盘。"""
    return JSONResponse(await test_connection(body or {}))


@app.post("/api/config/open-dir")
async def api_config_open_dir(request: Request):
    """在运行本服务的电脑上打开保存目录。

    只有本机浏览器能用（远端访问时打开的是服务器上的资源管理器，没有意义），
    远端就直接把路径返回去，让用户自己复制。
    """
    d = out_dir()
    if _client_ip(request) not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"ok": False, "path": str(d),
                            "message": "只有本机访问时能直接打开目录，请复制路径到资源管理器"})
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(d))                       # noqa: S606 - 本机打开文件夹
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
        return JSONResponse({"ok": True, "path": str(d)})
    except Exception as e:
        return JSONResponse({"ok": False, "path": str(d), "message": f"打开失败：{e}"})


@app.post("/api/compose")
async def api_compose(req: ComposeRequest):
    """把风格 + 主题色 + 图型 + 融合 + 每条文字，拼成出图指令与完整提示词。"""
    payload = {
        "style_id": req.style_id,
        "layout_id": req.layout_id,
        "color_id": req.color_id,
        "ratio": req.ratio,
        "mood": req.mood,
        "fusion": req.fusion,
        "whitespace": req.whitespace,
        "note": req.note,
        "topic": req.topic,
        "ref": req.ref,
        "image_mode": req.image_mode,
        "shoot_type": req.shoot_type,
        "instruction": req.instruction,
        "allow_blank": req.allow_blank,
        "items": [{"title": it.title, "body": it.body, "scene": it.scene} for it in req.items],
    }
    return JSONResponse(composer.compose(payload, CATALOG["styles"], CATALOG["layouts"], CATALOG["colors"]))


@app.post("/api/preview")
async def api_preview(req: GenRequest):
    if req.mode == "text":
        prompt = build_text_prompt(req)
    elif req.mode == "polish":
        prompt = build_polish_prompt(req)
    else:
        prompt = build_image_prompt(req)
    return {"prompt": prompt}


@app.post("/api/generate")
async def api_generate(req: GenRequest):
    rid = (req.replace_id or "").strip()
    if rid and not re.fullmatch(r"[0-9a-z]{6,32}", rid):
        raise HTTPException(400, "bad replace_id")
    if req.mode not in ("text", "image", "polish"):
        raise HTTPException(400, "mode must be text, image or polish")
    if not POOL.accounts:
        raise HTTPException(400, "pool is empty")
    if req.mode == "text":
        prompt = build_text_prompt(req)
    elif req.mode == "polish":
        prompt = build_polish_prompt(req)
    else:
        prompt = build_image_prompt(req)
    ref_path = ""
    if req.mode == "image" and req.ref_id:
        rid = req.ref_id.strip()
        # 只认我们自己生成的 hex id，避免路径穿越
        if not re.fullmatch(r"[0-9a-f]{4,32}", rid):
            raise HTTPException(400, "bad ref_id")
        p = REFS_DIR / f"{rid}.png"
        if not p.is_file():
            raise HTTPException(404, "reference image not found")
        ref_path = str(p)
    job = {
        "id": uuid.uuid4().hex[:12],
        "task_id": (req.task_id or "").strip()[:40] or uuid.uuid4().hex[:10],
        "mode": req.mode,
        "topic": req.topic.strip(),
        "title": (req.title or req.topic).strip(),
        "body": (req.body or "").strip(),
        "doc_body": (req.doc_body or "").strip(),
        "seq": req.seq,
        "group": (req.group or "").strip(),
        "style_id": req.style_id,
        "layout_id": req.layout_id,
        "color_id": req.color_id,
        "ratio": req.ratio,
        "ref_id": (req.ref_id or "") if ref_path else "",
        "ref_path": ref_path,
        "image_mode": (req.image_mode or "graphic-text"),
        "shoot_type": (req.shoot_type or ""),
        "replace_id": rid,
        "prompt": prompt,
        "status": "queued",
        "created": int(time.time()),
        "account": "",
    }
    await POOL.submit(job)
    return {"job_id": job["id"], "status": job["status"]}


@app.post("/api/generate/stop")
async def api_generate_stop():
    '''停止当前生图任务：中断在途请求、清空排队、阻止换号重试。'''
    return JSONResponse(await POOL.cancel_all())


@app.post("/api/zip")
async def api_zip(req: ZipRequest):
    """把一批 job 的图片按展示顺序打成 zip；重启后从历史回退，仍是原图。"""
    import io
    import zipfile

    buf = io.BytesIO()
    used: set[str] = set()
    packed = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for idx, jid in enumerate(req.ids, 1):
            if not re.fullmatch(r"[0-9a-f]{4,32}", jid or ""):
                continue
            # 服务重启后 POOL.jobs 会清空，但历史里还记着图片文件名，退回历史照打不误
            job = POOL.jobs.get(jid)
            rec = HISTORY.get(jid) if not job else None
            img = ((job.get("result") or {}).get("image") if job else None) or (rec or {}).get("image")
            if not img:
                continue
            p = image_path(img)
            if not p:
                continue
            title = str(((job.get("title") or job.get("topic")) if job else
                         ((rec or {}).get("title") or (rec or {}).get("topic"))) or "").strip()
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title)[:40].strip("_") or "card"
            name = f"{idx:02d}-{safe}{p.suffix or '.png'}"
            k = 2
            while name in used:
                name = f"{idx:02d}-{safe}-{k}{p.suffix or '.png'}"
                k += 1
            used.add(name)
            ratio = (job.get("ratio") if job else (rec or {}).get("ratio"))
            z.writestr(name, imaging.export_bytes(THUMB_DIR, jid, p, ratio))
            packed += 1
    if not packed:
        raise HTTPException(404, "no downloadable images in this batch")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="task-images.zip"'},
    )


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    """查不到内存 job 时退回历史记录，服务重启后轮询与预览 URL 照样可用。"""
    job = POOL.jobs.get(job_id)
    if not job:
        rec = HISTORY.get(job_id)
        if not rec:
            raise HTTPException(404, "job not found")
        img = str(rec.get("image") or "")
        job = {
            "id": job_id, "status": rec.get("status") or "done",
            "topic": rec.get("topic") or "", "title": rec.get("title") or "",
            "account": rec.get("account") or "", "created": rec.get("created"),
            "finished": rec.get("finished"), "error": rec.get("error") or "",
            "ratio": rec.get("ratio"),
            # 失败换号的轨迹要能跨重启还在，否则刷新一下就看不出是谁失败了
            "attempts": rec.get("attempts") or [],
            "account_email": rec.get("account_email") or "",
            "result": ({"image": img, "width": rec.get("width"),
                        "height": rec.get("height"), "bytes": rec.get("bytes")}
                       if img else {}),
        }
    payload = dict(job)
    result = payload.get("result") or {}
    if result.get("image"):
        raw = f"/files/{result['image']}"
        payload["raw_url"] = raw
        # 2.35:1 公众号首图：轮询拿到的就是裁好的成品，前端各处不用再自己判断比例
        payload["image_url"] = (f"/api/history/{job_id}/final"
                                if imaging.needs_crop(payload.get("ratio")) else raw)
        payload["preview_url"] = f"/api/history/{job_id}/preview"
    return JSONResponse(payload)


@app.get("/api/recent")
async def api_recent():
    jobs = sorted(POOL.jobs.values(), key=lambda j: j["created"], reverse=True)[:20]
    out = []
    for j in jobs:
        result = j.get("result") or {}
        img = result.get("image")
        out.append({
            "id": j["id"], "mode": j["mode"], "status": j["status"], "topic": j["topic"],
            "account": j.get("account", ""), "created": j["created"],
            "image_url": ((f"/api/history/{j['id']}/final" if imaging.needs_crop(j.get("ratio"))
                           else f"/files/{img}") if img else None),
            "error": j.get("error", ""),
        })
    return JSONResponse(out)


@app.get("/api/history")
async def api_history(limit: int = 24, offset: int = 0, q: str = "", status: str = ""):
    return JSONResponse(HISTORY.list(limit=limit, offset=offset, q=q, status=status))


@app.get("/api/tasks")
async def api_tasks(limit: int = 20, offset: int = 0, q: str = ""):
    """历史按「一次批量出图」分组，默认一页 20 个任务。"""
    return JSONResponse(HISTORY.tasks(limit=limit, offset=offset, q=q))


@app.get("/api/tasks/{task_id}")
async def api_task_detail(task_id: str):
    """载入历史任务：带提示词全文 + 当时选的图型/风格/主题色/比例。"""
    d = HISTORY.task_detail(task_id)
    if not d:
        raise HTTPException(404, "task not found")
    return JSONResponse(d)


def _plain_text(s: str) -> str:
    """把可能的 markdown 痕迹洗成纯文本：去代码围栏、# 标题、加粗、列表符、引用、链接壳。"""
    s = str(s or "")
    s = re.sub(r"```+[a-zA-Z0-9_-]*", "", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.S)
    s = re.sub(r"__(.+?)__", r"\1", s, flags=re.S)
    s = re.sub(r"^\s*[-*+·]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"`([^`]*)`", r"\1", s)
    return s.strip()


@app.get("/api/tasks/{task_id}/bundle")
async def api_task_bundle(task_id: str):
    """整任务打包下载：所有原图 + 文案.txt（标题 + 正文，纯文本不带 markdown 标记）。"""
    data = HISTORY.bundle_data(task_id)
    if not data:
        raise HTTPException(404, "task not found")
    title = str(data["title"] or "未命名任务")

    def safename(s, maxlen: int = 60) -> str:
        return "".join(ch for ch in str(s or "") if ch not in '\\/:*?"<>|\n\r\t').strip()[:maxlen]

    buf = io.BytesIO()
    n_img = 0
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        body = _plain_text(data["doc_body"])
        if not body:
            # 老记录没有 doc_body：退回每条的 标题+正文
            parts = []
            for i, r in enumerate(data["rows"], 1):
                t = (r.get("title") or r.get("topic") or "").strip()
                b = _plain_text(r.get("body") or "")
                if t or b:
                    parts.append((t or f"第{i:02d}条") + (("\n" + b) if b else ""))
            body = "\n\n".join(parts)
        z.writestr("文案.txt", (title + "\n\n" + body).strip() + "\n")
        for r in data["rows"]:
            if not r.get("image"):
                continue
            p = HISTORY.image_path(str(r["id"]))
            if not p:
                continue
            n_img += 1
            stem = safename(r.get("title") or r.get("topic") or "") or f"图片{n_img:02d}"
            base = f"{n_img:02d}_{stem}"
            name, j = base, 2
            while name in used:
                name = f"{base}_{j}"
                j += 1
            used.add(name)
            z.write(str(p), f"images/{name}.png")
    fname = (safename(title, 40) or task_id) + "_图片文案.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(fname)},
    )


@app.delete("/api/tasks/{task_id}")
async def api_task_delete(task_id: str):
    """删掉整个任务（索引 + 原图 + 缩略图），删完不能恢复。"""
    r = HISTORY.delete_task(task_id)
    if not r.get("deleted"):
        raise HTTPException(404, "task not found")
    return JSONResponse(r)


@app.get("/api/drafts")
async def api_drafts():
    return JSONResponse(DRAFTS.list())


@app.post("/api/drafts")
async def api_draft_save(body: dict):
    rec = DRAFTS.save(body)
    return JSONResponse({"ok": True, "id": rec["id"], "updated": rec.get("updated"),
                         "count": rec.get("count", 0)})


@app.get("/api/drafts/{draft_id}")
async def api_draft_get(draft_id: str):
    rec = DRAFTS.get(draft_id)
    if not rec:
        raise HTTPException(404, "draft not found")
    return JSONResponse(rec)


@app.delete("/api/drafts/{draft_id}")
async def api_draft_del(draft_id: str):
    if not DRAFTS.delete(draft_id):
        raise HTTPException(404, "draft not found")
    return {"ok": True}


@app.patch("/api/drafts/{draft_id}")
async def api_draft_rename(draft_id: str, body: dict):
    rec = DRAFTS.rename(draft_id, body.get("name") or "")
    if rec is None:
        raise HTTPException(404, "draft not found")
    return {"ok": True, "id": draft_id, "name": rec.get("name") or ""}


@app.post("/api/ref")
async def api_ref_upload(req: RefRequest):
    """参考图上传：前端压到 1400px 的 dataURL，这里只做落盘（PNG）。"""
    raw = (req.data or "").strip()
    if raw.lower().startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        blob = base64.b64decode(raw, validate=False)
    except Exception:
        raise HTTPException(400, "bad image data")
    if not blob or len(blob) > 12 * 1024 * 1024:
        raise HTTPException(400, "reference image too large (max 12MB)")

    import io

    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(blob))
        im = ImageOps.exif_transpose(im)
        im.load()
    except Exception:
        raise HTTPException(400, "not a readable image")
    if max(im.size) > 1400:
        im.thumbnail((1400, 1400))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")

    rid = uuid.uuid4().hex[:10]
    dst = REFS_DIR / f"{rid}.png"
    im.save(dst, "PNG", optimize=True)
    # 只留最近 120 张，老的自生自灭
    try:
        olds = sorted(REFS_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)[120:]
        for f in olds:
            f.unlink()
    except Exception:
        pass
    return JSONResponse({"id": rid, "url": f"/refs/{rid}.png",
                         "w": im.width, "h": im.height, "bytes": dst.stat().st_size})


@app.get("/api/history/{job_id}")
async def api_history_one(job_id: str):
    """单条完整记录（含提示词全文），历史里的「复用」用它。"""
    rec = HISTORY.get(job_id)
    if not rec:
        raise HTTPException(404, "record not found")
    return JSONResponse(HISTORY._public(rec))


def _variant_response(job_id: str, kind: str):
    """派生图统一出口。原图永远只在下载与大图弹窗里出现。

    公众号首图 2.35:1 是裁出来的：卡片和右侧预览都按记录里的比例裁，
    否则预览里看到的构图和发出去的不是一张。
    """
    src = HISTORY.image_path(job_id)
    if not src:
        raise HTTPException(404, "image not found")
    rec = HISTORY.get(job_id) or {}
    ratio = rec.get("ratio")
    try:
        # Pillow 解码 3MB PNG 要几百毫秒，整段放线程池，别堵住 event loop
        dst = imaging.ensure(THUMB_DIR, job_id, kind, src, ratio)
        return FileResponse(str(dst), media_type="image/webp",
                            headers={"Cache-Control": "private, max-age=604800"})
    except Exception:
        return FileResponse(str(src), media_type="image/png")


@app.get("/api/history/{job_id}/thumb")
async def api_history_thumb(job_id: str):
    """历史卡片小图：360px WebP 约 40KB；原图仍在 /files 供下载。"""
    return await run_in_threadpool(_variant_response, job_id, "card")


@app.get("/api/history/{job_id}/preview")
async def api_history_preview(job_id: str):
    """右侧排版预览：960px WebP 约 280KB；双击大图与所有下载都走原图。"""
    return await run_in_threadpool(_variant_response, job_id, "preview")


@app.get("/api/history/{job_id}/download")
async def api_history_download(job_id: str):
    path = HISTORY.image_path(job_id)
    if not path:
        raise HTTPException(404, "image not found")
    rec = HISTORY.get(job_id) or {}
    title = str(rec.get("title") or rec.get("topic") or job_id)
    name = "".join(ch for ch in title if ch not in '\\/:*?"<>|\n\r').strip() or job_id
    name = f"{name[:40]}.png"
    # 2.35:1 这类上游没有的尺寸，导出的就是裁好的成品；原图文件保持完整不覆盖
    dst = await run_in_threadpool(imaging.export_path, THUMB_DIR, job_id, path, rec.get("ratio"))
    return FileResponse(str(dst), media_type="image/png",
                        headers={"Content-Disposition":
                                 f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get("/api/history/{job_id}/final")
async def api_history_final(job_id: str):
    """成品图（inline）：公众号首图这类需要裁切的比例，大图弹窗和下载看到的是同一张。"""
    path = HISTORY.image_path(job_id)
    if not path:
        raise HTTPException(404, "image not found")
    rec = HISTORY.get(job_id) or {}
    dst = await run_in_threadpool(imaging.export_path, THUMB_DIR, job_id, path, rec.get("ratio"))
    return FileResponse(str(dst), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=604800"})


@app.get("/api/monitor")
async def api_monitor():
    # 号池占用状态只在出图时才变：有任务在跑 3 秒一刷，空闲时拉长到 20 秒，
    # 免得监控页开着就在后台空转打号池
    await POOL.refresh_accounts_throttled()
    snap = POOL.snapshot()
    by_env, by_mask = HISTORY.account_stats()
    for a in snap["accounts"]:
        st = (by_env.get(str(a["env_id"])) if a.get("env_id") else None) or by_mask.get(a["masked"]) or {}
        if st:  # 仅当工作台自身有该账号的历史记录时才覆盖，否则保留号池提供的成功/失败/最近成功
            a["hist_done"] = int(st.get("done") or 0)
            a["hist_error"] = int(st.get("error") or 0)
            a["hist_last_ok"] = int(st.get("last_ok") or 0)
    snap["history"] = HISTORY.stats()
    snap["check"] = POOL.check_state
    snap["recent_errors"] = HISTORY.recent_errors(6)
    return JSONResponse(snap)


@app.post("/api/pool/check")
async def api_pool_check():
    return JSONResponse(await POOL.check_all())


@app.get("/api/pool/check")
async def api_pool_check_state():
    return JSONResponse(POOL.check_state)


@app.get("/access")
async def access_page(request: Request):
    # expired=1 是前端 401 拦截带过来的：会话中途失效时不要弹回首页，
    # 否则「/access -> / -> 401 -> /access」会形成无限重载。
    if (request.query_params.get("expired") != "1"
            and access.check_session(request.cookies.get("wb_sid", ""))):
        return RedirectResponse("/", status_code=302)
    return FileResponse(str(STATIC / "access.html"))


@app.post("/api/access/request")
async def api_access_request(req: AccessRequest, request: Request):
    d = await run_in_threadpool(access.request_code, req.email or req.user,
                                _client_ip(request), request.headers.get("user-agent", ""))
    return JSONResponse(d, status_code=200 if d.get("ok") else (429 if d.get("limited") else 400))


@app.post("/api/access/verify")
async def api_access_verify(req: AccessVerify, request: Request):
    d = await run_in_threadpool(access.verify_code, req.email or req.user,
                                req.code, _client_ip(request))
    if not d.get("ok"):
        return JSONResponse(d, status_code=429 if d.get("limited") else 400)
    resp = JSONResponse({"ok": True, "msg": d["msg"]})
    _set_session_cookie(resp, d["token"])
    return resp


@app.post("/api/access/master")
async def api_access_master(req: MasterRequest, request: Request):
    d = await run_in_threadpool(access.master_login, req.password, _client_ip(request))
    if not d.get("ok"):
        return JSONResponse(d, status_code=429 if d.get("limited") else 400)
    resp = JSONResponse({"ok": True, "msg": d["msg"]})
    _set_session_cookie(resp, d["token"])
    return resp


@app.get("/")
async def index():
    # 工作台是单页应用，改版频繁；禁止缓存，避免用户拿到旧的 index.html
    return FileResponse(str(STATIC / "index.html"),
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/{path:path}")
async def static_page(path: str):
    f = STATIC / path
    if f.is_file():
        return FileResponse(str(f))
    raise HTTPException(404, "not found")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8020, log_level="info")
