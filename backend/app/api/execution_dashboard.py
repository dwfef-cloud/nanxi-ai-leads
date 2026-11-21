"""D26 管理面收尾 · 执行侧仪表盘（漏斗 + 合规护栏实时数据）

管理面「转化漏斗」「合规风险」两页原来只读南溪存量 leads / compliance 表，
执行侧（W1 采集筛选、W2 互动、D17 私信频控、D23 账号池、W3 养号）产生的
真实数据没有出口。本模块把这批数据按前端既有契约暴露出来：

    GET /api/dashboard/execution-funnel?days=30
        六级经营漏斗（与既有契约一致）+ 执行链路六段 + 今日快照 + 14 天趋势
    GET /api/dashboard/execution-compliance
        配额账本 / DM 官方频控 / 账号池状态机 / 编排器阻断 / 养号 / 审计

设计约束：
- 只读：绝不写任何业务表；缺表 / 缺库返回 0 + ready=false，不伪造数据
- 独立 router，不改南溪现有 api/router.py；挂载见 bridge_server.py
- 时间：执行侧表用 INTEGER epoch 秒，南溪存量表用 TEXT ISO，分别处理
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from app.db.init import get_db_path
from app.engagement.dm_guard import (
    DAILY_RECIPIENTS,
    HOURLY_RECIPIENTS,
    PER_RECIPIENT_LIMIT,
    SESSION_WINDOW_H,
    SESSION_WINDOW_LIMIT,
)
from app.execution.params import FILTER
from app.paths import DATA_DIR as _DATA_DIR

execution_dashboard_router = APIRouter(prefix="/api/dashboard", tags=["execution-dashboard"])

_CST = timezone(timedelta(hours=8))
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

# 南溪存量 leads 状态口径（历史数据两套命名并存，统计时合并）
_WECHAT_STATUSES = ("wechat_added", "deal_won", "won")
_DEAL_STATUSES = ("deal_won", "won")
_WECHAT_SQL = "(" + ",".join(f"'{s}'" for s in _WECHAT_STATUSES) + ")"
_DEAL_SQL = "(" + ",".join(f"'{s}'" for s in _DEAL_STATUSES) + ")"


# ═══════════════════════════════════════════════════════════
# 只读连接与容错查询
# ═══════════════════════════════════════════════════════════

def _execution_db() -> Path:
    return Path(os.getenv("EXECUTION_DB_PATH") or _DATA_DIR / "execution.db")


def _pool_db() -> Path:
    return Path(os.getenv("ACCOUNT_POOL_DB") or _DATA_DIR / "account_pool.db")


def _dm_db() -> Path:
    return Path(os.getenv("DM_GUARD_DB") or _DATA_DIR / "dm_guard.db")


def _ro_connect(path: Path) -> sqlite3.Connection | None:
    """只读连接。文件不存在直接返回 None（视为该数据源未接线）。"""
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _one(conn: sqlite3.Connection | None, sql: str, params: tuple = (), key: str = "cnt") -> int:
    if conn is None:
        return 0
    try:
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return 0
        value = row[key] if key in row.keys() else row[0]
        return int(value or 0)
    except sqlite3.Error:
        return 0


def _rows(conn: sqlite3.Connection | None, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    if conn is None:
        return []
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _has_table(conn: sqlite3.Connection | None, name: str) -> bool:
    return bool(_rows(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _cst_date(ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    return datetime.fromtimestamp(ts, _CST).strftime("%Y-%m-%d")


def _window_start_epoch(days: int) -> int:
    now = datetime.now(_CST)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def _pct(a: int, b: int) -> float:
    return round(b / a * 100, 1) if a else 0.0


def _ratio(numerator: float, denominator: int) -> float:
    return round(numerator / denominator, 1) if denominator else 0.0


# ═══════════════════════════════════════════════════════════
# 1. 执行侧漏斗
# ═══════════════════════════════════════════════════════════

@execution_dashboard_router.get("/execution-funnel")
def execution_funnel(
    days: int = Query(30, ge=1, le=90, description="统计窗口天数"),
) -> dict[str, Any]:
    """经营漏斗（存量口径）+ 执行链路（执行侧口径），全部为库内真实计数。"""
    start = _window_start_epoch(days)
    exec_conn = _ro_connect(_execution_db())
    main_conn = _ro_connect(get_db_path())
    dm_conn = _ro_connect(_dm_db())

    # ── 执行侧：采集 → 筛选 → 互动 → 私信 ──
    has_cand = _has_table(exec_conn, "lead_candidates")
    collected = _one(exec_conn, "SELECT COUNT(*) AS cnt FROM lead_candidates WHERE created_at >= ?", (start,))
    qualified = _one(
        exec_conn,
        "SELECT COUNT(*) AS cnt FROM lead_candidates WHERE created_at >= ? AND intent_score >= ?",
        (start, FILTER.intent_threshold),
    )
    commented = _one(
        exec_conn,
        "SELECT COUNT(*) AS cnt FROM comment_send_log WHERE created_at >= ? AND status = 'sent_ok'",
        (start,),
    )
    has_comment_log = _has_table(exec_conn, "comment_send_log")

    # 私信：执行侧频控账本 + 南溪存量发送结果合并（两条真实链路）
    guard_dm = _one(dm_conn, "SELECT COUNT(*) AS cnt FROM dm_send_log WHERE sent_at >= ?", (start,))
    legacy_dm_ok = _one(
        main_conn,
        "SELECT COUNT(*) AS cnt FROM dm_send_results WHERE status = 'success' AND created_at >= ?",
        (_iso_start(days),),
    )
    dm_sent = guard_dm + legacy_dm_ok

    replied = _one(
        main_conn,
        "SELECT COUNT(*) AS cnt FROM messages WHERE direction = 'in' AND created_at >= ?",
        (_iso_start(days),),
    )
    wechat = _one(
        main_conn,
        f"SELECT COUNT(*) AS cnt FROM leads WHERE status IN {_WECHAT_SQL} AND created_at >= ?",
        (_iso_start(days),),
    )
    deals = _one(
        main_conn,
        f"SELECT COUNT(*) AS cnt FROM leads WHERE status IN {_DEAL_SQL} AND created_at >= ?",
        (_iso_start(days),),
    )
    leads_in_window = _one(main_conn, "SELECT COUNT(*) AS cnt FROM leads WHERE created_at >= ?", (_iso_start(days),))

    # ── 经营漏斗：沿用前端既有六级契约 ──
    stages = [
        {"key": "exposure", "label": "评论区曝光", "value": collected,
         "note": "执行侧采集到的评论条数"},
        {"key": "collected", "label": "线索入库", "value": leads_in_window,
         "note": "南溪线索池新增"},
        {"key": "outreach", "label": "私信触达", "value": dm_sent,
         "note": "频控账本 + 存量发送结果"},
        {"key": "replied", "label": "私信回复", "value": replied,
         "note": "会话入站消息"},
        {"key": "wechat", "label": "加企微 ★", "value": wechat,
         "note": "wechat_added / deal_won"},
        {"key": "deal", "label": "成交", "value": deals, "note": "deal_won"},
    ]

    # ── 执行链路：新漏斗语义 ──
    exec_stages = [
        {"key": "collect", "label": "采集评论", "value": collected, "note": "lead_candidates"},
        {"key": "filter", "label": "意向入池", "value": qualified,
         "note": f"intent_score ≥ {FILTER.intent_threshold} 分"},
        {"key": "engage", "label": "评论互动", "value": commented, "note": "comment_send_log sent_ok"},
        {"key": "dm", "label": "私信触达", "value": dm_sent, "note": "过频控后真实发送"},
        {"key": "reply", "label": "私信回复", "value": replied, "note": "会话入站"},
        {"key": "convert", "label": "加微/成交", "value": wechat, "note": "leads 状态回写"},
    ]

    # ── 今日快照 ──
    today_start = int(datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today = {
        "collected": _one(exec_conn, "SELECT COUNT(*) AS cnt FROM lead_candidates WHERE created_at >= ?", (today_start,)),
        "qualified": _one(
            exec_conn,
            "SELECT COUNT(*) AS cnt FROM lead_candidates WHERE created_at >= ? AND intent_score >= ?",
            (today_start, FILTER.intent_threshold),
        ),
        "commented": _one(
            exec_conn,
            "SELECT COUNT(*) AS cnt FROM comment_send_log WHERE created_at >= ? AND status = 'sent_ok'",
            (today_start,),
        ),
        "dm": _one(dm_conn, "SELECT COUNT(*) AS cnt FROM dm_send_log WHERE sent_at >= ?", (today_start,)),
        "replied": _one(
            main_conn,
            "SELECT COUNT(*) AS cnt FROM messages WHERE direction = 'in' AND created_at >= ?",
            (datetime.now(_CST).strftime("%Y-%m-%d"),),
        ),
    }

    # ── 14 天趋势（采集 / 评论 / 私信） ──
    trend = _build_trend(exec_conn, dm_conn, days=14)

    # ── 成本：只认 ad_spend 真实录入 ──
    spend = _spend_in_window(main_conn, days)
    deal_amount = _deal_amount_in_window(main_conn, days)
    cost = {
        "totalSpend": round(spend, 2),
        "perLead": _ratio(spend, leads_in_window),
        "perWechatAdd": _ratio(spend, wechat),
        "perDeal": _ratio(spend, deals),
        "avgDealAmount": round(deal_amount / deals, 2) if deals else 0,
        "basis": "ad_spend 手工录入" if spend else "未录入投放成本，成本类指标按 0 展示",
    }

    return {
        "generated_at": datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S"),
        "window": f"近 {days} 天",
        "window_days": days,
        "stages": stages,
        "exec_stages": exec_stages,
        "today": today,
        "trend": trend,
        "cost": cost,
        "dealAmount": round(deal_amount, 2),
        "threshold": FILTER.intent_threshold,
        "sources": {
            "execution_db": {"path": str(_execution_db()), "ready": exec_conn is not None,
                             "has_candidates": has_cand, "has_comment_log": has_comment_log},
            "main_db": {"path": str(get_db_path()), "ready": main_conn is not None},
            "dm_guard_db": {"path": str(_dm_db()), "ready": dm_conn is not None},
        },
    }


def _iso_start(days: int) -> str:
    """南溪存量表用 ISO 文本时间，取窗口起点（含当天）。"""
    start = (datetime.now(_CST) - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start.strftime("%Y-%m-%dT%H:%M:%S")


def _build_trend(exec_conn, dm_conn, days: int = 14) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = {}
    today = datetime.now(_CST).date()
    for i in range(days - 1, -1, -1):
        key = (today - timedelta(days=i)).strftime("%m-%d")
        buckets[key] = {"date": key, "collected": 0, "commented": 0, "dm": 0}

    for row in _rows(exec_conn,
                     "SELECT created_at FROM lead_candidates WHERE created_at >= ?",
                     (_window_start_epoch(days),)):
        key = datetime.fromtimestamp(int(row["created_at"]), _CST).strftime("%m-%d")
        if key in buckets:
            buckets[key]["collected"] += 1
    for row in _rows(exec_conn,
                     "SELECT created_at FROM comment_send_log WHERE created_at >= ? AND status = 'sent_ok'",
                     (_window_start_epoch(days),)):
        key = datetime.fromtimestamp(int(row["created_at"]), _CST).strftime("%m-%d")
        if key in buckets:
            buckets[key]["commented"] += 1
    for row in _rows(dm_conn, "SELECT sent_at FROM dm_send_log WHERE sent_at >= ?",
                     (_window_start_epoch(days),)):
        key = datetime.fromtimestamp(int(row["sent_at"]), _CST).strftime("%m-%d")
        if key in buckets:
            buckets[key]["dm"] += 1
    return list(buckets.values())


def _spend_in_window(conn, days: int) -> float:
    total = 0.0
    for row in _rows(conn, "SELECT amount FROM ad_spend WHERE date >= ?",
                     ((datetime.now(_CST) - timedelta(days=days - 1)).strftime("%Y-%m-%d"),)):
        total += float(row["amount"] or 0)
    return total


def _deal_amount_in_window(conn, days: int) -> float:
    total = 0.0
    for row in _rows(
        conn,
        f"SELECT deal_amount FROM leads WHERE status IN {_DEAL_SQL} AND created_at >= ?",
        (_iso_start(days),),
    ):
        total += float(row["deal_amount"] or 0)
    return total


# ═══════════════════════════════════════════════════════════
# 2. 执行侧合规护栏
# ═══════════════════════════════════════════════════════════

@execution_dashboard_router.get("/execution-compliance")
def execution_compliance(
    days: int = Query(30, ge=1, le=90, description="统计窗口天数"),
) -> dict[str, Any]:
    """线上护栏状态：互动配额 / DM 官方频控 / 账号池 / 编排器阻断 / 养号。"""
    start = _window_start_epoch(days)
    exec_conn = _ro_connect(_execution_db())
    pool_conn = _ro_connect(_pool_db())
    dm_conn = _ro_connect(_dm_db())
    main_conn = _ro_connect(get_db_path())
    today_str = _cst_date()

    # ── 互动配额账本（account_quotas：按账号/日/指标） ──
    quota_rows = _rows(
        exec_conn,
        "SELECT metric, SUM(snapshot) AS used, COUNT(DISTINCT account_id) AS accounts, COUNT(*) AS rows"
        " FROM account_quotas WHERE day = ? GROUP BY metric",
        (today_str,),
    )
    quota_by_metric = {str(r["metric"]): int(r["used"] or 0) for r in quota_rows}
    quota = {
        "day": today_str,
        "read": quota_by_metric.get("read", 0),
        "like": quota_by_metric.get("like", 0),
        "comment": quota_by_metric.get("comment", 0),
        "dm": quota_by_metric.get("dm", 0),
        "accounts": max([int(r["accounts"] or 0) for r in quota_rows], default=0),
        "tracked_rows": sum(int(r["rows"] or 0) for r in quota_rows),
        "ready": _has_table(exec_conn, "account_quotas"),
    }

    # ── DM 官方频控（阈值写死，不允许配置放开） ──
    hour_ago = int(time.time()) - 3600
    day_start = int(datetime.now(_CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    dm_ready = dm_conn is not None and _has_table(dm_conn, "dm_send_log")
    dm_guard = {
        "ready": dm_ready,
        "limits": {
            "per_recipient": PER_RECIPIENT_LIMIT,
            "hourly_recipients": HOURLY_RECIPIENTS,
            "daily_recipients": DAILY_RECIPIENTS,
            "session_window_h": SESSION_WINDOW_H,
            "session_window_limit": SESSION_WINDOW_LIMIT,
        },
        "today_sent": _one(dm_conn, "SELECT COUNT(*) AS cnt FROM dm_send_log WHERE sent_at >= ?", (day_start,)),
        "hour_sent": _one(dm_conn, "SELECT COUNT(*) AS cnt FROM dm_send_log WHERE sent_at >= ?", (hour_ago,)),
        "today_recipients": _one(
            dm_conn, "SELECT COUNT(DISTINCT recipient) AS cnt FROM dm_send_log WHERE sent_at >= ?", (day_start,)),
        "hour_recipients": _one(
            dm_conn, "SELECT COUNT(DISTINCT recipient) AS cnt FROM dm_send_log WHERE sent_at >= ?", (hour_ago,)),
        "note": "" if dm_ready else "频控闸门未产生发送记录（接线后自动出现计数）",
    }

    # ── 账号池状态机 ──
    state_rows = _rows(pool_conn, "SELECT state, COUNT(*) AS cnt FROM account_pool_states GROUP BY state")
    by_state = {str(r["state"]): int(r["cnt"] or 0) for r in state_rows}
    pool = {
        "ready": pool_conn is not None and _has_table(pool_conn, "account_pool_states"),
        "total": sum(by_state.values()),
        "by_state": by_state,
        "risk_events_day": _one(
            pool_conn, "SELECT COUNT(*) AS cnt FROM account_risk_events WHERE happened_at >= ?",
            (int(time.time()) - 86400,)),
        "risk_events_window": _one(
            pool_conn, "SELECT COUNT(*) AS cnt FROM account_risk_events WHERE happened_at >= ?", (start,)),
    }

    # ── 编排器运行与合规阻断 ──
    run_rows = _rows(
        exec_conn,
        "SELECT id, stage, stats, started_at, finished_at FROM pipeline_runs"
        " WHERE started_at >= ? ORDER BY started_at DESC LIMIT 5",
        (start,),
    )
    blocked_engage = blocked_dm = 0
    for row in run_rows:
        try:
            stats = json.loads(row["stats"] or "{}")
        except (TypeError, ValueError):
            stats = {}
        if str(stats.get("engage_blocked", "")).startswith("no_handler"):
            blocked_engage += 1
        if str(stats.get("dm_blocked", "")).startswith("no_handler"):
            blocked_dm += 1
    last_run = None
    if run_rows:
        row = run_rows[0]
        last_run = {
            "run_id": row["id"],
            "stage": row["stage"],
            "started_at": datetime.fromtimestamp(int(row["started_at"]), _CST).strftime("%m-%d %H:%M"),
            "finished_at": (datetime.fromtimestamp(int(row["finished_at"]), _CST).strftime("%m-%d %H:%M")
                            if row["finished_at"] else None),
        }
    pipeline = {
        "ready": _has_table(exec_conn, "pipeline_runs"),
        "runs_in_window": _one(exec_conn, "SELECT COUNT(*) AS cnt FROM pipeline_runs WHERE started_at >= ?", (start,)),
        "checkpoints": _one(exec_conn, "SELECT COUNT(*) AS cnt FROM pipeline_progress WHERE done_at >= ?", (start,)),
        "blocked_engage": blocked_engage,
        "blocked_dm": blocked_dm,
        "last_run": last_run,
        "note": "互动/私信执行器未过 TEST-CHECKLIST 时闸门记 blocked，不触发真实写动作",
    }

    # ── 养号（W3） ──
    nurture = {
        "ready": _has_table(exec_conn, "nurture_sessions"),
        "sessions": _one(exec_conn, "SELECT COUNT(*) AS cnt FROM nurture_sessions WHERE started_at >= ?", (start,)),
        "videos_watched": _one(exec_conn, "SELECT SUM(videos_watched) AS cnt FROM nurture_sessions WHERE started_at >= ?", (start,)),
        "likes": _one(exec_conn, "SELECT SUM(likes) AS cnt FROM nurture_sessions WHERE started_at >= ?", (start,)),
        "favorites": _one(exec_conn, "SELECT SUM(favorites) AS cnt FROM nurture_sessions WHERE started_at >= ?", (start,)),
        "comments_browsed": _one(exec_conn, "SELECT SUM(comments_browsed) AS cnt FROM nurture_sessions WHERE started_at >= ?", (start,)),
    }

    # ── 评论发送失败分布（写入安全三态） ──
    send_rows = _rows(
        exec_conn,
        "SELECT status, COUNT(*) AS cnt FROM comment_send_log WHERE created_at >= ? GROUP BY status",
        (start,),
    )
    send_status = {str(r["status"]): int(r["cnt"] or 0) for r in send_rows}

    # ── 审计（南溪存量） ──
    audit_recent = []
    for row in _rows(
        main_conn,
        "SELECT type, level, text, time, created_at FROM compliance_events"
        " ORDER BY created_at DESC LIMIT 10",
    ):
        audit_recent.append({
            "type": row["type"],
            "level": row["level"],
            "text": row["text"],
            "time": row["time"] or str(row["created_at"])[5:16].replace("T", " "),
        })

    return {
        "generated_at": datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": days,
        "quota": quota,
        "dm_guard": dm_guard,
        "pool": pool,
        "pipeline": pipeline,
        "nurture": nurture,
        "send_status": send_status,
        "audit": {
            "events": _one(main_conn, "SELECT COUNT(*) AS cnt FROM compliance_events"),
            "recent": audit_recent,
        },
    }
