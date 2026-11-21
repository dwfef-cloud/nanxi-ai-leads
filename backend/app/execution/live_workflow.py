"""Desktop review -> exact actions. Preview reads/generates; it never sends."""
from __future__ import annotations

import hashlib
import json
import re
import random
import secrets
import sqlite3
import threading
import time

from app import leadgen_pools
from app.runtime import browser, registry
from app.execution.persistent_gate import PersistentWriteSafetyGate
from app.execution import leadgen_config as settings
from app.execution.leadgen_media import EXTRA_FIELDS, verify

_PREPARING = {}
_LOCK = threading.Lock()
# favorite-video 没有独立的页面开关：由「养护 / 主页首作」计划派生，
# 与 like-video 同属对作品的真实写动作，因此并入执行清单合法类型。
ACTIONS = set(settings.ACTION_FLAGS.values()) | {"favorite-video"}


def keywords_of(value):
    return settings.words(value)


def valid_video(value):
    return bool(isinstance(value, str) and re.fullmatch(r"https://www\.douyin\.com/video/\d{6,}", value))


def action_key(account, action):
    user_action = action["type"] in {"send-dm", "follow-user", "share-profile"}
    return json.dumps([account, action["type"], "" if user_action else action["videoUrl"],
                       action.get("recipientId", "") if user_action else action.get("commentId", "")],
                      separators=(",", ":"))


def binding_of(proposal):
    return hashlib.sha256(json.dumps(proposal, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def cancel(task_id="", account_id=""):
    with _LOCK:
        hits = [stop for _, (payload, stop) in _PREPARING.items()
                if (task_id and task_id in {payload.get("taskId"), payload.get("leadgenTaskId")})
                or (account_id and account_id == payload.get("accountId"))]
        for stop in hits:
            stop.set()
    return len(hits)


def preparing_records():
    with _LOCK:
        return [{"kind": "leadgen-preview", "taskId": p["taskId"], "accountId": p["accountId"]}
                for p, _ in _PREPARING.values()]


def _account(db_path, account_id):
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id,nickname,platform FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row or row["platform"] != "douyin" or not row["nickname"]:
        raise ValueError("请选择一个已登录的抖音账号")
    return dict(row)


def checked_text(text):
    if not isinstance(text, str) or text != text.strip() or not 5 <= len(text) <= 200:
        raise ValueError("发送正文需要 5 到 200 个字符，请检查主评、回复或私信文案")
    return text


# 出口裁剪：提案里必须留着 commentId 才能定点回复，但页面（以及 IPC 出口的
# assert_public_safe）不允许出现平台 uid 级字段。2026-11-16 真机在回复模式下
# 就撞了这个：actions 里带 commentId → 整条预览被合规校验判失败。
# 页面只需要看「对象 + 原文」，目标由 proposalId/binding 在服务端还原。
_EGRESS_UID_KEYS = frozenset({"commentId", "commentUserId", "userId", "videoId",
                              "uid", "secUid", "sec_uid", "authorId", "openid"})


def public_actions(actions):
    out = []
    for index, action in enumerate(actions or [], 1):
        item = {k: v for k, v in action.items() if k not in _EGRESS_UID_KEYS}
        item["ref"] = index  # 页面回指用，不含平台标识
        out.append(item)
    return out


def preview(db_path, payload):
    from app.api import task_runner as runner
    from app.execution.leadgen_plan import build
    payload = json.loads(json.dumps(payload))
    account_id = str(payload.get("accountId") or "")
    task_id = str(payload.get("leadgenTaskId") or payload.get("taskId") or "")
    if not account_id or not task_id or not payload.get("taskId"):
        raise ValueError("缺少任务或执行账号")
    report = settings.inspect(payload, account_id=account_id)
    if not report["success"]:
        raise ValueError("；".join(report["errors"]))
    account = _account(db_path, account_id)
    if browser.status(db_path).get("engine") != "shell":
        raise ValueError("请从桌面客户端启动并登录账号")
    owner = "leadgen-preview:" + secrets.token_hex(12)
    if not registry.claim_account(account_id, owner):
        raise ValueError("当前账号正在执行其他任务，请先停止")
    stop = threading.Event()
    with _LOCK:
        _PREPARING[owner] = (payload, stop)
    ctx = runner.RunCtx(task_id=payload["taskId"], leadgen_id=task_id, account_id=account_id,
                        payload=payload, db_path=db_path)
    def progress(message):
        runner._log(ctx, message)
        runner._action(ctx, message)
    def check():
        if stop.is_set():
            raise ValueError("已停止准备，未发送任何内容")
    check.stopped = stop.is_set
    try:
        plan = build(db_path, payload, account, check=check, progress=progress)
        check()
        proposal = {"id": secrets.token_hex(16), "taskId": payload["taskId"], "leadgenTaskId": task_id,
                    "accountId": account_id, "nickname": account["nickname"],
                    "keywords": plan["effectiveConfig"]["keywords"], **plan,
                    "payload": payload, "expiresAt": int(time.time()) + 600}
        with sqlite3.connect(str(db_path)) as con:
            con.execute("CREATE TABLE IF NOT EXISTS live_workflow_proposals (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            con.execute("INSERT INTO live_workflow_proposals VALUES (?,?)",
                        (proposal["id"], json.dumps(proposal, ensure_ascii=False)))
        progress(f"已按配置准备{len(plan['actions'])}项动作，等待确认账号、目标与原文；总上限"
                 f"{plan['effectiveConfig']['interactionLimit']}，尚未发送")
        return {"success": True, "proposalId": proposal["id"], "binding": binding_of(proposal),
                "accountId": account_id, "nickname": account["nickname"], "taskId": payload["taskId"],
                "actions": public_actions(plan["actions"]), "skippedActions": plan["skippedActions"],
                "planStats": plan["planStats"], "configSummary": report["summary"],
                "warnings": plan["warnings"], "expiresAt": proposal["expiresAt"]}
    finally:
        with _LOCK:
            _PREPARING.pop(owner, None)
        registry.release_account(account_id, owner)


def read_proposal(db_path, proposal_id, binding):
    with sqlite3.connect(str(db_path)) as con:
        row = con.execute("SELECT body FROM live_workflow_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("待确认草稿不存在，请重新开始")
    proposal = json.loads(row[0])
    if proposal["expiresAt"] <= time.time() or binding_of(proposal) != binding:
        raise ValueError("草稿已过期或被修改，请重新核对")
    if _account(db_path, proposal["accountId"])["nickname"] != proposal["nickname"]:
        raise ValueError("账号身份已改变，请重新核对")
    for action in proposal["actions"]:
        verify(action)
        allowed, _ = PersistentWriteSafetyGate(db_path, namespace="live").can_attempt(action_key(proposal["accountId"], action))
        if not allowed:
            raise ValueError("目标已有发送或待核验记录，禁止重复执行")
    return proposal


def checkpoint(db_path, scope_id, account_id, task_id, actions, completed, status):
    with sqlite3.connect(str(db_path), timeout=15) as con:
        con.execute("CREATE TABLE IF NOT EXISTS leadgen_checkpoints (scope_id TEXT PRIMARY KEY, "
                    "account_id TEXT,task_id TEXT,body TEXT,updated_ms INTEGER)")
        body = {"status": status, "completed": completed, "total": len(actions),
                "remaining": max(0, len(actions) - completed)}
        con.execute("INSERT INTO leadgen_checkpoints VALUES(?,?,?,?,?) ON CONFLICT(scope_id) "
                    "DO UPDATE SET body=excluded.body,updated_ms=excluded.updated_ms",
                    (scope_id, account_id, task_id, json.dumps(body), int(time.time() * 1000)))


def _boundary(ctx):
    from app.api import task_runner as runner
    if ctx.stop.is_set():
        return False
    if ctx.pause.is_set():
        runner._action(ctx, "已暂停；继续后执行剩余清单")
        runner._emit_task(ctx, "log", message="已暂停，未开始后续动作", level="normal")
        while ctx.pause.is_set():
            if ctx.stop.wait(0.1):
                return False
        runner._action(ctx, "继续执行已确认的剩余清单")
    return not ctx.stop.is_set()


def _wait_active(ctx, seconds, next_action="继续当前浏览流程"):
    """Pause freezes the displayed remaining delay; stop clears it immediately."""
    from app.api import task_runner as runner

    remaining = max(0.0, float(seconds))
    last_displayed = None

    def publish(active, value=0, paused=False):
        payload = {"active": active, "action": next_action,
                   "remainingSeconds": max(0, int(value)), "paused": paused}
        runner._emit_task(ctx, "countdown", **payload)
        runner._emit("countdown", ctx.task_id, ctx.account_id, **payload)

    try:
        was_paused = False
        while remaining > 0:
            if ctx.pause.is_set() and not was_paused:
                publish(True, remaining + 0.999, paused=True)
                was_paused = True
            if not _boundary(ctx):
                return False
            displayed = int(remaining + 0.999)
            if was_paused:
                publish(True, displayed, paused=False)
                was_paused = False
                last_displayed = displayed
            elif displayed != last_displayed:
                publish(True, displayed)
                last_displayed = displayed
            began = time.monotonic()
            if ctx.stop.wait(min(0.1, remaining)):
                return False
            remaining -= time.monotonic() - began
        return _boundary(ctx)
    finally:
        publish(False)


def _watch(ctx, action, seconds, next_action="继续浏览当前视频"):
    # Cancel only read-only playback during pause. Resume the remaining delay,
    # and handle a second pause just like the first one.
    from app.api import task_runner as runner

    remaining = max(0.0, float(seconds))
    was_paused = False

    def publish(active, paused=False):
        payload = {"action": next_action,
                   "remainingSeconds": max(0, int(remaining + 0.999)), "paused": paused}
        runner._emit_task(ctx, "countdown", active=active, **payload)
        runner._emit("countdown", ctx.task_id, ctx.account_id, active=active, **payload)

    while True:
        if ctx.pause.is_set() and not was_paused:
            publish(True, paused=True)
            was_paused = True
        if not _boundary(ctx):
            publish(False)
            return {"ok": False, "stopped": True}
        if was_paused:
            publish(True)
            was_paused = False
        else:
            publish(True)
        began = time.monotonic()
        watched = browser.execute({"type": "watch-video", "videoUrl": action["videoUrl"],
            "seconds": remaining, "runId": ctx.task_id}, db_path=ctx.db_path, account_id=ctx.account_id,
            engine="shell", stop_check=lambda: ctx.stop.is_set() or ctx.pause.is_set())
        remaining = max(0.0, remaining - (time.monotonic() - began))
        if ctx.stop.is_set():
            publish(False)
            return {"ok": False, "stopped": True}
        if not ctx.pause.is_set():
            publish(False)
            return watched
        publish(True, paused=True)
        was_paused = True


def run_reviewed(ctx, scope):
    """Execute only the approved list, within both total and per-action limits."""
    from app.api import task_runner as runner
    from app.execution.live_leadgen import perform
    status, message, reason = "failed", "任务未完成", "action_failed"
    actions, completed = scope.get("reviewedActions"), 0
    try:
        config = scope.get("effectiveConfig") or settings.normalize(ctx.payload)
        if (not isinstance(actions, list) or not actions or len(actions) > settings.MAX_ACTIONS
                or len(actions) > config["interactionLimit"]
                or any(not isinstance(a, dict) or a.get("type") not in ACTIONS or not valid_video(a.get("videoUrl")) for a in actions)
                or len({action_key(ctx.account_id, a) for a in actions}) != len(actions)):
            raise ValueError("已确认动作清单超出配置上限或包含重复目标")
        for kind in ACTIONS:
            count = sum(a["type"] == kind for a in actions)
            if count != scope.get("actions", {}).get(kind, 0):
                raise ValueError("动作数量与确认范围不一致")
        if sum(a["type"] == "send-dm" for a in actions) > config["dmLimit"]:
            raise ValueError("私信数量超过本任务上限")
        for kind, cap in (("follow-user", config["followLimit"]), ("share-profile", config["profileShareLimit"] if config.get("profileShareLimitEnabled") else config["interactionLimit"])):
            if sum(a["type"] == kind for a in actions) > cap:
                raise ValueError("关注或分享数量超过本任务上限")
        ctx.stats.update(scope.get("planStats", {}))
        searched, verified, reached, visited, comment_context_read = set(), set(), set(), set(), set()
        ctx.stats.update(attempted=0, remaining=len(actions))
        attempted = 0
        for item in scope.get("skippedActions", []):
            ctx.stats["skipped"] += 1
            runner._log(ctx, item["type"] + " 已跳过：" + item["reason"])
        checkpoint(ctx.db_path, scope["scopeId"], ctx.account_id, ctx.leadgen_id, actions, 0, "running")
        for index, action in enumerate(actions):
            if not _boundary(ctx):
                break
            if scope.get("expiresAt", 0) <= time.time():
                raise ValueError("确认清单已过期，请重新核对剩余目标；不会继续发送")
            # 入口是「关注流 / 推荐流 / 喜欢列表 / 话题页」时，关键词那一套核验不成立：
            # keyword 只是入口名，重读同一个入口也不保证还停在当时那一屏。这时改成
            # 直接核对视频页本身还打得开、标题还在 —— 同样能挡住「目标已消失」。
            source_entry = (config.get("videoSources") or ["search"])[0] not in {"search", "specific"}
            if action.get("profileTarget") != "1" and action["videoUrl"] not in verified and source_entry:
                runner._action(ctx, "重新核对已确认的视频页")
                again = browser.execute({"type": "inspect-video", "videoUrl": action["videoUrl"],
                                         "runId": ctx.task_id}, db_path=ctx.db_path,
                                        account_id=ctx.account_id, engine="shell",
                                        stop_check=ctx.stop.is_set)
                if ctx.stop.is_set():
                    break
                if not again.get("ok") or not str((again.get("item") or {}).get("title") or "").strip():
                    raise ValueError("已确认视频页已无法打开，未继续执行")
                verified.add(action["videoUrl"])
            if (not source_entry and action.get("profileTarget") != "1"
                    and action["keyword"] != "指定视频" and action["keyword"] not in searched):
                runner._action(ctx, "重新核对关键词视频：" + action["keyword"])
                found = browser.execute({"type": "search-videos", "keyword": action["keyword"],
                                         "limit": config["videoMax"], "runId": ctx.task_id},
                                        db_path=ctx.db_path, account_id=ctx.account_id,
                                        engine="shell", stop_check=ctx.stop.is_set)
                if ctx.stop.is_set():
                    break
                # Each action's target is checked, even when the keyword repeats.
                searched.add(action["keyword"])
                verified.update(i.get("url") for i in found.get("items", []) if isinstance(i, dict))
                if not found.get("ok"):
                    raise ValueError("关键词搜索失败，未继续执行")
            if (not source_entry and action.get("profileTarget") != "1"
                    and action["keyword"] != "指定视频" and action["videoUrl"] not in verified):
                raise ValueError("关键词搜索未再找到已确认视频，未继续执行")
            if not _boundary(ctx):
                break
            if attempted and not _wait_active(ctx, random.randint(config["intervalMin"], config["intervalMax"])):
                break
            if action["videoUrl"] not in visited and config["stayMax"]:
                runner._action(ctx, "正在进入视频并按配置停留")
                watched = _watch(ctx, action, random.randint(config["stayMin"], config["stayMax"]))
                if ctx.stop.is_set():
                    break
                if not watched.get("ok"):
                    raise ValueError("进入视频或停留失败，未执行互动")
            visited.add(action["videoUrl"])
            if not _boundary(ctx):
                break
            if action["type"] in {"post-comment", "reply-comment"} and action["videoUrl"] not in comment_context_read:
                runner._action(ctx, "正在读取视频评论区并复核互动上下文")
                fresh = browser.execute({"type": "read-comments", "videoUrls": [action["videoUrl"]],
                                         "limit": 60, "runId": ctx.task_id},
                                        db_path=ctx.db_path, account_id=ctx.account_id,
                                        engine="shell", stop_check=ctx.stop.is_set)
                if ctx.stop.is_set():
                    break
                if not fresh.get("ok"):
                    raise ValueError("执行前未能读取视频评论区，已停止且未发送")
                comments = [item for item in fresh.get("comments", []) if isinstance(item, dict)]
                if not comments:
                    raise ValueError("执行前未读到可核对的评论上下文，已停止且未发送")
                comment_context_read.add(action["videoUrl"])
                ctx.stats["executionCommentsRead"] = ctx.stats.get("executionCommentsRead", 0) + len(comments)
                runner._log(ctx, f"执行前已复核评论区：读取到 {len(comments)} 条评论")
                if action["type"] == "reply-comment" and not any(
                        str(item.get("commentId") or "") == str(action.get("commentId") or "")
                        for item in comments):
                    raise ValueError("已确认的原评论已不在当前评论区，已停止且未回复")
            if not _boundary(ctx):
                break
            # 主页首作评论前的「评论前随机操作」：先随机打开同主页另一个作品看 5–10 秒。
            # 浏览失败不阻断评论（与页面说明一致），但手动停止要立刻停下。
            if (action.get("browseUrl") and valid_video(action["browseUrl"])
                    and action["type"] == "post-comment" and config.get("profileFirstRandomBrowseBeforeComment")):
                runner._action(ctx, "首作评论前先随机浏览一个作品")
                browse = _watch(ctx, {"videoUrl": action["browseUrl"]}, random.randint(5, 10))
                if ctx.stop.is_set():
                    break
                if not browse.get("ok"):
                    runner._log(ctx, "评论前随机浏览未完成，跳过浏览继续评论", "normal")
            runner._action(ctx, f"正在执行第{index + 1}/{len(actions)}项：" + action["type"])
            checkpoint(ctx.db_path, scope["scopeId"], ctx.account_id, ctx.leadgen_id, actions, completed, "executing")
            attempted += 1
            ctx.stats["attempted"] = attempted
            result = perform(ctx.db_path, action["type"], account_id=ctx.account_id, task_id=ctx.leadgen_id,
                             video_url=action["videoUrl"], text=action.get("text", ""), comment_id=action.get("commentId", ""),
                             comment_author=action.get("commentAuthor", ""), recipient_id=action.get("recipientId", ""),
                             extras={k: action[k] for k in EXTRA_FIELDS if k in action},
                             stopped=ctx.stop.is_set, run_id=ctx.task_id)
            runner._log(ctx, f"{action['type']}：{result.get('status', 'uncertain')} · {result.get('error') or result.get('code', '')}")
            if result.get("ok") is True and result.get("status") == "sent_ok":
                field = {"post-comment": "videoComments", "reply-comment": "replies", "send-dm": "dms",
                         "like-comment": "likes", "like-video": "mainVideoLikes",
                         "favorite-video": "mainVideoFavorites", "follow-user": "follows",
                         "share-profile": "profileShares", "share-comment": "commentShares"}[action["type"]]
                ctx.stats[field] = ctx.stats.get(field, 0) + 1
                if field == "videoComments":
                    ctx.stats["published"] += 1
                elif field not in ("mainVideoLikes", "mainVideoFavorites"):
                    ctx.stats["interacted"] += 1
                    reached.add(action.get("recipientId") or action.get("commentAuthor"))
                    ctx.stats["reached"] = len(reached)
                if field == "dms":
                    ctx.stats["messages"] = ctx.stats["dms"]
                ctx.stats["videos"] = len(visited)
            elif result.get("status") == "skipped":
                ctx.stats["skipped"] += 1
                runner._log(ctx, "此项已跳过，不计成功：" + str(result.get("error") or result.get("code") or ""))
            else:
                field = "uncertain" if result.get("status") == "uncertain" else "failed"
                ctx.stats[field] = ctx.stats.get(field, 0) + 1
                message = "动作失败或结果未确认，已停止且不会自动重试：" + str(result.get("error") or result.get("code") or "未知原因")
                break
            completed = index + 1
            ctx.stats["attempted"] = attempted
            ctx.stats["remaining"] = len(actions) - completed
            checkpoint(ctx.db_path, scope["scopeId"], ctx.account_id, ctx.leadgen_id, actions, completed, "running")
            runner._emit_task(ctx, "stats", stats=dict(ctx.stats))
            if action["type"] in {"follow-user", "send-dm"} and result.get("status") == "sent_ok":
                if not _wait_active(ctx, random.randint(config["followDmDelayMin"], config["followDmDelayMax"])):
                    break
        else:
            status, message, reason = "completed", "已确认清单执行完成，真实成功数已记录", "completed"
        if ctx.stop.is_set():
            status, message, reason = "stopped", "已停止，剩余动作未执行；重新开始需核对剩余清单", "manual_stop"
    except Exception as exc:
        ctx.stats["failed"] += 1
        message = str(exc) if isinstance(exc, ValueError) else "执行异常，未确认成功"
    finally:
        if isinstance(actions, list) and scope.get("scopeId"):
            checkpoint(ctx.db_path, scope["scopeId"], ctx.account_id, ctx.leadgen_id, actions, completed, status)
        runner._log(ctx, message, "normal" if status in {"completed", "stopped"} else "error")
        runner._finish(ctx, status, dict(ctx.stats), message, reason)
        runner._emit_task(ctx, "finished", status=status, stats=dict(ctx.stats), message=message, endReason=reason)
        runner._emit("status", ctx.task_id, ctx.account_id, accountId=ctx.account_id, status="finished", finishMessage=message)
        runner.event_bus.publish("leadgen-task-finished", {"taskId": ctx.task_id, "accountId": ctx.account_id,
            "status": status, "reason": reason, "message": message, "progress": {"interactions": {
                "done": (ctx.stats.get("published", 0) + ctx.stats.get("interacted", 0)
                         + ctx.stats.get("mainVideoLikes", 0) + ctx.stats.get("mainVideoFavorites", 0)),
                "planned": len(actions or [])}}})
