"""Prepare one local acceptance scope from a saved task and real reader cache.

Does not start a task or contact the platform. Never prints the signing secret.
Run only for a task/account/comment explicitly authorized by the user.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.comment_likes import normalized, video_url


def preview(db, task_id, *, check_history=False):
    with sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        raw = con.execute("SELECT value FROM system_settings WHERE category='leadgen_tasks' AND key='records'").fetchone()
        task = json.loads(raw[0])[task_id]
        cfg = task["configSnapshot"]
        if task.get("status") in {"running", "starting", "stopping"}:
            raise ValueError("Task is still active")
        accounts = cfg["selectedAccounts"]
        if cfg.get("sourceTypes") != ["comment_like"] or len(accounts) != 1:
            raise ValueError("Requires exactly one executing account and one comment")
        account = con.execute("SELECT * FROM accounts WHERE id=?", (accounts[0],)).fetchone()
        if not account or account["platform"] != "douyin" or not account["nickname"]:
            raise ValueError("Account identity is missing")
        candidates = []
        source = cfg.get("commentLikeSource", "local")
        if source == "local":
            selected = cfg.get("visibilityLocalSampleIds") or {}
            if set(selected) != set(accounts) or len(selected[accounts[0]]) != 1:
                raise ValueError("Requires exactly one local comment owned by the executing account")
            key = selected[accounts[0]][0]
            for row in con.execute("SELECT * FROM live_action_results WHERE account_id=? AND action='post-comment' AND status='sent_ok'", (accounts[0],)):
                binding = [accounts[0], str(row["id"]), row["url"], row["text"], row["created_ms"]]
                digest = "main:" + hashlib.sha256(json.dumps(binding, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
                if key in {f"local:{row['id']}", digest}:
                    candidates.append({"videoUrl": video_url(row["url"]), "authorName": normalized(account["nickname"]), "content": normalized(row["text"])})
        elif source == "manual":
            targets = cfg.get("commentLikeManualTargets")
            if not isinstance(targets, list) or len(targets) != 1:
                raise ValueError("Requires exactly one cached manual comment")
            chosen = targets[0]
            row = con.execute("SELECT snapshot FROM comment_like_samples WHERE id=?", (chosen.get("sampleKey") or "",)).fetchone()
            if not row:
                raise ValueError("Manual comment must come from the real reader cache")
            cached = json.loads(row[0])
            target = {"videoUrl": video_url(chosen.get("videoUrl")),
                      "authorName": normalized(chosen.get("authorName")).lstrip("@").strip(),
                      "content": normalized(chosen.get("content"))}
            if cached.get("kind") != "main" or any(cached.get(k) != v for k, v in target.items()):
                raise ValueError("Cached manual comment identity was edited or is not a main comment")
            if not re.fullmatch(r"[0-9]{6,}", str(cached.get("platform_ref") or "")):
                raise ValueError("Cached manual comment platform identity is missing")
            candidates.append(target)
        else:
            raise ValueError("Unsupported comment source")
        if len(candidates) != 1:
            raise ValueError("Saved selection no longer resolves uniquely")
        target = candidates[0]
        matches = [json.loads(row[0]) for row in con.execute("SELECT snapshot FROM comment_like_samples")]
        refs = {str(s.get("platform_ref")) for s in matches
                if all(s.get(k) == target[k] for k in target) and re.fullmatch(r"[0-9]{6,}", str(s.get("platform_ref") or ""))}
        if len(refs) != 1:
            raise ValueError("Read the real comment first; platform identity must be unique")
        if check_history:
            logical = [target["videoUrl"], target["authorName"].casefold(), target["content"]]
            target_key = hashlib.sha256(json.dumps(logical, ensure_ascii=False).encode()).hexdigest()
            reservation_key = json.dumps(["comment-like-live", accounts[0], target_key])
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "write_reservations" in tables:
                row = con.execute("SELECT status FROM write_reservations WHERE namespace='live' AND target_hash=?",
                                  (hashlib.sha256(reservation_key.encode()).hexdigest(),)).fetchone()
                if row and row[0] in {"uncertain", "sent_ok"}:
                    raise ValueError("该账号对此评论已执行或结果待核验，禁止重复提交；请选择另一条评论")
        binding = hashlib.sha256(json.dumps([task_id, cfg, account["nickname"], target, sorted(refs)],
                                           ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"taskId": task_id, "accountId": accounts[0], "nickname": account["nickname"],
            "target": {**target, "commentId": next(iter(refs))}, "binding": binding, "attempts": 1}


def prepare(db, task_id, output, *, expected_binding=None, check_history=False):
    selected = preview(db, task_id, check_history=check_history)
    if expected_binding is not None and selected["binding"] != expected_binding:
        raise ValueError("任务或评论已修改，请重新核对后启动")
    target = selected["target"]
    scope = {"secret": secrets.token_hex(32), "scopeId": "comment-like-" + secrets.token_hex(12),
             "expiresAt": int(time.time()) + 1800, "accountId": selected["accountId"], "nickname": selected["nickname"],
             "leadgenTaskId": task_id, "videoUrls": [target["videoUrl"]], "actions": {"like-comment": 1},
             "commentTargets": [target]}
    path = Path(output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(scope, handle, ensure_ascii=False, indent=2)
    return {"scopeFile": str(path), "taskId": task_id, "nickname": scope["nickname"],
            "target": {k: v for k, v in target.items() if k != "commentId"}, "attempts": 1, "expiresAt": scope["expiresAt"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--output")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--check-history", action="store_true")
    parser.add_argument("--expected-binding")
    args = parser.parse_args()
    try:
        if args.preview:
            result = preview(args.db, args.task_id, check_history=args.check_history)
        else:
            if not args.output:
                raise ValueError("Output is required")
            result = prepare(args.db, args.task_id, args.output, expected_binding=args.expected_binding,
                             check_history=args.check_history)
        print(json.dumps({"success": True, **result}, ensure_ascii=True))
    except (ValueError, KeyError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=True))
        sys.exit(1)
