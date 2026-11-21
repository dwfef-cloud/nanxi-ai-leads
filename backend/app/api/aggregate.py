"""M0 聚合视图数据源 —— 账号数据 / 消息聚合 / 创作聚合 / 空态通道。

设计原则（与 DESIGN-DOC.md §1.3 / §0.5 一致）：
  1. 只读南溪本地库已存在的真实记录，不虚构平台侧指标、不发起平台请求。
  2. 无采集源的字段（粉丝数 / 作品数 / 点赞收藏 / 关注数）保持 0，
     并在 note 里逐项写明口径，避免把 0 误读成「平台真实值为 0」。
  3. 本地库没有账号归属关系的记录，不强行摊派到 4 个账号；
     只在能按名称/ID 精确匹配时才计入账号级，其余归入系统汇总。
  4. 出口不带任何平台 uid 级字段（由 ipc_bridge.assert_public_safe 二次校验）。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.paths import DATA_DIR as _DATA_DIR
BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB = _DATA_DIR / "lead_system.db"
DB_PATH = Path(os.getenv("LEADS_DB_PATH", str(DEFAULT_DB)))

NOTE_NO_PLATFORM_METRICS = (
    "粉丝/关注/作品/获赞来自创作者中心每日快照（打开创作聚合页时自动采集，"
    "未采集过的账号为 0）；评论、回复、私信、跟进为本地库真实事件数。"
)
NOTE_NO_ACCOUNT_OWNER = (
    "本地库未记录线索/作品的账号归属，账号级仅统计能按名称精确匹配的回复与私信；"
    "完整数据见系统汇总。"
)
NOTE_CHAT_LOCAL = "展示的是本地会话与消息记录，不是平台实时私信，也不触发任何平台请求。"


def _connect() -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, ValueError):
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    try:
        with closing(_connect()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _scalar(sql: str, params: tuple = (), default: int = 0) -> int:
    rows = _rows(sql, params)
    if not rows:
        return default
    value = next(iter(rows[0].values()))
    return int(value) if value is not None else default


def _exists(name: str) -> bool:
    return _scalar(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ) > 0


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _fmt(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16].replace("T", " ")


def _epoch_ms_now() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _epoch_ms(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _clip(text: Any, width: int = 90) -> str:
    value = str(text or "").replace("\n", " ").strip()
    return value if len(value) <= width else value[: width - 1] + "…"


def _key(text: Any) -> str:
    """系统内部记录的安全展示键（不可逆短哈希），不透出原始 ID。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12]


def _accounts() -> list[dict]:
    if not _exists("accounts"):
        return []
    return [{
        "accountId": str(r["id"]),
        "name": str(r.get("name") or ""),
        "nickname": str(r.get("nickname") or ""),
        "status": "online" if str(r.get("status")) == "active" else "offline",
    } for r in _rows(
        "SELECT id, name, nickname, status FROM accounts ORDER BY created_at"
    )]


# --------------------------------------------------------------------------
# 账号数据看板（AccountData 页：get-account-data-dashboard）
# --------------------------------------------------------------------------
def _date_axis() -> list[str]:
    """最近 7 个「有真实事件」的日期；保证包含今天，升序返回（前端热力表从左到右）。"""
    parts = []
    for table in ("behavior_events", "comment_tasks", "dm_send_results", "messages"):
        if _exists(table):
            parts.append(f"SELECT substr(created_at,1,10) AS d FROM {table} WHERE created_at IS NOT NULL")
    dates: list[str] = []
    if parts:
        sql = " UNION ".join(parts) + " ORDER BY d DESC LIMIT 7"
        dates = [str(r["d"]) for r in _rows(sql) if r.get("d")]
    if _today() not in dates:
        dates.insert(0, _today())
    return sorted(set(dates))[-7:]


def _system_daily() -> dict[str, dict[str, int]]:
    """系统级按天真实事件（用于汇总与 note，不摊派到账号）。"""
    out: dict[str, dict[str, int]] = {}
    if _exists("behavior_events"):
        for r in _rows(
            "SELECT substr(created_at,1,10) AS d, "
            "SUM(CASE WHEN event_type='comment' THEN 1 ELSE 0 END) AS comments, "
            "SUM(CASE WHEN event_type IN ('reply','comment_replied','comment_reply') THEN 1 ELSE 0 END) AS replies, "
            "SUM(CASE WHEN event_type='follow_up' THEN 1 ELSE 0 END) AS follow_ups, "
            "SUM(CASE WHEN event_type='comment_sent' THEN 1 ELSE 0 END) AS comments_sent "
            "FROM behavior_events GROUP BY d"
        ):
            out.setdefault(_day(r["d"]), {}).update({
                "comments": int(r["comments"] or 0),
                "replies": int(r["replies"] or 0),
                "followUps": int(r["follow_ups"] or 0),
                "commentsSent": int(r["comments_sent"] or 0),
            })
    if _exists("comment_tasks"):
        for r in _rows(
            "SELECT substr(COALESCE(replied_at, created_at),1,10) AS d, COUNT(*) AS n "
            "FROM comment_tasks WHERE status IN ('replied','user_replied') GROUP BY d"
        ):
            out.setdefault(_day(r["d"]), {})["taskReplies"] = int(r["n"] or 0)
    if _exists("dm_send_results"):
        for r in _rows(
            "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n FROM dm_send_results "
            "WHERE status='success' GROUP BY d"
        ):
            out.setdefault(_day(r["d"]), {})["directMessages"] = int(r["n"] or 0)
    if _exists("messages"):
        for r in _rows(
            "SELECT substr(created_at,1,10) AS d, "
            "SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) AS inbound, "
            "SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS outbound "
            "FROM messages GROUP BY d"
        ):
            cell = out.setdefault(_day(r["d"]), {})
            cell["inbound"] = int(r["inbound"] or 0)
            cell["outbound"] = int(r["outbound"] or 0)
    return out


def _account_daily_replies() -> dict[str, dict[str, int]]:
    """comment_tasks.account 是账号名，可按名称精确归属。"""
    if not _exists("comment_tasks"):
        return {}
    out: dict[str, dict[str, int]] = {}
    for r in _rows(
        "SELECT account, substr(COALESCE(replied_at, created_at),1,10) AS d, COUNT(*) AS n "
        "FROM comment_tasks WHERE status IN ('replied','user_replied') "
        "AND account IS NOT NULL AND account<>'' GROUP BY account, d"
    ):
        out.setdefault(str(r["account"]), {})[_day(r["d"])] = int(r["n"] or 0)
    return out


def _account_daily_dm() -> dict[str, dict[str, int]]:
    """dm_send_results.account_id 可精确匹配账号 id；conversations.account_name 可匹配账号名。"""
    out: dict[str, dict[str, int]] = {}
    if _exists("dm_send_results"):
        for r in _rows(
            "SELECT account_id, substr(created_at,1,10) AS d, COUNT(*) AS n "
            "FROM dm_send_results WHERE status='success' GROUP BY account_id, d"
        ):
            out.setdefault(str(r["account_id"]), {})[_day(r["d"])] = int(r["n"] or 0)
    if _exists("conversations") and _exists("messages"):
        for r in _rows(
            "SELECT c.account_name, substr(m.created_at,1,10) AS d, COUNT(*) AS n "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id "
            "WHERE m.direction='out' GROUP BY c.account_name, d"
        ):
            cell = out.setdefault(str(r["account_name"]), {})
            cell[_day(r["d"])] = cell.get(_day(r["d"]), 0) + int(r["n"] or 0)
    return out


def _day_entry(*, replies: int = 0, messages: int = 0, snapshot_day: str = "",
                stats: dict | None = None) -> dict:
    """一天的数据格：互动数来自本地真实事件，粉丝/关注/作品/获赞来自每日快照。"""
    st = stats or {}
    return {
        "followers": int(st.get("followers") or 0),
        "following": int(st.get("following") or 0),
        "works": int(st.get("works") or 0),
        "snapshotAt": f"{snapshot_day} 统计" if snapshot_day else _today(),
        "likes": int(st.get("likes") or 0),
        "collections": 0, "videoShares": 0,
        "replies": int(replies), "profileComments": 0, "videoComments": 0,
        "follows": 0, "messages": int(messages),
    }


def _profile_snapshots() -> dict[str, list[tuple[str, dict]]]:
    """account_stats_daily 每日快照（对标阿里山「每日自动统计」）。

    返回 accountId → [(day, {following,followers,works,likes}), ...] 升序。
    表不存在（老库未升级）时返回空，看板自然回退为 0。
    """
    if not _exists("account_stats_daily"):
        return {}
    out: dict[str, list[tuple[str, dict]]] = {}
    for r in _rows(
        "SELECT account_id, day, following, followers, works, likes "
        "FROM account_stats_daily ORDER BY day"
    ):
        out.setdefault(str(r["account_id"]), []).append((
            str(r["day"]),
            {"following": r["following"], "followers": r["followers"],
             "works": r["works"], "likes": r["likes"]},
        ))
    return out


def _stats_for_day(snaps: list[tuple[str, dict]], day: str) -> dict:
    """取 ≤ day 的最近一次快照（无快照日沿用上一次值，与原版趋势展示一致）。"""
    best: dict = {}
    for snap_day, st in snaps:
        if snap_day <= day:
            best = st
        else:
            break
    return best


def _account_data_time() -> str:
    """账号数据每日自动统计时刻（用户在页面上保存过的值）。"""
    from app import schedules

    try:
        return schedules.get_account_data_time(DB_PATH)
    except sqlite3.Error:
        return "00:00"


def _last_collect_ms() -> int:
    """上次统计时间 = account_stats_daily 最近一次写入时间（真实值）。

    历史实现返回 datetime.now()——页面永远显示「刚刚统计过」，
    与实际是否采过数完全无关。没有快照就返回 0，前端显示「暂无记录」。
    """
    if not _exists("account_stats_daily"):
        return 0
    row = None
    with closing(sqlite3.connect(str(DB_PATH))) as con:
        row = con.execute(
            "SELECT MAX(updated_at) FROM account_stats_daily").fetchone()
    text = str((row or [None])[0] or "").strip()
    if not text:
        return 0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def account_dashboard(_: dict | None = None) -> dict:
    accounts = _accounts()
    days = _date_axis()
    system = _system_daily()
    replies_by_name = _account_daily_replies()
    dm_by_key = _account_daily_dm()
    snapshots = _profile_snapshots()

    def for_account(acc: dict, day: str) -> dict:
        names = {acc["name"], acc["nickname"], acc["accountId"]}
        replies = sum(replies_by_name.get(n, {}).get(day, 0) for n in names if n)
        messages = sum(dm_by_key.get(n, {}).get(day, 0) for n in names if n)
        stats = _stats_for_day(snapshots.get(acc["accountId"], []), day)
        return _day_entry(replies=replies, messages=messages, snapshot_day=day, stats=stats)

    latest = {a["accountId"]: for_account(a, days[-1]) for a in accounts}
    baselines = {a["accountId"]: for_account(a, days[0]) for a in accounts}
    per_account = [{
        "accountId": a["accountId"],
        "days": {d: for_account(a, d) for d in days},
    } for a in accounts]

    totals = {
        key: sum(system.get(d, {}).get(key, 0) for d in days)
        for key in ("comments", "replies", "followUps", "commentsSent",
                    "taskReplies", "directMessages", "inbound", "outbound")
    }
    return {
        "success": True,
        "scheduleTime": _account_data_time(),
        "lastCollectAt": _last_collect_ms(),
        "collecting": False,
        "days": days,
        "errors": {},
        "baselines": baselines,
        "latest": latest,
        "accounts": per_account,
        "totals": totals,
        "systemDaily": system,
        "note": NOTE_NO_PLATFORM_METRICS + " " + NOTE_NO_ACCOUNT_OWNER,
    }


# --------------------------------------------------------------------------
# 创作聚合（CreatorAggregate 页离线数据视图）
# --------------------------------------------------------------------------
def creator_aggregate(args: dict | None = None) -> dict:
    args = args or {}
    wanted = str(args.get("accountName") or args.get("name") or "").strip()
    accounts = _accounts()

    videos: list[dict] = []
    if _exists("leads"):
        videos = [{
            "title": _clip(r["video"], 110),
            "leads": int(r["n"] or 0),
            "lastAt": _fmt(r["last_at"]),
            "keyword": str(r["kw"] or ""),
        } for r in _rows(
            "SELECT video, COUNT(*) AS n, MAX(created_at) AS last_at, "
            "MAX(source_keyword) AS kw FROM leads "
            "WHERE video IS NOT NULL AND video<>'' "
            "GROUP BY video ORDER BY n DESC, last_at DESC LIMIT 12"
        )]

    tasks: list[dict] = []
    if _exists("crawl_tasks"):
        tasks = [{
            "name": _clip(r.get("name") or f"{r.get('crawl_type') or '任务'} · {r.get('keyword') or ''}", 60),
            "type": str(r.get("crawl_type") or ""),
            "status": str(r.get("status") or ""),
            "collected": int(r.get("collected_count") or 0),
            "imported": int(r.get("imported_count") or 0),
            "createdAt": _fmt(r.get("created_at")),
        } for r in _rows(
            "SELECT name, crawl_type, keyword, status, collected_count, imported_count, created_at "
            "FROM crawl_tasks ORDER BY created_at DESC LIMIT 10"
        )]

    replies_by_name = _account_daily_replies()
    per_account = []
    for acc in accounts:
        names = {acc["name"], acc["nickname"]}
        reply_total = sum(sum(day_map.values()) for n, day_map in replies_by_name.items() if n in names)
        per_account.append({
            "accountId": acc["accountId"], "name": acc["name"] or acc["nickname"],
            "status": acc["status"], "replies": reply_total,
            "videos": 0, "leads": 0,
            "note": "本地未记录该账号的作品/线索归属",
        })

    timeline: list[dict] = []
    if _exists("leads"):
        timeline = [{
            "date": _day(r["d"]), "leads": int(r["n"] or 0),
        } for r in _rows(
            "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n FROM leads "
            "GROUP BY d ORDER BY d DESC LIMIT 7"
        )][::-1]

    totals = {
        "videos": len(videos),
        "leads": _scalar("SELECT COUNT(*) FROM leads") if _exists("leads") else 0,
        "comments": _scalar(
            "SELECT COUNT(*) FROM behavior_events WHERE event_type='comment'"
        ) if _exists("behavior_events") else 0,
        "replies": sum(a["replies"] for a in per_account),
        "tasks": _scalar("SELECT COUNT(*) FROM crawl_tasks") if _exists("crawl_tasks") else 0,
    }
    return {
        "success": True,
        "accountName": wanted,
        "totals": totals,
        "accounts": per_account,
        "videos": videos,
        "tasks": tasks,
        "timeline": timeline,
        "note": "创作聚合展示本地已记录的内容线索与采集任务；作品数/播放量/粉丝数无平台采集源，不展示。"
                "浏览器环境不内嵌真实创作者页面，也不触发任何平台请求。",
    }


# --------------------------------------------------------------------------
# 消息聚合（MessageCenter 页离线数据视图）
# --------------------------------------------------------------------------
def message_aggregate(args: dict | None = None) -> dict:
    args = args or {}
    wanted = str(args.get("accountName") or args.get("name") or "").strip()
    thread_key = str(args.get("threadKey") or "").strip()
    try:
        limit = min(200, max(10, int(args.get("limit") or 60)))
    except (TypeError, ValueError):
        limit = 60

    if not (_exists("conversations") and _exists("messages")):
        return {
            "success": True, "stats": {}, "threads": [], "messages": [],
            "accounts": [], "note": NOTE_CHAT_LOCAL + " 本地暂无会话表。",
        }

    account_clause, msg_clause = "", ""
    params: tuple = ()
    if wanted:
        account_clause = "WHERE c.account_name=?"
        params = (wanted,)

    threads = [{
        "threadKey": _key(r["id"]),
        "contact": _clip(r.get("nickname") or "未命名联系人", 24),
        "accountName": str(r.get("account_name") or ""),
        "status": str(r.get("status") or ""),
        "lastAt": _fmt(r.get("last_message_at")),
        "lastMessage": _clip(r.get("last_content"), 70),
        "messageCount": int(r.get("message_count") or 0),
        "unread": int(r.get("unread") or 0),
    } for r in _rows(
        "SELECT c.id, c.account_name, c.status, c.last_message_at, l.nickname, "
        "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count, "
        "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id "
        "  AND m.direction='in' AND m.is_read=0) AS unread, "
        "(SELECT m2.content FROM messages m2 WHERE m2.conversation_id=c.id "
        "  ORDER BY m2.created_at DESC LIMIT 1) AS last_content "
        "FROM conversations c LEFT JOIN leads l ON l.id=c.lead_id "
        + account_clause +
        " ORDER BY c.last_message_at DESC LIMIT 40", params
    )]

    # 会话详情：threadKey 只用于本地短哈希匹配，不透出原始 ID
    msg_where, msg_params = [], []
    if wanted:
        msg_where.append("c.account_name=?")
        msg_params.append(wanted)
    clause = " WHERE " + " AND ".join(msg_where) if msg_where else ""
    # conversation_id 只用于生成不可逆短哈希，绝不出现在出口里
    raw_messages = _rows(
        "SELECT m.conversation_id AS _cid, m.sender, m.direction, m.content, "
        "m.is_ai_generated, m.is_read, m.created_at, "
        "c.account_name, l.nickname FROM messages m "
        "JOIN conversations c ON c.id=m.conversation_id "
        "LEFT JOIN leads l ON l.id=c.lead_id" + clause +
        " ORDER BY m.created_at DESC LIMIT ?", tuple(msg_params) + (limit * 3,)
    )
    messages = [{
        "threadKey": _key(r.get("_cid")),
        "sender": str(r.get("sender") or ""),
        "direction": str(r.get("direction") or ""),
        "content": _clip(r.get("content"), 240),
        "isAi": bool(r.get("is_ai_generated")),
        "isRead": bool(r.get("is_read")),
        "time": _fmt(r.get("created_at")),
        "accountName": str(r.get("account_name") or ""),
        "contact": _clip(r.get("nickname") or "未命名联系人", 24),
    } for r in raw_messages]
    # threadKey 过滤在本地完成（SQL 不暴露原始 ID）
    if thread_key:
        messages = [m for m in messages if m["threadKey"] == thread_key][:limit]
    else:
        messages = messages[:limit]

    accounts = [{
        "accountName": str(r["account_name"] or ""),
        "threads": int(r["n"] or 0),
        "messages": int(r["m"] or 0),
        "unread": int(r["u"] or 0),
    } for r in _rows(
        "SELECT c.account_name, COUNT(DISTINCT c.id) AS n, COUNT(m.id) AS m, "
        "SUM(CASE WHEN m.direction='in' AND m.is_read=0 THEN 1 ELSE 0 END) AS u "
        "FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id "
        "GROUP BY c.account_name ORDER BY n DESC"
    )]

    stats = {
        "threads": _scalar("SELECT COUNT(*) FROM conversations"),
        "messages": _scalar("SELECT COUNT(*) FROM messages"),
        "inbound": _scalar("SELECT COUNT(*) FROM messages WHERE direction='in'"),
        "outbound": _scalar("SELECT COUNT(*) FROM messages WHERE direction='out'"),
        "unread": _scalar("SELECT COUNT(*) FROM messages WHERE direction='in' AND is_read=0"),
        "today": _scalar(
            "SELECT COUNT(*) FROM messages WHERE substr(created_at,1,10)=?", (_today(),)
        ),
    }
    return {
        "success": True,
        "accountName": wanted,
        "stats": stats,
        "threads": threads,
        "messages": messages,
        "accounts": accounts,
        "note": NOTE_CHAT_LOCAL + (" 账号名用于匹配本地会话归属，不等于平台账号登录态。" if wanted else ""),
    }


def chat_unread_state(_: dict | None = None) -> dict:
    if not (_exists("conversations") and _exists("messages")):
        return {"success": True, "unread": {}, "total": 0}
    # 与 message_aggregate.stats.unread 同口径：孤儿会话归入「未归属」
    rows = _rows(
        "SELECT CASE WHEN c.account_name IS NULL OR c.account_name='' "
        "THEN '未归属' ELSE c.account_name END AS account_name, COUNT(*) AS n "
        "FROM messages m LEFT JOIN conversations c ON c.id=m.conversation_id "
        "WHERE m.direction='in' AND m.is_read=0 GROUP BY 1"
    )
    unread = {str(r["account_name"] or "未归属"): int(r["n"] or 0) for r in rows}
    total = sum(unread.values())
    return {
        "success": True, "unread": unread, "total": total,
        "totalUnread": total, "note": NOTE_CHAT_LOCAL,
    }


# --------------------------------------------------------------------------
# 原先的中性空态通道：有真实数据源就接上，没有的保持空并写明口径
# --------------------------------------------------------------------------
def monitor_live_views(_: dict | None = None) -> dict:
    if not _exists("crawl_tasks"):
        return {"success": True, "views": [], "note": "本地无采集任务记录"}
    views = [{
        "taskKey": _key(r["id"]),
        "name": _clip(r.get("name") or f"{r.get('crawl_type')} · {r.get('keyword') or ''}", 60),
        "status": str(r.get("status") or ""),
        "collected": int(r.get("collected_count") or 0),
        "createdAt": _fmt(r.get("created_at")),
    } for r in _rows(
        "SELECT id, name, crawl_type, keyword, status, collected_count, created_at "
        "FROM crawl_tasks ORDER BY created_at DESC LIMIT 20"
    )]
    return {"success": True, "views": views, "note": "本地采集任务状态，非实时平台画面"}


def monitor_task_matches(args: dict | None = None) -> dict:
    """监控命中记录 = 真实带评论文本的线索。

    以前对空评论的线索用「[关键词] 无评论文本」补位，页面上看起来像抓到了评论，
    实际什么都没抓到。命中记录的定义本来就是「哪条评论命中了关键词」，
    没有评论就不该出现在这张表里——改为一律过滤，不用占位符凑数。
    """
    if not _exists("leads"):
        return {"success": True, "items": [], "total": 0}
    args = args if isinstance(args, dict) else {}
    keyword = str(args.get("keyword") or "").strip()
    task_id = str(args.get("taskId") or "").strip()
    where = "WHERE COALESCE(comment,'')<>''"
    params: tuple = ()
    if task_id:
        where += " AND source_task_id=? AND source='monitor_comment'"
        params += (task_id,)
    if keyword:
        where += " AND comment LIKE ?"
        params += (f"%{keyword}%",)
    rows = _rows(
        "SELECT id, comment, source_keyword, intent_level, created_at, nickname, account, "
        "source_url, video FROM leads " + where +
        " ORDER BY created_at DESC, id ASC LIMIT 1000", params
    )
    base_items = [{
        # Locally generated leads primary key; platform IDs stay in the DB only.
        "id": str(r.get("id") or ""),
        "recordId": str(r.get("id") or ""),
        "ts": _epoch_ms(r.get("created_at")),
        "comment": _clip(r.get("comment"), 120),
        "commentText": _clip(r.get("comment"), 120),
        "nickname": str(r.get("nickname") or ""),
        "userUrl": "",
        "keyword": str(r.get("source_keyword") or ""),
        "intention": str(r.get("intent_level") or "未评级"),
        "aiIntention": str(r.get("intent_level") or ""),
        "judgeStatus": "matched",
        "matched": True,
        "matchType": "keyword",
        "judgeReason": "命中监控规则并进入线索库",
        "reason": "命中监控规则并进入线索库",
        "account": str(r.get("account") or ""),
        "accountName": str(r.get("account") or ""),
        "videoUrl": str(r.get("source_url") or ""),
        "videoTitle": str(r.get("video") or ""),
        "likeStatus": "none",
        "followStatus": "none",
        "replyStatus": "none",
        "profileCommentStatus": "none",
        "dmStatus": "none",
        "profileShareStatus": "none",
        "liveWorkState": "idle",
        "time": _fmt(r.get("created_at")),
    } for r in rows]
    if _exists("live_action_results") and _exists("live_action_details"):
        results = _rows(
            "SELECT r.action,r.status,r.code,r.url,d.body FROM live_action_results r "
            "LEFT JOIN live_action_details d ON d.result_id=r.id "
            "WHERE r.task_id=? ORDER BY r.created_ms DESC", (task_id,)
        ) if task_id else []
        action_fields = {
            "like-comment": "likeStatus", "reply-comment": "replyStatus",
            "send-dm": "dmStatus", "follow-user": "followStatus",
            "share-comment": "profileShareStatus", "share-profile": "profileShareStatus",
        }
        for result in results:
            field = action_fields.get(str(result.get("action") or ""))
            if not field:
                continue
            try:
                import json
                details = json.loads(result.get("body") or "{}")
            except (TypeError, ValueError):
                details = {}
            target = None
            # Current result rows don't retain the local lead key. Match only when
            # their internal comment ID and author/content evidence identify one row.
            if target is None and details.get("commentId"):
                target = next((item for item in base_items
                               if item["videoUrl"] == str(result.get("url") or "")
                               and item["commentText"] == str(details.get("sourceText") or "")
                               and item["nickname"] == str(details.get("commentAuthor") or "")), None)
            if target is None:
                continue
            raw_status = str(result.get("status") or "")
            target[field] = ("success" if raw_status == "sent_ok" else
                             "pending" if raw_status == "uncertain" else
                             "skipped" if raw_status == "skipped" else "failed")
            target[field.replace("Status", "Error")] = _clip(result.get("code"), 120)
            target["liveWorkState"] = "action"
    query = str(args.get("search") or "").strip().casefold()
    if query:
        base_items = [item for item in base_items if query in " ".join(
            str(item.get(key) or "") for key in
            ("nickname", "commentText", "keyword", "account", "videoTitle", "videoUrl")
        ).casefold()]
    result_filter = str(args.get("result") or "all").casefold()
    if result_filter in {"hit", "matched", "success"}:
        base_items = [item for item in base_items if item["matched"]]
    elif result_filter in {"miss", "unmatched", "failed"}:
        base_items = [item for item in base_items if not item["matched"]]
    outcome_filter = str(args.get("outcome") or "all").casefold()
    if outcome_filter not in {"", "all"}:
        base_items = [item for item in base_items if outcome_filter in {
            str(item.get("likeStatus") or "none"), str(item.get("replyStatus") or "none"),
            str(item.get("followStatus") or "none"), str(item.get("dmStatus") or "none"),
            str(item.get("profileShareStatus") or "none"),
        }]
    action_filter = args.get("actions")
    if isinstance(action_filter, list) and action_filter:
        action_fields = {
            "like": "likeStatus", "reply": "replyStatus", "follow": "followStatus",
            "profileComment": "profileCommentStatus", "dm": "dmStatus",
            "profileShare": "profileShareStatus",
        }
        base_items = [item for item in base_items if any(
            item.get(action_fields[key], "none") not in {"", "none"}
            for key in action_filter if key in action_fields
        )]
    total = len(base_items)
    counts = {"all": total, "hit": total, "miss": 0}
    try:
        offset = max(0, int(args.get("offset") or 0))
        limit = min(100, max(1, int(args.get("limit") or 20)))
    except (TypeError, ValueError):
        offset, limit = 0, 20
    items = base_items[offset:offset + limit]
    return {"success": True, "items": items, "total": total, "counts": counts,
            "note": "仅返回带真实评论文本的线索；评论为空的线索不参与命中统计"}


def leadgen_video_comments(args: dict | None = None) -> dict:
    args = args if isinstance(args, dict) else {}
    task_id = str(args.get("taskId") or "")
    rows = _rows("SELECT * FROM live_action_results WHERE task_id=? AND action='post-comment' "
                 "ORDER BY created_ms DESC,id DESC", (task_id,)) if task_id and _exists("live_action_results") else []
    items = [{"id": str(r["id"]), "ts": r["created_ms"], "videoUrl": r["url"],
              "videoTitle": r["title"], "content": r["text"],
              "status": "success" if r["status"] == "sent_ok" else "skipped" if r["status"] == "skipped" else "failed",
              "reason": r["code"], "error": "结果不确定，禁止自动重试" if r["status"] == "uncertain" else r["code"],
              "accountId": r["account_id"], "accountName": r["account_id"],
              "automationTaskId": r["run_id"]} for r in rows]
    counts = {key: sum(i["status"] == key for i in items) for key in ("success", "failed", "skipped")}
    counts["all"] = len(items)
    query = str(args.get("search") or "").strip().lower()
    selected = [i for i in items if (args.get("status") in (None, "", "all", i["status"]))
                and (not args.get("accountId") or args["accountId"] == i["accountId"])
                and (not args.get("automationTaskId") or args["automationTaskId"] == i["automationTaskId"])
                and (not query or query in " ".join(str(v) for v in i.values()).lower())]
    offset = max(0, int(args.get("offset") or 0))
    limit = min(100, max(1, int(args.get("limit") or 20)))
    return {"success": True, "items": selected[offset:offset+limit], "total": len(selected),
            "counts": counts, "failureStats": [], "accountStats": [],
            "note": "仅展示当前任务实际执行的主评记录；采集到的他人评论不计作已发表"}


def warmup_events(_: dict | None = None) -> dict:
    if not _exists("account_events"):
        return {"success": True, "events": []}
    events = [{
        "action": str(r.get("action") or ""),
        "detail": _clip(r.get("detail"), 100),
        "success": bool(r.get("success")),
        "time": _fmt(r.get("created_at")),
    } for r in _rows(
        "SELECT action, detail, success, created_at FROM account_events "
        "ORDER BY created_at DESC LIMIT 30"
    )]
    return {"success": True, "events": events}


def runtime_logs(kind: str = "") -> dict:
    logs: list[dict] = []
    if _exists("tasks"):
        logs = [{
            "type": str(r.get("task_type") or ""),
            "status": str(r.get("status") or ""),
            "message": _clip(r.get("error_message") or "执行完成", 120),
            "time": _fmt(r.get("created_at")),
        } for r in _rows(
            "SELECT task_type, status, error_message, created_at FROM tasks "
            "ORDER BY created_at DESC LIMIT 30"
        )]
    return {"success": True, "logs": logs, "note": f"本地任务运行记录（{kind or '全部'}）"}


def running_task_ids(kind: str = "") -> list:
    ids: list[str] = []
    if _exists("tasks"):
        ids += [str(r["id"]) for r in _rows(
            "SELECT id FROM tasks WHERE status IN ('running','pending','processing')"
        )]
    if _exists("crawl_tasks"):
        ids += [str(r["id"]) for r in _rows(
            "SELECT id FROM crawl_tasks WHERE status IN ('running','pending','processing')"
        )]
    return sorted(set(ids))


_UI_TASK_KIND = {
    "warmup": "nurture",
    "entity": "entity_leadgen",
    "monitor": "video_monitor",
    "publish": "work_publish",
    "leadgen": "entity_leadgen",
}
_VIEW_PREFIX = {
    "nurture": "douyin",
    "entity_leadgen": "entity",
    "video_monitor": "monitor",
    "work_publish": "publish",
}


def _task_record(kind: str, task_id: str) -> dict:
    if not task_id:
        return {}
    try:
        from app.api import task_store
        from app.paths import DB_PATH

        rec = task_store.get_task(DB_PATH, kind, task_id) or {}
    except Exception:  # noqa: BLE001 读不到就少几个显示字段，不影响运行态
        return {}
    cfg = rec.get("configSnapshot")
    if isinstance(cfg, str):
        try:
            import json as _json

            cfg = _json.loads(cfg)
        except ValueError:
            cfg = {}
    if isinstance(cfg, dict):
        rec["configSnapshot"] = cfg
    return rec


def _account_row(account_id: str) -> dict:
    if not account_id:
        return {}
    try:
        rows = _rows("SELECT id, name, nickname, platform FROM accounts WHERE id=? LIMIT 1",
                     (account_id,))
    except Exception:  # noqa: BLE001
        return {}
    return rows[0] if rows else {}


def active_automation_tasks(_: dict | None = None) -> list[dict]:
    """当前**真的有线程在跑**的任务，逐账号一条，形状对齐实况大屏。

    以前这里返回的是「库里 status=running/pending 的任务 id」，两个后果：
      · `tasks` 表里躺着 66 条 9 月遗留 pending 行，从来没被收尾 —— 大屏永远有卡片；
      · 页面拿到的是字符串 id，读 `y.taskKind` 全是 undefined，养号运行列表被
        每次同步清空 —— 用户看到「养号卡住/卡片乱跳」。
    现在只认活线程（registry + task_runner），按账号聚合，字段补齐页面要用的。
    """
    items: list[dict] = []
    try:
        from app.runtime import registry

        items.extend(registry.active())
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.api import task_runner

        items.extend(task_runner.active_records())
    except Exception:  # noqa: BLE001
        pass

    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        account_id = str(item.get("accountId") or "").strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        kind = str(item.get("kind") or "")
        task_kind = _UI_TASK_KIND.get(kind, "entity_leadgen")
        rec = _task_record(kind, str(item.get("taskId") or ""))
        acc = _account_row(account_id)
        cfg = rec.get("configSnapshot") if isinstance(rec.get("configSnapshot"), dict) else {}
        entry = {
            "id": account_id,
            "accountId": account_id,
            "taskId": str(item.get("taskId") or rec.get("taskId") or ""),
            "taskKind": task_kind,
            "viewKey": f"{_VIEW_PREFIX.get(task_kind, 'douyin')}_{account_id}",
            "platform": str(acc.get("platform") or rec.get("platform") or "douyin"),
            "name": str(rec.get("name") or acc.get("name") or item.get("name") or ""),
            "nickname": str(rec.get("nickname") or acc.get("nickname") or ""),
            "status": "running",
            "startedAt": item.get("startedAt"),
            "stats": dict(item.get("stats") or rec.get("stats") or {}),
            "isSessionFinished": False,
        }
        if task_kind == "nurture":
            entry["durationMin"] = cfg.get("nurtureDurationMin") or cfg.get("durationMin")
            entry["durationMax"] = cfg.get("nurtureDurationMax") or cfg.get("durationMax")
        out.append(entry)
    return out


def schedules_from_settings(_: dict | None = None) -> dict:
    if not _exists("system_settings"):
        return {"success": True, "schedules": []}
    rows = _rows(
        "SELECT category, key, value, updated_at FROM system_settings "
        "WHERE key LIKE '%schedule%' OR key LIKE '%cron%' "
        "OR category IN ('crawl','dm_sender','notification') "
        "ORDER BY category, key LIMIT 20"
    )
    schedules = [{
        "category": str(r.get("category") or ""),
        "name": str(r.get("key") or ""),
        "value": _clip(r.get("value"), 60),
        "updatedAt": _fmt(r.get("updated_at")),
        "source": "本地运行参数（非定时排期）",
    } for r in rows]
    return {
        "success": True, "schedules": schedules,
        "note": "本地未配置定时排期；下列为已保存的运行参数，不启动任何定时任务",
    }
