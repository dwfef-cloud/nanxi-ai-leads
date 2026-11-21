"""User-visible offline runs. Own database, fixed fixtures, no live transports."""
from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import weakref

from app.execution.offline_simulation import OfflineSimulator, SimulatedAction
from app.execution.offline_tasks import run_leadgen_offline
from app.execution.persistent_gate import PersistentWriteSafetyGate

ROOT = Path(__file__).resolve().parents[3]
DRIVER = ROOT / "desktop" / "_verify" / "offline_action_driver.js"
COUNTERS = {"post_comment": "simulatedComments", "reply_comment": "simulatedReplies",
            "dm": "simulatedMessages", "favorite": "simulatedFavorites", "copy_share_link": "simulatedLinksCopied"}
TERMINAL = {"completed", "failed", "stopped", "needs_confirmation", "interrupted"}
LABELS = {"post_comment": "发表评论", "reply_comment": "回复评论", "dm": "发送私信",
          "favorite": "收藏视频", "copy_share_link": "复制链接"}
_SERVICES = weakref.WeakValueDictionary()


def owner_alive(item):
    pid = item.get("ownerPid")
    if type(pid) is not int or pid <= 0:
        return False
    if pid == os.getpid():
        owner = _SERVICES.get(item.get("ownerToken"))
        return owner is not None and item["id"] in owner._workers
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # inaccessible owner may still be alive
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def fixture_driver(action: SimulatedAction, scenario: str) -> dict:
    node = shutil.which("node")
    if not node:
        candidate = ROOT / "desktop" / "node_modules" / "electron" / "dist" / "electron.exe"
        node = str(candidate) if candidate.is_file() else None
    if not node or not DRIVER.is_file():
        raise RuntimeError("缺少离线执行器，请检查软件文件是否完整")
    options = {}
    if scenario == "uncertain":
        options = {"emptyReceipt": True, "noFavoriteEffect": True, "badClipboard": True}
    elif scenario == "declined" and action.action in {"post_comment", "reply_comment", "dm"}:
        options = {"decline": True}
    env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
    completed = subprocess.run([node, str(DRIVER)],
        input=json.dumps({**asdict(action), "fixture": options}, ensure_ascii=False) + "\n",
        text=True, encoding="utf-8", capture_output=True, timeout=10, check=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = json.loads(completed.stdout)
    if result.get("execution", {}).get("simulated") is not True:
        raise RuntimeError("离线执行器返回了无效结果")
    return result


class OfflineRunService:
    def __init__(self, db_path: Path, *, driver=fixture_driver, pace: float = .45):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.driver, self.pace = driver, pace
        self._lock = threading.RLock()
        self._workers: dict[str, tuple[threading.Event, threading.Thread]] = {}
        self._owner_token = uuid.uuid4().hex
        _SERVICES[self._owner_token] = self
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS offline_runs (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
            con.execute("BEGIN IMMEDIATE")
            for key, raw in con.execute("SELECT id,snapshot FROM offline_runs").fetchall():
                item = json.loads(raw)
                if item["status"] not in TERMINAL and not owner_alive(item):
                    item["status"] = "interrupted"
                    item["endedAt"] = int(time.time() * 1000)
                    item["logs"].append({"label": "软件已重启", "status": "interrupted",
                                         "reason": "上次未完成的离线测试已中断，不会自动重发"})
                    con.execute("UPDATE offline_runs SET snapshot=? WHERE id=?", (json.dumps(item), key))

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        try:
            with con:
                yield con
        finally:
            con.close()

    def shutdown(self):
        with self._lock:
            workers = list(self._workers.values())
            for stop, _ in workers:
                stop.set()
        for _, worker in workers:
            worker.join(timeout=11)

    def _save(self, item):
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            previous = con.execute("SELECT snapshot FROM offline_runs WHERE id=?", (item["id"],)).fetchone()
            if (previous and json.loads(previous[0])["status"] == "stopping"
                    and item["status"] not in TERMINAL):
                item["status"] = "stopping"
            con.execute("INSERT INTO offline_runs VALUES(?,?) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot",
                        (item["id"], json.dumps(item, ensure_ascii=False)))

    def get(self, run_id):
        with self._connect() as con:
            row = con.execute("SELECT snapshot FROM offline_runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self):
        with self._connect() as con:
            return [json.loads(row[0]) for row in con.execute("SELECT snapshot FROM offline_runs ORDER BY rowid DESC LIMIT 50")]

    def start(self, *, suite="all", scenario="success", sample="A"):
        if suite not in {"all", "leadgen", "nurture"} or scenario not in {"success", "uncertain", "declined"} or sample not in {"A", "B"}:
            raise ValueError("离线测试选项无效")
        if suite == "nurture" and scenario == "declined":
            raise ValueError("评论拒绝样例请使用评论获客或完整流程")
        with self._lock:
            run_id = uuid.uuid4().hex
            item = {"id": run_id, "mode": "offline", "suite": suite, "scenario": scenario, "sample": sample,
                    "status": "running", "startedAt": int(time.time() * 1000), "endedAt": None,
                    "ownerPid": os.getpid(), "ownerToken": self._owner_token,
                    "stats": {key: 0 for key in COUNTERS.values()}, "already": 0, "failed": 0,
                    "uncertain": 0, "skipped": 0, "logs": [], "progress": 0,
                    "realStats": {"published": 0, "interacted": 0, "reached": 0}}
            stop = threading.Event()
            worker = threading.Thread(target=self._run, args=(run_id, stop), daemon=True, name="offline-" + run_id[:8])
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                active = [json.loads(row[0]) for row in con.execute("SELECT snapshot FROM offline_runs")]
                if any(row["status"] not in TERMINAL for row in active):
                    raise RuntimeError("已有离线测试运行中，请先停止或等待完成")
                self._workers[run_id] = (stop, worker)
                con.execute("INSERT INTO offline_runs VALUES(?,?)", (run_id, json.dumps(item, ensure_ascii=False)))
            worker.start()
            return item

    def stop(self, run_id):
        with self._lock:
            item = self.get(run_id)
            if item is None:
                raise KeyError(run_id)
            if item["status"] in TERMINAL:
                return item
            if run_id in self._workers:
                self._workers[run_id][0].set()
            item["status"] = "stopping"
            self._save(item)
            return item

    def _run(self, run_id, stop):
        item = self.get(run_id)
        scenario, suite, sample = item["scenario"], item["suite"], item["sample"]
        # Sample and scenario have separate ledgers. Re-running the SAME sample
        # keeps its confirmed/uncertain reservations across application restarts.
        account = "offline-fixture-" + sample + "-" + scenario
        base = 7400000000000000001 if sample == "A" else 7400000000000000101
        videos = ["https://www.douyin.com/video/" + str(base + n) for n in range(2)]
        total = {"all": 10, "leadgen": 6, "nurture": 4}[suite]
        service = self
        detail = {}

        def stopped():
            if stop.is_set() or self.get(run_id)["status"] == "stopping":
                stop.set()
                return True
            return False

        def fixture(action):
            try:
                result = service.driver(action, scenario)
            except Exception:
                detail["code"] = "offline_driver_error"
                raise
            detail.update(result.get("execution") or {})
            return result.get("receipt")

        class ObservedSimulator(OfflineSimulator):
            def run(self, action):
                if stop.wait(service.pace) or stopped():
                    return "skipped_stopped"
                detail.clear()
                outcome = super().run(action)
                with service._lock:
                    current = service.get(run_id)
                    current["stats"] = {COUNTERS[key]: value for key, value in self.counts.items()}
                    field = ("already" if outcome == "skipped_already_sent" else
                             "uncertain" if outcome in {"uncertain", "skipped_uncertain_pending_manual", "skipped_manual_review_required"} else
                             "failed" if outcome == "simulated_fail" else "skipped" if outcome.startswith("skipped_") else None)
                    if field:
                        current[field] += 1
                    current["logs"].append({"label": LABELS[action.action], "status": outcome,
                                            "reason": detail.get("code", outcome)})
                    current["progress"] = min(95, round(len(current["logs"]) / total * 100))
                    service._save(current)
                return outcome

        state = "completed"
        try:
            simulator = ObservedSimulator(fixture, gate=PersistentWriteSafetyGate(self.db_path))
            if suite in {"all", "leadgen"}:
                report = run_leadgen_offline({"taskId": run_id, "accountId": account, "videoUrls": videos},
                    simulator=simulator, search=lambda _: [], generate=lambda _: "这是离线样例评论，不会发送到平台",
                    wait=lambda _: stop.wait(self.pace) or stopped(), stopped=stopped,
                    interactions=lambda url: [
                        SimulatedAction("reply_comment", account, "fixture-comment-" + url.rsplit("/", 1)[-1], "谢谢分享", url),
                        SimulatedAction("dm", account, "fixture-recipient-" + url.rsplit("/", 1)[-1], "这是离线样例私信")])
                state = report["status"]
            if suite in {"all", "nurture"} and state == "completed" and not stopped():
                for url in videos:
                    for action_name in ("favorite", "copy_share_link"):
                        if stopped():
                            break
                        outcome = simulator.run(SimulatedAction(action_name, account, url))
                        if "uncertain" in outcome or "manual_review" in outcome:
                            state = "needs_confirmation"
                            break
                    if state != "completed" or stopped():
                        break
        except Exception:
            state = "failed"
            with self._lock:
                current = self.get(run_id)
                current["logs"].append({"label": "离线执行器", "status": "failed",
                                        "reason": "离线执行器不可用或运行异常，请检查软件文件与运行环境"})
                self._save(current)
        finally:
            with self._lock:
                current = self.get(run_id)
                current["status"] = ("stopped" if stopped() else "needs_confirmation" if current["uncertain"] else
                                     "failed" if current["failed"] else state)
                current["endedAt"] = int(time.time() * 1000)
                if current["status"] == "completed":
                    current["progress"] = 100
                self._save(current)
                self._workers.pop(run_id, None)
