"""清理生产库里的演示/种子数据（默认只演练，加 --apply 才真删）。

删什么（全部按可复核的条件，不按行号）：
  1. leads            —— 昵称「日报客户N」40 条 + 视频名「AI获客系统演示*」100 条，共 140 条。
                         这批是 seed / 早期演示导入写进来的假客户，用户从未采集过。
  2. behavior_events  —— 挂在这 140 条线索上的行为事件（不删会留下孤儿行，
                         大屏「已触达」统计会继续把它们算进去）。
  3. notifications    —— 2026-09 期间写入的 18 条种子公告（「今日跟进·王总」
                         「成交喜报」「集成测试」这类），全是开库时一次性写入的虚构提醒。

不删什么：tasks / crawl_tasks / accounts 等业务记录；任务型演示记录（task_type='demo'）
只统计不删除，等确认后再单独处理。

CRM 演示数据（customers 120 / followups 59）**默认只报数不删**：
它们不是按名字能识别的种子行，而是开库时一次性写入的虚构客户（名字是
「张先生（AI创业公司）」这类人设，created_at 全部落在 2026-09-16/18，早于
任何一次真实采集）。删掉会把「客户管理 / 跟进」两页清空，且消息中心那些
「今日跟进」提醒就是 followups 巡检按它们生成、删表还会重建——所以要不要清
必须由人决定，加 --include-crm 才执行。

先备份再删：备份落在 data/_backups/lead_system.<时间戳>.db，删完打印逐表前后行数。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.paths import DATA_DIR  # noqa: E402

DB = DATA_DIR / "lead_system.db"
BACKUP_DIR = DATA_DIR / "_backups"

DEMO_LEAD_SQL = (
    "COALESCE(nickname,'') LIKE '日报客户%' "
    "OR COALESCE(video,'') LIKE 'AI获客系统演示%'"
)
SEED_NOTICE_SQL = "created_at >= '2026-09-01' AND created_at < '2026-10-01'"

TABLES = ("leads", "behavior_events", "notifications")
CRM_TABLES = ("customers", "followups")


def _crm_counts(con: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for table in CRM_TABLES:
        try:
            out[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = 0
    return out


def crm_report(con: sqlite3.Connection) -> None:
    """CRM 演示数据画像：只报数 + 抽样，不删。"""
    counts = _crm_counts(con)
    print("\n—— CRM 演示数据（默认不删，需 --include-crm 才清） ——")
    print(f"客户 customers   {counts['customers']} 条")
    print(f"跟进 followups   {counts['followups']} 条")
    try:
        for row in con.execute(
                "SELECT substr(created_at,1,10) d, COUNT(*) c FROM customers "
                "GROUP BY d ORDER BY d LIMIT 6"):
            print(f"   {row[0]} 建 {row[1]} 条")
        print("   抽样：", "、".join(
            str(r[0])[:16] for r in con.execute("SELECT name FROM customers LIMIT 5")))
        pending = con.execute(
            "SELECT COUNT(*) FROM notifications WHERE related_type='followup'").fetchone()[0]
        if pending:
            print(f"   当前 {pending} 条消息中心提醒由跟进记录生成（清跟进后会一起消失）")
    except sqlite3.Error as exc:
        print(f"   （画像读取失败：{exc}）")


def crm_apply(con: sqlite3.Connection) -> dict[str, int]:
    removed = {"customers": 0, "followups": 0, "notifications": 0}
    removed["followups"] = con.execute("DELETE FROM followups").rowcount
    removed["customers"] = con.execute("DELETE FROM customers").rowcount
    removed["notifications"] = con.execute(
        "DELETE FROM notifications WHERE related_type IN ('followup','customer')").rowcount
    con.commit()
    return removed


def counts(con: sqlite3.Connection) -> dict[str, int]:
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def report(con: sqlite3.Connection) -> None:
    leads = con.execute(f"SELECT COUNT(*) FROM leads WHERE {DEMO_LEAD_SQL}").fetchone()[0]
    events = con.execute(
        "SELECT COUNT(*) FROM behavior_events WHERE lead_id IN "
        f"(SELECT id FROM leads WHERE {DEMO_LEAD_SQL})").fetchone()[0]
    notices = con.execute(
        f"SELECT COUNT(*) FROM notifications WHERE {SEED_NOTICE_SQL}").fetchone()[0]
    print(f"待删线索        {leads} 条")
    print(f"待删行为事件    {events} 条（挂在这些线索上）")
    print(f"待删种子公告    {notices} 条（2026-09 期间写入）")
    print("\n公告标题抽样：")
    for row in con.execute(
            f"SELECT created_at, title FROM notifications WHERE {SEED_NOTICE_SQL} "
            "ORDER BY created_at LIMIT 6"):
        print(f"  {str(row[0])[:10]}  {str(row[1])[:40]}")
    demo_tasks = con.execute(
        "SELECT COUNT(*) FROM tasks WHERE task_type='demo'").fetchone()[0]
    if demo_tasks:
        print(f"\n另发现 tasks 里 {demo_tasks} 条 task_type='demo' 记录（未列入本次删除，仅提示）")


def apply(con: sqlite3.Connection) -> dict[str, int]:
    ids = [r[0] for r in con.execute(f"SELECT id FROM leads WHERE {DEMO_LEAD_SQL}")]
    removed = {"leads": 0, "behavior_events": 0, "notifications": 0}
    if ids:
        marks = ",".join("?" * len(ids))
        removed["behavior_events"] = con.execute(
            f"DELETE FROM behavior_events WHERE lead_id IN ({marks})", ids).rowcount
        removed["leads"] = con.execute(
            f"DELETE FROM leads WHERE id IN ({marks})", ids).rowcount
    removed["notifications"] = con.execute(
        f"DELETE FROM notifications WHERE {SEED_NOTICE_SQL}").rowcount
    con.commit()
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删（默认只演练）")
    ap.add_argument("--include-crm", action="store_true",
                    help="连 customers / followups 一起清（默认只报数，不动）")
    args = ap.parse_args()

    if not DB.is_file():
        print(f"库不存在：{DB}")
        return 2

    con = sqlite3.connect(str(DB))
    try:
        before = counts(con)
        crm_before = _crm_counts(con) if args.include_crm else {}
        print(f"库：{DB}\n清理前：" + "，".join(f"{k} {v}" for k, v in before.items()) + "\n")
        report(con)
        if args.include_crm:
            crm_report(con)
        else:
            crm = _crm_counts(con)
            print(f"\nCRM 演示数据：customers {crm['customers']} / followups {crm['followups']}"
                  "（默认不动，确认后加 --include-crm）")
        if not args.apply:
            print("\n[dry-run] 未做任何改动。加 --apply 执行。")
            return 0

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"lead_system.{time.strftime('%Y%m%d-%H%M%S')}.db"
        shutil.copy2(DB, backup)
        print(f"\n已备份 → {backup}")

        removed = apply(con)
        after = counts(con)
        print("\n删除结果：" + "，".join(f"{k} -{v}" for k, v in removed.items()))
        print("清理后：" + "，".join(f"{k} {v}" for k, v in after.items()))
        drift = {k: before[k] - after[k] - removed[k] for k in before}
        bad = {k: v for k, v in drift.items() if v}
        print("行数核对：", "一致" if not bad else f"不一致 {bad}")
        if args.include_crm:
            crm_removed = crm_apply(con)
            crm_after = _crm_counts(con)
            print("CRM 删除结果：" + "，".join(f"{k} -{v}" for k, v in crm_removed.items()))
            print("CRM 清理后：" + "，".join(f"{k} {v}" for k, v in crm_after.items()))
            crm_bad = {k: crm_before.get(k, 0) - crm_after[k] - crm_removed[k]
                       for k in crm_after if k != "notifications"}
            crm_bad = {k: v for k, v in crm_bad.items() if v}
            if crm_bad:
                print("CRM 行数核对：不一致", crm_bad)
                bad.update(crm_bad)
        return 0 if not bad else 3
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
