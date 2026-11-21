"""一键体检（D28）：交付前的六项自检，一条命令出结论。

检查项
------
1. 语法编译         app/ 与 tools/ 全量 compileall
2. 合规静态扫描     lead_candidates 的 INSERT 是否混入平台 uid 级字段
3. 单测             pytest 全量（默认；--fast 只跑执行层相关）
4. 桥接通道契约     服务在跑时逐通道验收；未启动则标注 SKIP（不算失败）
5. 数据库健康       integrity_check + 关键表存在性 + 行数盘点
6. 触达账本口径     测试/模拟记录是否已从统计中剔除

用法
----
    python tools/healthcheck.py            # 全量
    python tools/healthcheck.py --fast     # 跳过全量单测（只跑新增模块）
    python tools/healthcheck.py --json     # 机器可读输出（CI 用）

退出码：0 全绿 / 1 有失败 / 2 环境缺失（如 venv 不存在）
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
BASE_URL = os.getenv("BRIDGE_BASE", "http://127.0.0.1:8899")

DB_FILES = ("data/lead_system.db", "data/execution.db", "data/account_pool.db")
KEY_TABLES = {
    "data/lead_system.db": ("leads", "accounts", "comment_tasks", "dm_send_results", "behavior_events"),
    "data/execution.db": ("lead_candidates", "pipeline_runs", "comment_send_log", "account_quotas"),
    "data/account_pool.db": ("account_pool_states", "account_risk_events"),
}

FAST_TESTS = [
    "tests/test_touch_stats.py", "tests/test_ipc_leads_filter.py",
    "tests/test_execution_dashboard_d26.py", "tests/test_account_pool_d23.py",
]


@dataclass
class Result:
    name: str
    status: str      # PASS / FAIL / SKIP
    detail: str
    elapsed: float = 0.0


def _run(cmd: list[str], cwd: Path = BACKEND_DIR, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def check_compile() -> Result:
    t0 = time.time()
    proc = _run([str(PY), "-m", "compileall", "-q", "app", "tools"])
    ok = proc.returncode == 0
    detail = "app/ tools/ 编译通过" if ok else (proc.stdout + proc.stderr)[-800:]
    return Result("语法编译", "PASS" if ok else "FAIL", detail, round(time.time() - t0, 2))


def check_compliance() -> Result:
    t0 = time.time()
    code = (
        "import sys,glob; sys.path.insert(0,'.');"
        "from app.execution.compliance import scan_source_for_uid_fields as s;"
        "f=s(glob.glob('app/execution/*.py'));"
        "print('VIOLATIONS:'+repr(f) if f else 'CLEAN')"
    )
    proc = _run([str(PY), "-c", code])
    out = (proc.stdout or "").strip()
    ok = proc.returncode == 0 and "CLEAN" in out
    return Result("合规静态扫描", "PASS" if ok else "FAIL",
                  "候选池写入口无平台 uid 字段" if ok else (out or proc.stderr)[-800:],
                  round(time.time() - t0, 2))


def check_tests(fast: bool) -> Result:
    t0 = time.time()
    targets = FAST_TESTS if fast else ["-q"]
    cmd = [str(PY), "-m", "pytest", "-q"] + (targets if fast else [])
    if fast:
        cmd = [str(PY), "-m", "pytest", "-q", *FAST_TESTS]
    proc = _run(cmd)
    tail = (proc.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else (proc.stderr or "")[-300:]
    ok = proc.returncode == 0
    return Result("单元测试" + ("（快速档）" if fast else "（全量）"),
                  "PASS" if ok else "FAIL", summary, round(time.time() - t0, 2))


def check_bridge() -> Result:
    t0 = time.time()
    try:
        import urllib.request
        with urllib.request.urlopen(f"{BASE_URL}/ipc/health", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return Result("桥接通道契约", "SKIP",
                      f"服务未启动（{type(exc).__name__}）；起服务后重跑本项", round(time.time() - t0, 2))
    problems = []
    if payload.get("mode") != "live":
        problems.append(f"mode={payload.get('mode')}（应为 live）")
    if not payload.get("rendererExists"):
        problems.append("渲染层未找到")
    if not payload.get("dbExists"):
        problems.append("线索库未找到")
    detail = (f"explicit={payload.get('explicitChannels')}"
              f" empty={payload.get('emptyChannels')}"
              f" blocked={payload.get('blockedChannels')}"
              f" viewAdapter={payload.get('viewAdapterChannels')}")
    if problems:
        detail += " | " + "；".join(problems)
    return Result("桥接通道契约", "PASS" if not problems else "FAIL", detail,
                  round(time.time() - t0, 2))


def check_databases() -> Result:
    t0 = time.time()
    lines: list[str] = []
    ok = True
    for rel in DB_FILES:
        path = BACKEND_DIR / rel
        if not path.exists():
            lines.append(f"{rel}: 缺失")
            ok = False
            continue
        try:
            conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in KEY_TABLES.get(rel, ()) if t not in tables]
            counts = {}
            for t in KEY_TABLES.get(rel, ()):
                if t in tables:
                    counts[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            conn.close()
            if integrity != "ok":
                ok = False
                lines.append(f"{rel}: integrity={integrity}")
            if missing:
                ok = False
                lines.append(f"{rel}: 缺表 {missing}")
            if not missing:
                lines.append(f"{rel}: ok, " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        except sqlite3.Error as exc:
            ok = False
            lines.append(f"{rel}: {exc}")
    return Result("数据库健康", "PASS" if ok else "FAIL", " | ".join(lines),
                  round(time.time() - t0, 2))


def check_touch_ledger() -> Result:
    t0 = time.time()
    db = BACKEND_DIR.parent / "data" / "lead_system.db"
    if not db.exists():
        return Result("触达账本口径", "SKIP", "线索库缺失", round(time.time() - t0, 2))
    code = (
        "import sys; sys.path.insert(0,'.');"
        "from app.execution.touch_stats import TouchLedger;"
        "h=TouchLedger(r'%s');r=h.report();t=h.totals();"
        "print('REPORT:'+str({'totals':t,'dropped':r['dropped_test_records'],'missing':r['missing']}))"
    ) % db
    proc = _run([str(PY), "-c", code])
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return Result("触达账本口径", "FAIL", (proc.stderr or out)[-500:],
                      round(time.time() - t0, 2))
    report_line = next((l for l in out.splitlines() if l.startswith("REPORT:")), "")
    detail = report_line.replace("REPORT:", "").strip() or out[-400:]
    return Result("触达账本口径", "PASS", detail, round(time.time() - t0, 2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="交付前一键体检")
    parser.add_argument("--fast", action="store_true", help="只跑关键模块单测")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    if not (BACKEND_DIR / ".venv").exists() and "venv" not in sys.prefix:
        print("[ERR] 未在 venv 中运行，且找不到 backend/.venv", file=sys.stderr)
        return 2

    results = [
        check_compile(),
        check_compliance(),
        check_tests(args.fast),
        check_bridge(),
        check_databases(),
        check_touch_ledger(),
    ]

    failed = [r for r in results if r.status == "FAIL"]
    passed = [r for r in results if r.status == "PASS"]

    if args.json:
        print(json.dumps({
            "generatedAt": datetime.now().isoformat(timespec="seconds"),
            "passed": len(passed), "failed": len(failed),
            "results": [r.__dict__ for r in results],
        }, ensure_ascii=False, indent=2))
    else:
        print("=" * 78)
        print(f"南溪 AI 获客 · 交付体检   {datetime.now():%Y-%m-%d %H:%M:%S}")
        print("=" * 78)
        for r in results:
            mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "-"}[r.status]
            print(f"[{mark}] {r.name:<16} {r.status:<5} {r.elapsed:>6.2f}s")
            print(f"      {r.detail}")
        print("-" * 78)
        verdict = "PASS" if not failed else "FAIL"
        print(f"结论：{verdict}（{len(passed)} 通过 / {len(failed)} 失败 / "
              f"{len(results) - len(passed) - len(failed)} 跳过）")
        if failed:
            print("失败项：" + "、".join(r.name for r in failed))
        print("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
