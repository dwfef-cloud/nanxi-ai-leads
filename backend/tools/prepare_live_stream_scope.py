"""Create a bounded desktop-authorized scope for sequential live lead generation."""
import argparse
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.execution import leadgen_config
from app.execution.live_workflow import _account


def prepare(db, payload, output):
    payload = dict(payload or {})
    account_id = str(payload.get("accountId") or "")
    task_id = str(payload.get("leadgenTaskId") or "")
    run_id = str(payload.get("taskId") or "")
    if not account_id or not task_id or not run_id:
        raise ValueError("缺少任务或执行账号")
    report = leadgen_config.inspect(payload, account_id=account_id)
    if not report["success"]:
        raise ValueError("；".join(report["errors"]))
    config = leadgen_config.normalize(payload, account_id=account_id, sample=False)
    account = _account(db, account_id)
    caps = {}
    for flag, action in leadgen_config.ACTION_FLAGS.items():
        if not config.get(flag):
            continue
        cap = config["interactionLimit"]
        if action == "send-dm":
            cap = config["dmLimit"]
        elif action == "follow-user":
            cap = config["followLimit"]
        elif action == "share-profile" and config.get("profileShareLimitEnabled"):
            cap = config["profileShareLimit"]
        elif action == "like-comment":
            cap = config["likeMax"] * config["videoMax"]
        elif action == "reply-comment":
            cap = config["commentMax"] * config["videoMax"]
        elif action == "share-comment":
            cap = config["commentShareMax"] * config["videoMax"]
        caps[action] = min(config["interactionLimit"], cap)
    if config.get("enableWarmup") or config.get("commentOnProfileFirstWork"):
        caps["like-video"] = min(config["interactionLimit"], caps.get("like-video", 0) + config["warmupMax"])
        if config.get("enableWarmup"):
            caps["favorite-video"] = min(config["interactionLimit"], config["warmupMax"])
    if config.get("commentOnProfileFirstWork") and config.get("enableProfileFirstComment"):
        caps["post-comment"] = config["interactionLimit"]
        if config.get("profileFirstCommentFallbackMode") == "reply":
            caps["reply-comment"] = config["interactionLimit"]
    caps = {action: cap for action, cap in caps.items() if cap > 0}
    if not caps:
        raise ValueError("当前配置没有可执行的互动动作")
    video_count = config["videoMax"] * (len(config["keywords"])
        if config["videoSources"][0] in {"search", "hashtag"} else 1)
    duration = (600 + video_count * (config["stayMax"] + 240)
                + config["interactionLimit"] * (210 + config["intervalMax"]))
    if duration > 86400:
        raise ValueError("本次顺序浏览预计超过24小时，请减少关键词、视频数量或停留时间")
    scope = {
        "mode": "stream", "scopeId": "desktop-leadgen-stream-" + secrets.token_hex(16),
        "secret": secrets.token_hex(32), "expiresAt": int(time.time()) + duration,
        "accountId": account_id, "nickname": account["nickname"],
        "leadgenTaskId": task_id, "runTaskId": run_id,
        "payloadBinding": hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
        # 只用于「被拒时告诉用户哪几项参数变了」，不参与放行判断（放行仍看 payloadBinding）。
        "payloadSnapshot": payload,
        "keywords": config["keywords"], "effectiveConfig": config,
        "actions": caps, "interactionLimit": config["interactionLimit"], "videoUrls": ["*"],
        "createdAt": int(time.time() * 1000),
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(scope, stream, ensure_ascii=False)
    return {"success": True, "scopeFile": str(output), "payload": payload,
            "nickname": account["nickname"], "configSummary": report["summary"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--payload-json", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.db, json.loads(args.payload_json), args.output), ensure_ascii=True))
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "任务配置校验或授权准备失败"
        print(json.dumps({"success": False, "error": message}, ensure_ascii=True))
        sys.exit(1)
