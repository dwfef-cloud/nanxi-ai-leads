"""真机验收：三类运行时 + 三种临时停，全部打在**运行中的实例**上。

与 pytest 的分工
----------------
pytest 验的是逻辑契约（窗口判定、幂等、去重、不伪造）。这里验的是另一件事：
**这台机器此刻真的能不能跑**——桌面壳在不在、抖音登录态在不在、选择器还准不准、
到点以后是不是真的有人去执行了。

所以本脚本只走 HTTP（/ipc/*、/api/*），不 import 任何后端内部对象：
它看到的证据 = 用户在页面上能看到的证据。

用法（桌面壳必须先打开，后端在 8899）：
    .venv/Scripts/python.exe tools/acceptance_real.py --keyword "AI客服"
    .venv/Scripts/python.exe tools/acceptance_real.py --keyword "获客" --skip-switches

输出约定：每条 [PASS] / [FAIL] / [SKIP] 都带原因；最后汇总退出码。
脚本自己清理排期，避免留下无人值守的定时任务。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

BASE = "http://127.0.0.1:8899"

_RESULTS: list[tuple[str, str, str]] = []


def _pass(name: str, detail: str = "") -> None:
    _RESULTS.append(("PASS", name, detail))
    print(f"[PASS] {name}" + (f" —— {detail}" if detail else ""))


def _fail(name: str, detail: str = "") -> None:
    _RESULTS.append(("FAIL", name, detail))
    print(f"[FAIL] {name}" + (f" —— {detail}" if detail else ""))


def _skip(name: str, detail: str = "") -> None:
    _RESULTS.append(("SKIP", name, detail))
    print(f"[SKIP] {name}" + (f" —— {detail}" if detail else ""))


def _req(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        return json.loads(raw)
    except ValueError:
        return {"success": False, "error": raw[:200]}


def ipc(channel: str, payload: dict | None = None, timeout: float = 60) -> dict:
    packet = _req("POST", f"/ipc/{channel}", {"args": [payload or {}]}, timeout=timeout)
    if not packet.get("success"):
        return {"success": False, "error": packet.get("error") or "通道调用失败"}
    data = packet.get("data")
    return data if isinstance(data, dict) else {"success": True, "data": data}


def ipc_raw(channel: str, payload: dict | None = None, timeout: float = 60):
    packet = _req("POST", f"/ipc/{channel}", {"args": [payload or {}]}, timeout=timeout)
    return packet.get("data") if packet.get("success") else None


def health() -> dict:
    return _req("GET", "/ipc/health", timeout=10)


def now_ms() -> int:
    return int(time.time() * 1000)


def _hm(offset_min: int) -> str:
    return (datetime.now() + timedelta(minutes=offset_min)).strftime("%H:%M")


def wait_run(kind: str, task_id: str, limit_s: int = 420) -> dict:
    """等这个任务从「在跑」变成「不在跑」，返回最后一次运行记录。"""
    deadline = time.time() + limit_s
    saw_active = False
    while time.time() < deadline:
        st = ipc("get-runtime-status")
        active = [a for a in (st.get("active") or []) if a.get("taskId") == task_id]
        if active:
            saw_active = True
            print(f"  … {kind} 运行中：{active[0].get('stats')}")
        elif saw_active:
            break
        else:
            # 还没开始（线程启动有延迟），继续等
            time.sleep(1)
            continue
        time.sleep(3)
    runs = [r for r in (ipc("get-execution-history", {"kind": kind, "limit": 20})
                        .get("runs") or []) if r.get("task_id") == task_id]
    return runs[0] if runs else {}


# --------------------------------------------------------------------------
# 1. 引擎连通性
# --------------------------------------------------------------------------
def check_engine(wait_s: int = 30) -> bool:
    h = health()
    if not h or not h.get("success"):
        _fail("实例连通", "127.0.0.1:8899 /ipc/health 不可达")
        return False
    # 壳每 1.2s 来取一次指令；刚重启后后端还没被轮询过，要等一次心跳再判定引擎
    deadline = time.time() + wait_s
    st: dict = {}
    while time.time() < deadline:
        st = ipc("get-runtime-status") or {}
        if (st.get("engine") or {}).get("engine") not in (None, "none"):
            break
        time.sleep(1)
    st = st or ipc("get-runtime-status")
    eng = (st.get("engine") or {})
    mode = eng.get("engine") or "none"
    if mode == "none":
        _fail("执行引擎", "没有可用引擎：桌面壳未运行或抖音未登录")
        return False
    _pass("执行引擎", f"engine={mode} shell={eng.get('shell')} "
                     f"playwright={eng.get('playwright')}")
    return True


def check_probe_search(keyword: str) -> list[dict]:
    res = ipc("run-browser-probe", {"type": "search-videos", "keyword": keyword,
                                    "limit": 3}, timeout=240)
    if not res.get("success"):
        _fail("真机搜索探针", f"关键词「{keyword}」失败：{res.get('error')}")
        return []
    items = res.get("items") or []
    if not items:
        _fail("真机搜索探针", f"关键词「{keyword}」返回 0 条视频（可能是空结果或选择器失效）")
        return []
    sample = items[0]
    _pass("真机搜索探针",
          f"「{keyword}」找到 {len(items)} 条，例：{str(sample.get('title'))[:28]} "
          f"@{sample.get('author')}（{res.get('elapsedSec')}s）")
    return items


# --------------------------------------------------------------------------
# 2. 线索采集（真跑）
# --------------------------------------------------------------------------
def run_entity(keyword: str, account_id: str) -> dict:
    task_id = f"ent_real_{int(time.time())}"
    record = {
        "id": task_id,
        "name": f"真机验收·线索采集 {keyword}",
        "status": "ready",
        "createdAt": now_ms(),
        "entityKind": "entity",
        "runs": [{"accountId": account_id, "nickname": "真机验收"}],
        "configSnapshot": {"keywords": keyword, "videoMax": 2, "commentLimit": 12,
                           "accountId": account_id},
        "stats": {},
    }
    saved = ipc("save-entity-leadgen-task", record)
    if not saved.get("success"):
        _fail("线索采集建单", str(saved.get("error")))
        return {}
    started = ipc("start-entity-leadgen-task-runtime", {"taskRecord": record}, timeout=30)
    if not started.get("success"):
        _fail("线索采集启动", str(started.get("error")))
        return {}
    print(f"  线索采集已启动 runId={started.get('runId')}，等待执行…")
    run = wait_run("entity", task_id)
    leads = ipc("get-entity-leadgen-task-leads", {"id": task_id, "page": 1, "pageSize": 5})
    total = int(leads.get("total") or 0)
    if not run:
        _fail("线索采集执行", "没等到运行记录")
        return {}
    status = run.get("status")
    stats = run.get("stats") or {}
    if status == "completed" and (total > 0 or stats.get("comments")):
        _pass("线索采集（真机）",
              f"状态 {status}，视频 {stats.get('videos')} 条 / 评论 {stats.get('comments')} 条 / "
              f"新增线索 {stats.get('leads')}，任务线索总数 {total}")
    else:
        _fail("线索采集（真机）", f"状态 {status}：{run.get('message')}")
    return {"taskId": task_id, "run": run, "total": total,
            "sample": (leads.get("items") or [])[:3]}


# --------------------------------------------------------------------------
# 3. 监控（真跑）
# --------------------------------------------------------------------------
def run_monitor(video_url: str, keyword: str, account_id: str) -> dict:
    task_id = f"mon_real_{int(time.time())}"
    record = {
        "id": task_id,
        "name": f"真机验收·监控 {keyword}",
        "status": "ready",
        "createdAt": now_ms(),
        "configSnapshot": {
            "accountId": account_id,
            "videoUrls": [video_url],
            "keywords": [keyword],
            "maxCycles": 1,
            "intervalSec": 60,
            "commentLimit": 20,
        },
    }
    saved = ipc("save-monitor-task", record)
    if not saved.get("success"):
        _fail("监控建单", str(saved.get("error")))
        return {}
    started = ipc("start-monitor-task-runtime", {"taskRecord": record})
    if not started.get("success"):
        _fail("监控启动", str(started.get("error")))
        return {}
    print(f"  监控已启动 runId={started.get('runId')}，等待一轮巡检…")
    run = wait_run("monitor", task_id, limit_s=300)
    cycles = ipc_raw("get-monitor-task-cycles", {"taskId": task_id, "offset": 0,
                                                 "limit": 10}) or []
    stats = (run.get("stats") or {})
    if run.get("status") == "completed" and (stats.get("read") or cycles):
        _pass("监控（真机）",
              f"完成 {len(cycles)} 轮，读到 {stats.get('read')} 条评论，"
              f"新增 {stats.get('new')} 条，命中 {stats.get('matched')} 条")
    else:
        _fail("监控（真机）", f"状态 {run.get('status')}：{run.get('message')}")
    return {"taskId": task_id, "run": run}


# --------------------------------------------------------------------------
# 4. 养号（真跑，只浏览不点赞）
# --------------------------------------------------------------------------
def run_warmup(account_id: str) -> dict:
    task_id = f"warm_real_{int(time.time())}"
    record = {
        "id": task_id,
        "name": "真机验收·养号（只浏览）",
        "status": "ready",
        "createdAt": now_ms(),
        "configSnapshot": {"accountId": account_id, "cycles": 1, "scrolls": 2,
                           "dwellMs": 1200, "likeMax": 0},
    }
    saved = ipc("save-self-warmup-task", record)
    if not saved.get("success"):
        _fail("养号建单", str(saved.get("error")))
        return {}
    started = ipc("start-self-warmup-task-runtime", {"taskRecord": record})
    if not started.get("success"):
        _fail("养号启动", str(started.get("error")))
        return {}
    print(f"  养号已启动 runId={started.get('runId')}，等待一轮浏览…")
    run = wait_run("warmup", task_id, limit_s=240)
    stats = run.get("stats") or {}
    if run.get("status") == "completed" and stats.get("watched"):
        _pass("养号（真机）", f"浏览 {stats.get('watched')} 屏，点赞 {stats.get('liked', 0)} 次"
                             f"（本脚本强制 likeMax=0）")
    else:
        _fail("养号（真机）", f"状态 {run.get('status')}：{run.get('message')}")
    return {"taskId": task_id, "run": run}


# --------------------------------------------------------------------------
# 5. 三种临时停
# --------------------------------------------------------------------------
def switch_tests(warm_task_id: str) -> None:
    """三种停止手段逐一验证：排期 enabled=false / 合规安全模式 / SCHEDULER_OFF。"""
    # --- a. enabled=false：排期存在但不该被消费
    sched = {"type": "daily", "enabled": False, "startHm": _hm(-10), "endHm": _hm(30)}
    ipc("save-self-warmup-schedules", {warm_task_id: sched})
    before = len(_triggered("warmup"))
    time.sleep(26)  # 一个 tick（20s）之后
    after = len(_triggered("warmup"))
    if after == before:
        _pass("临时停①：排期 enabled=false", f"过了 1 个 tick 仍无触发记录（{after} 条）")
    else:
        _fail("临时停①：排期 enabled=false", f"触发记录从 {before} 变成 {after}")

    # --- b. 合规安全模式：整轮熔断
    enter = _req("POST", "/api/compliance/safe-mode/enter",
                 {"source": "acceptance", "reason": "真机验收：验证熔断"})
    h = health()
    safe_on = bool((h.get("scheduler") or {}).get("safeMode"))
    if not safe_on:
        _fail("临时停②：安全模式", f"进入安全模式失败：{enter}")
        return
    sched_on = {"type": "daily", "enabled": True, "startHm": _hm(-10), "endHm": _hm(30)}
    ipc("save-self-warmup-schedules", {warm_task_id: sched_on})
    before = len(_triggered("warmup"))
    time.sleep(26)
    during = len(_triggered("warmup"))
    if during == before:
        _pass("临时停②：安全模式熔断",
              "/ipc/health safeMode=true，窗口内到点也不触发（未占用窗口，解除后仍可执行）")
    else:
        _fail("临时停②：安全模式熔断", f"熔断期间仍产生触发：{before} → {during}")

    # --- c. 解除熔断后，同一个窗口应该立刻真的跑起来（证明不是「永久跳过」）
    _req("POST", "/api/compliance/safe-mode/exit")
    h = health()
    if (h.get("scheduler") or {}).get("safeMode"):
        _fail("解除安全模式", "退出后 safeMode 仍为 true")
        return
    fired = False
    for _ in range(6):
        time.sleep(12)
        if _triggered("warmup"):
            fired = True
            break
    if fired:
        _pass("解除熔断后到点真执行", "排期窗口内产生 fired 记录（不是永久跳过）")
    else:
        _fail("解除熔断后到点真执行", "解除后 70s 内仍无触发记录")

    # 收尾：关掉排期，别留无人值守的定时任务
    ipc("save-self-warmup-schedules", {warm_task_id: {**sched_on, "enabled": False}})
    print("  已关闭本次验收创建的排期（enabled=false）")

    # --- d. SCHEDULER_OFF=1：进程级开关（独立进程验证，不动线上实例）
    import os
    import subprocess
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    py = str(backend / ".venv" / "Scripts" / "python.exe")
    code = ("from app import scheduler;"
            "print('ON' if scheduler.start(r'%s') else 'OFF')" % (backend / "data" / "lead_system.db"))
    env = dict(os.environ)
    env["SCHEDULER_OFF"] = "1"
    try:
        out = subprocess.run([py, "-c", code], cwd=str(backend), env=env,
                             capture_output=True, text=True, timeout=30)
        value = (out.stdout or "").strip().splitlines()[-1:] or [""]
        if value[0].endswith("OFF"):
            _pass("临时停③：SCHEDULER_OFF=1", "环境变量置 1 时 scheduler.start() 不起线程")
        else:
            _fail("临时停③：SCHEDULER_OFF=1", f"输出：{out.stdout!r} {out.stderr[-200:]!r}")
    except Exception as exc:  # noqa: BLE001
        _fail("临时停③：SCHEDULER_OFF=1", f"{type(exc).__name__}: {exc}")


def _triggered(kind: str) -> list[dict]:
    hist = ipc("get-execution-history", {"kind": kind, "limit": 40})
    return hist.get("triggered") or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="AI客服")
    ap.add_argument("--account", default="")
    ap.add_argument("--skip-switches", action="store_true")
    ap.add_argument("--skip-collect", action="store_true")
    args = ap.parse_args()

    print("=" * 78)
    print(f"真机验收  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  关键词={args.keyword}")
    print("=" * 78)

    if not check_engine():
        return _summary()

    account = args.account
    if not account:
        st = ipc("get-runtime-status")
        accs = ipc_raw("get-account-pool", {}) or []
        # 账号 id 从账号池接口取（没有就用第一个活跃账号兜底）
        for item in (accs if isinstance(accs, list) else []):
            account = str(item.get("id") or "")
            if account:
                break
    if not account:
        _skip("账号归属", "未取到账号 id：运行时会自动回落到唯一活跃账号")

    items = check_probe_search(args.keyword)
    video_url = ""
    if items:
        video_url = str(items[0].get("url") or "")
        if video_url:
            res = ipc("run-browser-probe", {"type": "read-comments", "videoUrl": video_url,
                                            "limit": 10}, timeout=240)
            if res.get("success"):
                _pass("真机读评论探针",
                      f"{video_url.rsplit('/', 1)[-1]} 读到 {res.get('count')} 条"
                      + (f"，例：{res['samples'][0]['text'][:30]}" if res.get("samples") else ""))
            else:
                _fail("真机读评论探针", str(res.get("error")))

    if not args.skip_collect:
        run_entity(args.keyword, account)
        if video_url:
            run_monitor(video_url, args.keyword, account)
        else:
            _skip("监控（真机）", "没有可用的真实视频地址")
        run_warmup(account)

    if not args.skip_switches:
        warm = ipc_raw("get-self-warmup-tasks", {}) or []
        warm_id = ""
        for rec in (warm if isinstance(warm, list) else []):
            if str(rec.get("id") or "").startswith("warm_real_"):
                warm_id = str(rec["id"])
                break
        if warm_id:
            switch_tests(warm_id)
        else:
            _skip("三种临时停", "没有可用于排期验证的养号任务")

    return _summary()


def _summary() -> int:
    passed = sum(1 for r in _RESULTS if r[0] == "PASS")
    failed = sum(1 for r in _RESULTS if r[0] == "FAIL")
    skipped = sum(1 for r in _RESULTS if r[0] == "SKIP")
    print("-" * 78)
    print(f"合计：PASS {passed} · FAIL {failed} · SKIP {skipped}")
    if failed:
        print("失败项：")
        for state, name, detail in _RESULTS:
            if state == "FAIL":
                print(f"  - {name}：{detail}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
