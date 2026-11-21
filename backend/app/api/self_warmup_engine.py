"""Offline automatic-reply event evaluator.

This module deliberately stops at generating a proposal. It has no platform
reader and no write-capable adapter; callers cannot send comments or DMs here.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from app.api import task_store

EVENT_SWITCHES = {
    "comment": "watchComments",
    "reply": "watchReplies",
    "like": "watchLikes",
    "follow": "watchFollows",
    "message": "watchMessages",
}
ACTION_SWITCHES = {
    "comment": ("actionCommentLike", "actionCommentReply", "actionCommentFollow", "actionCommentDm"),
    "reply": ("actionReplyLike", "actionReplyReply", "actionReplyFollow", "actionReplyDm"),
    "like": ("", "", "actionLikeFollow", "actionLikeDm"),
    "follow": ("", "", "actionFollowBack", "actionFollowDm"),
    "message": ("", "actionMessageReply", "", ""),
}
ACTION_NAMES = ("like", "reply", "follow", "dm")
MAX_EVENTS = 1000


def _first(event: dict, *keys: str) -> str:
    for key in keys:
        value = event.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _words(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"[,，、;；\s]+", str(value or ""))
    return [str(item).strip() for item in values if str(item).strip()]


def _template(cfg: dict, event_type: str, nickname: str, content: str) -> str:
    if event_type == "message":
        items = cfg.get("dmTemplateItems")
        fallback = cfg.get("dmTemplate")
    else:
        items = cfg.get("replyTemplateItems")
        fallback = cfg.get("replyTemplate")
    text = ""
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and str(item.get("text") or "").strip():
                text = str(item["text"]).strip()
                break
    text = text or str(fallback or "").strip()
    return text.replace("{nickname}", nickname or "朋友").replace("{content}", content)


def evaluate_event(config: dict, event: dict) -> dict:
    """Return a deterministic offline proposal for one normalized event."""
    kind = str(event.get("eventType") or event.get("category") or "").strip().lower()
    if kind not in EVENT_SWITCHES:
        return {"status": "ignored", "reason": "unsupported_event_type", "actions": []}
    if config.get(EVENT_SWITCHES[kind]) is False:
        return {"status": "ignored", "reason": "source_disabled", "actions": []}
    if kind == "message" and config.get("excludeGroupChats") is True and event.get("isGroupChat") is True:
        return {"status": "ignored", "reason": "group_chat_excluded", "actions": []}

    nickname = _first(event, "nickname", "userName", "username")
    content = _first(event, "content", "text", "comment")
    excluded = {word.casefold() for word in _words(config.get("excludeUsers"))}
    if nickname.casefold() in excluded:
        return {"status": "ignored", "reason": "excluded_user", "actions": []}

    keywords = _words(config.get("keywords"))
    if keywords and content and not any(word.casefold() in content.casefold() for word in keywords):
        return {"status": "ignored", "reason": "keyword_mismatch", "actions": []}

    if config.get("enableAutoActions") is False:
        return {"status": "recorded", "reason": "automatic_actions_disabled", "actions": []}

    actions = []
    switches = ACTION_SWITCHES[kind]
    for name, key in zip(ACTION_NAMES, switches):
        if key and config.get(key) is True:
            actions.append({"type": name, "status": "proposal_only"})
    reply = _template(config, kind, nickname, content) if any(
        item["type"] in {"reply", "dm"} for item in actions) else ""
    for action in actions:
        if action["type"] in {"reply", "dm"}:
            action["text"] = reply
    return {
        "status": "suggested" if actions else "recorded",
        "reason": "offline_proposal_only" if actions else "no_matching_action",
        "actions": actions,
    }


def _fingerprint(task_id: str, event: dict) -> str:
    # Do not persist platform IDs: dedupe on a one-way digest of the minimal event fields.
    identity = "\x1f".join((task_id, _first(event, "accountId", "account"),
                             str(event.get("eventType") or event.get("category") or ""),
                             _first(event, "nickname", "userName", "username"),
                             _first(event, "content", "text", "comment"),
                             _first(event, "createdAt", "timestamp", "time")))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def simulate_event(db_path, args: Any) -> dict:
    """Persist one offline fixture event and its proposal; never calls a platform."""
    if not isinstance(args, dict) or args.get("simulation") is not True:
        return {"success": False, "code": "simulation_required",
                "error": "仅接受显式标记 simulation=true 的离线事件；未读取或发送平台消息"}
    task_id = str(args.get("taskId") or "").strip()
    raw = args.get("event")
    if not task_id or not isinstance(raw, dict):
        return {"success": False, "error": "需要 taskId 和 event 对象"}
    record = task_store.get_task(db_path, "warmup", task_id)
    if not record:
        return {"success": False, "error": "找不到自动回复任务"}
    config = record.get("configSnapshot")
    if not isinstance(config, dict) or "enableAutoActions" not in config:
        return {"success": False, "error": "该任务缺少自动回复配置快照"}

    event = {
        "eventType": str(raw.get("eventType") or raw.get("category") or "").strip().lower(),
        "nickname": _first(raw, "nickname", "userName", "username")[:120],
        "content": _first(raw, "content", "text", "comment")[:2000],
        "accountId": _first(raw, "accountId", "account")[:120],
        "isGroupChat": raw.get("isGroupChat") is True,
        "createdAt": _first(raw, "createdAt", "timestamp", "time") or int(time.time() * 1000),
    }
    fingerprint = _fingerprint(task_id, event)
    old_events = record.get("events") if isinstance(record.get("events"), list) else []
    if any(isinstance(item, dict) and item.get("fingerprint") == fingerprint for item in old_events):
        return {"success": True, "duplicate": True, "mode": "simulation",
                "event": next(item for item in old_events if item.get("fingerprint") == fingerprint)}

    proposal = evaluate_event(config, event)
    stored = {**event, **proposal, "fingerprint": fingerprint,
              "id": "sim-" + fingerprint[:16], "simulation": True,
              "createdAt": int(time.time() * 1000)}
    record["events"] = (old_events + [stored])[-MAX_EVENTS:]
    stats = record.get("stats") if isinstance(record.get("stats"), dict) else {}
    stats["eventsTotal"] = int(stats.get("eventsTotal") or 0) + 1
    stats["suggestions"] = int(stats.get("suggestions") or 0) + sum(
        action["type"] in {"reply", "dm"} for action in proposal["actions"])
    stats["actionsExecuted"] = int(stats.get("actionsExecuted") or 0)
    stats["simulatedActions"] = int(stats.get("simulatedActions") or 0) + len(proposal["actions"])
    record["stats"] = stats
    saved = task_store.save_task(db_path, "warmup", record)
    if not saved.get("success"):
        return {"success": False, "error": saved.get("error") or "离线事件保存失败"}
    return {"success": True, "mode": "simulation", "sourceAvailable": False,
            "event": stored, "stats": stats,
            "note": "离线模拟已记录；actionsExecuted保持为0，未读取或发送平台消息"}
