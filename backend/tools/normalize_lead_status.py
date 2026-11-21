"""线索状态归一工具（中文脏数据 → 英文状态机）。

背景
----
``leads.status`` 里混入了 70 条中文值：需求沟通 25 / 方案中 20 / 已报价 15 /
谈判中 10。追查后确认它们不是线索状态，而是**南溪 CRM 的客户阶段**
（``customers.stage`` 的 added/discovery/proposal/quoted/negotiating/won）
在历史数据迁移时被写进了线索表：

    customers.stage   discovery 25 | proposal 19 | quoted 15 | negotiating 10
    leads.status      需求沟通 25 | 方案中  20 | 已报价 15 | 谈判中    10

两者数量一一对应，可确认是同一批数据的两个投影。

因此归一口径为：**客户阶段归 customers，线索状态归 leads**。
线索层面只保留粗粒度状态，具体商务阶段以 customers.stage 为准。

映射表
------
========  ==========  ============================================
原值      归一为      理由
========  ==========  ============================================
需求沟通  contacted   已建立沟通，需求未定档
方案中    qualified   已确认需求并进入方案阶段
已报价    qualified   报价属意向推进；报价金额不回填，避免臆造
谈判中    qualified   进入商务谈判，同属意向确认后推进
========  ==========  ============================================

原值不丢弃：写入 ``note`` 尾部标记 ``【状态归一】原状态=xxx yyyy-mm-dd``，
便于对账与回滚。

用法
----
    # 只读预演（默认，不改库）
    python tools/normalize_lead_status.py

    # 实际执行（先自动备份，再单事务更新）
    python tools/normalize_lead_status.py --apply --yes

    # 指定其它库
    python tools/normalize_lead_status.py --db data/lead_system.db --apply --yes
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 中文客户阶段 → 线索状态
STATUS_MAP: dict[str, str] = {
    "需求沟通": "contacted",
    "方案中": "qualified",
    "已报价": "qualified",
    "谈判中": "qualified",
}

# 与 customers.stage 的对应关系（仅用于报告，不参与写库）
CUSTOMER_STAGE_HINT: dict[str, str] = {
    "需求沟通": "discovery",
    "方案中": "proposal",
    "已报价": "quoted",
    "谈判中": "negotiating",
}

NOTE_MARK = "【状态归一】"

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "lead_system.db"


@dataclass(frozen=True)
class PlanRow:
    lead_id: str
    nickname: str
    old_status: str
    new_status: str


def plan(db_path: Path) -> list[PlanRow]:
    """只读扫描：返回将要变更的行。"""
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        placeholders = ",".join("?" * len(STATUS_MAP))
        rows = conn.execute(
            f"SELECT id, nickname, status FROM leads WHERE status IN ({placeholders}) ORDER BY status, id",
            tuple(STATUS_MAP),
        ).fetchall()
    finally:
        conn.close()
    return [PlanRow(r[0], r[1] or "", r[2], STATUS_MAP[r[2]]) for r in rows]


def summarize(rows: list[PlanRow]) -> dict[str, dict[str, int]]:
    """{原状态: {目标状态: 条数}}"""
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        out.setdefault(row.old_status, {})
        out[row.old_status][row.new_status] = out[row.old_status].get(row.new_status, 0) + 1
    return out


def apply(db_path: Path, rows: list[PlanRow], *, backup: bool = True) -> Path | None:
    """单事务更新；默认先做整库冷备。返回备份路径。"""
    if not rows:
        return None
    backup_path = None
    if backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = db_path.with_suffix(db_path.suffix + f".bak-{stamp}")
        shutil.copy2(db_path, backup_path)

    stamp_text = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        with conn:
            for row in rows:
                note_patch = f"{NOTE_MARK}原状态={row.old_status} {stamp_text}"
                conn.execute(
                    "UPDATE leads SET status=?, updated_at=?,"
                    " note=CASE WHEN note IS NULL OR note='' THEN ?"
                    "           ELSE note || char(10) || ? END"
                    " WHERE id=? AND status=?",
                    (row.new_status, datetime.now().isoformat(timespec="seconds"),
                     note_patch, note_patch, row.lead_id, row.old_status),
                )
    finally:
        conn.close()
    return backup_path


def remaining(db_path: Path) -> dict[str, int]:
    """执行后复查：还剩多少非英文状态。"""
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM leads GROUP BY status ORDER BY 2 DESC"
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]): int(r[1]) for r in rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="线索状态归一（中文客户阶段 → 英文线索状态）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="lead_system.db 路径")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认只预演）")
    parser.add_argument("--yes", action="store_true", help="写库确认（--apply 时必填）")
    parser.add_argument("--no-backup", action="store_true", help="跳过自动备份（不建议）")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"[ERR] 库不存在：{db_path}")
        return 2

    rows = plan(db_path)
    groups = summarize(rows)
    print(f"库：{db_path}")
    print(f"待归一：{len(rows)} 条\n")
    if not rows:
        print("无需处理，所有状态已是英文状态机取值。")
        return 0

    print(f"{'原状态':<10}{'目标状态':<12}{'条数':<6}客户阶段(参考)")
    for old, targets in groups.items():
        for new, count in targets.items():
            print(f"{old:<10}{new:<12}{count:<6}{CUSTOMER_STAGE_HINT.get(old, '-')}")
    print("\n样例（前 5 条）：")
    for row in rows[:5]:
        print(f"  {row.lead_id}  {row.nickname[:12]:<14} {row.old_status} → {row.new_status}")

    if not args.apply:
        print("\n[预演] 未改动任何数据。确认口径后加 --apply --yes 执行。")
        return 0
    if not args.yes:
        print("\n[中止] --apply 需同时加 --yes 确认。")
        return 1

    backup_path = apply(db_path, rows, backup=not args.no_backup)
    if backup_path:
        print(f"\n[备份] {backup_path}")
    print(f"[完成] 已归一 {len(rows)} 条。")
    after = remaining(db_path)
    chinese = {k: v for k, v in after.items() if any("\u4e00" <= ch <= "\u9fff" for ch in k)}
    print("复查：剩余中文状态 " + ("无" if not chinese else str(chinese)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
