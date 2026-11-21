"""线索库 · 批量跟进执行器。

为什么单独一层
--------------
``batch_follow`` 只负责任务、运行记录、领取规则这些**数据**；真正把动作发到抖音
的是这里。以前 ``start_batch`` 到这一步直接返回「动作执行器未接通」，界面看着
能配、能点，实际一条都不会发 —— 这是清单 #124 的真缺口。

授权从哪来
----------
用户点「开始执行」时已经**明确选定**了执行账号、跟进方式和线索清单，这三件事就是
授权本身。执行器把它们固化成一份一次性作用域（``live_scope.write_batch_scope``）：
账号 + 动作集合 + 每个动作的次数上限 + 有效期。之后每条命令都要
``live_leadgen.perform`` 用作用域里的 secret 逐条签名，壳侧再按账号/任务/目标/
次数上限复核一遍。多一个动作、多一条线索都签不出来。

不假成功
--------
- 每个动作的真实结果（sent_ok / sent_fail / uncertain / skipped）原样进 ``batch_follow_items``；
- 首作不存在、用户私密、页面读不到，都记具体原因，绝不记成功；
- 桌面壳不在线直接不启动，不建「运行中」的空记录。
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.execution import live_scope

# 同时执行的账号数：与作品发布一致，2 个窗口并行足够，再多容易被判定异常。
MAX_PARALLEL_ACCOUNTS = 2
# 与 live_scope.BATCH_MAX_PER_ACTION / 壳侧 BATCH_MAX_CAP 对齐
MAX_ACTION_CAP = 5000
# 两条线索之间的随机间隔（秒），带一点真人节奏
LEAD_GAP_S = (3.0, 8.0)
# 作用域有效期：一次批量执行最多跑 6 小时
SCOPE_HOURS = 6.0
# 单条线索最多扫多少个作品来找「首作」
FIRST_WORK_SCAN = 12

_STOP: dict[str, threading.Event] = {}
_BUSY_ACCOUNTS: set[str] = set()
_STATE_LOCK = threading.Lock()
_DB_LOCK = threading.Lock()

# UI 的「跟进方式」→ 动作标记（与打包资源里的 hf 表一一对应）
_TYPE_FLAGS: dict[str, tuple[str, ...]] = {
    "profile_first_comment": ("profile",),
    "profile_first_follow_dm": ("profile", "follow", "dm"),
    "profile_first_follow": ("profile", "follow"),
    "profile_first_dm": ("profile", "dm"),
    "follow_dm": ("follow", "dm"),
    "follow": ("follow",),
    "message": ("dm",),
    "profile_share": ("share",),
}

_PROFILE_RE = re.compile(r"^https?://www\.douyin\.com/user/([A-Za-z0-9_-]{6,160})/?$")


def profile_id(url: Any) -> str:
    """用户主页地址 → sec_uid。线索库里的 ``userUrl`` 就是这个格式。"""
    match = _PROFILE_RE.match(str(url or "").strip().split("?")[0])
    return match.group(1) if match else ""


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------- 运行态
def is_running(run_id: str) -> bool:
    with _STATE_LOCK:
        return run_id in _STOP


def stop(run_id: str) -> bool:
    """请求停止一次批量执行。已经在跑的操作会走完，未开始的线索不再领取。"""
    with _STATE_LOCK:
        event = _STOP.get(run_id)
    if not event:
        return False
    event.set()
    return True


# --------------------------------------------------------------- 文案
def _pick_line(raw: Any) -> str:
    """话术框里可以写多行，每行一条；随机取一条，避免所有人收到同一句。"""
    lines = [x.strip() for x in str(raw or "").splitlines() if x.strip()]
    return random.choice(lines) if lines else ""


def _fill(text: str, nickname: str) -> str:
    return text.replace("{nickname}", nickname or "朋友").strip()


def _persona_payload(cfg: Mapping[str, Any], account_id: str) -> dict:
    payload = dict(cfg)
    persona = (cfg.get("personaByAccount") or {}).get(account_id) or {}
    if isinstance(persona, dict):
        for key in ("personaId", "aiRole", "aiGoal", "aiStyle", "aiPrompt",
                    "firstPostGoal", "firstPostStyle", "firstPostPrompt"):
            if persona.get(key):
                payload[key] = persona[key]
    return payload


def _comment_text(db_path, cfg: Mapping[str, Any], account_id: str, *, nickname: str,
                  works_title: str) -> tuple[str, str]:
    """返回 (正文, 失败原因)。AI 模式失败时返回空正文 —— 不拿话术冒充 AI 结果。"""
    if cfg.get("profileCommentUseAi") is not False:
        from app.execution.live_leadgen import GenerationError, generate

        try:
            generated = generate(db_path, _persona_payload(cfg, account_id), {
                "title": works_title, "nickname": nickname})
        except GenerationError as exc:
            return "", f"AI 生成评论失败：{exc}"
        if not generated.get("ok"):
            return "", str(generated.get("reason") or "AI 未生成可发送的评论")
        return str(generated.get("text") or ""), ""
    text = _fill(_pick_line(cfg.get("commentTemplate")), nickname)
    if not 5 <= len(text) <= 200:
        return "", "评论话术为空或长度不在 5 到 200 字之间"
    return text, ""


def _dm_text(db_path, cfg: Mapping[str, Any], account_id: str, *, nickname: str) -> tuple[str, str]:
    if cfg.get("dmUseAi") is True:
        from app.execution.live_leadgen import GenerationError, generate

        try:
            generated = generate(db_path, {**_persona_payload(cfg, account_id),
                                           "aiGoal": cfg.get("aiGoal") or "回应用户的评论，自然交流，不留联系方式"},
                                 {"title": "私信", "nickname": nickname,
                                  "comment": str((cfg.get("commentSeed") or "")[:200])})
        except GenerationError as exc:
            return "", f"AI 生成私信失败：{exc}"
        if not generated.get("ok"):
            return "", str(generated.get("reason") or "AI 未生成可发送的私信")
        return str(generated.get("text") or ""), ""
    text = _fill(_pick_line(cfg.get("template")), nickname)
    if not 5 <= len(text) <= 200:
        return "", "私信话术为空或长度不在 5 到 200 字之间"
    return text, ""


# --------------------------------------------------------------- 作用域
def _action_caps(flags: tuple[str, ...], count: int) -> dict[str, int]:
    """把计划里的动作折算成作用域的次数上限。"""
    caps: dict[str, int] = {}
    if "profile" in flags:
        caps.update({"post-comment": count, "like-video": count, "favorite-video": count})
    if "follow" in flags:
        caps["follow-user"] = count
    if "dm" in flags:
        caps["send-dm"] = count
    if "share" in flags:
        caps["share-profile"] = count
    return {k: max(1, min(MAX_ACTION_CAP, v)) for k, v in caps.items()}


def _append_reviewed_comment(scope_file: Path, video_url: str, text: str) -> None:
    """把即将发送的首作评论写进作用域清单，再交给 perform 签名。

    壳侧只放行「作用域里列出来过的那条视频 + 那段正文」，所以顺序必须是
    先落清单、再发命令。同一账号单线程执行，不存在并发覆盖。
    """
    data = json.loads(Path(scope_file).read_text(encoding="utf-8"))
    reviewed = data.setdefault("reviewedComments", [])
    reviewed.append({"videoUrl": video_url, "text": text})
    tmp = Path(scope_file).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(scope_file)


# --------------------------------------------------------------- 数据库
def _bump(db_path, run_id: str, *, current: int = 0, success: int = 0,
          failed: int = 0, skipped: int = 0) -> None:
    with _DB_LOCK, sqlite3.connect(str(db_path), timeout=15) as con:
        con.execute("UPDATE batch_follow_runs SET current=current+?, success=success+?, "
                    "failed=failed+?, skipped=skipped+? WHERE id=?",
                    (current, success, failed, skipped, run_id))


def _item(db_path, run_id: str, lead: Mapping[str, Any], account_id: str,
          action: str, status: str, reason: str = "") -> None:
    with _DB_LOCK, sqlite3.connect(str(db_path), timeout=15) as con:
        con.execute("INSERT INTO batch_follow_items(run_id,lead_id,nickname,account_id,action,"
                    "status,reason,created_ms) VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, str(lead.get("leadId") or ""), str(lead.get("nickname") or "")[:60],
                     account_id, action[:40], status[:20], (reason or "")[:300], _now_ms()))


def _log(db_path, run_id: str, message: str, *, level: str = "normal", account: str = "") -> None:
    with _DB_LOCK, sqlite3.connect(str(db_path), timeout=15) as con:
        con.execute("INSERT INTO batch_follow_logs(run_id,ts_ms,level,account,message) "
                    "VALUES(?,?,?,?,?)", (run_id, _now_ms(), level, account[:60], message[:400]))


def _touch(db_path, *, lead_id: str, account_id: str, kind: str, nickname: str,
           detail: str = "") -> None:
    """写触达证据，让「已触达」筛选和跟进历史看到这次动作。

    账本只认这三类来源：``account_events`` 的 follow/like/profile_comment、
    ``dm_send_results`` 的 success 记录。fake 成功不会走到这里 —— 调用方只在
    真实 sent_ok 时调用。
    """
    from app.runtime import store

    try:
        if kind == "message":
            with _DB_LOCK, sqlite3.connect(str(db_path), timeout=15) as con:
                con.execute("INSERT INTO dm_send_results(id,lead_id,account_id,content,status,message,"
                            "sender_mode,created_at,retry_count,retry_status,next_retry_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (f"dm_{uuid.uuid4().hex[:12]}", lead_id, account_id, detail[:500],
                             "success", "batch_follow", "batch", _now_ms(), 0, "", 0))
            return
        action = {"follow": "follow_sent", "like": "like_sent",
                  "profileComment": "profile_comment_sent"}.get(kind)
        if action:
            store.insert_account_event(db_path, account_id=account_id, action=action,
                                       detail=f"lead_id={lead_id} nickname={nickname} 批量跟进"[:300])
    except sqlite3.Error:
        # 账本写失败不能让已经发出去的动作变成「失败」，动作结果另有 items 记录。
        pass


# --------------------------------------------------------------- 单条线索
def _first_works(db_path, account_id: str, user_url: str, cfg: Mapping[str, Any],
                 stop: threading.Event) -> dict:
    from app.runtime import browser

    result = browser.execute(
        {"type": "read-author-works", "authorUrl": user_url, "limit": FIRST_WORK_SCAN,
         "filterPinnedAuthorWorks": cfg.get("profileFirstFilterPinned") is True},
        db_path=db_path, account_id=account_id, engine="shell", timeout=180,
        stop_check=stop.is_set)
    return result if isinstance(result, dict) else {}


def _pick_first_work(result: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
    items = [x for x in (result.get("items") or []) if isinstance(x, dict) and x.get("url")]
    if not items:
        return {}
    if cfg.get("profileFirstIgnorePinnedIfOnlyOne") is True and len(items) == 1:
        return items[0]
    return items[0]


def _process_lead(db_path, run_id: str, account_id: str, lead: Mapping[str, Any],
                  cfg: Mapping[str, Any], flags: tuple[str, ...],
                  stop: threading.Event) -> tuple[str, str, dict]:
    """执行一条线索。返回 (状态, 原因, 统计)。状态取 success/failed/skipped。"""
    from app.execution import live_leadgen

    user_url = str(lead.get("userUrl") or "").strip()
    recipient = profile_id(user_url)
    if not recipient:
        return "skipped", "缺少可识别的用户主页链接（需 UID）", {}
    user_url = f"https://www.douyin.com/user/{recipient}"

    stats = {"success": 0, "failed": 0, "skipped": 0}
    done: list[str] = []
    problems: list[str] = []

    def run_action(action: str, *, video_url: str, text: str = "", nickname: str,
                   extras: dict | None = None, touch: str = "") -> bool:
        result = live_leadgen.perform(
            db_path, action, account_id=account_id, task_id=run_id, video_url=video_url,
            text=text, comment_author=nickname, recipient_id=recipient,
            extras=extras or {}, stopped=stop.is_set, run_id=run_id)
        status = str(result.get("status") or "")
        if result.get("ok") is True and status == "sent_ok":
            stats["success"] += 1
            done.append(action)
            if touch:
                _touch(db_path, lead_id=str(lead.get("leadId") or ""), account_id=account_id,
                       kind=touch, nickname=nickname, detail=text)
            return True
        if status in {"skipped", "blocked"}:
            stats["skipped"] += 1
        else:
            stats["failed"] += 1
        problems.append(f"{action}:{result.get('code') or status or 'unknown'}")
        return False

    nickname = str(lead.get("nickname") or "")
    first_work: dict = {}
    works_title = ""

    if "profile" in flags:
        scanned = _first_works(db_path, account_id, user_url, cfg, stop)
        if stop.is_set():
            return "skipped", "已停止", stats
        if scanned.get("loginWall"):
            return "failed", "抖音账号未登录（检测到登录墙）", stats
        first_work = _pick_first_work(scanned, cfg)
        if not nickname:
            nickname = str(scanned.get("author") or "").strip()
        works_title = str(first_work.get("title") or "")
        if not first_work:
            stats["skipped"] += 1
            return "skipped", "该用户主页没有可互动的作品（可能为空号或私密账号）", stats

    if not nickname:
        return "skipped", "缺少用户昵称，无法核对主页身份", stats

    # 1) 首作互动：点赞 / 收藏按概率，评论按开关
    if first_work:
        work_url = str(first_work["url"])
        if int(cfg.get("profileFirstWorkLikePercent") or 0) and \
                random.randint(1, 100) <= int(cfg.get("profileFirstWorkLikePercent") or 0):
            run_action("like-video", video_url=work_url, nickname=nickname, touch="like")
        if int(cfg.get("profileFirstWorkCollectPercent") or 0) and \
                random.randint(1, 100) <= int(cfg.get("profileFirstWorkCollectPercent") or 0):
            run_action("favorite-video", video_url=work_url, nickname=nickname)
        if cfg.get("enableProfileFirstComment") is not False and not stop.is_set():
            text, why = _comment_text(db_path, cfg, account_id, nickname=nickname,
                                      works_title=works_title)
            if not text:
                stats["failed"] += 1
                problems.append(f"post-comment:{why}")
            else:
                _append_reviewed_comment(live_scope.monitor_scope_file(run_id, account_id),
                                         work_url, text)
                run_action("post-comment", video_url=work_url, text=text,
                           nickname=nickname, touch="profileComment")

    # 2) 关注
    do_follow = "follow" in flags
    if do_follow and "profile" in flags:
        percent = int(cfg.get("profileFirstFollowPercent") or 0)
        do_follow = bool(cfg.get("enableProfileFirstFollow")) and \
            (random.randint(1, 100) <= percent)
    if do_follow and not stop.is_set():
        run_action("follow-user", video_url=user_url, nickname=nickname, touch="follow")

    # 3) 私信
    do_dm = "dm" in flags and not stop.is_set()
    if do_dm and "profile" in flags:
        percent = int(cfg.get("profileFirstDmPercent") or 100)
        do_dm = (cfg.get("enableProfileFirstDm") is not False) and \
            (random.randint(1, 100) <= percent)
    if do_dm:
        text, why = _dm_text(db_path, cfg, account_id, nickname=nickname)
        if not text:
            stats["failed"] += 1
            problems.append(f"send-dm:{why}")
        else:
            run_action("send-dm", video_url=user_url, text=text, nickname=nickname,
                       touch="message")

    # 4) 分享主页
    if "share" in flags and not stop.is_set():
        target = _share_target(cfg)
        if not target:
            stats["skipped"] += 1
            problems.append("share-profile:未填写接收好友或群聊")
        else:
            run_action("share-profile", video_url=user_url, nickname=nickname,
                       extras={"shareRecipient": target["name"],
                               "shareRecipientId": target.get("id", ""),
                               "shareRecipientKind": target.get("kind", "")})

    if stats["failed"] and not done:
        return "failed", "；".join(problems)[:280], stats
    if not done:
        return "skipped", "；".join(problems)[:280] or "没有任何动作被执行", stats
    reason = "、".join(done) + ("；部分失败：" + "；".join(problems) if problems else "")
    return "success", reason[:280], stats


def _share_target(cfg: Mapping[str, Any]) -> dict:
    """接收对象按配置顺序轮流分配，这里取一个；没有就返回空。"""
    raw = str(cfg.get("profileShareFriends") or "").strip()
    names = [x.strip() for x in raw.splitlines() if x.strip()]
    if not names:
        return {}
    name = random.choice(names)
    ids = cfg.get("shareRecipientIds") if isinstance(cfg.get("shareRecipientIds"), dict) else {}
    entry = ids.get(name) if isinstance(ids, dict) else None
    if isinstance(entry, dict) and entry.get("id"):
        return {"name": name, "id": str(entry["id"]), "kind": str(entry.get("kind") or "")}
    return {"name": name}


# --------------------------------------------------------------- 账号线程
def _account_worker(db_path, run_id: str, account_id: str, nickname: str, leads: list[dict],
                    cfg: Mapping[str, Any], flags: tuple[str, ...], stop: threading.Event,
                    success_limit: int) -> None:
    from app.runtime import browser

    try:
        scope_file = live_scope.write_batch_scope(
            run_id=run_id, account_id=account_id, actions=_action_caps(flags, len(leads)),
            hours=SCOPE_HOURS, nickname=nickname)
        _log(db_path, run_id, f"账号 {nickname or account_id} 已获得本次执行授权"
                             f"（{len(leads)} 条线索，动作 {'/'.join(flags)}）",
             account=nickname or account_id)
        ok = 0
        for lead in leads:
            if stop.is_set():
                break
            if success_limit and ok >= success_limit:
                _log(db_path, run_id, f"账号 {nickname or account_id} 已达成功上限 {success_limit} 条，转到下一个账号",
                     account=nickname or account_id)
                break
            if not browser.shell_available():
                _log(db_path, run_id, "桌面壳已断开，本次执行中止", level="error",
                     account=nickname or account_id)
                stop.set()
                break
            status, reason, stats = _process_lead(db_path, run_id, account_id, lead, cfg, flags, stop)
            ok += 1 if status == "success" else 0
            _item(db_path, run_id, lead, account_id, str(cfg.get("type") or ""), status, reason)
            _bump(db_path, run_id, current=1,
                  success=1 if status == "success" else 0,
                  failed=1 if status == "failed" else 0,
                  skipped=1 if status == "skipped" else 0)
            _log(db_path, run_id, f"{lead.get('nickname') or lead.get('leadId')}：{reason}",
                 level="normal" if status != "failed" else "error",
                 account=nickname or account_id)
            if stop.is_set():
                break
            stop.wait(random.uniform(*LEAD_GAP_S))
        try:
            Path(scope_file).unlink()
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001 线程绝不能静默死亡
        _log(db_path, run_id, f"账号 {nickname or account_id} 执行线程异常：{type(exc).__name__}: {exc}",
             level="error", account=nickname or account_id)
    finally:
        with _STATE_LOCK:
            _BUSY_ACCOUNTS.discard(account_id)


def _finish(db_path, run_id: str, status: str) -> None:
    with _DB_LOCK, sqlite3.connect(str(db_path), timeout=15) as con:
        con.execute("UPDATE batch_follow_runs SET status=?, ended_ms=? WHERE id=?",
                    (status, _now_ms(), run_id))
    with _STATE_LOCK:
        _STOP.pop(run_id, None)


def _split_leads(leads: list[dict], account_ids: list[str]) -> dict[str, list[dict]]:
    """按线索自带的 ``accountId`` 分组；没带的按顺序补到账号上。"""
    buckets: dict[str, list[dict]] = {str(a): [] for a in account_ids}
    cursor = 0
    for lead in leads:
        account = str(lead.get("accountId") or "").strip()
        if account not in buckets:
            account = str(account_ids[cursor % len(account_ids)])
            cursor += 1
        buckets[account].append(lead)
    return {k: v for k, v in buckets.items() if v}


def start(db_path, payload: Any) -> dict:
    """建立 run 记录并启动执行线程。校验失败时不写任何记录。"""
    data = payload if isinstance(payload, Mapping) else {}
    leads = [x for x in (data.get("leads") or []) if isinstance(x, dict)]
    cfg = data.get("config") if isinstance(data.get("config"), Mapping) else {}
    account_ids = [str(a) for a in (cfg.get("batchAccountIds") or []) if str(a).strip()]
    if not leads:
        return {"ok": False, "reason": "没有可跟进的线索"}
    if not account_ids:
        return {"ok": False, "reason": "请至少选择一个执行账号"}

    from app.runtime import browser
    if not browser.shell_available():
        return {"ok": False,
                "reason": "桌面壳未运行：批量跟进的动作需要在本机账号窗口里执行"}

    kind = str(cfg.get("type") or "profile_first_comment")
    flags = _TYPE_FLAGS.get(kind)
    if not flags:
        return {"ok": False, "reason": f"不支持的跟进方式：{kind}"}
    if "share" in flags and not str(cfg.get("profileShareFriends") or "").strip():
        return {"ok": False, "reason": "请填写接收主页的好友昵称、备注名或群聊名称"}

    with _DB_LOCK, sqlite3.connect(str(db_path), timeout=15) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT id,nickname FROM accounts WHERE id IN ({','.join('?' * len(account_ids))})",
            account_ids).fetchall()
    nicknames = {str(r["id"]): str(r["nickname"] or "") for r in rows}
    missing = [a for a in account_ids if not nicknames.get(a)]
    if missing:
        return {"ok": False,
                "reason": "账号信息不完整（缺少登录昵称），请先在账号管理里重新同步：" + "、".join(missing)}

    # 同名账号并行会让页面身份核对互相干扰，串行排队
    with _STATE_LOCK:
        busy = [a for a in account_ids if a in _BUSY_ACCOUNTS]
        if busy:
            return {"ok": False, "reason": "这些账号正在执行其他批量任务，请稍后重试：" + "、".join(busy)}
        _BUSY_ACCOUNTS.update(account_ids)

    run_id = f"bfr_{uuid.uuid4().hex[:12]}"
    task_id = str(data.get("taskId") or "")
    task_name = str(data.get("taskName") or cfg.get("taskName") or "立即执行")
    limits = cfg.get("accountSuccessLimits") if isinstance(cfg.get("accountSuccessLimits"), dict) else {}
    now = _now_ms()
    with _DB_LOCK, sqlite3.connect(str(db_path), timeout=15) as con:
        con.execute("INSERT INTO batch_follow_runs(id,task_id,task_name,kind,status,started_ms,"
                    "total,account_ids,selection) VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, task_id, task_name, "manual", "running", now, len(leads),
                     json.dumps(account_ids), json.dumps(cfg, ensure_ascii=False)[:20000]))

    stop_event = threading.Event()
    with _STATE_LOCK:
        _STOP[run_id] = stop_event

    buckets = _split_leads(leads, account_ids)
    queue = list(buckets.items())
    # 账号数多于并行上限时排队：一个线程空出来再领下一个账号
    semaphore = threading.Semaphore(MAX_PARALLEL_ACCOUNTS)

    def account_thread(account_id: str, account_leads: list[dict]) -> None:
        with semaphore:
            limit = limits.get(account_id) if isinstance(limits, dict) else 0
            try:
                limit = int(limit or 0)
            except (TypeError, ValueError):
                limit = 0
            _account_worker(db_path, run_id, account_id, nicknames.get(account_id, ""),
                            account_leads, cfg, flags, stop_event, limit)

    def supervise() -> None:
        threads = [threading.Thread(target=account_thread, args=item, daemon=True,
                                    name=f"batch-{item[0][:8]}") for item in queue]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        _finish(db_path, run_id, "stopped" if stop_event.is_set() else "completed")

    threading.Thread(target=supervise, daemon=True, name=f"batch-run-{run_id}").start()
    return {"ok": True, "runId": run_id, "total": len(leads), "accounts": len(queue)}
