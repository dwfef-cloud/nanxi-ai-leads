"""「评论获客」执行器 —— 在关键词相关视频下发表主评，并对评论者互动。

业务定义（2026-11-14 按原软件语义纠正）
--------------------------------------
评论获客是**写动作**：按关键词/话题搜到视频 → 在该视频下发表主评（首作评论）
→ 对评论区用户互动（点赞 / 回复 / 关注 / 私信 / 分享主页）→ 触达潜客。

它与「线索采集」方向相反：
    - 评论获客：我去别人视频下留言，把人引过来         （写）
    - 线索采集：我去别人视频下把留言的人捞出来         （读，走 runtime/entity 引擎）

历史实现把「读已入库评论 → 打分 → 待触达」当成了评论获客，实际是线索采集的活，
且下发的是壳的 collect-comments（只读采集）指令。2026-11-14 已按此拆开：
采集逻辑归 runtime/entity 引擎（线索采集模块），本文件只保留评论获客的执行位。

当前桌面链路：用户点击启动后校验配置并创建有时限的签名作用域 → 同一账号 Electron
会话逐关键词、逐视频浏览 → 执行配置启用的互动 → 平台回执核对 → 持久记录。
启动动作明确授权本次配置；未知结果立即停止且不得重试。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.api import event_bus, task_logs
from app.runtime import store as rt_store

# 评论获客的执行结果字段（原软件「已发表 / 已互动 / 已触达 / 已经评过 / 发表失败」）
EMPTY_STATS: dict[str, int] = {
    "published": 0,   # 已发表主评
    "interacted": 0,  # 已互动（点赞/回复/关注/私信）
    "reached": 0,     # 已触达（对评论者完成动作）
    "already": 0,     # 已经评过（本账号在该视频留过评，跳过）
    "failed": 0,      # 发表失败
    "skipped": 0,     # 其它跳过（人设不匹配、链接失效等）
    "skippedAds": 0,  # 广告视频直接划过，不计入视频浏览目标
}

EXECUTOR_CODE = "live_review_required"

# 单次任务上限与节奏（真实执行器接入后使用）
MAX_VIDEOS_PER_RUN = 10
COMMENT_COOLDOWN_S = (300, 600)  # 设计文档 §5.1 实测：409/509/365s


@dataclass
class RunCtx:
    task_id: str
    leadgen_id: str
    account_id: str
    payload: dict = field(default_factory=dict)
    db_path: Path | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)
    # 运行时证据链：一次启动 → 一条 runtime_runs（执行历史页面靠它）
    run_id: str = ""
    task_name: str = ""
    trigger: str = "manual"
    engine: str = ""
    stats: dict = field(default_factory=lambda: dict(EMPTY_STATS))


_RUNS: dict[str, RunCtx] = {}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 执行器能力
# --------------------------------------------------------------------------
def executor_ready() -> bool:
    """是否存在当前有效的发送确认；桌面能力本身不由此布尔值判断。"""
    from app.execution.live_scope import load_scope
    return bool(load_scope())


# --------------------------------------------------------------------------
# 事件推送
# --------------------------------------------------------------------------
# 两条通道各有分工，缺一条就会出现"大屏动了、日志页空着"的情况：
#   automation-data          → 账号级日志 / 当前动作 / 运行状态（实况大屏、账号卡片）
#   entity-leadgen-task-event → 任务级日志（任务页「运行日志」抽屉）
def _task_key(run: RunCtx) -> str:
    """任务级事件的键：必须用任务记录 id，壳按它索引 taskLogs。"""
    return str(run.leadgen_id or run.task_id)


def _emit(kind: str, task_id: str, account_id: str, **payload: Any) -> None:
    event_bus.publish("automation-data", {
        "type": kind, "taskId": task_id, "payload": payload})


def _emit_task(run: RunCtx, kind: str, **payload: Any) -> None:
    """任务级事件（log / stats / finished），形状对齐壳的 entity-leadgen handler。"""
    event_bus.publish("entity-leadgen-task-event", {
        "type": kind,
        "taskId": _task_key(run),
        "accountId": run.account_id,
        "ts": int(time.time() * 1000),
        **payload,
    })


def _log(run: RunCtx, message: str, level: str = "normal") -> None:
    """写一条运行日志：落库 + 双通道推送（大屏账号日志 + 任务日志抽屉）。"""
    stamp = int(time.time() * 1000)
    if run.db_path is not None:
        task_logs.append(run.db_path, run.task_id, run.leadgen_id,
                         run.account_id, message, level, stamp)
    _emit("trace-log", run.task_id, run.account_id,
          accountId=run.account_id, message=message, level=level)
    _emit_task(run, "log", message=message, level=level, ts=stamp)


def _action(run: RunCtx, text: str) -> None:
    _emit("current-action", run.task_id, run.account_id,
          accountId=run.account_id, action=text)


# --------------------------------------------------------------------------
# 收尾：落运行证据 + 写回任务记录
# --------------------------------------------------------------------------
def _record_id(run: RunCtx) -> str:
    """任务记录 id：壳有时传 taskId，有时只传 leadgenTaskId，以库里实际存在的为准。"""
    from app import leadgen_pools

    candidates = [str(run.leadgen_id or "").strip(), str(run.task_id or "").strip()]
    try:
        records = leadgen_pools.load_records(run.db_path) if run.db_path else {}
    except sqlite3.Error:
        records = {}
    for cid in candidates:
        if cid and cid in records:
            return cid
    return next((c for c in candidates if c), "")


def _persist_task_result(run: RunCtx, status: str, stats: dict, summary: str,
                         end_reason: str = "") -> None:
    """把这次执行的结果写回任务记录（runs 追加 + stats/status/endedAt）。

    为什么要写：壳页面刷新后只认库里的字段。没有这一步，执行历史与任务详情
    永远看不到这次跑过（2026-11-14 用户实测）。
    """
    from app import leadgen_pools

    rid = _record_id(run)
    if not rid or run.db_path is None:
        return
    rec = leadgen_pools.load_records(run.db_path).get(rid) or {}
    if not rec:
        return
    if run.account_id in rec.get("reviewedAccounts", {}):
        ended = int(time.time() * 1000)
        leadgen_pools.reviewed_run_update(run.db_path, rid, run.account_id, run.task_id, result={
            "status": status, "stats": dict(stats), "endedAt": ended,
            "endReason": end_reason or status, "message": summary,
            "run": {"automationTaskId": run.run_id, "runTaskId": run.task_id, "accountId": run.account_id,
                    "startedAt": int(run.started_at * 1000), "endedAt": ended, "status": status,
                    "endReason": end_reason or status, "stats": dict(stats), "message": summary}})
        return
    runs = [r for r in (rec.get("runs") or []) if isinstance(r, dict)]
    runs.append({
        "automationTaskId": run.run_id,
        "accountId": run.account_id,
        "startedAt": int(run.started_at * 1000),
        "endedAt": int(time.time() * 1000),
        "status": status,
        "endReason": end_reason or status,
        "stats": dict(stats),
        "message": summary,
    })
    leadgen_pools.update_task_record(run.db_path, rid, {
        "status": status,
        "runs": runs[-20:],
        "stats": dict(stats),
        "endedAt": int(time.time() * 1000),
        "endReason": end_reason or status,
        "lastMessage": summary,
    })


def _finish(run: RunCtx, status: str, stats: dict, summary: str,
            end_reason: str = "") -> None:
    """一次执行的统一收尾：运行证据落库 → 任务记录写回 → 清活跃表。"""
    if run.db_path is not None and run.run_id:
        try:
            rt_store.run_end(run.db_path, run.run_id, status, stats, summary)
        except sqlite3.Error:
            pass
        try:
            _persist_task_result(run, status, stats, summary, end_reason)
        except sqlite3.Error:
            pass
    with _LOCK:
        _RUNS.pop(run.task_id, None)
    from app.runtime import registry
    registry.release_account(run.account_id, "leadgen:" + run.task_id)


def _task_name(db_path: Path, task_id: str, payload: dict) -> str:
    name = str(payload.get("taskName") or "").strip()
    if name:
        return name
    try:
        from app import leadgen_pools

        name = str((leadgen_pools.load_records(db_path).get(task_id) or {}).get("name") or "")
    except sqlite3.Error:
        name = ""
    return name or task_id


def _reject_start(db_path: Path, payload: dict, task_id: str, account_id: str) -> dict:
    """启动授权校验失败时留下明确的拒绝记录。"""
    from app.execution.live_scope import task_rejection_reason
    message = task_rejection_reason(payload)
    trigger = str(payload.get("trigger") or "manual").strip() or "manual"
    run = RunCtx(
        task_id=task_id,
        leadgen_id=str(payload.get("leadgenTaskId") or ""),
        account_id=account_id, payload=payload, db_path=Path(db_path),
        task_name=_task_name(db_path, task_id, payload),
        trigger=trigger, engine="none",
    )
    try:
        run.run_id = rt_store.run_begin(
            db_path, "leadgen", task_id, run.task_name, account_id,
            trigger=trigger, engine="none")
    except sqlite3.Error:
        run.run_id = ""

    _log(run, message, "error")
    # 壳的 automation-data handler 只在 status="finished" 时把 finishMessage 写进账号日志；
    # 传 "failed" 只会显示「任务已挂起」，用户看不到原因。
    _emit("status", task_id, account_id, accountId=account_id,
          status="finished", finishMessage=message)
    _emit_task(run, "finished", status="failed", endReason=EXECUTOR_CODE,
               stats=dict(EMPTY_STATS), message=message)
    _finish(run, "failed", dict(EMPTY_STATS), message, EXECUTOR_CODE)

    return {"success": False, "code": EXECUTOR_CODE,
            "taskId": task_id, "error": message,
            "msg": message, "stats": dict(EMPTY_STATS)}


# --------------------------------------------------------------------------
# 采集器设置（真机/引擎开关仍被线索采集与诊断页使用）
# --------------------------------------------------------------------------
def _setting(db_path: Path, key: str, default: str = "") -> str:
    try:
        con = sqlite3.connect(str(db_path), timeout=10)
        try:
            row = con.execute(
                "SELECT value FROM system_settings WHERE category='collector' AND key=?",
                (key,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return default
    return str((row or [default])[0] or default)


def real_mode_enabled(db_path: Path) -> bool:
    """是否启用真机采集（system_settings(collector, real) = "1"）。

    评论获客本身是写动作，不走这个开关；线索采集引擎与诊断页仍在读它。
    """
    return _setting(db_path, "real").strip() in {"1", "true", "on", "yes"}


def real_engine(db_path: Path) -> str:
    """真机引擎选择：playwright（交付包引擎，需独立登录）/ electron（壳内登录态）。"""
    want = _setting(db_path, "engine", "auto").strip().lower()
    if want in {"playwright", "electron"}:
        return want
    try:
        from app import douyin_runner
        if douyin_runner.engine_ready():
            return "playwright"
    except Exception:  # noqa: BLE001 引擎不可用就退回壳
        pass
    return "electron"


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------
def start(db_path: Path, payload: dict) -> dict:
    """启动一次评论获客任务。同一 taskId 重复启动直接返回已在运行。"""
    payload = payload if isinstance(payload, dict) else {}
    task_id = str(payload.get("taskId") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少 taskId，无法启动"}
    account_id = str(payload.get("accountId") or "").strip()
    if not account_id:
        return {"success": False, "error": "未指定执行账号"}
    with _LOCK:
        existing = _RUNS.get(task_id)
        if existing and not existing.stop.is_set():
            return {"success": True, "taskId": task_id, "status": "running",
                    "note": "任务已在运行，本次未重复启动"}
    from app.execution.live_scope import task_allowed
    if not task_allowed(payload):
        return _reject_start(db_path, payload, task_id, account_id)
    # Execute the locally reviewed plan on the same desktop account session.
    # Counts and targets come from the signed scope, never unchecked UI limits.
    from app.runtime import browser
    from app.execution.live_leadgen import run as run_live
    if browser.status(db_path)["engine"] != "shell":
        return {"success": False, "error": "请先启动桌面客户端，真机验收只使用桌面账号会话"}
    with _LOCK:
        if task_id in _RUNS or any(r.account_id == account_id for r in _RUNS.values()):
            return {"success": False, "error": "当前账号已有评论任务正在执行或收尾"}
        from app.runtime import registry
        if not registry.claim_account(account_id, "leadgen:" + task_id):
            return {"success": False, "error": "当前账号正在执行其他任务，请先停止"}
        run = RunCtx(task_id=task_id, leadgen_id=str(payload.get("leadgenTaskId") or ""),
                     account_id=account_id, payload=dict(payload), db_path=Path(db_path),
                     task_name=_task_name(db_path, task_id, payload), engine="shell")
        try:
            run.run_id = rt_store.run_begin(db_path, "leadgen", task_id, run.task_name, account_id,
                                            trigger="manual", engine="shell")
            _RUNS[task_id] = run
            threading.Thread(target=run_live, args=(run,), daemon=True, name="live-leadgen-acceptance").start()
        except Exception:
            _RUNS.pop(task_id, None)
            registry.release_account(account_id, "leadgen:" + task_id)
            raise
    return {"success": True, "taskId": task_id, "status": "running", "runId": run.run_id}


def stop(task_id: str) -> dict:
    tid = str(task_id or "").strip()
    with _LOCK:
        run = _RUNS.get(tid)
    if run is None:
        from app.execution.live_workflow import cancel
        if cancel(task_id=tid):
            return {"success": True, "taskId": tid, "status": "stopping"}
        return {"success": True, "taskId": tid, "note": "任务未在运行"}
    run.stop.set()
    return {"success": True, "taskId": tid, "status": "stopping"}


def pause_task(task_id: str, paused: bool) -> dict:
    with _LOCK:
        run = _RUNS.get(str(task_id or ""))
    if run is None or run.stop.is_set():
        return {"success": False, "error": "任务未在运行；已停止的任务请重新核对剩余清单"}
    if paused:
        run.pause.set()
    else:
        run.pause.clear()
    _action(run, "正在暂停，当前已提交动作核验完毕后暂停" if paused else "正在继续已确认的剩余清单")
    return {"success": True, "status": "pausing" if paused else "running", "taskId": run.task_id}


def stop_account(account_id: str) -> dict:
    """按账号停止：壳的「重启任务」清场只发 viewKey（platform_accountId），没有 taskId。"""
    acct = str(account_id or "").strip()
    if not acct:
        return {"success": True, "note": "无账号标识，未停止任何任务"}
    with _LOCK:
        hits = [r for r in _RUNS.values() if r.account_id == acct]
    for run in hits:
        run.stop.set()
    from app.execution.live_workflow import cancel
    return {"success": True, "accountId": acct, "stopped": len(hits) + cancel(account_id=acct)}


def active_ids() -> list[str]:
    with _LOCK:
        return [k for k, v in _RUNS.items() if not v.stop.is_set()]


def active_account_ids() -> list[str]:
    """正在运行任务的账号 id（壳的 get-entity-leadgen-busy-account-ids 用）。"""
    with _LOCK:
        return [v.account_id for v in _RUNS.values() if not v.stop.is_set()]


def active_records() -> list[dict]:
    """活跃运行的详情，字段形状对齐 registry.active()（实况大屏通道取并集用）。"""
    with _LOCK:
        out = []
        for v in _RUNS.values():
            out.append({
                "kind": "leadgen",
                "taskId": v.task_id,
                "accountId": v.account_id,
                "name": str(v.payload.get("nickname") or v.payload.get("name") or ""),
                "trigger": v.trigger,
                "paused": v.pause.is_set(),
                "stopping": v.stop.is_set(),
                "stats": dict(v.stats),
                "startedAt": int(v.started_at * 1000),
            })
        return out


def busy() -> bool:
    """是否还有任务线程在收尾（含已发出停止信号的），用于等待与自检。"""
    with _LOCK:
        return bool(_RUNS)


def reset_for_tests() -> None:
    with _LOCK:
        for run in _RUNS.values():
            run.stop.set()
        _RUNS.clear()
