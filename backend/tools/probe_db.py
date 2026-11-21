"""数据库体检：表完整性 / 空表 / 填充率 / 孤儿引用 / 代码引用但不存在的表。

只读，不修改任何数据。
"""
from __future__ import annotations

import collections
import os
import re
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
from app.paths import DB_PATH  # noqa: E402

con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
views = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name")]
indexes = collections.defaultdict(list)
for r in con.execute("SELECT tbl_name, name FROM sqlite_master WHERE type='index'"):
    indexes[r[0]].append(r[1])

print(f"DB 文件: {DB_PATH}  ({DB_PATH.stat().st_size / 1024 / 1024:.2f} MB)")
print(f"表 {len(tables)} 个 / 视图 {len(views)} 个\n")

print(f"{'表名':34} {'行数':>8}  {'索引':>4}  状态")
print("-" * 74)
empty, populated = [], []
for t in tables:
    n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    (populated if n else empty).append((t, n))
    flag = "" if n else "空表"
    print(f"{t:34} {n:8}  {len(indexes[t]):4}  {flag}")

# ---- 代码里引用的表名 vs 库里实际存在 ----
code = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                 for p in (BACKEND / "app").rglob("*.py"))
refs = set()
for pat in (r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([a-z_][a-z0-9_]*)",
            r"UPDATE\s+([a-z_][a-z0-9_]*)\s+SET",
            r"DELETE\s+FROM\s+([a-z_][a-z0-9_]*)",
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)",
            r"FROM\s+([a-z_][a-z0-9_]*)",
            r"JOIN\s+([a-z_][a-z0-9_]*)",
            r"ALTER\s+TABLE\s+([a-z_][a-z0-9_]*)",
            r"INTO\s+([a-z_][a-z0-9_]*)"):
    refs |= {m.lower() for m in re.findall(pat, code, re.I)}
stop = {"dual", "sqlite_master", "sqlite_sequence", "select", "where", "set", "values"}
refs -= stop
missing = sorted(refs - set(tables))
print(f"\n--- 代码引用但库里不存在（运行到就 OperationalError）: {len(missing)} ---")
for t in missing:
    print(" ", t)

unused = sorted(set(tables) - refs)
print(f"\n--- 库里有表但代码没引用（可能是历史遗留）: {len(unused)} ---")
print(" ", " ".join(unused) if unused else "无")

# ---- 关键字段填充率 ----
print("\n--- leads 字段填充率 ---")
cols = [r[1] for r in con.execute("PRAGMA table_info(leads)")]
total = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
for c in cols:
    nn = con.execute(f'SELECT COUNT(*) FROM leads WHERE "{c}" IS NOT NULL AND "{c}" != \'\'').fetchone()[0]
    pct = nn / total * 100 if total else 0
    if pct < 100:
        print(f"  {c:22} {nn:6}/{total:<6} {pct:5.1f}%")

# ---- 孤儿引用 ----
def has(t):
    return t in tables


print("\n--- 孤儿引用检查 ---")
checks = [
    ("leads.account_id → accounts.id", "SELECT COUNT(*) FROM leads WHERE account_id IS NOT NULL AND account_id!='' AND account_id NOT IN (SELECT id FROM accounts)"),
    ("account_events.account_id → accounts.id", "SELECT COUNT(*) FROM account_events WHERE account_id NOT IN (SELECT id FROM accounts)"),
    ("dm_send_results.account_id → accounts.id", "SELECT COUNT(*) FROM dm_send_results WHERE account_id IS NOT NULL AND account_id!='' AND account_id NOT IN (SELECT id FROM accounts)"),
    ("leads 无评论内容", "SELECT COUNT(*) FROM leads WHERE comment IS NULL OR TRIM(comment)=''"),
    ("leads 无昵称", "SELECT COUNT(*) FROM leads WHERE nickname IS NULL OR TRIM(nickname)=''"),
    ("leads score 为空", "SELECT COUNT(*) FROM leads WHERE score IS NULL"),
]
for label, sql in checks:
    try:
        print(f"  {label:44} {con.execute(sql).fetchone()[0]}")
    except sqlite3.OperationalError as exc:
        print(f"  {label:44} 跳过（{exc}）")

# ---- leads 来源分布 ----
print("\n--- leads 来源分布 ---")
try:
    for r in con.execute("SELECT COALESCE(source,'(null)') s, COUNT(*) n, MAX(created_at) last "
                         "FROM leads GROUP BY s ORDER BY n DESC"):
        print(f"  {r['s']:22} {r['n']:5}  last={r['last']}")
except sqlite3.OperationalError:
    print("  无 source 列")

print("\n--- 状态分布 ---")
try:
    for r in con.execute("SELECT COALESCE(status,'(null)') s, COUNT(*) n FROM leads GROUP BY s ORDER BY n DESC"):
        print(f"  {r['s']:22} {r['n']:5}")
except sqlite3.OperationalError:
    print("  无 status 列")

# ---- 运行期表是否已有数据 ----
print("\n--- 运行期表 ---")
for t in ("task_runtime_logs", "leadgen_pools", "bridge_events", "system_settings",
          "accounts", "licenses", "account_stats_daily", "dm_send_results"):
    if t in tables:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  {t:24} {n:6} 行")
    else:
        print(f"  {t:24} 表不存在")

con.close()
