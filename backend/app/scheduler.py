"""定时排期执行器 —— 让「下次 09:00 继续」真的到点会跑。

为什么要有这个模块
------------------
壳侧已经把排期算得很完整：type(daily/once/after_finish)、startHm/endHm、
nextAction(start/stop)、nextActionAt，界面上也会显示「下次 09:00」。
但整条链路里**没有任何东西去消费 nextActionAt**：页面写着下次 9 点，到点
什么都不会发生。配置能存能读、重启能恢复，就是不执行——这比不做更糟，
因为它让人以为任务会自动跑。

这里补执行端：常驻 tick（默认 20 秒）读四类排期表，到点调用真实执行器。

诚实边界（重要）
----------------
只有「关键词/评论获客」有执行引擎（task_runner：本地模式处理已入库评论，
真机模式由桌面壳或 Playwright 采集）。线索采集、监控、养号目前**没有**
运行时，排期到点时不假装启动任务，而是落一条 schedule_runs(status=skipped)
并在任务运行日志里写明原因——「到点了但没引擎」必须能被看见，不能静默。

去重为什么不用 nextActionAt
---------------------------
壳保存排期是**整表覆盖**：用户改 A 任务时会连同 B 任务的旧 nextActionAt
一起提交，后端推进过的时间会被冲回过去，于是同一个窗口反复触发。
所以幂等放在库层：schedule_runs 的 UNIQUE(kind, task_id, window_key, action)，
同一窗口的同一个动作只可能落一次，重启、双实例、前端回写都免疫。

已有的安全机制必须尊重，而不是另造一套
--------------------------------------
- runtime_state.safe_mode_active = 1（合规熔断）→ 本次 tick 不触发任何启动，
  也不落 claim 行：熔断解除后只要还在窗口内，仍会照常启动。
- scheduler_config.max_concurrent → 同时运行的任务数上限。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app import leadgen_pools
from app import schedules as sched_store
from app.api import event_bus, task_logs, task_runner

KINDS = ("leadgen", "entity", "monitor", "warmup")

DEFAULT_INTERVAL_S = float(os.getenv("SCHEDULER_INTERVAL_S", "20"))

# 瞬时失败（账号正忙 / 壳没加载执行器 / 启动超时）不放弃整个窗口，隔一段时间重试
RETRY_DELAY_MS = 10 * 60 * 1000
MAX_ATTEMPT = 6

# 有执行引擎的类型。四类都已接上运行时：leadgen 走 task_runner，
# entity/monitor/warmup 走 app.runtime.registry（真机链路，壳内已登录浏览器）。
# 引擎不可用（壳没开/未登录）时由 registry 返回 noEngine，这里落 skipped 并写明原因，
# 仍然不伪造启动。
ENGINES = {"leadgen": True, "entity": True, "monitor": True, "warmup": True}

_DDL = """CREATE TABLE IF NOT EXISTS schedule_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    task_id TEXT NOT NULL,
    window_key TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'fired',
    detail TEXT NOT NULL DEFAULT '',
    run_ref TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 1,
    retry_at INTEGER,
    fired_at TEXT NOT NULL,
    settled_at TEXT,
    finished_at TEXT,
    UNIQUE(kind, task_id, window_key, action)
)"""

_TICK_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"thread": None, "stop": None, "last_tick": None,
                          "last_result": None, "last_error": "",
                          # 执行器离线跟踪：None 表示在线或从未离线过
                          "offline_since_ms": None, "offline_notice": ""}

# 执行器离线多久才整体暂停本轮。
# 为什么需要宽限：壳/后端重启、页面刷新都会带来几十秒的心跳空洞，
# 一离线就暂停会让即将到点的任务白白错过时间窗口。3 分钟足够覆盖重启。
# SCHEDULER_OFFLINE_GRACE_S 只用于验收/联调（默认不设就是 180 秒），
# 生产上不需要也不应该调小。
OFFLINE_GRACE_MS = 180_000


def offline_grace_ms() -> int:
    try:
        sec = int(os.getenv("SCHEDULER_OFFLINE_GRACE_S", "0") or 0)
    except ValueError:
        sec = 0
    return sec * 1000 if sec > 0 else OFFLINE_GRACE_MS


# --------------------------------------------------------------------------
# 时间与窗口
# --------------------------------------------------------------------------
def _local(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _hm(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    return default


def _window(day: datetime, start_hm: Any, end_hm: Any) -> tuple[int, int]:
    """某一天对应的窗口 [开始, 结束)。end<=start 视为跨零点（22:00–06:00）。"""
    sh, sm = _hm(start_hm, (9, 0))
    eh, em = _hm(end_hm, (18, 0))
    start = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = day.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return _ms(start), _ms(end)


def current_window(now_ms: int, start_hm: Any, end_hm: Any) -> tuple[str, int, int] | None:
    """now 落在哪个窗口里：返回 (窗口键, 开始, 结束)。窗口键 = 窗口起始那一天。"""
    today = _local(now_ms).replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (today, today - timedelta(days=1)):
        start, end = _window(day, start_hm, end_hm)
        if start <= now_ms < end:
            return day.date().isoformat(), start, end
    return None


def last_ended_window(now_ms: int, start_hm: Any, end_hm: Any) -> tuple[str, int, int] | None:
    today = _local(now_ms).replace(hour=0, minute=0, second=0, microsecond=0)
    ended: list[tuple[int, str, int]] = []
    for back in (0, 1, 2):
        day = today - timedelta(days=back)
        start, end = _window(day, start_hm, end_hm)
        if end <= now_ms:
            ended.append((end, day.date().isoformat(), start))
    if not ended:
        return None
    end, key, start = max(ended)
    return key, start, end


def next_window_start(now_ms: int, start_hm: Any, end_hm: Any) -> int:
    today = _local(now_ms).replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (today, today + timedelta(days=1)):
        start, _ = _window(day, start_hm, end_hm)
        if start > now_ms:
            return start
    return now_ms + 24 * 60 * 60 * 1000


def due_actions(kind: str, task_id: str, sched: Any, now_ms: int) -> list[dict]:
    """算出这条排期在 now 时刻该做什么（可能为空 / 一条）。"""
    if not isinstance(sched, dict):
        return []
    if sched.get("enabled") is False:
        return []
    typ = str(sched.get("type") or "daily").strip().lower()
    out: list[dict] = []

    if typ == "once":
        try:
            run_at = int(sched.get("runAt") or 0)
        except (TypeError, ValueError):
            run_at = 0
        if not run_at or run_at > now_ms:
            return []
        return [{"action": "start", "window": f"once:{run_at}", "at": run_at,
                 "label": "一次性任务到点"}]

    if typ == "after_finish" or (sched.get("nextActionAt") and typ not in {"daily"}):
        action = str(sched.get("nextAction") or "start").strip().lower()
        try:
            at = int(sched.get("nextActionAt") or 0)
        except (TypeError, ValueError):
            at = 0
        if not at or at > now_ms or action not in {"start", "stop"}:
            return []
        return [{"action": action, "window": f"next:{action}:{at}", "at": at,
                 "label": "任务结束后按间隔继续"}]

    # daily
    win = current_window(now_ms, sched.get("startHm"), sched.get("endHm"))
    if win:
        key, start, _end = win
        return [{"action": "start", "window": f"w:{key}", "at": start,
                 "label": "进入每日执行时段"}]
    ended = last_ended_window(now_ms, sched.get("startHm"), sched.get("endHm"))
    if ended:
        key, _start, end = ended
        # 只有「这个窗口确实启动过」才需要下发停止；没启动过就什么都不做
        return [{"action": "stop", "window": f"w:{key}", "at": end,
                 "label": "每日执行时段结束"}]
    return out


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------
def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    con.execute(_DDL)
    # 旧库（本功能上线前建的 schedule_runs）缺列时补齐，不让升级卡住
    cols = {row[1] for row in con.execute("PRAGMA table_info(schedule_runs)")}
    if "finished_at" not in cols:
        try:
            con.execute("ALTER TABLE schedule_runs ADD COLUMN finished_at TEXT")
        except sqlite3.Error:
            pass
    return con


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _running_count() -> int:
    """当前真实在跑的任务数：评论获客线程 + 三类运行时实例。"""
    total = len(task_runner.active_ids())
    try:
        from app.runtime import registry
        total += len(registry.active())
    except Exception:  # noqa: BLE001
        pass
    return total


def _safe_mode_on(db_path) -> bool:
    """合规熔断：读不到表就当没开（历史库没有 runtime_state 的表结构差异）。"""
    try:
        con = sqlite3.connect(str(db_path), timeout=10)
        try:
            row = con.execute(
                "SELECT safe_mode_active FROM runtime_state LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row and int(row[0] or 0))


def _now_wall_ms() -> int:
    """物理时钟（毫秒）。

    离线时长必须用物理时间算，不能用排期的 now_ms —— 后者是业务时间，
    可以被人为传入/测试拨动。混用会导致「模拟时间跳了 5 分钟」被误判成
    「执行器离线 5 分钟」，把正常排期整体暂停掉。
    """
    return int(time.time() * 1000)


def _executor_online(db_path) -> tuple[bool, str]:
    """当前有没有任何可用执行引擎（壳在线 / 独立引擎已登录）。"""
    try:
        from app.runtime import browser as browser_layer
        st = browser_layer.status(db_path)
        return st.get("engine") != "none", str(st.get("engine") or "none")
    except Exception as exc:  # noqa: BLE001 引擎层异常不应让排期线程崩
        return False, f"引擎状态不可读: {type(exc).__name__}: {exc}"


def _offline_pause(db_path, now_ms: int | None = None) -> dict | None:
    """执行器长时间不可用 → 本轮整体暂停。

    为什么必须暂停
    --------------
    壳被强杀/崩溃后后端还活着，排期到点照样逐条触发，然后每条都落 skipped。
    第二天执行历史里是几百行「到点了没做事」，真正的原因（执行器不在）被淹掉，
    用户只会判断成「系统坏了」。整体暂停 + 一条明确原因，才看得懂。

    为什么有宽限期
    --------------
    壳重启、后端重启、页面刷新都会带来几十秒的心跳空洞。立刻暂停会让马上
    到点的任务错过窗口，代价比多刷几条 skipped 更大。

    暂停期间不动 schedule_runs、不推进 nextAction：执行器回来后下一轮照常触发，
    不需要人工「恢复」。
    """
    now_wall = int(now_ms if now_ms is not None else _now_wall_ms())
    online, engine = _executor_online(db_path)
    if online:
        if _STATE.get("offline_since_ms") or _STATE.get("offline_notice"):
            _STATE["offline_since_ms"] = None
            _STATE["offline_notice"] = ""
        return None

    since = _STATE.get("offline_since_ms")
    if not since:
        since = now_wall
        _STATE["offline_since_ms"] = now_wall
    waited = now_wall - int(since)
    if waited < offline_grace_ms():
        return None

    sec = round(waited / 1000)
    reason = (f"采集执行器已离线 {sec} 秒（engine={engine}），本轮不触发任何任务；"
              f"执行器恢复后自动继续")
    _STATE["offline_notice"] = reason
    return {"sinceMs": int(since), "waitedSec": sec, "engine": engine, "reason": reason}



def max_concurrent(db_path) -> int:
    """并发上限取自 scheduler_config.max_concurrent（表结构默认 3）。

    取不到时用 3：历史库没有这张表时若退回 1，会出现「已有一个任务在跑就
    永久不启动」的错觉，比不设上限更难排查。
    """
    try:
        con = sqlite3.connect(str(db_path), timeout=10)
        try:
            row = con.execute(
                "SELECT max_concurrent FROM scheduler_config LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 3
    try:
        return max(1, min(int(row[0]), 10)) if row else 3
    except (TypeError, ValueError):
        return 3


def _claim(con: sqlite3.Connection, kind: str, task_id: str, window: str,
           action: str, now_ms: int) -> int | None:
    """占位：同一 (kind, task, window, action) 只能成功占一次。"""
    cur = con.execute(
        "INSERT OR IGNORE INTO schedule_runs(kind, task_id, window_key, action, "
        "status, detail, run_ref, attempt, retry_at, fired_at) "
        "VALUES(?,?,?,?, 'fired', '', '', 1, NULL, ?)",
        (kind, task_id, window, action, _iso_now()))
    if cur.rowcount:
        return int(cur.lastrowid)
    row = con.execute(
        "SELECT * FROM schedule_runs WHERE kind=? AND task_id=? AND window_key=? AND action=?",
        (kind, task_id, window, action)).fetchone()
    if row is None:
        return None
    retry_at = row["retry_at"]
    if (row["status"] == "skipped" and retry_at and int(retry_at) <= now_ms
            and int(row["attempt"]) < MAX_ATTEMPT):
        con.execute(
            "UPDATE schedule_runs SET status='fired', detail='', retry_at=NULL, "
            "attempt=attempt+1, fired_at=?, settled_at=NULL WHERE id=?",
            (_iso_now(), row["id"]))
        return int(row["id"])
    return None


def _settle(con: sqlite3.Connection, rid: int, status: str, detail: str,
            run_ref: str = "", retry: bool = False, now_ms: int | None = None) -> None:
    con.execute(
        "UPDATE schedule_runs SET status=?, detail=?, run_ref=?, retry_at=?, settled_at=? "
        "WHERE id=?",
        (status, detail[:500], run_ref,
         (now_ms or 0) + RETRY_DELAY_MS if retry else None, _iso_now(), rid))


def _started_run_ref(con: sqlite3.Connection, kind: str, task_id: str,
                     window: str) -> str:
    row = con.execute(
        "SELECT run_ref FROM schedule_runs WHERE kind=? AND task_id=? AND window_key=? "
        "AND action='start' AND status='fired'", (kind, task_id, window)).fetchone()
    return str(row["run_ref"] or "") if row else ""


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def _log_scheduled_start(db_path, task_id: str, runtime_id: str, account_id: str,
                         message: str) -> None:
    """落库 + 推一条任务级事件。

    只落库不够：关键词获客页的「运行日志」抽屉只渲染实时事件
    （entity-legen-task-event），不主动拉历史。页面开着的时候要能立刻看到
    「定时启动」这一行，否则用户会以为到点没反应。
    """
    task_logs.append(db_path, runtime_id, task_id, account_id, message)
    try:
        event_bus.publish("entity-leadgen-task-event", {
            "type": "log", "taskId": task_id, "accountId": account_id,
            "ts": int(time.time() * 1000), "message": message, "level": "normal"})
    except Exception:  # noqa: BLE001 事件推送失败不能影响执行
        pass


def _leadgen_payload(rec: dict, last_run: dict, account_id: str, now_ms: int) -> dict:
    """按壳 start-task 的契约拼启动报文（见 index bundle 里 Et 的构造）。"""
    cfg = rec.get("configSnapshot") if isinstance(rec.get("configSnapshot"), dict) else {}
    runtime_id = str(last_run.get("automationTaskId") or f"task_{account_id}_{now_ms}")
    payload = dict(cfg)
    payload.update({
        "leadgenTaskId": str(rec.get("id") or ""),
        "taskId": runtime_id,
        "taskName": str(rec.get("name") or "评论获客任务"),
        "accountId": account_id,
        "platform": last_run.get("platform") or "douyin",
        "keywords": last_run.get("keywords") or cfg.get("keywords"),
        "nickname": last_run.get("nickname"),
        "name": last_run.get("name"),
    })
    return {k: v for k, v in payload.items() if v is not None}


def _start_leadgen(db_path, task_id: str, now_ms: int) -> tuple[str, str, str]:
    records = leadgen_pools.load_records(db_path)
    rec = records.get(task_id)
    if not isinstance(rec, dict):
        return "skipped", f"任务记录不存在（可能已删除）：{task_id}", ""
    if str(rec.get("entityKind") or "") == "entity":
        return "skipped", "该记录属于线索采集任务，请用线索采集的排期通道启动", ""
    if not task_runner.executor_ready():
        # 执行器未接入时不要在排期窗口里反复写失败运行记录，只记一条跳过原因
        return "skipped", "评论获客执行器未接入「发表评论」能力，本时段未启动", ""

    runs = [r for r in (rec.get("runs") or []) if isinstance(r, dict)]
    last_run = runs[-1] if runs else {}
    cfg = rec.get("configSnapshot") if isinstance(rec.get("configSnapshot"), dict) else {}
    account_id = str(last_run.get("accountId") or cfg.get("accountId") or "").strip()
    if not account_id:
        return "skipped", "任务里没有可执行账号（历史运行记录为空），未启动", ""

    if account_id in task_runner.active_account_ids():
        return "skipped", "该账号正在运行其他任务，稍后重试", ""

    payload = _leadgen_payload(rec, last_run, account_id, now_ms)
    if str(payload.get("taskId")) in task_runner.active_ids():
        return "skipped", "同一任务已在运行，本次不重复启动", ""

    res = task_runner.start(db_path, payload)
    if not res.get("success"):
        return "failed", str(res.get("msg") or res.get("error") or "启动失败"), ""

    # 记录状态同步为 running：否则列表显示「已停止」而实际在跑
    try:
        leadgen_pools.update_task_record(
            db_path, task_id,
            {"status": "running", "startedAt": now_ms,
             "endReason": None, "endedAt": None})
    except sqlite3.Error:
        pass

    mode = str(res.get("mode") or "local")
    note = ("真机采集" if mode == "real" else "本地模式，只处理已入库评论")
    _log_scheduled_start(
        db_path, task_id, str(payload["taskId"]), account_id,
        f"定时启动：进入排期时段，开始执行（{note}）")
    return "fired", f"已启动（{note}）", str(payload["taskId"])


def _stop_run(con: sqlite3.Connection, kind: str, task_id: str, window: str,
              now_ms: int, db_path=None) -> tuple[str, str, str]:
    ref = _started_run_ref(con, kind, task_id, window)
    if not ref:
        return "skipped", "本时段没有由排期启动过任务，无需停止", ""
    if kind == "leadgen":
        task_runner.stop(ref)
    elif kind in {"entity", "monitor", "warmup"} and db_path is not None:
        from app.runtime import registry
        registry.stop(db_path, kind, task_id)
    return "fired", "已下发停止（进入时段外）", ref


def _dispatch(con: sqlite3.Connection, kind: str, task_id: str, action: str,
              window: str, now_ms: int, db_path) -> tuple[str, str, str]:
    if action == "stop":
        return _stop_run(con, kind, task_id, window, now_ms, db_path)
    if not ENGINES.get(kind):
        label = {"entity": "线索采集", "monitor": "监控", "warmup": "养号"}.get(kind, kind)
        return ("skipped",
                f"{label}暂无执行引擎（该类型没有运行时），计划已到点但未启动任务", "")
    if kind == "leadgen":
        return _start_leadgen(db_path, task_id, now_ms)
    if kind in {"entity", "monitor", "warmup"}:
        from app.runtime import registry
        res = registry.start(db_path, kind, task_id, trigger="schedule")
        if res.get("success"):
            run_ref = str(res.get("runId") or "")
            note = "已在运行（本窗口内不重复启动）" if res.get("already") else \
                f"已启动运行时（引擎 {res.get('engine') or '未知'}）"
            return "fired", note, run_ref
        detail = str(res.get("error") or "启动失败")
        # 引擎不可用 / 账号忙 / 没有登录态都属于「环境没准备好」，
        # 隔 RETRY_DELAY 再试；任务记录本身有问题才是硬失败。
        return ("skipped" if (res.get("noEngine") or res.get("busy")) else "failed",
                detail, "")
    return "skipped", f"未知排期类型：{kind}", ""


def _transient(detail: str) -> bool:
    return any(word in detail for word in (
        "正在运行", "已在运行", "正在执行其他任务", "没有可用的执行引擎",
        "未加载采集执行器", "未登录", "超时"))


def _interval_ms(entry: dict) -> int:
    """「结束后间隔」的最小间隔：低于 5 分钟按 5 分钟算。

    未做下限时，用户填 0 小时 0 分钟会让任务跑完立刻再来一轮，
    在真机模式下等于不间断打平台接口——这是风控红线，不能由配置决定。
    """
    try:
        hours = int(entry.get("intervalHours") or 0)
        minutes = int(entry.get("intervalMinutes") or 0)
    except (TypeError, ValueError):
        hours = minutes = 0
    total = hours * 3600 + minutes * 60
    return max(300, min(total, 24 * 3600)) * 1000


def _rearm_after_finish(db_path, kind: str, task_id: str, now_ms: int) -> None:
    """after_finish 类排期的下一轮：本轮结束后隔 interval 再启动一次。"""
    table = sched_store.get_map(db_path, kind)
    entry = table.get(task_id)
    if not isinstance(entry, dict) or entry.get("enabled") is False:
        return
    if str(entry.get("type") or "").strip().lower() != "after_finish":
        return
    entry.update({"nextAction": "start",
                  "nextActionAt": now_ms + _interval_ms(entry),
                  "lastEndedAt": now_ms, "runStartedAt": None, "retryCount": 0})
    table[task_id] = entry
    sched_store.save_map(db_path, kind, table)


def _reconcile(con: sqlite3.Connection, db_path, now_ms: int) -> list[dict]:
    """收尾：排期启动的任务线程结束后，把任务记录从 running 落回终态。

    不做这一步的后果：09:00 定时启动 → 09:07 跑完 → 列表一直显示「运行中」，
    用户既不知道已经结束，也无法再次启动（壳认为它还在跑）。
    线程是否还活着以 task_runner 的注册表为准；重启后表是空的，
    此时按「已结束」收尾是诚实的——确实没有任何线程在跑。
    """
    active = set(task_runner.active_ids())
    try:
        from app.runtime import registry as _rt
        active |= {h["runId"] for h in _rt.active()}
    except Exception:  # noqa: BLE001 运行时不可用不影响评论获客的收尾
        pass
    rows = con.execute(
        "SELECT * FROM schedule_runs WHERE action='start' AND status='fired' "
        "AND finished_at IS NULL").fetchall()
    closed: list[dict] = []
    for row in rows:
        ref = str(row["run_ref"] or "")
        if ref and ref in active:
            continue
        con.execute("UPDATE schedule_runs SET finished_at=? WHERE id=?",
                    (_iso_now(), row["id"]))
        closed.append({"kind": row["kind"], "taskId": row["task_id"],
                       "runRef": ref})
    # 先提交 schedule_runs，再用各自的连接改任务记录：任务记录走 leadgen_pools
    # 的独立连接，占位事务不放手会把它们卡在 database is locked 上然后被静默吞掉。
    con.commit()
    for item in closed:
        if not item["runRef"]:
            continue
        try:
            rec = leadgen_pools.load_records(db_path).get(str(item["taskId"]))
        except sqlite3.Error:
            rec = None
        if isinstance(rec, dict) and str(rec.get("status")) == "running":
            try:
                leadgen_pools.update_task_record(
                    db_path, str(item["taskId"]),
                    {"status": "completed", "endedAt": int(time.time() * 1000),
                     "endReason": "completed"})
            except sqlite3.Error:
                pass
        # 「结束后间隔执行」类排期在这里排下一轮，否则只会跑第一次再不回来
        try:
            _rearm_after_finish(db_path, str(item["kind"]), str(item["taskId"]), now_ms)
        except sqlite3.Error:
            pass
    return closed


def _advance(db_path, kind: str, task_id: str, action: str, window: str,
             now_ms: int) -> None:
    """把 nextAction/nextActionAt 推到下一个节点（纯展示用途，正确性靠 schedule_runs）。"""
    table = sched_store.get_map(db_path, kind)
    entry = table.get(task_id)
    if not isinstance(entry, dict):
        return
    typ = str(entry.get("type") or "daily").strip().lower()
    if action == "start":
        if typ == "daily":
            win = current_window(now_ms, entry.get("startHm"), entry.get("endHm"))
            end = win[2] if win else now_ms + 60 * 60 * 1000
            entry.update({"nextAction": "stop", "nextActionAt": end,
                          "activeWindowId": window, "runStartedAt": now_ms,
                          "retryCount": 0})
        elif typ == "once":
            entry.update({"enabled": False, "nextAction": "stop",
                          "nextActionAt": now_ms})
        else:
            # after_finish（「结束后间隔 X 小时 Y 分钟」）：本轮跑完再算下一轮，
            # 所以这里只标记「等结束」，真正的下一次时间在收尾对账里计算。
            entry.update({"nextAction": "wait_finish", "nextActionAt": 0,
                          "runStartedAt": now_ms})
    else:
        if typ == "daily":
            entry.update({"nextAction": "start",
                          "nextActionAt": next_window_start(
                              now_ms, entry.get("startHm"), entry.get("endHm")),
                          "activeWindowId": None, "lastEndedAt": now_ms})
        else:
            entry.update({"nextAction": "start",
                          "nextActionAt": now_ms + 30 * 60 * 1000,
                          "lastEndedAt": now_ms})
    table[task_id] = entry
    sched_store.save_map(db_path, kind, table)


# --------------------------------------------------------------------------
# 账号数据每日自动统计（save-account-data-schedule 的到点执行）
# --------------------------------------------------------------------------
_STATS_DAILY: dict[str, Any] = {"day": "", "attempt": 0.0}
_STATS_RETRY_GAP_S = 600.0  # 失败后至少隔 10 分钟再试（壳没开时不要每轮都撞）


def _maybe_collect_account_stats(db_path, now_ms: int) -> bool:
    """到配置时刻触发一轮账号数据统计；返回本轮是否触发了。

    - 时刻由 save-account-data-schedule 落库（HH:MM），未设置（00:00）不触发；
    - 成功后当天不再触发；失败（壳不在线等）不占当天名额，但限频重试；
    - collect 在独立线程里跑，schedule-runner 的 tick 不被逐账号读主页拖住。
    """
    target = str(sched_store.get_account_data_time(db_path) or "").strip()
    if not target or target == "00:00":
        return False
    now = time.localtime(now_ms / 1000)
    today = time.strftime("%Y-%m-%d", now)
    if _STATS_DAILY.get("day") == today:
        return False
    if time.strftime("%H:%M", now) < target:
        return False
    now_ts = now_ms / 1000
    if now_ts - float(_STATS_DAILY.get("attempt") or 0.0) < _STATS_RETRY_GAP_S:
        return False
    _STATS_DAILY["attempt"] = now_ts

    def job():
        try:
            from app.api import account_data

            res = account_data.collect(db_path, {})
            if res.get("success"):
                _STATS_DAILY["day"] = today
        except Exception as exc:  # noqa: BLE001 - 统计失败不影响调度主循环
            _STATE["last_error"] = f"account_stats: {type(exc).__name__}: {exc}"

    threading.Thread(target=job, name="account-stats-daily", daemon=True).start()
    return True


def run_once(db_path, now_ms: int | None = None) -> dict:
    """跑一轮：读排期 → 到点触发 → 落记录。可被手工调用（诊断用）。"""
    now_ms = int(now_ms or time.time() * 1000)
    if not _TICK_LOCK.acquire(blocking=False):
        return {"success": False, "reason": "上一轮还没跑完，本次跳过"}
    try:
        if _safe_mode_on(db_path):
            _STATE["last_tick"] = _iso_now()
            _STATE["last_error"] = "合规安全模式已开启，本轮不触发任何任务"
            return {"success": True, "safeMode": True, "fired": [], "skipped": [],
                    "reason": _STATE["last_error"]}

        # 执行器长时间不在线：整体暂停，而不是逐条落 skipped。
        # 这里刻意不传 now_ms（业务时间），离线时长按物理时钟算。
        offline = _offline_pause(db_path)
        if offline:
            _STATE["last_tick"] = _iso_now()
            _STATE["last_error"] = offline["reason"]
            return {"success": True, "pausedByOffline": True,
                    "fired": [], "skipped": [], "reason": offline["reason"],
                    "executor": offline}

        result: dict[str, Any] = {"success": True, "fired": [], "skipped": [],
                                  "closed": [], "already": 0, "checked": 0}
        # 账号数据每日自动统计：到配置的时刻（save-account-data-schedule）
        # 触发一轮 collect。失败（壳不在线等）不占当天名额，限频重试；
        # 成功后当天不再触发。独立线程跑——collect 要逐账号读主页，
        # 不能阻塞 schedule-runner 的下一轮 tick。
        stats_fired = _maybe_collect_account_stats(db_path, now_ms)
        if stats_fired:
            result["accountStatsTriggered"] = True

        limit = max_concurrent(db_path)
        running_now = _running_count()
        con = _connect(db_path)
        try:
            result["closed"] = _reconcile(con, db_path, now_ms)
            for kind in KINDS:
                table = sched_store.get_map(db_path, kind)
                for task_id, sched in table.items():
                    for item in due_actions(kind, task_id, sched, now_ms):
                        result["checked"] += 1
                        if item["action"] == "stop" and not _started_run_ref(
                                con, kind, task_id, item["window"]):
                            continue
                        if item["action"] == "start" and running_now >= limit:
                            result["skipped"].append({
                                "kind": kind, "taskId": task_id,
                                "detail": f"已达并发上限 {limit}，本轮到点未启动（下轮重试）"})
                            continue
                        rid = _claim(con, kind, task_id, item["window"],
                                     item["action"], now_ms)
                        # 立刻提交：执行器/日志/任务记录都会用自己的连接写库，
                        # 占位事务不放手会把它们卡在 database is locked 上等到超时。
                        con.commit()
                        if rid is None:
                            result["already"] += 1
                            continue
                        status, detail, run_ref = _dispatch(
                            con, kind, task_id, item["action"], item["window"],
                            now_ms, db_path)
                        _settle(con, rid, status, detail, run_ref,
                                retry=status != "fired" and _transient(detail),
                                now_ms=now_ms)
                        con.commit()
                        if status == "fired" and item["action"] == "start":
                            running_now += 1
                        record = {"kind": kind, "taskId": task_id,
                                  "window": item["window"], "action": item["action"],
                                  "status": status, "detail": detail}
                        (result["fired"] if status == "fired"
                         else result["skipped"]).append(record)
                        # 只有真的执行了才推进 nextAction：没执行就保留原时间，
                        # 界面继续显示「待执行」，不会假装已经跑过。
                        if status in {"fired", "failed"}:
                            _advance(db_path, kind, task_id, item["action"],
                                     item["window"], now_ms)
        finally:
            con.close()
        _STATE["last_tick"] = _iso_now()
        _STATE["last_result"] = result
        _STATE["last_error"] = ""
        return result
    except Exception as exc:  # noqa: BLE001 后台线程不能因单轮异常退出
        _STATE["last_error"] = f"{type(exc).__name__}: {exc}"
        _STATE["last_tick"] = _iso_now()
        return {"success": False, "reason": _STATE["last_error"],
                "fired": [], "skipped": []}
    finally:
        _TICK_LOCK.release()


# --------------------------------------------------------------------------
# 状态（诊断用：让人能确认执行器到底在不在跑）
# --------------------------------------------------------------------------
def _runtime_status(db_path) -> dict:
    """三类运行时的引擎可用性与在跑实例（壳没开/未登录时如实说不可用）。"""
    try:
        from app.runtime import registry
        return registry.status(db_path)
    except Exception as exc:  # noqa: BLE001
        return {"engine": {"engine": "none"}, "active": [],
                "error": f"{type(exc).__name__}: {exc}"}


def status(db_path) -> dict:
    now_ms = int(time.time() * 1000)
    upcoming: list[dict] = []
    for kind in KINDS:
        for task_id, sched in sched_store.get_map(db_path, kind).items():
            if not isinstance(sched, dict) or sched.get("enabled") is False:
                continue
            typ = str(sched.get("type") or "daily").lower()
            at = 0
            action = str(sched.get("nextAction") or "start")
            in_window = False
            if typ == "daily":
                win = current_window(now_ms, sched.get("startHm"), sched.get("endHm"))
                if win:
                    # 时段进行中：下一个节点是「到点停止」，不是「开始」
                    in_window = True
                    action, at = "stop", win[2]
                else:
                    action = "start"
                    at = next_window_start(now_ms, sched.get("startHm"), sched.get("endHm"))
            elif typ == "once":
                at = int(sched.get("runAt") or 0)
            else:
                at = int(sched.get("nextActionAt") or 0)
            upcoming.append({
                "kind": kind, "taskId": task_id, "type": typ,
                "engine": bool(ENGINES.get(kind)),
                "inWindow": in_window,
                "at": at,
                "atTime": (datetime.fromtimestamp(at / 1000).strftime("%m-%d %H:%M")
                           if at else ""),
                "action": action,
            })
    upcoming.sort(key=lambda i: i["at"] or 0)

    recent: list[dict] = []
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT * FROM schedule_runs ORDER BY id DESC LIMIT 12").fetchall()
        finally:
            con.close()
        recent = [dict(r) for r in rows]
    except sqlite3.Error:
        pass

    thread = _STATE.get("thread")
    online, engine = _executor_online(db_path)
    offline_since = _STATE.get("offline_since_ms")
    return {
        "success": True,
        "running": bool(thread and thread.is_alive()),
        "intervalSeconds": DEFAULT_INTERVAL_S,
        "lastTickAt": _STATE.get("last_tick"),
        "lastError": _STATE.get("last_error"),
        "safeMode": _safe_mode_on(db_path),
        # 执行器状态：排期「暂停了没有、为什么暂停」必须能一眼看到，
        # 否则只能从「执行历史里一堆 skipped」反推。
        "executor": {
            "online": online,
            "engine": engine,
            "offlineSec": (round((_now_wall_ms() - int(offline_since)) / 1000)
                           if offline_since else 0),
            "paused": bool(_STATE.get("offline_notice")),
            "notice": _STATE.get("offline_notice") or "",
            "graceSec": offline_grace_ms() // 1000,
        },
        "maxConcurrent": max_concurrent(db_path),
        "activeRuns": _running_count(),
        "engines": ENGINES,
        "runtime": _runtime_status(db_path),
        "upcoming": upcoming[:10],
        "recent": recent,
    }


# --------------------------------------------------------------------------
# 常驻线程
# --------------------------------------------------------------------------
def _loop(db_path: Path, interval: float, stop: threading.Event) -> None:
    # 先等 3 秒让服务把启动收尾，然后立刻查一轮：否则每次重启都要空转一个
    # interval 才处理，整点重启就会漏掉该时段的启动。
    if stop.wait(3):
        return
    while True:
        try:
            run_once(db_path)
        except Exception as exc:  # noqa: BLE001
            _STATE["last_error"] = f"{type(exc).__name__}: {exc}"
        if stop.wait(interval):
            return


def start(db_path, interval: float | None = None) -> bool:
    """启动常驻 tick。重复调用无副作用（已有活线程直接返回 True）。"""
    thread = _STATE.get("thread")
    if thread and thread.is_alive():
        return True
    if os.getenv("SCHEDULER_OFF", "0") == "1":
        return False
    stop = threading.Event()
    _STATE["stop"] = stop
    _STATE["thread"] = threading.Thread(
        target=_loop, args=(Path(db_path), float(interval or DEFAULT_INTERVAL_S), stop),
        name="schedule-runner", daemon=True)
    _STATE["thread"].start()
    return True


def stop() -> None:
    event = _STATE.get("stop")
    if isinstance(event, threading.Event):
        event.set()
    _STATE["thread"] = None
