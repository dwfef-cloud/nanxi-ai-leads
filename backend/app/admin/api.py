"""后台控制台 API（/admin/api/*）。

分两类，鉴权边界不同：
  · 管理接口：需要 X-Admin-Token（登录后获得），只给发行方用。
  · 接入接口：activate / heartbeat / verify，由客户机调用，用密钥本身鉴权，不需要后台令牌。

安全：口令失败锁定、SQL 全参数绑定、导出加 BOM 便于 Excel、不返回任何业务数据。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import Response

from app.admin import auth, keys
from app.admin.store import (
    LICENSE_STATUSES,
    PLANS,
    AdminStore,
    now_iso,
    today_str,
)

router = APIRouter(prefix="/admin/api", tags=["admin"])

_STORE = AdminStore()


def store() -> AdminStore:
    return _STORE


# ---------------- 鉴权 ----------------
def require_admin(x_admin_token: str | None = Header(default=None)) -> str:
    if not auth.check_token(x_admin_token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return x_admin_token or ""


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------- 登录 ----------------
@router.post("/login")
def login(request: Request, payload: dict = Body(default={})) -> dict:
    source = _client_ip(request)
    locked, wait = auth.is_locked(source)
    if locked:
        raise HTTPException(status_code=429, detail=f"失败次数过多，请 {wait} 秒后再试")
    password = str(payload.get("password") or "")
    if not auth.verify_password(password):
        auth.record_fail(source)
        store().audit("登录失败", target=source, detail="口令错误")
        raise HTTPException(status_code=401, detail="口令不正确")
    auth.clear_fails(source)
    token = auth.issue_token()
    store().audit("登录成功", target=source)
    return {"token": token, "expiresIn": auth.SESSION_TTL}


@router.post("/logout")
def logout(x_admin_token: str | None = Header(default=None)) -> dict:
    auth.revoke_token(x_admin_token)
    return {"ok": True}


@router.get("/me")
def me(token: str = Query(default=""), x_admin_token: str | None = Header(default=None)) -> dict:
    require_admin(x_admin_token)
    return {"authenticated": True, "role": "admin"}


@router.post("/password")
def change_password(
    payload: dict = Body(default={}),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    ok, msg = auth.change_password(
        str(payload.get("oldPassword") or ""), str(payload.get("newPassword") or "")
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    store().audit("修改口令", detail=msg)
    return {"ok": True, "message": msg}


# ---------------- 概览 ----------------
@router.get("/overview")
def overview(
    days: int = Query(default=14, ge=1, le=90),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    s = store()
    s.expire_overdue()
    lic = s.license_stats()
    inst = s.instance_count()
    today = s.usage_today()
    return {
        "licenses": lic,
        "instances": inst,
        "today": {
            "leads": int(today.get("leads") or 0),
            "comments": int(today.get("comments") or 0),
            "dms": int(today.get("dms") or 0),
            "logins": int(today.get("logins") or 0),
            "actions": int(today.get("actions") or 0),
            "activeInstances": int(today.get("active_instances") or 0),
        },
        "series": s.usage_series(days),
        "topLicenses": s.usage_by_license(days)[:8],
        "recentAudit": s.list_audit(8),
        "generatedAt": now_iso(),
    }


# ---------------- 密钥 ----------------
@router.get("/licenses")
def list_licenses(
    status: str = Query(default=""),
    plan: str = Query(default=""),
    keyword: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    if status and status not in LICENSE_STATUSES:
        raise HTTPException(status_code=400, detail="状态取值不合法")
    if plan and plan not in PLANS:
        raise HTTPException(status_code=400, detail="套餐取值不合法")
    rows, total = store().list_licenses(status, keyword, plan, page, size)
    return {"items": [_lic_out(r) for r in rows], "total": total, "page": page, "size": size}


def _lic_out(row: dict) -> dict:
    code = row.get("code", "")
    parsed = keys.parse_code(code) or {}
    expires = row.get("expires_at")
    days_left: int | None = None
    if expires:
        try:
            exp = datetime.fromisoformat(expires)
            days_left = (exp - datetime.now(timezone.utc)).days
        except ValueError:
            days_left = None
    return {
        "code": code,
        "plan": row.get("plan"),
        "planCode": parsed.get("plan"),
        "customerName": row.get("customer_name") or "",
        "customerContact": row.get("customer_contact") or "",
        "seats": row.get("seats"),
        "dailyQuota": row.get("daily_quota"),
        "status": row.get("status"),
        "issuedAt": row.get("issued_at"),
        "activatedAt": row.get("activated_at"),
        "expiresAt": expires,
        "daysLeft": days_left,
        "boundMachine": row.get("bound_machine") or "",
        "note": row.get("note") or "",
    }


@router.post("/licenses")
def create_licenses(
    payload: dict = Body(default={}),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    plan = str(payload.get("plan") or "standard")
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="套餐取值不合法")
    try:
        count = max(1, min(200, int(payload.get("count") or 1)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="数量必须是整数") from None
    try:
        valid_days = payload.get("validDays")
        valid_days = None if valid_days in (None, "", 0, "0") else int(valid_days)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="有效天数必须是整数") from None
    try:
        seats = max(1, int(payload.get("seats") or 1))
        quota = max(0, int(payload.get("dailyQuota") or 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="席位数/配额必须是整数") from None

    s = store()
    expires_at = keys.expiry_from_days(valid_days)
    created: list[str] = []
    guard = 0
    while len(created) < count and guard < count * 20:
        guard += 1
        code = keys.make_code(plan, datetime.fromisoformat(expires_at) if expires_at else None)
        if s.code_exists(code) or code in created:
            continue
        s.insert_license({
            "code": code,
            "plan": plan,
            "customer_name": str(payload.get("customerName") or ""),
            "customer_contact": str(payload.get("customerContact") or ""),
            "seats": seats,
            "daily_quota": quota,
            "status": "unused",
            "issued_at": now_iso(),
            "expires_at": expires_at,
            "note": str(payload.get("note") or ""),
        })
        created.append(code)
    if len(created) < count:
        raise HTTPException(status_code=500, detail="密钥生成冲突过多，请重试")

    s.audit("生成密钥", target=f"{count} 把", detail=f"套餐={plan} 有效期={valid_days or '永久'}天")
    return {"codes": created, "expiresAt": expires_at, "plan": plan}


@router.patch("/licenses/{code}")
def update_license(
    code: str,
    payload: dict = Body(default={}),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    s = store()
    if not s.get_license(code):
        raise HTTPException(status_code=404, detail="密钥不存在")

    fields: dict[str, Any] = {}
    if "status" in payload:
        status = str(payload["status"])
        if status not in LICENSE_STATUSES:
            raise HTTPException(status_code=400, detail="状态取值不合法")
        fields["status"] = status
    if "note" in payload:
        fields["note"] = str(payload["note"] or "")
    if "customerName" in payload:
        fields["customer_name"] = str(payload["customerName"] or "")
    if "customerContact" in payload:
        fields["customer_contact"] = str(payload["customerContact"] or "")
    if "seats" in payload:
        fields["seats"] = max(1, int(payload["seats"] or 1))
    if "dailyQuota" in payload:
        fields["daily_quota"] = max(0, int(payload["dailyQuota"] or 0))
    if "extendDays" in payload:
        try:
            days = int(payload["extendDays"] or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="延期天数必须是整数") from None
        base = datetime.now(timezone.utc)
        cur = s.get_license(code) or {}
        if cur.get("expires_at"):
            try:
                cur_exp = datetime.fromisoformat(cur["expires_at"])
                base = max(base, cur_exp)
            except ValueError:
                pass
        fields["expires_at"] = (base.replace(microsecond=0) + timedelta(days=days)).isoformat()
    if "unbind" in payload and payload["unbind"]:
        fields["bound_machine"] = ""

    n = s.update_license(code, fields)
    s.audit("修改密钥", target=code, detail="、".join(f"{k}" for k in fields) or "无变更")
    return {"ok": True, "updated": n, "license": _lic_out(s.get_license(code) or {})}


@router.delete("/licenses/{code}")
def delete_license(
    code: str,
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    s = store()
    if not s.get_license(code):
        raise HTTPException(status_code=404, detail="密钥不存在")
    s.delete_license(code)
    s.audit("删除密钥", target=code)
    return {"ok": True}


@router.get("/export/licenses.csv")
def export_licenses(x_admin_token: str | None = Header(default=None)) -> Response:
    require_admin(x_admin_token)
    rows, _ = store().list_licenses(size=100000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["密钥", "套餐", "客户", "联系方式", "席位", "日配额", "状态",
                "签发时间", "激活时间", "到期时间", "绑定机器", "备注"])
    for r in rows:
        w.writerow([r["code"], r["plan"], r["customer_name"], r["customer_contact"],
                    r["seats"], r["daily_quota"], r["status"], r["issued_at"],
                    r["activated_at"] or "", r["expires_at"] or "", r["bound_machine"],
                    r["note"]])
    data = "\ufeff" + buf.getvalue()  # BOM：Excel 直接打开不乱码
    return Response(
        content=data.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="licenses.csv"'},
    )


# ---------------- 实例监控 ----------------
@router.get("/instances")
def list_instances(
    keyword: str = Query(default=""),
    onlineMinutes: int = Query(default=10, ge=1, le=1440),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    rows = store().list_instances(keyword, onlineMinutes)
    items = [{
        "machineId": r.get("machine_id"),
        "licenseCode": r.get("license_code"),
        "customerName": r.get("customer_name") or "",
        "plan": r.get("plan") or "",
        "hostname": r.get("hostname") or "",
        "appVersion": r.get("app_version") or "",
        "ip": r.get("ip") or "",
        "firstSeen": r.get("first_seen"),
        "lastSeen": r.get("last_seen"),
        "actionsTotal": int(r.get("actions_total") or 0),
        "online": bool(r.get("online")),
        "licenseStatus": r.get("license_status") or "unknown",
        "expiresAt": r.get("expires_at"),
    } for r in rows]
    return {"items": items, "total": len(items),
            "online": sum(1 for i in items if i["online"])}


# ---------------- 用量 ----------------
@router.get("/usage")
def usage(
    days: int = Query(default=14, ge=1, le=90),
    license: str = Query(default=""),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    s = store()
    return {
        "series": s.usage_series(days, license),
        "byLicense": s.usage_by_license(days),
        "today": s.usage_today(),
    }


# ---------------- 审计 ----------------
@router.get("/audit")
def audit(
    limit: int = Query(default=100, ge=1, le=500),
    keyword: str = Query(default=""),
    x_admin_token: str | None = Header(default=None),
) -> dict:
    require_admin(x_admin_token)
    rows = store().list_audit(limit, keyword)
    return {"items": [{
        "id": r["id"], "at": r["at"], "actor": r["actor"], "action": r["action"],
        "target": r["target"], "detail": r["detail"],
    } for r in rows]}


# ---------------- 客户机接入（无需后台令牌） ----------------
@router.get("/verify")
def verify(code: str = Query(default="")) -> dict:
    """离线校验的在线版：只回签名结果与到期日，不揭示客户信息。"""
    result = keys.verify_code(code)
    return {"valid": result["valid"], "reason": result["reason"],
            "plan": result["plan"], "expiresAt": result["expiresAt"]}


@router.post("/activate")
def activate(payload: dict = Body(default={}), request: Request = None) -> dict:  # type: ignore[assignment]
    """客户机首次激活：校验密钥 → 登记机器 → 返回授权信息。

    绑定规则：密钥未被绑定则绑当前机器；已绑其他机器则拒绝（后台可解绑）。
    """
    code = str(payload.get("licenseCode") or payload.get("code") or "").strip().upper()
    machine = str(payload.get("machineId") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="缺少 licenseCode")
    if not machine:
        raise HTTPException(status_code=400, detail="缺少 machineId")

    s = store()
    lic = s.get_license(code)
    if not lic:
        # 库里没有：可能是发出去的离线码尚未录入，也可能是伪造。明确区分原因。
        v = keys.verify_code(code)
        if v["valid"]:
            return {"ok": False, "status": "unknown",
                    "reason": "该密钥签名有效但未在后台登记，请联系发行方录入"}
        return {"ok": False, "status": "invalid", "reason": v["reason"]}

    if lic["status"] == "revoked":
        return {"ok": False, "status": "revoked", "reason": "密钥已被吊销"}
    if lic["status"] == "suspended":
        return {"ok": False, "status": "suspended", "reason": "密钥已暂停"}
    if lic["expires_at"]:
        try:
            if datetime.fromisoformat(lic["expires_at"]) < datetime.now(timezone.utc):
                s.update_license(code, {"status": "expired"})
                return {"ok": False, "status": "expired", "reason": "密钥已过期"}
        except ValueError:
            pass

    bound = (lic.get("bound_machine") or "").strip()
    if bound and bound != machine:
        return {"ok": False, "status": "bound_elsewhere",
                "reason": "该密钥已绑定其他机器，需发行方解绑"}
    if not bound:
        s.update_license(code, {"bound_machine": machine, "activated_at": now_iso(),
                                "status": "active"})
        s.audit("激活绑定", target=code, detail=f"机器={machine[:12]}…")

    s.upsert_instance(
        machine_id=machine, license_code=code,
        hostname=str(payload.get("hostname") or ""),
        app_version=str(payload.get("appVersion") or ""),
        ip=_client_ip(request) if request else "",
    )
    lic = s.get_license(code) or lic
    return {
        "ok": True, "status": "active",
        "plan": lic["plan"], "expiresAt": lic["expires_at"],
        "seats": lic["seats"], "dailyQuota": lic["daily_quota"],
        "customerName": lic["customer_name"],
    }


@router.post("/heartbeat")
def heartbeat(payload: dict = Body(default={}), request: Request = None) -> dict:  # type: ignore[assignment]
    """客户机定时上报：续活 + 用量增量。被吊销/过期的密钥返回 stop=true 让客户端停手。"""
    code = str(payload.get("licenseCode") or "").strip().upper()
    machine = str(payload.get("machineId") or "").strip()
    if not code or not machine:
        raise HTTPException(status_code=400, detail="缺少 licenseCode 或 machineId")

    s = store()
    lic = s.get_license(code)
    if not lic:
        return {"ok": False, "stop": True, "reason": "密钥未登记"}
    if lic["status"] in {"revoked", "suspended"}:
        return {"ok": False, "stop": True, "reason": f"密钥已{('吊销' if lic['status'] == 'revoked' else '暂停')}"}
    if lic["expires_at"]:
        try:
            if datetime.fromisoformat(lic["expires_at"]) < datetime.now(timezone.utc):
                s.update_license(code, {"status": "expired"})
                return {"ok": False, "stop": True, "reason": "密钥已过期"}
        except ValueError:
            pass

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    day = str(usage.get("day") or today_str())[:10]
    if not (len(day) == 10 and day[4] == "-"):
        day = today_str()

    def _n(key: str) -> int:
        try:
            return max(0, int(usage.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    leads, comments, dms = _n("leads"), _n("comments"), _n("dms")
    logins, actions = _n("logins"), _n("actions")
    total = actions or (leads + comments + dms)

    s.add_usage(day, machine, code, leads, comments, dms, logins, total)
    first_seen = s.upsert_instance(
        machine_id=machine, license_code=code,
        hostname=str(payload.get("hostname") or ""),
        app_version=str(payload.get("appVersion") or ""),
        ip=_client_ip(request) if request else "",
        actions_delta=total,
    )
    if lic["status"] == "unused":
        s.update_license(code, {"status": "active", "activated_at": now_iso(),
                                "bound_machine": lic.get("bound_machine") or machine})

    quota = int(lic.get("daily_quota") or 0)
    used_today = s.used_on(day, machine)
    over_quota = bool(quota) and int(used_today) > quota
    return {
        "ok": True, "stop": False,
        "plan": lic["plan"], "expiresAt": lic["expires_at"],
        "dailyQuota": quota, "usedToday": int(used_today), "overQuota": over_quota,
        "firstSeen": first_seen,
        "serverTime": now_iso(),
    }
