"""Trusted desktop helper; called only after the native review dialog succeeds."""
import argparse
import json
import hashlib
from pathlib import Path
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.execution.live_workflow import read_proposal
from app.execution.leadgen_config import scope_duration
from app import leadgen_pools


def prepare(db, proposal_id, binding, output):
    selected = read_proposal(db, proposal_id, binding)
    config = selected.get("effectiveConfig", {})
    duration = scope_duration(config, selected["actions"])
    scope = {"scopeId": "desktop-leadgen-" + secrets.token_hex(16), "secret": secrets.token_hex(32),
             "expiresAt": int(time.time()) + duration,
             "accountId": selected["accountId"], "nickname": selected["nickname"],
             "leadgenTaskId": selected["leadgenTaskId"], "runTaskId": selected["taskId"],
             "keywords": selected["keywords"], "reviewedActions": selected["actions"],
             "skippedActions": selected.get("skippedActions", []),
             "effectiveConfig": config, "planStats": selected.get("planStats", {}),
             "payloadBinding": hashlib.sha256(json.dumps(selected["payload"], ensure_ascii=False,
                 sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
             # 只用于被拒时逐项说明「哪几个参数变了」，不参与放行判断。
             "payloadSnapshot": selected["payload"],
             "videoUrls": list(dict.fromkeys(a["videoUrl"] for a in selected["actions"])),
             "actions": {kind: sum(a["type"] == kind for a in selected["actions"])
                         for kind in {a["type"] for a in selected["actions"]}}}
    scope["commentTargets"] = [{"videoUrl": a["videoUrl"], "commentId": a["commentId"],
                                "authorName": a["commentAuthor"], "content": a["text"]}
                               for a in selected["actions"] if a["type"] == "like-comment"]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(scope, stream, ensure_ascii=False)
    # The shipped frontend saves after start-task returns. Persist first so even
    # a very fast failure has a task record to update; late UI saves merge results.
    old = leadgen_pools.load_records(db).get(selected["leadgenTaskId"], {})
    record = {**old, "id": selected["leadgenTaskId"], "name": selected["payload"].get("taskName") or old.get("name") or "评论获客",
              "status": "stopped", "configSnapshot": selected["payload"], "liveReviewedRunId": selected["taskId"],
              "startedAt": int(time.time() * 1000), "endedAt": None, "endReason": "", "stats": {},
              "runs": old.get("runs", [])}
    leadgen_pools.reviewed_run_update(db, selected["leadgenTaskId"], selected["accountId"], selected["taskId"], initial=record)
    return {"success": True, "scopeFile": str(output), "payload": selected["payload"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("db", "proposal-id", "binding", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        # Windows pipes may use GBK while Electron decodes stdout as UTF-8.
        # ASCII JSON preserves Chinese paths and text across either encoding.
        print(json.dumps(prepare(args.db, args.proposal_id, args.binding, args.output), ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc) if isinstance(exc, ValueError) else "草稿校验或保存失败"}, ensure_ascii=True))
        sys.exit(1)
