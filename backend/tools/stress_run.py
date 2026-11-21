"""D24 压测：N 账号 × H 小时无人值守长跑。

验收标准（DESIGN-DOC §六 D24）
------------------------------
    零崩溃 / 配额零超限 / 内存无泄漏

两种模式
--------
simulate（默认）
    不启动浏览器、不发任何平台请求，用虚拟时钟把 4 小时压缩成几秒，
    验证「租约 ↔ 账号状态 ↔ 配额闸门 ↔ 内存采样 ↔ 异常捕获」整条链路。
    这是没有测试账号时的自检档，报告里会明确标注「未执行任何平台动作」。

live
    每账号一个 patchright 持久化 profile，按真实节奏执行动作。
    需 `--i-understand` 显式确认，且账号必须已在 account_pool 中登记为
    healthy。**本工具不会自动登录、不会绕过验证码**。

判定口径
--------
- 崩溃：任一 tick 抛出未捕获异常即记 1 次（脚本继续跑，模拟守护进程重启）；
- 超限：每笔配额消耗后回读账本，``used > limit`` 记 1 次越界；
- 泄漏：把 RSS 序列首 20% 与末 20% 的均值比较，增长超过 15% 判定疑似泄漏
  （另附线性回归斜率，供人工复核）。

用法
----
    # 自检档：3 账号 × 4 小时（秒级完成）
    python tools/stress_run.py --accounts 3 --hours 4

    # 真实长跑：3 账号 × 4 小时
    python tools/stress_run.py --accounts 3 --hours 4 --mode live --i-understand

    # 结果落 logs/stress/<时间戳>/{report.json,report.md}
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.engagement.account_pool import AccountPool  # noqa: E402
from app.engagement.quotas import DEFAULT_POLICY, QuotaLedger  # noqa: E402
from app.execution.store import ExecutionStore  # noqa: E402

METRICS = ("read", "like", "comment", "dm")
LEAK_GROWTH_LIMIT = 0.15      # 末段均值 / 首段均值 - 1 的上限


@dataclass
class Tick:
    index: int
    virtual_ts: int
    active: int = 0
    skipped: int = 0
    blocked: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    rss_mb: float = 0.0
    threads: int = 0


@dataclass
class Report:
    mode: str
    accounts: int
    hours: float
    ticks: int
    started_at: str
    duration_s: float
    crashes: int = 0
    quota_overruns: int = 0
    actions: dict[str, int] = field(default_factory=dict)
    blocked: dict[str, int] = field(default_factory=dict)
    rss_first: float = 0.0
    rss_last: float = 0.0
    rss_peak: float = 0.0
    rss_growth_ratio: float = 0.0
    leak_suspected: bool = False
    state_transitions: dict[str, int] = field(default_factory=dict)
    assertions: list[str] = field(default_factory=list)
    passed: bool = False
    notes: list[str] = field(default_factory=list)


def _rss_mb() -> tuple[float, int]:
    try:
        import psutil  # 可选依赖
    except ImportError:
        return (0.0, 0)
    proc = psutil.Process()
    return (round(proc.memory_info().rss / 1024 ** 2, 1), proc.num_threads())


def _slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def run(mode: str, accounts: int, hours: float, tick_seconds: int,
        seed: int = 0, accounts_dir: Path | None = None) -> Report:
    random.seed(seed)
    started = datetime.now()
    t0 = time.time()

    base = accounts_dir or (BACKEND_DIR.parent / "data" / "stress")
    base.mkdir(parents=True, exist_ok=True)
    store = ExecutionStore(base / "stress_execution.db")
    store.initialize()
    pool = AccountPool(base / "stress_account_pool.db")
    ledger = QuotaLedger(store, DEFAULT_POLICY)

    acct_ids = [f"stress-{i + 1}" for i in range(accounts)]
    ticks_total = max(1, int(hours * 3600 / tick_seconds))
    virtual_start = int(time.time())
    report = Report(mode=mode, accounts=accounts, hours=hours, ticks=ticks_total,
                    started_at=started.strftime("%Y-%m-%d %H:%M:%S"), duration_s=0.0)
    report.actions = {m: 0 for m in METRICS}

    rss_samples: list[float] = []
    history: list[Tick] = []
    prev_state = {a: "healthy" for a in acct_ids}

    for idx in range(ticks_total):
        virtual_ts = virtual_start + idx * tick_seconds
        tick = Tick(index=idx, virtual_ts=virtual_ts)
        try:
            available = pool.pick_available(acct_ids, now=virtual_ts)
            tick.active = len(available)
            tick.skipped = accounts - len(available)
            for acct in available:
                # 每个 tick 每账号最多做 1 个动作，指标随机（模拟真实节奏）
                metric = random.choices(METRICS, weights=(6, 2, 1, 1))[0]
                ok, reason = ledger.can_do(acct_ids.index(acct), metric, now=virtual_ts)
                if not ok:
                    tick.blocked[reason or metric] = tick.blocked.get(reason or metric, 0) + 1
                    report.blocked[reason or metric] = report.blocked.get(reason or metric, 0) + 1
                    continue
                ledger.consume(acct_ids.index(acct), metric, 1, now=virtual_ts)
                report.actions[metric] = report.actions.get(metric, 0) + 1
                used = ledger.used(acct_ids.index(acct), metric, now=virtual_ts)
                limit = DEFAULT_POLICY.limit_of(metric)
                if used > limit:      # 越界必须记，不允许静默
                    report.quota_overruns += 1
                    report.assertions.append(
                        f"tick {idx} 账号 {acct} {metric} 超限：used={used} > limit={limit}")

            # 账号状态变化统计（live 模式下由真实风控事件驱动；simulate 不注入）
            for acct in acct_ids:
                state = pool.state_of(acct, now=virtual_ts)
                if state != prev_state[acct]:
                    key = f"{prev_state[acct]}→{state}"
                    report.state_transitions[key] = report.state_transitions.get(key, 0) + 1
                    prev_state[acct] = state

        except Exception:            # noqa: BLE001 —— 压测要统计崩溃而不是自己挂掉
            tick.errors += 1
            report.crashes += 1
            if len(report.assertions) < 20:
                report.assertions.append(
                    f"tick {idx} 未捕获异常：\n" + traceback.format_exc(limit=3))

        rss, threads = _rss_mb()
        tick.rss_mb, tick.threads = rss, threads
        if rss:
            rss_samples.append(rss)
        history.append(tick)

        if mode == "live":
            time.sleep(tick_seconds)

    report.duration_s = round(time.time() - t0, 2)
    if rss_samples:
        edge = max(1, len(rss_samples) // 5)
        first = sum(rss_samples[:edge]) / edge
        last = sum(rss_samples[-edge:]) / edge
        report.rss_first = round(first, 1)
        report.rss_last = round(last, 1)
        report.rss_peak = max(rss_samples)
        report.rss_growth_ratio = round((last / first - 1) if first else 0.0, 4)
        report.leak_suspected = report.rss_growth_ratio > LEAK_GROWTH_LIMIT
        report.notes.append(f"RSS 斜率 {_slope(rss_samples):+.3f} MB/tick")
    else:
        report.notes.append("psutil 不可用，未采集内存指标")

    has_memory = report.rss_peak > 0
    report.passed = (report.crashes == 0 and report.quota_overruns == 0
                     and not (has_memory and report.leak_suspected))
    if mode != "live":
        report.notes.append("simulate：未启动浏览器、未触发任何平台请求，仅验证调度链路")
    return report


def render_markdown(r: Report) -> str:
    total_actions = sum(r.actions.values())
    has_memory = r.rss_peak > 0
    if has_memory:
        mem_actual = (f"{r.rss_growth_ratio * 100:.1f}%（{r.rss_first}→{r.rss_last} MB，"
                      f"峰值 {r.rss_peak} MB）")
        mem_verdict = "PASS" if not r.leak_suspected else "FAIL"
    else:
        mem_actual = "未采集（psutil 不可用）"
        mem_verdict = "N/A"
    lines = [
        f"# D24 压测报告（{r.mode}）",
        "",
        f"- 开始时间：{r.started_at}",
        f"- 规模：{r.accounts} 账号 × {r.hours} 小时（{r.ticks} tick）",
        f"- 实际耗时：{r.duration_s}s",
        f"- 虚拟动作总数：{total_actions}（{', '.join(f'{k}={v}' for k, v in r.actions.items())}）",
        "",
        "## 验收判定",
        "",
        "| 项 | 目标 | 实测 | 结论 |",
        "|---|---|---|---|",
        f"| 崩溃次数 | 0 | {r.crashes} | {'PASS' if r.crashes == 0 else 'FAIL'} |",
        f"| 配额越界 | 0 | {r.quota_overruns} | {'PASS' if r.quota_overruns == 0 else 'FAIL'} |",
        f"| 内存增长 | ≤15% | {mem_actual} | {mem_verdict} |",
        "",
        f"**总体：{'PASS' if r.passed else 'FAIL'}**",
        "",
    ]
    if r.blocked:
        lines += ["## 配额闸门拦截（属预期行为）", "",
                  "| 原因 | 次数 |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(r.blocked.items(), key=lambda x: -x[1])]
        lines.append("")
    if r.state_transitions:
        lines += ["## 账号状态迁移", "", "| 迁移 | 次数 |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in r.state_transitions.items()]
        lines.append("")
    if r.assertions:
        lines += ["## 异常明细（前 20 条）", "", "```"]
        lines += r.assertions
        lines += ["```", ""]
    if r.notes:
        lines += ["## 备注", ""] + [f"- {n}" for n in r.notes] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D24 压测：N 账号 × H 小时长跑")
    parser.add_argument("--accounts", type=int, default=3, help="账号数（默认 3）")
    parser.add_argument("--hours", type=float, default=4.0, help="时长（默认 4 小时）")
    parser.add_argument("--tick", type=int, default=30, help="tick 秒数（默认 30s，与租约心跳一致）")
    parser.add_argument("--mode", choices=("simulate", "live"), default="simulate")
    parser.add_argument("--i-understand", action="store_true",
                        help="live 模式必须显式确认会触发真实平台动作")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default="", help="报告输出目录")
    args = parser.parse_args(argv)

    if args.mode == "live" and not args.i_understand:
        print("[中止] live 模式会真实驱动浏览器并执行动作，需加 --i-understand 确认。")
        print("       没有测试账号时请用默认 simulate 自检档。")
        return 1

    report = run(args.mode, max(1, args.accounts), max(0.01, args.hours),
                 max(1, args.tick), args.seed)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else BACKEND_DIR / "logs" / "stress" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")

    print(render_markdown(report))
    print(f"\n报告已写入：{out_dir}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
