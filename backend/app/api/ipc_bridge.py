"""M0 渲染层桥接 —— 后端侧（DESIGN-DOC.md §3 / §0.3）。

把阿里山 renderer 提取版的 ipcRenderer.invoke(channel, ...args) 约 94 个通道
映射到南溪真实数据源。前端 bundle 与样式零改动，只换数据源。

协议约定：
    POST /ipc/{channel}  body: {"args": [...]}
    -> {"success": true, "data": <该通道原本的形状>, "mode": "live"}
    -> {"success": false, "error": "..."}  仅表示桥接层故障

注意：外层 success 表示「通道被正确应答」，业务层「功能未开通」用
data.success=false 表达，避免 live-bridge.js 误判为故障而回退到假数据。

合规边界（§1.3 / §0.5）：
  - 读通道对生产库只读（mode=ro，失败时强制 PRAGMA query_only=ON）；
    写通道（保存任务配置 / 删除任务 / 改线索意向 / 保存通知配置）只写
    leads、tasks、crawl_tasks、system_settings 四类表，逐条明确列名，不整行覆盖。
  - 线索出口只给最小展示集，显式拦截平台 uid 级字段；存量
    comment_user_id / video_id / comment_id / external_id 一律不透出。
  - 批量关注 / 私信 / 监控抓取 / 启动自动化等危险通道一律返回「功能未开通」。
  - 不触发任何平台请求，不启动 MediaCrawler 子进程。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from app import (ai_personas, collector, comment_likes, export_files, leadgen_pools, nurture_config,
                 scheduler, schedules, webhook_test)
from app.api import (account_data, account_pool, aggregate, app_settings, cookie_session,
                     desktop_commands, event_bus, faq_content, faq_feedback, license_local,
                     live_dashboard, task_logs, task_runner, task_store, self_warmup,
                     self_warmup_engine)
from app.paths import DATA_DIR, EXPORT_DIR
from app.execution.touch_stats import (
    TOUCH_KINDS,
    TouchLedger,
    blank_counts,
    counts_to_flags,
)
from app.runtime import batch_follow, publish as publish_runtime

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB = DATA_DIR / "lead_system.db"
DB_PATH = Path(os.getenv("LEADS_DB_PATH", str(DEFAULT_DB)))

# 触达账本（只读，剔除测试记录）。按库路径缓存实例，便于测试替换 DB_PATH。
_TOUCH_LEDGERS: dict[str, TouchLedger] = {}


def _touch_ledger() -> TouchLedger:
    key = str(DB_PATH)
    ledger = _TOUCH_LEDGERS.get(key)
    if ledger is None:
        ledger = TouchLedger(DB_PATH)
        _TOUCH_LEDGERS[key] = ledger
    return ledger

# 前端壳随包分发：默认取 backend 同级的 ui/，整个交付包拷到任何位置都能跑。
# 只有需要指向别处的构建产物（如调试新版 bundle）时才用 RENDERER_DIR 覆盖。
RENDERER_DIR = Path(os.getenv("RENDERER_DIR", str(BACKEND_DIR.parent / "ui")))

_STARTED = time.monotonic()
router = APIRouter()

# --------------------------------------------------------------------------
# 出口合规：平台侧标识字段白名单之外一律拦截（与落库最小集规则分开）
# --------------------------------------------------------------------------
PLATFORM_UID_FIELDS: frozenset[str] = frozenset({
    "uid", "sec_uid", "secUid", "webcast_uid", "douyinId", "douyin_id",
    "comment_user_id", "commentUserId", "comment_id", "commentId",
    "video_id", "videoId", "external_id", "externalId", "user_id", "userId",
    "author_id", "authorId", "openid", "unionid", "cookie", "raw_page", "raw_html",
})
_UID_SHAPE = re.compile(
    r"(?:^|[_-])(?:uid|secuid|webcastuid|userid|openid|unionid|"
    r"commentuserid|commentid|videoid|externalid|profileurl|rawpage|rawhtml)(?:$|[_-])",
    re.IGNORECASE,
)


class BridgeComplianceError(RuntimeError):
    """出口出现 uid 级字段。直接让通道失败，不允许降级放行。"""


def assert_public_safe(payload: Any, *, _depth: int = 0) -> None:
    """递归校验返回给页面的字段，命中平台 uid 形态即抛错。"""
    if _depth > 5:
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            name = str(key)
            if name in PLATFORM_UID_FIELDS or _UID_SHAPE.search(name):
                raise BridgeComplianceError(f"出口字段疑似平台 uid: {name}")
            assert_public_safe(value, _depth=_depth + 1)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            assert_public_safe(item, _depth=_depth + 1)


# --------------------------------------------------------------------------
# 只读数据库连接
# --------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, ValueError):
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(sql: str, params: tuple = ()) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def _scalar(sql: str, params: tuple = (), default: int = 0) -> int:
    row = _one(sql, params)
    if not row:
        return default
    value = next(iter(row.values()))
    return int(value) if value is not None else default


def _table_exists(name: str) -> bool:
    return _one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


# --------------------------------------------------------------------------
# 形状工具
# --------------------------------------------------------------------------
def _ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _iso(value: Any) -> str:
    return _ts(value).astimezone(timezone.utc).isoformat()


def _fmt(value: Any) -> str:
    return _ts(value).astimezone().strftime("%Y-%m-%d %H:%M")


def _epoch_ms(value: Any) -> int:
    return int(_ts(value).timestamp() * 1000)


def _arg(args: list, index: int = 0) -> dict:
    if index < len(args) and isinstance(args[index], Mapping):
        return dict(args[index])
    return {}


_INTENT_LABEL = {
    "A": "高意向", "B": "中意向", "C": "低意向",
    "high": "高意向", "medium": "中意向", "low": "低意向", "hot": "高意向",
}


def _lead_public(row: Mapping, counts: Mapping[str, int] | None = None) -> dict:
    """leads 表 -> 线索库页最小展示集。禁止带回任何平台 uid。

    counts 为该线索的真实触达次数（app.execution.touch_stats）；未传时全 0，
    绝不按 status 字段臆造动作次数。
    """
    level = str(row.get("intent_level") or row.get("intent") or "").lower()
    comment = str(row.get("comment") or "")
    touches = dict(counts) if counts else blank_counts()
    touch_total = sum(touches.get(kind, 0) for kind in TOUCH_KINDS)
    item = {
        "leadId": row.get("id"), "key": row.get("id"),
        "nickname": row.get("nickname") or "", "avatar": "",
        "location": row.get("region") or "",
        "intention": _INTENT_LABEL.get(str(row.get("intent_level") or ""), "未评级"),
        "commentLevel": "强意向评论" if level in {"a", "high", "hot"} else "普通评论",
        "comment": comment, "commentContent": comment,
        "accountId": "", "accountName": row.get("account") or "", "account": row.get("account") or "",
        "entrySource": row.get("source") or "",
        "userUrl": row.get("source_url") or "",
        "capturedAt": _iso(row.get("created_at")),
        "timestamp": _epoch_ms(row.get("created_at")),
        "time": _fmt(row.get("created_at")),
        "touchCounts": {kind: touches.get(kind, 0) for kind in TOUCH_KINDS},
        "batchFollowStatus": "已关注" if touches.get("follow", 0) else "未关注",
        "touchLog": [], "touchTotal": touch_total,
        "keyword": row.get("source_keyword") or "",
        "searchKeyword": row.get("source_keyword") or "",
        "videoUrl": row.get("source_url") or "",
        "title": row.get("video") or "",
        "note": row.get("note") or "",
        "timeText": str(row.get("note") or "").removeprefix("平台显示时间："),
    }
    # 吞评检测结论列：只有行里真的带这些列（已懒迁移）才透出，避免旧库炸键。
    if all(column in row for column in _VIS_STATUS_COLUMNS):
        item.update({
            "visibilityStatus": row.get("visibility_status") or "",
            "visibilityReason": row.get("visibility_reason") or "",
            "visibilityTargetAccountId": row.get("visibility_target_account") or "",
            "visibilityTargetAccountName": row.get("visibility_target_name") or "",
            "visibilitySampleKind": row.get("visibility_sample_kind") or "",
            "visibilitySampleContent": row.get("visibility_sample_content") or "",
            "visibilitySampleResult": {
                "visible": "本条样本正常", "not_visible": "本次样本疑似吞评",
                "inconclusive": "检测未完成", "no_sample": "无可用样本",
            }.get(str(row.get("visibility_status") or ""), "检测未完成"),
        })
        if row.get("visibility_cleanup"):
            item["visibilityCleanup"] = {
                "state": row.get("visibility_cleanup"),
                "deletedAt": _iso(row.get("cleanup_deleted_at")) if row.get("cleanup_deleted_at") else "",
            }
    item.update(counts_to_flags(touches))
    assert_public_safe(item)
    return item


def _status(raw: Any) -> str:
    """平台/本地状态词 → 前端四态。

    failed 必须原样透出：过去一律归到 stopped，页面显示「已停止」，用户以为是自己停的，
    实际是跑失败了，错误原因也就无处可看。
    """
    value = str(raw or "").lower()
    if value in {"completed", "done", "success"}:
        return "completed"
    if value in {"running", "pending", "processing"}:
        return "running"
    if value in {"failed", "error", "crashed", "exception"}:
        return "failed"
    return "stopped"


def _task_shape(row: Mapping, *, source_table: str) -> dict:
    leads_total = int(row.get("imported_count") or row.get("collected_count") or 0)
    status = _status(row.get("status"))
    created = row.get("created_at")
    ended = row.get("finished_at") or row.get("updated_at")
    label = (str(row.get("name") or "").strip()
             or str(row.get("keyword") or "").strip()
             or str(row.get("task_type") or "").strip()
             or str(row.get("crawl_type") or "").strip()
             or "未命名任务")
    return {
        "id": row.get("id"), "name": label,
        "status": status, "rawStatus": row.get("status"),
        "createdAt": _iso(created), "updatedAt": _iso(row.get("updated_at") or created),
        "startedAt": _iso(row.get("started_at") or created),
        "endedAt": None if status == "running" else _iso(ended or created),
        "endReason": (None if status == "running"
                      else "failed" if status == "failed" else "completed"),
        "errorMessage": str(row.get("error_message") or ""),
        "leadCount": leads_total,
        "runs": [],
        "stats": {
            "videos": 0, "leadsTotal": leads_total, "likes": 0, "replies": 0,
            "videoComments": 0, "profileComments": 0, "follows": 0,
            "messages": 0, "profileShares": 0, "commentShares": 0,
        },
        "accountStats": [],
        "configSnapshot": {
            "sourceTypes": [row.get("crawl_type") or row.get("task_type") or "unknown"],
            "selectedAccounts": [],
            "keyword": row.get("keyword") or "",
            "sourceTable": source_table,
        },
    }


# --------------------------------------------------------------------------
# 真数据通道
# --------------------------------------------------------------------------
def _license(_: dict) -> dict:
    base = {
        "success": True,
        "maxConcurrentAccounts": _scalar("SELECT COUNT(*) FROM accounts") if _table_exists("accounts") else 0,
        "aiQuota": 0, "deviceId": _device_id(), "runtimeConfig": {}, "offline": True,
        "demo": False, "mode": "live",
    }
    # 本地演示授权只覆盖展示状态，不改变任何能力开关（capabilitiesUnlocked 恒为 []）
    base.update(license_local.auth_overlay())
    return base


def _device_id() -> str:
    digest = hashlib.sha256(str(uuid.getnode()).encode()).hexdigest()[:8].upper()
    return f"LOCAL-{digest}"


def _version(_: dict) -> dict:
    return {
        "success": True, "version": "0.1.0+bridge", "platform": platform.system().lower(),
        "arch": platform.machine().lower(), "electron": None, "demo": False, "live": True,
    }


def _runtime_state(_: dict) -> dict:
    row = _one("SELECT * FROM runtime_state LIMIT 1") if _table_exists("runtime_state") else None
    return {
        "success": True, "state": "ready", "ready": True,
        "safeMode": bool(row and row.get("safe_mode_active")),
        "taskRunning": bool(row and row.get("task_running")),
        "mode": "live",
    }


def _announcements(_: dict) -> dict:
    if not _table_exists("notifications"):
        return {"success": True, "items": []}
    rows = _rows("SELECT id, title, content, level, created_at FROM notifications ORDER BY created_at DESC LIMIT 20")
    items = [{
        "id": r.get("id"), "title": r.get("title") or "", "content": r.get("content") or "",
        "level": r.get("level") or "info", "createdAt": _iso(r.get("created_at")),
    } for r in rows]
    return {"success": True, "items": items, "note": "来源 system notifications"}


def _performance(_: dict) -> dict:
    try:
        free_gb = round(shutil.disk_usage(DB_PATH.anchor).free / (1024 ** 3), 1)
    except OSError:
        free_gb = 0
    result = {
        "success": True, "status": "good", "diskFreeGB": free_gb,
        "uptimeHours": round((time.monotonic() - _STARTED) / 3600, 2),
        "cpuUsage": None, "memoryUsage": None, "memoryUsedMB": None, "memoryTotalMB": None,
        "note": "CPU/内存指标需 psutil；当前仅提供磁盘与进程运行时长真值",
    }
    try:
        import psutil  # type: ignore
        result.update({
            "cpuUsage": psutil.cpu_percent(interval=0),
            "memoryUsage": psutil.virtual_memory().percent,
            "memoryUsedMB": round(psutil.virtual_memory().used / 1024 ** 2),
            "memoryTotalMB": round(psutil.virtual_memory().total / 1024 ** 2),
        })
    except ImportError:
        pass
    return result


def _accounts(_: dict) -> list[dict]:
    if not _table_exists("accounts"):
        return []
    rows = _rows("SELECT id, name, nickname, platform, status, notes, created_at FROM accounts ORDER BY created_at")
    result = [{
        "id": r["id"], "platform": r.get("platform") or "douyin",
        "name": r.get("name") or "", "nickname": r.get("nickname") or "",
        "status": "online" if str(r.get("status")) == "active" else "offline",
        "loginState": "logged_in", "remark": r.get("notes") or "",
        "createdAt": _iso(r.get("created_at")),
    } for r in rows]
    assert_public_safe(result)
    return result


# keyword 搜索命中字段（与线索库搜索框提示「昵称、评论、入口关键词」一致，另含备注/需求）
_KW_FIELDS = ("nickname", "comment", "source_keyword", "note", "customer_need")

# 意向筛选取值归一：中文标签 / 大写等级 / 英文别名 都映射到库中 intent_level 的实际取值
_INTENT_GROUPS: dict[str, tuple[str, ...]] = {
    "高意向": ("a", "high", "hot"), "中意向": ("b", "medium"), "低意向": ("c", "low"),
    "high": ("a", "high", "hot"), "medium": ("b", "medium"), "low": ("c", "low"),
    "a": ("a", "high", "hot"), "b": ("b", "medium"), "c": ("c", "low"),
}

# 触达型筛选的 id 集合上限：超过则放弃该维度并说明，避免拼出巨型 IN 列表
_TOUCH_ID_CAP = 20000

# 本地确实没有记录源的筛选维度（有源的 touchTypes / batchFollowStatus 已真实实现）
_NO_SOURCE_FILTERS = ("accountFlags",)


def _as_list(value: Any) -> list:
    return [v for v in value if v not in (None, "", {})] if isinstance(value, list) else []


def _id_clause(ids: set[str], *, negate: bool = False) -> tuple[str, list] | None:
    """id 集合 -> SQL 片段。空集合返回 None（调用方按语义处理）。"""
    if not ids:
        return None
    if len(ids) > _TOUCH_ID_CAP:
        return None
    ordered = sorted(ids)
    clause = "id " + ("NOT IN " if negate else "IN ") + "(" + ",".join("?" * len(ordered)) + ")"
    return clause, ordered


def _leads_where(filters: Mapping) -> tuple[list[str], list, list[str]]:
    """把前端 filters 结构翻成 SQL 条件。返回 (条件, 参数, 被忽略的维度说明)。"""
    where: list[str] = []
    params: list = []
    notes: list[str] = []

    keyword = str(filters.get("keyword") or "").strip() or str(filters.get("searchKeyword") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        where.append("(" + " OR ".join(f"{f} LIKE ?" for f in _KW_FIELDS) + ")")
        params += [like] * len(_KW_FIELDS)

    intent = filters.get("intention")
    if intent:
        levels = _INTENT_GROUPS.get(str(intent).strip().lower()) or _INTENT_GROUPS.get(str(intent).strip())
        if levels:
            where.append("lower(intent_level) IN (" + ",".join("?" * len(levels)) + ")")
            params += list(levels)

    comment_level = str(filters.get("commentLevel") or "").strip()
    if comment_level == "强意向评论":
        where.append("lower(intent_level) IN ('a','high','hot')")
    elif comment_level == "普通评论":
        where.append("lower(intent_level) NOT IN ('a','high','hot')")

    sources = _as_list(filters.get("entrySource"))
    if sources:
        where.append("source IN (" + ",".join("?" * len(sources)) + ")")
        params += [str(s) for s in sources]

    accounts = _as_list(filters.get("account"))
    if accounts:
        names = [
            str(a.get("name") or a.get("nickname") or a.get("id")) if isinstance(a, Mapping) else str(a)
            for a in accounts
        ]
        where.append("account IN (" + ",".join("?" * len(names)) + ")")
        params += names

    locations = _as_list(filters.get("locations"))
    if locations:
        where.append("region IN (" + ",".join("?" * len(locations)) + ")")
        params += [str(x) for x in locations]

    for key in ("captureTimeRange", "timeRange"):
        rng = filters.get(key)
        start = end = None
        if isinstance(rng, Mapping):
            start, end = rng.get("start") or rng.get("from"), rng.get("end") or rng.get("to")
        elif isinstance(rng, (list, tuple)) and len(rng) == 2:
            start, end = rng[0], rng[1]
        if start:
            where.append("created_at >= ?")
            params.append(str(start))
        if end:
            where.append("created_at <= ?")
            params.append(str(end))
        if start or end:
            break

    # 触达类型筛选：账本里有该类型动作的线索
    touch_types = _as_list(filters.get("touchTypes"))
    if touch_types:
        hit = _touch_ledger().ids_with(touch_types)
        clause = _id_clause(hit)
        if clause:
            where.append(clause[0])
            params += clause[1]
        elif len(hit) > _TOUCH_ID_CAP:
            notes.append("touchTypes 命中线索过多，该维度未生效")
        else:
            where.append("1=0")
            notes.append(
                "触达维度本地无采集记录（" + "/".join(str(t) for t in touch_types) + "），按空结果处理"
            )

    # 批量关注状态：以真实关注记录为准（无关注记录源时「已关注」应为空集）
    follow_status = str(filters.get("batchFollowStatus") or "").strip()
    if follow_status:
        followed = _touch_ledger().ids_with(["follow"])
        wants_followed = follow_status == "已关注" or follow_status.lower() in {"followed", "true"}
        clause = _id_clause(followed, negate=not wants_followed)
        if wants_followed:
            if clause:
                where.append(clause[0])
                params += clause[1]
            else:
                where.append("1=0")
                notes.append("关注维度本地无采集记录，「已关注」按空结果处理")
        elif clause:
            where.append(clause[0])
            params += clause[1]
        # 未关注 + 无任何关注记录 = 全量，无需附加条件

    for key in _NO_SOURCE_FILTERS:
        raw = filters.get(key)
        # batchFollowStatus 是字符串，touchTypes/accountFlags 是数组，两种都算「已选中」
        selected = bool(_as_list(raw)) or (isinstance(raw, str) and raw.strip() != "")
        if selected:
            where.append("1=0")
            notes.append(f"{key} 维度本地无采集记录，按空结果处理")
            break

    return where, params, notes


def _public_rows(rows: list[Mapping]) -> list[dict]:
    """行 -> 展示集，并一次性批量取触达次数（避免逐条查库的 N+1）。"""
    touches = _touch_ledger().per_lead([r.get("id") for r in rows])
    return [_lead_public(r, touches.get(str(r.get("id")))) for r in rows]


def _leads_page(args: dict) -> dict:
    page = max(1, int(args.get("page") or 1))
    size = min(100, max(1, int(args.get("pageSize") or args.get("size") or 20)))
    filters = args.get("filters") if isinstance(args.get("filters"), Mapping) else {}
    if not filters:
        # 兼容旧调用：筛选条件直接平铺在顶层参数上
        filters = {k: v for k, v in args.items() if k not in {"page", "pageSize", "size"}}
    where, params, notes = _leads_where(filters)
    clause = " WHERE " + " AND ".join(where) if where else ""
    total = _scalar(f"SELECT COUNT(*) FROM leads{clause}", tuple(params))
    rows = _rows(
        f"SELECT * FROM leads{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (size, (page - 1) * size),
    )
    items = _public_rows(rows)
    assert_public_safe(items)
    out = {"items": items, "total": total, "runtime": "sqlite", "mode": "live"}
    if notes:
        out["note"] = "；".join(notes)
    return out


def _leads_count(args: dict) -> dict:
    filters = args.get("filters") if isinstance(args.get("filters"), Mapping) else {}
    if not filters:
        filters = {k: v for k, v in args.items()}
    where, params, _ = _leads_where(filters)
    clause = " WHERE " + " AND ".join(where) if where else ""
    return {"total": _scalar(f"SELECT COUNT(*) FROM leads{clause}", tuple(params)),
            "runtime": "sqlite"}


def _leads_ids(args: dict) -> dict:
    filters = args.get("filters") if isinstance(args.get("filters"), Mapping) else {}
    if not filters:
        filters = {k: v for k, v in args.items()}
    where, params, _ = _leads_where(filters)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = _rows(f"SELECT id FROM leads{clause} ORDER BY created_at DESC LIMIT 100000", tuple(params))
    return {"ids": [r["id"] for r in rows], "runtime": "sqlite"}


def _leads_by_ids(args: dict) -> dict:
    ids = [str(i) for i in _as_list(args.get("ids"))][:1000]
    if not ids:
        return {"items": [], "requested": 0, "missing": 0}
    rows = _rows(
        "SELECT * FROM leads WHERE id IN (" + ",".join("?" * len(ids)) + ")",
        tuple(ids),
    )
    items = _public_rows(rows)
    assert_public_safe(items)
    return {"items": items, "requested": len(ids), "missing": max(0, len(ids) - len(items))}


def _leads_for_batch(args: dict) -> dict:
    """批量跟进取数：与列表同筛选口径，上限 5 万条（前端传 limit）。"""
    limit = min(50000, max(1, int(args.get("limit") or 50000)))
    filters = args.get("filters") if isinstance(args.get("filters"), Mapping) else {}
    where, params, _ = _leads_where(filters)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = _rows(f"SELECT * FROM leads{clause} ORDER BY created_at DESC LIMIT ?", tuple(params) + (limit,))
    items = _public_rows(rows)
    assert_public_safe(items)
    return {"items": items, "total": len(items), "runtime": "sqlite"}


def _export_leads(args: Any) -> dict:
    """导出线索：与列表同一筛选口径，一次返回全部命中行（前端据此生成 CSV）。

    前端真正的导出按钮走 get-leads-page(forExport) + save-lead-export-csvs，
    这条是「导出走主进程」的备用路径；两条口径必须一致，否则同一筛选条件
    导出条数与列表对不上。
    """
    data = args if isinstance(args, Mapping) else {}
    filters = data.get("filters") if isinstance(data.get("filters"), Mapping) else {}
    if not filters:
        filters = {k: v for k, v in data.items() if k != "fields"}
    where, params, notes = _leads_where(filters)
    clause = " WHERE " + " AND ".join(where) if where else ""
    rows = _rows(f"SELECT * FROM leads{clause} ORDER BY created_at DESC LIMIT 50000", tuple(params))
    items = _public_rows(rows)
    assert_public_safe(items)
    out: dict[str, Any] = {"items": items, "total": len(items), "runtime": "sqlite", "mode": "live"}
    fields = data.get("fields")
    if isinstance(fields, list) and fields:
        out["fields"] = [str(f) for f in fields]
    if notes:
        out["note"] = "；".join(notes)
    return out


# ---- 线索库 · 批量跟进（任务 / 运行记录 / 日志 / 结果 / 领取规则） ----
def _batch_follow():
    """延迟导入：batch_follow 会回调本模块的 _leads_where 与触达账本。"""
    from app.runtime import batch_follow

    return batch_follow


def _bf_tasks(_: Any) -> dict:
    return _batch_follow().tasks(DB_PATH)


def _bf_save(payload: Any) -> dict:
    return _batch_follow().save_task(DB_PATH, payload)


def _bf_control(payload: Any) -> dict:
    return _batch_follow().control_task(DB_PATH, payload)


def _bf_runs_page(payload: Any) -> dict:
    return _batch_follow().runs_page(DB_PATH, payload)


def _bf_run_details(payload: Any) -> dict:
    return _batch_follow().run_details(DB_PATH, payload)


def _bf_user_history(payload: Any) -> dict:
    return _batch_follow().user_history(DB_PATH, payload)


def _bf_clear_runs(_: Any) -> dict:
    return _batch_follow().clear_runs(DB_PATH)


def _bf_delete_runs(payload: Any) -> dict:
    return _batch_follow().delete_runs(DB_PATH, payload)


def _bf_clear_run_logs(payload: Any) -> dict:
    return _batch_follow().clear_run_logs(DB_PATH, payload)


def _bf_cancel_user_history(payload: Any) -> dict:
    return _batch_follow().cancel_user_history(payload)


def _bf_delete_failed_leads(_: Any) -> dict:
    return _batch_follow().delete_failed_leads(DB_PATH)


def _bf_test_proxy(payload: Any) -> dict:
    return _batch_follow().test_proxy(payload)


def _bf_start(payload: Any) -> dict:
    return _batch_follow().start_batch(DB_PATH, payload)


def _bf_stop(payload: Any) -> dict:
    return _batch_follow().stop_batch(payload)


# ---- 养号安全暂停（#38/#39：「处理后继续」此前打到一个未注册的通道，必报错） ----
def _nurture_safety_resume(payload: Any) -> dict:
    from app.runtime import nurture_safety

    return nurture_safety.resume(DB_PATH, payload)


def _nurture_safety_state(payload: Any) -> dict:
    from app.runtime import nurture_safety

    return nurture_safety.state(DB_PATH, payload)


# ---- 采集关键词（联想词扩词）：前端弹窗早已存在，此前后端 6 个通道一个都没注册 ----
def _kwc_start(payload: Any) -> dict:
    from app.runtime import keyword_collector

    return keyword_collector.start(DB_PATH, payload)


def _kwc_stop(payload: Any) -> dict:
    from app.runtime import keyword_collector

    return keyword_collector.stop(DB_PATH, payload)


def _kwc_state(payload: Any) -> dict:
    from app.runtime import keyword_collector

    return keyword_collector.state_of(DB_PATH, payload)


def _kwc_show(payload: Any) -> dict:
    from app.runtime import keyword_collector

    return keyword_collector.show(DB_PATH, payload)


def _kwc_resume(payload: Any) -> dict:
    from app.runtime import keyword_collector

    return keyword_collector.resume(DB_PATH, payload)


def _leads_stats(_: dict) -> dict:
    """线索池统计。所有数字都来自触达账本，测试/模拟记录已剔除。"""
    total = _scalar("SELECT COUNT(*) FROM leads")
    totals = _touch_ledger().totals()
    touched_by_record = len(_touch_ledger().touched_ids())
    # 业务状态里已推进的线索也算「已触达」，与账本取并集（二者都是真数据）
    touched_by_status = _scalar(
        "SELECT COUNT(*) FROM leads WHERE status NOT IN ('','collected','pending_outreach')"
    )
    touched = min(total, max(touched_by_record, touched_by_status))
    report = _touch_ledger().report()
    stats = {
        "total": total,
        "likes": totals.get("like", 0),
        "replies": totals.get("reply", 0),
        "messages": totals.get("message", 0),
        "follows": totals.get("follow", 0),
        "profileComments": totals.get("profileComment", 0),
        "touched": touched,
        "untouched": max(0, total - touched),
        "touchedByRecord": touched_by_record,
        "touchedByStatus": touched_by_status,
    }
    notes = []
    if report["missing"]:
        notes.append("无记录源按 0 统计：" + "/".join(report["missing"]))
    if report["dropped_test_records"]:
        notes.append(f"已剔除 {report['dropped_test_records']} 条测试/模拟发送记录")
    return {
        "stats": stats, "runtime": "sqlite", "sources": report,
        "note": "；".join(notes) or "全部为真实触达记录",
    }


def _filter_options(_: dict) -> dict:
    accounts = [{"id": a["id"], "name": a["name"], "nickname": a["nickname"]} for a in _accounts({})]
    keywords = [r["k"] for r in _rows(
        "SELECT DISTINCT source_keyword AS k FROM leads "
        "WHERE source_keyword IS NOT NULL AND source_keyword<>'' ORDER BY k LIMIT 50"
    )]
    locations = [r["k"] for r in _rows(
        "SELECT DISTINCT region AS k FROM leads WHERE region IS NOT NULL AND region<>'' ORDER BY k LIMIT 30"
    )]
    return {"accounts": accounts, "keywords": keywords, "locations": locations}


def _reconcile_leadgen_records(records: dict[str, dict]) -> dict[str, dict]:
    """对账自愈：把「标着 running 但后端根本没有活跃线程」的幽灵记录归位为 stopped。

    为什么必须做：壳的重启守卫直接看任务记录的 status 字段
    （restartFromTask.status==="running" 即拦截「该任务仍在运行中」），
    而停止链路里 update-leadgen-task 补丁一旦丢失（壳异常 / 断连 / 旧版本停止路径），
    记录会永远残留在 running——列表徽标显示已停止、点重启却被拦，状态自相矛盾。
    后端执行器（task_runner）才是「是否真在跑」的唯一事实源。
    """
    live = {r["taskId"] for r in task_runner.active_records()}
    from app.execution.live_workflow import preparing_records
    live.update(r["taskId"] for r in preparing_records())
    live.update(comment_likes.service(DB_PATH).active())
    # Service initialization may recover an interrupted simulation after these
    # records were loaded. Keep its durable recovery status instead of rewriting
    # the stale in-memory copy as a generic stopped task.
    recovered = leadgen_pools.load_records(DB_PATH)
    if not live:
        live = set()  # 明确空集，避免下方交集语义歧义
    changed: list[str] = []
    now_ms = int(time.time() * 1000)
    for rid, rec in records.items():
        if rec.get("reviewedAccounts"):
            records[rid] = leadgen_pools.recover_reviewed_record(DB_PATH, rid, live, now_ms)
            continue
        latest = recovered.get(str(rid), {})
        if comment_likes.is_like_record(rec) and latest.get("status") == "interrupted":
            rec.update(latest)
            continue
        if str(rec.get("status") or "") != "running":
            continue
        run_ids = {str(rid)}
        runs = rec.get("runs") if isinstance(rec.get("runs"), list) else []
        for r in runs:
            if isinstance(r, dict):
                for key in ("automationTaskId", "taskId", "runTaskId"):
                    if r.get(key):
                        run_ids.add(str(r[key]))
        if run_ids & live:
            continue  # 确实还有活跃线程，不动
        rec["status"] = "stopped"
        if not rec.get("endedAt"):
            rec["endedAt"] = now_ms
        if not rec.get("endReason"):
            rec["endReason"] = "stopped"
        for r in runs:
            if isinstance(r, dict) and not r.get("endedAt"):
                r["endedAt"] = rec["endedAt"]
                r["endReason"] = r.get("endReason") or "stopped"
        changed.append(str(rid))
    if changed:
        # 自愈结果落库，下次加载不再重复对账
        for rid in changed:
            leadgen_pools.update_task_record(DB_PATH, rid, records[rid])
    return records


def _tasks(kind: str) -> Callable[[dict], list[dict]]:
    def build(_: dict) -> list[dict]:
        if kind in {"leadgen", "entity"}:
            if not _table_exists("crawl_tasks"):
                rows = []
                result = []
            elif kind == "leadgen":
                rows = _rows("SELECT * FROM crawl_tasks WHERE crawl_type='comment' ORDER BY created_at DESC")
                result = [_task_shape(r, source_table="crawl_tasks") for r in rows]
            else:
                rows = _rows("SELECT * FROM crawl_tasks WHERE crawl_type<>'comment' ORDER BY created_at DESC")
                result = [_task_shape(r, source_table="crawl_tasks") for r in rows]
            # 壳保存的任务记录（save-leadgen-task / save-entity-leadgen-task）优先展示：
            # 含 runs / stats / configSnapshot。两类任务共用同一个记录仓，
            # 用 entityKind 标记区分归属，避免评论获客任务串到关键词获客列表（反之亦然）。
            records = _reconcile_leadgen_records(leadgen_pools.load_records(DB_PATH))
            want_entity = kind == "entity"
            records = {
                rid: rec for rid, rec in records.items()
                if (str(rec.get("entityKind") or "") == "entity") == want_entity
            }
            if want_entity:
                # 评论获客任务（无 entityKind，leadgen 引擎执行）也要在「运行中」
                # 出现在本列表：实况大屏刷新后按 运行id ∩ 本列表 生成卡片，
                # 缺了它任务在跑、大屏空屏（2026-11-14 用户实测）。
                # 只并运行中的，避免停止的评论获客任务串进实体页任务列表。
                try:
                    from app.api import task_runner

                    running = set(task_runner.active_ids())
                    if running:
                        all_records = _reconcile_leadgen_records(
                            leadgen_pools.load_records(DB_PATH))
                        for rid, rec in all_records.items():
                            if (rid in running
                                    and str(rec.get("entityKind") or "") != "entity"):
                                records.setdefault(rid, rec)
                except Exception:  # noqa: BLE001
                    pass
            if records:
                by_id = {str(t.get("id")): t for t in result}
                merged: list[dict] = []
                seen: set[str] = set()
                for rec in sorted(
                    records.values(),
                    key=lambda r: str(r.get("createdAt") or 0),
                    reverse=True,
                ):
                    rid = str(rec.get("id") or "")
                    base = by_id.get(rid)
                    if base:
                        item = {**base, **{k: v for k, v in rec.items() if v is not None}}
                    else:
                        item = dict(rec)
                    merged.append(item)
                    seen.add(rid)
                # 壳的记录是增量，不能因此抹掉库里已有的历史任务
                merged.extend(t for t in result
                              if str(t.get("id")) not in seen)
                result = merged
            # 僵尸 running 归一：后端早已重启、执行线程不在了，
            # crawl_tasks 里 9 月的旧行却还挂着 running，
            # 前端「所选含运行中就整批拦下」会让批量删除永远删不掉。
            # 以当前真实活跃集合为准，只有真的在跑的才保留 running。
            try:
                # build 内部有局部 `from app.api import task_runner`（want_entity 分支），
                # 会把 task_runner 变成整个函数的局部名；这里必须用独立别名，
                # 否则归一执行时局部名尚未绑定 → UnboundLocalError 被吞、活跃集恒空。
                from app.api import task_runner as _tr
                _active_ids = {str(i) for i in _tr.active_ids()}
            except Exception:  # noqa: BLE001
                _active_ids = set()
            try:
                # 两个来源分开兜底：一个不可用不能清空另一个的活跃集
                _active_ids |= {
                    str(i) for i in comment_likes.service(DB_PATH).active()}
            except Exception:  # noqa: BLE001
                pass
            for _t in result:
                if str(_t.get("status")) != "running":
                    continue
                # 运行标识可能在记录 id 上，也可能在 runs[].automationTaskId 上
                _run_ids = {
                    str((r or {}).get("automationTaskId") or "")
                    for r in (_t.get("runs") or [])}
                if (str(_t.get("id")) in _active_ids
                        or (_run_ids & _active_ids)):
                    continue
                _t["status"] = "stopped"
                _t["staleStatus"] = True
        else:
            # 新任务由 save-monitor-task / save-self-warmup-task / save-work-publish-task
            # 写入 tasks.payload（完整 taskRecord），优先用原样记录，配置快照与运行账号不丢。
            result = task_store.list_tasks(DB_PATH, kind)
            seen = {str(t.get("id")) for t in result}
            if _table_exists("tasks"):
                pattern = {"monitor": "%live%", "warmup": "%warm%", "publish": "%publish%"}[kind]
                rows = _rows(
                    "SELECT * FROM tasks WHERE task_type LIKE ? ORDER BY created_at DESC", (pattern,)
                )
                result.extend(
                    _task_shape(r, source_table="tasks") for r in rows
                    if str(r.get("id")) not in seen
                )
        assert_public_safe(result)
        return result
    return build


def _task_detail(args: dict) -> dict:
    task_id = str(args.get("id") or "")
    task = None
    if task_id and _table_exists("crawl_tasks"):
        row = _one("SELECT * FROM crawl_tasks WHERE id=?", (task_id,))
        if row:
            task = _task_shape(row, source_table="crawl_tasks")
    if task is None:
        first = _tasks("leadgen")({})
        task = first[0] if first else None
    leads = _rows(
        "SELECT * FROM leads WHERE source_task_id=? ORDER BY created_at DESC LIMIT 12", (task_id,)
    ) if task_id else []
    items = [_lead_public(r) for r in leads]
    return {
        "success": True, "task": task, "leads": items,
        "total": _scalar("SELECT COUNT(*) FROM leads WHERE source_task_id=?", (task_id,)) if task_id else 0,
    }


_VIS_STATUS_COLUMNS = ("visibility_status", "visibility_reason", "visibility_target_account",
                       "visibility_target_name", "visibility_sample_kind",
                       "visibility_sample_content", "visibility_cleanup", "cleanup_deleted_at")


def _visibility_filters(args: dict) -> tuple[str, list]:
    """明细筛选（吞评检测页共用）：结果状态 / 关键词 / 起始时间。

    - visibilityStatus=all 或空 → 不过滤；failed → 全部非正常结果（not_visible /
      inconclusive / no_sample）；其余按 visibility_status 精确匹配。
    - sinceTs（毫秒）→ created_at（本地 ISO 秒级文本）之后的行。
    """
    where, params = [], []
    status = str(args.get("visibilityStatus") or "all").strip().lower()
    if status and status != "all":
        if status == "failed":
            where.append("AND (TRIM(COALESCE(visibility_status,''))<>'') "
                         "AND TRIM(COALESCE(visibility_status,''))<>'visible'")
        else:
            where.append("AND TRIM(COALESCE(visibility_status,''))=?")
            params.append(status)
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        where.append("AND (nickname LIKE ? OR comment LIKE ? "
                     "OR COALESCE(visibility_sample_content,'') LIKE ?)")
        like = f"%{keyword}%"
        params += [like, like, like]
    since = int(args.get("sinceTs") or 0)
    if since > 0:
        iso = datetime.fromtimestamp(since / 1000).astimezone().isoformat(timespec="seconds")
        where.append("AND created_at>=?")
        params.append(iso)
    return ("".join(where) if where else ""), params


def _task_leads(args: dict) -> dict:
    page = max(1, int(args.get("page") or 1))
    size = min(100, max(1, int(args.get("limit") or args.get("pageSize") or 20)))
    offset = max(0, int(args.get("offset") or 0)) or (page - 1) * size
    task_id = str(args.get("id") or args.get("taskId") or "")
    if not task_id or not _table_exists("leads"):
        return {"success": True, "items": [], "total": 0,
                "counts": {"all": 0, "comments": 0, "video": 0, "author": 0}}
    # tab：author=作者行（entity_author）；video=视频行；comments=其余（含吞评检测结论）
    tab = str(args.get("tab") or "").strip().lower()
    tab_where = ""
    if tab == "author":
        tab_where = "AND source='entity_author'"
    elif tab == "video":
        tab_where = "AND source='entity_video'"
    elif tab == "comments":
        tab_where = "AND source<>'entity_author'"
    vis_where, vis_params = _visibility_filters(args)
    where = f"WHERE source_task_id=? {tab_where} {vis_where}"
    params: tuple = (task_id, *vis_params)
    rows = _rows(
        f"SELECT * FROM leads {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, size, offset))
    total = _scalar(f"SELECT COUNT(*) FROM leads {where}", params)
    base = f"WHERE source_task_id=?"
    counts = {
        "all": _scalar(f"SELECT COUNT(*) FROM leads {base}", (task_id,)),
        "comments": _scalar(f"SELECT COUNT(*) FROM leads {base} AND source<>'entity_author'", (task_id,)),
        "video": _scalar(f"SELECT COUNT(*) FROM leads {base} AND source='entity_video'", (task_id,)),
        "author": _scalar(f"SELECT COUNT(*) FROM leads {base} AND source='entity_author'", (task_id,)),
    }
    return {"success": True, "items": [_lead_public(r) for r in rows],
            "total": total, "counts": counts}


# --------------------------------------------------------------------------
# 吞评检测明细：导出 / 删除关联记录 / 清空（2026-11-17 补齐真实通道）
# --------------------------------------------------------------------------
def _ensure_visibility_columns_ready() -> None:
    from app.runtime.store import _ensure_visibility_columns

    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads'").fetchone():
            _ensure_visibility_columns(con)
        con.commit()
    finally:
        con.close()


def _export_visibility_details(payload: dict) -> dict:
    """把检测明细按当前筛选导出成 CSV（落 EXPORT_DIR/visibility，BOM 头防乱码）。"""
    import csv
    import io

    data = payload if isinstance(payload, Mapping) else {}
    task_id = str(data.get("taskId") or "")
    if not task_id:
        return {"success": False, "error": "缺少任务 id，无法导出"}
    if not _table_exists("leads"):
        return {"success": False, "error": "本地暂无检测记录可导出"}
    _ensure_visibility_columns_ready()
    where = ("WHERE source_task_id=? AND source='entity_comment_visibility'")
    params: list = [task_id]
    vis_where, vis_params = _visibility_filters(data)
    if vis_where:
        where += f" {vis_where}"
        params += vis_params
    rows = _rows(f"SELECT * FROM leads {where} ORDER BY created_at DESC", tuple(params))
    if not rows:
        return {"success": False, "error": "当前筛选条件下没有可导出的检测记录"}

    verdicts = {"visible": "正常", "not_visible": "疑似吞评",
                "inconclusive": "检测未完成", "no_sample": "无可用样本"}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["时间", "被检账号", "样本类型", "样本内容", "检测结果",
                     "原因", "视频链接", "关联记录"])
    for r in rows:
        writer.writerow([
            _fmt(r.get("created_at")),
            r.get("visibility_target_name") or r.get("nickname") or "",
            r.get("visibility_sample_kind") or "main",
            r.get("visibility_sample_content") or r.get("comment") or "",
            verdicts.get(str(r.get("visibility_status") or ""), "检测未完成"),
            r.get("visibility_reason") or "",
            r.get("source_url") or "",
            ("已删除" + (f"（{_fmt(r.get('cleanup_deleted_at'))}）" if r.get("cleanup_deleted_at") else ""))
            if r.get("visibility_cleanup") else "保留",
        ])
    folder = EXPORT_DIR / "visibility"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"visibility_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    target = folder / name
    target.write_bytes(buf.getvalue().encode("utf-8-sig"))
    return {"success": True, "count": len(rows), "path": str(target),
            "url": f"/exports/visibility/{name}"}


def _delete_visibility_related(payload: dict) -> dict:
    """删除检测记录的本地关联数据：历史视频溯源 + 被检账号的视频主评回执。

    检测明细本身保留，只打删除标记与时间（页面显示「关联记录已删除」）。
    历史视频记录由账号共用（弹窗已提示）；其他账号的视频主评不删。
    """
    data = payload if isinstance(payload, Mapping) else {}
    task_id = str(data.get("taskId") or "")
    lead_ids = _ids_payload(data.get("leadIds") or [])
    if not task_id or not lead_ids:
        return {"success": False, "error": "请先勾选要删除关联记录的检测明细"}
    if not _table_exists("leads"):
        return {"success": False, "error": "本地暂无检测记录"}
    _ensure_visibility_columns_ready()
    ph = ",".join("?" * len(lead_ids))
    rows = _rows(
        f"SELECT * FROM leads WHERE source_task_id=? AND source='entity_comment_visibility' "
        f"AND id IN ({ph})", (task_id, *lead_ids))
    if not rows:
        return {"success": False, "error": "未找到这些检测明细（可能已被删除）"}

    marked = _exec_write(
        f"UPDATE leads SET visibility_cleanup='deleted', cleanup_deleted_at=?, updated_at=? "
        f"WHERE id IN ({ph})",
        (_now_text(), _now_text(), *lead_ids))
    urls = {str(r.get("source_url") or "").strip() for r in rows
            if str(r.get("source_url") or "").strip()}
    targets = {str(r.get("visibility_target_account") or "").strip() for r in rows
               if str(r.get("visibility_target_account") or "").strip()}
    videos_removed = comments_removed = 0
    if urls and _table_exists("live_action_results"):
        url_ph = ",".join("?" * len(urls))
        # 历史视频记录：采集/历史视频选择的共享溯源（search-video）
        videos_removed = _exec_write(
            f"DELETE FROM live_action_results WHERE action='search-video' "
            f"AND url IN ({url_ph})", tuple(urls))
        if targets:
            target_ph = ",".join("?" * len(targets))
            # 被检账号的视频主评回执：只删被检账号的，其他账号保留
            comments_removed = _exec_write(
                f"DELETE FROM live_action_results WHERE action='post-comment' "
                f"AND url IN ({url_ph}) AND account_id IN ({target_ph})",
                (*urls, *targets))
    return {"success": True, "marked": marked,
            "videosRemoved": videos_removed, "commentsRemoved": comments_removed}


def _delete_entity_lead_records(payload: dict) -> dict:
    """清空任务明细：删掉本任务写入 leads 的行，返回重置后的统计（页面直接替换）。"""
    data = payload if isinstance(payload, Mapping) else {}
    task_id = str(data.get("taskId") or "")
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}
    if not _table_exists("leads"):
        return {"success": False, "error": "本地暂无明细数据"}
    _ensure_visibility_columns_ready()
    if data.get("clearAll") is True:
        deleted = _exec_write("DELETE FROM leads WHERE source_task_id=?", (task_id,))
    else:
        ids = _ids_payload(data.get("leadIds") or [])
        if not ids:
            return {"success": False, "error": "未指定要删除的明细"}
        ph = ",".join("?" * len(ids))
        deleted = _exec_write(
            f"DELETE FROM leads WHERE source_task_id=? AND id IN ({ph})",
            (task_id, *ids))

    stats: dict = {}
    try:
        record = leadgen_pools.load_records(DB_PATH).get(task_id) or {}
        stats = dict(record.get("stats") or {})
    except sqlite3.Error:
        stats = {}
    remaining = _scalar("SELECT COUNT(*) FROM leads WHERE source_task_id=?", (task_id,))
    stats.update({
        "collected": remaining,
        "user": remaining,
        "visibilityAccounts": [],
        "visibilitySampleCounts": {"requested": 0, "sampled": 0, "normal": 0,
                                   "abnormal": 0, "inconclusive": 0, "missing": 0},
        "visibility_visible": 0, "visibility_suspected": 0,
        "visibility_inconclusive": 0, "visibility_no_sample": 0,
        "visibilityRunStartedAt": 0,
    })
    try:
        leadgen_pools.update_task_record(DB_PATH, task_id, {"stats": stats})
    except sqlite3.Error:
        pass
    return {"success": True, "deleted": deleted, "stats": stats}


def _task_stats(args: dict) -> dict:
    task_id = str(args.get("id") or "")
    row = _one("SELECT * FROM crawl_tasks WHERE id=?", (task_id,)) if task_id else None
    task = _task_shape(row, source_table="crawl_tasks") if row else None
    return {
        "success": True,
        "stats": task["stats"] if task else _task_shape({"id": "", "status": "completed"}, source_table="")["stats"],
        "total": task["leadCount"] if task else 0,
    }


def _dashboard(_: dict) -> dict:
    """账号数据看板：交给 aggregate.account_dashboard 按账号/按天做真实聚合。"""
    return aggregate.account_dashboard(_)


def _scheduler_config(_: dict) -> dict:
    row = _one("SELECT * FROM scheduler_config LIMIT 1") if _table_exists("scheduler_config") else None
    return {"success": True, "config": row or {}}


# --------------------------------------------------------------------------
# AI 智能体（人设库）：真实持久化到 system_settings，跨重启可恢复。
#
# 前端契约（bundle 内 TaskStore）：
#   get-task-config  -> 含 aiInitialized 时读 .config，否则整个返回值当配置；
#                       返回 null 表示库里没有，前端用本地副本并回写（首次引导）。
#   save-ai-agents   -> 入参是 personas 数组，必须严格返回 true
#   delete-ai-agent  -> 入参是 persona id 字符串，必须严格返回 true
# --------------------------------------------------------------------------
def _task_config(_: dict) -> Any:
    cfg = ai_personas.load_task_config(DB_PATH)
    if not cfg:
        return None
    return {"success": True, "aiInitialized": True, "config": cfg}


def _save_task_config(payload: dict) -> dict:
    if not isinstance(payload, dict) or not payload:
        # 空包（含契约测试的空参数调用）不落库：写一个空配置会让前端下次启动
        # 用空配置覆盖本地副本，属于数据丢失级副作用。
        return {"success": True, "skipped": "empty"}
    if ai_personas.save_task_config(DB_PATH, payload):
        return {"success": True}
    return {"success": False, "msg": "任务配置未能写入本地数据库"}


def _save_ai_agents(args: Any) -> Any:
    items = args[0] if isinstance(args, list) and args else None
    if not isinstance(items, list):
        return False
    ok, _count = ai_personas.save_personas(DB_PATH, items)
    return ok


def _delete_ai_agent(args: Any) -> Any:
    pid = args[0] if isinstance(args, list) and args else args
    return ai_personas.delete_persona(DB_PATH, str(pid or ""))


def _persona_test_quota(_: dict) -> dict:
    return {"success": True, "data": ai_personas.persona_test_quota(DB_PATH, license_local.auth_overlay())}


def _persona_test(payload: dict) -> dict:
    return ai_personas.run_persona_test(DB_PATH, payload, license_local.auth_overlay())


def _test_task_model(payload: dict) -> dict:
    from app.execution.live_leadgen import generate, GenerationError
    try:
        result = generate(DB_PATH, {}, {"title": "智能体工具调用、记忆和工作流设计"})
        return {"success": bool(result.get("ok")), "text": result.get("text", ""),
                "error": result.get("reason", "") if not result.get("ok") else ""}
    except GenerationError as exc:
        return {"success": False, "error": str(exc)}


def _preview_leadgen_live(payload: dict) -> dict:
    from app.execution.live_workflow import preview
    from app.execution.live_leadgen import GenerationError
    try:
        return preview(DB_PATH, payload)
    except (ValueError, GenerationError) as exc:
        return {"success": False, "error": str(exc)}
    except Exception:
        return {"success": False, "error": "准备草稿失败，请检查桌面登录、模型配置和后台日志；未发送"}


def _validate_leadgen_config(payload: dict) -> dict:
    from app.execution.leadgen_config import inspect
    return inspect(payload, account_id=str(payload.get("accountId") or ""))


def _cancel_leadgen_prepared(payload):
    tid, aid, run = payload.get("leadgenTaskId"), payload.get("accountId"), payload.get("taskId")
    if any(r["taskId"] == run for r in task_runner.active_records()):
        return {"success": False, "error": "运行中的任务请使用停止"}
    from app.execution.live_scope import desktop_leadgen_scope_file
    scope_file = desktop_leadgen_scope_file(str(tid or ""), str(aid or ""))
    try:
        scope = json.loads(scope_file.read_text(encoding="utf-8"))
        if scope.get("runTaskId") == run:
            scope_file.unlink()
    except (OSError, ValueError, TypeError):
        pass
    rec = leadgen_pools.load_records(DB_PATH).get(tid, {})
    account = rec.get("reviewedAccounts", {}).get(aid, {})
    if account.get("runTaskId") == run and account.get("status") == "prepared":
        ended = int(time.time() * 1000)
        leadgen_pools.reviewed_run_update(DB_PATH, tid, aid, run, result={
            "status": "failed", "stats": {}, "endedAt": ended, "endReason": "start_rejected",
            "message": "任务启动失败，未执行已确认清单", "run": {"automationTaskId": run,
            "accountId": aid, "status": "failed", "endedAt": ended, "stats": {}}})
    return {"success": True}


def _binding_changes(snapshot, current, limit: int = 4) -> list:
    """列出「准备授权」与「本次启动」之间被改动的参数，用来代替笼统的报错。"""
    if not isinstance(snapshot, dict) or not isinstance(current, dict):
        return []

    def short(value):
        if value is None:
            return "未设置"
        try:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
        text = text.replace("\n", " ")
        return text if len(text) <= 36 else text[:35] + "…"

    changes = []
    for key in sorted(set(snapshot) | set(current)):
        old, new = snapshot.get(key), current.get(key)
        if old == new:
            continue
        changes.append("%s %s → %s" % (key, short(old), short(new)))
    if len(changes) > limit:
        changes = changes[:limit] + ["等 %d 处" % len(changes)]
    return changes


def _scope_activation_problems(scope: dict, task_payload: dict) -> list:
    """逐项说明授权范围为什么不匹配，而不是一句「不一致」。

    2026-11-20 用户反馈：只看到「任务授权范围与启动参数不一致，请重新启动」，
    完全不知道该改哪一项。这里把原来的合并布尔拆开，每条都写清
    「哪一项 / 当前值 / 期望值 / 怎么处理」，并按最可能是人为原因的顺序排。
    """
    from datetime import datetime as _dt

    def clock(ms: float) -> str:
        return _dt.fromtimestamp(ms).strftime("%H:%M:%S")

    problems = []
    account_id = str(task_payload.get("accountId") or "")
    leadgen_task_id = str(task_payload.get("leadgenTaskId") or "")
    run_task_id = str(task_payload.get("taskId") or "")

    # 1) 授权是否还有效 —— 最常见：点过「保存并启动」，隔太久才真正启动。
    expires_at = scope.get("expiresAt")
    if type(expires_at) is not int:
        problems.append("授权范围没有有效期限，无法确认是否仍然有效；请重新点「保存并启动」")
    elif expires_at <= time.time():
        problems.append("授权已过期：有效期到 %s，现在已经是 %s；请在任务页重新点「保存并启动」"
                        % (clock(expires_at), clock(time.time())))

    # 2) 授权范围本身是否完整 —— 属于程序侧问题，但要说清缺什么。
    stream_scope = scope.get("mode") == "stream"
    if stream_scope:
        config = scope.get("effectiveConfig")
        caps = scope.get("actions")
        if scope.get("videoUrls") != ["*"]:
            problems.append("授权范围不是按「顺序浏览」方式生成（videoUrls 应为全部视频）；请重新点「保存并启动」")
        if not isinstance(config, dict) or not config:
            problems.append("授权范围缺少生效配置（effectiveConfig 为空）；请重新点「保存并启动」")
        if not isinstance(caps, dict) or not caps:
            problems.append("本次配置里没有任何可执行的互动动作（视频点赞、主评、评论回复、关注、私信全部关闭）；"
                            "请至少打开一项后重新保存")
        else:
            sources = (config or {}).get("videoSources") if isinstance(config, dict) else None
            entry = sources[0] if isinstance(sources, list) and sources else ""
            keywords = scope.get("keywords")
            # 只有「搜索关键词」「话题」需要关键词；推荐页/关注列表/喜欢列表/指定视频
            # 天然没有关键词。旧实现对全部入口都要求非空，导致推荐页永远启动不了。
            if entry in {"search", "hashtag"} and (not isinstance(keywords, list) or not keywords):
                problems.append("视频入口是「%s」，但授权范围里没有任何关键词；请填写关键词后重新启动"
                                % ("搜索关键词" if entry == "search" else "话题"))
    elif not (isinstance(scope.get("reviewedActions"), list) and scope["reviewedActions"]):
        problems.append("授权范围里没有已确认的动作清单（既不按顺序浏览、也没有逐条确认的动作）；"
                        "请在任务页重新确认后启动")

    # 3) 授权密钥是否可用。
    if not isinstance(scope.get("secret"), str) or len(scope.get("secret") or "") < 32:
        problems.append("授权密钥无效或过短（可能被截断）；请重新点「保存并启动」")

    # 4) 账号 / 任务 / 运行编号是否对得上 —— 常见于列表没刷新、切换了账号。
    if not account_id:
        problems.append("启动参数里没有执行账号；请先选择账号")
    elif (scope.get("accountId") or "") != account_id:
        problems.append("授权范围属于账号「%s」，本次启动的却是「%s」；请刷新任务列表后重新启动"
                        % (scope.get("accountId") or "空", account_id))
    if not leadgen_task_id:
        problems.append("启动参数里没有任务编号；请重新打开这条任务")
    elif (scope.get("leadgenTaskId") or "") != leadgen_task_id:
        problems.append("授权范围属于任务「%s」，本次启动的却是「%s」；请重新点「保存并启动」"
                        % (scope.get("leadgenTaskId") or "空", leadgen_task_id))
    if not run_task_id:
        problems.append("启动参数里没有本次运行编号；请重新点「保存并启动」")
    elif (scope.get("runTaskId") or "") != run_task_id:
        problems.append("授权范围绑定的是上一次运行（runTaskId %s），本次运行是 %s；"
                        "可能是重复点击了启动，请等当前任务结束或先停止"
                        % (scope.get("runTaskId") or "空", run_task_id))

    # 5) 参数指纹 —— 准备之后配置被改过，明确说是哪几项变了。
    expected = hashlib.sha256(json.dumps(task_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    if scope.get("payloadBinding") != expected:
        changes = _binding_changes(scope.get("payloadSnapshot"), task_payload)
        if changes:
            problems.append("准备授权之后启动参数被改动了（%s），为防越权已取消；"
                            "请回到任务页核对后重新点「保存并启动」" % "、".join(changes))
        else:
            problems.append("启动参数与准备授权时记录的指纹不一致（配置在准备过程中被改动，"
                            "或页面与后端不是同一次提交）；请重新点「保存并启动」")
    return problems


def _activate_leadgen_scope(payload: dict) -> dict:
    """Persist a natively reviewed scope where a reused backend can find it."""
    from app.execution.live_scope import desktop_leadgen_scope_file

    scope = payload.get("scope") if isinstance(payload, dict) else None
    task_payload = payload.get("taskPayload") if isinstance(payload, dict) else None
    if not isinstance(scope, dict) or not isinstance(task_payload, dict):
        return {"success": False, "error": "缺少已确认的任务作用域，请回到任务页重新点「保存并启动」"}
    problems = _scope_activation_problems(scope, task_payload)
    if problems:
        return {"success": False, "error": "；".join(problems), "problems": problems}
    account_id = str(task_payload.get("accountId") or "")
    leadgen_task_id = str(task_payload.get("leadgenTaskId") or "")
    path = desktop_leadgen_scope_file(leadgen_task_id, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".next")
    temporary.write_text(json.dumps(scope, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return {"success": True, "scopeFile": str(path)}


def _start_task(payload: dict) -> dict:
    """任务启动总入口 —— 按 taskMode 分流，别把不同模块塞进同一个执行器。

    页面用的同一个通道（start-task），但传的 taskMode 不同：
        nurture     养号（浏览推荐流 / 概率点赞）→ runtime/registry 的 warmup 执行器
        其它        评论获客（关键词下发表主评）→ task_runner 门禁
    之前统一转给评论获客门禁，结果养号一启动就被「执行器未接入」拒掉（P0）。
    """
    mode = str((payload or {}).get("taskMode") or "").strip().lower()
    if mode == "nurture":
        return _start_nurture_task(payload)
    res = task_runner.start(DB_PATH, payload)
    if not res.get("success"):
        return {"success": False, "msg": res.get("error") or "任务启动失败", **res}
    return res


def _live_acceptance_action(payload: dict) -> dict:
    from app.execution.live_leadgen import perform
    from app.execution.live_scope import task_allowed
    if not task_allowed(payload):
        return {"success": False, "error": "未配置此账号与任务的真机验收范围"}
    # No free-form content or recipient accepted here. Main/reply text belongs to
    # the reviewed task flow. This diagnostics entry only covers video utilities.
    action = str(payload.get("action") or "")
    if action not in {"copy-video-link", "favorite-video", "like-video"}:
        return {"success": False, "error": "此入口仅支持复制链接、收藏和视频点赞"}
    from app.runtime import registry
    if task_runner.busy() or registry.active():
        return {"success": False, "error": "请先等待当前任务结束"}
    result = perform(DB_PATH, action, account_id=payload["accountId"],
                     task_id=payload["leadgenTaskId"], video_url=str(payload.get("videoUrl") or ""))
    return {"success": bool(result.get("ok")), **result}


def _live_acceptance_info(payload: dict) -> dict:
    from app.execution.live_scope import load_scope
    scope = load_scope()
    if not scope:
        return {"enabled": False}
    rows = _rows("SELECT action,status,code,created_ms FROM live_action_results WHERE task_id=? "
                 "ORDER BY id DESC LIMIT 12", (scope["leadgenTaskId"],))
    return {"enabled": True, "accountId": scope["accountId"], "nickname": scope["nickname"],
            "leadgenTaskId": scope["leadgenTaskId"], "videoUrl": scope["videoUrls"][0],
            "results": rows, "expiresAt": scope["expiresAt"]}


def _start_nurture_task(payload: dict) -> dict:
    """养号启动：页面传的是**扁平配置**（nurtureDurationMin / stayMin / ...）。

    落地两步：先把整条记录存成 warmup 任务（配置包进 configSnapshot，runner 才读得到），
    再交给 registry 起线程。回执带回真实生效参数，页面对账不用猜。
    """
    from app.runtime import registry, store

    rec = dict(payload or {})
    task_id = str(rec.get("taskId") or rec.get("id") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少 taskId，无法启动"}
    account_id = str(rec.get("accountId") or "").strip()
    if not account_id:
        return {"success": False, "error": "未指定执行账号"}

    remaining = store.warmup_cooldown_seconds(DB_PATH, account_id)
    if remaining:
        return {"success": False, "taskId": task_id, "cooldown": True,
                "retryAfterSec": remaining,
                "error": f"距上次养号结束不足 3 分钟，请 {remaining} 秒后再启动"}

    saved_config = nurture_config.load(DB_PATH) or {}
    field_pairs = (("durationMin", "nurtureDurationMin"),
                   ("durationMax", "nurtureDurationMax"),
                   ("likePercent", "nurtureLikePercent"),
                   ("collectPercent", "nurtureCollectPercent"),
                   ("sharePercent", "nurtureSharePercent"))
    for saved_key, requested_key in field_pairs:
        if saved_key in saved_config and requested_key in rec:
            try:
                matches = float(saved_config[saved_key]) == float(rec[requested_key])
            except (TypeError, ValueError):
                matches = False
            if not matches:
                return {"success": False, "taskId": task_id,
                        "error": "养号页面参数与已保存配置不一致，请重新进入养号页核对设置后再启动"}

    rec["id"] = task_id
    rec["name"] = str(rec.get("name") or rec.get("nickname") or "养号任务")
    rec["status"] = "ready"
    rec["taskKind"] = "nurture"
    snapshot = rec.get("configSnapshot") if isinstance(rec.get("configSnapshot"), dict) else {}
    merged = dict(snapshot)
    merged.update({k: v for k, v in rec.items()
                   if k not in {"id", "name", "status", "configSnapshot"}})
    merged.setdefault("accountId", account_id)
    rec["configSnapshot"] = merged

    saved = task_store.save_task(DB_PATH, "warmup", rec)
    if not saved.get("success"):
        return {"success": False, "taskId": task_id,
                "error": saved.get("error") or "养号任务保存失败"}

    res = registry.start(DB_PATH, "warmup", task_id, trigger="manual")
    if not res.get("success"):
        return {"success": False, "taskId": task_id,
                "error": res.get("error") or "养号启动失败",
                "noEngine": bool(res.get("noEngine")), "busy": bool(res.get("busy"))}
    return {"success": True, "taskId": task_id, "runId": res.get("runId"),
            "status": "running", "engine": res.get("engine"),
            "accountId": res.get("accountId") or account_id,
            # 真实生效参数（页面可据此核对「我设的值有没有被采纳」）
            "durationMin": merged.get("nurtureDurationMin"),
            "durationMax": merged.get("nurtureDurationMax"),
            "stayMin": merged.get("stayMin"), "stayMax": merged.get("stayMax"),
            "likePercent": merged.get("nurtureLikePercent")}


def _stop_task(payload: dict) -> dict:
    tid = str(payload.get("taskId") or payload.get("id") or "").strip()
    if tid.startswith("nurture_"):
        return _stop_nurture_task(tid)
    if tid:
        return task_runner.stop(tid)
    # 重启路径的报文只有 viewKey（形如 "douyin_<accountId>"），没有 taskId：
    # 按 accountId 兜底停止该账号名下的所有运行线程，否则「重启」前清场会空转。
    view = str(payload.get("viewKey") or "")
    acct = view.split("_", 1)[1] if "_" in view else str(payload.get("accountId") or "")
    from app.runtime import registry

    leadgen = task_runner.stop_account(acct)
    runtime = registry.stop_account(DB_PATH, acct)
    return {"success": True, "accountId": acct,
            "stopped": int(leadgen.get("stopped", 0)) + int(runtime.get("stopped", 0)),
            "taskIds": runtime.get("taskIds", []),
            "note": "已下发停止" if leadgen.get("stopped") or runtime.get("stopped") else "该账号没有运行中的任务"}


def _stop_nurture_task(task_id: str) -> dict:
    """停养号：走 registry 的真实线程，同时把任务记录状态归位。"""
    from app.runtime import registry

    res = registry.stop(DB_PATH, "warmup", task_id)
    try:
        record = task_store.get_task(DB_PATH, "warmup", task_id)
        if isinstance(record, dict):
            record["status"] = "stopped"
            record["endedAt"] = int(time.time() * 1000)
            record["endReason"] = "manual_stop"
            task_store.save_task(DB_PATH, "warmup", record)
    except Exception:  # noqa: BLE001 停不下来也要把停止回执给页面
        pass
    return {"success": True, "taskId": task_id, **res}


def _task_log_key(payload: dict) -> str:
    return str(payload.get("taskId") or payload.get("leadgenTaskId")
               or payload.get("id") or "").strip()


def _entity_task_logs(payload: dict) -> list[dict]:
    """任务页「运行日志」抽屉：返回**裸数组**（壳直接 .map，不能包 success 对象）。"""
    return task_logs.list_for(DB_PATH, _task_log_key(payload))


def _clear_entity_task_logs(payload: dict) -> dict:
    removed = task_logs.clear(DB_PATH, _task_log_key(payload))
    return {"success": True, "removed": removed}


def _start_comment_like_simulation(payload: dict) -> dict:
    if payload.get("executionMode") != "offline":
        return {"success": False, "code": "live_writes_disabled", "error": "模拟入口只允许离线模式"}
    task = str(payload.get("taskId") or "")
    record = leadgen_pools.load_records(DB_PATH).get(task)
    if not record or not comment_likes.is_like_record(record):
        return {"success": False, "error": "请先保存有效的评论点赞任务"}
    return comment_likes.service(DB_PATH).start(record, mode=payload.get("executionMode"),
                                              scenario=payload.get("scenario", "success"))


def _start_comment_like_live(payload: dict) -> dict:
    task = str(payload.get("taskId") or "")
    record = leadgen_pools.load_records(DB_PATH).get(task)
    if not record or not comment_likes.is_like_record(record):
        return {"success": False, "error": "请先保存有效的评论点赞任务"}
    return comment_likes.service(DB_PATH).start(record, mode="live")


# --- 评论获客运行时资源池（关键词 / 话题 / 指定视频） -----------------------
def _make_pool_init(kind: str) -> Callable[[dict], dict]:
    def handler(payload: dict) -> dict:
        res = leadgen_pools.init_pool(
            DB_PATH,
            str(payload.get("leadgenTaskId") or ""),
            kind,
            payload.get("keywords") if kind == "keyword" else payload.get("urls"),
            resume=bool(payload.get("resume")),
        )
        if res.get("error"):
            return {"success": False, "error": res["error"], **res}
        return {"success": True, **res}
    return handler


def _claim_leadgen_keyword(payload: dict) -> dict:
    return leadgen_pools.claim_keyword(
        DB_PATH,
        str(payload.get("leadgenTaskId") or ""),
        str(payload.get("accountId") or ""),
        reuse_active=bool(payload.get("reuseActive")),
    )


def _make_pool_release(kind: str) -> Callable[[dict], dict]:
    def handler(payload: dict) -> dict:
        return leadgen_pools.release_pool(
            DB_PATH,
            str(payload.get("leadgenTaskId") or ""),
            kind,
            preserve=payload.get("preserve") is not False,
        )
    return handler


def _save_leadgen_task(payload: dict) -> dict:
    """壳的 taskRecord 全量落库（system_settings），供重启后任务列表恢复。"""
    rec = payload[0] if isinstance(payload, list) and payload else payload
    res = leadgen_pools.save_task_record(DB_PATH, rec if isinstance(rec, dict) else {}, preserve_live_run=True)
    if res.get("error"):
        return {"success": False, "error": res["error"]}
    return res


def _update_leadgen_task(payload: dict) -> dict:
    task_id = str(payload.get("id") or payload.get("taskId") or "")
    patch = payload.get("patch") if isinstance(payload.get("patch"), dict) else payload
    res = leadgen_pools.update_task_record(DB_PATH, task_id, patch)
    if res.get("error"):
        return {"success": False, "error": res["error"]}
    return res


def _exec_write(sql: str, params: tuple = ()) -> int:
    """写库并返回受影响行数（表不存在等错误返回 0，由调用方判为「未找到」）。"""
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as con:
            cur = con.execute(sql, params)
            return int(cur.rowcount or 0)
    except sqlite3.Error:
        return 0


def _ids_payload(payload: Any) -> list[str]:
    """删除/批量类通道的入参归一：["id"] / [{"id":…}] / {"ids":[…]} 都接受。

    壳里数组型入参原样透传（_RAW_ARGS_CHANNELS），所以这里不能只认 dict。
    """
    raw: Any = payload
    if isinstance(payload, Mapping):
        for key in ("ids", "taskIds", "taskId", "id", "leadIds"):
            if key in payload:
                raw = payload[key]
                break
        else:
            raw = []
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    # 壳的调用形如 invoke(ch, idArray) -> args=[idArray]，展开一层；再兜住嵌套
    flat: list[Any] = []
    stack = list(raw)
    guard = 0
    while stack and guard < 2000:
        guard += 1
        item = stack.pop(0)
        if isinstance(item, (list, tuple)):
            stack = list(item) + stack
        else:
            flat.append(item)
    out: list[str] = []
    for item in flat:
        value = ((item.get("id") or item.get("taskId") or item.get("leadId"))
                 if isinstance(item, Mapping) else item)
        value = str(value or "").strip()
        if value:
            out.append(value)
    return out


def _save_entity_leadgen_task(payload: Any) -> dict:
    """评论获客 / 评论点赞建单：整条 taskRecord 落记录仓，打上 entityKind 归属标记。"""
    rec = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(rec, Mapping) or not str(rec.get("id") or "").strip():
        return {"success": False, "error": "任务记录缺少 id"}
    if comment_likes.is_like_record(rec):
        svc = comment_likes.service(DB_PATH)
        if str(rec["id"]) in svc.active():
            return {"success": False, "error": "请先停止运行中的评论点赞模拟再修改"}
        try:
            svc.validate(rec)
        except (ValueError, TypeError, AttributeError) as exc:
            return {"success": False, "error": str(exc)}
    record = task_store.clean(dict(rec))
    if comment_likes.is_like_record(record):
        # The renderer displays simulationStats in its table. Never let that
        # projection (or edited client counters) overwrite persisted real data.
        previous = leadgen_pools.load_records(DB_PATH).get(str(record["id"]), {})
        for field in ("stats", "simulationStats", "simulationRuns", "status", "executionMode", "runs",
                      "startedAt", "endedAt", "endReason", "lastMessage"):
            record.pop(field, None)
            if field in previous:
                record[field] = previous[field]
        record.setdefault("status", "pending")
        record.setdefault("stats", {})
    record["entityKind"] = "entity"
    res = leadgen_pools.save_task_record(DB_PATH, record)
    if res.get("error"):
        return {"success": False, "error": res["error"]}
    # 壳拿到回执会直接把它当 taskRecord 去启动（te(D)），因此要把记录带回去
    return {**record, "success": True, "saved": True}


def _delete_task_records(ids: list[str], *, entity: bool) -> tuple[int, list[str]]:
    """按归属删除记录仓里的任务记录，返回 (删除数, 因归属不符跳过的 id)。"""
    records = leadgen_pools.load_records(DB_PATH)
    removed = 0
    skipped: list[str] = []
    for tid in ids:
        rec = records.get(tid)
        if rec is None:
            continue
        if (str(rec.get("entityKind") or "") == "entity") != entity:
            skipped.append(tid)
            continue
        removed += leadgen_pools.delete_task_record(DB_PATH, tid)
    return removed, skipped


def _delete_leadgen_tasks(payload: Any) -> dict:
    """删除评论获客任务：只动 crawl_type='comment' 的行，与列表口径一致。

    （2026-11-19 修复：原来写成 <>'comment'，与列表的 ='comment' 正好相反，
    批量删除永远 0 行、前端只弹「删除失败」。）
    """
    ids = _ids_payload(payload)
    if not ids:
        return {"success": False, "error": "未指定要删除的任务"}
    running = sorted(set(ids) & {str(i) for i in task_runner.active_ids()})
    targets = [i for i in ids if i not in running]
    if not targets:
        return {"success": False, "error": "运行中的任务无法删除，请先停止"}
    removed, skipped = _delete_task_records(targets, entity=False)
    rows = _exec_write(
        "DELETE FROM crawl_tasks WHERE crawl_type='comment' AND id IN (%s)"
        % ",".join("?" * len(targets)), tuple(targets))
    total = removed + rows
    if not total:
        return {"success": False, "error": "未找到这些任务（可能已被删除）",
                "running": running, "skipped": skipped}
    return {"success": True, "deleted": total, "running": running, "skipped": skipped}


def _delete_entity_leadgen_tasks(payload: Any) -> dict:
    """删除评论获客 / 评论点赞任务及其配置记录（不删已写入线索库的用户）。"""
    ids = _ids_payload(payload)
    if not ids:
        return {"success": False, "error": "未指定要删除的任务"}
    running = sorted(set(ids) & (set(task_runner.active_ids()) | set(comment_likes.service(DB_PATH).active())))
    targets = [i for i in ids if i not in running]
    if not targets:
        return {"success": False, "error": "运行中的任务无法删除，请先停止"}
    removed, skipped = _delete_task_records(targets, entity=True)
    rows = _exec_write(
        "DELETE FROM crawl_tasks WHERE crawl_type<>'comment' AND id IN (%s)"
        % ",".join("?" * len(targets)), tuple(targets))
    total = removed + rows
    if not total:
        return {"success": False, "error": "未找到这些任务（可能已被删除）",
                "running": running, "skipped": skipped}
    return {"success": True, "deleted": total, "running": running, "skipped": skipped}


def _update_leads_intention(payload: Any) -> dict:
    """线索池批量标记高/普通意向：直接改 leads.intent_level。"""
    data = payload if isinstance(payload, Mapping) else {}
    ids = _ids_payload(data.get("leadIds") or data.get("ids") or [])
    if not ids:
        return {"success": False, "error": "未选择线索"}
    raw = data.get("isHigh", data.get("high"))
    high = raw is True or str(raw).strip().lower() in {"true", "1", "yes", "high"}
    level = "high" if high else "low"
    updated = _exec_write(
        "UPDATE leads SET intent_level=?, updated_at=? WHERE id IN (%s)"
        % ",".join("?" * len(ids)),
        (level, _now_text(), *ids))
    return {"success": True, "updated": updated, "missing": max(0, len(ids) - updated),
            "intentLevel": level}


def _now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# 线索详情可写字段 -> leads 列。只映射真实存在的列；
# 前端还有一批本地展示态字段（touchCounts / worksCount …）库内无列，
# 统一并入 insight_json 保留，不做静默丢弃。
_LEAD_UPDATE_COLUMNS = {
    "note": "note", "remark": "note",
    "customerNeed": "customer_need",
    "searchKeyword": "source_keyword",
    "location": "region", "region": "region",
    "content": "comment", "comment": "comment",
    "status": "status", "tags": "tags",
    "referralNote": "referral_note",
}


def _update_lead_in_history(payload: Any) -> dict:
    """线索详情改备注/意向并保存：定位到 leads 行后写回真实列。

    定位顺序：leadId -> rowId -> 昵称(+评论内容) 兜底，与前端三处调用点一致。
    """
    data = payload if isinstance(payload, Mapping) else {}
    updates = data.get("updates") if isinstance(data.get("updates"), Mapping) else {}
    if not updates:
        return {"success": False, "error": "没有要保存的改动"}
    lead_id = str(data.get("leadId") or data.get("rowId") or "").strip()
    identity = data.get("leadIdentity") if isinstance(data.get("leadIdentity"), Mapping) else {}
    row = _one("SELECT id, insight_json FROM leads WHERE id=?", (lead_id,)) if lead_id else None
    if row is None and lead_id:
        tail = lead_id.split(":")[-1]
        if tail and tail != lead_id:
            row = _one("SELECT id, insight_json FROM leads WHERE id=?", (tail,))
    if row is None and identity:
        nick = str(identity.get("nickname") or "").strip()
        content = str(identity.get("content") or "").strip()
        if nick:
            sql = "SELECT id, insight_json FROM leads WHERE nickname=?"
            params: list[Any] = [nick]
            if content:
                sql += " AND comment=?"
                params.append(content)
            row = _one(sql + " ORDER BY created_at DESC LIMIT 1", tuple(params))
    if row is None:
        return False
    sets: list[str] = []
    values: list[Any] = []
    extras: dict[str, Any] = {}
    for key, value in updates.items():
        if key in ("isHighIntention", "highIntention"):
            high = value is True or str(value).strip().lower() in {"true", "1", "yes", "high"}
            sets.append("intent_level=?")
            values.append("high" if high else "low")
        elif key in _LEAD_UPDATE_COLUMNS:
            if value is None:
                continue
            sets.append(f"{_LEAD_UPDATE_COLUMNS[key]}=?")
            values.append(value if isinstance(value, (int, float, str)) else json.dumps(value, ensure_ascii=False))
        else:
            extras[key] = value
    if extras:
        try:
            merged = json.loads(row.get("insight_json") or "{}")
        except (TypeError, ValueError):
            merged = {}
        if not isinstance(merged, dict):
            merged = {}
        merged.update(extras)
        sets.append("insight_json=?")
        values.append(json.dumps(task_store.clean(merged), ensure_ascii=False))
    sets.append("updated_at=?")
    values.append(_now_text())
    updated = _exec_write(
        "UPDATE leads SET " + ", ".join(sets) + " WHERE id=?", (*values, row["id"]))
    if not updated:
        return False
    return {"success": True, "leadId": row["id"], "updated": updated,
            "columns": [s.split("=")[0] for s in sets if s.split("=")[0] != "updated_at"],
            "keptInInsight": sorted(extras)}


def _save_task(kind: str) -> Callable[[Any], dict]:
    """新建/保存监控、养号、发布任务：整条 taskRecord 落 tasks.payload。"""
    def build(payload: Any) -> dict:
        rec = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(rec, Mapping) and isinstance(rec.get("taskRecord"), Mapping):
            rec = rec["taskRecord"]
        if not isinstance(rec, Mapping):
            return {"success": False, "error": "任务数据格式不正确"}
        return task_store.save_task(DB_PATH, kind, dict(rec))
    return build


def _save_account_pool(args: Any) -> dict:
    """壳里账号池保存：真实同步到 accounts 表（含删除，让删号刷新后不再复活）。"""
    items = args[0] if isinstance(args, list) and args else None
    if not isinstance(items, list):
        return {"success": False, "reason": "账号池数据格式不正确"}
    return account_pool.sync_pool(DB_PATH, items)


def _check_account_pool_status(payload: Any) -> dict:
    """真机检测账号登录态（账号池「检测账号」按钮 / 登录后状态同步）。

    登录态只存在于壳的浏览器分区里（cookie 含 httpOnly），后端自己读不到；
    这里下发 check-login 指令让壳逐个账号检测，并把结果回写 accounts.status，
    否则「在界面里登录成功了」和「账号池一直显示未登录」永远对不上。
    """
    _record, args = _rt_args(payload)
    raw = (args.get("accountIds") or args.get("accountId") or [])
    if isinstance(raw, str):
        raw = [raw]
    ids = [str(i).strip() for i in (raw or []) if str(i).strip()][:20]
    if not ids:
        return {"success": False, "error": "未指定要检测的账号"}
    from app.runtime import browser

    results: list[dict] = []
    for aid in ids:
        if not _rows("SELECT id FROM accounts WHERE id=?", (aid,)):
            results.append({"accountId": aid, "status": "error",
                            "message": "账号不在账号池中（可能已被删除）"})
            continue
        try:
            res = browser.execute({"type": "check-login", "accountId": aid},
                                  db_path=DB_PATH, timeout=90, account_id=aid)
        except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断整批
            res = {"ok": False, "error": str(exc)}
        if res.get("ok"):
            status = "online" if res.get("loggedIn") else "offline"
            # 检测结果回写账号库，界面下次刷新就是新状态
            account_pool.set_login_status(DB_PATH, aid, status)
            message = str(res.get("note") or "")
        else:
            status = "error"
            message = str(res.get("error") or "检测失败")
        results.append({
            "accountId": aid, "status": status, "nickname": str(res.get("nickname") or ""),
            "message": message, "checkedAt": int(time.time() * 1000), "source": "shell",
        })
    counts = {"online": 0, "offline": 0, "error": 0}
    for r in results:
        counts[r["status"]] += 1
    return {"success": True, "results": results, "summary": counts}


def _quota(_: dict) -> dict:
    used = _scalar("SELECT COUNT(*) FROM dm_send_results WHERE date(created_at)=date('now')") \
        if _table_exists("dm_send_results") else 0
    total = _scalar("SELECT SUM(daily_limit) FROM accounts") if _table_exists("accounts") else 0
    return {"success": True, "data": {"used": used, "total": total, "remain": max(0, total - used), "aiQuota": 0}}


# 演示/验收数据源：seed 与历史演示导入写进来的假线索必须从一切聚合里剔掉，
# 否则「已处理视频」里会出现用户从没采集过的「AI获客系统演示视频」。
DEMO_SQL = ("(COALESCE(nickname,'') LIKE '日报客户%' OR COALESCE(video,'') LIKE 'AI获客系统演示%')")


_VIDEO_URL_EXPR = (
    "COALESCE("
    "CASE WHEN TRIM(COALESCE(source_url,'')) LIKE 'http%' THEN TRIM(source_url) END,"
    "CASE WHEN TRIM(COALESCE(video,'')) LIKE 'http%' THEN TRIM(video) END,"
    "CASE WHEN TRIM(COALESCE(video_id,''))<>'' "
    "THEN 'https://www.douyin.com/video/'||video_id END)")


def _processed_videos(_: dict) -> dict:
    """已处理视频：按**视频 URL** 聚合。

    两个必须兼顾的现实：
    - 早期导入把视频 URL 放在 `video` 列，正常采集放在 `source_url`、`video` 存文案；
      只认一列就会出现「标题是链接」或「弹窗空着」。
    - 「移除」写的是隐藏名单，值可能是 URL、video_id 或历史文案，所以过滤放在
      Python 里做（30 行以内，成本可忽略），三种形态都能命中。
    """
    hidden = _hidden_values("history")
    rows = _rows(
        f"SELECT {_VIDEO_URL_EXPR} AS url, MAX(video) AS video, "
        "MIN(created_at) AS first_seen, COUNT(*) AS n FROM leads "
        f"WHERE {_VIDEO_URL_EXPR} IS NOT NULL AND NOT {DEMO_SQL} "
        "GROUP BY url ORDER BY first_seen DESC LIMIT 30")
    items = [{
        "id": hashlib.md5(str(r["url"]).encode("utf-8")).hexdigest()[:8],
        "url": r["url"],
        "title": str(r["video"] or "历史视频").split("\n")[0][:60],
        "author": "", "createTime": "",
        "collectedAt": _fmt(r["first_seen"]), "account": "",
        "state": "已入库", "actions": {}, "leadCount": r["n"],
    } for r in rows]
    if hidden:
        items = [i for i in items
                 if str(i["url"]) not in hidden and str(i["title"]) not in hidden]
    return {"success": True, "total": len(items), "items": items}


def _collected_video_keys(_: dict) -> dict:
    """采集库现有视频的 key（视频 URL + video_id 两种写法都给）。

    导入弹窗拿它做去重：历史视频里已经在库的自然不显示，删掉的会重新出现。
    """
    if not _table_exists("leads"):
        return {"success": True, "keys": []}
    hidden = _hidden_values("video")
    where = "TRIM(COALESCE(video,''))<>''"
    params: list[Any] = []
    if hidden:
        ids_part = sorted(v for v in hidden if not v.startswith("http"))
        urls_part = sorted(v for v in hidden if v.startswith("http"))
        for column, values in (("COALESCE(video_id,'')", ids_part), ("video", urls_part)):
            if values:
                where += f" AND {column} NOT IN (" + ",".join("?" * len(values)) + ")"
                params.extend(values)
    keys: set[str] = set()
    for r in _rows(f"SELECT {_VIDEO_URL_EXPR} AS url, video_id FROM leads WHERE {where}",
                   tuple(params)):
        if r["url"]:
            keys.add(str(r["url"]).strip())
        if r["video_id"]:
            keys.add(str(r["video_id"]).strip())
    return {"success": True, "keys": sorted(keys), "total": len(keys)}


# 命中口径：与 _monitor_seen_stats / leads 库里的评级一致（a/b/high/medium/hot 算命中，
# 其余算未命中）。研判明细的「清空未命中」按这个口径删。
HIT_INTENTS = ("a", "b", "high", "medium", "hot")


def _monitor_seen_stats(_: dict) -> dict:
    # 已扫评论的去重记录在 monitor_seen（runtime/store.py 维护）；以前这里数的是
    # behavior_events，跟监控页「允许再研判」要清的表完全对不上，页面显示的
    # 「已扫 N 条」一直是别的模块的计数。
    seen = _scalar("SELECT COUNT(*) FROM monitor_seen") if _table_exists("monitor_seen") else 0
    matched = _scalar(
        "SELECT COUNT(*) FROM leads WHERE lower(intent_level) IN ('a','b','high','medium','hot')"
    )
    replied = _scalar(
        "SELECT COUNT(*) FROM comment_tasks WHERE status IN ('replied','user_replied')"
    ) if _table_exists("comment_tasks") else 0
    return {"success": True, "stats": {"seen": seen, "matched": matched, "replied": replied}}


# ---- 监控任务明细通道（2026-11-16 补齐） ----
# 页面早就有「删除任务 / 删除研判记录 / 允许再研判 / 视频主评 / 直播状态 / 旧成员」
# 的入口，但对应通道没有注册，dispatch 落到 _disabled 返回「功能未开通」，按钮全是死的。
# 这里按页面调用契约补齐；直播与群成员没有采集器，返回明确的空态而不是假数据。

def _delete_monitor_tasks(payload: Any) -> dict:
    """删除监控任务：连同运行证据、已扫去重与命中记录一并清除（页面承诺「命中记录与日志也将清除」）。"""
    ids = _ids_payload(payload)
    if not ids:
        return {"success": False, "error": "未指定要删除的任务"}
    running = sorted(set(ids) & {str(i) for i in task_runner.active_ids()})
    targets = [i for i in ids if i not in running]
    if not targets:
        return {"success": False, "error": "运行中的任务无法删除，请先停止"}
    ph = ",".join("?" * len(targets))
    args = tuple(targets)
    # runtime_cycles 挂在 run 上，先于 runtime_runs 清理（runtime_runs 主键是 run_id）
    if _table_exists("runtime_cycles"):
        _exec_write("DELETE FROM runtime_cycles WHERE run_id IN "
                    f"(SELECT run_id FROM runtime_runs WHERE task_id IN ({ph}))", args)
    if _table_exists("runtime_runs"):
        _exec_write(f"DELETE FROM runtime_runs WHERE task_id IN ({ph})", args)
    if _table_exists("task_runtime_logs"):
        _exec_write(f"DELETE FROM task_runtime_logs WHERE task_id IN ({ph})", args)
    if _table_exists("monitor_seen"):
        _exec_write(f"DELETE FROM monitor_seen WHERE task_id IN ({ph})", args)
    if _table_exists("leads"):
        _exec_write("DELETE FROM leads WHERE source='monitor_comment' AND "
                    f"source_task_id IN ({ph})", args)
    # 删除口径必须与列表一致：_tasks("monitor") 的兜底是 task_type LIKE '%live%'，
    # 历史导入的直播监控行是 media_crawler_live——只删 monitor_live 会删 0 行（假失败，
    # 前端又不检查 success，弹「已删除」实际没删，2026-11-20 用户实测）。
    deleted = _exec_write("DELETE FROM tasks WHERE task_type LIKE '%live%' AND "
                          f"id IN ({ph})", args) if _table_exists("tasks") else 0
    if not deleted:
        return {"success": False, "error": "未找到这些任务（可能已被删除）", "running": running}
    return {"success": True, "deleted": deleted, "running": running}


def _delete_monitor_match_records(payload: Any) -> dict:
    """删除监控研判明细：按 matchIds（线索主键）删单条，或清空 / 只清未命中。"""
    data = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], Mapping) else payload
    if not isinstance(data, Mapping):
        return {"success": False, "error": "参数格式不正确"}
    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}
    if not _table_exists("leads"):
        return {"success": True, "deleted": 0, "matches": []}
    base = "source='monitor_comment' AND source_task_id=?"
    match_ids = [str(x).strip() for x in (data.get("matchIds") or []) if str(x).strip()]
    if match_ids:
        ph = ",".join("?" * len(match_ids))
        sql = f"DELETE FROM leads WHERE {base} AND id IN ({ph})"
        params = (task_id, *match_ids)
    elif data.get("clearAll"):
        sql = f"DELETE FROM leads WHERE {base}"
        params = (task_id,)
    elif data.get("clearMisses"):
        holders = ",".join("?" * len(HIT_INTENTS))
        sql = (f"DELETE FROM leads WHERE {base} AND (intent_level IS NULL OR intent_level='' "
               f"OR lower(intent_level) NOT IN ({holders}))")
        params = (task_id, *HIT_INTENTS)
    else:
        return {"success": False, "error": "未指定要删除的研判记录"}
    deleted = _exec_write(sql, params)
    return {"success": True, "deleted": deleted, "matches": []}


def _clear_monitor_comment_seen(payload: Any) -> dict:
    """清掉「这条评论已经看过」的去重记录（不动研判明细），下一轮按当前时间窗重新研判。"""
    data = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], Mapping) else payload
    if not isinstance(data, Mapping):
        return {"success": False, "error": "参数格式不正确"}
    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}
    if not _table_exists("monitor_seen"):
        return {"success": True, "cleared": 0}
    cleared = _exec_write("DELETE FROM monitor_seen WHERE task_id=?", (task_id,))
    return {"success": True, "cleared": cleared}


def _monitor_task_video_comments(payload: Any) -> dict:
    """监控任务的视频主评明细：读 live_action_results 里本任务发过的 post-comment 回执。"""
    data = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], Mapping) else payload
    if not isinstance(data, Mapping):
        return {"success": False, "error": "参数格式不正确"}
    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}
    if not _table_exists("live_action_results"):
        return {"success": True, "items": [], "total": 0, "offset": 0, "limit": 0}
    where = "WHERE task_id=? AND action='post-comment'"
    params: list = [task_id]
    keyword = str(data.get("keyword") or "").strip()
    if keyword:
        where += " AND (title LIKE ? OR text LIKE ? OR url LIKE ?)"
        params += [f"%{keyword}%"] * 3
    status = str(data.get("status") or "").strip()
    if status and status != "all":
        where += " AND status=?"
        params.append(status)
    limit = max(1, min(200, int(data.get("limit") or 20)))
    offset = max(0, int(data.get("offset") or 0))
    total = _scalar(f"SELECT COUNT(*) FROM live_action_results {where}", tuple(params))
    rows = _rows(f"SELECT id, created_ms, url, title, text, status, code FROM live_action_results "
                 f"{where} ORDER BY created_ms DESC LIMIT {limit} OFFSET {offset}", tuple(params))
    items = [{
        "id": str(r["id"]), "ts": int(r["created_ms"] or 0),
        "videoUrl": str(r["url"] or ""), "videoTitle": str(r["title"] or ""),
        "text": str(r["text"] or ""), "status": str(r["status"] or ""),
        "code": str(r["code"] or ""), "time": _fmt(r["created_ms"]),
    } for r in rows]
    return {"success": True, "items": items, "total": total, "offset": offset, "limit": limit}


def _monitor_live_state(args: Any) -> dict:
    """直播监控明细首页：真实目标状态 + 待处理计数。

    数据源（runtime 每轮写）：
      monitor_live_targets —— 每个直播间/主播的目标状态（监听中/已下播/开播排队）
      monitor_live_events  —— 事件明细；pending=已读未研判，matched 且无动作回执=待互动
    没有任何记录时仍给空态（宁空勿假），note 说明原因。
    """
    data = args[0] if isinstance(args, list) and args and isinstance(args[0], Mapping) else args
    if not isinstance(data, Mapping):
        data = {}
    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        from app.runtime.store import _ensure_live_tables
        from app.runtime import monitor_engine
        _ensure_live_tables(con)
        targets: list[dict] = []
        has_row = con.execute("SELECT 1 FROM monitor_live_targets WHERE task_id=? LIMIT 1",
                              (task_id,)).fetchone()
        if has_row:
            for r in con.execute(
                    "SELECT * FROM monitor_live_targets WHERE task_id=? "
                    "ORDER BY updated_ms DESC, target_key", (task_id,)):
                targets.append({
                    "key": r["target_key"],
                    "name": r["name"] or monitor_engine.room_label(r["target_key"]),
                    "url": r["target_key"],
                    "type": r["type"] or "live",
                    "state": r["state"] or "queued",
                    "reason": r["reason"] or "",
                    "accountId": r["account_id"] or "",
                    "accountName": r["account_name"] or "",
                    "lastCheckAt": int(r["last_check_ms"] or 0),
                    "nextCheckAt": int(r["next_check_ms"] or 0),
                    "events": int(r["events"] or 0),
                    "matched": int(r["matched"] or 0),
                    "added": int(r["added"] or 0),
                    "updated": int(r["updated"] or 0),
                })
        pending = {
            # 待匹配：已读进明细但还没研判完的弹幕（轮内实时清零）
            "judge": int(con.execute(
                "SELECT COUNT(*) FROM monitor_live_events WHERE task_id=? AND outcome='pending'",
                (task_id,)).fetchone()[0]),
            # 待互动（含执行中）：已命中但动作回执还没写回的事件
            "action": int(con.execute(
                "SELECT COUNT(*) FROM monitor_live_events WHERE task_id=? "
                "AND outcome='matched' AND actions=''",
                (task_id,)).fetchone()[0]),
        }
    except sqlite3.Error:
        targets, pending = [], {"judge": 0, "action": 0}
        has_row = None
    finally:
        con.close()
    note = "" if has_row else "该任务还没有直播监控记录：启动任务并配置直播/主播目标后，这里会显示真实监听状态"
    return {"success": True, "targets": targets, "pending": pending, "note": note}


def _monitor_group_history(payload: Any) -> dict:
    """监控任务的群成员明细：读 leads 里本任务采集到的群成员线索。

    采集器在 runners._group_pass（监控群成员链路）：壳读完整群名单 →
    排除群主/管理员 → 按「群名:昵称」去重入库（source=monitor_group）。
    这里按任务/群/关键词分页读回；没有任何记录时仍返回空态（宁空勿假）。
    """
    data = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], Mapping) else payload
    if not isinstance(data, Mapping):
        return {"success": False, "error": "参数格式不正确"}
    task_id = str(data.get("taskId") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}
    if not _table_exists("leads"):
        return {"success": True, "items": [], "total": 0, "offset": 0, "limit": 0,
                "groups": [], "note": "还没有群成员记录：启动任务并配置监控群成员后，这里会显示采集到的名单"}
    where = "WHERE source='monitor_group' AND source_task_id=?"
    params: list = [task_id]
    group_name = str(data.get("groupName") or "").strip()
    if group_name:
        # video_id = f"{群名}:{昵称}"（见 _group_pass），按群前缀过滤
        where += " AND video_id LIKE ? ESCAPE '\\'"
        params.append(group_name.replace("\\", "\\\\").replace("%", "\\%")
                      .replace("_", "\\_") + ":%")
    keyword = str(data.get("keyword") or "").strip()
    if keyword:
        where += " AND (nickname LIKE ? OR comment LIKE ?)"
        params += [f"%{keyword}%"] * 2
    limit = max(1, min(200, int(data.get("limit") or 20)))
    offset = max(0, int(data.get("offset") or 0))
    total = _scalar(f"SELECT COUNT(*) FROM leads {where}", tuple(params))
    rows = _rows(
        f"SELECT id, nickname, comment, source_keyword, video_id, comment_id, "
        f"account, status, created_at FROM leads {where} "
        f"ORDER BY created_at DESC, rowid DESC LIMIT {limit} OFFSET {offset}", tuple(params))
    items = []
    for r in rows:
        vid = str(r["video_id"] or "")
        group = vid.split(":", 1)[0] if ":" in vid else ""
        items.append({
            "id": str(r["id"]),
            "nickname": str(r["nickname"] or ""),
            "groupName": group,
            # 群主/管理员在采集端按开关过滤后才入库，落库的都是普通成员
            "role": "member",
            "keyword": str(r["source_keyword"] or ""),
            "comment": str(r["comment"] or ""),
            "account": str(r["account"] or ""),
            "status": str(r["status"] or ""),
            "ts": _epoch_ms(r["created_at"]),
            "time": _fmt(r["created_at"]),
        })
    group_rows = _rows(
        f"SELECT DISTINCT video_id FROM leads {where}", tuple(params))
    groups = sorted({str(r["video_id"] or "").split(":", 1)[0]
                     for r in group_rows if str(r["video_id"] or "")})
    note = "" if items else "还没有群成员记录：启动任务并配置监控群成员后，这里会显示采集到的名单"
    return {"success": True, "items": items, "total": total, "offset": offset,
            "limit": limit, "groups": groups, "note": note}


# --------------------------------------------------------------------------
# 通道表
# --------------------------------------------------------------------------
# ---- 本地演示授权通道的薄包装（保持 builder 统一签名） ----
def _license_activate(source: Any) -> dict:
    return license_local.activate(source)


def _license_status(_: Any) -> dict:
    return license_local.status()


def _license_revoke(source: Any) -> dict:
    return license_local.revoke(source)


def _license_reset(_: Any) -> dict:
    return license_local.reset()


def _license_issue_code(args: Any) -> dict:
    payload = args if isinstance(args, Mapping) else {}
    hours = payload.get("ttlHours") or payload.get("ttl_hours")
    return license_local.issue_demo_code(
        ttl_hours=int(hours) if hours else None,
        note=str(payload.get("note") or ""),
    )


# 这两个通道可能收到「裸字符串码」或字符串数组，需要原始 args 而非只取 args[0]
_RAW_ARGS_CHANNELS = frozenset({
    "activate", "license-revoke",
    # 这些通道的入参是数组 / 字符串，不是对象：必须原样透传
    "save-ai-agents", "delete-ai-agent", "save-account-pool",
    # 删除类：壳传的是 id 数组（["t1","t2"]），归一交给各 handler 自己处理
    "delete-leadgen-tasks", "delete-entity-leadgen-tasks",
    "delete-monitor-tasks", "delete-self-warmup-tasks",
    # 线索库删除 / UID 导入 / 采集库删除：入参是 id 数组或条目对象数组
    "delete-leads-from-history", "append-imported-leads",
    "remove-processed-videos",
    # 作品发布：单 id / id 数组删除
    "delete-work-publish-task", "delete-work-publish-tasks",
    "delete-work-publish-template",
    # 批量跟进：删运行记录传 id 数组
    "delete-batch-follow-runs",
    # 定时排期：整表 map 是唯一入参，必须原样收到（_arg 会把非 Mapping 归一成 {}，
    # 导致一次格式错误的保存把已有排期整表清空）
    "save-leadgen-schedules", "save-entity-leadgen-schedules",
    "save-monitor-schedules", "save-self-warmup-schedules",
    # 系统设置：set-* 的入参是标量（缩放百分比 / 布尔开关），_arg 会归一成 {} 丢值
    "set-ui-zoom-factor", "set-use-system-proxy", "set-all-views-muted",
    "set-background-preview-auto-close", "set-manual-link-open-mode",
    "set-hardware-acceleration", "set-auto-cache-cleanup",
})


def _disabled(channel: str) -> dict:
    return {"success": False, "msg": "功能未开通", "channel": channel, "mode": "live"}


def _storage_ok(_: dict) -> dict:
    return {"success": True, "state": "ok"}


def _empty_logs(_: dict) -> dict:
    return {"success": True, "logs": []}


def _empty_items(_: dict) -> dict:
    return {"success": True, "items": [], "total": 0}


def _no_source(_: dict) -> dict:
    """确认本地无采集源的通道：保持空态并写明口径，不伪装成平台真实值。"""
    return {"success": True, "items": [], "total": 0, "note": "本地无该指标采集源"}


def _empty_schedules(_: dict) -> dict:
    return {"success": True, "schedules": []}


# --------------------------------------------------------------------------
# 定时排期（4 类）与账号数据统计时刻
#
# 存读契约是**裸 map**（键=任务 id），前端直接 store.replaceScheduled*(map)。
# 以前读侧返回 {success, schedules:[]} 信封，前端 ro() 逐键校验时把 "success"
# 当成一条 runAt=1 的排期，任务列表凭空多出幽灵排期——所以这里返回裸 map。
# --------------------------------------------------------------------------
def _schedules_read(kind: str) -> Callable[[Any], dict]:
    def build(_: Any = None) -> dict:
        return schedules.get_map(DB_PATH, kind)
    return build


def _schedules_write(kind: str) -> Callable[[Any], dict]:
    def build(payload: Any) -> dict:
        return schedules.save_map(DB_PATH, kind, payload)
    return build


def _stop_monitor(_: Any = None) -> dict:
    """停止全部监控任务：把库里标着运行中的监控任务归位为已停止。

    壳在切换页面 / 退出清理时无条件调用，不校验返回值。本地没有监控执行引擎，
    状态只能靠记录本身表达——所以这里做的是**真实的本地状态归位**（并落库），
    不是返回 success 了事：重开页面读到的是 stopped，不会继续显示运行中。
    """
    stopped: list[str] = []
    for rec in task_store.list_tasks(DB_PATH, "monitor"):
        if str(rec.get("status") or "") not in {"running", "pending", "starting"}:
            continue
        tid = str(rec.get("id") or "")
        if not tid:
            continue
        rec["status"] = "stopped"
        rec["endedAt"] = rec.get("endedAt") or int(time.time() * 1000)
        rec["endReason"] = rec.get("endReason") or "stopped"
        task_store.save_task(DB_PATH, "monitor", rec)
        stopped.append(tid)
    return {"success": True, "stopped": len(stopped), "taskIds": stopped}


def _save_account_data_schedule(payload: Any) -> dict:
    rec = payload[0] if isinstance(payload, list) and payload else payload
    value = rec.get("time") if isinstance(rec, Mapping) else rec
    return schedules.set_account_data_time(DB_PATH, value)


def _empty_ids(_: dict) -> list:
    return []


def _null(_: dict) -> None:
    return None


def _settings_get(category: str, key: str) -> str | None:
    """读 system_settings 单值（表不存在时返回 None，不抛）。"""
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as c:
            row = c.execute(
                "SELECT value FROM system_settings WHERE category=? AND key=?",
                (category, key)).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def _settings_set(category: str, key: str, value: str) -> bool:
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as c:
            c.execute(
                "INSERT INTO system_settings(category,key,value) VALUES(?,?,?) "
                "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value, "
                "updated_at=CURRENT_TIMESTAMP", (category, key, value))
        return True
    except sqlite3.Error:
        return False


# 消息中心「后台提醒」配置：字段取自 MessageCenter 表单
# （enabled / webhookType / webhookUrlFeishu / webhookUrlDingtalk / webhookSecretDingtalk）。
# 旧实现按 chat_ 前缀白名单过滤，而前端这 5 个字段都不带前缀 → 全部被丢弃，
# 接口却回 success:true，属「假成功」。此处改为显式字段表，并把值归一化。
_CHAT_CONFIG_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "webhookType": "none",
    "webhookUrlFeishu": "",
    "webhookUrlDingtalk": "",
    "webhookSecretDingtalk": "",
}
# 旧版提醒开关（历史键），保留可读写，避免老配置读不出来
_CHAT_LEGACY_KEYS: tuple[str, ...] = (
    "chat_sound", "chat_desktop_notify", "chat_badge", "chat_notify_only_unread",
)
_PUSH_NOTE = ("配置已持久化。本机尚未内置推送发送器：开启后侧栏未读会更新，"
              "但不会真的向飞书/钉钉发送消息。")


def _chat_value(key: str) -> Any:
    raw = _settings_get("notification", f"chat_{key}")
    default = _CHAT_CONFIG_DEFAULTS[key]
    if raw is None:
        return default
    if isinstance(default, bool):
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(str(raw).strip())
        except ValueError:
            return default
    return str(raw)


def _chat_config() -> dict[str, Any]:
    return {k: _chat_value(k) for k in _CHAT_CONFIG_DEFAULTS}


def _chat_config_payload() -> dict[str, Any]:
    """读/写共用回执。

    前端两处消费形状不同，必须同时满足：
      - 打开面板：ke(整个返回体) 把返回体摊进表单 → 需要顶层扁平键
      - 保存后回填：ke(返回体.config) → 需要 config 子对象
    """
    cfg = _chat_config()
    return {"success": True, "config": cfg, **cfg,
            "pushSupported": False, "note": _PUSH_NOTE}


def _chat_notification_config(_: dict) -> dict:
    return _chat_config_payload()


def _encode_chat_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _save_chat_notification_config(args: dict) -> dict:
    payload = args if isinstance(args, dict) else {}
    saved: list[str] = []
    skipped: list[str] = []
    for k, v in payload.items():
        if k in _CHAT_CONFIG_DEFAULTS:
            if _settings_set("notification", f"chat_{k}", _encode_chat_value(v)):
                saved.append(k)
            continue
        if k in _CHAT_LEGACY_KEYS:
            if _settings_set("notification", str(k), _encode_chat_value(v)):
                saved.append(k)
            continue
        skipped.append(str(k))
    result = _chat_config_payload()
    return {**result, "saved": saved, "skipped": skipped}


# --------------------------------------------------------------------------
# 段 2：只读通道补登记（全部复用现有表，不新建结构）
# --------------------------------------------------------------------------
def _refresh_announcements(_: dict) -> dict:
    """刷新公告：重新读一次库（前端 fire-and-forget，取返回值即可）。"""
    return _announcements({})


def _lead_history(_: dict) -> list[dict]:
    """线索历史处理记录：按来源任务分组（前端 get-history 直接当数组用）。

    返回 [{taskId, taskName, total, items:[最小展示集线索]}]。
    只取有来源任务的线索，未关联任务的散线索不在此列表出现。
    """
    if not _table_exists("leads"):
        return []
    has_tasks = _table_exists("crawl_tasks")
    sql = ("SELECT l.*, t.name AS task_name FROM leads l "
           + ("LEFT JOIN crawl_tasks t ON t.id = l.source_task_id " if has_tasks else "")
           + "WHERE l.source_task_id IS NOT NULL AND TRIM(l.source_task_id) <> '' "
             "ORDER BY l.created_at DESC LIMIT 400")
    try:
        rows = _rows(sql)
    except sqlite3.Error:
        return []
    groups: dict[str, dict] = {}
    for row in rows:
        tid = str(row.get("source_task_id") or "").strip()
        group = groups.setdefault(tid, {
            "taskId": tid,
            "taskName": row.get("task_name") or tid,
            "items": [], "total": 0,
        })
        item = _lead_public(row)
        item["taskId"] = tid
        item["taskName"] = group["taskName"]
        group["items"].append(item)
    for group in groups.values():
        group["total"] = len(group["items"])
    return list(groups.values())


def _als_license_seats(code: str) -> int:
    """商业码（ALS-）的席位按发行方后台登记的 seats 算；后台不可用时按 1 兜底。"""
    try:
        from app.admin.store import AdminStore

        lic = AdminStore().get_license(code)
        if lic:
            return max(1, int(lic.get("seats") or 1))
    except Exception:  # noqa: BLE001 - 后台库缺失/换机部署时按 1 席兜底
        pass
    return 1


def _demo_license_seats(state: dict) -> int:
    """本地演示码：按「1 个已激活授权码 = 1 个账号席位」折算。

    只数未过期、未吊销、已激活（used）的码；重复激活同一枚码不叠加席位。
    """
    count = 0
    for record in state.get("codes") or []:
        if not isinstance(record, dict):
            continue
        if record.get("status") != "used":
            continue
        try:
            if datetime.fromisoformat(str(record["expires_at"])) < datetime.now(timezone.utc):
                continue
        except (KeyError, ValueError):
            continue
        count += 1
    return max(1, count)


def _license_entitlement(_: dict) -> dict:
    """本机授权席位（规则：1 个已激活授权码 = 1 个账号席位）。

    商业码（ALS-）读发行方后台登记的 seats；本地演示码按已激活码数折算；
    也可用环境变量 BRIDGE_LICENSE_SEATS 临时覆盖（联调多账号时用，优先级最高）。
    旧实现写死 seats=1，商业后台明明登记了 N 席也只能登 1 个账号，
    「多账号登录」在界面上永远被拦。
    """
    try:
        state = license_local.load_state()
    except Exception:  # noqa: BLE001 - 授权文件损坏不应阻断账号页
        state = {}
    code = str(state.get("active_code") or "")
    if code.startswith("ALS-"):
        seats = _als_license_seats(code)
    else:
        seats = _demo_license_seats(state) if code else 0
    try:
        seats = max(seats, int(os.getenv("BRIDGE_LICENSE_SEATS", "0") or 0))
    except ValueError:
        pass
    used = _scalar("SELECT COUNT(*) FROM accounts") if _table_exists("accounts") else 0
    overlay = license_local.auth_overlay()
    return {
        "success": True,
        "maxConcurrentAccounts": max(seats, 1),
        "seats": seats,
        "usedAccounts": used,
        "remaining": max(seats - used, 0),
        "overQuota": used > seats,
        "licenseTier": overlay.get("licenseTier") or "local-demo",
        "licenseTierLabel": overlay.get("licenseTierLabel") or "本地演示授权",
        "activatedCodes": len(state.get("codes") or []),
        "rule": "1 个已激活授权码 = 1 个账号席位",
        "note": "席位按授权登记折算；本地演示授权不代表商业授权额度",
    }


def _hashtag_videos(args: dict) -> dict:
    """任务抓过的话题视频（按 video 去重，字段对齐 LeadgenHashtagVideos 列）。"""
    payload = args if isinstance(args, Mapping) else {}
    task_id = str(payload.get("taskId") or "").strip()
    if not task_id or not _table_exists("leads"):
        return {"success": True, "items": [], "total": 0, "counts": {
            "all": 0, "interacted": 0, "untouched": 0}, "actionOptions": []}
    limit = max(1, min(int(payload.get("limit") or 20), 200))
    offset = max(0, int(payload.get("offset") or 0))
    try:
        rows = _rows(
            "SELECT video, video_id, source_keyword, account, created_at, "
            "       MAX(comment_time) AS last_comment_at, COUNT(*) AS comment_count, "
            "       SUM(CASE WHEN status IN ('contacted','wechat_added','converted') "
            "                THEN 1 ELSE 0 END) AS interacted "
            "FROM leads WHERE source_task_id = ? AND TRIM(COALESCE(video,'')) <> '' "
            "GROUP BY video ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (task_id, limit, offset))
        total = _scalar(
            "SELECT COUNT(DISTINCT video) FROM leads "
            "WHERE source_task_id = ? AND TRIM(COALESCE(video,'')) <> ''", (task_id,))
        all_count = _scalar(
            "SELECT COUNT(*) FROM leads WHERE source_task_id = ?", (task_id,))
        interacted = _scalar(
            "SELECT COUNT(*) FROM leads WHERE source_task_id = ? "
            "AND status IN ('contacted','wechat_added','converted')", (task_id,))
    except sqlite3.Error:
        return {"success": True, "items": [], "total": 0,
                "counts": {"all": 0, "interacted": 0, "untouched": 0},
                "actionOptions": []}
    items = []
    for row in rows:
        interacted_row = int(row.get("interacted") or 0)
        items.append({
            "key": f"video:{row.get('video_id') or row.get('video')}",
            "videoUrl": row.get("video") or "",
            "title": row.get("video") or "",
            "author": row.get("account") or "",
            "topic": row.get("source_keyword") or "",
            "collectedAt": _fmt(row.get("created_at")),
            "createTime": _fmt(row.get("create_time") or row.get("created_at")),
            "commentCount": int(row.get("comment_count") or 0),
            "account": row.get("account") or "",
            "state": "已互动" if interacted_row else "未互动",
            "actions": f"{interacted_row} 次",
        })
    return {"success": True, "items": items, "total": total,
            "counts": {"all": all_count, "interacted": interacted,
                       "untouched": max(all_count - interacted, 0)},
            "actionOptions": []}


# 群聊读取：同一时刻只允许一个读取请求；取消用 requestId 标记，读到下一个账号时退出。
_GROUP_READ_CANCELS: set[str] = set()
_GROUP_READ_LOCK = threading.Lock()


def _read_monitor_group_list(payload: Any) -> dict:
    """监控对象弹窗「读取群聊」：逐个账号打开会话列表读群聊，并写入本机缓存。"""
    from app.runtime import group_list

    data = payload if isinstance(payload, Mapping) else {}
    request_id = str(data.get("requestId") or "")
    ids = [str(x).strip() for x in _as_list(data.get("accountIds")) if str(x).strip()]
    ids = list(dict.fromkeys(ids))[:group_list.MAX_ACCOUNTS]
    if not ids:
        return {"success": False, "error": "请先选择监控账号"}
    known = {str(r.get("id")) for r in _rows("SELECT id FROM accounts WHERE platform='douyin'")}
    missing = [i for i in ids if i not in known]
    if missing:
        return {"success": False, "error": f"有账号不存在或不是抖音账号：{missing[0]}"}

    def emit(event: dict) -> None:
        try:
            event_bus.publish("automation-data", {
                "type": "monitor-group-list-progress",
                "payload": {**event, "requestId": request_id}})
        except Exception:  # noqa: BLE001 推送失败不影响读取
            pass

    try:
        return group_list.read(
            DB_PATH, ids, emit=emit,
            cancelled=lambda: request_id in _GROUP_READ_CANCELS)
    finally:
        with _GROUP_READ_LOCK:
            _GROUP_READ_CANCELS.discard(request_id)


def _cancel_monitor_group_list(payload: Any) -> dict:
    data = payload if isinstance(payload, Mapping) else {}
    request_id = str(data.get("requestId") or "")
    if request_id:
        with _GROUP_READ_LOCK:
            _GROUP_READ_CANCELS.add(request_id)
    return {"success": True}


def _monitor_group_cache(_: Any) -> dict:
    from app.runtime import group_list
    try:
        return {"success": True, "groups": group_list.cached(DB_PATH)}
    except sqlite3.Error as exc:
        return {"success": False, "error": f"读取已保存群聊失败：{exc}"}


def _collected_videos_page(args: dict) -> dict:
    """采集库 · 按视频聚合分页（视频 URL + 作者 + 评论数）。"""
    payload = args if isinstance(args, Mapping) else {}
    page = max(1, int(payload.get("page") or 1))
    size = max(1, min(int(payload.get("pageSize") or 20), 200))
    keyword = str(payload.get("keyword") or "").strip()
    if not _table_exists("leads"):
        return {"success": True, "items": [], "total": 0}
    where = "TRIM(COALESCE(video,'')) <> ''"
    params: list[Any] = []
    # 隐藏名单（采集库「删除 / 清空」）：key = video_id 或完整 URL
    hidden = _hidden_values("video")
    if hidden:
        ids_part = sorted(v for v in hidden if not v.startswith("http"))
        urls_part = sorted(v for v in hidden if v.startswith("http"))
        for column, values in (("COALESCE(video_id,'')", ids_part), ("video", urls_part)):
            if values:
                where += f" AND {column} NOT IN (" + ",".join("?" * len(values)) + ")"
                params.extend(values)
    if keyword:
        where += " AND (video LIKE ? OR account LIKE ? OR source_keyword LIKE ?)"
        like = f"%{keyword}%"
        params += [like, like, like]
    try:
        rows = _rows(
            "SELECT video, MAX(video_id) AS video_id, MAX(account) AS account, "
            "       MAX(source_keyword) AS source_keyword, MIN(created_at) AS first_at, "
            "       COUNT(*) AS comment_count, COUNT(DISTINCT nickname) AS author_count "
            f"FROM leads WHERE {where} GROUP BY video "
            "ORDER BY comment_count DESC LIMIT ? OFFSET ?",
            tuple(params) + (size, (page - 1) * size))
        total = _scalar(f"SELECT COUNT(DISTINCT video) FROM leads WHERE {where}",
                        tuple(params))
    except sqlite3.Error:
        return {"success": True, "items": [], "total": 0}
    items = [{
        "key": f"video:{row.get('video_id') or row.get('video')}",
        "videoUrl": row.get("video") or "",
        "title": row.get("video") or "",
        "author": row.get("account") or "",
        "topic": row.get("source_keyword") or "",
        "commentCount": int(row.get("comment_count") or 0),
        "authorCount": int(row.get("author_count") or 0),
        "collectedAt": _fmt(row.get("first_at")),
    } for row in rows]
    return {"success": True, "items": items, "total": total}


def _collected_authors_page(args: dict) -> dict:
    """采集库 · 按评论作者聚合分页（昵称 + 评论数 + 地区）。"""
    payload = args if isinstance(args, Mapping) else {}
    page = max(1, int(payload.get("page") or 1))
    size = max(1, min(int(payload.get("pageSize") or 20), 200))
    keyword = str(payload.get("keyword") or "").strip()
    if not _table_exists("leads"):
        return {"success": True, "items": [], "total": 0}
    where = "TRIM(COALESCE(nickname,'')) <> ''"
    params: list[Any] = []
    hidden = _hidden_values("author")
    if hidden:
        values = sorted(hidden)
        where += " AND nickname NOT IN (" + ",".join("?" * len(values)) + ")"
        params.extend(values)
    if keyword:
        where += " AND (nickname LIKE ? OR region LIKE ?)"
        like = f"%{keyword}%"
        params += [like, like]
    try:
        rows = _rows(
            "SELECT nickname, MAX(region) AS region, COUNT(*) AS comment_count, "
            "       MAX(created_at) AS last_at, COUNT(DISTINCT video) AS video_count "
            f"FROM leads WHERE {where} GROUP BY nickname "
            "ORDER BY comment_count DESC LIMIT ? OFFSET ?",
            tuple(params) + (size, (page - 1) * size))
        total = _scalar(f"SELECT COUNT(DISTINCT nickname) FROM leads WHERE {where}",
                        tuple(params))
    except sqlite3.Error:
        return {"success": True, "items": [], "total": 0}
    items = [{
        "key": f"author:{row.get('nickname')}",
        "nickname": row.get("nickname") or "",
        "author": row.get("nickname") or "",
        "region": row.get("region") or "",
        "commentCount": int(row.get("comment_count") or 0),
        "videoCount": int(row.get("video_count") or 0),
        "lastActiveAt": _fmt(row.get("last_at")),
    } for row in rows]
    return {"success": True, "items": items, "total": total}


# ---- 线索库删除 / UID 导入 / 采集库隐藏通道（2026-11-17 补齐） ----
# 「删除已选 / 删除低意向 / 删除已触达 / UID 导入 / 采集库删除」此前全落
# dispatch 的 delete-/save- 前缀拦截返回「功能未开通」。数据都在 leads 与触达
# 账本里，这里按页面调用契约补齐真实读写。
# 采集库（视频 / 主页 / 历史已分析）是 leads 的聚合视图：删除语义 = 加入隐藏
# 名单（collected_hidden），视图过滤后不再出现，也「不会从任务明细同步回来」；
# 不动线索本体，避免聚合页的操作误删真实线索。

def _ensure_hidden_table() -> None:
    _exec_write(
        "CREATE TABLE IF NOT EXISTS collected_hidden ("
        "kind TEXT NOT NULL, key TEXT NOT NULL, hidden_ms INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY(kind, key))")


def _hidden_values(kind: str) -> set[str]:
    if not _table_exists("collected_hidden"):
        return set()
    return {str(r.get("key") or "") for r in _rows(
        "SELECT key FROM collected_hidden WHERE kind=?", (kind,))}


def _hide_values(kind: str, values: list[str]) -> int:
    _ensure_hidden_table()
    now = int(time.time() * 1000)
    hidden = 0
    for value in values:
        value = str(value or "").strip()
        if not value:
            continue
        hidden += _exec_write(
            "INSERT OR IGNORE INTO collected_hidden(kind,key,hidden_ms) VALUES(?,?,?)",
            (kind, value, now))
    return hidden


def _delete_leads_from_history(payload: Any) -> dict:
    ids = _ids_payload(payload)
    if not ids:
        return {"success": False, "error": "未指定要删除的线索"}
    uniq = list(dict.fromkeys(ids))[:20000]
    ph = ",".join("?" * len(uniq))
    deleted = _scalar(f"SELECT COUNT(*) FROM leads WHERE id IN ({ph})", tuple(uniq))
    _exec_write(f"DELETE FROM leads WHERE id IN ({ph})", tuple(uniq))
    return {"success": True, "deleted": int(deleted)}


def _delete_low_intention_leads(_: dict) -> dict:
    # 与前端「删除低意向」同口径：高意向 = a/high/hot（页面 isHighIntention），
    # 其余（中意向 / 低意向 / 未评级）都在删除范围。
    where = ("lower(COALESCE(intent_level,'')) NOT IN ('a','high','hot') "
             "AND lower(COALESCE(intent,'')) NOT IN ('high','hot')")
    deleted = _scalar(f"SELECT COUNT(*) FROM leads WHERE {where}")
    _exec_write(f"DELETE FROM leads WHERE {where}")
    return {"success": True, "deleted": int(deleted)}


def _delete_touched_leads(_: dict) -> dict:
    # 触达口径与线索池统计一致：触达账本里任何动作次数 > 0 的线索。
    touched = sorted(_touch_ledger().touched_ids())
    if not touched:
        return {"success": True, "deleted": 0}
    deleted = 0
    for start in range(0, len(touched), 900):
        chunk = touched[start:start + 900]
        ph = ",".join("?" * len(chunk))
        deleted += _scalar(f"SELECT COUNT(*) FROM leads WHERE id IN ({ph})", tuple(chunk))
        _exec_write(f"DELETE FROM leads WHERE id IN ({ph})", tuple(chunk))
    return {"success": True, "deleted": int(deleted)}


def _append_imported_leads(payload: Any) -> dict:
    """UID 导入：前端分批（每批 300）送来线索对象数组，按稳定 id 去重入库。"""
    raw = payload
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    items = raw if isinstance(raw, list) else []
    if not items:
        return {"success": False, "error": "没有可导入的线索"}
    from app.runtime import store as rt_store
    inserted = skipped = 0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        user_url = str(item.get("userUrl") or item.get("sourceUrl") or "").strip()
        nickname = str(item.get("nickname") or "").strip()
        lead_id = str(item.get("leadId") or item.get("id") or "").strip()
        if not lead_id:
            if not user_url and not nickname:
                continue
            seed = user_url or nickname
            lead_id = hashlib.md5(seed.encode("utf-8")).hexdigest()[:32]
        ok = rt_store.insert_lead(
            DB_PATH, lead_id=lead_id, nickname=nickname or "UID导入",
            comment="", keyword="UID导入", source="import",
            task_id=str(item.get("taskId") or "import_uid"),
            video_url=user_url, account=str(item.get("accountName")
                                             or item.get("account") or ""))
        inserted += 1 if ok else 0
        skipped += 0 if ok else 1
    return {"success": True, "inserted": inserted, "skipped": skipped}


def _remove_processed_videos(payload: Any) -> dict:
    """采集库删除：条目进隐藏名单（video:ID / author:昵称 / 历史已分析按视频文本）。"""
    raw = payload
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], list):
        raw = raw[0]
    items = raw if isinstance(raw, list) else []
    if not items:
        return {"success": False, "error": "未指定要删除的条目"}
    removed = 0
    for item in items:
        if isinstance(item, Mapping):
            key = str(item.get("key") or "").strip()
            if key.startswith("video:"):
                removed += _hide_values("video", [key[len("video:"):]])
                continue
            if key.startswith("author:"):
                removed += _hide_values("author", [key[len("author:"):]])
                continue
            removed += _hide_values("history", [str(item.get("title")
                                                    or item.get("video") or "")])
        else:
            removed += _hide_values("history", [str(item or "")])
    return {"success": True, "removed": removed}


def _clear_collected_library(payload: Any) -> dict:
    """清空采集库：type=video（采集视频）/ author（采集主页）。"""
    data = payload if isinstance(payload, Mapping) else {}
    kind = str(data.get("type") or "").strip().lower()
    if kind in {"video", "videos"}:
        kind = "video"
    elif kind in {"author", "authors"}:
        kind = "author"
    else:
        return {"success": False, "error": "未指定要清空的采集库类型"}
    if not _table_exists("leads"):
        return {"success": True, "cleared": 0}
    if kind == "video":
        rows = _rows("SELECT video, MAX(video_id) AS video_id FROM leads "
                     "WHERE TRIM(COALESCE(video,''))<>'' GROUP BY video")
        values = [str(r.get("video_id") or r.get("video") or "") for r in rows]
    else:
        rows = _rows("SELECT DISTINCT nickname FROM leads WHERE TRIM(COALESCE(nickname,''))<>''")
        values = [str(r.get("nickname") or "") for r in rows]
    cleared = _hide_values(kind, values)
    return {"success": True, "cleared": cleared}


def _test_account_proxy(payload: Any) -> dict:
    """账号池「测试连接」：后端直接经代理发起 HTTPS 请求实测连通性。

    说明：SOCKS5（含认证）由本地桥接在浏览器侧生效（Chromium 不支持 SOCKS5
    认证），Python 标准库也无法直连 SOCKS5，这里诚实返回无法测试而不是假成功。
    """
    import urllib.request
    import urllib.parse

    data = payload if isinstance(payload, Mapping) else {}
    proxy = data.get("proxy") if isinstance(data.get("proxy"), Mapping) else data
    host = str(proxy.get("host") or "").strip()
    try:
        port = int(proxy.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    ptype = str(proxy.get("type") or "http").strip().lower()
    enabled = proxy.get("enabled", proxy.get("proxyEnabled", True))
    if enabled in (False, "false", "0", 0, None) and "enabled" in proxy:
        return {"success": False, "message": "该账号未启用代理"}
    if not host or not port:
        return {"success": False, "message": "代理配置不完整（缺主机或端口）"}
    if ptype == "socks5":
        return {"success": False, "message": "SOCKS5 由本地桥接在浏览器侧生效，后端无法直接测试；如需后端测试请改用 HTTP 代理"}
    proxy_url = f"http://{host}:{port}"
    username = str(proxy.get("username") or "").strip()
    if username:
        token = urllib.parse.quote(username, safe="") + ":" + urllib.parse.quote(
            str(proxy.get("password") or ""), safe="")
        proxy_url = f"http://{token}@{host}:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": proxy_url, "https": proxy_url}))
    last_error = ""
    for target in ("https://www.douyin.com", "https://www.baidu.com"):
        try:
            started = time.time()
            with opener.open(target, timeout=10) as resp:
                resp.read(64)
            latency = int((time.time() - started) * 1000)
            return {"success": True, "message": f"连接成功（经代理访问 {target}，{latency}ms）",
                    "latencyMs": latency, "target": target}
        except Exception as exc:  # noqa: BLE001 - 逐个目标试，最后一个错误带回
            last_error = str(exc) or exc.__class__.__name__
    return {"success": False, "message": f"连接失败：{last_error}"}


def _get_cache_size(_: dict) -> int:
    """缓存大小（字节）：数据目录非数据库文件 + Electron 网络/分区缓存。

    只统计 DATA_DIR 会把 1.3G 的壳缓存报成 400M —— 用户点「清理缓存」后
    数字几乎不动，所以这里把壳的 Cache / Code Cache / GPUCache 与各分区
    Cache 一起算进来（见 app_settings.cache_size）。
    """
    return int(app_settings.cache_size())


# --------------------------------------------------------------- 系统设置 --
# 前端 SystemSettings 页的开关绝大多数是「壳侧行为」（缩放 / 静音 / 代理 /
# 硬件加速 / 实况自动收起 / 链接打开方式）。后端负责持久化 + 下发，壳读同一个
# app_settings.json 应用：重启生效项读文件启动时应用，立即生效项按 mtime 轮询。


def _setting_raw(payload: Any) -> Any:
    """设置类通道的入参归一：RAW 数组 [value] / dict {value} / 裸标量都认。"""
    if isinstance(payload, (list, tuple)):
        return payload[0] if payload else None
    if isinstance(payload, Mapping):
        for key in ("value", "enabled", "factor", "mode"):
            if key in payload:
                return payload[key]
        return None
    return payload


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "on", "yes", "enabled", "open"):
            return True
        if low in ("false", "0", "off", "no", "disabled", "close"):
            return False
    return default


def _settings_flag_handler(key: str, cast):
    """生成 get-/set- 成对的布尔/枚举设置通道。"""

    def getter(_: dict):
        return app_settings.get(key)

    def setter(payload: Any):
        raw = _setting_raw(payload)
        current = app_settings.get(key)
        value = cast(raw, current)
        merged = app_settings.update(**{key: value})
        return merged[key]

    return getter, setter


def _get_ui_zoom(_: dict) -> int:
    return int(app_settings.get("uiZoomFactor") or 100)


def _set_ui_zoom(payload: Any) -> int:
    raw = _setting_raw(payload)
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return int(app_settings.get("uiZoomFactor") or 100)
    value = max(50, min(200, value))  # 前端下拉是 100/110/125/150，手输也不越界
    return int(app_settings.update(uiZoomFactor=value)["uiZoomFactor"])


def _set_all_views_muted(payload: Any) -> dict:
    value = _as_bool(_setting_raw(payload), bool(app_settings.get("allViewsMuted")))
    app_settings.update(allViewsMuted=value)
    return {"success": True, "muted": value}


def _shell_task(action: str, timeout: float = 8.0, **extra) -> dict | None:
    """下发一条壳侧任务并等结果（壳的采集执行器轮询 /desktop/command）。

    壳忙着跑采集时会排在队尾；超时返回 None，调用方必须如实告知用户
    「没做成」，绝不返回 success:true 冒充成功。
    """
    cmd_id = desktop_commands.publish({"type": "shell-task", "action": action, **extra})
    return desktop_commands.wait_result(cmd_id, timeout=timeout)


def _clear_cache(_: dict) -> dict:
    """清理缓存：后端临时缓存立即清；壳的网络/图形缓存交给壳清（等回执）。"""
    freed, failed = app_settings.clear_backend_cache()
    # 壳要逐个会话 clearCache 再删图形缓存目录，冷启动首个会话可能十几秒；
    # 给足 30s，避免「活干完了但回执没到」被误报成「需空闲时清理」
    shell = _shell_task("clear-cache", timeout=30.0)
    if shell is None:
        return {
            "success": bool(freed or not failed),
            "cleared": 1,
            "failed": failed,
            "skipped": 0,
            "freed": int(freed),
            "note": "已清理本地数据缓存；浏览器缓存需桌面壳空闲时清理"
                    if desktop_commands.agent_alive() else
                    "已清理本地数据缓存；桌面壳未运行，浏览器缓存暂未清理",
        }
    ok = bool(shell.get("ok", shell.get("success", False)))
    if not ok:
        return {"success": False,
                "reason": str(shell.get("error") or "桌面壳缓存清理失败"),
                "failed": int(shell.get("failed") or 0) + failed}
    return {
        "success": True,
        "cleared": 1 + int(shell.get("cleared") or 0),
        "failed": int(shell.get("failed") or 0) + failed,
        "skipped": int(shell.get("skipped") or 0),
        "freed": int(freed) + int(shell.get("freed") or 0),
    }


def _cleanup_idle_accounts(_: dict) -> dict:
    """清理闲置账号数据：14 天未使用的 Session 分区（仅确认后执行）。"""
    pruned = app_settings.prune_history(30)
    shell = _shell_task("cleanup-idle-accounts", timeout=30.0, days=14)
    if shell is None:
        alive = desktop_commands.agent_alive()
        return {"success": False,
                "reason": "桌面壳未运行或正忙（有任务在跑），Session 分区只在壳内可清理"
                          if not alive else
                          "桌面壳正忙（有任务在跑），请等任务结束后再试"}
    if not bool(shell.get("ok", shell.get("success", False))):
        return {"success": False, "reason": str(shell.get("error") or "清理闲置账号数据失败")}
    partitions = shell.get("partitions") or {}
    store_prune = shell.get("storePrune") or {}
    return {
        "success": True,
        "partitions": {
            "scanned": int(partitions.get("scanned") or 0),
            "deleted": int(partitions.get("deleted") or 0),
        },
        "storePrune": {"changed": bool(store_prune.get("changed")) or pruned > 0},
    }


def _submit_faq_question(payload: Any) -> dict:
    """问题反馈：编号由后端生成并落盘，用户拿到的编号一定查得到。"""
    return faq_feedback.submit(payload)


def _faq_clipboard_image(_: dict) -> dict:
    """读剪贴板截图：剪贴板只存在于桌面壳，后端下发壳侧任务并等回执。

    壳忙着跑采集时任务排在队尾，超时/失败如实返回「读不到」——前端会提示
    用户直接 Ctrl+V 或从本地选图，绝不假装成功（假 base64 会变成坏图）。
    """
    shell = _shell_task("read-clipboard-image", timeout=6.0)
    if shell is None:
        alive = desktop_commands.agent_alive()
        return {
            "success": False,
            "msg": "桌面壳正忙（有任务在跑），请直接 Ctrl+V 粘贴或从本地选择图片"
                   if alive else
                   "桌面壳未运行，暂时读不到剪贴板，请直接从本地选择图片",
        }
    if not bool(shell.get("ok", shell.get("success", False))):
        return {"success": False, "msg": str(shell.get("error") or "剪贴板里没有可用的截图")}
    data_url = str(shell.get("base64") or "").strip()
    if not data_url.startswith("data:image/"):
        return {"success": False, "msg": "剪贴板里没有可用的截图"}
    return {"success": True, "base64": data_url, "bytes": int(shell.get("bytes") or 0)}


def _get_settings(_: dict) -> dict:
    """诊断用：一次读出全部设置（供验收脚本与健康检查）。"""
    data = app_settings.all_settings()
    data["cacheSize"] = int(app_settings.cache_size())
    data["autoCleanup"] = app_settings.auto_cleanup_state()
    return data


_get_use_system_proxy, _set_use_system_proxy = _settings_flag_handler(
    "useSystemProxy", lambda raw, cur: _as_bool(raw, cur))
_get_preview_auto_close, _set_preview_auto_close = _settings_flag_handler(
    "backgroundPreviewAutoClose", lambda raw, cur: _as_bool(raw, cur))
_get_hardware_accel, _set_hardware_accel = _settings_flag_handler(
    "hardwareAcceleration", lambda raw, cur: _as_bool(raw, cur))
_get_auto_cache_cleanup, _set_auto_cache_cleanup = _settings_flag_handler(
    "autoCacheCleanup", lambda raw, cur: _as_bool(raw, cur))
_get_manual_link_mode, _set_manual_link_mode_pair = _settings_flag_handler(
    "manualLinkOpenMode", lambda raw, cur: ("account" if str(raw).strip().lower() == "account"
                                            else "browser") if raw is not None else cur)


def _templates_file() -> Path:
    return DATA_DIR / "work_publish_templates.json"


def _load_publish_templates() -> list[dict]:
    path = _templates_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _store_publish_templates(templates: list[dict]) -> None:
    path = _templates_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(templates, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _save_work_publish_template(payload: Any) -> list[dict]:
    data = payload if isinstance(payload, Mapping) else {}
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")
    settings = data.get("settings") if isinstance(data.get("settings"), Mapping) else {}
    templates = _load_publish_templates()
    now = int(time.time() * 1000)
    for rec in templates:
        if str(rec.get("name") or "") == name:
            rec["settings"] = dict(settings)
            rec["updatedAt"] = now
            break
    else:
        templates.append({"id": str(now), "name": name, "createdAt": now,
                          "updatedAt": now, "settings": dict(settings)})
    _store_publish_templates(templates)
    return templates


def _delete_work_publish_template(payload: Any) -> list[dict]:
    raw = payload
    if isinstance(raw, list):
        raw = raw[0] if len(raw) == 1 else raw
    template_id = str(raw or "").strip()
    templates = _load_publish_templates()
    kept = [rec for rec in templates if str(rec.get("id") or "") != template_id]
    if len(kept) == len(templates):
        raise ValueError("模板不存在或已被删除")
    _store_publish_templates(kept)
    return kept


def _delete_work_publish_tasks(payload: Any) -> dict:
    ids = _ids_payload(payload)
    if not ids:
        return {"success": False, "error": "未指定要删除的任务"}
    uniq = list(dict.fromkeys(ids))[:500]
    deleted = 0
    for task_id in uniq:
        deleted += task_store.delete_task(DB_PATH, "publish", task_id)
    ph = ",".join("?" * len(uniq))
    if _table_exists("task_runtime_logs"):
        _exec_write(f"DELETE FROM task_runtime_logs WHERE task_id IN ({ph})", tuple(uniq))
    return {"success": True, "deleted": int(deleted)}


def _resolve_douyin_video_urls(payload: Any) -> dict:
    """v.douyin.com 短链 / 分享文案批量解析（复用监控链路的解析器与缓存）。"""
    data = payload if isinstance(payload, Mapping) else {}
    urls = data.get("urls") or data.get("url") or []
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list):
        urls = []
    from app.runtime.monitor_engine import resolve_video_url
    resolved: list[str] = []
    for url in urls[:200]:
        text = str(url or "").strip()
        resolved.append(resolve_video_url(text) if text else "")
    return {"success": True, "resolved": resolved}


def _pick_work_publish_media(payload: Any) -> dict:
    """pick-work-publish-media：壳里选素材。数量/大小/时长都在壳侧先卡一遍。"""
    data = payload if isinstance(payload, Mapping) else {}
    kind = str(data.get("type") or data.get("mediaType") or "video")
    if kind not in {"video", "image"}:
        return {"success": False, "msg": "未知素材类型"}
    res = _shell_task("pick-work-publish-media", timeout=600.0, mediaType=kind)
    if res is None:
        return {"success": False, "msg": "桌面壳未响应，无法打开文件选择框"}
    if res.get("canceled"):
        return {"success": False, "canceled": True, "msg": "已取消选择"}
    if not res.get("success", res.get("ok")):
        return {"success": False, "msg": str(res.get("msg") or res.get("error") or "选择素材失败")}
    return {"success": True, "files": res.get("files") or []}


def _pick_work_publish_cover(_: Any) -> dict:
    res = _shell_task("pick-work-publish-cover", timeout=600.0)
    if res is None:
        return {"success": False, "msg": "桌面壳未响应，无法打开文件选择框"}
    if res.get("canceled"):
        return {"success": False, "canceled": True, "msg": "已取消选择"}
    if not res.get("success", res.get("ok")):
        return {"success": False, "msg": str(res.get("msg") or res.get("error") or "选择封面失败")}
    return {"success": True, "file": res.get("file")}


def _show_work_publish_monitor(payload: Any) -> dict:
    """show-work-publish-monitor：在账号自己的窗口里打开发布页（不新开浏览器）。"""
    data = payload if isinstance(payload, Mapping) else {}
    task_id = str(data.get("taskId") or data.get("id") or "").strip()
    rec = task_store.get_task(DB_PATH, "publish", task_id) if task_id else None
    if not rec:
        return {"success": False, "error": "任务不存在"}
    items = [i for i in (rec.get("items") or []) if isinstance(i, Mapping)]
    account_id = str(data.get("accountId") or "")
    if not account_id:
        pending = [i for i in items if str(i.get("status") or "") not in {"published", "uncertain"}]
        account_id = str((pending or items or [{}])[0].get("accountId") or "")
    if not account_id:
        return {"success": False, "error": "任务里没有可用的发布账号"}
    from app.runtime import browser
    if not browser.shell_available():
        return {"success": False, "error": "桌面壳未运行，发布画面无法创建"}
    res = browser.execute({"type": "open-work-publish-view", "accountId": account_id},
                          db_path=DB_PATH, timeout=120, account_id=account_id, engine="shell")
    if not res.get("ok"):
        return {"success": False, "error": str(res.get("error") or "发布画面尚未创建，请稍后再试")}
    return {"success": True, "accountId": account_id, "url": str(res.get("url") or "")}


def _work_publish_generate(payload: Any) -> dict:
    """AI 生成发布文案：主题 / 要求 / 差异度 → 每篇 {title, description}。

    契约（useWorkPublishRunner）：响应 items 数量必须正好等于 count 且全部
    非空，否则前端整批报错；标题字数 视频≤30 / 图文≤20，描述≤1000，不生成
    话题标签。
    """
    data = payload if isinstance(payload, Mapping) else {}
    seed_title = str(data.get("seedTitle") or "").strip()
    seed_desc = str(data.get("seedDesc") or "").strip()
    try:
        variation = max(0, min(100, int(data.get("variation") or 0)))
    except (TypeError, ValueError):
        variation = 0
    media_type = str(data.get("mediaType") or "video").strip().lower()
    try:
        count = max(1, min(20, int(data.get("count") or 1)))
    except (TypeError, ValueError):
        count = 1
    if not seed_title and not seed_desc:
        return {"success": False, "error": "请先填写文案主题或要求"}
    title_limit = 20 if media_type == "image" else 30

    from app.core.dependencies import get_ai_service
    from app.services.ai_service import AIService

    service = get_ai_service()
    kind_label = "图文" if media_type == "image" else "视频"
    system = ("你是抖音内容运营文案助手。只输出 JSON 数组，不要输出任何解释文字。"
              "文案口语化、符合抖音风格；不生成话题标签（#）、不 @ 任何人。")
    prompt = (
        f"请为 {count} 条待发布的{kind_label}作品各生成一组文案。\n"
        f"主题：{seed_title or '（未填写，参考要求自行展开）'}\n"
        f"要求：{seed_desc or '（未填写）'}\n"
        f"差异度：{variation}/100（数值越高，各条之间的表达角度和措辞差异越大；"
        f"始终围绕同一主题）。\n"
        f"硬性规则：每条 title（{kind_label}标题）不超过 {title_limit} 个字；"
        f"description（作品描述）不超过 1000 个字且非空；各条之间明显不同。\n"
        f'只输出 JSON 数组，格式：[{{"title":"...","description":"..."}}, …]，'
        f"正好 {count} 条。")
    raw = service._call_llm(prompt, system=system, timeout=120)
    parsed = AIService._extract_json(raw)
    if isinstance(parsed, Mapping):
        parsed = parsed.get("items") or parsed.get("list") or []
    if not isinstance(parsed, list) or len(parsed) < count:
        raise ValueError("AI 返回文案数量不足，请重试")
    items: list[dict] = []
    for entry in parsed[:count]:
        if not isinstance(entry, Mapping):
            raise ValueError("AI 返回文案格式不正确，请重试")
        title = str(entry.get("title") or "").strip()
        description = str(entry.get("description") or "").strip()
        if not title or not description:
            raise ValueError("AI 返回的标题或描述为空，请重试")
        if len(title) > title_limit:
            title = title[:title_limit]
        if len(description) > 1000:
            description = description[:1000]
        items.append({"title": title, "description": description})
    quota = _quota({})
    remain = int((quota.get("data") or {}).get("remain") or 0)
    return {"success": True, "items": items, "workPublishQuota": remain}


def _video_main_comments(payload: Any) -> dict:
    """视频主评记录：读 live_action_results 里 post-comment 真实回执。"""
    data = payload if isinstance(payload, Mapping) else {}
    page = max(1, int(data.get("page") or 1))
    size = max(1, min(int(data.get("pageSize") or 20), 100))
    account = str(data.get("accountId") or "").strip()
    keyword = str(data.get("keyword") or "").strip()
    if not _table_exists("live_action_results"):
        return {"success": True, "items": [], "total": 0}
    where = "action='post-comment'"
    params: list[Any] = []
    if account:
        where += " AND account_id=?"
        params.append(account)
    if keyword:
        where += " AND (text LIKE ? OR url LIKE ? OR title LIKE ?)"
        like = f"%{keyword}%"
        params += [like, like, like]
    total = _scalar(f"SELECT COUNT(*) FROM live_action_results WHERE {where}", tuple(params))
    rows = _rows(
        f"SELECT id, account_id, url, text, title, status, created_ms "
        f"FROM live_action_results WHERE {where} "
        "ORDER BY created_ms DESC LIMIT ? OFFSET ?",
        tuple(params) + (size, (page - 1) * size))
    name_map: dict[str, str] = {}
    if rows and _table_exists("accounts"):
        ids = list({str(r.get("account_id") or "") for r in rows} - {""})
        if ids:
            ph = ",".join("?" * len(ids))
            for r in _rows(f"SELECT id, name FROM accounts WHERE id IN ({ph})", tuple(ids)):
                name_map[str(r.get("id") or "")] = str(r.get("name") or "")
    items = [{
        "id": str(r.get("id") or ""),
        "content": str(r.get("text") or ""),
        "comment": str(r.get("text") or ""),
        "accountId": str(r.get("account_id") or ""),
        "accountName": name_map.get(str(r.get("account_id") or ""), ""),
        "url": str(r.get("url") or ""),
        "videoUrl": str(r.get("url") or ""),
        "videoId": (re.search(r"/video/(\d+)", str(r.get("url") or "")).group(1)
                    if re.search(r"/video/(\d+)", str(r.get("url") or "")) else ""),
        "videoTitle": str(r.get("title") or ""),
        "status": str(r.get("status") or ""),
        "time": _fmt(r.get("created_ms")),
    } for r in rows]
    return {"success": True, "items": items, "total": int(total)}


def _clear_video_main_comments(_: dict) -> dict:
    """清空主评记录 = 使「已主评视频」去重记忆失效（页面确认弹窗明示后果）。"""
    if not _table_exists("live_action_results"):
        return {"success": True, "cleared": 0}
    cleared = _scalar(
        "SELECT COUNT(*) FROM live_action_results WHERE action IN ('post-comment','post-comment-readback')")
    _exec_write("DELETE FROM live_action_results WHERE action IN ('post-comment','post-comment-readback')")
    return {"success": True, "cleared": int(cleared)}


def _monitor_live_events(args: dict) -> dict:
    """监控运行时事件明细：读 monitor_live_events 真实事件（runtime 每轮写入）。

    筛选：targetKey（房间） / eventType（事件类型 enter/like/follow/comment/gift）/
    outcome（all / success=命中 / failed=未命中或研判失败）。
    运行日志在 get-monitor-task-runtime-logs（页面「日志」tab），这里只出事件。
    """
    payload = args if isinstance(args, Mapping) else {}
    task_id = str(payload.get("taskId") or "").strip()
    if not task_id:
        return {"success": True, "items": [], "total": 0, "offset": 0,
                "limit": 0, "note": "缺少 taskId"}
    limit = max(1, min(int(payload.get("limit") or 100), 500))
    offset = max(0, int(payload.get("offset") or 0))
    where = "WHERE task_id=?"
    params: list = [task_id]
    target_key = str(payload.get("targetKey") or "").strip()
    if target_key:
        where += " AND target_key=?"
        params.append(target_key)
    event_type = str(payload.get("eventType") or "").strip()
    if event_type:
        where += " AND kind=?"
        params.append(event_type)
    outcome = str(payload.get("outcome") or "").strip()
    if outcome == "success":
        where += " AND outcome IN ('matched','signal')"
    elif outcome == "failed":
        where += " AND outcome IN ('filtered','ai_failed')"
    kind_labels = {"comment": "弹幕", "enter": "进入直播间", "like": "点赞",
                   "follow": "关注主播", "gift": "送礼"}
    outcome_labels = {"pending": "处理中", "matched": "命中", "filtered": "未命中",
                      "ai_failed": "AI 研判失败", "dedup": "已读过", "signal": "互动信号"}
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        from app.runtime.store import _ensure_live_tables
        from app.runtime import monitor_engine
        _ensure_live_tables(con)
        total = int(con.execute(f"SELECT COUNT(*) FROM monitor_live_events {where}",
                                tuple(params)).fetchone()[0])
        rows = con.execute(
            f"SELECT * FROM monitor_live_events {where} ORDER BY id DESC "
            f"LIMIT {limit} OFFSET {offset}", tuple(params)).fetchall()
        # 执行账号展示名：account_name 缺失时从 accounts 表补
        acc_names: dict[str, str] = {}
        if rows:
            ids = {r["account_id"] for r in rows if r["account_id"]}
            if ids and _table_exists("accounts"):
                marks = ",".join("?" * len(ids))
                for r in con.execute(
                        f"SELECT id, nickname, name FROM accounts WHERE id IN ({marks})",
                        tuple(ids)):
                    acc_names[r["id"]] = str(r["nickname"] or r["name"] or r["id"])
        items = [{
            "id": r["id"],
            "taskId": task_id,
            "targetKey": r["target_key"],
            "room": monitor_engine.room_label(r["target_key"]),
            "kind": r["kind"],
            "eventType": kind_labels.get(r["kind"], r["kind"]),
            "user": r["nickname"] or "",
            "event": (r["content"] if r["kind"] == "comment"
                      else kind_labels.get(r["kind"], r["kind"])),
            "result": outcome_labels.get(r["outcome"], r["outcome"]),
            "outcome": r["outcome"],
            "keyword": r["keyword"] or "",
            "note": r["note"] or "",
            "actions": r["actions"] or "",
            "retry": "",      # 直播事件暂无补做机制
            "accountId": r["account_id"] or "",
            "accountName": acc_names.get(r["account_id"], r["account_id"] or ""),
            "createdAt": _iso(r["created_ms"] / 1000 if r["created_ms"] else None),
            "time": _fmt(r["created_ms"] / 1000 if r["created_ms"] else None),
        } for r in rows]
    except sqlite3.Error:
        items, total = [], 0
    finally:
        con.close()
    note = "" if items or total else "该任务暂无直播事件：启动任务并监听到直播间后，事件会逐条写入"
    return {"success": True, "items": items, "total": total,
            "offset": offset, "limit": limit, "note": note}


# --------------------------------------------------------------------------
# 三类真机运行时（线索采集 / 监控 / 养号）的启停与证据
#
# 契约（从前端 bundle 反推，必须严格对齐）：
#   start-*-runtime   入参 {taskRecord, accounts}；成功时**返回整条 taskRecord**
#                     （页面 `v.success ? M` 直接把它当运行中的任务用），
#                     已在跑则带 alreadyRunning=true。
#   stop-*-runtime    入参 {taskId} / {taskIds}；返回 {success, ...}。
#   get-monitor-task-cycles  入参 {taskId, offset, limit}；返回**裸数组**。
#
# 为什么要在这里再存一次 taskRecord：页面每次点启动都会把当前配置整条发过来，
# 如果只启动不落库，运行时读到的是上一次保存的旧配置（改完关键词点启动仍按旧词跑）。
# --------------------------------------------------------------------------
def _rt_args(payload: Any) -> tuple[dict, dict]:
    args = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(args, Mapping):
        return {}, {}
    record = args.get("taskRecord")
    record = dict(record) if isinstance(record, Mapping) else dict(args)
    return record, dict(args)


def _rt_start(kind: str) -> Callable[[Any], dict]:
    def build(payload: Any) -> dict:
        from app.runtime import registry

        record, args = _rt_args(payload)
        task_id = str(record.get("id") or args.get("taskId") or "").strip()
        if not task_id:
            return {"success": False, "error": "缺少任务 id，无法启动"}
        record["id"] = task_id
        # 启动 ≠ 重写配置。壳在「只传 id 就启动」的路径上（重跑、排期、外部调用）
        # 会把整条记录覆盖成只剩 id——配置被抹平，任务随即报「没有可执行目标」，
        # 而且这个损坏是持久的。所以：payload 没带 configSnapshot 时以库里为准，
        # 只把 payload 里的非空字段补上去。
        existing = None
        try:
            if kind == "entity":
                existing = leadgen_pools.load_records(DB_PATH).get(task_id)
            else:
                existing = task_store.get_task(DB_PATH, kind, task_id)
        except Exception:  # noqa: BLE001
            existing = None
        if isinstance(existing, dict) and not isinstance(record.get("configSnapshot"), dict):
            merged = dict(existing)
            merged.update({k: v for k, v in record.items()
                           if v not in (None, "", [], {})})
            record = merged
        record["id"] = task_id
        if kind == "entity":
            if comment_likes.is_like_record(record):
                return {"success": False, "code": "live_writes_disabled",
                        "error": "真实评论点赞保持关闭，请使用离线模拟；不会转为线索采集"}
            record.setdefault("entityKind", "entity")
            saved = _save_entity_leadgen_task(record)
            if not saved.get("success"):
                return {"success": False, "error": saved.get("error") or "任务保存失败"}
            record = {k: v for k, v in saved.items() if k not in {"success", "saved"}}
        else:
            saved = task_store.save_task(DB_PATH, kind, record)
            if not saved.get("success"):
                return {"success": False, "error": saved.get("error") or "任务保存失败"}

        res = registry.start(DB_PATH, kind, task_id, trigger="manual")
        if not res.get("success"):
            return {"success": False, "alreadyRunning": False,
                    "error": res.get("error") or "启动失败",
                    "noEngine": bool(res.get("noEngine")), "busy": bool(res.get("busy"))}

        back = dict(record)
        back.update({"success": True, "status": "running",
                     "runId": res.get("runId"), "engine": res.get("engine")})
        if res.get("already"):
            back["alreadyRunning"] = True
        return back
    return build


def _rt_mark_stopped(kind: str, task_id: str, reason: str = "") -> None:
    """停止后把任务记录状态归位：页面列表不该继续显示「运行中」。"""
    now = int(time.time() * 1000)
    try:
        if kind == "entity":
            leadgen_pools.update_task_record(DB_PATH, task_id, {
                "status": "stopped", "endedAt": now,
                "endReason": reason or "manual_stop"})
        else:
            rec = task_store.get_task(DB_PATH, kind, task_id)
            if rec:
                rec.update({"status": "stopped", "endedAt": now,
                            "endReason": reason or "manual_stop"})
                task_store.save_task(DB_PATH, kind, rec)
    except sqlite3.Error:
        pass


def _rt_stop(kind: str) -> Callable[[Any], dict]:
    def build(payload: Any) -> dict:
        from app.runtime import registry

        record, args = _rt_args(payload)
        task_ids = args.get("taskIds")
        if isinstance(task_ids, list) and task_ids:
            results = []
            for tid in task_ids:
                tid = str(tid or "").strip()
                if not tid:
                    continue
                out = registry.stop(DB_PATH, kind, tid)
                _rt_mark_stopped(kind, tid, str(args.get("stopContext") or ""))
                results.append({"taskId": tid, "success": True,
                                "stopped": out.get("stopped", 0)})
            return {"success": True, "results": results, "count": len(results)}

        task_id = str(args.get("taskId") or record.get("id") or "").strip()
        if not task_id:
            return {"success": False, "error": "缺少 taskId"}
        out = registry.stop(DB_PATH, kind, task_id)
        _rt_mark_stopped(kind, task_id, str(args.get("stopContext") or ""))
        return {"success": True, "taskId": task_id, "stopped": out.get("stopped", 0),
                "note": out.get("note") or "已下发停止"}
    return build


def _rt_cycles(payload: Any) -> list:
    from app.runtime import store as rt_store

    record, args = _rt_args(payload)
    task_id = str(args.get("taskId") or record.get("id") or "").strip()
    try:
        offset = max(0, int(args.get("offset") or 0))
        limit = min(200, max(1, int(args.get("limit") or 20)))
    except (TypeError, ValueError):
        offset, limit = 0, 20
    items = rt_store.cycles_of(DB_PATH, task_id=task_id, limit=offset + limit)
    return items[offset:offset + limit]


def _rt_cycles_detail(payload: Any) -> dict:
    """执行历史页展开一行时用：按 runId 取这一轮执行的所有分轮记录。"""
    from app.runtime import store as rt_store

    record, args = _rt_args(payload)
    run_id = str(args.get("runId") or record.get("runId") or "").strip()
    task_id = str(args.get("taskId") or record.get("taskId") or "").strip()
    try:
        limit = min(200, max(1, int(args.get("limit") or 100)))
    except (TypeError, ValueError):
        limit = 100
    if run_id:
        items = rt_store.cycles_of(DB_PATH, run_id=run_id, limit=limit)
    elif task_id:
        items = rt_store.cycles_of(DB_PATH, task_id=task_id, limit=limit)
    else:
        items = rt_store.cycles_of(DB_PATH, limit=limit)
    return {"success": True, "cycles": items, "total": len(items)}


def _retry_monitor_interactions(payload: Any) -> dict:
    from app.runtime import manual_retry
    return manual_retry.retry(DB_PATH, payload)


def _stop_monitor_manual_retry(payload: Any) -> dict:
    from app.runtime import manual_retry
    return manual_retry.stop(str((payload or {}).get("taskId") or ""))


def _monitor_manual_retry_ids(_: Any = None) -> list[str]:
    from app.runtime import manual_retry
    return manual_retry.active_ids()


def _busy_account_ids(_: Any = None) -> list:
    """正在跑任何任务的账号 id（壳的启动校验直接读这个数组）。"""
    from app.runtime import registry

    ids: set[str] = set()
    try:
        ids |= set(registry.active_accounts())
    except Exception:  # noqa: BLE001
        pass
    try:  # 旧引擎（task_runner）里的线程也算忙，避免两条路径互相看不见
        ids |= {a for a in task_runner.active_account_ids() if a}
    except Exception:  # noqa: BLE001
        pass
    return sorted(a for a in ids if a and a != "default")


def _concurrency_limit(_: Any = None) -> dict:
    """并发上限回执：runningAccountCount 必须是真值，否则壳的「已占用」判断跑偏。"""
    from app.runtime import registry

    try:
        running = len(registry.active_accounts())
    except Exception:  # noqa: BLE001
        running = 0
    return {"success": True, "recommended": 3, "runningAccountCount": running}


# 实况大屏的三种画面来源 → 对应运行时 kind（作品发布暂无运行时，给结构完整的空态）
_LIVE_PREVIEW_KIND = {
    "entity-leadgen": "entity",
    "video-monitor": "monitor",
    "work-publish": None,
}


def _live_preview(slot: str) -> Callable[[Any], dict]:
    """`consume-*-live-preview`：大屏刷新后回来问「哪些账号有画面可看」。

    以前这里恒返回 null。壳把 null 当「没有实况」，连兜底分支都不走，
    于是大屏只显示一行「暂无需要监控的窗口」——任务明明在后台采。
    返回的 viewKey 必须和大屏卡片用同一套拼法（`<前缀>_<账号id>`），
    否则卡片有了、画面贴不上去。
    """
    prefix = {"entity-leadgen": "entity", "video-monitor": "monitor",
              "work-publish": "publish"}[slot]

    def build(_: Any = None) -> dict:
        from app.api import task_runner
        from app.runtime import registry

        kind = _LIVE_PREVIEW_KIND[slot]
        items = []
        if kind:
            try:
                items = list(registry.active(kind))
            except Exception:  # noqa: BLE001
                items = []
        if slot == "entity-leadgen":
            # 评论获客走 task_runner 引擎（不进 registry），不并集的话
            # 任务运行中大屏照样显示「暂无需要监控的窗口」。
            try:
                seen = {str(i.get("taskId")) for i in items}
                for rec in task_runner.active_records():
                    if rec.get("taskId") not in seen:
                        items.append(rec)
            except Exception:  # noqa: BLE001
                pass
        account_ids = [str(i.get("accountId")) for i in items if i.get("accountId")]
        return {
            "success": True,
            "viewKeys": [f"{prefix}_{acc}" for acc in account_ids],
            "accountIds": account_ids,
            "taskIds": [str(i.get("taskId")) for i in items if i.get("taskId")],
            "slot": slot,
        }
    return build


def _rt_running_ids(kind: str) -> Callable[[Any], list]:
    """「现在真的有任务在跑吗」——只认活着的执行线程。

    这里曾经把 `tasks` 表里 status=running/pending 的行也并进来。那张表里躺着
    66 条 9 月遗留的 pending 行（从来没有被收尾过），于是本通道恒定返回一大堆
    陈年 id。壳拿到这份「假运行清单」会做两件坏事：

      1. 用任务 id 去匹配库里的实体任务，永远匹配不上 → 认为「后台没有任务在跑」；
      2. 顺手 clearEntityLeadgenRunningTasks() 把运行中任务清空。

    结果就是：任务在跑、日志在刷，实况大屏却一张卡片都没有。
    运行状态以线程为准，命中即真，绝不拿历史行充数。
    """
    def build(_: Any = None) -> list:
        from app.api import task_runner
        from app.runtime import registry

        try:
            ids = set(registry.active_ids(kind))
        except Exception:  # noqa: BLE001
            ids = set()
        if kind == "entity":
            # 评论获客（task_runner 引擎）的运行线程不在 registry 里，必须并进来，
            # 否则壳拿到「假空清单」会把大屏的运行中任务清空。
            try:
                ids |= set(task_runner.active_ids())
            except Exception:  # noqa: BLE001
                pass
        return sorted(ids)
    return build


# 自动回复：手动回复某条私信。
# 为什么需要它：自动回复运行时是「按规则批量回」，用户看到某条消息想自己回一句时，
# 走的是同一条真实发送链路（会话内输入 → 点发送 → 校验消息出现在会话里），
# 而不是另开一条「只写数据库不真发」的假通道。
def _send_autoreply_dm(payload: Any) -> dict:
    from app.api import task_store
    from app.execution import live_scope
    from app.runtime import browser, registry

    args = payload if isinstance(payload, dict) else {}
    task_id = str(args.get("taskId") or "").strip()
    title = str(args.get("conversationTitle") or args.get("title") or "").strip()
    text = str(args.get("text") or "").strip()
    account_id = str(args.get("accountId") or "").strip()
    if not task_id or not title or not text:
        return {"success": False, "error": "需要 taskId、conversationTitle、text"}
    if len(text) < 2 or len(text) > 500:
        return {"success": False, "error": "私信正文需在 2-500 字之间"}
    if not account_id:
        rec = task_store.get_task(DB_PATH, "warmup", task_id)
        if not isinstance(rec, dict):
            return {"success": False, "error": "找不到自动回复任务"}
        from app.runtime import runners

        account_id = runners.resolve_account(DB_PATH, rec, runners.task_config(rec))
    if not account_id:
        return {"success": False, "error": "该任务没有可执行账号"}
    if registry.claim_account(account_id, f"autoreply-manual:{task_id}") is False:
        return {"success": False, "error": "该账号正在执行其他任务，请稍后重试"}
    try:
        live_scope.write_monitor_scope(task_id=task_id, account_id=account_id,
                                       actions={"send-dm-chat": 1}, hours=1.0)
        try:
            cmd = live_scope.signed_command("send-dm-chat", account_id=account_id,
                                           task_id=task_id, video_url="chat",
                                           text=text, comment_author=title)
        except ValueError as exc:
            return {"success": False, "error": f"发送未获授权：{exc}"}
        res = browser.execute(cmd, db_path=DB_PATH, account_id=account_id,
                              timeout=240.0)
        return {"success": bool(res.get("ok")),
                "status": res.get("status") or ("sent_ok" if res.get("ok") else "failed"),
                "code": res.get("code") or "", "error": res.get("error") or ""}
    finally:
        registry.release_account(account_id, f"autoreply-manual:{task_id}")
        live_scope.clear_monitor_scope(task_id, account_id)


# 只读探针：用于确认「此刻这台机器能不能真的读到平台数据」。
# 不允许任何写动作（点赞/评论/关注），养号探针强制 likeMax=0。
_PROBE_TYPES = {"search-videos", "read-source-videos", "read-comments", "read-author-works",
                "suggest-keywords", "open-video", "next-video", "watch-video",
                "warmup-watch", "sample-dom", "read-live-events", "inspect-live-ui",
                "read-group-members", "read-dm-conversations", "read-dm-threads",
                "read-interaction-notices", "read-group-list", "show-worker"}


def _browser_probe(payload: Any) -> dict:
    from app.runtime import browser

    record, args = _rt_args(payload)
    kind = str(args.get("type") or record.get("type") or "search-videos").strip()
    if kind not in _PROBE_TYPES:
        return {"success": False, "error": f"探针只允许只读指令，收到：{kind}"}
    cmd: dict[str, Any] = {"type": kind}
    if kind in {"search-videos", "suggest-keywords"}:
        cmd["keyword"] = str(args.get("keyword") or record.get("keyword") or "").strip()
        cmd["limit"] = int(args.get("limit") or 3)
        if not cmd["keyword"]:
            return {"success": False, "error": "缺少 keyword"}
    elif kind == "read-source-videos":
        # 视频入口探针：关注流 / 推荐流 / 喜欢列表 / 话题页
        source = str(args.get("source") or record.get("source") or "").strip()
        if source not in {"follow", "recommend", "like", "hashtag"}:
            return {"success": False,
                    "error": f"source 只能是 follow/recommend/like/hashtag，收到：{source or '空'}"}
        cmd["source"] = source
        cmd["keyword"] = str(args.get("keyword") or record.get("keyword") or "").strip()
        cmd["limit"] = int(args.get("limit") or 6)
        # 探针默认走 default 分区，而任务按账号走独立分区 —— 2026-11-20 真机：
        # 探针一直成功、任务一直失败，两边根本不在同一个浏览器会话里。
        # 这里透传 accountId，让探针能以任务同款分区验证入口。
        if args.get("accountId") or record.get("accountId"):
            cmd["accountId"] = str(args.get("accountId") or record.get("accountId"))
    elif kind == "read-comments":
        urls = args.get("videoUrls") or record.get("videoUrls")
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            single = str(args.get("videoUrl") or record.get("videoUrl") or "").strip()
            urls = [single] if single else []
        if not urls:
            return {"success": False, "error": "缺少 videoUrl"}
        cmd["videoUrls"] = [str(u) for u in urls][:3]
        cmd["limit"] = int(args.get("limit") or 20)
        # 楼中楼开关：评论监控链路（runners.py）一直有传，探针这边漏了，
        # 结果「回复了我的评论」这条事件源探针永远读不到 —— 子回复全被
        # 页面的「展开 N 条回复」折叠着。
        if args.get("expandSubComments") is True or record.get("expandSubComments") is True:
            cmd["expandSubComments"] = True
        win = args.get("commentWindowMinutes", record.get("commentWindowMinutes"))
        if win is not None:
            try:
                cmd["commentWindowMinutes"] = int(win)
            except (TypeError, ValueError):
                pass
    elif kind == "open-video":
        # 人式浏览探针：点搜索结果封面进视频（当前页已是目标视频则直接复用）。
        url = str(args.get("videoUrl") or record.get("videoUrl") or "").strip()
        if not re.match(r"^https://www\.douyin\.com/video/\d{6,}$", url):
            return {"success": False, "error": "缺少有效的 videoUrl"}
        cmd["videoUrl"] = url
    elif kind == "next-video":
        # 点平台的「下一个」：切换成功后回新视频地址/标题
        pass
    elif kind == "watch-video":
        # 只读「视频能不能正常播」核验：秒数封顶 10 秒，不产生任何互动。
        url = str(args.get("videoUrl") or record.get("videoUrl") or "").strip()
        if not re.match(r"^https://www\.douyin\.com/video/\d{6,}$", url):
            return {"success": False, "error": "缺少有效的 videoUrl"}
        cmd["videoUrl"] = url
        try:
            cmd["seconds"] = max(1, min(int(args.get("seconds") or 3), 10))
        except (TypeError, ValueError):
            cmd["seconds"] = 3
    elif kind == "read-author-works":
        url = str(args.get("authorUrl") or record.get("authorUrl") or "").strip()
        if not url:
            return {"success": False, "error": "缺少 authorUrl"}
        cmd["authorUrls"] = [url]
        cmd["limit"] = int(args.get("limit") or 6)
        # 养护/主页首作要按「置顶」挑选作品：探针出口也带上过滤开关与置顶标记
        if args.get("filterPinnedAuthorWorks") is True or record.get("filterPinnedAuthorWorks") is True:
            cmd["filterPinnedAuthorWorks"] = True
    elif kind == "read-group-list":
        try:
            cmd["scrollScreens"] = int(args.get("scrollScreens") or record.get("scrollScreens") or 6)
        except (TypeError, ValueError):
            cmd["scrollScreens"] = 6
    elif kind == "read-live-events":
        # 直播采集探针：贴分享文案也能用（后端负责解析出真实直播间）
        raw = args.get("liveUrl") or record.get("liveUrl") or ""
        urls = args.get("liveUrls") or record.get("liveUrls") or [raw]
        if isinstance(urls, str):
            urls = [urls]
        from app.runtime import monitor_engine as _eng

        parsed = [u for u in (_eng.extract_share_url(x) for x in urls) if u]
        au_raw = args.get("authorUrls") or record.get("authorUrls") or []
        if isinstance(au_raw, str):
            au_raw = [au_raw]
        au_parsed = [u for u in (_eng.extract_share_url(x) for x in au_raw) if u]
        if not parsed and not au_parsed:
            return {"success": False, "error": "缺少 liveUrl 或 authorUrls"}
        if parsed:
            cmd["liveUrls"] = parsed[:3]
        if au_parsed:
            # 监控主播直播探针：传主播主页链接，壳负责开播检测
            cmd["authorUrls"] = au_parsed[:3]
        dbg = args.get("debug", record.get("debug"))
        if dbg:
            cmd["debug"] = dbg  # True 或 "popup"（点昵称看用户卡片）
    elif kind == "read-group-members":
        # 群成员采集探针：传群名（与消息页显示一致），壳负责打开群读名单
        names = args.get("groupNames") or record.get("groupNames") or []
        if isinstance(names, str):
            names = [n.strip() for n in names.splitlines() if n.strip()]
        names = [str(n).strip() for n in names if str(n).strip()]
        if not names:
            return {"success": False, "error": "缺少 groupNames"}
        cmd["groupNames"] = names[:5]
    elif kind == "read-dm-conversations":
        # 私信会话列表（自动回复的监听源）：scrollScreens 控制往下滚几屏
        cmd["scrollScreens"] = int(args.get("scrollScreens") or record.get("scrollScreens") or 0)
        if args.get("accountId") or record.get("accountId"):
            cmd["accountId"] = str(args.get("accountId") or record.get("accountId"))
    elif kind == "read-dm-threads":
        # 单个会话的消息读取：titles 传会话名（与列表显示一致）
        titles = args.get("titles") or record.get("titles") or []
        if isinstance(titles, str):
            titles = [t.strip() for t in titles.splitlines() if t.strip()]
        titles = [str(t).strip() for t in titles if str(t).strip()]
        if not titles:
            return {"success": False, "error": "缺少 titles（会话名）"}
        cmd["titles"] = titles[:8]
        cmd["limit"] = int(args.get("limit") or 20)
        if args.get("accountId") or record.get("accountId"):
            cmd["accountId"] = str(args.get("accountId") or record.get("accountId"))
    elif kind == "read-interaction-notices":
        # 互动通知弹层（只读）：作品点赞 / 新增关注 / 评论被赞的事件源。
        # 抖音网页版没有独立通知页，弹层要走真实点击才会出现，壳侧已处理。
        if args.get("url") or record.get("url"):
            cmd["url"] = str(args.get("url") or record.get("url"))
        cmd["limit"] = int(args.get("limit") or record.get("limit") or 40)
        cmd["scrolls"] = int(args.get("scrolls") or record.get("scrolls") or 0)
        if args.get("accountId") or record.get("accountId"):
            cmd["accountId"] = str(args.get("accountId") or record.get("accountId"))
    elif kind == "inspect-live-ui":
        # 人工核对直播间控件：开一个真正可见的窗口，页面只读扫描 + 截图，
        # 用来回答「点赞入口 / 福袋抽奖到底在不在页面上」这种只能靠眼睛判的问题。
        raw = args.get("liveUrl") or record.get("liveUrl") or ""
        from app.runtime import monitor_engine as _eng2

        target = _eng2.extract_share_url(raw) or str(raw).strip()
        if not target:
            return {"success": False, "error": "缺少 liveUrl"}
        cmd["liveUrl"] = target
        cmd["waitMs"] = int(args.get("waitMs") or 9000)
    elif kind == "sample-dom":
        # 只读结构采样：给写选择器用。url 可选（不给就采当前页），
        # script 可选（不给就跑壳内置的结构采样）。
        url = str(args.get("url") or record.get("url") or "").strip()
        if url:
            cmd["url"] = url
        script = str(args.get("script") or record.get("script") or "").strip()
        if script:
            cmd["script"] = script
        # 显式 false 才走后台隐藏采样。默认让壳把页面置于可见渲染状态：
        # 抖音的弹层 / 懒加载列表在 document.hidden 时不渲染，隐藏采样采到的
        # 是空骨架，用它校准选择器会把「页面没渲染」误判成「选择器不对」。
        if args.get("visible") is not None or record.get("visible") is not None:
            cmd["visible"] = bool(args.get("visible", record.get("visible")))
        # 可选真实点击（页面坐标）：用于展开只在该交互后才存在的弹层再采样。
        # 仅采样通道使用，不是写动作 —— 不点关注/点赞/发送这类按钮。
        for src in (args, record):
            if src.get("clickX") is not None and src.get("clickY") is not None:
                cmd["clickX"] = float(src["clickX"])
                cmd["clickY"] = float(src["clickY"])
                break
        if args.get("clickWait") or record.get("clickWait"):
            cmd["clickWait"] = int(args.get("clickWait") or record.get("clickWait"))
        cmd["waitMs"] = int(args.get("waitMs") or 6000)
    else:  # warmup-watch：探针绝不点赞
        cmd.update({"scrolls": int(args.get("scrolls") or 2),
                    "dwellMs": int(args.get("dwellMs") or 1200), "likeMax": 0})

    started = time.time()
    # 探针也带账号：用来验证「账号 A 的登录态」和「账号 B 的隔离分区」是不是分开的。
    account_id = str(args.get("accountId") or record.get("accountId") or "").strip()
    res = browser.execute(cmd, db_path=DB_PATH, timeout=float(args.get("timeout") or 180),
                          account_id=account_id)
    out = {"success": bool(res.get("ok")), "probe": kind,
           "engine": res.get("engine"), "elapsedSec": round(time.time() - started, 1)}
    if not res.get("ok"):
        out["error"] = res.get("error") or "未知原因"
        # 失败也把壳回传的诊断带上：视频入口这类「页面没渲染 / 没登录」的
        # 结论只能靠 diag 里的计数区分，光一句错误看不出该改什么。
        if res.get("diag"):
            out["diag"] = res["diag"]
        return out
    if kind == "show-worker":
        # 「验证拦截是否真弹出来了」只能看宿主窗口可见性，不能只看 show() 没报错。
        out["visible"] = bool(res.get("visible"))
        out["account"] = res.get("account")
    if kind == "search-videos":
        items = res.get("items") or []
        # 出口不带 videoId：它属于 PLATFORM_UID_FIELDS，合规层会整条通道拦掉。
        # 页面只需要「谁发了什么、去哪看」，url 已经是公开地址；去重用的 id
        # 留在后端内部，不出接口。
        # 条数按请求的 limit 回传：写死 5 条时，壳明明抓到 60 条，页面只看到 5 条，
        # 用户会以为是「平台只给这么点」——这正是「同一个关键词只搜到 3 个视频」的一半原因。
        try:
            want = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            want = 20
        want = max(1, min(want, 200))
        out["items"] = [{"title": i.get("title"), "author": i.get("author"),
                         "url": i.get("url")} for i in items[:want]]
        out["count"] = len(items)
        # 解析到 0 条时必须能看出原因（还在渲染 / 被验证码拦 / 真空结果），
        # 否则验收只能看到「count=0」，分不清是采集坏了还是关键词真没内容。
        if res.get("note"):
            out["note"] = res["note"]
        if res.get("diag"):
            out["diag"] = res["diag"]
    elif kind == "suggest-keywords":
        # 联想词：原样带出词表 + 诊断（取证阶段要看得到容器选择依据）
        items = res.get("items") or []
        out["keyword"] = res.get("keyword")
        out["items"] = [str(t) for t in items[:20]]
        out["count"] = len(items)
        out["inputValue"] = res.get("inputValue") or ""
        if res.get("note"):
            out["note"] = res["note"]
        if res.get("diag"):
            out["diag"] = res["diag"]
    elif kind == "read-source-videos":
        items = res.get("items") or []
        out["source"] = res.get("source")
        out["url"] = res.get("url")
        out["count"] = len(items)
        # 出口不带 videoId（PLATFORM_UID_FIELDS 合规约束），只给「标题 + 作者 + 公开地址」
        out["items"] = [{"title": i.get("title"), "author": i.get("author"),
                         "url": i.get("url"), "publishedText": i.get("publishedText")}
                        for i in items[:8]]
        if res.get("note"):
            out["note"] = res["note"]
        if res.get("diag"):
            out["diag"] = res["diag"]
    elif kind in {"open-video", "next-video"}:
        # 人式浏览：带出「是怎么进到这条视频的」——点封面 / 点下一个 / 复用当前页 /
        # 退回按地址打开。验收时要能一眼分清，不要只看 ok=true。
        out["mode"] = res.get("mode") or ("next" if kind == "next-video" else "")
        out["videoUrl"] = res.get("videoUrl")
        out["title"] = res.get("title") or ""
        out["urlChanged"] = bool(res.get("urlChanged"))
        if res.get("diag"):
            out["diag"] = res["diag"]
    elif kind == "watch-video":
        # 播放核验：只报「有没有真的在播 + 这次有没有重新加载页面」。
        item = res.get("item") or {}
        out["title"] = item.get("title") or ""
        out["playing"] = bool(item.get("playing"))
        if res.get("diag"):
            out["diag"] = res["diag"]
    elif kind == "read-comments":
        comments = res.get("comments") or []
        out["count"] = len(comments)
        # 楼中楼必须连层级一起出：只有「父评论 / 子回复」的归属，后端才判得出
        # 「谁回复了谁」。以前只出昵称+正文，父与子拍平后再也拼不回去。
        out["samples"] = [{"nickname": c.get("nickname"), "text": str(c.get("text"))[:60],
                           "depth": int(c.get("depth") or 0),
                           "isAuthor": bool(c.get("isAuthor")),
                           "parentNick": c.get("parentNick") or "",
                           # 只出「有没有」，不出 id 本身：回复/点赞要靠它定位，
                           # 验收时需要知道这条链路能不能落地，但 id 不必出接口。
                           "hasCommentId": bool(c.get("commentId")),
                           "hasUid": bool(c.get("userId"))}
                          for c in comments[:8]]
        out["nested"] = sum(1 for c in comments if int(c.get("depth") or 0) > 0)
        if res.get("diag"):
            out["diag"] = res["diag"]
    elif kind == "sample-dom":
        out["url"] = res.get("url")
        if "data" in res:            # 自定义脚本：原样带回
            out["data"] = res.get("data")
        else:                       # 结构采样：整包转出，键名保持壳里那套
            for key in ("title", "readyState", "bodyLen", "loginWall",
                        "e2e", "scrollers", "rows", "leaves"):
                if key in res:
                    out[key] = res[key]
    elif kind == "read-interaction-notices":
        items = res.get("items") or []
        out["count"] = len(items)
        # 出口只带「谁做了什么」：昵称 + 动作类型 + 时间，不带任何平台 uid。
        # hasUid 只回答「这一行能不能落到具体某个人」（回关要用它做目标绑定），
        # 布尔值本身不构成标识符，所以可以出接口。
        out["items"] = [{"kind": i.get("kind"), "nickname": i.get("nickname"),
                         "timeText": i.get("timeText"), "text": str(i.get("text") or "")[:120],
                         "followState": i.get("followState"),
                         "canFollowBack": i.get("canFollowBack"),
                         "hasUid": bool(i.get("userId"))} for i in items[:60]]
        # 按类型分组计数：验收时一眼能看出「这轮到底读到了几类通知」
        tally: dict[str, int] = {}
        for i in items:
            key = str(i.get("kind") or "unknown")
            tally[key] = tally.get(key, 0) + 1
        out["tally"] = tally
        out["diag"] = res.get("diag")
        if res.get("note"):
            out["note"] = res["note"]
    elif kind == "read-author-works":
        items = res.get("items") or []
        out["count"] = len(items)
        out["items"] = [{"title": i.get("title"), "url": i.get("url"),
                         "pinned": i.get("pinned") is True} for i in items[:5]]
        out["pinnedSkipped"] = res.get("pinnedSkipped")
        out["author"] = res.get("author")
    elif kind == "read-group-list":
        out["rows"] = res.get("rows")
        out["groups"] = [{"name": g.get("name"), "groupIcon": g.get("groupIcon") is True,
                          "semiAvatar": g.get("semiAvatar"), "countSuffix": g.get("countSuffix")}
                         for g in (res.get("groups") or [])[:30]]
        # 诊断用：会话列表里的全部行 + 群判定信号（为什么被判成单聊）
        out["conversations"] = [{"name": g.get("name"), "groupIcon": g.get("groupIcon") is True,
                                 "semiAvatar": g.get("semiAvatar"),
                                 "countSuffix": g.get("countSuffix"),
                                 "preview": g.get("preview")}
                                for g in (res.get("all") or [])[:30]]
        out["note"] = res.get("note")
    elif kind == "read-live-events":
        rooms = res.get("rooms") or []
        out["rooms"] = [{
            "url": r.get("url"), "finalUrl": r.get("finalUrl"),
            "status": r.get("status"), "title": r.get("roomTitle") or r.get("title"),
            "itemCount": r.get("itemCount"),
            "events": [{"kind": e.get("kind"), "nickname": e.get("nickname"),
                        "content": e.get("content")} for e in (r.get("events") or [])[:20]],
            "debug": r.get("debug"),
        } for r in rooms]
        out["eventCount"] = sum(len(r.get("events") or []) for r in rooms)
    elif kind == "read-group-members":
        groups = res.get("groups") or []
        out["groups"] = [{
            "name": g.get("name"), "found": g.get("found"),
            "countText": g.get("countText"), "note": g.get("note"),
            "members": [{"nickname": m.get("nickname"), "role": m.get("role")}
                        for m in (g.get("members") or [])[:100]],
        } for g in groups]
        out["groupCount"] = sum(1 for g in groups if g.get("found"))
        if res.get("note"):
            out["note"] = res["note"]
    elif kind == "read-dm-conversations":
        convs = res.get("conversations") or []
        out["url"] = res.get("url")
        out["count"] = len(convs)
        out["conversations"] = [{"title": c.get("title"), "time": c.get("time"),
                                 "preview": c.get("preview"),
                                 "groupLike": int(c.get("avatarImgs") or 0) > 1}
                                for c in convs[:60]]
        if res.get("note"):
            out["note"] = res["note"]
    elif kind == "read-dm-threads":
        threads = res.get("threads") or []
        out["threads"] = [{
            "title": t.get("title"), "found": t.get("found"), "opened": t.get("opened"),
            "isGroup": t.get("isGroup"), "reason": t.get("reason"),
            "total": t.get("total"),
            "messages": [{"time": m.get("time"), "fromMe": m.get("fromMe"),
                          "text": str(m.get("text") or "")[:200]}
                         for m in (t.get("messages") or [])[:40]],
        } for t in threads]
        out["threadCount"] = sum(1 for t in threads if t.get("found"))
        if res.get("note"):
            out["note"] = res["note"]
    elif kind == "inspect-live-ui":
        first = res.get("first") or {}
        ctl = lambda i: {"tag": i.get("tag"), "cls": str(i.get("cls") or "")[:60],
                         "e2e": i.get("e2e"), "label": i.get("label"),
                         "txt": i.get("txt"), "icon": bool(i.get("icon")),
                         "x": i.get("x"), "y": i.get("y"),
                         "w": i.get("w"), "h": i.get("h")}
        out["url"] = res.get("url")
        out["title"] = first.get("title")
        out["viewport"] = {"w": first.get("vw"), "h": first.get("vh")}
        out["bodyLen"] = first.get("bodyLen")
        out["flags"] = first.get("flags") or {}
        out["likeCountText"] = first.get("likeCountText") or ""
        out["likeCandidates"] = [ctl(i) for i in (first.get("likeCandidates") or [])[:12]]
        out["luckyCandidates"] = [ctl(i) for i in (first.get("luckyCandidates") or [])[:12]]
        out["controls"] = [ctl(i) for i in (first.get("items") or [])[:80]]
        out["hoverAdded"] = [ctl(i) for i in (res.get("hoverAdded") or [])[:30]]
        out["shot"] = res.get("shot")
        if res.get("note"):
            out["note"] = res["note"]
    else:
        acts = res.get("actions") or []
        out["cycles"] = len(acts)
        out["liked"] = 0
        out["samples"] = [{"round": a.get("round"), "caption": a.get("caption")}
                          for a in acts[:3]]
    return out


def _runtime_status(_: Any = None) -> dict:
    from app.runtime import registry

    st = registry.status(DB_PATH)
    from app.execution.live_workflow import preparing_records
    return {"success": True, **st,
            "busy": task_runner.busy() or bool(comment_likes.service(DB_PATH).active()) or bool(preparing_records()),
            "active": st.get("active", []) + task_runner.active_records() + preparing_records()}


def _warmup_on_ready(payload: Any) -> dict:
    """壳启动时恢复「开机自动养号」的任务；没配置就明确返回 0，不假装启动过。"""
    from app.runtime import registry, runners

    started: list[str] = []
    skipped: list[str] = []
    for rec in task_store.list_tasks(DB_PATH, "warmup"):
        tid = str(rec.get("id") or "")
        if not tid:
            continue
        cfg = runners.task_config(rec)
        if not (cfg.get("autoStartOnLaunch") or rec.get("autoStartOnLaunch")):
            continue
        out = registry.start(DB_PATH, "warmup", tid, trigger="startup")
        (started if out.get("success") else skipped).append(tid)
    return {"success": True, "started": started, "skipped": skipped,
            "note": "只恢复勾选了「开机自动养号」的任务" if started else "没有配置开机自动养号的任务"}


# --------------------------------------------------------------------------
# 执行历史：把「排期触发了什么」与「运行时实际怎么跑的」放在一条时间线上
# --------------------------------------------------------------------------
def _execution_history(payload: Any) -> dict:
    from app.runtime import browser, registry
    from app.runtime import store as rt_store

    record, args = _rt_args(payload)
    try:
        limit = min(200, max(5, int(args.get("limit") or 50)))
    except (TypeError, ValueError):
        limit = 50
    kind = str(args.get("kind") or "").strip()

    runs = rt_store.recent_runs(DB_PATH, kind=kind, limit=limit)
    for run in runs:
        if run.get("task_name"):
            continue
        tid = str(run.get("task_id") or "")
        try:
            rec = (leadgen_pools.load_records(DB_PATH).get(tid)
                   or task_store.get_task(DB_PATH, str(run.get("kind")), tid) or {})
        except sqlite3.Error:
            rec = {}
        run["task_name"] = str(rec.get("name") or "")

    triggered = _rows(
        "SELECT kind, task_id, window_key, action, status, detail, fired_at, "
        "settled_at, finished_at, attempt FROM schedule_runs "
        + ("WHERE kind=? " if kind else "")
        + "ORDER BY id DESC LIMIT ?",
        ((kind, limit) if kind else (limit,)))
    for row in triggered:
        row["evidence"] = "schedule_runs"

    return {
        "success": True,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "engine": browser.status(DB_PATH),
        "engines": scheduler.status(DB_PATH)["engines"],
        "active": registry.active(),
        "runs": runs,
        "triggered": triggered,
        "diagnostics": rt_store.diagnostics(DB_PATH),
        "note": ("runs 是运行时实际执行记录（含每轮统计）；triggered 是排期到点的触发记录。"
                 "两者用 runId / taskId 对应，互为证据。"),
    }


EXPLICIT: dict[str, Callable[[dict], Any]] = {
    # 授权 / 系统信息
    "check-auth": _license,
    "refresh-auth": _license,
    # 本地演示授权（非商业、离线、不解锁能力）
    "activate": _license_activate,
    "license-status": _license_status,
    "license-revoke": _license_revoke,
    "license-reset": _license_reset,
    "issue-demo-license-code": _license_issue_code,
    "get-device-id": lambda _: _device_id(),
    "get-version-info": _version,
    "get-app-info": _version,
    "app-version": lambda _: "0.1.0+bridge",
    "get-runtime-state": _runtime_state,
    "get-system-announcements": _announcements,
    "refresh-system-announcements": _refresh_announcements,
    "get-radar-faqs": faq_content.faq_items,
    "submit-faq-question": _submit_faq_question,
    "read-faq-clipboard-image": _faq_clipboard_image,
    "get-license-entitlement": _license_entitlement,
    # 账号池 CK：解析/导入（写壳分区并验证登录态）/ 选文件 / 导出到剪贴板
    "parse-douyin-cookie-batch-text": cookie_session.parse_batch,
    "import-douyin-cookie-session": cookie_session.import_session,
    "pick-douyin-cookie-import-files": cookie_session.pick_files,
    "export-douyin-cookie-session": cookie_session.export_sessions,
    # 批量跟进运行态（大屏进度卡）
    "get-batch-runtime-state": lambda _: batch_follow.runtime_state(DB_PATH),
    "get-history": _lead_history,
    "get-collected-videos-page": _collected_videos_page,
    "get-collected-authors-page": _collected_authors_page,
    "get-leadgen-task-hashtag-videos": _hashtag_videos,
    # 监控对象 · 群聊选择：真机读取会话列表 + 本机缓存（未勾选的也保留）
    "read-monitor-group-list": _read_monitor_group_list,
    "cancel-monitor-group-list": _cancel_monitor_group_list,
    "get-monitor-group-list-cache": _monitor_group_cache,
    "get-system-performance": _performance,
    # 账号池 / 看板
    "get-account-pool": _accounts,
    "save-account-pool": _save_account_pool,
    "check-account-pool-status": _check_account_pool_status,
    "get-account-data-dashboard": _dashboard,
    "run-account-data-collect": lambda p: account_data.collect(DB_PATH, p),
    # 离线数据视图：创作聚合 / 消息聚合（浏览器环境不内嵌真实平台页面）
    "get-creator-aggregate": aggregate.creator_aggregate,
    "get-message-aggregate": aggregate.message_aggregate,
    "get-chat-unread-state": aggregate.chat_unread_state,
    # 任务列表
    "get-leadgen-tasks": _tasks("leadgen"),
    "get-entity-leadgen-tasks": _tasks("entity"),
    "get-monitor-tasks": _tasks("monitor"),
    "get-self-warmup-tasks": _tasks("warmup"),
    "get-work-publish-tasks": _tasks("publish"),
    "get-self-warmup-task": lambda _: (_tasks("warmup")({}) or [None])[0],
    # 任务运行：接本地执行器（真实处理已入库评论 + 真实运行日志）
    "start-task": _start_task,
    "retry-monitor-interactions": _retry_monitor_interactions,
    "stop-monitor-manual-retry": _stop_monitor_manual_retry,
    "preview-leadgen-live": lambda p: _preview_leadgen_live(p),
    "activate-leadgen-scope": _activate_leadgen_scope,
    "run-live-acceptance-action": _live_acceptance_action,
    "stop-task": _stop_task,
    # 启动前置：资源池初始化 / 关键词领取（不接线则启动流程第一步中止）
    "init-leadgen-keyword-pool": _make_pool_init("keyword"),
    "init-leadgen-hashtag-pool": _make_pool_init("hashtag"),
    "init-leadgen-specific-video-pool": _make_pool_init("specific"),
    "claim-leadgen-keyword": _claim_leadgen_keyword,
    "release-leadgen-keyword-pool": _make_pool_release("keyword"),
    "release-leadgen-hashtag-pool": _make_pool_release("hashtag"),
    "release-leadgen-specific-video-pool": _make_pool_release("specific"),
    # 任务记录持久化（跨重启恢复任务列表与运行详情）
    "save-leadgen-task": _save_leadgen_task,
    "update-leadgen-task": _update_leadgen_task,
    # 评论获客 / 评论点赞 建单与删除（整条 taskRecord 落记录仓，entityKind 标记归属）
    "save-entity-leadgen-task": _save_entity_leadgen_task,
    "delete-leadgen-tasks": _delete_leadgen_tasks,
    "delete-entity-leadgen-tasks": _delete_entity_leadgen_tasks,
    # 监控任务明细通道（此前未注册，页面按钮全部返回「功能未开通」）
    "delete-monitor-tasks": _delete_monitor_tasks,
    "delete-monitor-match-records": _delete_monitor_match_records,
    "clear-monitor-comment-seen": _clear_monitor_comment_seen,
    "clear-monitor-task-runtime-logs": _clear_entity_task_logs,
    "get-monitor-task-video-comments": _monitor_task_video_comments,
    "get-monitor-live-state": _monitor_live_state,
    "get-monitor-group-history": _monitor_group_history,
    # 监控 / 养号 / 作品发布 任务配置落库（tasks.payload 存整条 taskRecord）
    "save-monitor-task": _save_task("monitor"),
    "save-self-warmup-task": _save_task("warmup"),
    "delete-self-warmup-tasks": lambda payload: self_warmup.delete_tasks(DB_PATH, payload),
    "delete-self-warmup-events": lambda payload: self_warmup.delete_events(DB_PATH, payload),
    "clear-self-warmup-event-category": lambda payload: self_warmup.clear_category(DB_PATH, payload),
    "clear-self-warmup-user-rounds": self_warmup.clear_user_rounds,
    "clear-self-warmup-task-logs": lambda payload: self_warmup.clear_logs(DB_PATH, payload),
    "simulate-self-warmup-event": lambda payload: self_warmup_engine.simulate_event(DB_PATH, payload),
    "retry-self-warmup-events": self_warmup.retry_events,
    "stop-self-warmup-manual-retry": self_warmup.stop_retry,
    # 自动回复：手动回复某条私信（走真实会话发送 + 页面可见回执校验）
    "send-autoreply-dm": _send_autoreply_dm,
    "save-work-publish-task": _save_task("publish"),
    # 养号配置 / 自热任务补丁：纯本地写回
    #   save-nurture-config      入参是整份配置（页面 300ms 防抖全量提交）
    #   update-self-warmup-task  入参 {id, patch}，必须回执落库后的整条记录，
    #                            页面靠回执里的 autoStartOnLaunch 判断是否保存成功
    "save-nurture-config": lambda payload: nurture_config.save(DB_PATH, payload),
    "update-self-warmup-task": lambda payload: nurture_config.patch_task(DB_PATH, payload),
    # 消息中心「测试推送」：真发一条到用户配置的飞书/钉钉机器人
    # （只允许官方域名+路径，见 app/webhook_test.py，避免成为任意 URL 转发器）
    "test-chat-message-webhook": lambda payload: webhook_test.send_test(payload),
    # 定时排期：整表存读（裸 map，键=任务 id），跨重启恢复
    "save-leadgen-schedules": _schedules_write("leadgen"),
    "save-entity-leadgen-schedules": _schedules_write("entity"),
    "save-monitor-schedules": _schedules_write("monitor"),
    "save-self-warmup-schedules": _schedules_write("warmup"),
    # 监控停止：本地状态归位并落库（无执行引擎，不产生平台请求）
    "stop-monitor": _stop_monitor,
    # 账号数据页：每日自动统计时刻
    "save-account-data-schedule": _save_account_data_schedule,
    # 线索意向与详情写回（真实列：intent_level / note / comment / region …）
    "update-leads-intention": _update_leads_intention,
    "update-lead-in-history": _update_lead_in_history,
    # 线索库
    "get-leads-page": _leads_page,
    "get-leads-count": _leads_count,
    # 线索池导出：壳把 CSV 文本交上来，后端落盘到 data/exports/leads
    "save-lead-export-csvs": export_files.save_csvs,
    "get-lead-ids": _leads_ids,
    "get-leads-by-ids": _leads_by_ids,
    "query-leads-for-batch": _leads_for_batch,
    # 导出备用路径（导出走主进程时用），筛选口径与列表一致
    "export-leads": _export_leads,
    # 批量跟进：任务 / 运行记录 / 日志 / 结果 / 用户历史 / 领取规则
    "get-batch-follow-tasks": _bf_tasks,
    "save-batch-follow-task": _bf_save,
    "control-batch-follow-task": _bf_control,
    "get-batch-follow-runs-page": _bf_runs_page,
    "get-batch-follow-run-details-page": _bf_run_details,
    "get-batch-follow-user-history": _bf_user_history,
    "clear-batch-follow-runs": _bf_clear_runs,
    "delete-batch-follow-runs": _bf_delete_runs,
    "clear-batch-follow-run-logs": _bf_clear_run_logs,
    "cancel-batch-follow-user-history": _bf_cancel_user_history,
    "delete-failed-batch-follow-leads": _bf_delete_failed_leads,
    "test-batch-follow-proxy": _bf_test_proxy,
    "start-batch-action": _bf_start,
    "stop-batch-action": _bf_stop,
    # 养号安全暂停：恢复（幂等）+ 状态查询
    "resume-nurture-safety": _nurture_safety_resume,
    "get-nurture-safety": _nurture_safety_state,
    "get-leads-pool-stats": _leads_stats,
    "get-leads-filter-options": _filter_options,
    # 明细
    "get-leadgen-task-detail": _task_detail,
    "get-entity-leadgen-task": lambda a: (_task_detail(a) or {}).get("task"),
    "get-leadgen-task-leads": _task_leads,
    "get-entity-leadgen-task-leads": _task_leads,
    # 吞评检测明细：导出 / 删除关联记录 / 清空（此前未注册 → 按钮全是「功能未开通」）
    "export-comment-visibility-details": _export_visibility_details,
    "delete-comment-visibility-related-records": _delete_visibility_related,
    "delete-entity-leadgen-lead-records": _delete_entity_lead_records,
    "refresh-leadgen-task-stats": _task_stats,
    "get-processed-videos": _processed_videos,
    "get-monitor-comment-seen-stats": _monitor_seen_stats,
    # 设置类真数据
    "get-task-config": _task_config,
    "validate-leadgen-config": _validate_leadgen_config,
    "cancel-leadgen-prepared": _cancel_leadgen_prepared,
    "pause-leadgen-task": lambda a: task_runner.pause_task(a.get("taskId", ""), True),
    "resume-leadgen-task": lambda a: task_runner.pause_task(a.get("taskId", ""), False),
    "save-task-config": _save_task_config,
    "save-task-config-sync": _save_task_config,
    # AI 智能体人设库（真实落库）
    "save-ai-agents": _save_ai_agents,
    "delete-ai-agent": _delete_ai_agent,
    "get-persona-test-quota": _persona_test_quota,
    "dev-test-persona-ai": _persona_test,
    "get-task-model-settings": lambda a: ai_personas.public_model_settings(DB_PATH),
    "save-task-model-settings": lambda a: ai_personas.save_model_settings(DB_PATH, a),
    "test-task-model": _test_task_model,
    "get-live-acceptance-info": _live_acceptance_info,
    "get-work-publish-quota": _quota,
    "get-concurrency-limit": _concurrency_limit,
    "get-entity-leadgen-concurrency-limit": _concurrency_limit,
    # 实体页启动校验：壳要求该通道直接返回 id 数组（Array.isArray 判断）。
    # 「忙账号」必须包含所有 kind 的真线程，否则壳会认为后台空着，
    # 把实体任务的运行中标记清掉（实况大屏随之空卡片）。
    "get-entity-leadgen-busy-account-ids": _busy_account_ids,
    # 定时排期执行器自检：轮询线程是否活着、各类是否真有执行引擎、
    # 下一批触发点、最近实际触发记录。放在 EXPLICIT（真实接线）而不是
    # EMPTY_CHANNELS——那张表名义是「无数据源」，混进真 handler 会误导排查。
    "get-scheduler-status": lambda _: scheduler.status(DB_PATH),
    # 三类真机运行时：线索采集 / 监控 / 养号。
    # 这些通道以前落在 BLOCKED（start- 前缀），点启动只会得到「功能未开通」，
    # 现在接的是 app.runtime.registry —— 真在已登录的壳浏览器里执行。
    "start-entity-leadgen-task-runtime": _rt_start("entity"),
    "stop-entity-leadgen-task-runtime": _rt_stop("entity"),
    "start-monitor-task-runtime": _rt_start("monitor"),
    "stop-monitor-task-runtime": _rt_stop("monitor"),
    "start-self-warmup-task-runtime": _rt_start("warmup"),
    "stop-self-warmup-tasks-runtime": _rt_stop("warmup"),
    "stop-self-warmup-task": _rt_stop("warmup"),
    "start-self-warmup-on-app-ready": _warmup_on_ready,
    "get-monitor-task-cycles": _rt_cycles,
    "get-runtime-cycles": _rt_cycles_detail,
    "get-runtime-status": _runtime_status,
    # 采集关键词（联想词扩词）：前端 Settings 的 keyword-collector 弹窗，
    # 事件通道 keyword-collector-update 由 runtime 里的采集线程推送。
    "keyword-collector-start": _kwc_start,
    "keyword-collector-stop": _kwc_stop,
    "keyword-collector-state": _kwc_state,
    "keyword-collector-show": _kwc_show,
    "keyword-collector-resume": _kwc_resume,
    "run-browser-probe": _browser_probe,
    # 执行器诊断：壳是不是真的在轮询、队列里有没有积压、结果有没有回来。
    # 排查「点了没反应」时先看这个，别靠猜。
    "get-desktop-agent": lambda _: {"success": True, **desktop_commands.stats()},
    "get-execution-history": _execution_history,
    "get-work-publish-templates": lambda _: _load_publish_templates(),
    "save-work-publish-template": _save_work_publish_template,
    "delete-work-publish-template": _delete_work_publish_template,
    "delete-work-publish-task": _delete_work_publish_tasks,
    "delete-work-publish-tasks": _delete_work_publish_tasks,
    "resolve-douyin-video-urls": _resolve_douyin_video_urls,
    "work-publish-generate": _work_publish_generate,
    # 素材选择 / 实况 / 运行器（发布真机执行，见 app/runtime/publish.py）
    "pick-work-publish-media": _pick_work_publish_media,
    "pick-work-publish-cover": _pick_work_publish_cover,
    "show-work-publish-monitor": _show_work_publish_monitor,
    "update-work-publish-task": lambda p: publish_runtime.update_task(DB_PATH, p),
    "start-work-publish-task-runtime": lambda p: publish_runtime.start(DB_PATH, p),
    "stop-work-publish-task-runtime": lambda p: publish_runtime.stop(DB_PATH, p),
    "list-collected-video-import-keys": _collected_video_keys,
    "test-account-proxy": _test_account_proxy,
    "get-cache-size": _get_cache_size,
    "get-video-main-comments": _video_main_comments,
    "clear-video-main-comments": _clear_video_main_comments,
    "delete-leads-from-history": _delete_leads_from_history,
    "delete-low-intention-leads": _delete_low_intention_leads,
    "delete-touched-leads": _delete_touched_leads,
    "append-imported-leads": _append_imported_leads,
    "remove-processed-videos": _remove_processed_videos,
    "clear-collected-library": _clear_collected_library,
    # 系统设置（2026-11-17）：持久化在后端 app_settings，壳按同一份文件应用
    "get-ui-zoom-factor": _get_ui_zoom,
    "set-ui-zoom-factor": _set_ui_zoom,
    "get-use-system-proxy": _get_use_system_proxy,
    "set-use-system-proxy": _set_use_system_proxy,
    "get-background-preview-auto-close": _get_preview_auto_close,
    "set-background-preview-auto-close": _set_preview_auto_close,
    "get-manual-link-open-mode": _get_manual_link_mode,
    "set-manual-link-open-mode": _set_manual_link_mode_pair,
    "get-hardware-acceleration": _get_hardware_accel,
    "set-hardware-acceleration": _set_hardware_accel,
    "get-auto-cache-cleanup": _get_auto_cache_cleanup,
    "set-auto-cache-cleanup": _set_auto_cache_cleanup,
    "set-all-views-muted": _set_all_views_muted,
    "clear-cache": _clear_cache,
    "cleanup-idle-account-data": _cleanup_idle_accounts,
    "get-app-settings": _get_settings,
}

# 只读但暂无真实数据源 -> 保持中性空态（不造假）
EMPTY_CHANNELS: dict[str, Callable[[dict], Any]] = {
    "get-leadgen-tasks-storage-status": _storage_ok,
    "get-entity-leadgen-tasks-storage-status": _storage_ok,
    "get-monitor-tasks-storage-status": _storage_ok,
    "get-self-warmup-tasks-storage-status": _storage_ok,
    # 有本地真实数据源 -> 接真数据，不再一律空表
    "get-leadgen-schedules": _schedules_read("leadgen"),
    "get-entity-leadgen-schedules": _schedules_read("entity"),
    "get-monitor-schedules": _schedules_read("monitor"),
    "get-self-warmup-schedules": _schedules_read("warmup"),
    "get-entity-leadgen-task-runtime-logs": _entity_task_logs,
    "clear-entity-leadgen-task-runtime-logs": _clear_entity_task_logs,
    "get-monitor-task-runtime-logs": lambda _: aggregate.runtime_logs("monitor"),
    "get-self-warmup-task-logs": lambda payload: self_warmup.logs(DB_PATH, payload),
    "get-work-publish-task-runtime-logs": lambda _: aggregate.runtime_logs("publish"),
    "get-monitor-live-views": aggregate.monitor_live_views,
    "get-monitor-live-events": _monitor_live_events,
    "get-self-warmup-events": lambda payload: self_warmup.list_events(DB_PATH, payload),
    "get-self-warmup-user-stats": lambda payload: self_warmup.user_stats(DB_PATH, payload),
    "get-self-warmup-account-stats": lambda payload: self_warmup.account_stats(DB_PATH, payload),
    "get-monitor-task-matches": aggregate.monitor_task_matches,
    "get-leadgen-task-video-comments": aggregate.leadgen_video_comments,
    # 运行中判定以**真实线程**为准：库里 status=running 但线程已死的记录不算
    "get-running-entity-leadgen-task-ids": _rt_running_ids("entity"),
    "get-running-monitor-task-ids": _rt_running_ids("monitor"),
    "get-running-self-warmup-task-ids": _rt_running_ids("warmup"),
    "get-running-self-warmup-manual-retry-task-ids": self_warmup.running_retry_ids,
    "get-running-work-publish-task-ids": lambda _: aggregate.running_task_ids("publish"),
    "get-monitor-manual-retry-task-ids": _monitor_manual_retry_ids,
    "get-active-automation-tasks": aggregate.active_automation_tasks,
    "get-entity-leadgen-monitor-view-keys": lambda _: aggregate.running_task_ids("view-keys"),
    # 确认无采集源 -> 保持空，但写明口径，不假装「平台值为 0」
    "get-comment-like-results": lambda p: comment_likes.service(DB_PATH).results(p),
    "get-comment-visibility-candidates": lambda p: comment_likes.service(DB_PATH).candidates(p),
    "start-comment-like-reader": lambda p: comment_likes.service(DB_PATH).reader_start(p),
    "stop-comment-like-reader": lambda p: comment_likes.service(DB_PATH).reader_stop(p),
    "start-comment-like-simulation": _start_comment_like_simulation,
    "start-comment-like-live": _start_comment_like_live,
    "stop-comment-like-simulation": lambda p: comment_likes.service(DB_PATH).stop(str(p.get("taskId") or "")),
    "get-comment-like-runtime": lambda _: comment_likes.service(DB_PATH).runtime(),
    "get-live-dashboard-state": lambda _: live_dashboard.snapshot(DB_PATH),
    "stop-live-dashboard-task": lambda p: live_dashboard.stop(DB_PATH, p),
    "stop-all-live-dashboard-tasks": lambda _: live_dashboard.stop_all(DB_PATH),
    "get-monitor-lease-pauses": lambda _: {},
    "get-self-warmup-lease-pauses": lambda _: {},
    "consume-video-monitor-live-preview": _live_preview("video-monitor"),
    "consume-work-publish-live-preview": _live_preview("work-publish"),
    "consume-entity-leadgen-live-preview": _live_preview("entity-leadgen"),
    "get-pending-update": lambda _: {"success": True, "update": None},
    "get-last-update-download-path": lambda _: {"success": True, "path": ""},
    "support-bootstrap": lambda _: {"success": True, "enabled": False},
    "chat-reminder-diagnostic": lambda _: {"success": True, "ok": True},
    "get-chat-notification-config": _chat_notification_config,
    "save-chat-notification-config": _save_chat_notification_config,
    # 养号配置读侧必须返回**裸对象**：页面 Object.assign 直接摊进配置，
    # 包 success 信封会把 success/config 两个键写进用户配置。
    # 没有配置时返回 None，页面回落到 localStorage 并把旧配置推上来（自然迁移）。
    "get-nurture-config": lambda _: nurture_config.load(DB_PATH),
    "get-runtime-config-status": lambda _: {"success": True, "state": "ready"},
    "get-runtime-feature-defaults": lambda _: {"success": True, "features": {}, "defaults": {}},
}

# 写入 / 自动化 / 危险动作：一律未开通（DESIGN-DOC.md §3 页面接线范围）
# 例外（已接线、只动本地库或本地线程，不产生平台请求）：
#   save-account-pool  账号池真实同步（删号刷新后不再复活）
#   start-task/stop-task  评论获客本地执行器（处理已入库评论 + 真实运行日志）
BLOCKED_CHANNELS: frozenset[str] = frozenset({
    "generate",
})
_BLOCKED_PREFIXES = ("save-", "set-", "start-", "stop-", "open-", "destroy-", "run-", "update-")

# Electron BrowserView 在浏览器环境不存在。下面这些通道只做本地窗口状态管理，
# 既不是采集也不是互动；放行后由前端 bridge-views.js 往宿主容器嵌入真实抖音
# 页面 iframe（creator-micro / douyin web），避免页面永远停在「加载中」。
VIEW_ADAPTER_CHANNELS: frozenset[str] = frozenset({
    "open-creator-view", "open-chat-view",
    "reload-creator-view", "reload-chat-view",
    "update-creator-view-bounds", "update-chat-view-bounds",
    "set-creator-view-attached", "set-chat-view-attached", "set-views-visible",
    "set-message-center-session",
    "destroy-creator-view", "destroy-chat-view",
})


def _view_adapter(channel: str) -> dict:
    return {
        "success": True, "mode": "iframe-embed", "channel": channel,
        "message": "浏览器环境以 iframe 嵌入真实抖音页面，登录由用户自行完成，系统不做代操作",
    }


def dispatch(channel: str, args: list) -> Any:
    payload = args if channel in _RAW_ARGS_CHANNELS else _arg(args)
    if channel in EXPLICIT:
        return EXPLICIT[channel](payload)
    if channel in VIEW_ADAPTER_CHANNELS:
        return _view_adapter(channel)
    if channel in EMPTY_CHANNELS:
        return EMPTY_CHANNELS[channel](payload)
    if channel in BLOCKED_CHANNELS or channel.startswith(_BLOCKED_PREFIXES):
        return _disabled(channel)
    # 未识别通道：明确返回「功能未开通」，绝不返回 success:true。
    # 历史实现返回 {"success": True} 会让删除/清理/导出这类写操作变成**假成功**
    # （前端提示成功、实际什么都没发生），属数据安全级缺陷。
    return _disabled(channel)


# --------------------------------------------------------------------------
# 路由（GET 必须在 /ipc/{channel} 之前注册，避免被当作 channel 捕获）
# --------------------------------------------------------------------------
@router.get("/history", response_class=HTMLResponse)
def execution_history_page() -> str:
    """执行历史页：跨任务的统一时间线（详见 app/api/history_page.py）。"""
    from app.api.history_page import PAGE

    return PAGE


@router.get("/ipc/health")
def ipc_health() -> dict:
    sched = scheduler.status(DB_PATH)
    return {
        "success": True, "mode": "live", "renderer": str(RENDERER_DIR),
        "rendererExists": RENDERER_DIR.is_dir(), "db": str(DB_PATH), "dbExists": DB_PATH.is_file(),
        "explicitChannels": len(EXPLICIT), "emptyChannels": len(EMPTY_CHANNELS),
        "blockedChannels": len(BLOCKED_CHANNELS),
        "viewAdapterChannels": len(VIEW_ADAPTER_CHANNELS),
        # 排期执行器是否真的在跑：以前「下次 09:00」只是个显示值，到点无事发生，
        # 而健康检查也看不出来。这里给出运行线程状态与可执行类型。
        "scheduler": {
            "running": sched["running"],
            "engines": sched["engines"],
            "planned": len(sched["upcoming"]),
            "safeMode": sched["safeMode"],
            "lastTickAt": sched["lastTickAt"],
            # 执行器在线状态 + 离线暂停原因：排期「没跑」时先看这里，
            # 区分「没到点」和「执行器不在所以整体暂停」。
            "executor": sched.get("executor"),
        },
    }


@router.get("/ipc/events")
def ipc_events() -> dict:
    """事件队列：壳页面每 5 秒轮询，取走运行日志 / 状态 / 当前动作等推送。"""
    return {"events": event_bus.drain(), "pending": event_bus.pending()}


# --------------------------------------------------------------------------
# 桌面壳执行器通道（真机采集）——只走本机 loopback，与 /ipc 同一安全边界
# --------------------------------------------------------------------------
@router.get("/desktop/command")
def desktop_next_command(agent: str = "") -> dict:
    """壳轮询取一条待执行的自动化指令。

    `agent` 是壳的实例标识（诊断用）。指令「取走即出队」，一旦有两个壳同时轮询，
    后到的那个会拿到空 —— 但更糟的是指令被**另一个实例**取走、那边卡住不回传，
    等结果的这边就只能干等超时。stats 里按实例分开计数，能一眼看出是不是多开。
    """
    return {"command": desktop_commands.next_command(agent)}


@router.get("/desktop/command-status/{cmd_id}")
def desktop_command_status(cmd_id: str) -> dict:
    return desktop_commands.command_status(cmd_id)


@router.post("/desktop/command-requeue")
def desktop_requeue_command(payload: dict = Body(default={})) -> dict:
    """壳把刚取走、尚未执行的指令放回队列头（防御路径）。

    正常情况下后端派发已按账号过滤（忙账号不派发）、系统任务等全局空闲，
    壳侧不该收到「条件不满足」的指令；两道判断之间有时间差时，靠这条通道
    把指令还回去，保证不丢、不假成功。
    """
    cmd_id = str((payload or {}).get("id") or "")
    ok = bool(cmd_id) and desktop_commands.requeue_command(cmd_id)
    return {"success": ok}


@router.post("/desktop/warmup-progress")
def desktop_warmup_progress(payload: dict = Body(default={})) -> dict:
    account_id = desktop_commands.warmup_progress_account(str(payload.get("id") or ""))
    video_id = str(payload.get("videoId") or "")
    if not account_id or not video_id.isdecimal() or not 6 <= len(video_id) <= 24:
        return {"success": False}
    event_bus.publish("automation-data", {
        "type": "current-action", "payload": {"accountId": account_id,
        "action": "养号中：正在播放视频", "level": "normal"}})
    return {"success": True}


@router.post("/desktop/result")
def desktop_submit_result(payload: dict = Body(default={})) -> dict:
    """壳回传指令执行结果：评论列表 / 统计 / 失败原因。"""
    cmd_id = str((payload or {}).get("id") or "")
    if not cmd_id:
        return {"success": False, "error": "缺少指令 id"}
    desktop_commands.submit_result(cmd_id, payload)
    return {"success": True}


@router.post("/desktop/collect-once")
def desktop_collect_once(payload: dict = Body(default={})) -> dict:
    """试跑一次真机采集：下发一条 collect-comments 并等回传（不建任务记录）。

    用于验收链路：搜得到 → 进得去 → 读得到评论。默认只读不写库（ingest=false），
    确认无误后再由正式任务走入库流程。
    """
    payload = payload if isinstance(payload, Mapping) else {}
    keyword = str(payload.get("keyword") or "").strip()
    if not keyword:
        return {"success": False, "error": "请提供一个搜索关键词"}
    if not desktop_commands.agent_alive():
        return {"success": False,
                "error": "桌面壳执行器不在线：请先打开「阿里山AI获客」桌面应用，"
                         "并确认抖音账号已登录"}
    try:
        video_max = max(1, min(int(payload.get("videoMax") or 3), 20))
    except (TypeError, ValueError):
        video_max = 3
    try:
        timeout = max(20.0, min(float(payload.get("timeout") or 150), 300.0))
    except (TypeError, ValueError):
        timeout = 150.0

    cmd_id = desktop_commands.publish({
        "type": "collect-comments",
        "keyword": keyword,
        "videoMax": video_max,
        "accountId": str(payload.get("accountId") or ""),
        "viewKey": str(payload.get("viewKey") or ""),
        "taskId": "trial",
        "leadgenTaskId": "",
    })
    res = desktop_commands.wait_result(cmd_id, timeout=timeout)
    if res is None:
        return {"success": False, "cmdId": cmd_id,
                "error": f"等待壳回传超时（{timeout:.0f}s）：可能页面加载慢或被验证码拦住，"
                         f"请看桌面壳里的抖音画面当前停在哪个页面"}

    ok = bool(res.get("ok"))
    comments = res.get("comments") or []
    out: dict = {
        "success": ok,
        "cmdId": cmd_id,
        "engine": "electron",
        "keyword": keyword,
        "stats": res.get("stats") or {},
        "note": res.get("note") or "",
        "diag": res.get("diag") or {},
        "sample": [{"nickname": c.get("nickname"), "text": str(c.get("text") or "")[:60]}
                   for c in comments[:5]],
        "commentCount": len(comments),
    }
    if not ok:
        out["error"] = res.get("error") or "采集失败（壳未给出原因）"
        return out
    if payload.get("ingest") and comments:
        out["ingest"] = collector.ingest_comments(
            DB_PATH, comments, keyword, task_id="trial",
            account=str(payload.get("accountId") or ""))
    return out


@router.get("/desktop/collector-status")
def desktop_collector_status() -> dict:
    """自检用：真机模式开关 + 指令队列水位 + 最近入库。"""
    return {
        "realMode": task_runner.real_mode_enabled(DB_PATH),
        "engine": task_runner.real_engine(DB_PATH),
        "agentAlive": desktop_commands.agent_alive(),
        "queue": desktop_commands.stats(),
        "recent": collector.latest_collected(DB_PATH, limit=5),
    }


@router.post("/ipc/{channel}")
async def ipc_invoke(channel: str, request: Request, payload: dict = Body(default={})) -> dict:
    """通道分发的统一入口。

    ⚠️ 必须把 dispatch 丢到线程池：dispatch 下面的通道全是同步实现，其中
    run-browser-probe / start-*-runtime 这类会**等壳回传几十秒到几分钟**。
    以前这里是 `data = dispatch(...)` 直接同步调用 —— 它跑在事件循环线程上，
    一条探针就能把整个后端冻住：期间壳的 /desktop/command 轮询收不到响应、
    /bridge-info 也超时，看起来像「后端挂了」，实际是 event loop 被占住。
    run_in_threadpool 之后，长任务只占一个工作线程，loop 照常收发。
    """
    raw = payload.get("args") if isinstance(payload, Mapping) else None
    args = list(raw) if isinstance(raw, (list, tuple)) else []
    try:
        data = await run_in_threadpool(dispatch, channel, args)
        assert_public_safe(data)
        return {"success": True, "data": data, "mode": "live"}
    except BridgeComplianceError as exc:
        # 合规问题不降级、不回退假数据
        return {"success": False, "error": str(exc), "mode": "live"}
    except Exception as exc:  # noqa: BLE001 - 通道级隔离，单通道异常不影响其它页面
        return {"success": False, "error": f"{type(exc).__name__}: {exc}", "mode": "live"}
