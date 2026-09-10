"""
数据库初始化模块
================
负责：
1. 确保 data/ 目录存在
2. 执行 schema.sql 建表
3. 初始化单例记录（runtime_state / 配置域）
4. 播种默认数据（compliance_rules / script_templates）
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.models.domain import now_utc

import os

# 项目根目录（app/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "lead_system.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_db_path() -> Path:
    """获取数据库文件路径，支持 DB_PATH 环境变量覆盖（C盘空间不足时放D盘）"""
    env_path = os.environ.get("DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def init_db(db_path: str | Path | None = None) -> Path:
    """初始化数据库：建目录、建表、播种默认数据

    Returns:
        数据库文件路径
    """
    if db_path is None:
        db_path = get_db_path()
    db_path = Path(db_path)

    # 1. 确保 data/ 目录存在
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 2. 连接并执行 schema
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")

        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema_sql)

        # 3.5 列迁移（为已存在的旧表补列，幂等）
        _migrate_columns(conn)

        # 4. 播种默认数据
        _seed_defaults(conn)

        conn.commit()
    finally:
        conn.close()

    return db_path


def _migrate_columns(conn: sqlite3.Connection) -> None:
    """为已存在的旧表补列（幂等，列已存在时跳过）"""
    def _has_column(table: str, col: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == col for r in rows)

    def _has_table(table: str) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    if not _has_column("messages", "is_read"):
        conn.execute("ALTER TABLE messages ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0")
    if not _has_column("messages", "script_id"):
        conn.execute("ALTER TABLE messages ADD COLUMN script_id TEXT")

    # ── 多账号调度模块迁移 ──
    if not _has_column("accounts", "weight"):
        conn.execute("ALTER TABLE accounts ADD COLUMN weight INTEGER NOT NULL DEFAULT 1")
    if not _has_column("accounts", "today_sent"):
        conn.execute("ALTER TABLE accounts ADD COLUMN today_sent INTEGER NOT NULL DEFAULT 0")
    if not _has_column("accounts", "today_success"):
        conn.execute("ALTER TABLE accounts ADD COLUMN today_success INTEGER NOT NULL DEFAULT 0")

    # scheduler_config 表（如不存在则创建）
    if not _has_table("scheduler_config"):
        conn.execute(
            """CREATE TABLE scheduler_config (
                id INTEGER PRIMARY KEY,
                strategy TEXT NOT NULL DEFAULT 'round_robin',
                health_threshold_warn INTEGER NOT NULL DEFAULT 60,
                health_threshold_critical INTEGER NOT NULL DEFAULT 30,
                max_concurrent INTEGER NOT NULL DEFAULT 3,
                updated_at TEXT NOT NULL
            )"""
        )


def _seed_defaults(conn: sqlite3.Connection) -> None:
    """播种默认单例记录和基础数据（仅在不存在时插入）"""
    now = now_utc().isoformat()

    # ── runtime_state 单例 ──
    conn.execute(
        """INSERT OR IGNORE INTO runtime_state
           (id, safe_mode_active, safe_mode_reason, safe_mode_triggered_at,
            safe_mode_trigger_source, task_running, updated_at)
           VALUES ('default', 0, '', NULL, NULL, 1, ?)""",
        (now,),
    )

    # ── compliance_rules 默认 R1/R2/R3 ──
    default_rules = [
        ("R1", "单账号日频 ≥ 80 → 降速至 40", "standby", None, None),
        ("R2", "话术变体加微转化率 < 8%（样本≥30）→ 自动切换", "standby", None, None),
        ("R3", "黑名单日增 > 3% 或账号封禁 → 全量暂停", "standby", None, None),
    ]
    conn.executemany(
        """INSERT OR IGNORE INTO compliance_rules
           (rule_id, desc, status, hit_at, target)
           VALUES (?, ?, ?, ?, ?)""",
        default_rules,
    )

    # ── script_templates 默认模板包 ──
    default_templates = [
        ("装修", 8, "首次触达×3 / 报价跟进×2 / 加微引导×3", 1),
        ("教育", 0, "v2 规划中", 0),
        ("医疗口腔", 0, "v2 规划中", 0),
    ]
    conn.executemany(
        """INSERT OR IGNORE INTO script_templates
           (industry, count, desc, installed)
           VALUES (?, ?, ?, ?)""",
        default_templates,
    )

    # ── 配置域单例（5张表各一条 default 记录）──
    conn.execute(
        """INSERT OR IGNORE INTO business_profile
           (id, industry, product, service_area, target_customer,
            price_range, conversion_goal, tone, updated_at)
           VALUES ('default', '', '', '', '', '', '添加微信', '专业、真诚', ?)""",
        (now,),
    )
    conn.execute(
        """INSERT OR IGNORE INTO product_knowledge
           (id, product_name, description, selling_points, target_customers,
            price_range, faq, forbidden_claims, updated_at)
           VALUES ('default', '', '', '', '', '', '', '', ?)""",
        (now,),
    )
    conn.execute(
        """INSERT OR IGNORE INTO audience_profile
           (id, name, industry, region, needs, pain_points,
            intent_keywords, excluded_keywords, updated_at)
           VALUES ('default', '', '', '', '', '', '', '', ?)""",
        (now,),
    )
    conn.execute(
        """INSERT OR IGNORE INTO script_strategy
           (id, comment_script, private_message_script, wechat_script,
            objection_script, updated_at)
           VALUES ('default', '', '', '', '', ?)""",
        (now,),
    )
    conn.execute(
        """INSERT OR IGNORE INTO wechat_settings
           (id, wechat_id, guide_timing, guide_reason, compliance_note, updated_at)
           VALUES ('default', '', '客户明确表达兴趣后', '发送详细方案和案例', '', ?)""",
        (now,),
    )

    # ── 调度器配置单例 ──
    conn.execute(
        """INSERT OR IGNORE INTO scheduler_config
           (id, strategy, health_threshold_warn, health_threshold_critical,
            max_concurrent, updated_at)
           VALUES (1, 'round_robin', 60, 30, 3, ?)""",
        (now,),
    )
