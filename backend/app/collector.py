"""真机采集结果入库（评论 → leads）。

与「本地模式」的区别
------------------
本地模式读的是**库里已有的**评论；本模块写的是**刚从平台抓到**的评论。
清洗、打分、推进仍复用 app.execution.rules.classify，链路不分叉。

去重口径
--------
优先用平台评论 id（comment_id，内部存储，不入前端出口）；
缺 id 时退化为 (昵称 + 评论文本) 的组合键。同一关键词重复采集不会产生重复线索。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lead_id(comment: dict) -> str:
    raw = str(comment.get("commentId") or "").strip()
    if raw:
        return "ld_" + hashlib.sha1(f"cid:{raw}".encode()).hexdigest()[:16]
    key = f"{comment.get('nickname') or ''}|{comment.get('text') or ''}"
    return "ld_" + hashlib.sha1(key.encode()).hexdigest()[:16]


def lead_id_of(comment: dict) -> str:
    """与入库同一口径的线索 id（判定完成后按它推进状态）。"""
    return _lead_id(comment)


def promote(db_path, lead_id: str, score: int, level: str, tags: list[str]) -> None:
    """把一条真实采集的线索推进到待触达（只动 collected，避免覆盖人工处理结果）。"""
    if not lead_id:
        return
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        con.execute(
            "UPDATE leads SET score=?, intent_level=?, tags=?, status='pending_outreach', "
            "updated_at=? WHERE id=? AND status='collected'",
            (int(score), level, json.dumps(tags, ensure_ascii=False), _now(), lead_id),
        )
        con.commit()
    finally:
        con.close()


def ingest_comments(db_path, comments: list[dict], keyword: str,
                    task_id: str = "", account: str = "") -> dict:
    """把壳抓回来的评论写入 leads。返回 {inserted, duplicated, skipped}。"""
    items = [c for c in (comments or []) if isinstance(c, dict)]
    inserted = dupe = skipped = 0
    if not items:
        return {"inserted": 0, "duplicated": 0, "skipped": 0}
    con = sqlite3.connect(str(db_path), timeout=15)
    try:
        for c in items:
            text = str(c.get("text") or "").strip()
            if not text:
                skipped += 1
                continue
            lid = _lead_id(c)
            if con.execute("SELECT 1 FROM leads WHERE id=?", (lid,)).fetchone():
                dupe += 1
                continue
            con.execute(
                "INSERT INTO leads(id, nickname, comment, source_keyword, source, "
                "source_task_id, source_url, video, video_id, comment_id, comment_user_id, "
                "account, note, region, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'collected',?,?)",
                (lid,
                 str(c.get("nickname") or "未知用户")[:60],
                 text[:500],
                 str(keyword or "")[:80],
                 "keyword_comment",
                 str(task_id or ""),
                 str(c.get("videoUrl") or "")[:300],
                 str(c.get("caption") or "")[:200],
                 str(c.get("videoId") or "")[:80],
                 str(c.get("commentId") or "")[:80],
                 str(c.get("userId") or "")[:80],
                 str(account or ""),
                 ("平台显示时间：" + str(c["publishedText"])[:60]) if c.get("publishedText") else "",
                 str(c.get("publishedText") or "").split("·", 1)[1][:30] if "·" in str(c.get("publishedText") or "") else "",
                 _now(), _now()),
            )
            inserted += 1
        con.commit()
    finally:
        con.close()
    return {"inserted": inserted, "duplicated": dupe, "skipped": skipped}


def annotate_intent(db_path, comment):
    """Preserve user-managed leads; annotate only freshly collected records."""
    matched = comment.get("intentMatched") is True
    reason = str(comment.get("intentReason") or "")[:300]
    with sqlite3.connect(str(db_path), timeout=15) as con:
        con.execute("UPDATE leads SET intent_level=?,score=?,tags=?,note=CASE WHEN note='' THEN ? ELSE note END, "
                    "status=?,updated_at=? WHERE id=? AND status='collected'",
                    ("high" if matched else "normal", 80 if matched else 0,
                     json.dumps([reason], ensure_ascii=False), reason,
                     "pending_outreach" if matched else "collected", _now(), _lead_id(comment)))


def new_task_id() -> str:
    return "ldt_" + uuid.uuid4().hex[:10]


def latest_collected(db_path, keyword: str = "", limit: int = 20) -> list[dict]:
    """调试用：看某个关键词最新入库的评论。"""
    con = sqlite3.connect(str(db_path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        sql = ("SELECT nickname, comment, source_keyword, status, created_at FROM leads "
               "WHERE source='keyword_comment'")
        args: tuple[Any, ...] = ()
        if keyword:
            sql += " AND source_keyword=?"
            args = (keyword,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        return [dict(r) for r in con.execute(sql, args + (limit,))]
    finally:
        con.close()
