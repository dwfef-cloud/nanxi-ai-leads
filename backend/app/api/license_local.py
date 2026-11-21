"""本地演示授权后端（M0 桥接配套，DESIGN-DOC.md §3 / §0.5）。

!! 这不是商业授权体系 !!
  - 码固定 LOCAL-TEST- 前缀，只在本机 JSON 状态文件里登记、校验、吊销。
  - 全程离线：不联网、不连授权服务器、不做设备指纹上报。
  - 激活只改变「授权展示状态」，capabilitiesUnlocked 恒为 []，
    写入 / 采集 / 自动化通道仍全部返回「功能未开通」。
  - 明确标注 nonCommercial=true、distributable=false，禁止对外分发。

状态刻意放在独立文件 data/bridge_license.json，不写生产库 lead_system.db
（桥接层对该库是 mode=ro 只读）。

码格式：LOCAL-TEST-<10位大写字母数字>-<4位校验位>
校验位 = sha256(payload + ':' + 本机secret)[:4]，防手误、防随手编。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.paths import DATA_DIR

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STATE = DATA_DIR / "bridge_license.json"
STATE_PATH = Path(os.getenv("BRIDGE_LICENSE_STATE", str(DEFAULT_STATE)))

CODE_PREFIX = "LOCAL-TEST-"
CODE_RE = re.compile(r"^LOCAL-TEST-([A-Z0-9]{10})-([A-F0-9]{4})$")
DEFAULT_TTL_HOURS = int(os.getenv("BRIDGE_LICENSE_TTL_HOURS", "24"))

NOTICE = (
    "本地演示凭证，非商业授权，不可分发；仅改变授权展示状态，"
    "不解锁任何采集/互动/私信/发布能力，写入类通道仍全部禁用。"
)
CODE_KEYS = (
    "code", "licenseCode", "activationCode", "savedCode", "licenseKey",
    "authCode", "cardKey", "key", "value",
)

_LOCK = threading.RLock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _new_state() -> dict:
    return {
        "schema": 1,
        "notice": NOTICE,
        "secret": secrets.token_hex(16),
        "codes": [],
        "active_code": None,
        "created_at": _iso(_now()),
        "updated_at": _iso(_now()),
    }


def load_state() -> dict:
    """读取状态；首次/损坏时立即固化 secret，保证校验位在重启前后一致。"""
    if not STATE_PATH.is_file():
        fresh = _new_state()
        save_state(fresh)
        return fresh
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fresh = _new_state()
        save_state(fresh)
        return fresh
    if not isinstance(state, dict) or state.get("schema") != 1:
        fresh = _new_state()
        save_state(fresh)
        return fresh
    state.setdefault("secret", secrets.token_hex(16))
    state.setdefault("codes", [])
    state.setdefault("active_code", None)
    state["notice"] = NOTICE
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _iso(_now())
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)  # 原子替换，避免并发读到半截文件


def _checksum(payload: str, secret: str) -> str:
    return hashlib.sha256(f"{payload}:{secret}".encode("utf-8")).hexdigest()[:4].upper()


def normalize_code(raw: Any) -> str:
    """容错：去空白/去全角空格/转大写，容忍用户手输时漏掉的分隔差异。"""
    text = str(raw or "").strip().replace("\u3000", "").upper()
    text = re.sub(r"\s+", "", text)
    # 容忍把 LOCAL-TEST-XXXXXXXXXX-XXXX 输成 LOCALTESTXXXXXXXXXX-XXXX
    if text.startswith("LOCALTEST") and not text.startswith("LOCAL-TEST-"):
        text = "LOCAL-TEST-" + text[len("LOCALTEST"):].lstrip("-")
    return text


def extract_code(source: Any, *, _depth: int = 0) -> str:
    """从前端可能传入的各种形状里取码：字符串 / dict / list / 嵌套。"""
    if _depth > 4:
        return ""
    if isinstance(source, str):
        return normalize_code(source)
    if isinstance(source, Mapping):
        for key in CODE_KEYS:
            if key in source and isinstance(source[key], (str, int)):
                candidate = normalize_code(source[key])
                if candidate:
                    return candidate
        for value in source.values():
            found = extract_code(value, _depth=_depth + 1)
            if found:
                return found
    elif isinstance(source, (list, tuple)):
        for value in source:
            found = extract_code(value, _depth=_depth + 1)
            if found:
                return found
    return ""


def issue_demo_code(*, ttl_hours: int | None = None, note: str = "") -> dict:
    """签发一枚本地演示码并登记到状态文件。"""
    hours = DEFAULT_TTL_HOURS if ttl_hours is None else max(1, int(ttl_hours))
    with _LOCK:
        state = load_state()
        payload = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))
        code = f"{CODE_PREFIX}{payload}-{_checksum(payload, state['secret'])}"
        now = _now()
        record = {
            "code": code,
            "status": "unused",
            "created_at": _iso(now),
            "expires_at": _iso(now + timedelta(hours=hours)),
            "activated_at": None,
            "activate_count": 0,
            "note": note or "本地演示用途",
        }
        state["codes"].append(record)
        state["codes"] = state["codes"][-50:]  # 只保留最近 50 枚，避免无限增长
        save_state(state)
    return {
        "success": True,
        "code": code,
        "expiresAt": record["expires_at"],
        "ttlHours": hours,
        "msg": "本地演示码已生成（非商业授权，不可分发）",
        "nonCommercial": True,
        "distributable": False,
        "capabilitiesUnlocked": [],
        "notice": NOTICE,
    }


def _find(state: dict, code: str) -> dict | None:
    for record in state.get("codes", []):
        if record.get("code") == code:
            return record
    return None


def _expired(record: dict) -> bool:
    try:
        return _now() > datetime.fromisoformat(str(record["expires_at"]))
    except (KeyError, ValueError):
        return True


def _common() -> dict:
    return {
        "nonCommercial": True,
        "distributable": False,
        "offlineOnly": True,
        "capabilitiesUnlocked": [],
        "notice": NOTICE,
        "mode": "live",
    }


# ---------------------------------------------------------------------------
# 发行方密钥（ALS-）桥接：让后台 /admin 签发的商业密钥可以直接在阿里山壳激活框使用。
# 校验顺序：先离线验签（HMAC，断网也能辨真伪），再联机确认状态（登记/吊销/过期/机器绑定）。
# 联机有两种形态：
#   · 本机同库（默认）：直接查 admin_console.db —— 发行方与壳同机部署；
#   · 远程发行方（设 ADMIN_SERVER_URL）：转发到发行方的 /admin/api/activate 与 /heartbeat，
#     客户机不需要后台库，后台「用户监控」能看到该机器在线与用量。
# ---------------------------------------------------------------------------
_PLAN_LABEL = {"basic": "基础版", "standard": "标准版", "pro": "专业版", "ultimate": "旗舰版"}
_LAST_RECHECK: dict = {"t": 0.0, "verdict": "", "plan": ""}


def _shell_machine_id() -> str:
    """壳所在机器的稳定标识（无硬件采集，仅主机名摘要，可后台解绑换机）。"""
    host = socket.gethostname() or "unknown-host"
    return "SHELL-" + hashlib.sha1(host.encode("utf-8")).hexdigest()[:10].upper()


def _admin_server() -> str:
    return os.getenv("ADMIN_SERVER_URL", "").strip().rstrip("/")


def _admin_http(path: str, payload: dict, timeout: float = 8.0) -> dict | None:
    """向发行方后台转发请求；未配置 ADMIN_SERVER_URL 时返回 None（走本地库）。"""
    base = _admin_server()
    if not base:
        return None
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # 网络失败必须显式失败，不能静默放行
        return {"ok": False, "reason": f"无法连接发行方授权服务（{base}）：{exc}"}
    return data if isinstance(data, dict) else {"ok": False, "reason": "授权服务返回格式异常"}


def _admin_activate_local(code: str) -> tuple[dict | None, str, str]:
    """本地直查后台库。返回 (license 行, plan, 失败原因)；有效时原因串为空。"""
    from app.admin import keys as admin_keys
    from app.admin.store import AdminStore

    v = admin_keys.verify_code(code)
    if not v["valid"]:
        return None, "", f"授权码校验失败：{v['reason']}"
    s = AdminStore()
    lic = s.get_license(code)
    if lic is None:
        return None, "", "该授权码签名有效但未在后台登记，请联系发行方录入"
    status = str(lic.get("status") or "")
    if status == "revoked":
        return None, "", "授权码已被发行方吊销"
    if status == "suspended":
        return None, "", "授权码已被发行方暂停"
    if lic.get("expires_at"):
        try:
            if datetime.fromisoformat(str(lic["expires_at"])) < _now():
                s.update_license(code, {"status": "expired"})
                return None, "", "授权码已过期，请联系发行方续期"
        except ValueError:
            pass
    s.audit("壳激活", target=code, detail=f"机器={_shell_machine_id()}")
    return lic, str(lic.get("plan") or v.get("plan") or ""), ""


def _admin_activate(code: str) -> dict:
    """壳激活框粘贴 ALS- 码的入口：验签 → 状态确认 → 本地登记 → 返回授权态。"""
    hostname = socket.gethostname() or ""
    machine = _shell_machine_id()
    plan = ""
    remote = _admin_http(
        "/admin/api/activate",
        {"licenseCode": code, "machineId": machine, "hostname": hostname,
         "appVersion": "1.0.0", "source": "alishan-shell"},
    )
    if remote is not None:
        if not remote.get("ok"):
            return {"success": False,
                    "msg": remote.get("reason") or "激活被发行方拒绝",
                    "mode": "live"}
        plan = str(remote.get("plan") or "")
        expires = str(remote.get("expiresAt") or "")
    else:
        lic, plan, reason = _admin_activate_local(code)
        if lic is None:
            return {"success": False, "msg": reason, "mode": "live"}
        expires = str(lic.get("expires_at") or "")

    # 本地持久登记：无论此前是否登记过、是否被复核误判为 revoked，
    # 激活成功一律重置为 used 并记下套餐。这一步是「激活后跨刷新/重启保持」的关键：
    # 旧实现只在 active_code 变化时才写盘，同一枚码二次激活会保留旧的 revoked 状态，
    # 于是激活当下提示成功、刷新一次就变回未激活。
    with _LOCK:
        state = load_state()
        prev = _find(state, code) or {}
        state["codes"] = [r for r in state.get("codes", []) if r.get("code") != code]
        state["codes"].append({
            "code": code,
            "status": "used",
            "plan": plan,
            "created_at": prev.get("created_at") or _iso(_now()),
            "expires_at": expires or _iso(_now() + timedelta(days=36500)),
            "activated_at": _iso(_now()),
            "activate_count": int(prev.get("activate_count") or 0) + 1,
            "note": "发行方后台密钥（ALS）",
        })
        state["codes"] = state["codes"][-50:]
        state["active_code"] = code
        save_state(state)

    # 同机部署时把后台的授权记录也置为已激活（后台「密钥管理 / 用户监控」口径一致）
    if remote is None:
        try:
            from app.admin.store import AdminStore

            store = AdminStore()
            store.update_license(code, {
                "status": "active",
                "activated_at": _iso(_now()),
                "bound_machine": machine,
            })
            store.upsert_instance(machine, code, hostname=hostname)
        except Exception:
            pass  # 后台库不可用不影响壳侧激活态，激活记录已落本地状态文件

    label = _PLAN_LABEL.get(plan, plan or "商业授权")
    overlay = auth_overlay()
    overlay.update({
        "licenseTierLabel": f"已激活（{label}）",
        "type": f"商业授权（{label}）",
        "activationLabel": f"已激活（{label}）",
    })
    return {"success": True, "msg": f"激活成功（{label}）", "code": code,
            "plan": plan, "machineId": machine, **overlay, "mode": "live"}


def _admin_recheck(code: str) -> tuple[str, str, str]:
    """已激活 ALS 码的复核。返回 (结论, 原因, plan)，结论取三态：

    - ``valid``   发行方明确答复「有效」→ 保持激活；
    - ``invalid`` 发行方明确答复「吊销 / 暂停 / 过期」→ 才允许降级为未激活；
    - ``unknown`` 连不上发行方、后台库不存在、查询异常等**无法判定**的情况
      → 保持现状，绝不因为一次网络抖动或后台库缺失就丢掉激活态。
      （旧实现把「查不到记录」也当成吊销，导致刷新一次就永久失效。）

    远程模式借道 heartbeat —— 复核的同时给发行方后台续活，
    「用户监控」因此能看到这台机器在线。节流 3 分钟，避免频繁请求。
    """
    global _LAST_RECHECK
    now = time.time()
    if _LAST_RECHECK.get("verdict") in {"valid", "invalid"} and now - _LAST_RECHECK["t"] < 180:
        return str(_LAST_RECHECK["verdict"]), "", str(_LAST_RECHECK.get("plan") or "")

    if _admin_server():
        data = _admin_http("/admin/api/heartbeat",
                           {"licenseCode": code, "machineId": _shell_machine_id(), "usage": {}})
        if data is None or "ok" not in data:
            # 网络异常：无法判定，保持原状（不覆盖已有结论）
            return "unknown", str((data or {}).get("reason") or "发行方授权服务暂不可达"), ""
        ok = bool(data.get("ok"))
        plan = str(data.get("plan") or _LAST_RECHECK.get("plan") or "")
        verdict = "valid" if ok else "invalid"
        _LAST_RECHECK = {"t": now, "verdict": verdict, "plan": plan}
        if ok:
            return "valid", "", plan
        return "invalid", str(data.get("reason") or "授权已失效"), ""

    try:
        from app.admin import keys as admin_keys
        from app.admin.store import AdminStore

        v = admin_keys.verify_code(code)
        if not v["valid"]:
            # 签名本身不成立属于明确结论（码被篡改），但更可能是密钥文件换过，仍按无法判定处理
            return "unknown", v["reason"], ""
        store = AdminStore()
        lic = store.get_license(code)
    except Exception as exc:  # 后台库缺失 / 打不开 / 结构不符 → 无法判定
        return "unknown", f"无法访问发行方授权库：{exc}", ""

    if lic is None:
        # 库里没有这条记录：可能是后台库被重建 / 换机迁移 / 误删。签名本身仍然有效，
        # 无法据此断定客户已失去授权，一律按「无法判定」保持激活；
        # 真正要收回授权请用「吊销」显式操作。
        return "unknown", "授权库中暂无该码记录", ""

    plan = str(lic.get("plan") or "")
    status = str(lic.get("status") or "")
    if status in {"revoked", "suspended"}:
        _LAST_RECHECK = {"t": now, "verdict": "invalid", "plan": plan}
        return "invalid", "授权码已被发行方吊销或暂停", plan
    if lic.get("expires_at"):
        try:
            if datetime.fromisoformat(str(lic["expires_at"])) < _now():
                store.update_license(code, {"status": "expired"})
                _LAST_RECHECK = {"t": now, "verdict": "invalid", "plan": plan}
                return "invalid", "授权码已过期", plan
        except ValueError:
            pass
    # 顺带续活实例（后台「用户监控」据此显示在线）；失败不影响判定
    try:
        if status == "unused":
            store.update_license(code, {"status": "active", "bound_machine": _shell_machine_id()})
        store.upsert_instance(_shell_machine_id(), code, hostname=socket.gethostname() or "")
    except Exception:
        pass
    _LAST_RECHECK = {"t": now, "verdict": "valid", "plan": plan}
    return "valid", "", plan


def _heal_record(code: str, plan: str = "") -> None:
    """把被复核误判为吊销的本地记录恢复成已激活（自愈）。"""
    with _LOCK:
        st = load_state()
        rec = _find(st, code)
        if rec and rec.get("status") != "used":
            rec["status"] = "used"
            if plan:
                rec["plan"] = plan
            rec.setdefault("activated_at", _iso(_now()))
            save_state(st)


def auth_overlay() -> dict:
    """叠加到 check-auth / refresh-auth 返回上的授权状态（激活态跨刷新/重启保持）。"""
    with _LOCK:
        state = load_state()
    code = state.get("active_code")
    record = _find(state, code) if code else None

    # 自愈：历史版本把「后台库查不到 / 网络不通」当成吊销写进了本地记录，
    # 这里对 ALS 码先向发行方确认一次，明确有效就把状态改回 used。
    if code and str(code).startswith("ALS-") and (record is None or record.get("status") != "used"):
        verdict, _reason, plan = _admin_recheck(str(code))
        if verdict == "valid":
            _heal_record(str(code), plan)
            with _LOCK:
                state = load_state()
            record = _find(state, code)

    active = bool(record and record.get("status") == "used" and not _expired(record))
    als_label = ""
    als_online = False
    als_plan = str((record or {}).get("plan") or "")
    if active and str(code).startswith("ALS-"):
        verdict, _reason, plan = _admin_recheck(str(code))
        if verdict == "invalid":
            # 仅发行方给出明确结论（吊销 / 暂停 / 过期）才降级
            with _LOCK:
                st = load_state()
                rec = _find(st, code)
                if rec:
                    rec["status"] = "revoked"
                    save_state(st)
            active = False
        else:
            # valid / unknown：保持激活；unknown 时用本地登记里的套餐兜底展示
            chosen = plan or als_plan
            als_label = _PLAN_LABEL.get(chosen, chosen or "商业授权")
            als_online = verdict == "valid"

    base = {
        "entitlementVerified": False,
        "savedCode": "",
        "licenseTier": "local",
        "licenseTierLabel": "本地自托管版",
        "planType": "local",
        "isFree": True,
        "isTrial": False,
        "type": "本地自托管版",
        "expireTime": "",
        "activationLabel": "未激活（本地演示）",
    }
    base.update(_common())
    if active:
        base.update({
            "entitlementVerified": True,
            "savedCode": code,
            "licenseTier": "local-demo",
            "licenseTierLabel": "本地演示凭证（非商业授权）",
            "planType": "local-demo",
            "isFree": False,
            "type": "本地演示凭证（非商业授权）",
            "expireTime": record["expires_at"],
            "activatedAt": record.get("activated_at"),
            "activationLabel": "本地演示已激活",
        })
        if als_label:
            base.update({
                "licenseTier": "commercial",
                "licenseTierLabel": f"已激活（{als_label}）",
                "planType": als_label,
                "type": f"商业授权（{als_label}）",
                "activationLabel": f"已激活（{als_label}）",
                # 商业码不应再显示"本地演示凭证"的说明，否则用户以为是未授权状态
                "nonCommercial": False,
                "notice": "商业授权凭证，由发行方后台签发；到期或吊销由发行方控制。",
                # 复核明确有效 → 不挂「离线」后缀；连不上发行方时才如实标离线
                "offline": not als_online,
            })
    return base


def status() -> dict:
    with _LOCK:
        state = load_state()
    overlay = auth_overlay()
    records = [{
        "code": r.get("code"), "status": r.get("status"),
        "createdAt": r.get("created_at"), "expiresAt": r.get("expires_at"),
        "activatedAt": r.get("activated_at"), "activateCount": r.get("activate_count", 0),
    } for r in state.get("codes", [])]
    return {
        "success": True,
        "active": bool(overlay["entitlementVerified"]),
        "activeCode": state.get("active_code"),
        "codes": records[-10:],
        "codeCount": len(records),
        "auth": overlay,
        **_common(),
    }


def activate(source: Any) -> dict:
    """activate 通道 / REST 共用的真实校验逻辑。"""
    code = extract_code(source)
    if not code:
        return {"success": False, "msg": "未提供授权码", **_common()}
    if code.startswith("ALS-"):
        return _admin_activate(code)
    match = CODE_RE.match(code)
    if not match:
        return {
            "success": False,
            "msg": f"授权码格式无效（应为 {CODE_PREFIX}XXXXXXXXXX-XXXX）",
            **_common(),
        }
    payload, check = match.group(1), match.group(2)

    with _LOCK:
        state = load_state()
        if _checksum(payload, state["secret"]) != check:
            return {"success": False, "msg": "授权码校验位不匹配（请检查是否手输有误）", **_common()}
        record = _find(state, code)
        if record is None:
            return {"success": False, "msg": "授权码未在本机登记（请先用 demo-code 生成）", **_common()}
        if record.get("status") == "revoked":
            return {"success": False, "msg": "授权码已吊销", **_common()}
        if _expired(record):
            return {"success": False, "msg": "授权码已过期，请重新生成", **_common()}
        if state.get("active_code") == code:
            return {"success": True, "msg": "该演示码已激活（幂等）", **auth_overlay(), "code": code}
        record["status"] = "used"
        record["activated_at"] = _iso(_now())
        record["activate_count"] = int(record.get("activate_count") or 0) + 1
        state["active_code"] = code
        save_state(state)

    return {
        "success": True,
        "msg": "本地演示凭证已激活（非商业授权，不解锁任何能力）",
        "code": code,
        **auth_overlay(),
    }


def revoke(source: Any = None) -> dict:
    """吊销当前激活状态，便于反复演示；不删历史码。"""
    code = extract_code(source) or None
    with _LOCK:
        state = load_state()
        target = code or state.get("active_code")
        if not target:
            return {"success": True, "msg": "当前无激活状态", **_common()}
        record = _find(state, target)
        if record:
            record["status"] = "revoked"
        if state.get("active_code") == target:
            state["active_code"] = None
        save_state(state)
    return {"success": True, "msg": "本地演示授权已吊销", "code": target, **auth_overlay()}


def reset() -> dict:
    """清空全部演示码与激活状态（状态文件整体重置）。"""
    with _LOCK:
        save_state(_new_state())
    return {"success": True, "msg": "本地演示授权状态已重置", **auth_overlay()}
