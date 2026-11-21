"""账号资料同步 —— 后端侧（对标阿里山原版「登录账号资料采集 + 每日快照」机制）。

原版阿里山语义：
    accounts.name     = 用户设置的备注（内部名，用户手填，永不被覆盖）
    accounts.nickname = 抖音真实昵称（登录后从创作者中心同步，未同步显示「未获取抖音昵称」）
    账号数据看板      = 每日定时采集已登录账号创作者中心的 粉丝/关注/作品/获赞，按天存快照

本模块职责：
    接收桌面壳从「用户本人已登录的创作者中心页面」读取的只读资料
    （昵称 / 抖音号 / 关注 / 粉丝指数 / 获赞），写回 accounts.nickname
    并落一张按天快照表 account_stats_daily，供账号数据看板展示趋势。

合规边界：
    - 只接收用户本人账号、用户本人屏幕上已展示的数字；不做任何平台主动请求，
      不代操作、不抓取他人数据；桌面壳侧也仅 read-only executeJavaScript。
    - 抖音号（douyinId）落库用于核对身份，但对外出口仍按 ipc_bridge 的
      平台 uid 白名单拦截，不在任何列表接口透出。
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body

from app.api.ipc_bridge import DB_PATH

router = APIRouter()

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS account_stats_daily (
    account_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    nickname   TEXT NOT NULL DEFAULT '',
    douyin_id  TEXT NOT NULL DEFAULT '',
    following  INTEGER NOT NULL DEFAULT 0,
    followers  INTEGER NOT NULL DEFAULT 0,
    works      INTEGER NOT NULL DEFAULT 0,
    likes      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, day)
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _clean(text: object, limit: int = 60) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _int(value: object) -> int:
    """解析「146」「1.2万」「97016917378」等展示值。"""
    s = str(value or "").strip().replace(",", "")
    m = re.search(r"[\d.]+", s)
    if not m:
        return 0
    try:
        num = float(m.group())
    except ValueError:
        return 0
    if "万" in s:
        num *= 10000
    elif "亿" in s:
        num *= 100_000_000
    return int(num)


@router.post("/api/accounts/profile-sync")
def profile_sync(payload: dict = Body(default={})) -> dict:
    account_id = _clean(payload.get("accountId"), 64)
    if not account_id:
        return {"success": False, "error": "accountId 必填"}

    nickname = _clean(payload.get("nickname"))
    douyin_id = _clean(payload.get("douyinId"), 64)
    stats = {
        "following": _int(payload.get("following")),
        "followers": _int(payload.get("followers")),
        "works": _int(payload.get("works")),
        "likes": _int(payload.get("likes")),
    }

    with closing(sqlite3.connect(str(DB_PATH), timeout=5)) as conn:
        conn.execute(TABLE_SQL)
        exists = conn.execute(
            "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if not exists:
            return {"success": False, "error": "账号不存在：" + account_id}

        # 1) 真实昵称回写（备注 name 不动）
        if nickname:
            conn.execute(
                "UPDATE accounts SET nickname = ?, updated_at = ? WHERE id = ?",
                (nickname, _now(), account_id),
            )
        # 2) 当日快照 upsert（同一天反复打开创作者中心 → 刷新为最新值）
        conn.execute(
            "INSERT INTO account_stats_daily "
            " (account_id, day, nickname, douyin_id, following, followers, works, likes, updated_at) "
            " VALUES (?,?,?,?,?,?,?,?,?) "
            " ON CONFLICT(account_id, day) DO UPDATE SET "
            "   nickname=excluded.nickname, douyin_id=excluded.douyin_id, "
            "   following=excluded.following, followers=excluded.followers, "
            "   works=excluded.works, likes=excluded.likes, updated_at=excluded.updated_at",
            (account_id, _today_local(), nickname, douyin_id,
             stats["following"], stats["followers"], stats["works"], stats["likes"], _now()),
        )
        conn.commit()

    return {
        "success": True,
        "accountId": account_id,
        "nickname": nickname,
        "douyinId": douyin_id,
        "snapshotDay": _today_local(),
        **stats,
    }


@router.get("/api/accounts/profile-stats")
def profile_stats(accountId: str = "") -> dict:
    """查看某账号的每日快照（调试 / 对账用）。"""
    if not accountId:
        return {"success": False, "error": "accountId 必填"}
    with closing(sqlite3.connect(str(DB_PATH), timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, nickname, douyin_id, following, followers, works, likes, updated_at "
            "FROM account_stats_daily WHERE account_id = ? ORDER BY day",
            (accountId,),
        ).fetchall()
    return {"success": True, "snapshots": [dict(r) for r in rows]}
