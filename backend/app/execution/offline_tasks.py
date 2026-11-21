"""Offline task orchestration with injected search/generator/clock fixtures only.

The dedicated offline API may run it with fixed fixtures. It never imports a live
browser transport, emits production events, or updates live task statistics.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.execution.offline_simulation import OfflineSimulator, SimulatedAction, _video_id


def run_leadgen_offline(
    payload: dict, *, simulator: OfflineSimulator,
    search: Callable[[str], list[str]], generate: Callable[[str], str | None],
    wait: Callable[[float], bool], stopped: Callable[[], bool],
    interactions: Callable[[str], list[SimulatedAction]] | None = None,
) -> dict:
    """Exercise the real task decisions with fixtures; wait returns True on stop.

    All public counters are explicitly simulated. An uncertain comment stops the
    whole task before another target or interaction can be attempted.
    """
    from app.api.task_runner import COMMENT_COOLDOWN_S, EMPTY_STATS, MAX_VIDEOS_PER_RUN

    stats = {"simulatedPublished": 0, "simulatedInteracted": 0,
             "simulatedReached": 0, "already": 0, "skipped": 0,
             "failed": 0, "uncertain": 0}
    report = {"mode": "offline", "status": "completed", "stats": dict(EMPTY_STATS),
              "simulationStats": stats, "items": []}
    account = payload.get("accountId")
    if not isinstance(account, str) or not account.strip() or not payload.get("taskId"):
        return {**report, "status": "rejected", "error": "缺少任务或账号"}
    targets = payload.get("videoUrls") or []
    if not isinstance(targets, list):
        return {**report, "status": "rejected", "error": "视频链接须为列表"}
    targets = list(targets)
    keywords = payload.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    if not isinstance(keywords, list):
        return {**report, "status": "rejected", "error": "关键词须为列表"}
    if not targets:
        for keyword in keywords[:MAX_VIDEOS_PER_RUN]:
            if stopped():
                report["status"] = "stopped"
                return report
            if not isinstance(keyword, str) or not keyword.strip():
                continue
            try:
                results = search(keyword.strip())
            except Exception:
                return {**report, "status": "failed", "error": "离线搜索驱动异常"}
            if not isinstance(results, list):
                return {**report, "status": "failed", "error": "离线搜索结果格式错误"}
            targets.extend(results)
    if not targets:
        return {**report, "status": "rejected", "error": "没有可执行视频目标"}
    seen = set()
    count = 0
    previous_attempt = False
    reached = set()
    for url in targets:
        if stopped():
            report["status"] = "stopped"
            break
        if not isinstance(url, str) or not _video_id(url):
            stats["skipped"] += 1
            continue
        video_id = _video_id(url)
        if video_id in seen:
            stats["already"] += 1
            continue
        if count >= MAX_VIDEOS_PER_RUN:
            break
        seen.add(video_id)
        count += 1
        if previous_attempt and wait(COMMENT_COOLDOWN_S[0]):
            report["status"] = "stopped"
            break
        if stopped():
            report["status"] = "stopped"
            break
        try:
            text = generate(url)
        except Exception:
            text = None
        if not isinstance(text, str) or not text.strip():
            stats["skipped"] += 1
            report["items"].append({"target": url, "status": "skipped_no_ai"})
            continue
        result = simulator.run(SimulatedAction("post_comment", account.strip(), url, text))
        report["items"].append({"target": url, "status": result})
        if result == "uncertain":
            stats["uncertain"] += 1
            report["status"] = "needs_confirmation"
            break
        if result == "simulated_ok":
            stats["simulatedPublished"] += 1
            previous_attempt = True
            if interactions is not None:
                try:
                    plan = interactions(url)
                except Exception:
                    report["status"] = "failed"
                    report["error"] = "离线互动计划生成失败"
                    break
                if not isinstance(plan, list):
                    report["status"] = "failed"
                    report["error"] = "离线互动计划格式错误"
                    break
                for action in plan[:MAX_VIDEOS_PER_RUN]:
                    if stopped():
                        report["status"] = "stopped"
                        break
                    if (not isinstance(action, SimulatedAction) or action.account_id != account.strip()
                            or action.action not in {"reply_comment", "dm"}):
                        stats["skipped"] += 1
                        continue
                    if action.action == "reply_comment":
                        if (not isinstance(action.video_url, str)
                                or action.video_url and _video_id(action.video_url) != video_id):
                            stats["skipped"] += 1
                            continue
                        action = replace(action, video_url=url)
                    outcome = simulator.run(action)
                    report["items"].append({"action": action.action, "target": action.target, "status": outcome})
                    if outcome == "simulated_ok":
                        stats["simulatedInteracted"] += 1
                        # Only DM has a recipient key. A comment key must not be
                        # counted as another distinct person without evidence.
                        if action.action == "dm":
                            reached.add(action.target)
                            stats["simulatedReached"] = len(reached)
                    elif outcome in {"uncertain", "skipped_uncertain_pending_manual",
                                     "skipped_manual_review_required"}:
                        stats["uncertain"] += 1
                        report["status"] = "needs_confirmation"
                        break
                    elif outcome == "simulated_fail":
                        stats["failed"] += 1
                    elif outcome == "skipped_already_sent":
                        stats["already"] += 1
                    else:
                        stats["skipped"] += 1
                if report["status"] != "completed":
                    break
        elif result == "simulated_fail":
            stats["failed"] += 1
            previous_attempt = True
        elif result in {"skipped_uncertain_pending_manual", "skipped_manual_review_required"}:
            stats["uncertain"] += 1
            report["status"] = "needs_confirmation"
            break
        elif result == "skipped_already_sent":
            stats["already"] += 1
        else:
            stats["skipped"] += 1
    return report
