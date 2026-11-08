"""本地版访问控制：单机 127.0.0.1 单人使用，不需要验证码审批。

保留与 VPS 版相同的接口签名，让 app.py 一行都不用改就能加载。
VPS 原件已备份为 access.py.vps_original（其顶层 import fcntl 在 Windows 不存在）。
"""
from __future__ import annotations

SESSION_TTL = 7 * 86400
LOCAL_MODE = True


def check_session(token: str) -> bool:
    """本地版没有会话概念，永远放行。"""
    return True


def request_code(email: str, ip: str, ua: str = "") -> dict:
    return {"ok": True, "msg": "本地版无需申请，请直接返回工作台"}


def verify_code(email: str, code: str, ip: str) -> dict:
    return {"ok": True, "msg": "本地版无需验证码", "token": "local"}


def master_login(password: str, ip: str) -> dict:
    return {"ok": True, "msg": "本地版无需口令", "token": "local"}
