"""后台控制台鉴权：口令哈希 + 会话令牌 + 失败锁定。

要点：
  · 口令只存 PBKDF2-HMAC-SHA256 哈希，不存明文。
  · 首次启动自动生成 16 位强口令，写入 data/admin_initial_password.txt
    （登录后建议改密并删除该文件），同时打到启动日志。
  · 会话令牌只存内存，12 小时过期；服务重启即失效（安全优先于便利）。
  · 同一来源连续 5 次失败锁定 60 秒。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from app.paths import DATA_DIR

CRED_FILE = Path(os.getenv("ADMIN_CRED_FILE", str(DATA_DIR / "admin_credentials.json")))
INITIAL_PW_FILE = Path(os.getenv("ADMIN_INITIAL_PW_FILE", str(DATA_DIR / "admin_initial_password.txt")))

ITERATIONS = 200_000
SESSION_TTL = int(os.getenv("ADMIN_SESSION_TTL", str(12 * 3600)))
MAX_FAILS = 5
LOCK_SECONDS = 60

_LOCK = threading.RLock()
_SESSIONS: dict[str, float] = {}
_FAILS: dict[str, tuple[int, float]] = {}


# ---------- 口令 ----------
def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS
    ).hex()


def _write_cred(password: str) -> dict:
    salt = secrets.token_hex(16)
    rec = {"salt": salt, "hash": _hash(password, salt), "iterations": ITERATIONS,
           "updated_at": int(time.time())}
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    CRED_FILE.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CRED_FILE, 0o600)
    except OSError:
        pass
    return rec


def init_credentials() -> str | None:
    """确保凭据存在。返回首次生成的明文口令（仅首次返回，用于提示管理员）。"""
    env_pw = os.getenv("ADMIN_PASSWORD", "").strip()
    if env_pw:
        if not CRED_FILE.is_file():
            _write_cred(env_pw)
        return None
    if CRED_FILE.is_file():
        return None
    pw = secrets.token_urlsafe(12)  # 16 位
    _write_cred(pw)
    INITIAL_PW_FILE.write_text(
        "后台初始口令（首次登录后请修改并删除本文件）\n"
        f"口令：{pw}\n"
        "登录地址：http://127.0.0.1:8899/admin/\n",
        encoding="utf-8",
    )
    return pw


def verify_password(password: str) -> bool:
    if not CRED_FILE.is_file():
        init_credentials()
    try:
        rec = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = rec.get("hash", "")
    got = _hash(password or "", rec.get("salt", ""))
    return secrets.compare_digest(expected, got)


def change_password(old: str, new: str) -> tuple[bool, str]:
    if not verify_password(old):
        return False, "原口令不正确"
    if len(new or "") < 8:
        return False, "新口令至少 8 位"
    _write_cred(new)
    with _LOCK:
        _SESSIONS.clear()
    if INITIAL_PW_FILE.is_file():
        try:
            INITIAL_PW_FILE.unlink()
        except OSError:
            pass
    return True, "口令已更新，请用新口令重新登录"


# ---------- 会话 ----------
def issue_token() -> str:
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = time.time() + SESSION_TTL
    return token


def check_token(token: str | None) -> bool:
    if not token:
        return False
    with _LOCK:
        exp = _SESSIONS.get(token)
        if not exp:
            return False
        if exp < time.time():
            _SESSIONS.pop(token, None)
            return False
        return True


def revoke_token(token: str | None) -> None:
    with _LOCK:
        if token:
            _SESSIONS.pop(token, None)


# ---------- 失败锁定 ----------
def is_locked(source: str) -> tuple[bool, int]:
    with _LOCK:
        fails, until = _FAILS.get(source, (0, 0.0))
        if until and until > time.time():
            return True, int(until - time.time())
        return False, 0


def record_fail(source: str) -> None:
    with _LOCK:
        fails, _ = _FAILS.get(source, (0, 0.0))
        fails += 1
        until = time.time() + LOCK_SECONDS if fails >= MAX_FAILS else 0.0
        _FAILS[source] = (0 if until else fails, until)


def clear_fails(source: str) -> None:
    with _LOCK:
        _FAILS.pop(source, None)
