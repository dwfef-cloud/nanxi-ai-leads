"""后端 → 桌面壳 的指令队列（真机采集专用）。

为什么需要它
------------
真实采集必须跑在**有登录态的浏览器**里，而浏览器在桌面壳（Electron）进程中。
后端不能直接操作 DOM，只能下发「指令」，由壳执行后把结果回传——这就是本模块。

设计取舍
--------
- 指令队列放内存：进程重启即失效，符合"一次任务一次调度"的语义（任务本身也不跨重启续跑）。
- 结果按指令 id 存放，超时可查（`wait_result` 返回 None 让调用方按"无结果"落日志，
  而不是把任务卡死）。
- 不做鉴权：仅监听 127.0.0.1，与其它 /ipc 通道同一安全边界。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

_LOCK = threading.Lock()
_QUEUE: list[dict] = []
_RESULTS: dict[str, dict] = {}
_CANCELLED: dict[str, str] = {}
_EVENT = threading.Condition(_LOCK)
_LAST_POLL = 0.0  # 壳最近一次取指令的时间，用于判断「执行器是否在线」
_POLLS = 0        # 累计轮询次数（诊断用：判断壳到底在不在取指令）
_LAST_CMD: dict | None = None  # 最近一次被壳取走的指令（诊断用）

# 按壳实例分开计数：指令是「取走即出队」的，两个壳同时开着时会出现
# 「这边在轮询、指令却被另一个实例拿走、那边卡住不回传」的假死。
# 单看 total 轮询数会误判成「壳活得好好的」，按实例拆开才看得出来。
_AGENTS: dict[str, dict] = {}

# 已完成的结果保留上限，避免长驻进程无限增长
_MAX_RESULTS = 200

# 壳每 1.2s 轮询一次；超过这个窗口没轮询过，视为执行器不在线
AGENT_ALIVE_WINDOW_S = 30.0

# 正在执行的指令：壳是「同账号串行、跨账号并行」的 —— 同一账号的窗口一次跑
# 一条指令；不同账号的常驻窗口互不干扰，可以同时各跑各的。
# 只要还有在飞的指令，就不能因为「30 秒没轮询」判定它掉线：
# 养号浏览一条指令就要 40+ 秒，误判的后果是任务中途连续失败（真机 2026-11-14 复现）。
_INFLIGHT: dict[str, float] = {}
_INFLIGHT_TIMEOUTS: dict[str, float] = {}
# 在飞指令的最长容忍时间：壳侧单条硬超时 6 分钟，留 1 分钟余量，防泄漏导致永远 alive
INFLIGHT_MAX_S = 420.0

# 派发即执行记账：cmd_id -> accountId。壳取走一条就占住该账号，回传结果后释放；
# 取指时跳过「账号正忙」的指令（留在队列里，等它的上一条跑完再派），
# 这样壳才能放心地对不同账号同时执行。shell-task（无账号的系统任务）要求全局空闲。
_RUNNING: dict[str, str] = {}
_RUNNING_AT: dict[str, float] = {}
# 壳已取走、尚未回传的指令快照（warmup 进度核对与 requeue 防御都要用；
# 并发后「最后取走的一条」不再等于「正在执行的那条」，不能只留 _LAST_CMD）
_TAKEN: dict[str, dict] = {}


def _reap_running_locked(now: float) -> None:
    """执行中记账的泄漏兜底：壳崩溃后收不到回执，超时释放账号，别把队列卡死。"""
    for cmd_id, ts in list(_RUNNING_AT.items()):
        if now - ts > INFLIGHT_MAX_S:
            _RUNNING.pop(cmd_id, None)
            _RUNNING_AT.pop(cmd_id, None)
            _TAKEN.pop(cmd_id, None)


def _inflight_active(now: float) -> bool:
    for cmd_id, ts in list(_INFLIGHT.items()):
        if now - ts > _INFLIGHT_TIMEOUTS.get(cmd_id, INFLIGHT_MAX_S):
            _INFLIGHT.pop(cmd_id, None)
            _INFLIGHT_TIMEOUTS.pop(cmd_id, None)
    return bool(_INFLIGHT)


def publish(command: dict) -> str:
    """下发一条指令，返回指令 id。"""
    cmd_id = str(command.get("id") or f"cmd_{uuid.uuid4().hex[:10]}")
    payload = dict(command)
    payload["id"] = cmd_id
    payload["ts"] = int(time.time() * 1000)
    with _EVENT:
        _QUEUE.append(payload)
        _INFLIGHT[cmd_id] = time.time()
        if command.get("type") == "watch-video":
            try:
                seconds = float(command.get("seconds", 0))
                if 0 <= seconds <= 3600:
                    _INFLIGHT_TIMEOUTS[cmd_id] = max(INFLIGHT_MAX_S, seconds + 180)
            except (ValueError, TypeError):
                pass
        _EVENT.notify_all()
    return cmd_id


def next_command(agent: str = "") -> dict | None:
    """壳轮询取一条待执行指令（取走即出队，壳崩溃需重发）。

    按账号过滤：同一账号的上一条指令还在执行时，后面的指令留在队列里不派发
    （同账号窗口必须串行）；不同账号的指令照常派发（跨账号并行）。
    shell-task（清缓存等系统任务）没有账号归属，等全部在跑指令结束后才派发。
    """
    global _LAST_POLL, _POLLS, _LAST_CMD
    key = str(agent or "unknown")[:40]
    with _LOCK:
        now = time.time()
        _LAST_POLL = now
        _POLLS += 1
        rec = _AGENTS.setdefault(key, {"polls": 0, "taken": 0, "lastPoll": 0.0})
        rec["polls"] += 1
        rec["lastPoll"] = now
        _reap_running_locked(now)
        accounts_busy = set(_RUNNING.values())
        # 系统任务独占：它跑着的时候，任何账号指令都不派发（清缓存会打断页面）
        shell_running = any(
            str(_TAKEN.get(cid, {}).get("type") or "") == "shell-task"
            for cid in _RUNNING)
        chosen = -1
        for idx, cmd in enumerate(_QUEUE):
            if str(cmd.get("type") or "") == "shell-task":
                if not _RUNNING:
                    chosen = idx
                    break
                continue  # 系统任务独占执行：等在跑的全部结束
            if shell_running:
                continue  # 系统任务在跑：账号指令排队等它
            acc = str(cmd.get("accountId") or "")
            if acc in accounts_busy:
                continue  # 该账号的上一条还在执行：留在队列等它
            chosen = idx
            break
        if chosen < 0:
            return None
        cmd = dict(_QUEUE.pop(chosen))
        cmd_id = str(cmd.get("id") or "")
        _RUNNING[cmd_id] = str(cmd.get("accountId") or "")
        _RUNNING_AT[cmd_id] = now
        _TAKEN[cmd_id] = dict(cmd)
        while len(_TAKEN) > _MAX_RESULTS:
            _TAKEN.pop(next(iter(_TAKEN)))
        _LAST_CMD = dict(cmd)
        rec["taken"] += 1
        return cmd


def agent_alive() -> bool:
    """桌面壳的采集执行器最近有没有来取过指令。

    注意：壳执行指令时不再轮询。一条养号浏览要 40 秒以上，如果只看「最近轮询时间」，
    指令还在正常跑就会被判成掉线，后续几轮全部报「壳未加载执行器」（真机复现）。
    所以在飞指令存在时一律算在线。
    """
    with _LOCK:
        now = time.time()
        if _inflight_active(now):
            return True
        return _LAST_POLL > 0 and (now - _LAST_POLL) < AGENT_ALIVE_WINDOW_S


def mark_alive() -> None:
    """测试/自检用：假装壳刚轮询过。"""
    global _LAST_POLL
    with _LOCK:
        _LAST_POLL = time.time()


def submit_result(cmd_id: str, result: dict) -> bool:
    with _EVENT:
        if not cmd_id:
            return False
        _INFLIGHT.pop(str(cmd_id), None)
        _INFLIGHT_TIMEOUTS.pop(str(cmd_id), None)
        _RUNNING.pop(str(cmd_id), None)
        _RUNNING_AT.pop(str(cmd_id), None)
        _TAKEN.pop(str(cmd_id), None)
        if str(cmd_id) in _CANCELLED:
            return True  # A late receipt cannot turn a stopped command into success.
        _RESULTS[str(cmd_id)] = result if isinstance(result, dict) else {"ok": False}
        _EVENT.notify_all()
        while len(_RESULTS) > _MAX_RESULTS:
            _RESULTS.pop(next(iter(_RESULTS)))
    return True


def requeue_command(cmd_id: str) -> bool:
    """壳把刚取走、还没开始执行的指令放回队列头（防御路径，正常不该走到）。

    后端派发时已经按账号过滤（忙账号不派发），但壳侧还有两道自己的门槛：
    系统任务（shell-task）要求全局独占。万一两边判断有时间差，指令不能丢、
    也不能被当成功 —— 放回队头等下一轮重新派发。
    """
    with _EVENT:
        cmd = _TAKEN.pop(str(cmd_id), None)
        if not cmd:
            return False
        _RUNNING.pop(str(cmd_id), None)
        _RUNNING_AT.pop(str(cmd_id), None)
        if not any(str(c.get("id") or "") == str(cmd_id) for c in _QUEUE):
            _QUEUE.insert(0, dict(cmd))
        _EVENT.notify_all()
    return True


def wait_result(cmd_id: str, timeout: float = 180.0,
                stop_check=None) -> dict | None:
    """等指令结果。stop_check 为回调，返回 True 时提前放弃（支持手动停止）。"""
    global _LAST_POLL
    deadline = time.time() + timeout
    with _EVENT:
        while True:
            if stop_check is not None and stop_check():
                _cancel_locked(cmd_id, "manual_stop")
                return None
            if cmd_id in _RESULTS:
                # 刚收到壳的回报 = 壳刚刚活着。执行长指令的几十秒里壳不轮询，
                # 心跳会「过期」，紧接着下发写动作就会被误判成 desktop_required
                # （真机复现：读会话 36 秒 → 回来马上要发私信，被判不在线）。
                # 这里不调 mark_alive()：它会再拿一次同一把锁（_EVENT 底层就是 _LOCK），
                # 在持有条件变量锁时再取会直接死锁。
                _LAST_POLL = time.time()
                return _RESULTS.pop(cmd_id)
            remain = deadline - time.time()
            if remain <= 0:
                _cancel_locked(cmd_id, "timeout")
                return None
            _EVENT.wait(min(0.5, remain))


def _cancel_locked(cmd_id: str, reason: str) -> None:
    _INFLIGHT.pop(str(cmd_id), None)
    _INFLIGHT_TIMEOUTS.pop(str(cmd_id), None)
    _RUNNING.pop(str(cmd_id), None)
    _RUNNING_AT.pop(str(cmd_id), None)
    _TAKEN.pop(str(cmd_id), None)
    _RESULTS.pop(str(cmd_id), None)
    _QUEUE[:] = [cmd for cmd in _QUEUE if cmd.get("id") != cmd_id]
    _CANCELLED[str(cmd_id)] = reason
    while len(_CANCELLED) > _MAX_RESULTS:
        _CANCELLED.pop(next(iter(_CANCELLED)))


def command_status(cmd_id: str) -> dict:
    """Separate heartbeat: the desktop must receive stop while busy executing."""
    with _LOCK:
        reason = _CANCELLED.get(str(cmd_id), "")
        return {"cancelled": bool(reason), "reason": reason}


def warmup_progress_account(cmd_id: str) -> str:
    """Only the currently taken, uncancelled warmup may update its UI action."""
    with _LOCK:
        cmd = _TAKEN.get(str(cmd_id))
        if not cmd or str(cmd_id) not in _RUNNING:
            return ""
        if str(cmd.get("type") or "") != "warmup-watch":
            return ""
        return str(cmd.get("accountId") or "")


def peek_queue() -> list[dict]:
    with _LOCK:
        return list(_QUEUE)


def reset_for_tests() -> None:
    global _LAST_POLL, _POLLS, _LAST_CMD
    with _EVENT:
        _QUEUE.clear()
        _RESULTS.clear()
        _CANCELLED.clear()
        _AGENTS.clear()
        _INFLIGHT.clear()
        _INFLIGHT_TIMEOUTS.clear()
        _RUNNING.clear()
        _RUNNING_AT.clear()
        _TAKEN.clear()
        _LAST_POLL = 0.0
        _POLLS = 0
        _LAST_CMD = None
        _EVENT.notify_all()


def stats() -> dict[str, Any]:
    with _LOCK:
        now = time.time()
        _reap_running_locked(now)  # 壳崩溃留下的占位在这里也要回收，否则看板永远显示在跑
        agents = [{
            "id": k,
            "polls": v["polls"],
            "taken": v["taken"],
            "lastPollSecAgo": round(now - v["lastPoll"], 1) if v["lastPoll"] else None,
        } for k, v in _AGENTS.items()]
        return {
            "queued": len(_QUEUE),
            "running": len(_RUNNING),
            "runningAccounts": sorted(set(_RUNNING.values())),
            "results": len(_RESULTS),
            "polls": _POLLS,
            "lastPollSecAgo": round(now - _LAST_POLL, 1) if _LAST_POLL else None,
            "alive": _inflight_active(now) or (
                _LAST_POLL > 0 and (now - _LAST_POLL) < AGENT_ALIVE_WINDOW_S),
            "inflight": len(_INFLIGHT),
            "agents": agents,
            "agentCount": len(agents),
            "lastCommand": ({"type": _LAST_CMD.get("type"),
                             "keyword": _LAST_CMD.get("keyword"),
                             "id": _LAST_CMD.get("id")} if _LAST_CMD else None),
        }
