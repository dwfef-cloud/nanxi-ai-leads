"""§4 数据层 —— 6 张表（5 新建 + leads 补列）+ 90 天 TTL + 最小集写入。

设计约束：
- 只用 sqlite3 标准库 + 参数绑定，不引入 ORM（与南溪现有 backend/app/db 风格一致）
- 所有时间字段用 INTEGER epoch 秒（与 §4 DDL 一致）；南溪存量表用 TEXT ISO，互不干扰
- CREATE TABLE IF NOT EXISTS / 幂等补列，不修改、不删除任何存量数据
- 写入口统一走 validate_candidate_payload，越界字段直接拒绝
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from app.execution.compliance import ComplianceViolation, validate_candidate_payload
from app.execution.params import TTL_SECONDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS lead_candidates (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  video_url         TEXT NOT NULL,
  nickname          TEXT,
  comment_text      TEXT,
  comment_text_hash TEXT NOT NULL,
  intent_score      INTEGER,
  intent_tags       TEXT,
  source_keyword    TEXT,
  created_at        INTEGER NOT NULL,
  expire_at         INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_dedup
  ON lead_candidates(video_url, comment_text_hash);
CREATE INDEX IF NOT EXISTS idx_candidate_expire ON lead_candidates(expire_at);
CREATE INDEX IF NOT EXISTS idx_candidate_score ON lead_candidates(intent_score);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id          TEXT PRIMARY KEY,
  stage       TEXT NOT NULL,
  account_id  INTEGER,
  stats       TEXT,
  started_at  INTEGER NOT NULL,
  finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pipeline_stage ON pipeline_runs(stage);

-- D22 断点续跑：阶段内逐条目 checkpoint。每处理完一条（评论/候选/私信目标）
-- 立即落盘，杀进程重启后按 (run_id, stage, item_key) 跳过已完成条目，
-- 配合 worker 幂等键实现双重防重复。
CREATE TABLE IF NOT EXISTS pipeline_progress (
  run_id   TEXT NOT NULL,
  stage    TEXT NOT NULL,
  item_key TEXT NOT NULL,
  result   TEXT NOT NULL DEFAULT '',
  done_at  INTEGER NOT NULL,
  PRIMARY KEY (run_id, stage, item_key)
);

CREATE TABLE IF NOT EXISTS comment_send_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id   INTEGER,
  candidate_id INTEGER,
  content      TEXT,
  status       TEXT NOT NULL,
  created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comment_log_status ON comment_send_log(status);

CREATE TABLE IF NOT EXISTS account_quotas (
  account_id INTEGER NOT NULL,
  day        TEXT NOT NULL,
  metric     TEXT NOT NULL,
  baseline   INTEGER NOT NULL DEFAULT 0,
  snapshot   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(account_id, day, metric)
);

CREATE TABLE IF NOT EXISTS nurture_sessions (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id        INTEGER,
  videos_watched    INTEGER NOT NULL DEFAULT 0,
  likes             INTEGER NOT NULL DEFAULT 0,
  favorites         INTEGER NOT NULL DEFAULT 0,
  shares            INTEGER NOT NULL DEFAULT 0,
  comments_browsed  INTEGER NOT NULL DEFAULT 0,
  started_at        INTEGER NOT NULL,
  ended_at          INTEGER
);
"""

# 允许流水线状态机写入的字段（防止 status 等任意值落库）
VALID_STAGES = frozenset({"collect", "filter", "engage", "dm", "done", "stopped"})
VALID_SEND_STATUS = frozenset({"sent_ok", "sent_fail", "uncertain"})
VALID_QUOTA_METRICS = frozenset({"read", "like", "comment", "dm"})


def comment_hash(text: str) -> str:
    """去重用哈希。只用于重复判定，不做用户标识。"""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:32]


class ExecutionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        """建表（幂等）。不触碰任何存量表数据。"""
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def migrate_leads(self) -> list[str]:
        """给存量 leads 表补两列（幂等 ADD COLUMN，不改不删）。

        返回实际新增的列名。生产库执行前应先备份。
        """
        added: list[str] = []
        with self.connect() as conn:
            existing = {
                row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()
            }
            for column, ddl in (
                ("intent_score", "ALTER TABLE leads ADD COLUMN intent_score INTEGER"),
                ("source_candidate_id", "ALTER TABLE leads ADD COLUMN source_candidate_id INTEGER"),
            ):
                if column not in existing:
                    conn.execute(ddl)
                    added.append(column)
        return added

    # ── lead_candidates ──────────────────────────────────────
    def insert_candidate(self, payload: Mapping[str, object], *, now: int | None = None) -> int | None:
        """写入一条候选。返回 id；重复记录返回 None（不报错、不覆盖）。

        服务端强制生成 created_at / expire_at / comment_text_hash，
        不信任调用方传入的时间与哈希，避免 TTL 被绕过。
        """
        validate_candidate_payload(payload, client_side=True)
        text = str(payload.get("comment_text") or "")
        video_url = str(payload.get("video_url") or "").strip()
        if not video_url:
            raise ComplianceViolation("video_url 不能为空")

        ts = int(time.time()) if now is None else int(now)
        values = {
            "video_url": video_url,
            "nickname": payload.get("nickname"),
            "comment_text": text,
            "comment_text_hash": comment_hash(text),
            "intent_score": payload.get("intent_score"),
            "intent_tags": _dump_tags(payload.get("intent_tags")),
            "source_keyword": payload.get("source_keyword"),
            "created_at": ts,
            "expire_at": ts + TTL_SECONDS,
        }
        # 二次校验：实际入库字段也必须全部在白名单内
        validate_candidate_payload(values)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO lead_candidates ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            return cur.lastrowid if cur.rowcount == 1 else None

    def list_candidates(self, *, min_score: int | None = None) -> list[dict]:
        sql = "SELECT * FROM lead_candidates"
        args: tuple = ()
        if min_score is not None:
            sql += " WHERE intent_score >= ?"
            args = (int(min_score),)
        sql += " ORDER BY id"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def purge_expired(self, *, now: int | None = None) -> int:
        """清理过期候选（对应 scheduler 每日 03:00 任务）。返回删除行数。"""
        ts = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM lead_candidates WHERE expire_at < ?", (ts,))
            return cur.rowcount

    # ── pipeline_runs ────────────────────────────────────────
    def create_run(self, run_id: str, *, now: int | None = None) -> None:
        ts = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pipeline_runs (id,stage,stats,started_at) VALUES (?,?,?,?)",
                (run_id, "collect", "{}", ts),
            )

    def set_stage(self, run_id: str, stage: str, stats: Mapping[str, object] | None = None,
                  *, now: int | None = None) -> None:
        if stage not in VALID_STAGES:
            raise ValueError(f"非法 stage: {stage}")
        ts = int(time.time()) if now is None else int(now)
        finished = ts if stage in {"done", "stopped"} else None
        with self.connect() as conn:
            conn.execute(
                "UPDATE pipeline_runs SET stage=?, stats=?, finished_at=? WHERE id=?",
                (stage, json.dumps(dict(stats or {}), ensure_ascii=False), finished, run_id),
            )

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pipeline_runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    # ── pipeline_progress（D22 断点续跑）──────────────────────
    def mark_item_done(self, run_id: str, stage: str, item_key: str,
                       *, result: str = "", now: int | None = None) -> None:
        ts = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pipeline_progress (run_id,stage,item_key,result,done_at)"
                " VALUES (?,?,?,?,?)",
                (run_id, stage, item_key, result, ts),
            )

    def is_item_done(self, run_id: str, stage: str, item_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM pipeline_progress WHERE run_id=? AND stage=? AND item_key=?",
                (run_id, stage, item_key),
            ).fetchone()
            return row is not None

    def done_items(self, run_id: str, stage: str) -> set[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT item_key FROM pipeline_progress WHERE run_id=? AND stage=?",
                (run_id, stage),
            ).fetchall()
            return {r["item_key"] for r in rows}

    # ── comment_send_log ─────────────────────────────────────
    def log_send(self, *, candidate_id: int, content: str, status: str,
                 account_id: int | None = None, now: int | None = None) -> int:
        if status not in VALID_SEND_STATUS:
            raise ValueError(f"非法写入状态: {status}")
        ts = int(time.time()) if now is None else int(now)
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO comment_send_log (account_id,candidate_id,content,status,created_at) "
                "VALUES (?,?,?,?,?)",
                (account_id, candidate_id, content, status, ts),
            )
            return int(cur.lastrowid)

    # ── account_quotas ───────────────────────────────────────
    def upsert_quota(self, account_id: int, day: str, metric: str,
                     baseline: int, snapshot: int) -> None:
        """快照对账法：delta = max(0, snapshot - baseline) 由查询侧计算。"""
        if metric not in VALID_QUOTA_METRICS:
            raise ValueError(f"非法配额指标: {metric}")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO account_quotas (account_id,day,metric,baseline,snapshot) "
                "VALUES (?,?,?,?,?) ON CONFLICT(account_id,day,metric) "
                "DO UPDATE SET baseline=excluded.baseline, snapshot=excluded.snapshot",
                (account_id, day, metric, int(baseline), int(snapshot)),
            )

    def quota_delta(self, account_id: int, day: str, metric: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT baseline,snapshot FROM account_quotas "
                "WHERE account_id=? AND day=? AND metric=?",
                (account_id, day, metric),
            ).fetchone()
        if row is None:
            return 0
        return max(0, int(row["snapshot"]) - int(row["baseline"]))

    # ── nurture_sessions ─────────────────────────────────────
    def log_nurture(self, stats: Mapping[str, int], *, account_id: int | None = None,
                    now: int | None = None) -> int:
        allowed = {"videos_watched", "likes", "favorites", "shares", "comments_browsed"}
        unknown = set(stats) - allowed
        if unknown:
            raise ValueError(f"非法养号统计字段: {sorted(unknown)}")
        ts = int(time.time()) if now is None else int(now)
        values = {key: int(stats.get(key, 0)) for key in sorted(allowed)}
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO nurture_sessions "
                "(account_id,videos_watched,likes,favorites,shares,comments_browsed,started_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (account_id, values["videos_watched"], values["likes"], values["favorites"],
                 values["shares"], values["comments_browsed"], ts),
            )
            return int(cur.lastrowid)


def _dump_tags(tags: object) -> str | None:
    if tags is None:
        return None
    if isinstance(tags, str):
        return json.dumps([tags], ensure_ascii=False)
    if isinstance(tags, Iterable):
        return json.dumps([str(t) for t in tags], ensure_ascii=False)
    return json.dumps([str(tags)], ensure_ascii=False)
