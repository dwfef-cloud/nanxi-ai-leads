"""Explicit, expiring desktop acceptance scope; closed unless configured locally.

No request flag can enable this. The launcher selects a local scope file. Commands
are signed and the desktop consumes each action's single attempt durably.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import time
import uuid
from pathlib import Path
from app.execution.leadgen_media import EXTRA_FIELDS


FIELDS = ("type", "accountId", "leadgenTaskId", "videoUrl", "commentId", "text", "scopeId", "deadlineAt")

# 直播间动作：点赞 / 关注主播 / 参与抽奖。它们对「房间」操作，不针对某条评论，
# 因此 videoUrl 存的是直播间地址（live.douyin.com/<roomId>）。
LIVE_ROOM_ACTIONS = frozenset({"like-live", "follow-live", "lottery-live"})
# send-dm-chat：自动回复在「私信会话里」发消息（不是从某个视频的人主页进私信），
# 目标绑定的是会话标题而不是视频地址，因此单独一个动作键。
LIVE_ACTIONS = frozenset({"like-comment", "reply-comment", "follow-user", "send-dm",
                          "send-dm-chat", "post-comment", "share-profile",
                          "like-video", "favorite-video"}) | LIVE_ROOM_ACTIONS


# 批量跟进（线索库）用的动作集：只做「对用户」的四类，且必须从用户主页/视频页发起。
# 与监控作用域共用同一套文件格式与验签，区别只在 mode 与动作次数上限。
BATCH_ACTIONS = frozenset({"follow-user", "send-dm", "share-profile", "share-comment",
                           "post-comment", "like-video", "favorite-video"})
# 一次批量最多跟进 5 万条线索（与页面「每次最大数」上限一致），远超监控的单动作 100 次。
BATCH_MAX_PER_ACTION = 50000


def monitor_scope_dir() -> Path:
    env = os.environ.get("LEADS_MONITOR_SCOPE_DIR")
    if env:
        return Path(env)
    from app import paths

    return paths.DATA_DIR / "monitor_scopes"


def monitor_scope_file(task_id: str, account_id: str) -> Path:
    digest = hashlib.sha256(str(account_id).encode()).hexdigest()[:16]
    return monitor_scope_dir() / f"{task_id}-{digest}.json"


def desktop_leadgen_scope_file(task_id: str, account_id: str) -> Path:
    """Shared location for scopes explicitly activated by the desktop launcher."""
    digest = hashlib.sha256(str(account_id).encode()).hexdigest()
    from app import paths

    return paths.DATA_DIR / "acceptance" / "leadgen_scopes" / f"{task_id}-{digest}.json"


def write_monitor_scope(*, task_id: str, account_id: str, actions: dict[str, int],
                        hours: float = 12.0, nickname: str = "") -> Path:
    """监控任务自己的写作用域。

    只在本机写、只由 task 配置显式开启的动作进白名单，有效期按任务时长给
    （默认 12 小时）。动作次数在 actions 里限定，超出即被 signed_command 拒绝。
    """
    if not task_id or not account_id:
        raise ValueError("监控作用域需要任务与账号")
    allowed = {k: int(v) for k, v in (actions or {}).items()
               if k in LIVE_ACTIONS and int(v) > 0}
    path = monitor_scope_file(task_id, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mode": "monitor", "scopeId": uuid.uuid4().hex, "secret": secrets.token_hex(32),
        "accountId": account_id, "leadgenTaskId": task_id,
        # 壳侧按昵称核对登录身份（视频页用页头「昵称关注」比对）；
        # 直播动作则用「页面已登录 + 房间在播」核对，昵称留作日志与排障。
        "nickname": str(nickname or ""),
        "expiresAt": time.time() + float(hours) * 3600,
        # 主播作品、直播间都是运行时才发现的，作用域用 * 表示「本任务范围内的任意目标」，
        # 具体对谁操作仍由 perform() 的签名命令逐条绑定。
        "actions": allowed, "videoUrls": ["*"],
        "createdAt": int(time.time() * 1000),
    }, ensure_ascii=False), encoding="utf-8")
    return path


def clear_monitor_scope(task_id: str, account_id: str = "") -> None:
    targets = ([monitor_scope_file(task_id, account_id)] if account_id
               else list(monitor_scope_dir().glob(f"{task_id}-*.json")))
    for path in targets:
        try:
            path.unlink()
        except OSError:
            pass


def write_batch_scope(*, run_id: str, account_id: str, actions: dict[str, int],
                      hours: float = 12.0, nickname: str = "") -> Path:
    """批量跟进的一次性作用域。

    一次「开始执行」= 用户在页面上明确选定了执行账号与跟进方式，这里把这份选择
    固化成作用域文件：动作集合 + 每个动作的次数上限 + 有效期，多一个动作都签不出来。
    文件与监控作用域同目录同名规则（`{run_id}-{账号哈希}.json`），因此
    `load_scope` / 壳侧 `scopeFor` 不需要额外路径逻辑。
    """
    if not run_id or not account_id:
        raise ValueError("批量作用域需要运行与账号")
    allowed = {k: int(v) for k, v in (actions or {}).items()
               if k in BATCH_ACTIONS and int(v) > 0}
    if not allowed:
        raise ValueError("批量作用域至少需要一个可执行动作")
    path = monitor_scope_file(run_id, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mode": "batch", "scopeId": uuid.uuid4().hex, "secret": secrets.token_hex(32),
        "accountId": account_id, "leadgenTaskId": run_id,
        # 壳侧核对登录身份用；空昵称会被壳判为无效作用域，所以宁可写失败也不留空。
        "nickname": str(nickname or ""),
        "expiresAt": time.time() + float(hours) * 3600,
        # 线索库的目标用户是运行时逐条取的，作用域用 * 表示「本批范围内的任意用户」，
        # 具体对谁操作仍由 perform() 的签名命令逐条绑定。
        "actions": allowed, "videoUrls": ["*"],
        "createdAt": int(time.time() * 1000),
    }, ensure_ascii=False), encoding="utf-8")
    return path


def clear_batch_scope(run_id: str, account_id: str = "") -> None:
    for path in ([monitor_scope_file(run_id, account_id)] if account_id
                 else list(monitor_scope_dir().glob(f"{run_id}-*.json"))):
        try:
            path.unlink()
        except OSError:
            pass


def scope_path(action="", account_id="", task_id="") -> str:
    # 监控任务先找自己的作用域：一个任务一个文件，互不覆盖。
    if task_id and account_id:
        candidate = monitor_scope_file(task_id, account_id)
        if candidate.is_file():
            return str(candidate)
        candidate = desktop_leadgen_scope_file(task_id, account_id)
        if candidate.is_file():
            return str(candidate)
    directory = os.environ.get("LEADS_LEADGEN_SCOPE_DIR")
    if directory and account_id:
        candidate = Path(directory) / (hashlib.sha256(account_id.encode()).hexdigest() + ".json")
        if candidate.is_file():
            return str(candidate)
    if action == "like-comment" and os.environ.get("LEADS_COMMENT_LIKE_SCOPE"):
        return os.environ["LEADS_COMMENT_LIKE_SCOPE"]
    if action in {"", "post-comment", "reply-comment", "send-dm", "like-comment", "like-video",
                  "favorite-video", "follow-user", "share-profile", "share-comment"} and os.environ.get("LEADS_LEADGEN_SCOPE"):
        return os.environ["LEADS_LEADGEN_SCOPE"]
    return os.environ.get("LEADS_LIVE_ACCEPTANCE_SCOPE", "")


def load_scope(action="", account_id="", task_id="") -> dict:
    try:
        path = scope_path(action, account_id, task_id)
        scope = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        if task_id and isinstance(scope, dict) and scope.get("leadgenTaskId") != task_id and account_id:
            return load_scope(action, task_id=task_id)
        if (not isinstance(scope, dict) or type(scope.get("expiresAt")) not in (int, float)
                or not math.isfinite(scope["expiresAt"]) or scope["expiresAt"] <= time.time()
                or not isinstance(scope.get("secret"), str) or len(scope["secret"]) < 32
                or not scope.get("accountId") or not scope.get("leadgenTaskId")
                or not scope.get("scopeId") or not isinstance(scope.get("actions"), dict)
                or not isinstance(scope.get("videoUrls"), list) or not scope["videoUrls"]):
            return {}
        return scope
    except (OSError, ValueError, TypeError):
        return {}


def task_allowed(payload: dict) -> bool:
    scope = load_scope(account_id=str(payload.get("accountId") or ""), task_id=payload.get("leadgenTaskId", ""))
    if scope.get("payloadBinding") and scope["payloadBinding"] != hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
        return False
    return bool(scope and payload.get("accountId") == scope["accountId"]
                and payload.get("leadgenTaskId") == scope["leadgenTaskId"]
                and (not scope.get("runTaskId") or payload.get("taskId") == scope["runTaskId"]))


def task_rejection_reason(payload: dict) -> str:
    """Explain a failed start check without weakening the signed task boundary."""
    account_id = str(payload.get("accountId") or "")
    task_id = str(payload.get("leadgenTaskId") or "")
    scope = load_scope(account_id=account_id, task_id=task_id)
    if not scope:
        return "未找到当前账号和任务的有效桌面启动授权，未发送"
    if scope.get("accountId") != account_id or scope.get("leadgenTaskId") != task_id:
        return "桌面启动授权中的账号或任务与本次启动不一致，未发送"
    if scope.get("runTaskId") and payload.get("taskId") != scope["runTaskId"]:
        return "桌面启动授权与本次运行编号不一致，未发送"
    if scope.get("payloadBinding") and scope["payloadBinding"] != hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
        return "启动参数与点击保存并开始时的配置不一致，未发送；请重新保存启动"
    return "桌面启动授权校验失败，未发送；请检查任务、账号和配置状态"


def _canonical(cmd: dict) -> str:
    fields = FIELDS + (("commentAuthor",) if cmd.get("type") == "like-comment" else ())
    if cmd.get("type") in {"reply-comment", "send-dm", "send-dm-chat", "follow-user",
                           "share-profile", "share-comment"}:
        fields += ("commentAuthor", "recipientId")
    if any(cmd.get(k) for k in EXTRA_FIELDS):
        fields += EXTRA_FIELDS
    return json.dumps([str(cmd.get(key) or "") for key in fields], ensure_ascii=False,
                      separators=(",", ":"))


def comment_target_allowed(scope: dict, video: str, comment: str, author: str, content: str) -> bool:
    targets = scope.get("commentTargets")
    return bool(isinstance(targets, list) and any(isinstance(t, dict) and
        t.get("videoUrl") == video and t.get("commentId") == comment and
        t.get("authorName") == author and t.get("content") == content
        for t in targets))


def signed_command(action: str, *, account_id: str, task_id: str, video_url: str,
                   text: str = "", comment_id: str = "", comment_author: str = "",
                   recipient_id: str = "",
                   extras: dict | None = None,
                   deadline_at: int | None = None) -> dict:
    scope = load_scope(action, account_id, task_id)
    extras = extras or {}
    if not isinstance(extras, dict) or any(k not in EXTRA_FIELDS for k in extras):
        raise ValueError("附图或分享参数无效")
    batch_mode = scope.get("mode") == "batch" if isinstance(scope, dict) else False
    stream_mode = scope.get("mode") == "stream" if isinstance(scope, dict) else False
    action_cap = BATCH_MAX_PER_ACTION if batch_mode else 100
    if (not scope or account_id != scope["accountId"] or task_id != scope["leadgenTaskId"]
            or (video_url not in scope["videoUrls"] and "*" not in scope["videoUrls"])
            or type(scope.get("actions", {}).get(action)) is not int
            or not 1 <= scope["actions"][action] <= action_cap):
        raise ValueError("当前账号、任务、视频或动作未包含在本次真机验收范围")
    monitor_mode = scope.get("mode") == "monitor"
    if "reviewedActions" in scope:
        approved = scope["reviewedActions"]
        if not isinstance(approved, list) or not any(isinstance(a, dict) and
                a.get("type") == action and a.get("videoUrl") == video_url and a.get("text") == text and
                a.get("commentId", "") == comment_id and a.get("commentAuthor", "") == comment_author and
                a.get("recipientId", "") == recipient_id and
                all(a.get(k, "") == extras.get(k, "") for k in EXTRA_FIELDS) for a in approved):
            raise ValueError("动作、正文或收件人与已确认清单不一致，未发送")
    elif not (monitor_mode or batch_mode or stream_mode) and (scope["actions"][action] != 1
                                               or action in {"send-dm", "follow-user", "share-profile", "share-comment"}
                                               or extras):
        raise ValueError("私信必须先确认准确的收件人与正文")
    # 监控动作来自任务配置（用户自己勾开的开关 + 每日配额），评论目标在运行时
    # 才出现，因此不需要 preview 阶段的 commentTargets 绝对清单；次数仍受
    # actions 上限与写安全门禁约束。
    if action == "like-comment" and not (monitor_mode or stream_mode) and not comment_target_allowed(
            scope, video_url, comment_id, comment_author, text):
        raise ValueError("该评论的作者、正文和定位信息未包含在本次真机验收范围")
    # 批量跟进的首作评论：视频与正文由签名逐条绑定，再要求落到本次已写入的
    # reviewedComments 清单里（作用域由执行器在生成文案时追加，见 batch_runner）。
    if action == "post-comment" and batch_mode and "reviewedComments" not in scope:
        raise ValueError("批量首作评论缺少已确认文案清单，未发送")
    if action == "post-comment" and "reviewedComments" in scope:
        reviewed = scope["reviewedComments"]
        if not isinstance(reviewed, list) or not any(
                isinstance(item, dict) and item.get("videoUrl") == video_url
                and item.get("text") == text for item in reviewed):
            raise ValueError("评论正文与本次已确认草稿不一致，未发送")
    cmd = {"type": action, "accountId": account_id, "leadgenTaskId": task_id,
           "videoUrl": video_url, "text": text, "commentId": comment_id,
           "scopeId": scope["scopeId"]}
    if action == "like-comment":
        cmd["commentAuthor"] = comment_author
    if action in {"reply-comment", "send-dm", "send-dm-chat", "follow-user",
                  "share-profile", "share-comment"}:
        cmd.update(commentAuthor=comment_author, recipientId=recipient_id)
    cmd.update(extras)
    cmd["deadlineAt"] = int(deadline_at if deadline_at is not None else time.time() * 1000 + 105000)
    cmd["acceptanceSignature"] = hmac.new(scope["secret"].encode(),
                                          _canonical(cmd).encode(), hashlib.sha256).hexdigest()
    # 把作用域文件位置一并给壳：监控作用域按「任务+账号」命名，壳没法靠账号哈希推导出来，
    # 只给路径 —— 内容仍由壳逐项校验（账号/任务/scopeId/有效期/动作上限）并用 secret 验签。
    try:
        cmd["scopeFile"] = str(scope_path(action, account_id, task_id))
    except OSError:
        pass
    return cmd


def command_allowed(cmd: dict, account_id: str = "") -> bool:
    try:
        expected = signed_command(str(cmd.get("type") or ""),
                                  account_id=str(cmd.get("accountId") or ""),
                                  task_id=str(cmd.get("leadgenTaskId") or ""),
                                  video_url=str(cmd.get("videoUrl") or ""),
                                  text=str(cmd.get("text") or ""),
                                  comment_id=str(cmd.get("commentId") or ""),
                                  comment_author=str(cmd.get("commentAuthor") or ""),
                                  recipient_id=str(cmd.get("recipientId") or ""),
                                  extras={k: cmd[k] for k in EXTRA_FIELDS if k in cmd},
                                  deadline_at=int(cmd.get("deadlineAt") or 0))
        return ((not account_id or cmd.get("accountId") == account_id)
                and 0 < expected["deadlineAt"] - time.time() * 1000 <= 110000
                and cmd.get("scopeId") == expected["scopeId"]
                and hmac.compare_digest(str(cmd.get("acceptanceSignature") or ""),
                                        expected["acceptanceSignature"]))
    except (ValueError, TypeError):
        return False
