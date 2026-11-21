"""运行时注册表：谁在跑、能不能跑、怎么停。

为什么要集中在这里
------------------
壳里只有**一个已登录的浏览器 session**。同一个账号同时跑两个任务，等于把两个
任务的动作交错到同一个页面上，既采不准也更容易触发风控。所以并发约束放在这一层：
一个账号同一时刻只允许一个运行时实例；跨账号才并行。

对外三个入口：start / stop / status。排期执行器与页面通道都只调这三个，
不直接碰 runner，避免两条路径各自演化出不同的行为。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import leadgen_pools
from app.api import task_store
from app.runtime import browser, runners, store

KINDS = ("leadgen", "entity", "monitor", "warmup")

_LOCK = threading.Lock()
_RUNS: dict[str, "Handle"] = {}
_ACCOUNT_LEASES: dict[str, str] = {}


def claim_account(account_id: str, owner: str) -> bool:
    """Reserve the shared desktop session for a reader or scoped interaction."""
    with _LOCK:
        if account_id in _ACCOUNT_LEASES or any(
                h.account_id == account_id and h.thread.is_alive() for h in _RUNS.values()):
            return False
        _ACCOUNT_LEASES[account_id] = owner
        return True


def release_account(account_id: str, owner: str) -> None:
    with _LOCK:
        if _ACCOUNT_LEASES.get(account_id) == owner:
            _ACCOUNT_LEASES.pop(account_id, None)


@dataclass
class Handle:
    kind: str
    task_id: str
    run_id: str
    account_id: str
    name: str
    ctx: runners.Ctx
    thread: threading.Thread
    trigger: str


def _key(kind: str, task_id: str) -> str:
    return f"{kind}:{task_id}"


def _record(db_path, kind: str, task_id: str) -> dict | None:
    if kind == "entity":
        rec = leadgen_pools.load_records(db_path).get(str(task_id))
        if isinstance(rec, dict) and str(rec.get("entityKind") or "") == "entity":
            return rec
        return None
    if kind in {"monitor", "warmup"}:
        return task_store.get_task(db_path, kind, task_id)
    return None


def start(db_path, kind: str, task_id: str, trigger: str = "manual") -> dict:
    """启动一个运行时实例。已在跑则不重复启动（幂等）。"""
    kind = str(kind or "").strip()
    task_id = str(task_id or "").strip()
    if kind not in {"entity", "monitor", "warmup"}:
        return {"success": False, "error": f"不支持的运行时类型：{kind}"}
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}

    with _LOCK:
        existing = _RUNS.get(_key(kind, task_id))
        if existing and existing.thread.is_alive():
            return {"success": True, "status": "running", "runId": existing.run_id,
                    "note": "任务已在运行，本次未重复启动", "already": True}

    rec = _record(db_path, kind, task_id)
    if not isinstance(rec, dict):
        return {"success": False,
                "error": f"找不到任务记录（可能已删除）：{kind} / {task_id}"}

    cfg = runners.task_config(rec)
    # 自动回复任务和养号任务共用 self_warmup 表（前端只认一个任务类型），
    # 靠 is_auto_reply_config 分流：自动回复走自己的运行时，绝不会被当成养号去刷推荐流。
    auto_reply = kind == "warmup" and runners.is_auto_reply_config(cfg)
    if kind == "entity" and "comment_like" in cfg.get("sourceTypes", []):
        return {"success": False, "code": "live_writes_disabled",
                "error": "评论点赞不能作为线索采集启动；请使用评论点赞页的独立入口"}
    # 吞评检测：常规互检（window + 本地样本 / 推荐页样本）、高速自检（backend_api）
    # 都已支持。配置问题（账号不足、人设未绑定等）在这里就拦下，让用户在点击
    # 启动的瞬间看到人话原因，而不是跑空一趟。
    if kind == "entity" and "entity_comment_visibility" in cfg.get("sourceTypes", []):
        from app.runtime import visibility as _visibility

        problem = _visibility.config_problem(cfg, db_path)
        if problem:
            return {"success": False, "code": "visibility_engine_unavailable",
                    "error": problem}
    # 先看引擎再看账号：没有已登录的浏览器时，让用户去「加账号」是误导。
    engine = browser.status(db_path)["engine"]
    if engine == "none":
        return {"success": False, "noEngine": True,
                "error": "没有可用的执行引擎：请确认「阿里山AI获客」桌面应用已打开并登录抖音"}

    account_id = runners.resolve_account(db_path, rec, cfg)
    if not account_id:
        return {"success": False,
                "error": "任务没有可执行账号：请先在账号池里添加并登录一个抖音账号"}

    if kind == "warmup" and cfg.get("taskMode") == "nurture":
        remaining = store.warmup_cooldown_seconds(db_path, account_id)
        if remaining:
            return {"success": False, "cooldown": True,
                    "retryAfterSec": remaining,
                    "error": f"距上次养号结束不足 3 分钟，请 {remaining} 秒后再启动"}

    with _LOCK:
        busy = [h for h in _RUNS.values()
                if h.account_id == account_id and h.thread.is_alive()]
        if account_id in _ACCOUNT_LEASES:
            return {"success": False, "busy": True, "error": "该账号正在读取评论或执行受限互动，请先停止"}
        if busy:
            return {"success": False, "busy": True,
                    "error": f"账号 {account_id} 正在执行其他任务（{busy[0].name}），稍后重试"}

    run_id = store.run_begin(
        db_path, kind, task_id, str(rec.get("name") or ""), account_id,
        trigger=trigger, engine=engine)
    ctx = runners.Ctx(kind=kind, db_path=Path(db_path), task_id=task_id,
                      name=str(rec.get("name") or task_id), account_id=account_id,
                      run_id=run_id, trigger=trigger, engine=engine)

    runner = {"entity": runners.run_entity, "monitor": runners.run_monitor,
              "warmup": runners.run_warmup}[kind]
    if auto_reply:
        runner = runners.run_auto_reply

    def _worker() -> None:
        try:
            # 养号自己会打一条信息量更大的开始日志（含时长/停留/概率/运行号），
            # 这里不再重复一条「运行时已启动」，否则一次点击会出现两行、看不出区别。
            if kind != "warmup":
                runners.log(ctx, f"运行时已启动（触发方式：{trigger}，引擎：{engine}）")
            runners._evt(ctx, "status", status="running")  # noqa: SLF001 同一模块约定
            runner(db_path, rec, ctx)
        except Exception as exc:  # noqa: BLE001 线程内异常必须落证据
            # 带上出错位置：否则日志只有一行「执行异常：NameError」，还得靠猜。
            import traceback

            frames = traceback.extract_tb(exc.__traceback__)
            where = ""
            if frames:
                last = frames[-1]
                where = f"（{(last.filename or '').split(chr(92))[-1]}:{last.lineno} {last.name}）"
            msg = f"执行异常：{type(exc).__name__}: {exc}{where}"
            try:
                runners.log(ctx, msg, "error")
                runners._evt(ctx, "finished", status="failed", endReason="error",
                             stats=ctx.stats, message=msg)  # noqa: SLF001
                store.run_end(db_path, run_id, "failed", ctx.stats, msg)
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _LOCK:
                _RUNS.pop(_key(kind, task_id), None)

    thread = threading.Thread(target=_worker, name=f"{kind}-{task_id}", daemon=True)
    handle = Handle(kind=kind, task_id=task_id, run_id=run_id, account_id=account_id,
                    name=str(rec.get("name") or task_id), ctx=ctx, thread=thread,
                    trigger=trigger)
    with _LOCK:
        if account_id in _ACCOUNT_LEASES or any(
                h.account_id == account_id and h.thread.is_alive() for h in _RUNS.values()):
            store.run_end(db_path, run_id, "failed", ctx.stats, "账号正在执行其他任务")
            return {"success": False, "busy": True, "error": "该账号正在执行其他任务，请稍后重试"}
        _RUNS[_key(kind, task_id)] = handle
        thread.start()
    return {"success": True, "status": "running", "runId": run_id,
            "taskId": task_id, "kind": kind, "accountId": account_id, "engine": engine}


def stop(db_path, kind: str, task_id: str, wait: float = 0.0) -> dict:
    with _LOCK:
        handle = _RUNS.get(_key(str(kind), str(task_id)))
    if handle is None:
        return {"success": True, "stopped": 0, "note": "任务未在运行"}
    handle.ctx.stop.set()
    if wait:
        handle.thread.join(timeout=wait)
    return {"success": True, "stopped": 1, "runId": handle.run_id,
            "note": "已下发停止"}


def stop_all(db_path, kind: str = "") -> dict:
    with _LOCK:
        handles = [h for h in _RUNS.values() if not kind or h.kind == kind]
    for handle in handles:
        handle.ctx.stop.set()
    return {"success": True, "stopped": len(handles),
            "taskIds": [h.task_id for h in handles]}


def stop_account(db_path, account_id: str) -> dict:
    """Stop only this account's runtimes, including warmup and collection."""
    account_id = str(account_id or "").strip()
    with _LOCK:
        handles = [h for h in _RUNS.values()
                   if account_id and h.account_id == account_id and h.thread.is_alive()]
        for handle in handles:
            handle.ctx.stop.set()
    return {"success": True, "stopped": len(handles),
            "taskIds": [h.task_id for h in handles]}


def active(kind: str = "") -> list[dict]:
    with _LOCK:
        handles = [h for h in _RUNS.values() if h.thread.is_alive()]
    out = []
    for h in handles:
        if kind and h.kind != kind:
            continue
        out.append({"kind": h.kind, "taskId": h.task_id, "runId": h.run_id,
                    "accountId": h.account_id, "name": h.name,
                    "trigger": h.trigger,
                    "startedAt": int(h.ctx.started_at * 1000),
                    "stats": dict(h.ctx.stats)})
    return out


def active_ids(kind: str) -> list[str]:
    return [i["taskId"] for i in active(kind)]


def active_accounts() -> list[str]:
    with _LOCK:
        return sorted(set(_ACCOUNT_LEASES) | {h.account_id for h in _RUNS.values()
                      if h.account_id and h.thread.is_alive()})


def status(db_path) -> dict:
    eng = browser.status(db_path)
    return {
        "engine": eng,
        "engines": {"entity": eng["engine"] != "none",
                    "monitor": eng["engine"] != "none",
                    "warmup": eng["engine"] != "none"},
        "active": active(),
        "diagnostics": store.diagnostics(db_path),
        "recent": store.recent_runs(db_path, limit=8),
    }


def wait_idle(db_path, timeout: float = 5.0) -> bool:
    """测试用：等所有实例收尾。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _LOCK:
            alive = [h for h in _RUNS.values() if h.thread.is_alive()]
        if not alive:
            return True
        time.sleep(0.05)
    return False


def reset_for_tests() -> None:
    with _LOCK:
        for handle in _RUNS.values():
            handle.ctx.stop.set()
        _RUNS.clear()
        _ACCOUNT_LEASES.clear()
