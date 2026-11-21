"""Downgrade one local acceptance result after observed refresh lost its like.

This local audit cannot promote success or release retry reservations. Evidence
files must come from the authenticated desktop observer; there is no IPC route.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.comment_likes import normalized


def append_review_log(con, run_id, task_id, account_id, stamp):
    message = "[受限真机] 刷新复核：平台返回成功但未保留点赞；真实新增已归零，记为结果不确定，禁止自动重试"
    con.execute("INSERT INTO task_runtime_logs(task_id,leadgen_task_id,account_id,ts,level,message) "
                "SELECT ?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM task_runtime_logs WHERE task_id=? AND ts=? AND message=?)",
                (run_id, task_id, account_id, stamp, "warning", message, run_id, stamp, message))


def review(db, accepted, refreshed):
    target = accepted["target"]
    real = accepted["realResults"]
    if (len(real) != 1 or real[0].get("mode") != "live" or real[0].get("status") != "success"
            or accepted.get("probe", {}).get("already") is not True
            or refreshed.get("afterRefresh") is not True or refreshed.get("readbackOk") is not True
            or refreshed.get("http") != 200 or refreshed.get("code") != 0
            or type(refreshed.get("userDigged")) is not int or refreshed["userDigged"] != 0
            or refreshed.get("commentId") != target["commentId"]
            or refreshed.get("videoUrl") != target["videoUrl"]
            or any(refreshed.get(k) != accepted[k] for k in ("taskId", "accountId"))
            or refreshed.get("observedAt", 0) <= accepted.get("observedAt", 0)
            or not any(r.get("comment") == target["commentId"] and r.get("video") == target["videoUrl"].rsplit("/", 1)[-1]
                       and r.get("action") == "1" and r.get("http") == 200 and r.get("code") == 0
                       for r in accepted.get("receipts", []))):
        raise ValueError("Evidence is not an exact successful receipt followed by lost refresh state")
    expected = real[0]
    with sqlite3.connect(db, timeout=15) as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute("SELECT id,snapshot FROM comment_like_results WHERE run_id=? AND task_id=?", (expected["runId"], accepted["taskId"])).fetchall()
        if len(rows) != 1:
            raise ValueError("Run result is not unique")
        result_id, raw = rows[0]
        result = json.loads(raw)
        if result.get("refreshReview", {}).get("code") == "comment_like_not_retained_after_refresh":
            append_review_log(con, expected["runId"], accepted["taskId"], accepted["accountId"], refreshed["observedAt"])
            return {"changed": False, "status": "uncertain"}
        if result != expected or result["accountId"] != accepted["accountId"]:
            raise ValueError("Persisted result changed since observation")
        sample = result["sample"]
        cached = con.execute("SELECT snapshot FROM comment_like_samples WHERE id=?", (sample["sampleKey"],)).fetchone()
        if not cached or json.loads(cached[0]).get("platform_ref") != target["commentId"] or any(sample[k] != target[k] for k in ("videoUrl", "authorName", "content")):
            raise ValueError("Comment identity no longer matches")
        item = json.loads(con.execute("SELECT snapshot FROM comment_like_runs WHERE id=?", (expected["runId"],)).fetchone()[0])
        records = json.loads(con.execute("SELECT value FROM system_settings WHERE category='leadgen_tasks' AND key='records'").fetchone()[0])
        task = records[accepted["taskId"]]
        if item["status"] != "completed" or item["mode"] != "live" or task.get("stats", {}).get("commentLikeRunId") != item["id"]:
            raise ValueError("Only the same terminal current real run can be reviewed")
        code = "comment_like_not_retained_after_refresh"
        reason = "平台返回成功但刷新后未保留点赞，已停止且禁止自动重试"
        result["originalAcceptance"] = {k: result[k] for k in ("status", "reason", "updatedAt")}
        result.update(status="uncertain", reason=reason, updatedAt=refreshed["observedAt"],
                      refreshReview={"code": code, "observedAt": refreshed["observedAt"], "userDigged": 0, "diggCount": refreshed.get("diggCount")})
        if item["stats"]["commentLikeSuccess"] != 1:
            raise ValueError("Unexpected acceptance counter")
        item["stats"]["commentLikeSuccess"] = 0
        item["stats"]["commentLikeUncertain"] += 1
        item["status"] = "failed"
        task.update(status="failed", endReason=code, lastMessage=reason, stats={**task["stats"], **item["stats"]})
        con.execute("UPDATE comment_like_results SET snapshot=? WHERE id=?", (json.dumps(result, ensure_ascii=False), result_id))
        con.execute("UPDATE comment_like_runs SET snapshot=? WHERE id=?", (json.dumps(item, ensure_ascii=False), item["id"]))
        con.execute("UPDATE system_settings SET value=? WHERE category='leadgen_tasks' AND key='records'", (json.dumps(records, ensure_ascii=False),))
        con.execute("UPDATE live_action_results SET status='uncertain',code=? WHERE run_id=? AND action='like-comment' AND status='sent_ok'", (code, item["id"]))
        logical = [target["videoUrl"], normalized(target["authorName"]).casefold(), normalized(target["content"])]
        target_key = hashlib.sha256(json.dumps(logical, ensure_ascii=False).encode()).hexdigest()
        keys = [json.dumps(["comment-like-live", accepted["accountId"], target_key]),
                json.dumps([accepted["accountId"], "like-comment", target["videoUrl"], target["commentId"]], separators=(",", ":"))]
        for key in keys:
            con.execute("UPDATE write_reservations SET status='uncertain',reason=?,updated_at=? WHERE namespace='live' AND target_hash=?", (code, int(time.time()), hashlib.sha256(key.encode()).hexdigest()))
        append_review_log(con, item["id"], accepted["taskId"], accepted["accountId"], refreshed["observedAt"])
    return {"changed": True, "taskId": accepted["taskId"], "runId": item["id"], "status": "uncertain", "reason": reason}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--accepted", required=True)
    parser.add_argument("--refreshed", required=True)
    args = parser.parse_args()
    print(json.dumps(review(args.db, json.loads(Path(args.accepted).read_text(encoding="utf-8")),
                            json.loads(Path(args.refreshed).read_text(encoding="utf-8"))), ensure_ascii=True))
