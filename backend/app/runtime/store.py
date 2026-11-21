"""运行时证据链：每一轮执行、每一次循环都落库。

为什么不能只写日志
------------------
「任务跑过了」这件事必须能事后被查：谁在什么时候启动的（手动/排期）、
跑了几轮、每轮读到多少条、命中几条、为什么停下。只写运行日志时，
页面刷新后只剩一行行文字，没法回答「今天到底跑了多少次」。

三张表
------
runtime_runs    一次执行（一次启动 → 一次结束）
runtime_cycles  执行内的一轮（监控每轮巡检、养号每轮浏览、采集每个目标）
monitor_seen    监控已读过的评论键（去重，避免同一批评论反复命中）
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

_DDL = [
    """CREATE TABLE IF NOT EXISTS runtime_runs (
        run_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        task_id TEXT NOT NULL,
        task_name TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        trigger TEXT NOT NULL DEFAULT 'manual',
        status TEXT NOT NULL DEFAULT 'running',
        engine TEXT NOT NULL DEFAULT '',
        started_ms INTEGER NOT NULL,
        ended_ms INTEGER,
        stats TEXT NOT NULL DEFAULT '{}',
        message TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS runtime_cycles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        task_id TEXT NOT NULL,
        cycle_no INTEGER NOT NULL DEFAULT 1,
        started_ms INTEGER NOT NULL,
        ended_ms INTEGER,
        status TEXT NOT NULL DEFAULT 'running',
        detail TEXT NOT NULL DEFAULT '',
        stats TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS monitor_seen (
        task_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        first_seen_ms INTEGER NOT NULL,
        PRIMARY KEY(task_id, item_key)
    )""",
    """CREATE TABLE IF NOT EXISTS monitor_live_targets (
        task_id TEXT NOT NULL,
        target_key TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'live',
        name TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        account_name TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        last_check_ms INTEGER NOT NULL DEFAULT 0,
        next_check_ms INTEGER NOT NULL DEFAULT 0,
        events INTEGER NOT NULL DEFAULT 0,
        matched INTEGER NOT NULL DEFAULT 0,
        added INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0,
        updated_ms INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(task_id, target_key)
    )""",
    """CREATE TABLE IF NOT EXISTS monitor_live_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        target_key TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'comment',
        nickname TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT 'pending',
        keyword TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        actions TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        created_ms INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runs_kind ON runtime_runs(kind, started_ms)",
    "CREATE INDEX IF NOT EXISTS idx_cycles_run ON runtime_cycles(run_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_live_events_task ON monitor_live_events(task_id, id)",
]


def _now_ms() -> int:
    from time import time
    return int(time() * 1000)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    for ddl in _DDL:
        con.execute(ddl)
    return con


def init(db_path) -> None:
    con = connect(db_path)
    # Existing installations predate exact comment targeting in monitor leads.
    # Keep platform identifiers internal; IPC read models must never expose them.
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leads'").fetchone():
        columns = {row[1] for row in con.execute("PRAGMA table_info(leads)")}
        for column in ("video_id", "comment_id", "comment_user_id", "comment_time"):
            if column not in columns:
                con.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        _ensure_visibility_columns(con)
    con.commit()
    con.close()


# 吞评检测结论落在 leads 的专属列上（懒迁移，幂等 ADD COLUMN，不改不删）。
# 明细页 / 任务卡徽标 / 线索库来源筛选都直接读这些列。
_VISIBILITY_COLUMNS = (
    "visibility_status",          # visible / not_visible / inconclusive / no_sample
    "visibility_reason",          # 判定原因（用户可见）
    "visibility_target_account",  # 被检测账号 id（样本归属）
    "visibility_target_name",     # 被检测账号昵称
    "visibility_sample_kind",     # main / manual / …（样本类型）
    "visibility_sample_content",  # 样本正文（被检测的评论内容）
    "visibility_cleanup",         # '' / deleted（关联记录删除标记）
    "cleanup_deleted_at",         # 关联记录删除时间
)


def _ensure_visibility_columns(con: sqlite3.Connection) -> None:
    columns = {row[1] for row in con.execute("PRAGMA table_info(leads)")}
    for column in _VISIBILITY_COLUMNS:
        if column in columns:
            continue
        try:
            con.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError as exc:
            # 推荐页互检是多线程写结论：两个线程同时做迁移时，后到的 ALTER
            # 会撞「duplicate column」——列已经建好就是成功，其他错误照常抛。
            if "duplicate column" not in str(exc).lower():
                raise


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def run_begin(db_path, kind: str, task_id: str, task_name: str = "",
              account_id: str = "", trigger: str = "manual", engine: str = "") -> str:
    run_id = f"{kind[:3]}_{uuid.uuid4().hex[:12]}"
    con = connect(db_path)
    try:
        con.execute(
            "INSERT INTO runtime_runs(run_id, kind, task_id, task_name, account_id, "
            "trigger, status, engine, started_ms) VALUES(?,?,?,?,?,?,'running',?,?)",
            (run_id, kind, task_id, task_name[:120], account_id, trigger, engine,
             int(time.time() * 1000)))
        con.commit()
    finally:
        con.close()
    return run_id


def run_end(db_path, run_id: str, status: str, stats: dict | None = None,
            message: str = "") -> None:
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE runtime_runs SET status=?, ended_ms=?, stats=?, message=? "
            "WHERE run_id=?",
            (status, int(time.time() * 1000),
             json.dumps(stats or {}, ensure_ascii=False), str(message)[:500], run_id))
        con.commit()
    finally:
        con.close()


def cycle_begin(db_path, run_id: str, kind: str, task_id: str, cycle_no: int) -> int:
    con = connect(db_path)
    try:
        cur = con.execute(
            "INSERT INTO runtime_cycles(run_id, kind, task_id, cycle_no, started_ms, status) "
            "VALUES(?,?,?,?,?,'running')",
            (run_id, kind, task_id, cycle_no, int(time.time() * 1000)))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def cycle_end(db_path, cycle_id: int, status: str, detail: str = "",
              stats: dict | None = None) -> None:
    con = connect(db_path)
    try:
        con.execute(
            "UPDATE runtime_cycles SET status=?, ended_ms=?, detail=?, stats=? WHERE id=?",
            (status, int(time.time() * 1000), str(detail)[:400],
             json.dumps(stats or {}, ensure_ascii=False), int(cycle_id)))
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# 启动收尾：清理上一个进程遗留的「运行中」
# --------------------------------------------------------------------------
def reap_stale_runs(db_path, reason: str = "服务重启，任务已中断（未完成，不会自动续跑）") -> int:
    """把 status='running' 的遗留记录收尾成 aborted。

    为什么必须做：`running` 的真实含义是「本进程里还有线程在跑」。进程一退出
    （关软件、崩溃、被 taskkill），这行就永远停在 running ——
      · 养号页/大屏读到的运行态与线程对不上，用户以为「卡住了」；
      · 执行历史里堆一串永远不结束的僵尸记录，越积越多分不清真假。
    启动时统一收尾，用户看到的是「上次被打断」，而不是一个假的「运行中」。
    """
    now = int(time.time() * 1000)
    con = connect(db_path)
    try:
        cur = con.execute(
            "UPDATE runtime_runs SET status='aborted', ended_ms=?, message=? "
            "WHERE status='running'", (now, str(reason)[:500]))
        runs = cur.rowcount or 0
        con.execute(
            "UPDATE runtime_cycles SET status='aborted', ended_ms=?, detail=? "
            "WHERE status='running'", (now, str(reason)[:400]))
        con.commit()
        return int(runs)
    finally:
        con.close()


# --------------------------------------------------------------------------
# 监控去重
# --------------------------------------------------------------------------
def seen_has(db_path, task_id: str, key: str) -> bool:
    con = connect(db_path)
    try:
        return con.execute(
            "SELECT 1 FROM monitor_seen WHERE task_id=? AND item_key=?",
            (task_id, key)).fetchone() is not None
    finally:
        con.close()


def seen_mark(db_path, task_id: str, keys: list[str]) -> int:
    """批量标记已读；返回真正新增的条数（重复的不计入）。"""
    if not keys:
        return 0
    stamp = int(time.time() * 1000)
    con = connect(db_path)
    try:
        added = 0
        for key in keys:
            cur = con.execute(
                "INSERT OR IGNORE INTO monitor_seen(task_id, item_key, first_seen_ms) "
                "VALUES(?,?,?)", (task_id, str(key)[:200], stamp))
            added += 1 if cur.rowcount else 0
        con.commit()
        return added
    finally:
        con.close()


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 直播监控明细（真实数据源：runtime 每轮写，页面 get-monitor-live-state 读）
# --------------------------------------------------------------------------
_LIVE_TABLES_DDL = (
    """CREATE TABLE IF NOT EXISTS monitor_live_targets (
        task_id TEXT NOT NULL,
        target_key TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'live',
        name TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        account_name TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        last_check_ms INTEGER NOT NULL DEFAULT 0,
        next_check_ms INTEGER NOT NULL DEFAULT 0,
        events INTEGER NOT NULL DEFAULT 0,
        matched INTEGER NOT NULL DEFAULT 0,
        added INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0,
        updated_ms INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(task_id, target_key)
    )""",
    """CREATE TABLE IF NOT EXISTS monitor_live_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        target_key TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'comment',
        nickname TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL DEFAULT '',
        outcome TEXT NOT NULL DEFAULT 'pending',
        keyword TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        actions TEXT NOT NULL DEFAULT '',
        account_id TEXT NOT NULL DEFAULT '',
        created_ms INTEGER NOT NULL DEFAULT 0
    )""",
)


def _ensure_live_tables(con: sqlite3.Connection) -> None:
    for ddl in _LIVE_TABLES_DDL:
        con.execute(ddl)


def live_target_upsert(db_path, *, task_id: str, target_key: str, type_: str = "live",
                       name: str = "", account_id: str = "", account_name: str = "",
                       state: str = "", reason: str = "", last_check_ms: int = 0,
                       next_check_ms: int = 0, events_delta: int = 0,
                       matched_delta: int = 0, added_delta: int = 0,
                       updated_delta: int = 0) -> None:
    """每轮采集后写回单个直播目标的真实状态（存在则合并，不存在则建行）。"""
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        _ensure_live_tables(con)
        con.execute(
            "INSERT OR IGNORE INTO monitor_live_targets(task_id, target_key, type, name, "
            "account_id, account_name, state, reason, last_check_ms, next_check_ms, "
            "updated_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, target_key[:300], type_[:20], name[:80], account_id[:60],
             account_name[:80], state[:20], reason[:120], int(last_check_ms),
             int(next_check_ms), int(_now_ms())))
        con.execute(
            "UPDATE monitor_live_targets SET name=CASE WHEN ?<>'' THEN ? ELSE name END, "
            "account_id=CASE WHEN ?<>'' THEN ? ELSE account_id END, "
            "account_name=CASE WHEN ?<>'' THEN ? ELSE account_name END, "
            "state=CASE WHEN ?<>'' THEN ? ELSE state END, "
            "reason=CASE WHEN ?<>'' THEN ? ELSE reason END, "
            "last_check_ms=CASE WHEN ?>0 THEN ? ELSE last_check_ms END, "
            "next_check_ms=CASE WHEN ?>0 THEN ? ELSE next_check_ms END, "
            "events=events+?, matched=matched+?, added=added+?, updated=updated+?, "
            "updated_ms=? WHERE task_id=? AND target_key=?",
            (name[:80], name[:80], account_id[:60], account_id[:60], account_name[:80],
             account_name[:80], state[:20], state[:20], reason[:120], reason[:120],
             int(last_check_ms), int(last_check_ms), int(next_check_ms), int(next_check_ms),
             int(events_delta), int(matched_delta), int(added_delta), int(updated_delta),
             int(_now_ms()), task_id, target_key[:300]))
        con.commit()
    finally:
        con.close()


def live_event_insert(db_path, *, task_id: str, target_key: str, kind: str,
                      nickname: str, content: str, account_id: str = "",
                      outcome: str = "pending") -> int:
    """写一条直播事件明细；返回行 id（0 = 写失败，调用方跳过回写）。"""
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        _ensure_live_tables(con)
        cur = con.execute(
            "INSERT INTO monitor_live_events(task_id, target_key, kind, nickname, content, "
            "outcome, account_id, created_ms) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, target_key[:300], kind[:20], nickname[:60], content[:500],
             outcome[:20], account_id[:60], int(_now_ms())))
        con.commit()
        return int(cur.lastrowid or 0)
    finally:
        con.close()


def live_event_set_outcome(db_path, event_id: int, outcome: str, keyword: str = "",
                           note: str = "", actions: str = "") -> None:
    if not event_id:
        return
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        _ensure_live_tables(con)
        con.execute(
            "UPDATE monitor_live_events SET outcome=?, keyword=?, note=?, actions=? WHERE id=?",
            (outcome[:20], keyword[:80], note[:200], actions[:200], int(event_id)))
        con.commit()
    finally:
        con.close()


def live_target_bump(db_path, *, task_id: str, target_key: str,
                     added: int = 0, updated: int = 0, matched: int = 0) -> None:
    """命中线索落库后累加目标计数；目标行不存在时静默跳过（群监控没有目标行）。"""
    if not (added or updated or matched):
        return
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        _ensure_live_tables(con)
        con.execute(
            "UPDATE monitor_live_targets SET added=added+?, updated=updated+?, "
            "matched=matched+?, updated_ms=? WHERE task_id=? AND target_key=?",
            (int(added), int(updated), int(matched), int(_now_ms()),
             task_id, target_key[:300]))
        con.commit()
    finally:
        con.close()


def live_events_prune(db_path, task_id: str, keep: int = 20000) -> None:
    """明细保留最近 keep 条（页面承诺「已结束明细保留最近约 20,000 条」）。"""
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        _ensure_live_tables(con)
        con.execute(
            "DELETE FROM monitor_live_events WHERE task_id=? AND id NOT IN "
            "(SELECT id FROM monitor_live_events WHERE task_id=? ORDER BY id DESC LIMIT ?)",
            (task_id, task_id, int(keep)))
        con.commit()
    finally:
        con.close()


def recent_runs(db_path, kind: str = "", limit: int = 30) -> list[dict]:
    con = connect(db_path)
    try:
        if kind:
            rows = con.execute(
                "SELECT * FROM runtime_runs WHERE kind=? ORDER BY started_ms DESC LIMIT ?",
                (kind, int(limit))).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM runtime_runs ORDER BY started_ms DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [_run_row(r) for r in rows]
    finally:
        con.close()


def warmup_cooldown_seconds(db_path, account_id: str, interval_s: int = 180) -> int:
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT MAX(ended_ms) FROM runtime_runs "
            "WHERE kind='warmup' AND account_id=? AND ended_ms IS NOT NULL",
            (account_id,)).fetchone()
        ended_ms = int(row[0] or 0)
        if not ended_ms:
            return 0
        return max(0, int((ended_ms + interval_s * 1000 - int(time.time() * 1000) + 999) / 1000))
    finally:
        con.close()


def cycles_of(db_path, run_id: str = "", task_id: str = "", limit: int = 50) -> list[dict]:
    con = connect(db_path)
    try:
        if run_id:
            rows = con.execute(
                "SELECT * FROM runtime_cycles WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, int(limit))).fetchall()
        elif task_id:
            rows = con.execute(
                "SELECT * FROM runtime_cycles WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, int(limit))).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM runtime_cycles ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["stats"] = json.loads(item.get("stats") or "{}")
            except (TypeError, ValueError):
                item["stats"] = {}
            item["startedAt"] = _fmt_ms(item.get("started_ms"))
            item["endedAt"] = _fmt_ms(item.get("ended_ms"))
            out.append(item)
        return out
    finally:
        con.close()


def _run_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["stats"] = json.loads(item.get("stats") or "{}")
    except (TypeError, ValueError):
        item["stats"] = {}
    item["startedAt"] = _fmt_ms(item.get("started_ms"))
    item["endedAt"] = _fmt_ms(item.get("ended_ms"))
    item["durationSec"] = (
        round((int(item["ended_ms"]) - int(item["started_ms"])) / 1000, 1)
        if item.get("ended_ms") else None)
    return item


def _fmt_ms(value: Any) -> str:
    try:
        ms = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# 业务结果落库（页面本来就在读的表）
# --------------------------------------------------------------------------
def insert_lead(db_path, *, lead_id: str, nickname: str, comment: str,
                keyword: str, source: str, task_id: str, video_url: str = "",
                video_id: str = "", comment_id: str = "", comment_user_id: str = "",
                comment_time: str = "", account: str = "", score: int = 0,
                level: str = "C", status: str = "collected") -> bool:
    """写入一条真实采集到的线索；已存在则不重复写（返回 False）。"""
    stamp = _now()
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO leads(id, nickname, comment, source_keyword, source, "
            "source_task_id, source_url, video_id, comment_id, comment_user_id, comment_time, "
            "account, status, score, intent_level, platform, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lead_id, nickname[:60], comment[:500], keyword[:80], source, task_id,
             video_url[:300], video_id[:80], comment_id[:120], comment_user_id[:120],
             comment_time[:80], account[:60], status, int(score), level,
             "douyin", stamp, stamp))
        con.commit()
        return bool(cur.rowcount)
    finally:
        con.close()


def insert_visibility_lead(db_path, *, task_id: str, target_account: str,
                           target_name: str, sample_kind: str, sample_content: str,
                           video_url: str = "", status: str = "no_sample",
                           reason: str = "", keyword: str = "吞评检测",
                           account: str = "") -> str:
    """写入 / 刷新一条吞评检测结论（source=entity_comment_visibility）。

    id 由 任务+被检账号+样本正文 稳定推导：同一样本重跑不产生重复行，
    而是把最新判定（含原因）覆盖回去；人工删除标记同时清零，以最新运行为准。
    返回线索 id。
    """
    import hashlib

    # 样本的唯一定位 = 任务 + 被检账号 + 视频 + 正文：推荐页互检的样本是按视频发的，
    # 两条不同视频上恰好用了同一句（随机表情很容易撞）不能被压成一条结论。
    key = "|".join((str(task_id), str(target_account), str(video_url or ""),
                    str(sample_content or "")))
    lid = "ld_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        _ensure_visibility_columns(con)
        con.execute(
            "INSERT INTO leads(id, nickname, comment, source_keyword, source, "
            "source_task_id, source_url, account, status, intent_level, platform, "
            "visibility_status, visibility_reason, visibility_target_account, "
            "visibility_target_name, visibility_sample_kind, visibility_sample_content, "
            "visibility_cleanup, cleanup_deleted_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'C','douyin',?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "nickname=excluded.nickname, comment=excluded.comment, "
            "source_url=excluded.source_url, account=excluded.account, "
            "visibility_status=excluded.visibility_status, "
            "visibility_reason=excluded.visibility_reason, "
            "visibility_target_name=excluded.visibility_target_name, "
            "visibility_sample_kind=excluded.visibility_sample_kind, "
            "visibility_cleanup='', cleanup_deleted_at='', updated_at=excluded.updated_at",
            (lid, (target_name or target_account or "未知账号")[:60],
             (sample_content or "（无可用样本）")[:500], keyword[:80],
             "entity_comment_visibility", task_id, (video_url or "")[:300],
             (account or "")[:60], "collected",
             status[:20], (reason or "")[:300], (target_account or "")[:60],
             (target_name or "")[:60], (sample_kind or "main")[:20],
             (sample_content or "")[:500], "", "", _now(), _now()))
        con.commit()
        return lid
    finally:
        con.close()


def insert_account_event(db_path, *, account_id: str, action: str, detail: str,
                         success: bool = True) -> None:
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        con.execute(
            "INSERT INTO account_events(id, account_id, action, detail, success, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (f"ae_{uuid.uuid4().hex[:12]}", account_id, action[:40], detail[:300],
             1 if success else 0, _now()))
        con.commit()
    finally:
        con.close()


def diagnostics(db_path) -> dict:
    """诊断用：三张表的规模，确认证据链真的在长。"""
    con = connect(db_path)
    try:
        return {
            "runs": con.execute("SELECT COUNT(*) FROM runtime_runs").fetchone()[0],
            "cycles": con.execute("SELECT COUNT(*) FROM runtime_cycles").fetchone()[0],
            "seen": con.execute("SELECT COUNT(*) FROM monitor_seen").fetchone()[0],
        }
    finally:
        con.close()


def db_file(db_path) -> Path:
    return Path(db_path)
