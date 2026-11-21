"""作品发布运行器：把「队列里的每一篇」真的交给壳去上传。

为什么不是「把通道放行」
------------------------
`start-work-publish-task-runtime` 一旦只是返回 success，页面会显示「已开始自动发布」，
但实际上什么都没发，用户等到第二天才发现作品没上线。所以这里的最小闭环是：

1. 启动前逐条校验（账号、素材文件存在、标题/描述长度），不合格直接拒绝；
2. 桌面壳不在线 → 拒绝启动（发布必须用壳里的登录态，后端没有会话）；
3. 通过壳的 `publish-submit` 真机执行，结果分三档落库：
   `published`（页面给了成功信号）/ `uncertain`（点了发布但没拿到成功提示，可能审核中）
   / `failed`（明确失败或前置条件不满足，带原因）；
4. 定时发布：runAt 未到就挂起等待，等待期间可被 stop 打断，**不会提前发**。

`uncertain` 这一档很关键：重复发布对账号是伤害，所以「不确定」绝不自动重试，
也不显示成成功，交给用户在详情里确认。
"""
from __future__ import annotations

import os
import random
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from app.api import task_store

TERMINAL = {"published", "uncertain"}
CLAIMABLE_EXCLUDE = TERMINAL | {"failed"}   # failed 也绝不重发（登录失效跳账号依赖这点）
TITLE_MAX = {"video": 30, "image": 20}
DESC_MAX = 1000
# 定时分界：距离发布时间超过这个时长，优先把定时交给平台（官方定时发布），
# 软件不用一直挂着等；在这个时长内由软件等到点再提交（页面口径 2 小时）。
OFFICIAL_SCHEDULE_MIN_MS = 2 * 3600 * 1000
MAX_PARALLEL_ACCOUNTS = 2

_RUNS: dict[str, dict] = {}          # task_id → {stop: Event, thread: Thread}
_LOCK = threading.RLock()
_BUSY_ACCOUNTS: dict[str, set] = {}  # task_id → 正在被 worker 占用的账号（同账号串行）


def _connect(db_path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _set_db_status(db_path: Path | str, task_id: str, status: str, error: str = "") -> None:
    """只动 tasks.status：页面靠它判断「运行中」，不写就会一直显示停止。"""
    con = _connect(db_path)
    try:
        con.execute("UPDATE tasks SET status=?, error_message=?, updated_at=? WHERE id=?",
                    (status, error, time.strftime("%Y-%m-%d %H:%M:%S"), task_id))
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        con.close()


def _patch_task(db_path: Path | str, task_id: str, **changes) -> dict | None:
    """读-改-写整条 payload（任务配置本身就是前端 JSON，只能整体覆盖）。"""
    with _LOCK:
        rec = task_store.get_task(db_path, "publish", task_id)
        if not rec:
            return None
        rec.update(changes)
        task_store.save_task(db_path, "publish", rec)
        return rec


def _norm_items(rec: Mapping) -> list[dict]:
    items = rec.get("items")
    return [dict(i) for i in items if isinstance(i, Mapping)] if isinstance(items, list) else []


def _limits(item: Mapping) -> str:
    media = item.get("media") if isinstance(item.get("media"), Mapping) else {}
    kind = "image" if str(media.get("type") or "video") == "image" else "video"
    title = str(item.get("title") or "")
    desc = str(item.get("description") or "")
    title_max = TITLE_MAX[kind]
    if len(title) > title_max:
        return f"{'图文' if kind == 'image' else '视频'}标题不能超过 {title_max} 字"
    if len(desc) > DESC_MAX:
        return "作品描述不能超过 1000 字"
    if not title.strip() and not desc.strip():
        return "缺少标题和描述"
    return ""


def _files_of(item: Mapping) -> list[str]:
    media = item.get("media") if isinstance(item.get("media"), Mapping) else {}
    paths = media.get("filePaths")
    files = [str(p) for p in paths if str(p or "").strip()] if isinstance(paths, list) else []
    single = str(media.get("filePath") or "").strip()
    if single and single not in files:
        files.insert(0, single)
    return files


def _log(db_path, task_id: str, message: str, level: str = "normal") -> None:
    """运行日志写进 payload.logs（页面会随任务一起读回，刷新不丢）。"""
    with _LOCK:
        rec = task_store.get_task(db_path, "publish", task_id)
        if not rec:
            return
        logs = rec.get("logs") if isinstance(rec.get("logs"), list) else []
        logs.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "level": level, "message": str(message)[:300]})
        rec["logs"] = logs[-200:]
        task_store.save_task(db_path, "publish", rec)


def _item_schedule(item: Mapping, rec: Mapping) -> int:
    """单篇/整单的定时时间（毫秒）。0 表示立刻发。"""
    for source in (item.get("schedule"), rec.get("schedule")):
        if isinstance(source, Mapping) and source.get("enabled"):
            try:
                run_at = int(source.get("runAt") or 0)
            except (TypeError, ValueError):
                run_at = 0
            if run_at > 0:
                return run_at
    return 0


def update_task(db_path, payload: Any) -> dict:
    """update-work-publish-task：前端停止/改状态时回写一条补丁。"""
    data = payload if isinstance(payload, Mapping) else {}
    task_id = str(data.get("id") or data.get("taskId") or "").strip()
    patch = data.get("patch")
    if not task_id or not isinstance(patch, Mapping):
        return {"success": False, "error": "缺少任务 id 或 patch"}
    rec = _patch_task(db_path, task_id, **dict(patch))
    if rec is None:
        return {"success": False, "error": "任务不存在"}
    return {"success": True, "id": task_id, "status": rec.get("status") or ""}


def start(db_path, payload: Any) -> dict:
    """start-work-publish-task-runtime：校验 → 落状态 → 起线程。"""
    from app.runtime import browser

    data = payload if isinstance(payload, Mapping) else {}
    rec = data.get("taskRecord") if isinstance(data.get("taskRecord"), Mapping) else data
    task_id = str((rec or {}).get("id") or "").strip()
    if not task_id:
        return {"success": False, "error": "任务缺少 id"}
    saved = task_store.get_task(db_path, "publish", task_id)
    if saved is None:
        # 前端启动时刚存过；这里再存一次作为兜底（id 相同即覆盖）
        task_store.save_task(db_path, "publish", dict(rec))
        saved = task_store.get_task(db_path, "publish", task_id)
    if not saved:
        return {"success": False, "error": "任务保存失败，无法启动"}

    items = _norm_items(saved)
    pending = [i for i in items if str(i.get("status") or "") not in CLAIMABLE_EXCLUDE]
    if not pending:
        return {"success": False, "error": "队列里没有待发布的作品（可能都已发布）"}

    problems: list[str] = []
    for item in pending:
        label = str(item.get("accountName") or item.get("accountId") or "未命名作品")
        if not str(item.get("accountId") or "").strip():
            problems.append(f"{label}：缺少发布账号")
            continue
        files = _files_of(item)
        if not files:
            problems.append(f"{label}：缺少素材文件")
            continue
        # 文件可能在选完之后被移动/删除：启动前核对一次，别等上传时才报错
        missing = [f for f in files if not os.path.exists(f)]
        if missing:
            problems.append(f"{label}：素材文件不存在（{os.path.basename(missing[0])}）")
            continue
        reason = _limits(item)
        if reason:
            problems.append(f"{label}：{reason}")
    if problems:
        return {"success": False, "error": "；".join(problems[:4])}

    if not browser.shell_available():
        return {"success": False,
                "error": "桌面壳未运行：作品发布必须使用本机已登录的账号会话，请启动桌面端后重试"}

    with _LOCK:
        if task_id in _RUNS:
            return {"success": False, "error": "该任务已在运行"}
        stop_event = threading.Event()
        thread = threading.Thread(target=_run, args=(db_path, task_id, stop_event),
                                  name=f"publish-{task_id}", daemon=True)
        _RUNS[task_id] = {"stop": stop_event, "thread": thread}

    _set_db_status(db_path, task_id, "running")
    _patch_task(db_path, task_id, status="running", startedAt=int(time.time() * 1000), error="")
    _log(db_path, task_id, f"启动发布：待发 {len(pending)} 篇")
    thread.start()
    return {"success": True, "id": task_id, "pending": len(pending)}


def stop(db_path, payload: Any) -> dict:
    """stop-work-publish-task-runtime：打断等待/当前篇，未发的回到 ready。"""
    data = payload if isinstance(payload, Mapping) else {}
    task_id = str(data.get("taskId") or data.get("id") or "").strip()
    if not task_id:
        return {"success": False, "error": "缺少任务 id"}
    with _LOCK:
        run = _RUNS.pop(task_id, None)
    if run:
        run["stop"].set()
    _set_db_status(db_path, task_id, "stopped")
    rec = task_store.get_task(db_path, "publish", task_id)
    if rec:
        items = _norm_items(rec)
        for item in items:
            if str(item.get("status") or "") in {"pending", "processing", "queued"}:
                item["status"] = "ready"
        _patch_task(db_path, task_id, items=items, status="stopped",
                    endedAt=int(time.time() * 1000),
                    stopReason=str(data.get("reason") or "用户停止"))
        _log(db_path, task_id, "已停止发布", "warn")
    return {"success": True, "id": task_id}


def running_ids() -> list[str]:
    return sorted(tid for tid, run in _RUNS.items() if run["thread"].is_alive())


def reset_for_tests() -> None:
    with _LOCK:
        for run in _RUNS.values():
            run["stop"].set()
        _RUNS.clear()


def _save_item(db_path, task_id: str, item_id: str, **changes) -> None:
    with _LOCK:
        rec = task_store.get_task(db_path, "publish", task_id)
        if not rec:
            return
        items = _norm_items(rec)
        for item in items:
            if str(item.get("id") or "") == item_id:
                item.update(changes)
        rec["items"] = items
        task_store.save_task(db_path, "publish", rec)


def _publish_mode(rec: Mapping) -> str:
    return str(rec.get("publishMode") or "interval")


def _stagger_seconds(rec: Mapping) -> tuple[float, float]:
    """依次处理模式：每条提交后的随机间隔区间（秒）。"""

    def _num(v, default):
        try:
            n = float(v)
        except (TypeError, ValueError):
            return default
        return n if n >= 0 else default

    lo = _num(rec.get("staggerMinSec"), 0.0)
    hi = max(lo, _num(rec.get("staggerMaxSec"), lo))
    return lo, hi


def _claim_next(db_path, task_id: str) -> dict | None:
    """领取下一篇：非终态、账号空闲（同账号严格串行）。

    processing 但账号不在占用集里的，是上次运行崩溃留下的残留，重新领取覆盖，
    不然那一篇会永远卡在「处理中」。占用记账与状态标记在同一个锁里完成，
    两个 worker 不会领到同一篇。
    """
    with _LOCK:
        rec = task_store.get_task(db_path, "publish", task_id)
        if not rec:
            return None
        busy = _BUSY_ACCOUNTS.setdefault(task_id, set())
        for item in _norm_items(rec):
            status = str(item.get("status") or "")
            if status in CLAIMABLE_EXCLUDE:
                continue
            account = str(item.get("accountId") or "")
            if account in busy:
                continue  # 该账号的上一篇还在发布（同账号按顺序）
            item_id = str(item.get("id") or "")
            _save_item(db_path, task_id, item_id, status="processing", error="")
            item = dict(item)
            item["status"] = "processing"
            busy.add(account)
            return item
    return None


def _release_account(task_id: str, account: str) -> None:
    with _LOCK:
        busy = _BUSY_ACCOUNTS.get(task_id)
        if busy is not None:
            busy.discard(account)


def _run(db_path, task_id: str, stop: threading.Event) -> None:
    from app.runtime import browser

    completed = failed = uncertain = 0
    rec0 = task_store.get_task(db_path, "publish", task_id) or {}
    parallel = _publish_mode(rec0) == "parallel"
    worker_count = min(MAX_PARALLEL_ACCOUNTS, max(1, len({
        str(i.get("accountId") or "") for i in _norm_items(rec0)} - {""}))) \
        if parallel else 1
    stagger = _stagger_seconds(rec0)
    _log(db_path, task_id,
         f"发布节奏：{'多账号并行（最多 2，同账号按顺序）' if parallel else '依次处理'}"
         + (f"，每条提交后随机等待 {stagger[0]:.0f}–{stagger[1]:.0f} 秒"
            if not parallel and stagger[1] > 0 else ""))
    _BUSY_ACCOUNTS.setdefault(task_id, set())

    def worker() -> None:
        nonlocal completed, failed, uncertain
        while not stop.is_set():
            try:
                item = _claim_next(db_path, task_id)
            except Exception as exc:  # noqa: BLE001 - 领取失败不能杀掉 worker 线程
                import traceback
                traceback.print_exc()
                _log(db_path, task_id, f"领取待发篇目失败：{type(exc).__name__}: {exc}", "error")
                if stop.wait(0.2):
                    return
                continue
            if item is None:
                return  # 队列里没有可领取的待发篇目了
            try:
                _process_item(db_path, task_id, item, stop, stagger if not parallel else None)
            except Exception as exc:  # noqa: BLE001 - 最后防线：不把 processing 留在库里
                import traceback
                traceback.print_exc()
                _save_item(db_path, task_id, str(item.get("id") or ""),
                           status="failed", error=f"{type(exc).__name__}: {exc}")
                _release_account(task_id, str(item.get("accountId") or ""))

    threads = [threading.Thread(target=worker, name=f"publish-{task_id}-w{n}",
                                daemon=True) for n in range(worker_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 汇总本次运行的三档计数（从 payload 读回，worker 内已逐篇落库）
    rec = task_store.get_task(db_path, "publish", task_id) or {}
    for item in _norm_items(rec):
        status = str(item.get("status") or "")
        if status == "published":
            completed += 1
        elif status == "uncertain":
            uncertain += 1
        elif status == "failed":
            failed += 1

    summary = f"发布结束：成功 {completed}，未确认 {uncertain}，失败 {failed}"
    _patch_task(db_path, task_id, status="stopped" if failed or uncertain else "done",
                endedAt=int(time.time() * 1000), summary=summary)
    _set_db_status(db_path, task_id, "stopped")
    _log(db_path, task_id, summary)
    with _LOCK:
        _BUSY_ACCOUNTS.pop(task_id, None)


def _process_item(db_path, task_id: str, item: dict, stop: threading.Event,
                  stagger: tuple[float, float] | None) -> None:
    from app.runtime import browser

    item_id = str(item.get("id") or "")
    account = str(item.get("accountId") or "")
    try:
        run_at = _item_schedule(item, task_store.get_task(db_path, "publish", task_id) or {})
        now_ms = int(time.time() * 1000)
        official_schedule_at = 0
        if run_at and run_at > now_ms:
            if (run_at - now_ms >= OFFICIAL_SCHEDULE_MIN_MS
                    and not item.get("officialScheduleFailed")):
                # 2 小时外：把定时交给平台（官方定时发布），软件不用一直挂着等。
                # 壳侧设置失败会明确回报，届时回落「软件等到点提交」。
                official_schedule_at = run_at
            else:
                wait_s = (run_at - now_ms) / 1000.0
                item_name = str(item.get("accountName") or account or item_id)
                _log(db_path, task_id,
                     f"{item_name}：由软件等到点提交"
                     f"（{time.strftime('%Y-%m-%d %H:%M', time.localtime(run_at / 1000))}）")
                if str(item.get("status") or "") != "scheduled":
                    _save_item(db_path, task_id, item_id, status="scheduled")
                if stop.wait(wait_s):
                    _save_item(db_path, task_id, item_id, status="ready", error="")
                    return
                if stop.is_set():
                    _save_item(db_path, task_id, item_id, status="ready", error="")
                    return

        files = _files_of(item)
        label = str(item.get("accountName") or account or item_id)
        _log(db_path, task_id, f"{label}：开始上传 {len(files)} 个文件")
        res = browser.execute(
            {"type": "publish-submit",
             "files": files,
             "title": str(item.get("title") or ""),
             "description": str(item.get("description") or ""),
             "mediaType": str((item.get("media") or {}).get("type") or "video"),
             "officialScheduleAt": official_schedule_at,
             **_cover_args(item)},
            db_path=db_path, timeout=1800, account_id=account, engine="shell")
        status = str(res.get("status") or "")
        if res.get("ok"):
            note = "（官方定时发布）" if official_schedule_at else ""
            changes = {"status": "published", "publishedAt": int(time.time() * 1000),
                       "error": ""}
            if official_schedule_at:
                changes["officialScheduledAt"] = official_schedule_at
            _save_item(db_path, task_id, item_id, **changes)
            _log(db_path, task_id, f"{label}：发布成功{note}")
        elif status == "uncertain":
            _save_item(db_path, task_id, item_id, status="uncertain",
                       error=str(res.get("error") or "发布结果未确认"))
            _log(db_path, task_id,
                 f"{label}：发布结果未确认（{res.get('error') or '无回执'}），请到创作者中心核对", "warn")
        elif status in {"stopped", "schedule_failed"} or stop.is_set():
            # schedule_failed：官方定时没设置成（控件找不到/平台拒绝）——
            # 篇目回到 ready 并记下「官方定时已失败」，下一轮领取后回落
            # 「软件等到点提交」，绝不提前发、也绝不无限重试官方定时。
            if status == "schedule_failed":
                _save_item(db_path, task_id, item_id, status="ready",
                           officialScheduleFailed=True, error="")
                _log(db_path, task_id,
                     f"{label}：官方定时设置未成功（{res.get('error') or '页面控件不可用'}），"
                     "改由软件等到点提交", "warn")
            else:
                _save_item(db_path, task_id, item_id, status="ready", error="")
                return
        else:
            _save_item(db_path, task_id, item_id, status="failed",
                       error=str(res.get("error") or "发布失败"))
            _log(db_path, task_id, f"{label}：发布失败（{res.get('error') or '未知原因'}）", "error")
            if str(res.get("code") or "") == "login_required":
                # 登录失效是账号级问题，该账号后面排队的同样会失败；
                # 其余账号照常并行（并行模式下不整体中断）。
                _log(db_path, task_id, f"{label}：该账号未登录创作者中心，本次跳过其后续作品", "error")
                _fail_rest_of_account(db_path, task_id, account)
        if stagger and stagger[1] > 0 and not stop.is_set():
            stop.wait(random.uniform(stagger[0], stagger[1]))
    except Exception as exc:  # noqa: BLE001 - 单篇异常落库，不拖垮整个队列
        _save_item(db_path, task_id, item_id, status="failed",
                   error=f"{type(exc).__name__}: {exc}")
        _log(db_path, task_id, f"{item_id} 发布线程异常：{type(exc).__name__}", "error")
    finally:
        _release_account(task_id, account)


def _cover_args(item: Mapping) -> dict:
    """视频自定义封面：coverEnabled + custom 模式时把封面文件交给壳。

    文件不存在就不传（壳侧不会设置封面），「自动横竖封面」和图文第 N 张
    平台侧本来就有默认行为，不需要软件干预。
    """
    if not item.get("coverEnabled") or str(item.get("coverMode") or "") != "custom":
        return {}
    media = item.get("media") if isinstance(item.get("media"), Mapping) else {}
    if str(media.get("type") or "video") != "video":
        return {}
    out: dict[str, str] = {}
    for key in ("coverLandscapePath", "coverPortraitPath"):
        path = str(item.get(key) or "").strip()
        if path and os.path.exists(path):
            out[key] = path
    if out:
        out["coverEnabled"] = True
    return out


def _fail_rest_of_account(db_path, task_id: str, account: str) -> None:
    """登录失效后，该账号其余待发篇直接落失败（原因写明），不再逐篇上传。"""
    with _LOCK:
        rec = task_store.get_task(db_path, "publish", task_id)
        if not rec:
            return
        items = _norm_items(rec)
        for item in items:
            if (str(item.get("accountId") or "") == account
                    and str(item.get("status") or "") not in TERMINAL):
                item["status"] = "failed"
                item["error"] = "该账号未登录创作者中心"
        rec["items"] = items
        task_store.save_task(db_path, "publish", rec)
