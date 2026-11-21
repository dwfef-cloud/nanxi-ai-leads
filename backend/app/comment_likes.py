"""Comment selection, isolated simulation and explicitly scoped desktop acceptance.

No payload can open live writes. A local, expiring, exact-comment scope is required.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from urllib.parse import urlparse

from app import leadgen_pools
from app.api import event_bus, task_logs
from app.execution.persistent_gate import PersistentWriteSafetyGate
from app.execution.write_safety import WriteResult, WriteStatus

STATUSES = ("simulated_success", "success", "already_liked", "not_found", "failed", "skipped", "uncertain")
TERMINAL = {"completed", "stopped", "failed", "interrupted"}
_SERVICES = {}
_SERVICES_LOCK = threading.Lock()


def now():
    return int(time.time() * 1000)


def normalized(value):
    return re.sub(r"\s+", " ", re.sub(r"[\u200b\u200e\u200f\u2060\ufeff]", "",
                  unicodedata.normalize("NFKC", str(value or "")))).strip()


def video_url(value):
    try:
        parsed = urlparse(str(value or "").strip())
        if (parsed.scheme != "https" or parsed.hostname != "www.douyin.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not re.fullmatch(r"/video/[0-9]{6,}/?", parsed.path)):
            raise ValueError()
        return "https://www.douyin.com" + parsed.path.rstrip("/")
    except ValueError:
        raise ValueError("请使用完整抖音视频链接：https://www.douyin.com/video/视频编号") from None


def integer(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or int(value) != value:
        raise ValueError(f"{label}须为 {low}–{high} 的整数")
    return int(value)


def is_like_record(record):
    if not isinstance(record, dict):
        return False
    cfg = record.get("configSnapshot") or {}
    return isinstance(cfg, dict) and isinstance(cfg.get("sourceTypes"), list) and "comment_like" in cfg["sourceTypes"]


def classify_receipt(receipt, account, target):
    """An unbound response or an HTTP 200 alone cannot confirm a simulation."""
    if not isinstance(receipt, dict) or receipt.get("accountId") != account or receipt.get("target") != target:
        return "uncertain"
    if isinstance(receipt.get("status"), str) and receipt["status"] in {"already_liked", "not_found"}:
        return receipt["status"]
    if type(receipt.get("http")) is not int or type(receipt.get("code")) is not int:
        return "uncertain"
    if receipt["http"] >= 500 or receipt["http"] < 200 or 300 <= receipt["http"] < 400:
        return "uncertain"
    if receipt["http"] >= 400 or receipt["code"] != 0:
        return "failed"
    return "simulated_success" if receipt.get("before") is False and receipt.get("after") is True else "uncertain"


def fixture_receipt(account, target, scenario):
    receipt = {"accountId": account, "target": target, "http": 200, "code": 0,
               "before": False, "after": True}
    if scenario == "uncertain":
        receipt.pop("after")
    elif scenario == "declined":
        receipt["code"] = 1
    elif scenario in {"already_liked", "not_found"}:
        receipt["status"] = scenario
    return receipt


class CommentLikeService:
    def __init__(self, db_path, *, fixture=fixture_receipt):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.fixture = fixture
        self._lock = threading.RLock()
        self._workers = {}
        self._readers = {}
        interrupted = []
        with self.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS comment_like_runs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, snapshot TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS comment_like_results (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL, snapshot TEXT NOT NULL)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_comment_like_result_task ON comment_like_results(task_id,run_id,id)")
            con.execute("CREATE TABLE IF NOT EXISTS comment_like_samples (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
            # Restart recovery applies only to owners that have actually exited.
            for run_id, raw in con.execute("SELECT id,snapshot FROM comment_like_runs").fetchall():
                item = json.loads(raw)
                if item["status"] in TERMINAL:
                    continue
                try:
                    owner = int(item.get("ownerPid") or 0)
                    if owner <= 0:
                        raise ValueError("missing owner")
                    os.kill(owner, 0)
                except (OSError, ValueError):
                    item.update(status="interrupted", endedAt=now())
                    con.execute("UPDATE comment_like_runs SET snapshot=? WHERE id=?", (json.dumps(item), run_id))
                    interrupted.append(item)
        for item in interrupted:
            self._persist(item)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(str(self.db_path), timeout=15)
        try:
            with con:
                yield con
        finally:
            con.close()

    def accounts(self):
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            try:
                return {str(r["id"]): dict(r) for r in con.execute("SELECT * FROM accounts")}
            except sqlite3.OperationalError:
                return {}

    def candidates(self, args):
        account = str(args.get("accountId") or "")
        kinds = args.get("kinds", ["main", "reply", "profile"])
        selected = args.get("selectedIds")
        minimum = integer(args.get("minTimestamp") or 0, "时间起点", 0, 1000000000000000)
        cursor = integer(args.get("cursor") or 0, "分页起点", 0, 1000000)
        if not isinstance(kinds, list) or any(k not in {"main", "reply", "profile"} for k in kinds):
            raise ValueError("评论类型格式不正确")
        if selected is not None and (not isinstance(selected, list) or len(selected) > 100):
            raise ValueError("每账号最多选择100条评论")
        items = []
        names = self.accounts()
        if account not in names:
            return {"success": False, "error": "请选择有效的评论所属账号"}
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            # 回复评论（reply-comment）与主评一样有确认发表成功的回执记录，
            # 同样可以作为样本候选；kind 由动作决定，选择键前缀区分。
            try:
                rows = con.execute("SELECT * FROM live_action_results WHERE account_id=? "
                                   "AND action IN ('post-comment','reply-comment') AND status='sent_ok' "
                                   "ORDER BY created_ms DESC,id DESC", (account,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
        for row in rows:
            # The shared picker accepts kind:sha256 keys and filters by kind.
            # A local:<row number> key was silently discarded on every toggle.
            kind = "reply" if row["action"] == "reply-comment" else "main"
            if kind not in kinds:
                continue
            binding = [account, str(row["id"]), row["url"], row["text"], row["created_ms"]]
            key = kind + ":" + hashlib.sha256(json.dumps(binding, ensure_ascii=False,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            legacy_key = f"local:{row['id']}" if kind == "main" else ""
            stamp = row["created_ms"] or 0
            if selected is not None:
                if key not in selected:
                    if not legacy_key or legacy_key not in selected:
                        continue
                    # Existing saved tasks still resolve their old explicit key.
                    key = legacy_key
            elif stamp < minimum:
                continue
            try:
                url = video_url(row["url"])
            except ValueError:
                continue
            items.append({"id": key, "videoUrl": url, "content": row["text"], "kind": kind,
                          "commentedAt": stamp, "authorName": names[account].get("nickname") or names[account].get("name") or account,
                          "ownerId": account})
        return {"success": True, "items": items[cursor:cursor+50], "hasMore": cursor+50 < len(items),
                "nextCursor": cursor+50, "defaultSelectedIds": [],
                "note": "只列出本地确认发表成功的主评与楼中楼回复；首作评论的发表记录同为 post-comment，随主评一并列出。"}

    def normalize_target(self, source):
        if not isinstance(source, dict):
            raise ValueError("评论目标格式不正确")
        if any(not isinstance(source.get(field), str) for field in ("authorName", "content")):
            raise ValueError("评论作者和正文须为文本")
        if source.get("kind", "manual") not in ("main", "reply", "profile", "manual"):
            raise ValueError("不支持的评论类型")
        target = {"videoUrl": video_url(source.get("videoUrl")),
                  "authorName": normalized(source.get("authorName")).lstrip("@").strip(),
                  "content": normalized(source.get("content")), "kind": source.get("kind", "manual"),
                  "ownerId": str(source.get("ownerId") or ""), "id": str(source.get("id") or uuid.uuid4().hex)}
        if not target["authorName"] or not target["content"] or len(target["content"]) > 2000:
            raise ValueError("每条评论须填写作者昵称与完整正文（最多2000字）")
        # Opaque cache keys preserve the selected comment without returning any
        # platform identifiers to the renderer or trusting edited identities.
        sample_key = str(source.get("sampleKey") or "")
        if sample_key:
            with self.connect() as con:
                row = con.execute("SELECT snapshot FROM comment_like_samples WHERE id=?", (sample_key,)).fetchone()
            if not row:
                raise ValueError("读取的评论记录已失效，请重新读取")
            cached = json.loads(row[0])
            if any(target[k] != cached[k] for k in ("videoUrl", "authorName", "content")):
                raise ValueError("所选评论的作者、正文或视频已修改，请重新选择")
            target.update(sampleKey=sample_key, kind=cached["kind"])
        logical = [target["videoUrl"], target["authorName"].casefold(), target["content"]]
        target["targetKey"] = hashlib.sha256(json.dumps(logical, ensure_ascii=False).encode()).hexdigest()
        return target

    def validate(self, record):
        cfg = record.get("configSnapshot") or {}
        if not is_like_record(record):
            raise ValueError("这不是评论点赞任务")
        accounts = cfg.get("selectedAccounts")
        if not isinstance(accounts, list) or not accounts or len(accounts) > 20:
            raise ValueError("请选择1–20个执行账号")
        accounts = list(dict.fromkeys(map(str, accounts)))
        known = self.accounts()
        if any(a not in known or known[a].get("platform", "douyin") != "douyin" for a in accounts):
            raise ValueError("执行账号已失效，请重新选择抖音账号")
        limit = integer(cfg.get("commentLikeLimit", 0), "每账号上限", 0, 100)
        low = integer(cfg.get("commentLikeIntervalMin", 5), "最小间隔", 1, 3600)
        high = integer(cfg.get("commentLikeIntervalMax", 10), "最大间隔", low, 3600)
        source = cfg.get("commentLikeSource", "local")
        if source == "manual":
            raw = cfg.get("commentLikeManualTargets")
            if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
                raise ValueError("请添加1–100条指定评论")
        elif source == "local":
            raw = []
            selected = cfg.get("visibilityLocalSampleIds") or {}
            if not isinstance(selected, dict):
                raise ValueError("评论选择格式不正确")
            for owner, ids in selected.items():
                if not isinstance(ids, list) or len(ids) > 100:
                    raise ValueError("每账号最多选择100条已有评论")
                if not ids:
                    continue
                found = self.candidates({"accountId": owner, "selectedIds": ids, "kinds": cfg.get("visibilityLocalSampleKinds", ["main"])})
                if not found.get("success"):
                    raise ValueError(found.get("error"))
                # Explicit selections are resolved server-side, never from caller rows.
                candidates = list(found["items"])
                if found["hasMore"]:
                    candidates += self.candidates({"accountId": owner, "selectedIds": ids, "cursor": 50, "kinds": cfg.get("visibilityLocalSampleKinds", ["main"])})["items"]
                if set(map(str, ids)) != {item["id"] for item in candidates}:
                    raise ValueError("部分已选评论不存在或类型已变更，请重新选择")
                raw.extend(candidates)
        else:
            raise ValueError("不支持的评论来源")
        if not raw or len(raw) > 100:
            raise ValueError("每个任务请选择1–100条评论")
        targets = {}
        for item in raw:
            target = self.normalize_target(item)
            targets.setdefault(target["targetKey"], target)
        return {"accounts": accounts, "targets": list(targets.values()), "limit": limit, "low": low, "high": high}

    def _save(self, item):
        with self.connect() as con:
            con.execute("UPDATE comment_like_runs SET snapshot=? WHERE id=?", (json.dumps(item, ensure_ascii=False), item["id"]))

    def _persist(self, item):
        previous = leadgen_pools.load_records(self.db_path).get(item["taskId"], {})
        real_stats = dict(previous.get("stats") or {})
        mode = item.get("mode", "offline")
        changes = {
            "status": item["status"], "startedAt": item["startedAt"], "endedAt": item.get("endedAt"),
            "executionMode": mode, "endReason": item["status"],
            "lastMessage": "离线模拟，未执行真实点赞" if mode == "offline" else "受限真机验收，以评论点赞回执及状态为准"}
        stats_key, runs_key = ("simulationStats", "simulationRuns") if mode == "offline" else ("stats", "runs")
        changes[stats_key] = item["stats"] if mode == "offline" else {**real_stats, **item["stats"]}
        changes[runs_key] = [{"accountId": a, "name": a, "startedAt": item["startedAt"],
                              "endedAt": item.get("endedAt"), "mode": mode} for a in item["accounts"]]
        leadgen_pools.update_task_record(self.db_path, item["taskId"], changes)

    def _event(self, item, kind, **extra):
        event_bus.publish("comment-like-task-event", {"taskId": item["taskId"], "runId": item["id"],
            "accountIds": item.get("accounts", []), "status": item.get("status"),
            "stats": item.get("stats", {}), "type": kind, "mode": item.get("mode", "offline"), **extra})

    def _log(self, item, message):
        prefix = "[受限真机] " if item.get("mode") == "live" else "[离线模拟] "
        stamp = now()
        task_logs.append(self.db_path, item["id"], item["taskId"], "", prefix + message, ts=stamp)
        self._event(item, "log", message=prefix + message, ts=stamp)

    def live_status(self):
        from app.execution.live_scope import load_scope, scope_path
        scope = load_scope("like-comment")
        available = bool(scope and scope.get("actions", {}).get("like-comment") == 1
                         and scope.get("commentTargets") and scope.get("nickname"))
        claimed = bool(available and (Path(scope_path("like-comment")).parent / "attempts" /
            (hashlib.sha256((scope["scopeId"] + ":like-comment").encode()).hexdigest() + ".claim")).exists())
        available = available and not claimed
        return {"available": available, "taskId": scope.get("leadgenTaskId", "") if available else "",
                "expiresAt": int(scope.get("expiresAt", 0) * 1000) if available else 0,
                "note": "本次真实点赞的一次尝试已使用，禁止重复提交" if claimed else
                        "仅限本地已授权的一账号、一条评论、一次尝试" if available else "真实点赞未授权；默认只可离线模拟"}

    def _live_plan(self, record, plan):
        from app.execution.live_scope import signed_command, load_scope
        from app.runtime import browser, registry
        if not self.live_status()["available"]:
            raise ValueError("真实点赞未授权；需先完成指定账号、评论及一次尝试的本地验收授权")
        if len(plan["accounts"]) != 1 or len(plan["targets"]) != 1:
            raise ValueError("受限真机验收每次仅允许一个账号和一条评论")
        account, target = plan["accounts"][0], plan["targets"][0]
        key = target.get("sampleKey")
        with self.connect() as con:
            row = con.execute("SELECT snapshot FROM comment_like_samples WHERE id=?", (key or "",)).fetchone()
        cached = json.loads(row[0]) if row else {}
        if not key and record.get("configSnapshot", {}).get("commentLikeSource", "local") == "local":
            # A confirmed local post still needs a fresh, exact platform identity.
            # Resolve only from the read-only reader cache, never from row numbers.
            with self.connect() as con:
                samples = [(sample_key, json.loads(raw)) for sample_key, raw in
                           con.execute("SELECT id,snapshot FROM comment_like_samples")]
            matches = [(sample_key, sample) for sample_key, sample in samples
                       if all(sample.get(field) == target[field] for field in ("videoUrl", "authorName", "content"))
                       and re.fullmatch(r"[0-9]{6,}", str(sample.get("platform_ref") or ""))]
            identities = {str(sample["platform_ref"]) for _, sample in matches}
            if len(identities) > 1:
                raise ValueError("同作者同正文存在多条平台评论，请重新读取并明确选择其中一条")
            if matches:
                key, cached = matches[0]
                target["sampleKey"] = key
        ref = str(cached.get("platform_ref") or "")
        if not re.fullmatch(r"[0-9]{6,}", ref):
            raise ValueError("请先读取视频评论并选择一条可核对的评论（主评或楼中楼回复），再做真机测试")
        scope = load_scope("like-comment")
        name = self.accounts()[account].get("nickname") or self.accounts()[account].get("name")
        if not name or scope.get("nickname") != name:
            raise ValueError("授权账号昵称与当前账号记录不一致")
        signed_command("like-comment", account_id=account, task_id=str(record["id"]),
                       video_url=target["videoUrl"], comment_id=ref, text=target["content"],
                       comment_author=target["authorName"])
        if not browser.shell_available():
            raise ValueError("请先打开桌面客户端并登录指定账号")
        if account in registry.active_accounts() or any(v[2] == account for v in self._readers.values()):
            raise ValueError("该账号有任务或评论读取正在运行，请先停止")
        target["liveRef"] = ref  # Internal only; stripped from public results.
        plan["limit"] = 1
        return plan

    def _live_attempt(self, item, account, target, stop):
        from app.execution.live_leadgen import perform
        result = perform(self.db_path, "like-comment", account_id=account, task_id=item["taskId"],
                         video_url=target["videoUrl"], comment_id=target["liveRef"],
                         comment_author=target["authorName"], text=target["content"],
                         stopped=stop.is_set, run_id=item["id"])
        if result.get("ok") is True and result.get("status") == "sent_ok" and result.get("code") == "comment_like_receipt_state_and_reload_verified":
            return "success", "平台点赞回执、目标评论状态与刷新后留存均已核对"
        code = str(result.get("code") or "comment_like_not_confirmed")
        if code in {"live_writes_disabled", "desktop_required"}:
            return "failed", "验收授权已关闭或桌面会话不可用，本次未执行"
        if code in {"already_liked", "already_sent"}:
            return "already_liked", "目标评论已赞或已确认执行，跳过且不取消"
        if code == "comment_not_found":
            return "not_found", "当前视频页未找到与所选作者、正文和定位信息一致的评论"
        if result.get("status") in {"skipped", "blocked", "sent_fail"}:
            return "failed", {"live_writes_disabled": "验收授权已关闭或目标不匹配", "acceptance_attempt_consumed": "本次一次尝试已使用，请勿重复提交",
                              "like_state_unknown": "当前页面未提供可核对的点赞状态，未点击",
                              "comment_like_declined": "平台明确拒绝本次评论点赞",
                              "live_preview_not_ready": "实况画面未准备好，本次没有点击点赞"}.get(code, "未执行或未确认点赞：" + code)
        return "uncertain", ("平台返回成功但刷新后未保留点赞，已停止且禁止自动重试" if code == "comment_like_not_retained_after_refresh"
                             else "点赞结果不确定，已停止该账号且禁止自动重试")

    def active(self):
        with self._lock:
            return [key for key, (_, thread) in self._workers.items() if thread.is_alive()]

    def runtime(self):
        tasks = self.active()
        with self.connect() as con:
            snapshots = [json.loads(row[0]) for row in con.execute("SELECT snapshot FROM comment_like_runs")]
        modes = {item.get("mode", "offline") for item in snapshots
                 if item["taskId"] in tasks and item["status"] not in TERMINAL}
        summaries = {}
        for item in sorted(snapshots, key=lambda r: r["startedAt"]):
            summaries.setdefault(item["taskId"], {})[item.get("mode", "offline")] = {
                key: item.get(key) for key in ("status", "startedAt", "endedAt", "stats")}
        return {"success": True, "taskIds": tasks, "taskModes": summaries, "live": self.live_status(),
                "mode": next(iter(modes)) if len(modes) == 1 else "mixed" if modes else "offline"}

    def dashboard(self):
        """Live runs only, including short completed runs missed by polling."""
        active = set(self.active())
        names = self.accounts()
        with self.connect() as con:
            snapshots = [json.loads(row[0]) for row in con.execute("SELECT snapshot FROM comment_like_runs")]
        records = leadgen_pools.load_records(self.db_path)
        out = []
        live = sorted((item for item in snapshots if item.get("mode") == "live"),
                      key=lambda r: r["startedAt"], reverse=True)
        selected = ([item for item in live if item["status"] not in TERMINAL] +
                    [item for item in live if item["status"] in TERMINAL][:100])
        for item in selected:
            finished = item["status"] in TERMINAL
            if not finished and item["taskId"] not in active:
                continue
            if finished and now() - (item.get("endedAt") or 0) > 30 * 60 * 1000:
                continue
            record = records.get(item["taskId"], {})
            results = self.results({"taskId": item["taskId"], "runId": item["id"], "limit": 100})["items"]
            target = item.get("target") or (results[0].get("sample") if results else {}) or {}
            logs = task_logs.list_for(self.db_path, item["id"], limit=80)
            reason = results[0].get("reason") if results else item.get("phase")
            for account in item["accounts"]:
                with self._lock:
                    handle = self._workers.get(item["taskId"])
                    stopping = bool(handle and handle[0].is_set() and not finished)
                out.append({"taskKind": "comment_like", "taskId": item["taskId"], "runId": item["id"],
                    "accountId": account, "name": record.get("name", "评论点赞"),
                    "nickname": names.get(account, {}).get("nickname") or account,
                    "platform": "douyin", "mode": "live", "trigger": "manual",
                    "status": "stopping" if stopping else item["status"], "startedAt": item["startedAt"], "endedAt": item.get("endedAt"),
                    "isSessionFinished": finished, "phase": "正在停止，等待执行器确认" if stopping else reason or "正在定位评论并核对实况",
                    "viewKey": "entity_" + account, "stats": item["stats"], "target": target,
                    "logs": logs, "canStop": not finished})
        return out

    def start(self, record, *, mode="", scenario="success"):
        if mode not in {"offline", "live"}:
            return {"success": False, "code": "live_writes_disabled", "error": "真实评论点赞保持关闭，请使用离线模拟；不会转为线索采集"}
        if scenario not in {"success", "uncertain", "declined", "already_liked", "not_found"}:
            return {"success": False, "error": "无效的离线样例"}
        try:
            plan = self.validate(record)
            if mode == "live":
                with self._lock:
                    plan = self._live_plan(record, plan)
        except (ValueError, TypeError, AttributeError) as exc:
            return {"success": False, "error": str(exc), **({"code": "live_writes_disabled"} if mode == "live" else {})}
        task = str(record.get("id") or "")
        if not task:
            return {"success": False, "error": "缺少任务编号"}
        with self._lock:
            with self.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                for raw, in con.execute("SELECT snapshot FROM comment_like_runs"):
                    previous = json.loads(raw)
                    if previous["status"] not in TERMINAL and (previous["taskId"] == task or set(plan["accounts"]) & set(previous["accounts"])):
                        return {"success": False, "error": "该账号或任务已有评论点赞任务运行，请先停止"}
                item = {"id": uuid.uuid4().hex, "taskId": task, "ownerPid": os.getpid(), "mode": mode,
                        "status": "running", "startedAt": now(), "accounts": plan["accounts"],
                        "target": {k: v for k, v in plan["targets"][0].items() if k not in {"targetKey", "liveRef"}},
                        "phase": "正在打开视频、定位评论并准备实况",
                        "stats": {"commentLikeRunId": "", "commentLikePlanned": len(plan["accounts"])*len(plan["targets"]),
                                  "commentLikeProcessed": 0, "commentLikeSuccess": 0, "commentLikeAlready": 0,
                                  "commentLikeNotFound": 0, "commentLikeFailed": 0, "commentLikeUncertain": 0, "commentLikeSkipped": 0}}
                item["stats"]["commentLikeRunId"] = item["id"]
                if mode == "live":
                    from app.runtime import registry
                    if not registry.claim_account(plan["accounts"][0], item["id"]):
                        return {"success": False, "busy": True, "error": "该账号正在执行其他任务，请先停止"}
                try:
                    con.execute("INSERT INTO comment_like_runs VALUES(?,?,?)", (item["id"], task, json.dumps(item)))
                except Exception:
                    if mode == "live":
                        registry.release_account(plan["accounts"][0], item["id"])
                    raise
            try:
                self._persist(item)
                stop = threading.Event()
                thread = threading.Thread(target=self._run, args=(item, plan, scenario, stop), daemon=True)
                self._workers[task] = (stop, thread)
                self._event(item, "started")
                thread.start()
            except Exception:
                self._workers.pop(task, None)
                if mode == "live":
                    registry.release_account(plan["accounts"][0], item["id"])
                item.update(status="failed", endedAt=now())
                self._save(item)
                raise
        return {"success": True, "mode": mode, "runId": item["id"]}

    def _run(self, item, plan, scenario, stop):
        counts = {"simulated_success": "commentLikeSuccess", "success": "commentLikeSuccess", "already_liked": "commentLikeAlready", "not_found": "commentLikeNotFound",
                  "failed": "commentLikeFailed", "uncertain": "commentLikeUncertain", "skipped": "commentLikeSkipped"}
        reasons = {"simulated_success": "模拟回执核对通过，未执行平台点赞", "already_liked": "模拟已赞或此前模拟成功，跳过且不取消",
                   "not_found": "模拟未找到目标评论", "failed": "模拟平台明确拒绝", "uncertain": "模拟结果不确定，停止该账号且禁止自动重试", "skipped": "达到本账号模拟新增上限"}
        try:
            self._log(item, f"开始：{len(plan['accounts'])}个账号 × {len(plan['targets'])}条评论")
            mode = item.get("mode", "offline")
            gate = PersistentWriteSafetyGate(self.db_path, namespace=mode)
            names = self.accounts()
            for account in plan["accounts"]:
                successes = 0
                for index, target in enumerate(plan["targets"]):
                    if stop.is_set():
                        break
                    status = "skipped"
                    reason_text = "达到本账号新增上限" if mode == "live" else reasons["skipped"]
                    if not plan["limit"] or successes < plan["limit"]:
                        key = json.dumps(["comment-like-live" if mode == "live" else "comment-like-simulation", account, target["targetKey"]])
                        allowed, reason = gate.reserve(key)
                        if not allowed:
                            status = "already_liked" if reason == "already_sent" else "uncertain"
                            reason_text = ("已确认执行，跳过且不取消" if status == "already_liked" else "结果不确定，禁止自动重试") if mode == "live" else reasons[status]
                        else:
                            if mode == "live":
                                try:
                                    status, reason_text = self._live_attempt(item, account, target, stop)
                                except Exception:
                                    status, reason_text = "uncertain", "执行连接中断，结果不确定，禁止自动重试"
                            else:
                                try:
                                    receipt = self.fixture(account, target["targetKey"], scenario)
                                except Exception:
                                    receipt = None
                                status = classify_receipt(receipt, account, target["targetKey"])
                                reason_text = reasons[status]
                            state = WriteStatus.SENT_OK if status in {"success", "simulated_success", "already_liked"} else WriteStatus.UNCERTAIN if status == "uncertain" else WriteStatus.SENT_FAIL
                            gate.record(key, WriteResult(state, state == WriteStatus.SENT_FAIL, state == WriteStatus.UNCERTAIN, status))
                    result = {"runId": item["id"], "taskId": item["taskId"], "mode": mode, "accountId": account,
                              "accountName": names[account].get("nickname") or names[account].get("name") or account,
                              "ownerName": target["authorName"], "sample": {k: v for k, v in target.items() if k not in {"targetKey", "liveRef"}},
                              "status": status, "reason": reason_text, "updatedAt": now()}
                    with self.connect() as con:
                        con.execute("INSERT INTO comment_like_results(run_id,task_id,snapshot) VALUES(?,?,?)", (item["id"], item["taskId"], json.dumps(result, ensure_ascii=False)))
                    item["stats"]["commentLikeProcessed"] += 1
                    item["stats"][counts[status]] += 1
                    successes += status in {"success", "simulated_success"}
                    self._save(item)
                    self._persist(item)
                    self._log(item, f"{result['accountName']}：{reason_text}")
                    self._event(item, "stats")
                    if status == "uncertain":
                        break
                    if index+1 < len(plan["targets"]) and status != "skipped" and stop.wait(random.randint(plan["low"], plan["high"])):
                        break
            item["status"] = "stopped" if stop.is_set() else "failed" if item["stats"]["commentLikeUncertain"] or item["stats"]["commentLikeFailed"] else "completed"
        except Exception:
            item["status"] = "failed"
            self._log(item, "执行异常，已停止；请检查本地记录和软件文件")
        finally:
            item["endedAt"] = now()
            try:
                self._save(item)
                self._persist(item)
                self._log(item, "已停止" if stop.is_set() else "受限真机验收结束" if item.get("mode") == "live" else "模拟结束；真实点赞0次")
            finally:
                if item.get("mode") == "live":
                    from app.runtime import registry
                    registry.release_account(plan["accounts"][0], item["id"])
                with self._lock:
                    self._workers.pop(item["taskId"], None)
                self._event(item, "finished")

    def stop(self, task, run_id=""):
        with self._lock:
            handle = self._workers.get(task)
            if run_id:
                with self.connect() as con:
                    row = con.execute("SELECT task_id,snapshot FROM comment_like_runs WHERE id=?", (run_id,)).fetchone()
                if not handle or not row or row[0] != task or json.loads(row[1])["status"] in TERMINAL:
                    return {"success": False, "error": "本次运行已结束，未停止其他运行"}
            if handle:
                handle[0].set()
        return {"success": True, "stopped": int(handle is not None)}

    def results(self, args):
        task = str(args.get("taskId") or "")
        offset = integer(args.get("offset", 0), "分页起点", 0, 1000000)
        limit = integer(args.get("limit", 20), "每页条数", 1, 100)
        with self.connect() as con:
            rows = con.execute("SELECT snapshot FROM comment_like_results WHERE task_id=? ORDER BY id DESC", (task,)).fetchall()
        items = [json.loads(row[0]) for row in rows]
        run, account = args.get("runId"), args.get("accountId")
        items = [i for i in items if (not run or i["runId"] == run) and (not account or i["accountId"] == account)]
        counts = {status: sum(i["status"] == status for i in items) for status in STATUSES}
        selected = [i for i in items if args.get("status") in (None, "", "all", i["status"])]
        modes = {i.get("mode", "offline") for i in items}
        return {"success": True, "mode": next(iter(modes)) if len(modes) == 1 else "mixed" if modes else "offline",
                "items": selected[offset:offset+limit], "total": len(selected), "counts": counts,
                "note": "每条记录标明模拟或真机；模拟结果不增加真实统计"}

    def reader_start(self, args):
        from app.runtime import browser, registry
        try:
            session = str(args.get("sessionId") or "")
            account = str(args.get("accountId") or "")
            if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", session):
                raise ValueError("读取会话编号无效")
            known = self.accounts()
            if account not in known or known[account].get("platform", "douyin") != "douyin":
                raise ValueError("请选择有效的读取账号")
            url = video_url(args.get("videoUrl"))
            limit = integer(args.get("limit", 40), "当前页读取上限", 1, 60)
            # 楼中楼开关：2026-11-18 壳侧把评论抽取补上了 depth / parentIdx，
            # 这里才有办法把「主评 / 回复」分开标。以前直接拒掉，是因为解析把
            # 父子拍平成一层，样本分不清 kind，点赞时就可能点错行。
            include_replies = args.get("includeReplies") is True
            if not browser.shell_available():
                raise ValueError("请先启动桌面客户端并登录读取账号")
            if account in registry.active_accounts():
                raise ValueError("该账号正在执行其他任务，请先停止后读取")
        except (ValueError, TypeError) as exc:
            return {"success": False, "error": str(exc)}
        with self._lock:
            if any(v[2] == account or k == session for k, v in self._readers.items()):
                return {"success": False, "error": "该账号已有评论读取，请先停止"}
            if not registry.claim_account(account, "comment-reader:" + session):
                return {"success": False, "busy": True, "error": "该账号正在执行其他任务，请先停止"}
            stop = threading.Event()
            thread = threading.Thread(target=self._read,
                                      args=(session, account, url, limit, stop, include_replies),
                                      daemon=True)
            self._readers[session] = (stop, thread, account)
            try:
                thread.start()
            except Exception:
                self._readers.pop(session, None)
                registry.release_account(account, "comment-reader:" + session)
                raise
        return {"success": True, "sessionId": session, "mode": "read_only"}

    def _read(self, session, account, url, limit, stop, include_replies=False):
        from app.comment_tree import annotate, is_reply
        from app.runtime import browser
        rows, message = [], ""
        command = {"type": "read-comments", "videoUrls": [url], "limit": limit}
        if include_replies:
            # 壳侧按这个开关点开「展开 N 条回复」，并把 depth / parentIdx 带回来。
            command["expandSubComments"] = True
        try:
            result = browser.execute(command, db_path=self.db_path, account_id=account,
                                     engine="shell", stop_check=stop.is_set)
            if stop.is_set():
                message = "已停止读取，已读取的评论保留"
            elif not result.get("ok"):
                message = result.get("error") or "评论读取失败"
            else:
                seen = set()
                comments = annotate(result.get("comments") or [])
                for source in comments[:limit]:
                    if stop.is_set():
                        break
                    reply = is_reply(source)
                    if reply and not include_replies:
                        # 没点「包含楼中楼」却拿到回复（例如平台默认展开了前排回复）：
                        # 旧行为是拿 parentCommentId 过滤，但壳侧从来没给过这个字段，
                        # 等于没过滤。现在按 depth 判，才真的挡得住。
                        continue
                    try:
                        target = self.normalize_target({"videoUrl": source.get("videoUrl") or url,
                            "authorName": source.get("nickname"), "content": source.get("text"),
                            "kind": "reply" if reply else "main"})
                    except ValueError:
                        continue
                    if target["videoUrl"] != url or target["targetKey"] in seen:
                        continue
                    seen.add(target["targetKey"])
                    key = hashlib.sha256((target["targetKey"] + str(source.get("commentId") or "")).encode()).hexdigest()[:32]
                    target.update(id=key, platform_ref=str(source.get("commentId") or ""),
                                  parentCommentId=str(source.get("parentCommentId") or ""))
                    with self.connect() as con:
                        con.execute("INSERT OR REPLACE INTO comment_like_samples VALUES(?,?)", (key, json.dumps(target, ensure_ascii=False)))
                    rows.append({"key": key, "authorName": target["authorName"], "content": target["content"],
                                 "videoUrl": url, "kind": "reply" if reply else "main",
                                 "time": str(source.get("publishedText") or "时间未知"),
                                 "commentedAt": source.get("commentedAt")})
                mains = sum(1 for row in rows if row["kind"] == "main")
                message = (f"已读取当前页{len(rows)}条（主评 {mains} 条 / 楼中楼回复 {len(rows) - mains} 条）"
                           f"（最多60条，不代表完整评论区）；未点赞")
                if include_replies and len(rows) == mains:
                    message += "。本页没有读到楼中楼回复：可能确实没有回复，也可能回复被折叠且展开按钮没点到"
        except Exception:
            message = "读取失败，请检查桌面登录状态与视频评论区"
        finally:
            from app.runtime import registry
            registry.release_account(account, "comment-reader:" + session)
            with self._lock:
                self._readers.pop(session, None)
            event_bus.publish("comment-like-read-progress", {"sessionId": session, "rows": rows, "message": message, "done": True})

    def reader_stop(self, args):
        with self._lock:
            handle = self._readers.get(str(args.get("sessionId") or ""))
            if handle:
                handle[0].set()
        return {"success": True, "stopped": int(handle is not None)}

    def shutdown(self):
        with self._lock:
            workers = list(self._workers.values()) + [v[:2] for v in self._readers.values()]
            for stop, _ in workers:
                stop.set()
        for _, thread in workers:
            thread.join(timeout=5)


def service(db_path):
    key = str(Path(db_path).resolve())
    with _SERVICES_LOCK:
        if key not in _SERVICES:
            _SERVICES[key] = CommentLikeService(db_path)
        return _SERVICES[key]


def shutdown_all():
    with _SERVICES_LOCK:
        services = list(_SERVICES.values())
    for svc in services:
        svc.shutdown()
