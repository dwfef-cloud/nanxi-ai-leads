"""A read-only projection over actual execution services; no platform actions."""
from app import comment_likes
from app.runtime import registry
from app.api import task_runner


def snapshot(db_path):
    # Do not swallow transport/service failures as an empty running list.
    ordinary = [*registry.active(), *task_runner.active_records()]
    likes = comment_likes.service(db_path).dashboard()
    return {"success": True, "items": likes, "executions": ordinary}


def stop(db_path, payload):
    if payload.get("taskKind") != "comment_like" or not payload.get("runId"):
        return {"success": False, "error": "缺少准确的任务类型或运行编号"}
    return comment_likes.service(db_path).stop(str(payload.get("taskId") or ""), str(payload["runId"]))


def stop_all(db_path):
    """Stop only the observed instances, never a newly substituted run."""
    service = comment_likes.service(db_path)
    results = [service.stop(item["taskId"], item["runId"])
               for item in service.dashboard() if not item["isSessionFinished"]]
    # A run finishing between snapshot and stop needs no further control.
    return {"success": True, "requested": sum(item.get("stopped", 0) for item in results)}
