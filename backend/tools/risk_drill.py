"""D25 风控对抗演练：人为触发疑似风控，验证降级与告警。

验收标准（DESIGN-DOC §六 D25）
------------------------------
    系统自动 cooling 并停任务

演练编排（逐步断言，任一步失败即整体 FAIL）
--------------------------------------------
1. captcha            → cooling（2h），租约池不再派发该账号
2. cooling 到期        → 读时自动恢复 healthy
3. send_fail_403      → cooling（4h）
4. send_uncertain ×3   → 风险积分 3 → risk_warning
5. risk_warning + 再挂一次 → suspended（人工恢复才解除）
6. suspended          → 长期不派发；manual_recover 后恢复
7. 停任务联动          → 无可用账号时 pick_available 为空，
                         编排器/worker 拿不到租约 = 任务实际停摆

默认在**临时副本**上演练，不污染生产账号池状态；`--inplace` 才操作真实库
（真实演练会写 account_risk_events，属审计记录，建议保留）。

用法
----
    python tools/risk_drill.py                # 沙盘演练（默认）
    python tools/risk_drill.py --inplace      # 真实账号池演练
    python tools/risk_drill.py --db data/account_pool.db --out logs/drills
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.engagement.account_pool import (  # noqa: E402
    COOLING_HOURS_403,
    COOLING_HOURS_CAPTCHA,
    RISK_WARNING_THRESHOLD,
    STATE_COOLING,
    STATE_HEALTHY,
    STATE_RISK_WARNING,
    STATE_SUSPENDED,
    AccountPool,
)

DEFAULT_DB = BACKEND_DIR.parent / "data" / "account_pool.db"
ACCT = "drill-account"


@dataclass
class Step:
    name: str
    expect: str
    actual: str
    passed: bool


@dataclass
class DrillReport:
    mode: str
    db: str
    started_at: str
    steps: list[Step] = field(default_factory=list)
    passed: bool = False

    def add(self, name: str, expect: str, actual: str, passed: bool) -> None:
        self.steps.append(Step(name, expect, actual, passed))


def _dispatched(pool: AccountPool, now: int) -> bool:
    """账号是否会被派发（= 能否拿到任务租约）。"""
    return ACCT in pool.pick_available([ACCT], now=now)


def run_drill(pool: AccountPool, report: DrillReport) -> None:
    now = int(time.time())

    # 1) 验证码 → cooling 2h
    out = pool.report_event(ACCT, "captcha", detail="演练：登录后触发滑块验证", now=now)
    expected_until = now + int(COOLING_HOURS_CAPTCHA * 3600)
    ok = (out["state"] == STATE_COOLING
          and out["cooling_until"] == expected_until
          and not _dispatched(pool, now))
    report.add("captcha → cooling",
               f"state=cooling, until=now+{COOLING_HOURS_CAPTCHA}h, 不派发",
               f"state={out['state']}, until=now+{(out['cooling_until'] or now) - now}s, "
               f"派发={_dispatched(pool, now)}", ok)

    # 2) 冷却到期自动恢复
    after = expected_until + 1
    state_after = pool.state_of(ACCT, now=after)
    ok = state_after == STATE_HEALTHY and _dispatched(pool, after)
    report.add("cooling 到期自动恢复", "state=healthy 且重新派发",
               f"state={state_after}, 派发={_dispatched(pool, after)}", ok)

    # 3) 403 → cooling 4h
    now2 = after + 10
    out = pool.report_event(ACCT, "send_fail_403", detail="演练：HTTP 403 风控拦截", now=now2)
    ok = (out["state"] == STATE_COOLING
          and out["cooling_until"] == now2 + int(COOLING_HOURS_403 * 3600))
    report.add("send_fail_403 → cooling 4h", f"state=cooling, until=now+{COOLING_HOURS_403}h",
               f"state={out['state']}, until=now+{(out['cooling_until'] or now2) - now2}s", ok)

    # 4) 连续 uncertain 累计到阈值 → risk_warning
    now3 = now2 + int(COOLING_HOURS_403 * 3600) + 1     # 先跳过冷却期
    points = 0
    for i in range(RISK_WARNING_THRESHOLD):
        out = pool.report_event(ACCT, "send_uncertain",
                                detail=f"演练：写入结果不确定 #{i + 1}", now=now3 + i)
        points = out["risk_points"]
    ok = out["state"] == STATE_RISK_WARNING and points >= RISK_WARNING_THRESHOLD
    report.add(f"send_uncertain ×{RISK_WARNING_THRESHOLD} → risk_warning",
               f"state=risk_warning, risk_points>={RISK_WARNING_THRESHOLD}",
               f"state={out['state']}, risk_points={points}", ok)

    # 5) risk_warning 期间再挂 → suspended
    now4 = now3 + RISK_WARNING_THRESHOLD + 1
    out = pool.report_event(ACCT, "send_uncertain", detail="演练：警告期再次不确定", now=now4)
    ok = out["state"] == STATE_SUSPENDED and bool(out["suspend_reason"])
    report.add("risk_warning 期间再出风险 → suspended", "state=suspended 且带原因",
               f"state={out['state']}, reason={out['suspend_reason'] or '-'}", ok)

    # 6) suspended 不自动解除，只能人工恢复
    far = now4 + 7 * 24 * 3600
    stayed = pool.state_of(ACCT, now=far)
    dispatched_before = _dispatched(pool, far)
    ok_blocked = stayed == STATE_SUSPENDED and not dispatched_before
    ok_recover = False
    recovered_state = "-"
    if ok_blocked:
        rec = pool.report_event(ACCT, "manual_recover", detail="演练：人工确认恢复", now=far)
        recovered_state = rec["state"]
        ok_recover = rec["state"] == STATE_HEALTHY and _dispatched(pool, far)
    report.add("suspended 七天不自动解除 + 人工恢复生效",
               "7 天后仍 suspended 且不派发；manual_recover 后恢复 healthy",
               f"7 天后 state={stayed}, 派发={dispatched_before}, "
               f"人工恢复后={recovered_state}",
               ok_blocked and ok_recover)

    # 7) 停任务联动：唯一账号不可用 → 租约池为空 → 编排器拿不到租约 = 任务停摆
    now5 = far + 5
    pool.report_event(ACCT, "manual_suspend", detail="演练：模拟长期封停", now=now5)
    available = pool.pick_available([ACCT], now=now5)
    ok = available == []
    report.add("停任务联动（无可用账号 → 无租约 → 任务停摆）",
               "pick_available 返回空列表",
               f"pick_available={available}", ok)

    # 收尾：恢复现场，避免后续演练/真实运行被卡住
    pool.report_event(ACCT, "manual_recover", detail="演练收尾：恢复", now=now5 + 1)

    # 审计留痕检查
    events = pool.events_of(ACCT, limit=50)
    kinds = {e["kind"] for e in events}
    ok = {"captcha", "send_fail_403", "send_uncertain", "manual_recover"} <= kinds
    report.add("风控事件审计留痕", "account_risk_events 记录全部事件类型",
               f"事件数={len(events)}, 类型={sorted(kinds)}", ok)


def render_markdown(r: DrillReport) -> str:
    lines = [
        f"# D25 风控对抗演练记录（{r.mode}）",
        "",
        f"- 开始时间：{r.started_at}",
        f"- 演练库：{r.db}",
        "",
        "| # | 演练项 | 预期 | 实测 | 结论 |",
        "|---|---|---|---|---|",
    ]
    for i, s in enumerate(r.steps, 1):
        lines.append(f"| {i} | {s.name} | {s.expect} | {s.actual} | "
                     f"{'PASS' if s.passed else 'FAIL'} |")
    lines += ["", f"**总体：{'PASS' if r.passed else 'FAIL'}**", ""]
    if r.mode != "inplace":
        lines.append("- 沙盘模式：在生产库副本上演练，真实账号池状态未被修改。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D25 风控对抗演练")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="账号池库路径")
    parser.add_argument("--inplace", action="store_true",
                        help="直接在真实账号池上演练（会写审计事件）")
    parser.add_argument("--out", default="", help="记录输出目录")
    args = parser.parse_args(argv)

    src = Path(args.db).expanduser().resolve()
    tmpdir = None
    if args.inplace:
        db_path = src
        mode = "inplace"
        if not src.exists():
            print(f"[ERR] 账号池库不存在：{src}（先跑一次后端生成）")
            return 2
    else:
        tmpdir = tempfile.mkdtemp(prefix="risk-drill-")
        db_path = Path(tmpdir) / "account_pool.db"
        if src.exists():
            shutil.copy2(src, db_path)
        mode = "sandbox(副本)"

    report = DrillReport(mode=mode, db=str(db_path),
                         started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    pool = AccountPool(db_path)
    run_drill(pool, report)
    report.passed = all(s.passed for s in report.steps) and bool(report.steps)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else BACKEND_DIR / "logs" / "drills" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "drill.json").write_text(
        json.dumps({"mode": report.mode, "db": report.db, "started_at": report.started_at,
                    "passed": report.passed,
                    "steps": [s.__dict__ for s in report.steps]},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "drill.md").write_text(render_markdown(report), encoding="utf-8")

    print(render_markdown(report))
    print(f"\n记录已写入：{out_dir}")
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
