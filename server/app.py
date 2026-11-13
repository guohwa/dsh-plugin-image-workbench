"""Workbench backend: handraw-style catalogs + ChatGPT account pool as token source."""

from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
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


def _plugin_version() -> str:
    """插件版本，取自插件根目录的 package.json。

    独立运行时（只把 server/ 拷出来跑）读不到那个文件，返回 unknown 即可，
    版本仅用于界面显示与排查「装的是哪一版」，不影响任何功能。
    """
    for cand in (LAB.parent / "package.json", LAB / "package.json"):
        try:
            v = str(json.loads(cand.read_text(encoding="utf-8")).get("version") or "").strip()
            if v:
                return v
        except Exception:
            continue
    return "unknown"


VERSION = _plugin_version()
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

    async def _patch_running_pool():
        """号池可能在上次会话里就活着（插件升级后很常见），此时 watchdog 不会碰它，
        源码补丁也就永远落不了地。这里主动走一次 start()：它会补打补丁，
        并在补丁刚应用时重启号池，让新代码真正加载。"""
        try:
            await asyncio.sleep(3)          # 先让 watchdog 的首次巡检跑完
            v = pool_service.view()
            if not v.get("manageable") or not pool_service.probe(v["port"], 0.5):
                return                      # 没在跑就交给 watchdog，那条路径同样会打补丁
            r = await run_in_threadpool(pool_service.start)
            pool_mod.log("号池启动自检：" + str(r.get("message") or ""))
        except Exception as e:
            pool_mod.log("号池启动自检异常：" + str(e)[:110])

    pool_service.start_watchdog()
    asyncio.create_task(_patch_running_pool())
    await POOL.start()
    yield
    await POOL.stop()


app = FastAPI(title="Handraw Workbench", lifespan=lifespan)
@app.get("/img/{full_path:path}")
async def img_with_fallback(full_path: str):
    """风格/版式预览图路由（带兜底占位）。

    repo_images/ 里的样例图不进 git（避免仓库膨胀），新安装时目录为空会全部 404、
    画廊破图。这里做兜底：文件存在原样返回；不存在则现场生成一张 SVG 占位图
    （渐变 + 风格编号），保证任何安装都显示整齐、可用，且不引入任何二进制资源。
    真要放高清样例图时，直接丢进 repo_images/ 即自动生效，无需改代码。
    """
    target = (REPO_IMAGES / full_path).resolve()
    root = str(REPO_IMAGES.resolve())
    # 目录穿越防护：只允许访问 repo_images 之内
    if str(target).startswith(root + os.sep) and target.is_file():
        # .webp 在部分系统（如 Windows）的 MIME 注册表里缺失，会被猜成
        # application/octet-stream；显式声明，保证浏览器 <img> 正确识别。
        mt = "image/webp" if target.suffix.lower() == ".webp" else None
        return FileResponse(str(target), media_type=mt,
                            headers={"Cache-Control": "public, max-age=604800"})
    # 文件缺失 → 生成 SVG 占位图
    label = Path(full_path).stem
    h = hashlib.md5(full_path.encode("utf-8")).hexdigest()
    c1, c2 = h[:6], h[6:12]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160" viewBox="0 0 240 160">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#{c1}"/><stop offset="1" stop-color="#{c2}"/>'
        "</linearGradient></defs>"
        f'<rect width="240" height="160" fill="url(#g)"/>'
        f'<text x="120" y="88" font-family="-apple-system,Segoe UI,sans-serif" font-size="22" '
        f'fill="#ffffff" text-anchor="middle" opacity="0.95">{label}</text>'
        "</svg>"
    )
    # 占位图必须 no-store：它只是「文件还没下载」的瞬态提示，绝不能进浏览器缓存。
    # 否则真图补上后，浏览器仍按旧 URL 命中缓存里的渐变占位，画廊就一直显示渐变。
    return Response(content=svg.encode("utf-8"), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


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
    # 参考图用法：style=只借画风/光影（默认，严禁 1:1 复刻）| full=尽量一比一还原参考图
    ref_mode: str = "style"
    # 出图模式与拍摄类型，只用于历史回看/载入还原，不参与拼提示词（提示词前端已拼好）
    image_mode: str | None = Field(default=None, max_length=16)
    shoot_type: str | None = Field(default=None, max_length=40)
    # 单张重做：新图成功后删掉这条旧记录，实现同一任务内原位替换
    replace_id: str | None = Field(default=None, max_length=32)
    # 「AI 生成文章」时正文要拆成多少条贴图条目（数字框控制，默认 7）；不传则按选题识别或走单篇
    num_items: int | None = Field(default=None, ge=1, le=20)
    # 从「AI 热点选题」点「用作选题」带来的事件 id：有它就取事件综述当唯一事实来源，
    # 不让模型只凭一句标题脑补日期、名单和数字。前端只在选题文字没被改动时才传。
    story_id: str | None = Field(default=None, max_length=64)


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
    # 小说拆分给出的「画面表演要点」；老链路不带这个字段，行为完全不变
    visual: str = ""


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
    # 参考图用法：style=只借画风（默认）| full=完整复刻
    ref_mode: str = "style"
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
    # 参考图约束是「内部指令」：只喂给模型，不进用户可编辑的提示词正文。
    # compose 返回的 prompt 已是干净版，这里在真正出图时才把约束拼到文本最前面。
    ref_instr = ""
    if req.ref_id:
        # style=只借画风（默认，严禁 1:1 复刻）；full=尽量一比一还原参考图
        ref_instr = composer.REF_FUSION if (req.ref_mode or "style") != "full" else composer.REF_FUSION_FULL
    if req.prompt and req.prompt.strip():
        # 已有文案（来自「生成提示词」或手改）：把参考约束拼在最前，绝不丢掉
        return (ref_instr + "\n" + req.prompt.strip()) if ref_instr else req.prompt.strip()
    style = _find(CATALOG["styles"], req.style_id)
    layout = _find(CATALOG["layouts"], req.layout_id)
    color = _find(CATALOG["colors"], req.color_id)
    parts: list[str] = []
    if ref_instr:
        parts.append(ref_instr)
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
    parts.append(composer.NO_CUP_RULE)
    parts.append("请立即生成图片，不要反问，不要输出任何解释文字。")
    return "\n".join(parts)


_LIST_INTENT_RE = re.compile(
    r"(?:\d+\s*(?:条|张|个|则)\s*(?:适合)?(?:生成)?(?:贴图|分镜|卡片|清单|文案))"
    r"|(?:贴图|分镜|卡片|清单|文案)[^\n]{0,12}\d+\s*条"
    r"|01\s*[｜|]"
)


def _story_fact_block(story: dict | None) -> str:
    """热点事件的资料块：先划清事实边界，再让模型按格式写。

    不要求「全文照抄资料」——综述常常只有四五百字，硬锁死会让 7 条文案变成复读；
    只锁住会翻车的部分：时间、人名、公司名、产品名、数字、名单。
    """
    if not story:
        return ""
    digest = str(story.get("digest") or "").strip()[:3000]
    if not digest:
        return ""
    parts = ["下面是这条热点事件的资料，来自第三方热点聚合站，是本次写作唯一的事实来源。"]
    title = str(story.get("title") or "").strip()[:200]
    if title:
        parts.append("事件：" + title)
    parts.append("综述：" + digest)
    latest = str(story.get("latest") or "").strip()[:600]
    if latest and latest not in digest:
        parts.append("最新进展：" + latest)
    meta = []
    if story.get("sourceCount"):
        meta.append(f"{story['sourceCount']} 个信源")
    for key, label in (("firstReportAt", "首次报道"), ("latestAt", "最近更新")):
        val = str(story.get(key) or "").strip()[:19]
        if val:
            meta.append(label + " " + val.replace("T", " "))
    if meta:
        parts.append("资料元信息：" + "，".join(meta))
    parts.append(
        "事实红线：时间、人名、公司名、产品名、数字和名单只能取自上面的资料；"
        "资料里没写的不要补，也不要把「据报道」「据称」改写成确定事实。"
        "观点、场景、口语化表达和情绪可以自由发挥，但不能编造新的事件细节。")
    return "\n".join(parts) + "\n\n"


def build_text_prompt(req: GenRequest, story: dict | None = None) -> str:
    """普通选题走小红书笔记模板；选题明确要「N 条贴图/分镜文案」、给了 01｜ 示例、
    或前端「条数」框填了数字时，改走编号清单模板，避免 AI 输出
    「开场导语 + 编号条目 + 结尾标签」的混合格式（那种格式前端拆分只能识别成 1 条）。
    编号清单模板末尾带 3-5 个 #话题标签，方便直接贴图与回看。

    story 由 /api/preview、/api/generate 从 AIHOT 事件综述取来；为空时提示词逐字不变。
    """
    topic = req.topic.strip()
    head = _story_fact_block(story)
    # 条数框优先；其次从选题里识别「N 条」意图；都没有就走单篇笔记模板
    n = None
    if req.num_items and req.num_items >= 1:
        n = min(max(req.num_items, 1), 20)
    elif _LIST_INTENT_RE.search(topic):
        m = re.search(r"(\d+)\s*条", topic)
        n = min(max(int(m.group(1)) if m else 8, 2), 20)
    if n:
        return head + (
            f"你是贴图分镜文案写手。围绕选题「{topic}」，直接输出成品，不要任何解释。\n"
            f"第一行：整组标题（20字以内，带情绪钩子，可用一个emoji）。\n"
            "第二行：空一行后，先写一段 2-4 句的概述段落，用自然流畅的简体中文把这件事的来龙去脉"
            "和为什么值得聊一句话点透，作为整组内容的总起；不要加「概述」「导语」之类的标题标记，"
            "直接写正文段落，也不要写成客套话。\n"
            f"概述之后空一行，严格输出 {n} 条贴图文案，编号从 01 连续到 {n:02d}。\n"
            "每一条占两行：第一行是两位编号、一个竖线、再加小标题（小标题 8 字以内），"
            "第二行紧挨着写这一条的正文（60到100字，口语化，写具体场景和细节，不要空话），"
            "编号行和正文之间不要有空行。\n"
            f"条与条之间用一个空行隔开，全文一共 {n} 个编号行。\n"
            "硬性要求：编号条目里不要出现 markdown 标记（不要 ** 加粗、不要以 # 开头的标题行），"
            "不要写「以下是」「最后想告诉你」这类客套话；正文里不要出现新的编号行，"
            "也不要把上面这些格式说明当成内容输出。\n"
            "最后单独一行：3-5 个 #话题标签（每个标签 2-6 字，覆盖选题核心词与人群/场景，"
            "格式如「#AI看病 #健康科普 #避坑」），标签之间用空格隔开，这一行不要再写别的字。\n"
            "全程使用流畅的简体中文写作，不要输出英文碎片、URL、链接、乱码或无意义的字母数字"
            "组合（例如 turn/5u8495ea07e7 这类字符串），也不要输出代码块或任何非正文内容。"
        )
    return head + (
        f"你是小红书爆款笔记写手。围绕选题「{topic}」，直接输出成品，不要任何解释：\n"
        "第一行：标题（20字以内，带情绪钩子，可用一个emoji）\n"
        "空行后：正文 300-500 字，口语化、分段、适量emoji，有具体细节不要空话\n"
        "空行后：5 个 #话题标签\n"
        "不要写“以下是”“希望对你有帮助”这类客套话。\n"
        "全程使用流畅的简体中文写作，不要输出英文碎片、URL、链接、乱码或无意义的字母数字组合。"
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
    return {"ok": True, "version": VERSION}


def _ver_key(v: str) -> tuple:
    """把 x.y.z 解析成可比较的键。

    数字段按整数比（所以 1.1.9 < 1.1.10，字符串比会错）；缺的段补 0，
    1.1 与 1.1.0 视为同版本；带 -beta / -rc 后缀的排在同号正式版之前。
    """
    core = (v or "").strip().split("+")[0]
    head, sep, _pre = core.partition("-")
    parts = []
    for seg in head.split("."):
        m = re.match(r"\d+", seg)
        parts.append(int(m.group()) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return (tuple(parts), 0 if sep else 1)


_UPGRADE_CACHE: dict = {"at": 0.0, "data": None, "ttl": 0.0}
# 缓存时长分三档：确实有新版本才敢锁久（10 分钟），
# 没查到新版本只锁 90 秒——镜像同步慢的时候，锁 10 分钟会让用户晚十分钟才看见提示；
# 查询失败锁 60 秒，网络恢复能尽快重试。
_UPGRADE_TTL_NEW = 600
_UPGRADE_TTL_SAME = 90
_UPGRADE_TTL_FAIL = 60
_REGISTRIES = (
    "https://registry.npmmirror.com/dsh-plugin-image-workbench/latest",
    "https://registry.npmjs.org/dsh-plugin-image-workbench/latest",
)
# 更新说明随包一起发（server/changelog.json）。旧版本自己的这份文件里没有新版本的条目，
# 所以只能去 CDN 上读新包里的那份；npmmirror 的 files 接口要白名单，用不了。
_CHANGELOG_BASES = (
    "https://cdn.jsdelivr.net/npm/dsh-plugin-image-workbench@{}/server/changelog.json",
    "https://unpkg.com/dsh-plugin-image-workbench@{}/server/changelog.json",
)
_NOTES_MAX_VERSIONS = 3      # 落后很多版本时只列最近三个，别把面板撑爆
_NOTES_MAX_ITEMS = 8


async def _fetch_changelog(ver: str) -> list:
    """取指定版本随包发布的更新说明，两个 CDN 依次兜底，都失败返回空。"""
    import httpx

    async with httpx.AsyncClient(trust_env=False, timeout=6, follow_redirects=True) as c:
        for tpl in _CHANGELOG_BASES:
            try:
                r = await c.get(tpl.format(ver))
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        return data
            except Exception:
                continue
    return []


def _notes_between(current: str, latest: str, log: list) -> list:
    """从完整 changelog 里挑出「当前版本之后」的条目，最新排前面。"""
    rows = []
    for it in log:
        if not isinstance(it, dict):
            continue
        v = str(it.get("version") or "").strip()
        if not v:
            continue
        # 只要 current < v <= latest：跨版本升级时一次把漏掉的说明都补上
        if _ver_key(v) > _ver_key(current) and _ver_key(v) <= _ver_key(latest):
            items = [str(t) for t in (it.get("items") or []) if str(t).strip()]
            rows.append({"version": v, "date": str(it.get("date") or ""),
                         "items": items[:_NOTES_MAX_ITEMS]})
    rows.sort(key=lambda x: _ver_key(x["version"]), reverse=True)
    return rows[:_NOTES_MAX_VERSIONS]


async def _latest_version() -> str:
    """问所有源，取最高的版本号。

    镜像同步比官方源慢几分钟，只看镜像会在发版后漏报；而「镜像有响应」不等于
    「镜像已同步」，所以要两边都问再比大小，不能谁先答就用谁。
    """
    import httpx

    async def one(c, url: str) -> str:
        try:
            r = await c.get(url)
            if r.status_code == 200:
                return str(r.json().get("version") or "").strip()
        except Exception:
            pass
        return ""

    async with httpx.AsyncClient(trust_env=False, timeout=6, follow_redirects=True) as c:
        got = await asyncio.gather(*[one(c, u) for u in _REGISTRIES])
    best = ""
    for v in got:
        if v and (not best or _ver_key(v) > _ver_key(best)):
            best = v
    return best


@app.get("/api/upgrade")
async def api_upgrade():
    """对比 npm 上的最新版本，供左下角出「有新版本」提示。

    直连 registry（不吃代理环境变量，避免被带偏），结果按是否有新版本分档缓存；
    网络不通就当没有新版本，绝不因为查版本失败打扰使用。
    """
    now = time.time()
    cached = _UPGRADE_CACHE["data"]
    if cached is not None and now - _UPGRADE_CACHE["at"] < _UPGRADE_CACHE["ttl"]:
        return JSONResponse(cached)
    if VERSION == "unknown":
        # 独立运行读不到 package.json：没有可比基准，不做升级判定，免得永远提示有新版本
        data, ttl = {"current": VERSION, "latest": "", "has_new": False, "notes": []}, _UPGRADE_TTL_SAME
    else:
        lat = await _latest_version()
        has_new = bool(lat) and _ver_key(lat) > _ver_key(VERSION)
        data = {"current": VERSION, "latest": lat, "has_new": has_new, "notes": []}
        if has_new:
            # 只说清了「有新版本」还不够，得让人知道新在哪：随包发的 changelog 里捞
            data["notes"] = _notes_between(VERSION, lat, await _fetch_changelog(lat))
        ttl = _UPGRADE_TTL_NEW if has_new else (_UPGRADE_TTL_SAME if lat else _UPGRADE_TTL_FAIL)
    _UPGRADE_CACHE.update({"at": now, "data": data, "ttl": ttl})
    return JSONResponse(data)


# ---------- 一键升级：从 npm 拉整包，只覆盖代码文件 ----------
#
# 插件的运行目录就是源码目录（DSH 用目录联接指过来），草稿、历史、图片、配置、
# 虚拟环境都躺在同一个目录里。所以升级只做一件事：把新包的代码文件逐个盖上去，
# 其余一律不碰——用白名单而不是黑名单，认不出来的路径直接跳过。

PLUGIN_ROOT = LAB.parent
# 顶层这几个是插件外壳，跟随版本一起更新
_TOP_CODE = {"host.js", "client.js", "cordis.patch.yml", "install.mjs", "package.json"}
# 这些目录整棵不动：虚拟环境、运行态数据、号池内核（几十 MB，升级用不到）
_KEEP_ANY = {"vendor", "node_modules", ".venv", "__pycache__", "workbench", "images",
             "thumbs", "refs", "repo_images", ".pytest_cache", ".git"}
_KEEP_FILES = {"config.local.json", "app.db", "history.db"}
_MAX_TARBALL = 120 * 1024 * 1024
_TARBALL_BASES = (
    "https://registry.npmmirror.com/dsh-plugin-image-workbench/-/dsh-plugin-image-workbench-{}.tgz",
    "https://registry.npmjs.org/dsh-plugin-image-workbench/-/dsh-plugin-image-workbench-{}.tgz",
)


def _upgrade_rel(rel: str) -> str | None:
    """tarball 内的相对路径 -> 该不该写盘。不该写返回 None。"""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    name = parts[-1]
    if any(p in _KEEP_ANY for p in parts[:-1]):
        return None
    if name in _KEEP_FILES or name.endswith((".db", ".log", ".pyc", ".upnew")):
        return None
    if len(parts) == 1:
        return rel if name in _TOP_CODE else None
    top = parts[0]
    if top == "server":
        if len(parts) == 2:
            ok = name.endswith(".py") or name in {"catalog.json", "requirements.txt", "changelog.json"}
            return rel if ok else None
        return rel if parts[1] == "static" else None
    if top == "scripts":
        return rel if name.endswith((".mjs", ".py")) else None
    return None


async def _fetch_tarball(ver: str) -> bytes:
    """取 npm 整包。镜像优先、官方源兜底，都失败返回空。"""
    import httpx
    async with httpx.AsyncClient(trust_env=False, timeout=90, follow_redirects=True) as c:
        for tpl in _TARBALL_BASES:
            try:
                r = await c.get(tpl.format(ver))
                if r.status_code == 200 and r.content and len(r.content) <= _MAX_TARBALL:
                    return r.content
            except Exception:
                continue
    return b""


def _extract_tarball(blob: bytes, dest: Path) -> Path | None:
    """解开 npm 包，返回 package/ 目录；版本号对不上就当失败。"""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        try:
            tf.extractall(dest, filter="data")   # 3.12+ 默认要 filter，挡路径穿越
        except TypeError:
            tf.extractall(dest)
    pkg = dest / "package"
    return pkg if pkg.is_dir() else None


@app.post("/api/upgrade/apply")
async def api_upgrade_apply():
    """下载并覆盖代码后返回；重启由 /api/upgrade/restart 单独触发。

    出图进行中直接拒绝：重启会把在途请求掐断，宁可让用户等几十秒。
    """
    snap = POOL.snapshot()
    if snap.get("running_now") or int(snap.get("queued") or 0) > 0 or int(snap.get("busy") or 0) > 0:
        return JSONResponse({"ok": False, "error": "正在出图，等这批跑完再升级（升级会重启服务）"},
                            status_code=409)
    latest = await _latest_version()
    if not latest:
        return JSONResponse({"ok": False, "error": "取不到最新版本号，检查网络后重试"}, status_code=502)
    if _ver_key(latest) <= _ver_key(VERSION):
        return JSONResponse({"ok": False, "error": "已经是最新版本 v" + VERSION}, status_code=400)

    blob = await _fetch_tarball(latest)
    if not blob:
        return JSONResponse({"ok": False, "error": "安装包下载失败，可点「复制命令」手工升级"}, status_code=502)

    tmp = Path(tempfile.mkdtemp(prefix="iw-upgrade-"))
    try:
        pkg = _extract_tarball(blob, tmp)
        if pkg is None:
            return JSONResponse({"ok": False, "error": "安装包结构不对，已放弃（未改动任何文件）"},
                                status_code=502)
        try:
            got = str(json.loads((pkg / "package.json").read_text(encoding="utf-8")).get("version") or "")
        except Exception:
            got = ""
        if got.strip() != latest:
            return JSONResponse({"ok": False, "error": "安装包版本核对不上（拿到 " + (got or "空") + "），已放弃"},
                                status_code=502)

        # 先把「要写哪些文件」算清楚，再动手，避免中途才发现路径不对
        plans = []
        for f in sorted(pkg.rglob("*")):
            if not f.is_file():
                continue
            rel = _upgrade_rel(f.relative_to(pkg).as_posix())
            if not rel:
                continue
            dest = (PLUGIN_ROOT / rel).resolve()
            if PLUGIN_ROOT not in dest.parents:
                continue   # 越界，宁可不写
            plans.append((f, dest, rel))
        if not plans:
            return JSONResponse({"ok": False, "error": "包里没有可覆盖的代码文件，已放弃"}, status_code=502)

        # 备份：把即将被覆盖的现有文件打包，出问题可以手工解回去
        bdir = LAB / "workbench" / "backup"
        bdir.mkdir(parents=True, exist_ok=True)
        zpath = bdir / ("v" + VERSION + "-" + time.strftime("%Y%m%d-%H%M%S") + ".zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for _src, dest, rel in plans:
                if dest.exists():
                    z.write(dest, rel)

        # 覆盖：先写临时文件再 os.replace，中途失败不会留下半截文件。
        # 行尾跟着原文件走：Windows 上仓库是 CRLF 检出，包内是 LF，直接盖过去会让
        # git status 一片 M（内容其实没变），所以文本文件保持原来的行尾风格。
        _text_ext = {".py", ".html", ".css", ".js", ".mjs", ".json", ".txt",
                     ".yml", ".yaml", ".md"}
        for src, dest, _rel in plans:
            dest.parent.mkdir(parents=True, exist_ok=True)
            raw = src.read_bytes()
            if dest.suffix.lower() in _text_ext and dest.exists():
                try:
                    old = dest.read_bytes()
                except OSError:
                    old = b""
                if b"\r\n" in old and b"\r\n" not in raw:
                    raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            staging = dest.with_name(dest.name + ".upnew")
            staging.write_bytes(raw)
            os.replace(staging, dest)

        # 备份只留最近 5 份：每次 200K 出头，攒一年也占不了多少，但没必要无限堆
        try:
            olds = sorted(bdir.glob("v*.zip"), key=lambda p: p.stat().st_mtime)
            for p in olds[:-5]:
                p.unlink()
        except OSError:
            pass

        _UPGRADE_CACHE.update({"at": 0.0, "data": None, "ttl": 0.0})
        return JSONResponse({"ok": True, "from": VERSION, "to": latest,
                             "files": len(plans), "backup": zpath.name,
                             "note": "号池内核 vendor/ 未替换"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": "升级中断：" + type(e).__name__ + " " + str(e)[:120]},
                            status_code=500)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/upgrade/restart")
async def api_upgrade_restart():
    """升级后自重启：进程退出，DSH 宿主的守护会拉起新代码。"""
    async def _bye():
        await asyncio.sleep(0.8)   # 先让这次响应发出去
        os._exit(0)
    asyncio.create_task(_bye())
    return JSONResponse({"ok": True})


@app.get("/api/catalog")
async def api_catalog():
    return JSONResponse(CATALOG)


@app.post("/api/catalog/reload")
async def api_catalog_reload():
    """上游风格库自动同步后热更新内存目录，免重启 8092。

    scripts/sync-handraw-style.mjs 只负责写 catalog.json；这里重读文件并原地
    clear+update，保持 CATALOG 的对象引用不变，其它模块持有的引用也能看到新数据。
    """
    try:
        data = json.loads((LAB / "catalog.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(data.get(k), list)
                                                 for k in ("styles", "layouts", "colors")):
            raise ValueError("catalog.json 结构不完整")
        CATALOG.clear()
        CATALOG.update(data)
        return JSONResponse({"ok": True, "styles": len(CATALOG["styles"]),
                            "layouts": len(CATALOG["layouts"]),
                            "colors": len(CATALOG["colors"])})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ---------------- AIHOT 热点选题 ----------------
# 外部内容源：AIHOT（aihot.news）的公开只读接口，匿名可读、无需 key。
# 前端直连会被跨域挡住，所以由后端代取；顺便当缓存层——官方建议最快 60 秒一次，
# 这里 10 分钟一取，手动刷新只是把这个间隔提前，不至于把人家的接口打爆。
# 自己按 AIHOT 仓库搭了站，把 AIHOT_API_BASE 指过去就换成自家的信源和精选标准。
_AIHOT_API = os.environ.get("AIHOT_API_BASE", "https://aihot.news/api/v1").rstrip("/")
_AIHOT_TTL = 600              # 热点榜缓存 10 分钟
_AIHOT_STORY_TTL = 1800       # 事件综述缓存 30 分钟，综述是按天演化的，不用勤取
_AIHOT_HOT: dict = {"at": 0.0, "items": [], "err": ""}
_AIHOT_STORY: dict = {}


async def _aihot_json(path: str) -> dict:
    """取 AIHOT 的一个 JSON 接口。

    trust_env=False：不吃系统代理变量。这个域名国内直连能通，反而是走代理更容易被拦；
    和上面取版本号的做法保持一致。
    """
    import httpx

    async with httpx.AsyncClient(
        trust_env=False, timeout=12, follow_redirects=True,
        headers={"User-Agent": "BluxeImageWorkbench (local workbench)"},
    ) as c:
        r = await c.get(_AIHOT_API + path)
        if r.status_code == 429:
            raise RuntimeError("请求太频繁，等一分钟再试")
        r.raise_for_status()
        return r.json()


def _aihot_story_id(link: str) -> str:
    """事件页地址 https://aihot.news/story/<uuid> → uuid。"""
    s = str(link or "").rstrip("/")
    if "/story/" not in s:
        return ""
    return s.rsplit("/", 1)[-1]


def _aihot_slim(raw: dict) -> list:
    """热点榜原样字段太多（信源名一长串、各种计数），只留前端要用的。"""
    out = []
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        links = it.get("links") or {}
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "rank": int(it.get("rank") or 0),
            "title": title,
            "sourceCount": int(it.get("sourceCount") or 0),
            "participantCount": int(it.get("participantCount") or 0),
            "latestAt": str(it.get("latestAt") or ""),
            "storyId": _aihot_story_id(links.get("story")),
            "aihot": str(links.get("aihot") or ""),
            "original": str(links.get("original") or ""),
            "sources": [str(x) for x in (it.get("sourceNames") or [])][:8],
        })
    return out


@app.get("/api/aihot/topics")
async def api_aihot_topics(fresh: int = 0):
    """当前 AI 热点榜（事件级），给左栏选题卡片和放大弹窗用。

    拉不到时退回上一次的结果并带上 err，前端标一句「刚才那份」，不让整块变空白。
    """
    now = time.time()
    have = bool(_AIHOT_HOT["items"])
    if have and not fresh and now - _AIHOT_HOT["at"] < _AIHOT_TTL:
        return JSONResponse({"ok": True, "stale": False, "at": _AIHOT_HOT["at"],
                             "items": _AIHOT_HOT["items"]})
    try:
        items = _aihot_slim(await _aihot_json("/hot-topics"))
        if not items:
            raise RuntimeError("接口没返回内容")
        _AIHOT_HOT.update({"at": now, "items": items, "err": ""})
        return JSONResponse({"ok": True, "stale": False, "at": now, "items": items})
    except Exception as e:
        msg = (str(e) or e.__class__.__name__)[:120]
        _AIHOT_HOT["err"] = msg
        return JSONResponse({"ok": have, "stale": have, "at": _AIHOT_HOT["at"],
                             "items": _AIHOT_HOT["items"], "err": msg})


def _aihot_story_clean(sid: str) -> str:
    """事件 id 只认 AIHOT 自己给的 uuid，顺手挡掉拼 URL 的花样。"""
    return re.sub(r"[^0-9a-zA-Z\-]", "", str(sid or ""))[:64]


def _aihot_story_pack(raw: dict) -> dict:
    """事件详情压成前端和写作提示词共用的字段。"""
    st = raw.get("story") or {}
    return {
        "ok": True,
        "title": str(st.get("title") or ""),
        "digest": str(st.get("digest") or ""),
        "latest": str(st.get("latest") or ""),
        "sourceCount": int(st.get("sourceCount") or 0),
        "reportCount": int(st.get("reportCount") or 0),
        "firstReportAt": str(st.get("firstReportAt") or ""),
        "latestAt": str(st.get("latestAt") or ""),
    }


async def _aihot_story_data(sid: str) -> dict | None:
    """「AI 生成文章」取事实资料：优先用 30 分钟缓存，用户没点开过综述就现拉一次。

    取不到就返回 None，提示词退回老模板，绝不因为热点接口抖动挡住用户写稿。
    """
    sid = _aihot_story_clean(sid)
    if not sid:
        return None
    hit = _AIHOT_STORY.get(sid)
    if hit and time.time() - hit["at"] < _AIHOT_STORY_TTL:
        return hit["data"] if str((hit["data"] or {}).get("digest") or "").strip() else None
    try:
        raw = await _aihot_json("/stories/" + sid)
    except Exception:
        return None
    data = _aihot_story_pack(raw)
    if not data["digest"].strip():
        return None
    _AIHOT_STORY[sid] = {"at": time.time(), "data": data}
    return data


async def _text_story(req: "GenRequest") -> dict | None:
    """写文章时才取热点资料，并核对选题确实就是这条热点。

    前端已经比对过标题，这里再兜一层：手动构造请求、或者会话恢复出旧 id 时，
    不会把 A 事件的综述塞进 B 选题的提示词。
    """
    if req.mode != "text" or not req.story_id:
        return None
    story = await _aihot_story_data(req.story_id)
    if not story:
        return None
    st = str(story.get("title") or "").strip()
    topic = (req.topic or "").strip()
    if not st or not topic:
        return None
    if not (st == topic or st in topic or topic in st or st[:8] == topic[:8]):
        return None
    return story


@app.get("/api/aihot/story")
async def api_aihot_story(id: str = ""):
    """单个事件的 AI 综述。id 只来自热点榜返回的 storyId，不接受猜测。"""
    sid = _aihot_story_clean(id)
    if not sid:
        raise HTTPException(status_code=400, detail="缺少事件 id")
    hit = _AIHOT_STORY.get(sid)
    if hit and time.time() - hit["at"] < _AIHOT_STORY_TTL:
        return JSONResponse(hit["data"])
    try:
        raw = await _aihot_json("/stories/" + sid)
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=(str(e) or "AIHOT 拉取失败")[:120])
    data = _aihot_story_pack(raw)
    _AIHOT_STORY[sid] = {"at": time.time(), "data": data}
    return JSONResponse(data)


# ---------------- 推荐话题 ----------------
# 目标：给「选题」框喂能引起讨论、带判断依据的题，不要空问「你怎么看」，也不要鸡汤。
# 数据分三层，任一层拿不到都不会让这块变空白：
#   1. 实时知乎热榜（公开聚合接口，匿名只读；可用 TOPIC_HOT_API 换成自己的源）
#   2. 本进程缓存的 AIHOT 热点（真事件，不额外发请求）
#   3. 内置常青讨论题库（逐条挑过：具体数字 / 工具 / 动作 + 一个能站队的两难点）
# 界面会标明这批来自哪一层，兜底不冒充实时热榜。加工只做「把事件变成可讨论的
# 切口」，不替用户写结论，也不给热点补编事实。
_REC_TTL = 1800
_REC_CACHE: dict = {"at": 0.0, "items": [], "source": "", "live": False}
# 公开聚合站对非浏览器 UA 直接 403（实测 403 / 200 只差这一行），
# 这里只用于匿名只读热榜，不携带任何本机信息。
_REC_UA = "Mozilla/5.0"

# 空词和大词：命中就不加工，避免把热榜标题熬成毒鸡汤
_TOPIC_BAD = re.compile(
    r"震惊|泪目|破防|一定要|人生赢家|格局|认知差|真正厉害|成年人的世界|"
    r"逆袭|揭秘|看完沉默|建议收藏|他说了一句话|扎心|治愈|自律|坚持|努力")
# 干货的最低门槛：得有数字，或者有一个能动手验证的动作
_TOPIC_SOLID = re.compile(r"\d|[0-9一二三四五六七八九十]|[先查算改拆记核实标列数试对比留换删]")

# 把一条热点标题变成可讨论的选题切口。每条都要求读者给出依据或做法，
# 而不是表态。选哪个角度由标题决定，同一条热点每次都稳定落在同一个角度。
_HOT_ANGLES = (
    ("{t}：先别急着转发，把信息源、时间线、原始出处这 3 类公开数据查一遍再下结论", "事实核查 · 3 类公开数据可复核"),
    ("{t}：如果要把这条写进团队周报，哪 2 个数字必须先核实？", "逼出具体数字，不靠印象站队"),
    ("同一个「{t}」，写给老板看和写给同行看，信息顺序该怎么换？", "同一事实两种写法，同行能直接晒自己的顺序"),
    ("{t}之后，团队里哪一件重复劳动可以真的删掉？给一个能算清工时的判断法", "落到工时，能算出结论就吵不起来"),
    ("拿「{t}」练一次提示词：怎么让 AI 只复述可核实的事实，不自己补细节", "有可复制的提示词做法"),
    ("{t}：这类变化要不要直接写进团队 SOP？先摆清 3 个前提再站队", "明确两难，正反两边都有话说"),
)

# 常青题库：按知乎 / 小红书上长期有人讨论的方向写，全部带可验证的细节。
# 这些不是「今日热榜」，source 会如实标成「常青选题库」。
_EVERGREEN_TOPICS = (
    ("用 AI 写了 3 周会议纪要，我把出错分成 4 类：最麻烦的不是错别字，是把待办派给了没参会的人", "知乎向", "有出错分类，同行能补自己的第 5 类"),
    ("同一份 200 页 PDF，先标页码再问 AI 和整本直接丢进去，答案能不能翻回原文核对，差在哪", "知乎向", "两步做法可照着复现"),
    ("让 AI 总结 90 分钟会议，提示词里加「只写有原话支撑的结论」之后，我删掉了几段很顺但没人说过的话", "知乎向", "一句约束能直接试"),
    ("团队要不要把 AI 生成的内容直接进 SOP？先算清返工工时、责任人、留痕这 3 笔账", "知乎向", "三笔账给出判断框架"),
    ("用 AI 做竞品调研，最该防的不是它编造，而是它把 3 个来源的时间线揉成一条", "知乎向", "指出一个常被漏掉的具体错误"),
    ("「会用 AI」要不要算进招聘要求？拿一份可复核的交付物来判，比问会不会写提示词靠谱", "知乎向", "两边都有明确立场"),
    ("把一个重复了半年的表格流程交给 AI 之前，我先记了 5 天工时，才敢说到底省了多少", "知乎向", "先有基线再谈收益"),
    ("AI 改写的文案读起来更顺，却把具体数字和责任主体改没了，这种损失该怎么量化", "知乎向", "把「变味」变成可算的项"),
    ("本地小模型和云端大模型做同一件整理工作，我按准确率、等待时间、单次成本记了 3 列", "小红书向", "三列对照表能直接抄"),
    ("让 AI 读合同，我要求每条结论后面附原文行号，这条约束挡掉过哪些看起来很确定的错", "知乎向", "可验证的行号约束"),
    ("为什么同一条提示词换一批资料就从好用变瞎编？我拆了输入长度和字段顺序两个变量", "知乎向", "把玄学问题拆成变量"),
    ("知识库接了 AI 问答之后，最先暴露的不是模型不行，是文档从来没人写清适用范围", "知乎向", "反直觉但有现场细节"),
    ("用 AI 生成周报，要不要保留「本周没做成的事」？删掉之后我少拿到了哪些反馈", "知乎向", "一个小改动引出管理话题"),
    ("把客户邮件交给 AI 起草，哪 3 类句子必须人工改，不然语气容易越界", "小红书向", "给出可执行的检查清单"),
    ("AI 给的方案看着完整，怎么在 10 分钟内判断哪一步没法落地？先查有没有写责任人和截止日", "知乎向", "10 分钟判断法"),
    ("我把 7 个 AI 写作工具用同一篇稿子跑了一遍，只留下能一次改好标题和小标题的那个", "小红书向", "同一篇稿子做对照"),
    ("提示词里写「给 3 个版本并说明取舍」，比写「写得更好」多花的 20 秒值不值", "小红书向", "差异能当场计时"),
    ("手机备忘录加 AI 整理能不能替代专门笔记软件？我按找得到、改得动、带得走三项试了两周", "小红书向", "三项标准可直接套用"),
    ("做图文封面，AI 出图和模板改图各花多久？我按 10 张图记了时间", "小红书向", "有样本量有时间账"),
    ("把长文拆成 9 张贴图，先写小标题还是先配图？两种顺序我各做了一次", "小红书向", "同题两种流程对比"),
    ("用 AI 起标题，一次给 20 个和分 4 轮各给 5 个，哪种更容易挑出能用的", "小红书向", "两种做法能复跑验证"),
    ("AI 写的口播稿念出来总别扭，我把句子压到 18 字以内才顺，这个长度对所有题材都成立吗", "小红书向", "给了具体数字也留了争议"),
    ("同一份素材发公众号和小红书，标题、开头、结尾各要换什么？我列了 6 处", "小红书向", "六处改动能逐条对照"),
    ("把 AI 当校对用，让它只挑错不改文，比让它润色少踩了哪些坑", "小红书向", "两种用法对比"),
    ("用 AI 做选题库，30 条里最后真能写的只有 5 条，卡住的是哪一类", "知乎向", "把转化率摆到台面上"),
    ("拍照笔记转文字再让 AI 整理，识别错的字会把结论带偏多少？我故意留了 5 处错试了下", "小红书向", "故意注入错误做实验"),
    ("给 AI 的参考样例放 1 个还是 3 个？我用同一段文案对比了输出稳定性", "小红书向", "变量单一，结论可复现"),
    ("把 AI 生成的内容打回重写，写「哪里不对」和写「改成什么样」，返工次数差多少", "小红书向", "两种反馈写法可计数"),
    ("一份提示词用到第 3 周开始失灵，是模型变了还是输入变了？我留了版本记录才分清", "知乎向", "引出版本管理习惯"),
    ("先让 AI 列大纲再写，和直接写再让它补结构，哪版更少改？我拿 8 篇稿子数了改动行数", "小红书向", "用改动行数代替感觉"),
)


def _topic_clean(text: str) -> str:
    """压掉空白和控制字符，截断到能塞进选题框的长度。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) > 64:
        s = s[:63].rstrip("，,、：: ") + "…"
    return s


def _topic_ok(text: str) -> bool:
    """干货闸门：长度够、不含鸡汤空词、有数字或可动手验证的动作。"""
    s = str(text or "")
    # 上限放到 130：热点标题本身可能就 60 多字，套上讨论角度会到 100 出头，
    # 截断到 100 会把句子切坏；选题框能放两行，长一点不影响阅读。
    return (12 <= len(s) <= 130 and not _TOPIC_BAD.search(s)
            and bool(_TOPIC_SOLID.search(s)))


def _topic_angle(title: str) -> tuple[str, str]:
    """同一条热点稳定挑一个讨论角度，不在「你怎么看」里打转。"""
    key = hashlib.md5(title.encode("utf-8", "replace")).digest()[0]
    tpl, why = _HOT_ANGLES[key % len(_HOT_ANGLES)]
    return tpl.format(t=title), why


def _topics_from_titles(titles: list, tag: str) -> list:
    out, seen, used_angle = [], set(), set()
    for raw in titles:
        t = _topic_clean(raw)
        if len(t) < 6 or _TOPIC_BAD.search(t) or t in seen:
            continue
        seen.add(t)
        # 同一批里角度尽量摊开，别连着三条都是「先别急着转发」
        idx = hashlib.md5(t.encode("utf-8", "replace")).digest()[0] % len(_HOT_ANGLES)
        if idx in used_angle:
            idx = next((j for j in range(len(_HOT_ANGLES)) if j not in used_angle), idx)
        used_angle.add(idx)
        tpl, why = _HOT_ANGLES[idx]
        text = tpl.format(t=t)
        if not _topic_ok(text):
            continue
        out.append({"id": "h" + hashlib.md5((tag + t).encode("utf-8", "replace")).hexdigest()[:10],
                    "text": text, "tag": tag, "why": why})
    return out


def _evergreen_topics() -> list:
    out = []
    for i, (text, tag, why) in enumerate(_EVERGREEN_TOPICS):
        if not _topic_ok(text):
            continue
        out.append({"id": "e%02d" % i, "text": text, "tag": tag, "why": why})
    return out


def _rec_interleave(groups: list) -> list:
    """按层轮流取，保证前几批就掺着实时热点和常青题，不会连着 10 条都是一类。"""
    out, seen = [], set()
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i >= len(g):
                continue
            it = g[i]
            if it["text"] in seen:
                continue
            seen.add(it["text"])
            out.append(it)
    return out


def _hot_titles_from_json(data) -> list:
    """兼容几种公开热榜的返回壳：直接数组 / items / data / data.items。"""
    rows = data
    if isinstance(data, dict):
        rows = data.get("items") or data.get("data") or data.get("list") or []
        if isinstance(rows, dict):
            rows = rows.get("items") or rows.get("list") or []
    if not isinstance(rows, list):
        return []
    out = []
    for x in rows:
        if isinstance(x, str):
            t = x
        elif isinstance(x, dict):
            t = (x.get("title") or x.get("name") or x.get("query")
                 or x.get("word") or x.get("desc") or "")
        else:
            t = ""
        t = str(t).strip()
        if t:
            out.append(t)
    return out


async def _fetch_zhihu_titles() -> list:
    """尽力抓一次真实知乎热榜；拿不到就返回空，交给下面两层兜底。

    公共聚合站会限流，所以每跳只给 2 秒、整体最多 4.5 秒；请求失败不算错误，
    更不能让首屏或「换一批」卡在转圈上。要走代理的机器先试代理再试直连。
    """
    import httpx

    custom = str(os.environ.get("TOPIC_HOT_API") or "").strip()
    urls = ([custom] if custom else []) + [
        "https://newsnow.busiyi.world/api/s?id=zhihu",
        "https://api-hot.imsyy.top/zhihu",
        "https://api.vvhan.com/api/hotlist/zhihuHot",
    ]
    has_proxy = bool(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
    # 直连优先。实测本机代理转发要多花 4 秒，先走代理会把整段超时吃光。
    modes = (False, True) if has_proxy else (False,)

    async def probe():
        for trust_env in modes:
            for url in urls:
                if not url.startswith(("http://", "https://")):
                    continue
                try:
                    async with httpx.AsyncClient(
                        trust_env=trust_env, timeout=2.6, follow_redirects=True,
                        headers={"User-Agent": _REC_UA, "Accept": "application/json"},
                    ) as c:
                        r = await c.get(url)
                    if r.status_code != 200:
                        continue
                    titles = _hot_titles_from_json(r.json())
                    if titles:
                        return titles
                except Exception:
                    continue
        return []

    try:
        return await asyncio.wait_for(probe(), timeout=5)
    except Exception:
        return []


async def _ensure_aihot_hot():
    """推荐话题可能比左栏热点卡片先到，这里自己补一次，别让第 2 层永远落空。"""
    if _AIHOT_HOT.get("items"):
        return
    try:
        items = _aihot_slim(await _aihot_json("/hot-topics"))
        if items:
            _AIHOT_HOT.update({"at": time.time(), "items": items, "err": ""})
    except Exception:
        pass


@app.get("/api/topics/recommend")
async def api_topics_recommend(fresh: int = 0):
    """一批可点选的推荐话题。前端每次露 3 条，点「下一批」在本地翻池子。"""
    now = time.time()
    if (not fresh and _REC_CACHE["items"]
            and now - _REC_CACHE["at"] < _REC_TTL):
        return JSONResponse({"ok": True, "stale": False, **{
            k: _REC_CACHE[k] for k in ("at", "items", "source", "live")}})

    # 两路并行：外部热榜最多 9 秒，自家热点通常 1 秒内回来，不互相拖
    live, _ = await asyncio.gather(_fetch_zhihu_titles(), _ensure_aihot_hot())
    aihot = [str(x.get("title") or "") for x in (_AIHOT_HOT.get("items") or [])[:8]]
    g_live = _topics_from_titles(live, "知乎热榜")[:9]
    g_ai = _topics_from_titles(aihot, "AIHOT")[:6]
    items = _rec_interleave([g_live, g_ai, _evergreen_topics()])
    if not items:
        items = _evergreen_topics()
    # 池子里掺了常青题就照实说，别让徽标看起来像整批都是实时热点
    source = ("知乎热榜 + 常青题库" if g_live
              else ("AIHOT 热点 + 常青题库" if g_ai else "常青选题库"))
    _REC_CACHE.update({"at": now, "items": items, "source": source, "live": bool(g_live)})
    return JSONResponse({"ok": True, "stale": False, "at": now, "items": items,
                         "source": source, "live": bool(g_live)})


# ---------------- 多平台热点榜（1.1.37）----------------
# 弹窗只读展示各平台热榜标题，每条再挂一个能直接写进选题框的讨论切口。
# 公开聚合站会限流，抓不到的平台不空白也不造假：明确标成「平台向 · 加工题」。
_HB_TTL = 600
_HB_CACHE: dict = {"at": 0.0, "data": None}
_HB_PLATFORMS = (
    ("zhihu",    "知乎",     ("https://newsnow.busiyi.world/api/s?id=zhihu",)),
    ("xhs",      "小红书",   ()),
    ("douyin",   "抖音",     ("https://newsnow.busiyi.world/api/s?id=douyin",
                              "https://60s.viki.moe/v2/douyin")),
    ("toutiao",  "今日头条", ("https://newsnow.busiyi.world/api/s?id=toutiao",)),
    ("kuaishou", "快手",     ()),
    ("weibo",    "微博",     ("https://newsnow.busiyi.world/api/s?id=weibo",
                              "https://60s.viki.moe/v2/weibo")),
    ("bilibili", "B 站",     ("https://newsnow.busiyi.world/api/s?id=bilibili",)),
)
_HB_NOTE = ("实时榜来自公开聚合站，缓存 10 分钟。带黄点的平台没有稳定公开热榜源，"
            "显示按该平台调性加工的常青讨论题，不冒充实时；"
            "可用 TOPIC_HOT_API_<平台 KEY> 环境变量接自己的源。")

# 弹窗里的热榜什么题材都有：油价、球赛、辟谣、社会新闻。
# _HOT_ANGLES 是给 AI 办公类热点写的，套到「油价下调」上会变成
# 「团队里哪件重复劳动可以删掉」这种不通顺的句子。这里只用对任何题材都成立的
# 事实核查 / 信息组织角度，依旧带数字、带可动手验证的动作。
_HB_ANGLES = (
    ("{t}：这条里哪 2 个数字最该先核实？先把能查到的来源列出来再下结论", "逼出具体数字和可查来源"),
    ("围绕「{t}」，把已确认的事实和还没证实的说法分成两栏，哪一栏更容易漏", "两栏对照能当场做"),
    ("{t}：只给 30 秒讲清这件事，你会保留哪个数字、砍掉哪句背景", "有限时间逼出取舍"),
    ("同一条「{t}」，写给客户看和发给同行看，哪 2 处信息顺序必须换", "同一事实两种写法"),
    ("拿「{t}」练一次提示词：怎么让 AI 只复述可核实的事实，不自己补细节", "有可复制的提示词做法"),
    ("{t}：先别急着转发，把信息源、时间线、原始出处这 3 类公开数据查一遍", "3 类公开数据可复核"),
)


def _hb_angle(title: str):
    """给一条任意题材的热榜标题挑通用讨论角度；标题带情绪词就只展示原题。"""
    # 太短的榜单词（例如「白小纯成绩」）本身没给足事实，只展示原题
    if len(title) < 8 or _TOPIC_BAD.search(title):
        return None
    key = hashlib.md5(title.encode("utf-8", "replace")).digest()[0]
    tpl, why = _HB_ANGLES[key % len(_HB_ANGLES)]
    text = tpl.format(t=_topic_clean(title))
    if not _topic_ok(text):
        return None
    return text, why

# 缺源平台的内置讨论题。依旧守干货闸门：具体数字 / 可复现动作 + 一个能站队的两难，
# 不写「真正厉害的人」「你怎么看」这类空句。
_HB_CURATED = {
    "xhs": (
        ("把一篇 1200 字长文拆成 9 张贴图，先写小标题和先配图各做一版，哪版返工少", "两版流程能照着复跑"),
        ("同一套封面换 3 种标题字号，收藏率差多少？我记了 10 篇笔记的数字", "有样本量有可查的数字"),
        ("笔记发布 2 小时后改标题，和一开始多花 20 分钟想标题，哪种更划算", "时间成本能当场算清"),
        ("用 AI 生成 20 个标题，最后能直接发的只有 3 个，剩下 17 个卡在同一类问题上", "把转化率摆到台面上"),
        ("口播稿压到每句 18 字以内，念着顺了但少了几条信息，这个取舍怎么算", "给了具体数字也留了争议"),
        ("笔记放 3 张步骤截图和放 1 张长图，评论区问的问题差在哪？我翻了 60 条评论", "有可复核的评论区样本"),
    ),
    "kuaishou": (
        ("同一条 45 秒口播，开头先报结论还是先摆现场，完播率差多少？两版各发 3 天", "两版对照能复跑"),
        ("把 3 分钟长视频剪成 6 条切片，哪条留住了来问做法的人？我按评论数排了一遍", "给出可数的判断口径"),
        ("字幕每行 12 字和 20 字，念起来都顺，评论区问的细节却不一样，差在哪", "把体感问题拆成变量"),
        ("拍同一套操作，固定机位一遍过和分 5 个镜头剪，返工时间我记了两天", "工时账能自己核对"),
        ("视频里报一个能查到的数字，比说效果好带来多少认真提问？我数了 4 条视频", "把虚的形容词换成计数"),
        ("把评论区问得最多的 3 个问题做成下一条，和不看评论直接拍，数据差在哪", "正反两种做法都有话说"),
    ),
}


def _hb_hot(v) -> str:
    """热度值统一成人能读的短串；上游没给就留空，不编数字。"""
    s = str(v or "").strip()
    if not s:
        return ""
    try:
        n = float(s)
        if n >= 100000000:
            return "%.1f 亿" % (n / 100000000)
        if n >= 10000:
            return "%.1f 万" % (n / 10000)
        return str(int(n))
    except Exception:
        return s[:12]


def _hot_board_rows(data) -> list:
    """兼容 NewsNow(items) / 60s(data) / vvhan(data) 三种壳，留标题、热度、链接。"""
    rows = data
    if isinstance(data, dict):
        rows = data.get("items") or data.get("data") or data.get("list") or []
        if isinstance(rows, dict):
            rows = rows.get("items") or rows.get("data") or rows.get("list") or []
    if not isinstance(rows, list):
        return []
    out = []
    for x in rows:
        if isinstance(x, str):
            t, hot, url = x, "", ""
        elif isinstance(x, dict):
            t = str(x.get("title") or x.get("name") or x.get("word")
                    or x.get("query") or x.get("desc") or "")
            hot = _hb_hot(x.get("hot_value") or x.get("hot") or x.get("hotValue")
                          or x.get("heat") or "")
            url = str(x.get("url") or x.get("link") or x.get("mobileUrl") or "")
        else:
            continue
        t = re.sub(r"\s+", " ", t).strip()[:80]
        if t:
            out.append({"title": t, "hot": hot, "url": url[:300]})
    return out[:30]


def _hot_board_items(rows: list, name: str, status: str) -> list:
    """实时标题原样展示；能过闸门才套讨论角度，套不上就只给原题。"""
    out = []
    for i, r in enumerate(rows[:12], 1):
        title = str(r.get("title") or "")
        made = _hb_angle(title) if status == "live" else None
        if made:
            text, why, angle = made[0], made[1], True
        else:
            text, why, angle = title, "原始热榜标题，未套讨论角度", False
        out.append({"rank": i, "title": title, "hot": str(r.get("hot") or ""),
                    "url": str(r.get("url") or ""), "text": text, "why": why,
                    "angle": angle, "status": status})
    return out


def _hb_curated(key: str, name: str) -> list:
    """缺源 / 断线时的平台向常青题，status 一律 curated，界面不冒充实时。"""
    pool = list(_HB_CURATED.get(key) or ())
    if not pool:
        ev = _evergreen_topics()
        want = "小红书向" if key == "xhs" else "知乎向"
        ev = [x for x in ev if x["tag"] == want] + [x for x in ev if x["tag"] != want]
        pool = [(x["text"], x["why"]) for x in ev[:8]]
    return [{"rank": i + 1, "title": t, "text": t, "why": w, "angle": True,
             "status": "curated", "hot": "", "url": ""}
            for i, (t, w) in enumerate(pool)]


async def _hb_fetch_all(specs: list) -> list:
    """多平台共用一个连接池并发抓。

    实测：七个平台各建一个 AsyncClient 会互相拖到 10 秒以上、全部超时；
    共用一个 client 时同域名 5 个平台 3.4 秒全回来。所以这里按「轮」抓，
    直连一轮，失败的才用代理补一轮，任何一轮超时都只丢弃没回来的那几个。
    """
    import httpx

    has_proxy = bool(os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
    headers = {"User-Agent": _REC_UA, "Accept": "application/json"}
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
    result = [[] for _ in specs]

    async def one_round(trust_env: bool, idxs: list, budget: float):
        if not idxs:
            return
        async with httpx.AsyncClient(
            trust_env=trust_env, timeout=3.0, follow_redirects=True,
            headers=headers, limits=limits,
        ) as c:
            async def one(i: int):
                for url in specs[i]:
                    if not str(url).startswith(("http://", "https://")):
                        continue
                    try:
                        r = await c.get(str(url))
                        if r.status_code == 200:
                            rows = _hot_board_rows(r.json())
                            if rows:
                                return i, rows
                    except Exception:
                        continue
                return i, []

            tasks = [asyncio.create_task(one(i)) for i in idxs]
            try:
                done, pending = await asyncio.wait(tasks, timeout=budget)
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                for t in done:
                    try:
                        i, rows = t.result()
                    except Exception:
                        continue
                    if rows:
                        result[i] = rows
            except Exception:
                for t in tasks:
                    t.cancel()

    await one_round(False, list(range(len(specs))), 4.2)
    if has_proxy:
        # 没有配置源的平台（小红书 / 快手）不用再开一轮空请求
        await one_round(True, [i for i, r in enumerate(result) if not r and specs[i]], 4.0)
    return result


@app.get("/api/topics/platforms")
async def api_topics_platforms(fresh: int = 0):
    """多平台热点榜：并发抓各平台，缓存十分钟；切页签在前端本地做。"""
    now = time.time()
    ttl = _HB_CACHE.get("ttl", _HB_TTL)
    if (not fresh and _HB_CACHE["data"]
            and now - _HB_CACHE["at"] < ttl):
        return JSONResponse({"ok": True, "stale": False, "at": _HB_CACHE["at"],
                             "note": _HB_NOTE, "platforms": _HB_CACHE["data"]})

    specs = []
    for key, _name, urls in _HB_PLATFORMS:
        custom = str(os.environ.get("TOPIC_HOT_API_" + key.upper()) or "").strip()
        specs.append(((custom,) if custom else ()) + tuple(urls))
    try:
        got = await _hb_fetch_all(specs)
    except Exception:
        got = [[] for _ in _HB_PLATFORMS]

    prev = {p["key"]: p for p in (_HB_CACHE["data"] or [])}
    platforms = []
    for (key, name, _urls), rows in zip(_HB_PLATFORMS, got):
        if rows:
            items, status, updated = _hot_board_items(rows, name, "live"), "live", now
        elif prev.get(key, {}).get("status") == "live":
            # 单次限流别让页签突然从「实时」掉成「加工题」，沿用上一次真数据
            old = prev[key]
            items, status, updated = old["items"], "live", old.get("updated") or now
        else:
            items, status, updated = _hb_curated(key, name), "curated", 0
        platforms.append({"key": key, "name": name, "status": status,
                          "updated": updated, "count": len(items),
                          "items": items})
    if platforms:
        live_n = sum(1 for p in platforms if p["status"] == "live")
        # 几乎全没抓到时别缓存十分钟，给下次打开留一次快速重试
        _HB_CACHE.update({"at": now, "data": platforms,
                          "ttl": _HB_TTL if live_n >= 3 else 90})
    elif _HB_CACHE["data"]:
        return JSONResponse({"ok": True, "stale": True, "at": _HB_CACHE["at"],
                             "note": _HB_NOTE, "platforms": _HB_CACHE["data"]})
    return JSONResponse({"ok": True, "stale": False, "at": now,
                         "note": _HB_NOTE, "platforms": platforms})


_SAMPLES_CACHE = {"at": 0.0, "data": None}


def _sample_progress() -> dict:
    """数一遍缩略图准备到哪了。

    首次安装时 repo_images 是空的（这 500 多张图不进发布包），插件在后台慢慢补。
    前端拿这个数字决定要不要在顶部提示条里说一句「先看到渐变占位是正常的」，
    补完自动撤掉，不做成永久横幅。
    """
    expected = have = 0
    for key in ("styles", "layouts", "colors"):
        for item in CATALOG.get(key) or []:
            # catalog 里写成 images/individual/...，落到磁盘时去掉 images/ 前缀，
            # 和前端 imgUrl() 的拼法、/img 路由的取法保持一致，否则永远数成 0 张。
            rel = re.sub(r"^images/", "", str((item or {}).get("img") or "").strip())
            if not rel:
                continue
            expected += 1
            if (REPO_IMAGES / rel).is_file():
                have += 1
    return {
        "total": expected,
        "have": have,
        "missing": expected - have,
        "ready": expected == 0 or have >= expected,
    }


@app.get("/api/samples")
async def api_samples(fresh: int = 0):
    """缩略图同步进度（只读）。补图是后台进行的，这里只报数。"""
    now = time.time()
    cached = _SAMPLES_CACHE["data"]
    if cached is not None and not fresh and now - _SAMPLES_CACHE["at"] < 10:
        return JSONResponse(cached)
    data = await run_in_threadpool(_sample_progress)
    _SAMPLES_CACHE["at"] = now
    _SAMPLES_CACHE["data"] = data
    return JSONResponse(data)


@app.get("/api/pool")
async def api_pool():
    # 前端每 10 秒轮询一次，顺手在这里做离线自愈：号池后起也能自己从「离线」翻成在线
    await POOL.ensure_online()
    return JSONResponse(POOL.snapshot())


@app.post("/api/pool/refresh")
async def api_pool_refresh():
    await POOL.refresh_env_ids()
    return JSONResponse(POOL.snapshot())


# 并行数：默认 3，可调，上限 = 可用账号数。调完立即生效（不重启）
class WorkersBody(BaseModel):
    workers: int = 0


@app.post("/api/pool/workers")
async def api_pool_workers(body: WorkersBody):
    res = await POOL.set_workers(body.workers)
    # 带一份最新快照回去，前端不用再等下一轮轮询
    res["snapshot"] = {"workers": POOL.workers, "workers_max": POOL.workers_cap(),
                       "queued": POOL.snapshot()["queued"]}
    return JSONResponse(res)


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


# ---------- 号池出口代理（受限网络必填，默认直连） ----------
@app.get("/api/pool/proxy")
async def api_pool_proxy():
    """读取本机号池的出口代理配置（admin 透传，浏览器不碰 key）。"""
    res = await pool_admin.get_pool_proxy()
    # 顺带回传工作台侧记录的「自动探测」标记，让前端区分手动/自动
    try:
        home = pool_service.view()["home"]
        res["proxy_auto_url"] = pool_service._read_state(home).get("proxy_auto_url") or ""
    except Exception:
        res["proxy_auto_url"] = ""
    return JSONResponse(res)


@app.get("/api/pool/egress")
async def api_pool_egress(fresh: int = 0):
    """探测当前设备实际可用的出口代理（只读，不改号池配置）。

    生图必须过代理，所以面板里这一栏永远展示本机真实情况：换电脑、换代理
    端口，反显的值跟着变，不会停在某个写死的地址上。
    """
    try:
        data = await asyncio.to_thread(
            pool_service.detect_egress, 0 if fresh else 20.0)
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)[:160]})
    return JSONResponse(data)


@app.post("/api/pool/proxy")
async def api_pool_proxy_save(body: dict):
    """保存本机号池的出口代理配置。默认直连；受限网络由用户填自己的代理。"""
    return JSONResponse(await pool_admin.set_pool_proxy(body or {}))


@app.post("/api/pool/proxy/test")
async def api_pool_proxy_test(body: dict):
    """用号池已保存的代理测连通性。"""
    return JSONResponse(await pool_admin.test_pool_proxy(str((body or {}).get("url", ""))))


@app.post("/api/pool/accounts/refresh")
async def api_pool_accounts_refresh():
    """换出口代理后重算额度：让号池重新拉一遍每个账号的可用次数。"""
    return JSONResponse(await pool_admin.refresh_accounts())


@app.get("/api/pool/accounts/refresh/{progress_id}")
async def api_pool_accounts_refresh_progress(progress_id: str):
    """刷新进度：total / processed / done / status_counts / total_quota。"""
    return JSONResponse(await pool_admin.refresh_progress(progress_id))


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


@app.post("/api/pool/use-builtin")
async def api_pool_use_builtin():
    """一键切回本工作台自带的内置号池（38651）并启动它。

    典型场景：出图接口曾指向外部号池 / 写作台号池（如 38650），想切回来。
    只动 config.local.json 与内置池的停止标记，不碰外部号池的账号与进程。
    注意必须同时把 api_key 对齐到内置池：只改 base_url 不换 key，会用旧外部
    key 去打内置池的 /v1，结果 401，账号面板照样刷不出内置号。
    """
    home = Path.home() / ".dsh" / "chatgpt2api-image"
    builtin_url = "http://127.0.0.1:38651/v1"
    # 读内置池自带的用户 Key，写进 config，确保接口密钥与内置池对齐
    user_key = pool_mod._read_pool_user_key({"pool_dir": str(home)}) or ""
    if not user_key:
        try:
            user_key = pool_service.ensure_admin_key(home) or ""
        except Exception:
            user_key = ""
    cfg = {}
    if POOL_CONFIG_FILE.exists():
        try:
            cfg = json.loads(POOL_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["api_base_url"] = builtin_url
    cfg["pool_base_url"] = builtin_url
    cfg["api_key"] = user_key
    cfg["pool_dir"] = str(home)
    cfg["monitor_mode"] = "auto"
    cfg.setdefault("port", 8092)
    POOL_CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 清掉手动停止标记，让内置池能被启动 / 守护自动拉起
    try:
        (home / pool_service.STOP_FLAG).unlink(missing_ok=True)
    except Exception:
        pass
    # 启动内置池（首次建运行时可能要几十秒，丢线程池不堵事件循环）
    res = await run_in_threadpool(pool_service.start)
    await POOL.refresh_env_ids()
    await POOL.refresh_accounts(force=True)
    return JSONResponse({
        "ok": bool(res.get("ok", True)),
        "started": bool(res.get("ok", True)),
        "message": res.get("message", ""),
        "config": _cfg_view(),
    })


# ---------- 接口配置历史（用过的号池 / 接口，一键切回来） ----------
# 切接口最容易翻车的是三件套不同步：Base URL 换了、用户 Key 还是上一个池的、
# pool_dir 也没跟着走，/api/accounts 立刻 401。所以档案必须整套管参数一起存，
# 切换时一起恢复，不能只记个地址让用户自己补 Key。
# 档案随 config.local.json 走，明文 Key 只落本机（这个文件本来就不随插件分发），
# 对外视图一律脱敏。图片保存目录、微信推送这些全局设置不跟接口走，不进档案。
PROFILE_KEYS = (
    "api_base_url", "pool_base_url", "pool_dir", "api_key", "provider_name",
    "model_image", "model_text", "supports_edits", "edits_style",
    "image_response_format", "monitor_mode",
)
PROFILE_MAX = 12


def _profile_id(base_url: str) -> str:
    return hashlib.sha1(norm_base_url(str(base_url or "")).encode("utf-8")).hexdigest()[:12]


def _profile_values(cfg: dict) -> dict:
    """从完整配置里抽出接口相关的键；密钥取实际生效值（含号池目录兜底）。"""
    v = {k: cfg.get(k) for k in PROFILE_KEYS if cfg.get(k) is not None}
    try:
        v["api_key"] = provider_cfg(cfg)["api_key"]
    except Exception:
        pass
    base = norm_base_url(str(cfg.get("api_base_url") or cfg.get("pool_base_url") or ""))
    v["api_base_url"] = base
    v["pool_base_url"] = base
    # 手填地址切池时 pool_dir 常常没跟着换（38650 却还指着生图池目录）。
    # 档案里按端口校正一次，避免把这套脏参数固化，日后切回来又 401。
    try:
        from urllib.parse import urlparse
        port = str(urlparse(base).port or "")
        mapped = pool_mod.LOCAL_POOL_PORTS.get(port)
        if mapped:
            d = pool_mod._dsh_home() / mapped
            if (d / "admin-key").exists() or (d / "user-api-keys.json").exists():
                v["pool_dir"] = str(d)
    except Exception:
        pass
    return v


def _profile_name(cfg: dict, v: dict) -> str:
    n = str(cfg.get("provider_name") or "").strip()
    # 旧版默认名「生图接口」和现行「当前接口」都不该当档案名，改成用地址识别
    if n and n not in ("生图接口", "当前接口"):
        return n[:40]
    base = str(v.get("api_base_url") or "")
    try:
        from urllib.parse import urlparse
        return (urlparse(base).netloc or base or "未命名接口")[:40]
    except Exception:
        return (base or "未命名接口")[:40]


def _profiles(cfg: dict) -> list[dict]:
    raw = cfg.get("provider_profiles")
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict) and x.get("id") and isinstance(x.get("values"), dict)]


def _upsert_profile(cfg: dict, values: dict | None = None, name: str = "") -> list[dict]:
    """把一套接口配置记进历史；同地址只留一份，最近用过的排最前。"""
    v = dict(values or _profile_values(cfg))
    base = str(v.get("api_base_url") or "").strip()
    if not base:
        return _profiles(cfg)
    pid = _profile_id(base)
    items = _profiles(cfg)
    hit = next((x for x in items if x.get("id") == pid), None)
    now = int(time.time())
    if hit:
        merged = dict(hit.get("values") or {})
        # 空值不覆盖已有值：API Key 留空是「沿用」，别把历史里的 Key 抹掉
        merged.update({k: val for k, val in v.items() if val not in (None, "")})
        hit["values"] = merged
        hit["name"] = name or hit.get("name") or _profile_name(cfg, merged)
        hit["base_url"] = base
        hit["last_used"] = now
        items.remove(hit)
        items.insert(0, hit)
    else:
        items.insert(0, {
            "id": pid, "name": name or _profile_name(cfg, v), "base_url": base,
            "values": v, "last_used": now, "created": now,
        })
    return items[:PROFILE_MAX]


def _seed_profiles(cfg: dict) -> list[dict]:
    """首次启用历史：当前这套 + 本机已知的两个号池端口一次补齐。

    过去没记过档案，但 38650 / 38651 的用户 Key 都在各自数据目录里，
    直接读出来就能让下拉框一开始就有得切，不用用户回去翻旧配置。
    """
    items = _upsert_profile(cfg)
    cur = _active_profile_id(cfg)
    now = int(time.time())
    for port, dirname in pool_mod.LOCAL_POOL_PORTS.items():
        d = pool_mod._dsh_home() / dirname
        if not (d / "user-api-keys.json").exists():
            continue
        base = norm_base_url(f"http://127.0.0.1:{port}/v1")
        pid = _profile_id(base)
        if pid == cur or any(x.get("id") == pid for x in items):
            continue
        seed = {
            "api_base_url": base, "pool_base_url": base, "pool_dir": str(d),
            "provider_name": ("本地写作号池" if port == "38650" else "本地生图号池"),
            "model_image": str(cfg.get("model_image") or "gpt-image-2"),
            "model_text": str(cfg.get("model_text") or "auto"),
            "supports_edits": bool(cfg.get("supports_edits", True)),
            "edits_style": str(cfg.get("edits_style") or "auto"),
            "image_response_format": str(cfg.get("image_response_format") or "auto"),
            "monitor_mode": "pool",
        }
        key = pool_mod._read_pool_user_key(seed)
        if not key:
            continue
        seed["api_key"] = key
        items.append({
            "id": pid, "name": seed["provider_name"], "base_url": base,
            "values": seed, "last_used": 0, "created": now,
        })
    return items[:PROFILE_MAX]


def _profile_view_item(x: dict) -> dict:
    v = x.get("values") or {}
    key = str(v.get("api_key") or "")
    masked = (key[:6] + "***" + key[-4:]) if len(key) > 12 else ("***" if key else "")
    return {
        "id": x.get("id"), "name": x.get("name") or "未命名接口",
        "base_url": x.get("base_url") or "", "last_used": int(x.get("last_used") or 0),
        "api_key_set": bool(key), "api_key_masked": masked,
        "model_image": str(v.get("model_image") or ""),
        "pool_dir": str(v.get("pool_dir") or ""),
    }


def _active_profile_id(cfg: dict) -> str:
    return _profile_id(str(cfg.get("api_base_url") or cfg.get("pool_base_url") or ""))


def _read_cfg_file() -> dict:
    try:
        if POOL_CONFIG_FILE.exists():
            got = json.loads(POOL_CONFIG_FILE.read_text(encoding="utf-8"))
            return got if isinstance(got, dict) else {}
    except Exception:
        pass
    return {}


def _write_cfg_file(cfg: dict) -> None:
    POOL_CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
        "version": VERSION,
        "active_profile_id": _profile_id(p["base_url"]),
        # 自动清理失效号：默认开，可在「本地号池」页签关掉
        "auto_purge_dead": (lambda v: v is not False and str(v).lower() not in ("0", "false", "no", "off"))(
            load_config().get("auto_purge_dead", True)),
        # 微信公众号推送：网关地址可明文回显；Token 只回脱敏，绝不回传完整值
        "wx_gateway": load_config().get("wx_gateway") or "",
        "wx_push_mode": (load_config().get("wx_push_mode") or "news").strip().lower(),
        "wx_token_set": bool(load_config().get("wx_token")),
        "wx_token_masked": (lambda t: (t[:3] + "***" + t[-3:]) if len(t) > 8 else "***")(load_config().get("wx_token") or ""),
    }


@app.get("/api/config")
async def api_config_get():
    return JSONResponse(_cfg_view())


@app.post("/api/config")
async def api_config_save(body: dict):
    cfg = _read_cfg_file()

    def put(k: str, v) -> None:
        cfg[k] = v

    # 先把「保存前」这套接口存档：第一次换池时，旧池的 Key / 数据目录不能丢
    cfg["provider_profiles"] = _upsert_profile(cfg)

    if "app_name" in body:
        # 只影响显示名：留空 = 回到内置默认名
        put("app_name", str(body["app_name"] or "").strip()[:40])
    if "provider_name" in body:
        put("provider_name", str(body["provider_name"]).strip() or "当前接口")
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
    # 微信公众号推送：网关地址与 Token，留空表示不推送；Token 留空 = 沿用已保存值
    if "wx_gateway" in body:
        put("wx_gateway", str(body["wx_gateway"]).strip())
    if body.get("wx_token"):
        put("wx_token", str(body["wx_token"]).strip())
    if "wx_push_mode" in body:
        m = str(body["wx_push_mode"]).strip().lower()
        put("wx_push_mode", m if m in ("news", "newspic") else "news")
    if "monitor_mode" in body:
        put("monitor_mode", str(body["monitor_mode"]).strip() or "auto")
    if "auto_purge_dead" in body:
        put("auto_purge_dead", bool(body["auto_purge_dead"]))
    cfg.setdefault("port", 8092)
    # 再把「保存后」这套存档（可能是个全新地址）
    cfg["provider_profiles"] = _upsert_profile(cfg)
    _write_cfg_file(cfg)
    # 立即重探，让前端保存后马上看到新状态
    await POOL.refresh_env_ids()
    await POOL.refresh_accounts(force=True)
    return JSONResponse(_cfg_view())


@app.get("/api/config/profiles")
async def api_config_profiles():
    """用过的接口 / 号池列表（脱敏）。老配置没有历史时拿当前这套种第一条。"""
    cfg = _read_cfg_file()
    items = _profiles(cfg)
    if not items:
        items = _seed_profiles(cfg)
        cfg["provider_profiles"] = items
        _write_cfg_file(cfg)
    return JSONResponse({
        "active_id": _active_profile_id(cfg),
        "items": [_profile_view_item(x) for x in items],
    })


@app.post("/api/config/profiles/switch")
async def api_config_profile_switch(body: dict):
    """整套恢复一份历史接口：Base URL、用户 Key、号池数据目录、模型、开关一起换。"""
    pid = str((body or {}).get("id") or "").strip()
    cfg = _read_cfg_file()
    hit = next((x for x in _profiles(cfg) if x.get("id") == pid), None)
    if not hit:
        raise HTTPException(404, "这份接口配置不存在")
    v = dict(hit.get("values") or {})
    raw = str(v.get("api_base_url") or v.get("pool_base_url") or "").strip()
    base = norm_base_url(raw) if raw else ""
    if not base:
        raise HTTPException(400, "这份配置缺接口地址")
    # 切走之前先把当前这套（含可能被手改过的 Key）存好
    cfg["provider_profiles"] = _upsert_profile(cfg)
    for k in PROFILE_KEYS:
        if k in v and v[k] not in (None, ""):
            cfg[k] = v[k]
    cfg["api_base_url"] = base
    cfg["pool_base_url"] = base
    cfg["provider_profiles"] = _upsert_profile(cfg)
    _write_cfg_file(cfg)
    await POOL.refresh_env_ids()
    await POOL.refresh_accounts(force=True)
    return JSONResponse(_cfg_view())


@app.post("/api/config/profiles/delete")
async def api_config_profile_delete(body: dict):
    pid = str((body or {}).get("id") or "").strip()
    cfg = _read_cfg_file()
    if pid == _active_profile_id(cfg):
        raise HTTPException(400, "正在用的这份不能删，先切到别的号池")
    old = _profiles(cfg)
    items = [x for x in old if x.get("id") != pid]
    if len(items) == len(old):
        raise HTTPException(404, "这份接口配置不存在")
    cfg["provider_profiles"] = items
    _write_cfg_file(cfg)
    return JSONResponse({"ok": True, "items": [_profile_view_item(x) for x in items]})


def _profile_detail(x: dict, cfg: dict) -> dict:
    """单份档案的可编辑详情。API Key 只回脱敏值，绝不回明文。"""
    v = dict(x.get("values") or {})
    key = str(v.get("api_key") or "")
    masked = (key[:6] + "***" + key[-4:]) if len(key) > 12 else ("***" if key else "")
    return {
        "id": x.get("id"), "name": x.get("name") or "未命名接口",
        "base_url": str(v.get("api_base_url") or x.get("base_url") or ""),
        "active": x.get("id") == _active_profile_id(cfg),
        "api_key_set": bool(key), "api_key_masked": masked,
        "model_image": str(v.get("model_image") or ""),
        "model_text": str(v.get("model_text") or ""),
        "pool_dir": str(v.get("pool_dir") or ""),
        "supports_edits": bool(v.get("supports_edits", True)),
        "edits_style": str(v.get("edits_style") or "auto"),
        "image_response_format": str(v.get("image_response_format") or "auto"),
        "last_used": int(x.get("last_used") or 0),
    }


def _profile_check_dir(raw: str) -> str:
    p = str(raw or "").strip()
    if p and not Path(os.path.expandvars(os.path.expanduser(p))).is_dir():
        raise HTTPException(400, "号池数据目录不存在：" + p)
    return p


@app.get("/api/config/profiles/{pid}")
async def api_config_profile_get(pid: str):
    """查看态用：取一份档案的完整可编辑字段（Key 脱敏）。"""
    cfg = _read_cfg_file()
    hit = next((x for x in _profiles(cfg) if x.get("id") == pid), None)
    if not hit:
        raise HTTPException(404, "这份接口配置不存在")
    return JSONResponse(_profile_detail(hit, cfg))


@app.post("/api/config/profiles")
async def api_config_profile_create(body: dict):
    """新增态用：只往历史里加一份档案，不改当前运行配置（新增 ≠ 启用）。"""
    b = body or {}
    name = str(b.get("name") or "").strip()[:40]
    base = norm_base_url(str(b.get("base_url") or "").strip())
    key = str(b.get("api_key") or "").strip()
    if not name:
        raise HTTPException(400, "请填名称（用于列表展示）")
    # norm_base_url 会补协议头与 /v1（既有约定），这里只拦空值；
    # 严格格式校验放在前端，避免把「少打 https://」这种正常输入误判成错误。
    if not base:
        raise HTTPException(400, "请填接口地址")
    if not key:
        raise HTTPException(400, "新增接口必须填 API Key")
    cfg = _read_cfg_file()
    items = _profiles(cfg)
    pid = _profile_id(base)
    if any(x.get("id") == pid for x in items):
        dup = next(x for x in items if x.get("id") == pid)
        raise HTTPException(409, "该地址已有档案：" + str(dup.get("name") or "未命名接口"))
    if any(str(x.get("name") or "") == name for x in items):
        raise HTTPException(409, "已有同名号池，换个名字")
    pool_dir = _profile_check_dir(str(b.get("pool_dir") or ""))
    v = {
        "api_base_url": base, "pool_base_url": base, "api_key": key,
        "provider_name": name, "pool_dir": pool_dir,
        "model_image": str(b.get("model_image") or "gpt-image-2").strip(),
        "model_text": str(b.get("model_text") or "auto").strip(),
        "supports_edits": bool(b.get("supports_edits", True)),
        "edits_style": str(b.get("edits_style") or "auto"),
        "image_response_format": str(b.get("image_response_format") or "auto"),
        "monitor_mode": "auto",
    }
    items = _upsert_profile(cfg, values=v, name=name)
    for x in items:
        if x.get("id") == pid:
            x["last_used"] = 0      # 没启用过，别在列表里冒充「最近使用」
    cfg["provider_profiles"] = items
    _write_cfg_file(cfg)
    return JSONResponse({"ok": True, "created_id": pid,
                         "active_id": _active_profile_id(cfg),
                         "items": [_profile_view_item(x) for x in items]})


@app.patch("/api/config/profiles/{pid}")
async def api_config_profile_patch(pid: str, body: dict):
    """编辑态用：只改这一份档案。改的是当前在用的那份时，同步顶层运行配置。"""
    b = body or {}
    cfg = _read_cfg_file()
    items = _profiles(cfg)
    hit = next((x for x in items if x.get("id") == pid), None)
    if not hit:
        raise HTTPException(404, "这份接口配置不存在")
    v = dict(hit.get("values") or {})
    name = str(b.get("name") if "name" in b else (hit.get("name") or "")).strip()[:40]
    raw_base = b.get("base_url") if "base_url" in b else (v.get("api_base_url") or hit.get("base_url") or "")
    base = norm_base_url(str(raw_base or "").strip())
    if not name:
        raise HTTPException(400, "请填名称（用于列表展示）")
    # norm_base_url 会补协议头与 /v1（既有约定），这里只拦空值；
    # 严格格式校验放在前端，避免把「少打 https://」这种正常输入误判成错误。
    if not base:
        raise HTTPException(400, "请填接口地址")
    new_pid = _profile_id(base)
    if new_pid != pid and any(x.get("id") == new_pid for x in items):
        dup = next(x for x in items if x.get("id") == new_pid)
        raise HTTPException(409, "该地址已有档案：" + str(dup.get("name") or "未命名接口"))
    if any(str(x.get("name") or "") == name and x.get("id") != pid for x in items):
        raise HTTPException(409, "已有同名号池，换个名字")
    if b.get("api_key"):        # 留空 = 沿用，绝不把已保存的 Key 清空
        v["api_key"] = str(b["api_key"]).strip()
    for k in ("model_image", "model_text", "edits_style", "image_response_format", "pool_dir"):
        if k in b:
            v[k] = str(b[k] or "").strip()
    if "supports_edits" in b:
        v["supports_edits"] = bool(b["supports_edits"])
    v["pool_dir"] = _profile_check_dir(v.get("pool_dir") or "")
    v["api_base_url"] = base
    v["pool_base_url"] = base
    v["provider_name"] = name
    hit.update({"values": v, "name": name, "base_url": base, "id": new_pid})
    was_active = pid == _active_profile_id(cfg)
    if was_active:
        for k in PROFILE_KEYS:
            if k in v and v[k] not in (None, ""):
                cfg[k] = v[k]
        cfg["api_base_url"] = base
        cfg["pool_base_url"] = base
    cfg["provider_profiles"] = items
    _write_cfg_file(cfg)
    if was_active:
        await POOL.refresh_env_ids()
        await POOL.refresh_accounts(force=True)
    return JSONResponse({"ok": True, "id": new_pid,
                         "active_id": _active_profile_id(cfg),
                         "items": [_profile_view_item(x) for x in items],
                         "config": _cfg_view() if was_active else None})


# ---------- 出图 / 文案模型下拉的候选来源 ----------
# 号池与 OpenAI 兼容接口都提供 /v1/models，实测本机 38650 会返回 auto、gpt-image-2
# 以及一批文本模型。候选必须从「当前正在编辑的那份档案」取，不能用全局在用的接口：
# 用户在编辑一份还没启用的档案时，模型列表跟着全局接口走会给出错误选项。
# 编辑态的 API Key 允许留空（沿用已保存值），密钥只能在后端补齐，绝不回传浏览器。
_IMAGE_MODEL_HINTS = ("image", "dall-e", "flux", "imagen", "seedream",
                      "wanx", "qwen-image", "kolors", "midjourney", "sd-")
_FALLBACK_IMAGE_MODELS = ("gpt-image-2", "gpt-image-1", "dall-e-3")
_FALLBACK_TEXT_MODELS = ("auto", "gpt-4o", "gpt-4o-mini", "gpt-5", "gpt-5-mini")


def _is_image_model(name: str) -> bool:
    low = str(name or "").lower()
    return any(k in low for k in _IMAGE_MODEL_HINTS)


@app.post("/api/config/models")
async def api_config_models(body: dict):
    """给编辑态的两个模型下拉取候选；接口不通时退回常用值，仍可手动输入。"""
    b = body or {}
    cfg = _read_cfg_file()
    pid = str(b.get("profile_id") or "").strip()
    explicit_base = "base_url" in b
    hit = next((x for x in _profiles(cfg) if x.get("id") == pid), None) if pid else None
    v = dict((hit or {}).get("values") or {})
    base = (str(b.get("base_url") or "").strip()
            or str(v.get("api_base_url") or (hit or {}).get("base_url") or ""))
    key = str(b.get("api_key") or "").strip() or str(v.get("api_key") or "")
    if not key and not hit:
        key = str(cfg.get("api_key") or "")
    # 表单里明确把地址清空了，别悄悄拿「当前在用的接口」去探，那会把别的号池
    # 模型塞进这份档案；整个 body 为空则是「查当前生效接口」，正常走全局配置。
    if not base and explicit_base:
        return JSONResponse({"ok": False, "source": "fallback", "all": [],
                             "message": "先填接口地址再读取模型",
                             "image": list(_FALLBACK_IMAGE_MODELS),
                             "text": list(_FALLBACK_TEXT_MODELS),
                             "image_extra": []})
    if not base:
        base = str(cfg.get("api_base_url") or cfg.get("pool_base_url") or "")
    ov = {"base_url": base}
    if key:
        ov["api_key"] = key
    r = await test_connection(ov)
    live = [str(m).strip() for m in (r.get("models") or []) if str(m).strip()]
    if live:
        images = [m for m in live if _is_image_model(m)]
        texts = [m for m in live if not _is_image_model(m)]
        source = "live"
        message = str(r.get("message") or "已读取接口模型列表")
        # 号池的 /v1/models 会按账号能力动态追加图像模型，普通账号池常常只列出
        # 一个 gpt-image-2，一个选项的下拉等于没得选。把常见图像模型作为「未列出」
        # 候选补上，但单独返回，前端必须分组注明「需上游支持」，不能冒充可用。
        image_extra = [m for m in _FALLBACK_IMAGE_MODELS if m not in images]
    else:
        images, texts = list(_FALLBACK_IMAGE_MODELS), list(_FALLBACK_TEXT_MODELS)
        source = "fallback"
        message = str(r.get("message") or "接口未返回模型") + "，已显示常用候选"
        image_extra = []
    # auto 是号池「自动挑号」的内部约定，永远排在文案候选第一位
    texts = (["auto"] if "auto" in texts else []) + [m for m in texts if m != "auto"]
    return JSONResponse({"ok": bool(r.get("ok")), "source": source, "message": message,
                         "image": images[:40], "text": texts[:40], "all": live[:60],
                         "image_extra": image_extra})


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


# ---------------- 小说拆最小单元 ----------------
# 独立链路：不碰 splitDoc / splitToItems 那套老拆分。核心约束是「忠实原文」，
# 所以模型只给分页的句子编号区间与画面表演要点，正文由程序按编号从原文逐字截取，
# 模型根本没有改写正文的机会。AI 不可用时降级成按句窗口的保守拆分，并如实标记 degraded。
NOVEL_MIN_CHARS = 4
NOVEL_MAX_CHARS = 8000
NOVEL_MAX_PAGES = 120
_SENT_END = "。！？!?…"
_QUOTE_TAIL = "」』”\"’')）】"


class NovelSplitRequest(BaseModel):
    text: str = ""
    title: str = ""


def _novel_clean(s, limit: int) -> str:
    """模型字段统一清洗：去 markdown / 破折号 / 弯引号 / 多余空白。"""
    t = str(s or "").strip()
    t = t.replace("**", "").replace("`", "").replace("#", "")
    t = t.replace("——", "，").replace("—", "，").replace("–", "，")
    t = t.replace("“", "「").replace("”", "」").replace("‘", "「").replace("’", "」")
    t = re.sub(r"\s+", " ", t).strip().strip('"\'')
    return t[:limit].strip()


def _novel_sentences(text: str) -> list[str]:
    """按句末标点 / 换行切原文，保留全部字符（拼接后与原文逐字相同）。

    纯空白段并进上一句，不单独占编号，免得给模型的编号里夹一堆空行。
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        buf.append(c)
        cut = False
        if c in _SENT_END:
            while i + 1 < n and text[i + 1] in _QUOTE_TAIL:
                i += 1
                buf.append(text[i])
            cut = True
        elif c == "\n":
            cut = True
        if cut:
            seg = "".join(buf)
            if not seg.strip() and out:
                out[-1] += seg
            else:
                out.append(seg)
            buf = []
        i += 1
    if buf:
        seg = "".join(buf)
        if not seg.strip() and out:
            out[-1] += seg
        else:
            out.append(seg)
    return out


def _novel_autotitle(unit: str, limit: int = 14) -> str:
    t = re.sub(r"\s+", "", str(unit or ""))
    t = re.split(r"[。！？!?…，,；;：:]", t)[0] or t
    return t[:limit] or "这一页"


def _novel_local_split(text: str) -> list[dict]:
    """AI 不可用时的保守拆分：按 120 到 180 字窗口合并原句，正文逐字取自原文，画面要点留空。"""
    sents = _novel_sentences(text)
    pages: list[dict] = []
    a = 0
    while a < len(sents):
        b, size = a, 0
        while b < len(sents):
            ln = len(sents[b])
            if size and size + ln > 180:
                break
            size += ln
            b += 1
            if size >= 120:
                break
        if b <= a:
            b = a + 1
        body = "".join(sents[a:b]).strip()
        if body:
            pages.append({"title": _novel_autotitle(body), "body": body, "visual": ""})
        a = b
    return pages


def _novel_prompt(text: str, sents: list[str], title: str) -> str:
    numbered = "\n".join(f"[{i + 1}] {s.strip()}" for i, s in enumerate(sents))
    return (
        "你是漫画分镜编辑。下面是一篇连贯小说的原文，已经按句编号。请通读全文后把它拆成一页页分镜，"
        "每页是一个最小完整表达。只输出一个 JSON 对象，不要解释、不要代码块。\n\n"
        "【输出格式】\n"
        '{"pages":[{"from":1,"to":3,"title":"本页小标题","visual":"本页画面与表演要点"}]}\n'
        "- from / to 是句子编号，含两端。所有页必须连续且不重不漏：第一页 from=1，"
        "下一页 from = 上一页 to + 1，最后一页 to = 总句数。\n"
        "- 正文由程序按编号从原文逐字截取，你不要复述、不要改写、不要输出 body 字段。\n"
        "- title 从本页原文里摘 6 到 14 个字，不要编号、不要引号、不要新增原文没有的信息。\n"
        "- visual 用 60 到 120 字写清本页画面，不要标签式情绪词。\n"
        "- 顶层只输出 pages 一个字段。\n\n"
        "【六条制作准则，必须逐条落实】\n"
        "1. 先理解全文：结合人物关系、事件因果、情绪变化与信息揭露顺序，再安排分页和画面。"
        "核心目标是在忠实于原文的前提下，让读者读得顺、看得懂、情绪接得上，翻页有意义；"
        "文字讲清经过，配图表现重点。\n"
        "2. 每页要形成最小完整表达，或承担明确的情绪、期待与停顿作用。"
        "不要把引出动作和实际内容机械拆开，也不要把多个需要强调的重要变化挤在同一页。"
        "页数与每页字数服从实际表达，不机械凑数。\n"
        "3. 画面抓住最有表现力的瞬间：先确定本页要让读者理解什么、感受到什么，再选代表瞬间。"
        "优先抓人物做出决定、动作发生、受到触动或反应显露的时刻，让画面准确承接本页文字。\n"
        "4. 人物情绪、表情、动作必须准确、鲜明、看得见。根据原文支持的情绪及强度，"
        "把反应落实到具体的眉眼、嘴部、视线、手部、肩背、身体重心和人物间距离；"
        "动作写清对象、方向、接触关系和所处阶段，让人看出力度、迟疑或停顿；"
        "景别和取景要让这些细节看得见。克制、沉默的场面也要体现内在变化。"
        "表现强度符合原文，不擅自增加哭喊、摔物、攻击等原文没有的行为。"
        "禁止只写生气、难过、震惊这类标签。\n"
        "5. 表演和情绪要连续、有变化：文字、画面与翻页共同服务本页重点，"
        "保持人物、物件和空间状态连续。相邻页要体现情绪的积累、变化或释放，"
        "避免反复使用相同站姿、表情和动作，不提前揭露后文信息。\n"
        "6. 定稿前连续回看相邻页，检查零碎页、拥挤页、图文不对应、阅读顺序问题；"
        "检查关键情绪能否从表演看出来、动作是否明确、力度是否合情境、"
        "连续页是否只是换了文字却重复同一种状态。不达标的页在原文范围内重新选瞬间、"
        "调整取景或补足可见细节。\n\n"
        "【硬性约束】\n"
        "- 严禁改写、概括、扩写、删减原文，严禁调换事件顺序，严禁提前透露后文。\n"
        "- 不要破折号、不要弯引号（用「」）、不要 markdown、不要 emoji、不要碎片化短句。\n\n"
        f"整组标题（可空）：{(title or '（无）').strip()}\n"
        f"原文共 {len(sents)} 句：\n{numbered}"
    )


def _novel_build_pages(raw: str, text: str, sents: list[str]) -> list[dict]:
    """解析模型的分页区间，正文按编号从原文逐字拼回，顺带做覆盖率校验。"""
    raw = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", (raw or "").strip())
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        d = json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise ValueError("模型没有返回 JSON")
        d = json.loads(m.group(0))
    rows = d.get("pages") if isinstance(d, dict) else (d if isinstance(d, list) else None)
    if not isinstance(rows, list) or not rows:
        raise ValueError("模型没有给出分镜页")
    if len(rows) > NOVEL_MAX_PAGES:
        raise ValueError("分镜页数过多")
    n = len(sents)
    pages: list[dict] = []
    prev = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            a, b = int(row.get("from")), int(row.get("to"))
        except Exception:
            continue
        a = max(prev + 1, min(a, n))
        b = max(a, min(b, n))
        if a > n:
            continue
        body = "".join(sents[a - 1:b]).strip()
        if not body:
            continue
        pages.append({
            "title": _novel_clean(row.get("title"), 20) or _novel_autotitle(body),
            "body": body,
            "visual": _novel_clean(row.get("visual"), 220),
        })
        prev = b
    if prev < n:
        body = "".join(sents[prev:]).strip()
        if body:
            pages.append({"title": _novel_autotitle(body), "body": body, "visual": ""})
    if not pages:
        raise ValueError("分镜页为空")
    joined = re.sub(r"\s+", "", "".join(p["body"] for p in pages))
    origin = re.sub(r"\s+", "", text)
    if len(joined) < len(origin) * 0.92:
        raise ValueError("分镜没有覆盖全文")
    return pages


@app.post("/api/novel/split")
async def api_novel_split(req: NovelSplitRequest):
    text = (req.text or "").strip()
    n = len(text)
    if n < NOVEL_MIN_CHARS:
        return JSONResponse({"ok": False, "error": f"小说太短，至少 {NOVEL_MIN_CHARS} 个字"}, status_code=400)
    if n > NOVEL_MAX_CHARS:
        return JSONResponse({"ok": False, "error": f"小说超过 {NOVEL_MAX_CHARS} 字，请先删减到 8000 字以内"}, status_code=400)
    sents = _novel_sentences(text)
    if not sents:
        return JSONResponse({"ok": False, "error": "没有可拆分的正文"}, status_code=400)
    title = (req.title or "").strip()[:60]
    err = ""
    try:
        raw = await pool_mod.chat_once(
            _novel_prompt(text, sents, title),
            max_tokens=min(12000, 3600 + n * 2),
            temperature=0.35,
            # 长文实测 7900 字约 122 秒；按字数留余量，避免临界超时掉进保守拆分
            timeout=min(300, 120 + n // 25),
        )
        pages = _novel_build_pages(raw, text, sents)
        degraded = False
    except Exception as e:
        pages = _novel_local_split(text)
        degraded = True
        err = str(e)[:160]
    return JSONResponse({
        "ok": True, "degraded": degraded, "pages": pages,
        "chars": n, "sentences": len(sents), "error": err,
    })


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
        "ref_mode": req.ref_mode,
        "image_mode": req.image_mode,
        "shoot_type": req.shoot_type,
        "instruction": req.instruction,
        "allow_blank": req.allow_blank,
        "items": [{"title": it.title, "body": it.body, "scene": it.scene,
                   "visual": it.visual} for it in req.items],
    }
    return JSONResponse(await composer.compose(payload, CATALOG["styles"], CATALOG["layouts"], CATALOG["colors"]))


@app.post("/api/autoscene")
async def api_autoscene(req: ComposeRequest):
    """只回填「中景小道具」：给所有空 scene 的条目挑一件实物道具，不拼完整提示词。

    供「拆分镜」后立刻同步显示用——用户不必自己填，生成提示词前就能看见每一条挑了什么。
    大模型批量挑，失败静默回退关键词规则，返回的 scene 数量与入参 items 一一对应。
    """
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
        "ref_mode": req.ref_mode,
        "image_mode": req.image_mode,
        "shoot_type": req.shoot_type,
        "instruction": req.instruction,
        "allow_blank": req.allow_blank,
        "items": [{"title": it.title, "body": it.body, "scene": it.scene,
                   "visual": it.visual} for it in req.items],
    }
    scenes = await composer.fill_scenes(payload["items"], bool(payload["fusion"]))
    return JSONResponse({"items": [{"scene": s} for s in scenes]})


@app.post("/api/preview")
async def api_preview(req: GenRequest):
    story = await _text_story(req)
    if req.mode == "text":
        prompt = build_text_prompt(req, story)
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
    # 号池未就绪：单接口模式本就不拉账号，放行（由真实请求暴露错误）；
    # 号池 / auto 模式账号为空时，给出明确指引而非笼统的「pool is empty」。
    if not POOL.accounts and provider_cfg().get("monitor_mode") != "single":
        cfg = load_config()
        base = provider_cfg(cfg).get("base_url") or ""
        user_set = bool(cfg.get("api_base_url"))
        if not base:
            raise HTTPException(
                400,
                "尚未配置接口。请在「接口与号池」面板填写接口地址与 API Key 后再生成。")
        if not user_set:
            raise HTTPException(
                400,
                "尚未配置接口：当前仍指向内置空号池。请在「接口与号池」面板填写你自己的接口地址"
                "（例如 http://127.0.0.1:38650/v1）与 API Key，或先启动内置号池并导入账号。")
        raise HTTPException(
            400,
            f"号池 {base} 已配置但暂无可用账号。请确认号池进程已启动并已导入账号；"
            "可在「接口与号池」面板点「测试」验证连通性。")
    if req.mode == "text":
        prompt = build_text_prompt(req, await _text_story(req))
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
        "ref_mode": (req.ref_mode or "style") if ref_path else "",
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


# ============================================================
# 微信公众号「图文消息」草稿箱推送（news 类型，带正文）
# 每张图是一屏：正文图经 /uploadimg 拿到 url 嵌进 content；
# 首图经 /material/add 拿 media_id 当封面 thumb_media_id；
# access_token 走 /token，草稿走 /draft/add?access_token=。
# 推送前对每张图反复确认落盘（6×1.5s），解决「4 张只推 3 张」的竞态。
# ============================================================
# 默认不内置任何网关 / Token：必须在「接口与号池」配置页由用户自己填写，
# 避免凭据随插件包（GitHub/npm）公开，导致他人装了也能往你的公众号草稿箱推。
WX_GATEWAY = ""
WX_TOKEN = ""

class PushDraftItem(BaseModel):
    job_id: str
    caption: str = ""   # 正文
    title: str = ""     # 小标题（每张分镜的标题，图文模式会渲染为带色条的 h2）

class PushDraftRequest(BaseModel):
    title: str = ""
    mode: str = "news"  # "news" 图文消息（带正文+小标题） / "newspic" 贴图（纯图片）
    items: list[PushDraftItem] = []

def _wx_cfg() -> tuple[str, str]:
    cfg = load_config()
    gw = str(cfg.get("wx_gateway") or "").strip()
    if gw and "://" not in gw:        # 配置页可能只填了域名，自动补 https://
        gw = "https://" + gw
    return (gw, str(cfg.get("wx_token") or "").strip())

def _wx_escape(s: str) -> str:
    return html.escape(str(s or ""), quote=False)

def _wx_resize(raw: bytes, max_edge: int = 1080) -> bytes:
    """微信正文图压成 1080 长边的 JPEG（q82）。最长边都不超 1080 时跳过缩放。"""
    from io import BytesIO
    from PIL import Image, ImageOps
    im = Image.open(BytesIO(raw))
    im = ImageOps.exif_transpose(im).convert("RGB")
    w, h = im.size
    if max(w, h) > max_edge:
        if w >= h:
            im = im.resize((max_edge, int(h * max_edge / w)), Image.LANCZOS)
        else:
            im = im.resize((int(w * max_edge / h), max_edge), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, "JPEG", quality=82)
    return buf.getvalue()

async def _wx_confirm_on_disk(job_id: str) -> Path | None:
    """反复确认图片真的落盘了再上传：6 次 × 1.5s，存在且 >1KB 才认。"""
    for _ in range(6):
        p = HISTORY.image_path(job_id)
        if p and p.is_file() and p.stat().st_size > 1024:
            return p
        await asyncio.sleep(1.5)
    return HISTORY.image_path(job_id)

async def _wx_post_media(gateway: str, token: str, img_bytes: bytes,
                         endpoint: str, retries: int = 3) -> str | None:
    """上传一张图到网关。uploadimg→返回 url（嵌正文）；material/add→返回 media_id（封面）。"""
    import httpx
    b64 = base64.b64encode(img_bytes).decode()
    payload = json.dumps({"media_b64": b64, "filename": "sticker.jpg",
                          "content_type": "image/jpeg"}).encode()
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0"}
    last_err = ""
    async with httpx.AsyncClient(trust_env=False, timeout=90) as c:
        for attempt in range(1, retries + 1):
            try:
                r = await c.post(gateway + "/" + endpoint, content=payload, headers=headers)
                d = r.json() if r.content else {}
                if endpoint == "uploadimg":
                    url = d.get("url") or d.get("media_url")
                    if url:
                        return url
                else:
                    mid = d.get("media_id") or d.get("mediaId")
                    if mid:
                        return mid
                last_err = f"{endpoint} 第{attempt}次未返回有效结果: {str(d)[:120]}"
            except Exception as e:
                last_err = f"{endpoint} 第{attempt}次异常: {e}"
            await asyncio.sleep(1.0)
    print("wx upload failed:", last_err)
    return None

async def _wx_get_token(gateway: str, token: str) -> str | None:
    import httpx
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.get(gateway + "/token",
                            headers={"Authorization": "Bearer " + token,
                                     "User-Agent": "Mozilla/5.0"})
            return (r.json() or {}).get("access_token")
    except Exception as e:
        print("wx token failed:", e)
        return None

# ---------- 图文消息（news）推送模板 ----------
# 每一屏 = 一个小标题（h2，带左侧色条）+ 正文段落 + 一张配图。
# 小标题来自每条分镜的 title 字段；若为空则只显示正文与图片，不出空标题。
# tail_html：首屏专用，在第一张图之后追加「内容要点」等区块。
def _wx_news_section(title: str, caption: str, url: str, tail_html: str = "") -> str:
    esc_title = _wx_escape((title or "").strip())
    esc_cap = _wx_escape((caption or "").strip())
    parts: list[str] = ['<section style="margin:0 0 22px;">']
    if esc_title:
        parts.append(
            '<h2 style="font-size:18px;font-weight:700;color:#1f9d6b;margin:0 0 10px;'
            'padding:4px 0 4px 12px;line-height:1.45;'
            'border-left:4px solid #1f9d6b;'
            'background:linear-gradient(90deg,rgba(31,157,107,.10),rgba(31,157,107,0));">'
            + esc_title + "</h2>")
    if esc_cap:
        parts.append('<p style="margin:0 0 14px;font-size:16px;line-height:1.85;'
                     'color:#3a3a3a;letter-spacing:.2px;">' + esc_cap + "</p>")
    parts.append('<p style="text-align:center;margin:6px 0 10px;">'
                 '<img src="' + url + '" style="max-width:100%;border-radius:10px;'
                 'box-shadow:0 2px 12px rgba(0,0,0,.08);"></p>')
    if tail_html:
        parts.append(tail_html)
    parts.append("</section>")
    return "".join(parts)


def _wx_lead_block(points: list[str]) -> str:
    """首图下方的「内容要点」区块：同款 h2 + 每条一个段落，紧扣事实、不抒情。"""
    h2 = ('<h2 style="font-size:18px;font-weight:700;color:#1f9d6b;margin:6px 0 10px;'
          'padding:4px 0 4px 12px;line-height:1.45;'
          'border-left:4px solid #1f9d6b;'
          'background:linear-gradient(90deg,rgba(31,157,107,.10),rgba(31,157,107,0));">'
          "内容要点</h2>")
    ps = []
    for pt in points[:5]:
        ps.append('<p style="margin:0 0 8px;font-size:15px;line-height:1.7;'
                  'color:#3a3a3a;letter-spacing:.2px;">' + _wx_escape(pt) + "</p>")
    return '<section style="margin:14px 0 4px;">' + h2 + "".join(ps) + "</section>"


def _wx_parse_points(raw: str) -> list[str]:
    """解析模型返回的要点 JSON 数组，清洗成带「· 」前缀的要点列表。"""
    raw = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", (raw or "").strip())
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        d = json.loads(raw)
    except Exception:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            return []
        try:
            d = json.loads(m.group(0))
        except Exception:
            return []
    if not isinstance(d, list) or not d:
        return []
    out = []
    for x in d:
        s = _wx_clean_line(x, 34)
        if s and re.search(r"[一-鿿]", s):
            out.append("· " + re.sub(r"^\s*·\s*", "", s))
    return out[:5]


def _wx_lead_fallback(screens: list[dict]) -> list[str]:
    """AI 不可用时的兜底：取前 3 屏正文第一句当作要点。"""
    out = []
    for sc in screens[:3]:
        c = re.sub(r"\s+", "", (sc.get("caption") or "").strip())
        if not c:
            continue
        first = re.split(r"(?<=[。！？!?])", c)[0].strip()[:32]
        if first and re.search(r"[一-鿿]", first):
            out.append("· " + first)
        if len(out) >= 3:
            break
    return out


async def _wx_lead_summary(topic: str, screens: list[dict]) -> list[str]:
    """从本期图文正文提炼 3-5 条事实要点，放在首图下方。AI 不可用/非法时降级。"""
    lines = []
    for i, sc in enumerate(screens[:12], 1):
        t = re.sub(r"\s+", " ", (sc.get("title") or "").strip())[:40]
        c = re.sub(r"\s+", " ", (sc.get("caption") or "").strip())[:200]
        if t or c:
            lines.append(f"{i}. 小标题：{t}｜正文：{c}")
    if not lines:
        return []
    prompt = (
        "你是公众号编辑，下面是一期图文的内容（各屏小标题与正文）。请提炼 3 到 5 条「内容要点」，"
        "放在首图下方给读者快速过一遍。\n"
        "要求：\n"
        "- 只说清楚事实，不抒情、不喊口号、不写金句、不复述小标题原话\n"
        "- 每条是一句完整的话，不超过 32 字，直接点出关键信息（数字、人物、结论）\n"
        "- 禁止空词大词：坚持、努力、重要、重视、关键、认知、思维、突破、赋能、打造、助力、"
        "旨在、标志着、大家都知道、我们要、一定要\n"
        "- 不要破折号、不要弯引号（用「」）、不要加粗、不要 emoji、不要 markdown\n"
        "- 只输出一个 JSON 数组，元素是字符串，不要解释，不要代码块\n\n"
        f"本期标题：{(topic or '').strip() or '（无）'}\n"
        "本期内容：\n" + "\n".join(lines)
    )
    try:
        raw = await pool_mod.chat_once(prompt, max_tokens=400, temperature=0.4, timeout=25)
        pts = _wx_parse_points(raw)
        if pts:
            return pts
    except Exception as e:
        print("wx lead summary AI failed:", e)
    return _wx_lead_fallback(screens)


_WX_FOLLOW = "觉得有用，点个「关注」，我会持续分享能落地的 AI 新技术、新玩法。"
_WX_BRAND_TAG = "#Bluxe的AI办公笔记"


def _wx_topic_key(topic: str, screens: list[dict]) -> str:
    """从文章标题或第一屏小标题里抠一个本期主题词，供兜底文案使用。"""
    raw = (topic or "").strip()
    if not raw and screens:
        raw = (screens[0].get("title") or "").strip()
    raw = re.sub(r"^\s*\d+\s*[｜|、.:：\-]\s*", "", raw)
    raw = re.sub(r"[\s#《》「」“”\"'，,。！!？?]+", "", raw)
    return raw[:12] or "这期内容"


def _wx_clean_line(s: str, limit: int) -> str:
    """清洗模型输出：去 markdown 残留、弯引号、破折号，压成单行并限长。"""
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(s or "")).strip()
    s = s.replace("**", "").replace("`", "")
    s = s.replace("“", "「").replace("”", "」").replace("‘", "「").replace("’", "」")
    s = s.replace("——", "，").replace("—", "，").replace("–", "，")
    s = re.sub(r"\s+", " ", s).strip().strip('"\'')
    return s[:limit]


def _wx_parse_footer(raw: str) -> dict:
    """解析模型给的结尾 JSON，字段不齐直接抛，交给调用方降级。"""
    raw = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", (raw or "").strip())
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        d = json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise ValueError("结尾文案不是 JSON")
        d = json.loads(m.group(0))
    if not isinstance(d, dict):
        raise ValueError("结尾文案不是对象")
    out = {
        "closing": _wx_clean_line(d.get("closing"), 90),
        "quote": _wx_clean_line(d.get("quote"), 50),
        "discuss": _wx_clean_line(d.get("discuss"), 60),
        "follow": _WX_FOLLOW,
    }
    tags = [t for t in dict.fromkeys(re.findall(r"#[^\s#]+", str(d.get("tags") or "")))][:6]
    if _WX_BRAND_TAG not in tags:
        tags.append(_WX_BRAND_TAG)
    out["tags"] = " ".join(tags)
    for k in ("closing", "quote", "discuss", "tags"):
        if not out[k] or not re.search(r"[一-鿿]", out[k]):
            raise ValueError("结尾文案字段缺失：" + k)
    return out


def _wx_footer_fallback(topic: str, screens: list[dict]) -> dict:
    """AI 不可用时的兜底：也紧扣本期主题，不再塞通用 AI 金句。"""
    key = _wx_topic_key(topic, screens)
    words: list[str] = []
    for sc in screens[:4]:
        t = re.sub(r"^\s*\d+\s*[｜|、.:：\-]\s*", "", (sc.get("title") or "").strip())
        t = re.sub(r"[\s#《》「」“”\"'，,。！!？?]+", "", t)[:8]
        if t and t not in words:
            words.append(t)
    tags = ["#" + w for w in words[:3]] + [_WX_BRAND_TAG]
    step = words[0] if words else key
    focus = words[1] if len(words) > 1 else key
    return {
        "closing": f"这期聊到的「{key}」，看着是小事，真上手才知道卡在哪一步。"
                   f"今晚先挑其中一条试一次，做过一遍，再回来看这几张图会有不一样的感受。",
        "quote": f"把「{step}」这一步做实了，比多收藏十篇教程管用。",
        "discuss": f"关于「{focus}」，你踩过哪一次坑？评论区说说，我挑几条写进下一期。",
        "follow": _WX_FOLLOW,
        "tags": " ".join(tags),
    }


async def _wx_footer_content(topic: str, screens: list[dict]) -> dict:
    """按这一期图文的标题和正文现写结尾；没配号池/限流/返回非法时静默降级。"""
    lines = []
    for i, sc in enumerate(screens[:12], 1):
        t = re.sub(r"\s+", " ", (sc.get("title") or "").strip())[:40]
        c = re.sub(r"\s+", " ", (sc.get("caption") or "").strip())[:160]
        lines.append(f"{i}. 小标题：{t}｜正文：{c}")
    if not any((sc.get("title") or sc.get("caption") or "").strip() for sc in screens):
        return _wx_footer_fallback(topic, screens)   # 纯图片没文字，别让模型瞎编
    prompt = (
        "你是公众号编辑，要给下面这期图文写结尾。只输出一个 JSON 对象，字段：\n"
        "closing：60 字以内的收尾感悟，紧扣本期具体主题，写一个具体场景或一个能上手的动作，"
        "可以带一点人生感悟或正能量，不要讲道理\n"
        "quote：30 字以内的一句金句，紧扣本期主题，口语、有画面\n"
        "discuss：35 字以内，一个引导读者在评论区讨论的具体问题，必须和本期主题相关\n"
        "tags：4 到 6 个与本期主题相关的中文话题标签，空格分隔，每个以 # 开头\n"
        "要求：\n"
        "- 只写本期内容里真实出现的东西。本期没讲 AI、工具、效率，就一个字都不要提它们\n"
        "- 禁止空词大词：坚持、努力、重要、重视、关键、认知、思维、突破、赋能、打造、助力、"
        "旨在、标志着、大家都知道、我们要、一定要\n"
        "- 不要破折号、不要弯引号（用「」）、不要加粗、不要 emoji、不要 markdown、"
        "不要碎片化短句、不要复述标题、不要写「以上就是本期内容」\n"
        "- 只输出 JSON，不要解释，不要代码块\n\n"
        f"本期标题：{(topic or '').strip() or '（无）'}\n"
        "本期分镜：\n" + "\n".join(lines)
    )
    try:
        raw = await pool_mod.chat_once(prompt, max_tokens=500, temperature=0.7, timeout=25)
        return _wx_parse_footer(raw)
    except Exception as e:
        print("wx footer AI failed:", e)
        return _wx_footer_fallback(topic, screens)


def _wx_news_footer(data: dict) -> str:
    """图文消息结尾：版式与正文每一屏完全一致（同款 h2 + 同款正文段落），仅 news 追加。"""
    h2 = ('<h2 style="font-size:18px;font-weight:700;color:#1f9d6b;margin:0 0 10px;'
          'padding:4px 0 4px 12px;line-height:1.45;'
          'border-left:4px solid #1f9d6b;'
          'background:linear-gradient(90deg,rgba(31,157,107,.10),rgba(31,157,107,0));">')
    p = ('<p style="margin:0 0 14px;font-size:16px;line-height:1.85;'
         'color:#3a3a3a;letter-spacing:.2px;">')
    p_gap = ('<p style="margin:22px 0 14px;font-size:16px;line-height:1.85;'
             'color:#3a3a3a;letter-spacing:.2px;">')
    p_end = ('<p style="margin:0;font-size:16px;line-height:1.85;'
             'letter-spacing:.2px;color:#1f9d6b;">')
    parts = ['<section style="margin:0 0 22px;">', h2, '写在最后', '</h2>']
    parts.append(p + _wx_escape(data["closing"]) + "</p>")
    parts.append(p + _wx_escape(data["quote"]) + "</p>")
    parts.append(p + "💬 " + _wx_escape(data["discuss"]) + "</p>")
    parts.append(p_gap + "⭐ " + _wx_escape(data["follow"]) + "</p>")
    parts.append(p_end + _wx_escape(data["tags"]) + "</p>")
    parts.append("</section>")
    return "".join(parts)


@app.post("/api/push_wechat_draft")
async def api_push_wechat_draft(req: PushDraftRequest):
    """把一批生成的图推到微信公众号草稿箱。

    mode="news"    → 图文消息（带正文+小标题，每屏一张图）
    mode="newspic" → 贴图（纯图片，无正文；每屏一张图，最多 20 张）
    """
    import httpx
    if not req.items:
        raise HTTPException(400, "没有要推送的图片")
    mode = (req.mode or "news").strip().lower()
    if mode not in ("news", "newspic"):
        raise HTTPException(400, "mode 只能是 news 或 newspic，收到：" + str(req.mode))
    if len(req.items) > 20:
        raise HTTPException(400, "微信一次最多 20 张，当前 " + str(len(req.items)))
    gateway, token = _wx_cfg()
    if not gateway or not token:
        return JSONResponse({"ok": False, "error": "未配置微信网关：请在「接口与号池」配置页填写微信网关地址和 Token"})

    # 先逐张确认落盘并压缩，得到本地图字节（后面两种模式复用）
    prepared: list[dict] = []   # {job_id, title, caption, jpg}
    errors: list[str] = []
    for it in req.items:
        p = await _wx_confirm_on_disk(it.job_id)
        if not p or not p.is_file():
            errors.append("找不到图片或尚未落盘: " + it.job_id)
            continue
        try:
            jpg = _wx_resize(p.read_bytes())
        except Exception as e:
            errors.append("压缩失败: " + it.job_id + " " + str(e)[:80])
            continue
        prepared.append({"job_id": it.job_id, "title": it.title or "",
                         "caption": it.caption or "", "jpg": jpg})
    if not prepared:
        return JSONResponse({"ok": False, "error": "没有成功读取任何图片", "errors": errors})

    if mode == "newspic":
        # 贴图：每张都要永久素材 media_id
        media_ids: list[str] = []
        for it in prepared:
            mid = await _wx_post_media(gateway, token, it["jpg"], "material/add", retries=3)
            if not mid:
                errors.append("贴图素材上传失败: " + it["job_id"])
                continue
            media_ids.append(mid)
        if not media_ids:
            return JSONResponse({"ok": False, "error": "没有成功上传任何贴图", "errors": errors})
        article = {
            "article_type": "newspic",
            "title": ((req.title or "AI 生成的贴图").strip() or "AI 生成的贴图")[:32],
            "image_info": {"image_list": [{"image_media_id": m} for m in media_ids]},
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }
        wx_token = await _wx_get_token(gateway, token)
        if not wx_token:
            return JSONResponse({"ok": False, "error": "无法获取微信 access_token", "errors": errors})
        payload = json.dumps({"articles": [article]}, ensure_ascii=False).encode()
        url = gateway + "/draft/add" + ("?access_token=" + wx_token)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
                r = await c.post(url, content=payload,
                                headers={"Authorization": "Bearer " + token,
                                         "Content-Type": "application/json",
                                         "User-Agent": "Mozilla/5.0"})
                return JSONResponse({"ok": True, "status": r.status_code, "mode": "newspic",
                                     "screens": len(media_ids), "pushed": len(media_ids),
                                     "body": r.text[:500], "errors": errors})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e), "errors": errors})

    # ---- news 图文消息 ----
    screens: list[dict] = []        # 每屏：{title, caption, url}
    thumb_media_id: str | None = None
    for idx, it in enumerate(prepared):
        url = await _wx_post_media(gateway, token, it["jpg"], "uploadimg", retries=3)
        if not url:
            errors.append("正文图上传失败: " + it["job_id"])
            continue
        screens.append({"title": it["title"], "caption": it["caption"], "url": url})
        if idx == 0:
            thumb_media_id = await _wx_post_media(gateway, token, it["jpg"], "material/add", retries=3)
    if not screens:
        return JSONResponse({"ok": False, "error": "没有成功上传任何图片", "errors": errors})
    if not thumb_media_id:
        return JSONResponse({"ok": False, "error": "封面 media_id 未取得（正文图已传完，material/add 失败）",
                             "errors": errors})
    # 拼带小标题+正文+插图的 HTML；结尾按本期文案现写，AI 不可用时自动降级
    # 首图下方追加「内容要点」：从本期正文提炼 3-5 条事实，直接说清关键信息
    lead_points = await _wx_lead_summary(req.title, screens)
    sections = []
    for i, sc in enumerate(screens):
        tail = _wx_lead_block(lead_points) if (i == 0 and lead_points) else ""
        sections.append(_wx_news_section(sc["title"], sc["caption"], sc["url"], tail_html=tail))
    footer_data = await _wx_footer_content(req.title, screens)
    parts = sections + [_wx_news_footer(footer_data)]
    content_html = (
        '<div style="font-size:16px;line-height:1.85;color:#3a3a3a;letter-spacing:.2px;'
        "font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Hiragino Sans GB',"
        "'Microsoft YaHei',sans-serif;padding:2px 2px 20px;\">"
        + "".join(parts) + "</div>"
    )
    first_title = _wx_escape((screens[0]["title"] or "").strip())
    first_cap = (screens[0]["caption"] or "").strip()
    digest = ((first_title + "：" if first_title else "") + first_cap)[:64]
    title = ((req.title or "AI 生成的图文").strip() or "AI 生成的图文")[:32]
    wx_token = await _wx_get_token(gateway, token)
    if not wx_token:
        return JSONResponse({"ok": False, "error": "无法获取微信 access_token", "errors": errors})
    article = {
        "title": title,
        "author": "Bluxe",
        "digest": digest,
        "content": content_html,
        "thumb_media_id": thumb_media_id,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    payload = json.dumps({"articles": [article]}, ensure_ascii=False).encode()
    url = gateway + "/draft/add" + ("?access_token=" + wx_token)
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=30) as c:
            r = await c.post(url, content=payload,
                            headers={"Authorization": "Bearer " + token,
                                     "Content-Type": "application/json",
                                     "User-Agent": "Mozilla/5.0"})
            return JSONResponse({"ok": True, "status": r.status_code, "mode": "news",
                                 "screens": len(screens), "pushed": len(screens),
                                 "body": r.text[:500], "errors": errors})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "errors": errors})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8020, log_level="info")
