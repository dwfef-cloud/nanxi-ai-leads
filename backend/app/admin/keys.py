"""密钥签发引擎。

设计目标：**离线可校验**。客户机拿不到后台也能判断密钥真伪与到期日，
不必联网换取票据（联网只用于吊销与用量上报）。

码结构（共 22 字符，大写，去掉易混字符 0/1/I/O）：

    ALS-{套餐}{到期日3位}-{随机8位}-{签名4位}
         例：ALS-P2K8-7QMX3RTB-9D4F

  · 套餐 1 位：B 基础 / S 标准 / P 专业 / U 旗舰
  · 到期日 3 位：自 EPOCH(2026-01-01) 起的天数，base32 编码；ZZZ 表示永久
  · 随机 8 位：40 bit 熵，防猜
  · 签名 4 位：HMAC-SHA256(secret, 前两段) 截断 20 bit，防伪造/防手改

安全性说明：离线校验必然把 secret 带到客户端，理论上可被逆向提取。因此
  - secret 只用于「防随手编造、防误改」，不承担抗专业破解；
  - 真正防滥用靠服务端：激活登记 + 心跳上报 + 后台一键吊销；
  - 客户机离线超期后应停用（由客户端实现，到期日已编码在码内）。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets as py_secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.paths import DATA_DIR

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # 32 字符，去掉 0 1 I O
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
PERPETUAL = "ZZZ"

PLAN_CODE = {"basic": "B", "standard": "S", "pro": "P", "ultimate": "U"}
CODE_PLAN = {v: k for k, v in PLAN_CODE.items()}

SECRET_FILE = Path(os.getenv("ADMIN_SECRET_FILE", str(DATA_DIR / "admin_secret.key")))


def _decode_fixed(s: str, width: int) -> int:
    n = 0
    for ch in s:
        n = n * 32 + ALPHABET.index(ch)
    return n


def _encode_fixed(n: int, width: int) -> str:
    out = []
    for _ in range(width):
        out.append(ALPHABET[n % 32])
        n //= 32
    return "".join(reversed(out))


def load_secret() -> bytes:
    """读取或首次生成签名密钥（0600）。环境变量 ADMIN_LICENSE_SECRET 优先。"""
    env = os.getenv("ADMIN_LICENSE_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    if SECRET_FILE.is_file():
        raw = SECRET_FILE.read_text(encoding="utf-8").strip()
        if raw:
            return raw.encode("utf-8")
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    raw = py_secrets.token_hex(32)
    SECRET_FILE.write_text(raw, encoding="utf-8")
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass  # Windows 上 chmod 语义有限，失败不影响功能
    return raw.encode("utf-8")


def _sign(payload: str) -> str:
    digest = hmac.new(load_secret(), payload.encode("utf-8"), hashlib.sha256).digest()
    return _encode_fixed(int.from_bytes(digest[:8], "big"), 4)


def days_from_epoch(dt: datetime) -> int:
    delta = dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - EPOCH
    return max(0, delta.days)


def make_code(plan: str = "standard", expires_at: datetime | str | None = None) -> str:
    """expires_at=None 表示永久；datetime 或 UTC ISO8601 字符串均可。"""
    p = PLAN_CODE.get(plan)
    if not p:
        raise ValueError(f"未知套餐：{plan}")
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if expires_at is None:
        dseg = PERPETUAL
    else:
        d = days_from_epoch(expires_at)
        if d >= 32 ** 3:
            raise ValueError("到期日超出可编码范围")
        dseg = _encode_fixed(d, 3)
    body = f"{p}{dseg}-{''.join(py_secrets.choice(ALPHABET) for _ in range(8))}-"
    prefix = f"ALS-{body}"
    return prefix + _sign(prefix)


def parse_code(code: str) -> dict | None:
    """结构解析（不校验签名）。返回 None 表示结构不合法。"""
    text = (code or "").strip().upper()
    parts = text.split("-")
    if len(parts) != 4 or parts[0] != "ALS":
        return None
    head, rnd, sig = parts[1], parts[2], parts[3]
    if len(head) != 4 or len(rnd) != 8 or len(sig) != 4:
        return None
    for seg in (head, rnd, sig):
        if any(ch not in ALPHABET for ch in seg):
            return None
    plan = CODE_PLAN.get(head[0])
    if plan is None:
        return None
    dseg = head[1:]
    if dseg == PERPETUAL:
        expires_at = None
    else:
        days = _decode_fixed(dseg, 3)
        expires_at = (EPOCH + timedelta(days=days)).isoformat()
    return {"plan": plan, "expiresAt": expires_at, "random": rnd, "signature": sig}


def verify_code(code: str) -> dict:
    """完整校验：结构 + 签名 + 是否过期。返回 {valid, reason, plan, expiresAt}。"""
    text = (code or "").strip().upper()
    parsed = parse_code(text)
    if not parsed:
        return {"valid": False, "reason": "格式不合法", "plan": None, "expiresAt": None}
    expected = _sign("-".join(text.split("-")[:3]) + "-")
    if not hmac.compare_digest(expected, parsed["signature"]):
        return {"valid": False, "reason": "签名校验失败（密钥被改动或伪造）",
                "plan": parsed["plan"], "expiresAt": parsed["expiresAt"]}
    if parsed["expiresAt"]:
        exp = datetime.fromisoformat(parsed["expiresAt"])
        if exp < datetime.now(timezone.utc):
            return {"valid": False, "reason": "密钥已过期",
                    "plan": parsed["plan"], "expiresAt": parsed["expiresAt"]}
    return {"valid": True, "reason": "校验通过",
            "plan": parsed["plan"], "expiresAt": parsed["expiresAt"]}


def expiry_from_days(days: int | None) -> str | None:
    """签发时的便捷换算：N 天后到期（None=永久），返回 UTC ISO8601。"""
    if days is None or days <= 0:
        return None
    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.replace(microsecond=0).isoformat()
