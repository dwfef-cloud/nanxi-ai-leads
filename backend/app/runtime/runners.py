"""三类真机运行时：线索采集 / 监控 / 养号。

一条共同原则
------------
**能跑出真实数据才算跑过**。所以每个 runner 的收尾统计只统计真正落库的东西：
新增线索数、命中数、真实浏览轮次；壳没在线、未登录、登录墙、读到 0 条评论，
都按失败或空结果落库并在日志里写清原因——绝不把「准备执行」写成「已采集」。

产物写哪里（页面本来就在读的表，不改前端契约）
----------------------------------------------
- 线索采集 → leads（source_task_id=任务 id，source='entity_comment'）
- 监控     → leads（source='monitor_comment'）+ runtime_cycles（每轮巡检）
- 养号     → account_events（action=watch / like）+ runtime_cycles
"""
from __future__ import annotations

import hashlib
import fnmatch
import random
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import collector, comment_tree, leadgen_pools
from app.api import event_bus, task_logs, task_store
from app.execution import leadgen_config, live_scope
from app.execution.params import FILTER
from app.execution.rules import classify
from app.runtime import browser, monitor_engine, store

EVENT_CHANNEL = {
    "entity": "entity-leadgen-task-event",
    "monitor": "monitor-task-event",
    "warmup": "self-warmup-task-event",
}

# 风控下限：监控巡检间隔不低于 60s（比这更密就是在刷平台接口）
MIN_MONITOR_INTERVAL_S = 60
# 单轮读评论上限
MAX_COMMENTS = 120


# --------------------------------------------------------------------------
# 上下文与事件
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    kind: str
    db_path: Path
    task_id: str
    name: str
    account_id: str
    run_id: str
    trigger: str = "manual"
    # 本次运行钉住的执行引擎（shell / playwright）：中途不换引擎，否则同一轮
    # 任务会一半跑在用户已登录的壳里、一半跑在独立浏览器里（见 browser.execute）。
    engine: str = ""
    stop: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)
    stats: dict = field(default_factory=dict)
    cycles: int = 0

    def bump(self, **kw: int) -> None:
        for key, val in kw.items():
            self.stats[key] = int(self.stats.get(key, 0)) + int(val)


def _fail(ctx: Ctx, message: str) -> None:
    """记一次失败：落日志、计数、把原因带到本次执行的结束说明里。

    只 log 不记原因的话，运行记录会变成「失败但看不出为什么」——
    用户只能去翻几十行日志找第一处报错。
    """
    log(ctx, message, "error")
    ctx.stats["failed"] = int(ctx.stats.get("failed", 0)) + 1
    ctx.stats["lastError"] = message[:200]


def _evt(ctx: Ctx, kind: str, **payload: Any) -> None:
    channel = EVENT_CHANNEL.get(ctx.kind)
    if not channel:
        return
    msg = dict(payload)
    msg.update({"type": kind, "taskId": ctx.task_id, "accountId": ctx.account_id,
                "ts": int(time.time() * 1000)})
    try:
        event_bus.publish(channel, msg)
    except Exception:  # noqa: BLE001 推送失败不影响执行
        pass


def _nurture_event(ctx: Ctx, kind: str, **payload: Any) -> None:
    """养号专属事件：形状必须是 `{type, payload:{...}}`。

    页面（automation-data 的订阅者）只认这种嵌套形状：它按 `y.type` 分支、
    再从 `y.payload` 取字段。以前后端这段时间只发 `self-warmup-task-event`，
    页面的 `nurture-progress` 分支永远收不到 → 账号卡片一直停在启动时写死的
    「养号初始化...」，5 分钟养号看起来就像卡死（用户 2026-11-14 反馈）。
    进度、剩余时长按轮次推真实值，不猜。
    """
    try:
        event_bus.publish("automation-data", {
            "type": kind,
            "taskId": ctx.task_id,
            "payload": {**payload, "accountId": ctx.account_id, "taskId": ctx.task_id},
        })
    except Exception:  # noqa: BLE001 推送失败不影响执行
        pass


def log(ctx: Ctx, message: str, level: str = "normal") -> None:
    """写运行日志：落库（刷新可回看）+ 双通道推送（抽屉 / 大屏）。"""
    stamp = int(time.time() * 1000)
    task_logs.append(ctx.db_path, ctx.task_id, ctx.task_id, ctx.account_id,
                     message, level, stamp)
    try:
        event_bus.publish("automation-data", {
            "type": "trace-log", "taskId": ctx.task_id,
            "payload": {"accountId": ctx.account_id, "message": message, "level": level}})
    except Exception:  # noqa: BLE001
        pass
    _evt(ctx, "log", message=message, level=level)


# --------------------------------------------------------------------------
# 配置归一（壳的字段名会随页面改动，这里容忍多种写法）
# --------------------------------------------------------------------------
def _pick(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) not in (None, "", [], {}):
            return mapping[key]
    return default


def _str_list(value: Any, limit: int = 12) -> list[str]:
    out: list[str] = []
    if isinstance(value, (list, tuple)):
        seq = value
    elif isinstance(value, str):
        seq = re.split(r"[,，\n;；\s]+", value)
    else:
        seq = []
    for item in seq:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        num = default
    return max(lo, min(num, hi))


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(lo, min(num, hi))


def _pct(value: Any, default: float) -> float:
    """百分比归一：页面填 10（%）→ 0.10。缺省用 default（已是 0-1）。"""
    if value in (None, ""):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    # 页面可能存 0-100 也可能存 0-1：>1 一律按百分数理解
    if num > 1:
        num = num / 100.0
    return max(0.0, min(num, 1.0))


def _pair(cfg: dict, lo_keys: tuple[str, ...], hi_keys: tuple[str, ...],
          default_lo: float, default_hi: float, lo: float, hi: float) -> tuple[float, float]:
    """区间配置：页面填「最小-最大」，读不到就回落默认；顺序反了就交换。"""
    low = _num(_pick(cfg, *lo_keys), default_lo, lo, hi)
    high = _num(_pick(cfg, *hi_keys), max(default_hi, low), lo, hi)
    if high < low:
        low, high = high, low
    return low, high


def task_config(rec: dict) -> dict:
    cfg = rec.get("configSnapshot")
    if not isinstance(cfg, dict):
        cfg = rec.get("config") if isinstance(rec.get("config"), dict) else {}
    return cfg or {}


def run_auto_reply(db_path, rec: dict, ctx: "Ctx") -> None:
    """自动回复运行时入口。

    真正的循环在 app/runtime/autoreply.py：读私信/作品评论 → 研判 → 发送。
    这里只做转发，保持 registry 的 runner 表整齐（一个 kind 一个函数）。
    """
    from app.runtime import autoreply

    autoreply.run(db_path, rec, ctx)


def is_auto_reply_config(cfg: dict) -> bool:
    """识别自动回复页面配置，防止被 warmup runner 当成养号任务执行。

    自动回复任务目前也保存在 warmup 表中，但字段形状不同。只有明确的
    自动回应开关或私信规则字段才视为自动回复，避免误判旧养号配置。
    """
    if not isinstance(cfg, dict):
        return False
    mode = str(cfg.get("taskMode") or "").strip().lower().replace("_", "-")
    if mode in {"auto-reply", "autoreply", "reply"}:
        return True
    if "enableAutoActions" in cfg:
        return True
    return "dmReplyMode" in cfg and any(
        key in cfg for key in ("watchMessages", "watchComments", "dmReplyRules")
    )


def resolve_account(db_path, rec: dict, cfg: dict) -> str:
    """任务归属账号：优先任务自身记录，其次唯一活跃账号。"""
    direct = str(_pick(cfg, "accountId", "account", default="") or "").strip()
    if direct:
        return direct
    accounts = _pick(cfg, "accounts", "selectedAccounts", default=[])
    if isinstance(accounts, list):
        for item in accounts:
            if isinstance(item, dict):
                value = str(item.get("id") or item.get("accountId") or "").strip()
            else:
                value = str(item or "").strip()
            if value:
                return value
    task_level = str(rec.get("accountId") or "").strip()
    if task_level:
        return task_level
    try:
        con = sqlite3.connect(str(db_path), timeout=10)
        try:
            row = con.execute(
                "SELECT id FROM accounts WHERE status='active' ORDER BY created_at LIMIT 1"
            ).fetchone()
        finally:
            con.close()
        if row:
            return str(row[0])
    except sqlite3.Error:
        pass

    return ""


def _threshold() -> int:
    try:
        return int(FILTER.intent_threshold)
    except Exception:  # noqa: BLE001
        return 60


def _level(score: int, threshold: int) -> str:
    if score >= 75:
        return "A"
    if score >= threshold:
        return "B"
    return "C"


def _lead_key(video_id: str, nickname: str, text: str) -> str:
    return "ld_" + hashlib.sha1(
        f"{video_id}|{nickname}|{text}".encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 线索采集
# --------------------------------------------------------------------------
def run_entity(db_path, rec: dict, ctx: Ctx) -> None:
    cfg = task_config(rec)
    # 吞评检测（常规互检）也是 entity 任务：分流给专属引擎，避免落进
    # 「线索采集没有可执行目标」——那正是此前模块完全不可用的原因。
    from app.runtime import visibility as _visibility

    if _visibility.is_visibility_config(cfg):
        _visibility.run_visibility(ctx, rec, cfg)
        return
    keywords = _str_list(_pick(cfg, "keywords", "keywordList", "keyword",
                               default=rec.get("keywords")), limit=8)
    video_urls = _str_list(_pick(cfg, "specifiedUrls", "videoUrls", "specificVideos", "videos"), limit=20)
    author_urls = _str_list(_pick(cfg, "bloggerProfileUrls", "authorUrls", "bloggers", "bloggerUrls"), limit=8)
    sources = _str_list(cfg.get("sourceTypes"))
    if sources:
        keywords = keywords if "video_search" in sources else []
        video_urls = video_urls if "video_specific" in sources else []
        author_urls = author_urls if "blogger" in sources or "author_specific" in sources else []
    # 上限必须和设置页/配置层同口径（leadgen_config.MAX_VIDEOS=200）。
    # 2026-11-19 之前这里硬写 20：页面能填 200、壳侧按 200 抓，到了执行器被静默砍成 20，
    # 用户看到的就是「填了 60 只跑 20，日志也不说为什么」。
    video_max = _int(_pick(cfg, "videoMax", "videosPerKeyword"), 6, 1, leadgen_config.MAX_VIDEOS)
    comment_limit = _int(_pick(cfg, "maxCommentsPerVideo", "commentLimit", "commentsPerVideo"), 40, 4, 60)
    threshold = _threshold()

    if not (keywords or video_urls or author_urls):
        log(ctx, "线索采集没有可执行目标：请在任务里填关键词、视频链接或博主主页", "warning")
        ctx.stats["reason"] = "no-targets"
        _finish(ctx, "entity")
        return

    log(ctx, f"开始线索采集：关键词 {('、'.join(keywords) or '无')}，"
             f"指定视频 {len(video_urls)} 条，博主 {len(author_urls)} 个，"
             f"每个关键词最多 {video_max} 条视频", "normal")

    targets: list[dict] = [{"type": "keyword", "value": kw} for kw in keywords]
    targets += [{"type": "video", "value": u} for u in video_urls]
    targets += [{"type": "author", "value": u} for u in author_urls]

    for idx, target in enumerate(targets, 1):
        if ctx.stop.is_set():
            log(ctx, "已手动停止：剩余目标未采集", "warning")
            ctx.stats["stopped"] = 1
            break
        ctx.cycles += 1
        cycle_id = store.cycle_begin(db_path, ctx.run_id, "entity", ctx.task_id, idx)
        max_collect = _int(cfg.get("maxCollect"), 0, 0, 100000)
        remaining = max_collect - int(ctx.stats.get("leads", 0)) if max_collect else 0
        if max_collect and remaining <= 0:
            break
        cycle_stats = _entity_target(ctx, target, video_max, comment_limit, threshold, cfg, remaining)
        status = "ok" if cycle_stats["videos"] or cycle_stats["comments"] else "empty"
        store.cycle_end(db_path, cycle_id, status,
                        f"{target['type']}:{target['value'][:60]}", cycle_stats)
        _evt(ctx, "stats", stats=dict(ctx.stats))

    _finish(ctx, "entity")


def _entity_target(ctx: Ctx, target: dict, video_max: int, comment_limit: int,
                   threshold: int, filters: dict | None = None, remaining: int = 0) -> dict:
    stats = {"videos": 0, "comments": 0, "leads": 0}
    urls: list[str] = []
    keyword = ""
    if target["type"] == "keyword":
        keyword = target["value"]
        log(ctx, f"搜索关键词「{keyword}」（最多 {video_max} 条视频）")
        res = browser.execute({"type": "search-videos", "keyword": keyword,
                               "limit": video_max},
                              db_path=ctx.db_path,
                              log=lambda m, l="normal": log(ctx, m, l),
                              stop_check=ctx.stop.is_set,
                              account_id=ctx.account_id, engine=ctx.engine)
        if not res.get("ok"):
            _fail(ctx, f"关键词「{keyword}」搜索失败：{res.get('error') or '未知原因'}")
            return stats
        items = res.get("items") or []
        stats["videos"] = len(items)
        ctx.bump(videos=len(items))
        log(ctx, f"关键词「{keyword}」找到 {len(items)} 条视频"
                 + ("（含作者信息）" if any(i.get("author") for i in items) else ""))
        urls = [str(i.get("url") or "") for i in items if i.get("url")][:video_max]
        # 作者本身也是线索：先把「谁在发」记下来
        for item in items:
            author = str(item.get("author") or "").strip()
            if not author:
                continue
            lid = _lead_key(str(item.get("videoId") or ""), author, str(item.get("title") or ""))
            created = store.insert_lead(
                ctx.db_path, lead_id=lid, nickname=author,
                comment=str(item.get("title") or "")[:300],
                keyword=keyword, source="entity_author", task_id=ctx.task_id,
                video_url=str(item.get("url") or ""), video_id=str(item.get("videoId") or ""),
                account=ctx.account_id, status="collected")
            if created:
                stats["leads"] += 1
    elif target["type"] == "author":
        log(ctx, f"读取博主主页作品：{target['value'][:80]}")
        res = browser.execute({"type": "read-author-works", "authorUrls": [target["value"]],
                               "limit": video_max},
                              db_path=ctx.db_path,
                              log=lambda m, l="normal": log(ctx, m, l),
                              stop_check=ctx.stop.is_set,
                              account_id=ctx.account_id, engine=ctx.engine)
        if not res.get("ok"):
            _fail(ctx, f"博主主页读取失败：{res.get('error') or '未知原因'}")
            return stats
        items = res.get("items") or []
        stats["videos"] = len(items)
        ctx.bump(videos=len(items))
        urls = [str(i.get("url") or "") for i in items if i.get("url")][:video_max]
        log(ctx, f"博主 {res.get('author') or target['value'][:40]} 最近 {len(items)} 条作品")
    else:
        urls = [target["value"]]
        # A video selected from a previous keyword search retains that proven
        # origin. Hidden keyword fields in the new form are never provenance.
        try:
            with sqlite3.connect(str(ctx.db_path)) as con:
                origins = con.execute("SELECT DISTINCT keyword FROM live_action_results "
                                      "WHERE action='search-video' AND status='selected' "
                                      "AND url=? AND keyword!=''", (target["value"],)).fetchall()
            if len(origins) == 1:
                keyword = origins[0][0]
        except sqlite3.Error:
            pass

    if not urls:
        return stats
    # filters 在这里就是整份任务配置（调用方按位置把 cfg 传进来的），
    # 「采集二级评论」开关一直存在，但这条链路从来没传过 —— 开了等于没开。
    command = {"type": "read-comments", "videoUrls": urls, "limit": comment_limit}
    if (filters or {}).get("expandSubComments") is True:
        command["expandSubComments"] = True
    res = browser.execute(command,
                          db_path=ctx.db_path,
                          log=lambda m, l="normal": log(ctx, m, l),
                          stop_check=ctx.stop.is_set,
                              account_id=ctx.account_id, engine=ctx.engine)
    if not res.get("ok"):
        _fail(ctx, f"读取评论失败：{res.get('error') or '未知原因'}")
        return stats
    comments = res.get("comments") or []
    stats["comments"] = len(comments)
    ctx.bump(comments=len(comments))
    if not comments:
        log(ctx, f"这一批 {len(urls)} 条视频没读到评论：{res.get('note') or '评论区为空'}", "warning")
        return stats

    filters = filters or {}
    def accepted(c):
        text, name = str(c.get("text") or ""), str(c.get("nickname") or "")
        include = _str_list(filters.get("commentIncludeKeywords"))
        exclude = _str_list(filters.get("commentExcludeKeywords"))
        names = _str_list(filters.get("commentIncludeNicknames"))
        excluded_names = _str_list(filters.get("commentExcludeNicknames"))
        return ((not include or any(x in text for x in include)) and
                not any(x in text for x in exclude) and
                (not names or any(fnmatch.fnmatchcase(name, x) for x in names)) and
                not any(fnmatch.fnmatchcase(name, x) for x in excluded_names))
    eligible = [c for c in comments if accepted(c)]
    ctx.bump(filtered=len(comments)-len(eligible))
    comments = eligible[:remaining] if remaining else eligible
    ingest = collector.ingest_comments(ctx.db_path, comments, keyword or "线索采集",
                                       task_id=ctx.task_id, account=ctx.account_id)
    stats["leads"] += ingest["inserted"]
    ctx.bump(leads=ingest["inserted"], duplicated=ingest["duplicated"])
    log(ctx, f"入库：新增 {ingest['inserted']} 条，重复跳过 {ingest['duplicated']} 条")

    for c in comments:
        if ctx.stop.is_set():
            break
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        verdict = classify(text)
        if verdict.is_noise:
            ctx.bump(noise=1)
            continue
        if verdict.base_score >= threshold:
            ctx.bump(hit=1)
            collector.promote(ctx.db_path, collector.lead_id_of(c),
                              min(100, verdict.base_score),
                              _level(verdict.base_score, threshold), list(verdict.tags))
    return stats


# --------------------------------------------------------------------------
# 监控
# --------------------------------------------------------------------------
def run_monitor(db_path, rec: dict, ctx: Ctx) -> None:
    cfg = task_config(rec)
    eng = monitor_engine
    targets = eng.target_types(cfg)
    video_urls = eng.valid_urls(cfg, "videoUrls")
    # 视频分享短链（v.douyin.com/xxx）：跟随重定向归一成 /video/<id>，短链不解析
    # 壳里打开会被抖音拦成验证页。失败原样保留，不阻塞任务启动。
    if video_urls:
        resolved_videos: list[str] = []
        short_resolved = 0
        for u in video_urls:
            rv = eng.resolve_video_url(u)
            if rv != u:
                short_resolved += 1
            resolved_videos.append(rv)
        if short_resolved:
            log(ctx, f"已解析 {short_resolved} 条视频分享短链为真实视频地址", "normal")
        video_urls = resolved_videos
    author_urls = eng.valid_urls(cfg, "authorUrls")
    live_targets = eng.live_targets(cfg)
    live_urls = [t["url"] for t in live_targets]
    # 监控主播直播（author_live）：liveAuthorUrls 存主播主页链接，
    # 开播检测在壳里做（打开主页找直播中入口），这里只透传。
    author_live_urls = [t["url"] for t in eng.author_live_targets(cfg)]
    accounts = eng.accounts(cfg) or [ctx.account_id]
    if ctx.account_id and ctx.account_id not in accounts:
        accounts.insert(0, ctx.account_id)
    keywords = _str_list(_pick(cfg, "keywords", "keywordList", "matchKeywords"), limit=12)
    author_work_limit = _int(_pick(cfg, "maxAuthorWorksPerCheck", "authorWorksLimit"),
                             12, 1, 20)
    interval_fixed = _int(_pick(cfg, "intervalSec", "checkIntervalSec", "monitorIntervalSec"),
                          600, MIN_MONITOR_INTERVAL_S, 6 * 3600)
    # 页面设置的是区间（intervalSecondsMin/Max「检查间隔 20-3600 秒随机」），
    # 只认单值会让用户设的区间失效 —— 每轮在区间内重新掷一次。
    interval_lo, interval_hi = _pair(
        cfg, ("intervalSecondsMin", "checkIntervalSecondsMin", "intervalMin"),
        ("intervalSecondsMax", "checkIntervalSecondsMax", "intervalMax"),
        interval_fixed, interval_fixed, MIN_MONITOR_INTERVAL_S, 6 * 3600)
    max_cycles = _int(_pick(cfg, "maxCycles", "cycles"), 0, 0, 240)  # 0 = 不设上限
    if max_cycles == 1:
        # 任务配置里存了「只跑 1 轮」时，任务会在一轮后自己结束；
        # 不说明的话用户会以为「监控自己停了」。
        log(ctx, "本次只跑 1 轮就结束（任务配置 maxCycles=1）；需要持续监控请把循环轮数改为 0（不限）",
            "warning")
    comment_limit = _int(_pick(cfg, "maxCommentsPerVideo", "commentLimit", "limit"),
                         40, 4, MAX_COMMENTS)
    limits = {k: eng.limit_of(cfg, k) for k in ("like", "reply", "follow", "dm")}
    allowed_actions = eng.scopes_for(cfg)

    # 监控群成员（group_members）：壳负责打开消息页读完整成员名单，这里入库/互动
    group_names = eng.group_targets(cfg)

    skipped_links = (eng.invalid_count(cfg, "videoUrls")
                     + eng.invalid_count(cfg, "authorUrls"))
    if skipped_links:
        log(ctx, f"已跳过 {skipped_links} 个标记失效的监控链接", "normal")

    if not (video_urls or author_urls or keywords or live_urls or author_live_urls
            or group_names):
        log(ctx, "监控任务没有目标：请先选择要监控的视频 / 主播 / 直播 / 关键词", "warning")
        ctx.stats["reason"] = "no-targets"
        _finish(ctx, "monitor")
        return

    if live_urls or author_live_urls:
        # 分享文案里贴的短链要先解析成真实直播间，否则壳里打开只能是空壳。
        resolved = []
        for item in live_targets:
            try:
                resolved.append(eng.resolve_live_url(item["url"]))
            except Exception:  # noqa: BLE001 解析失败保留原样，壳会自己跟随跳转
                resolved.append(item["url"])
        live_urls = list(dict.fromkeys(resolved))
        log(ctx, "直播监控目标：" + "、".join(eng.room_label(u) for u in live_urls)
            + (f"；另监控 {len(author_live_urls)} 位主播（开播后自动采集）"
               if author_live_urls else ""), "normal")
        if allowed_actions:
            live_actions = [k for k in ("like-live", "follow-live", "lottery-live")
                            if k in allowed_actions]
            log(ctx, "直播动作已开启：" + ("、".join(
                {"like-live": "直播间点赞", "follow-live": "关注主播",
                 "lottery-live": "参与抽奖"}[k] for k in live_actions) or "无（只采集）")
                + "；命中用户还会按配置关注/私信", "normal")
        else:
            log(ctx, "直播监控只采集，不执行任何写动作（任务里未开启自动动作）", "normal")

    log(ctx, f"开始监控：视频 {len(video_urls)} 条、主播 {len(author_urls)} 个、"
             f"直播 {len(live_urls)} 个、待检测主播 {len(author_live_urls)} 个、"
             f"关键词 {('、'.join(keywords) or '全部评论')}、群 {len(group_names)} 个；账号 {len(accounts)} 个；"
             f"每轮间隔 {interval_lo:g}-{interval_hi:g}s（下限 {MIN_MONITOR_INTERVAL_S}s）", "normal")
    if allowed_actions:
        log(ctx, "自动动作已开启：" + "、".join(sorted(allowed_actions))
                 + f"；单账号每日上限 {limits}", "normal")

    cycle_no = 0
    while not ctx.stop.is_set():
        cycle_no += 1
        ctx.cycles = cycle_no
        cid = store.cycle_begin(db_path, ctx.run_id, "monitor", ctx.task_id, cycle_no)
        stats = {"videos": 0, "read": 0, "new": 0, "matched": 0,
                 "filtered": 0, "aiFailed": 0, "actions": 0, "actionFailed": 0}
        failure = ""

        # 监控作用域按轮续期：长任务不会因为 12 小时过期而突然发不出动作。
        if allowed_actions:
            for account_id in accounts:
                try:
                    live_scope.write_monitor_scope(
                        task_id=ctx.task_id, account_id=account_id,
                        actions=allowed_actions, hours=12.0,
                        nickname=_account_nickname(ctx.db_path, account_id))
                except (OSError, ValueError) as exc:
                    log(ctx, f"监控作用域写入失败（{account_id}）：{type(exc).__name__}", "warning")

        for account_id in accounts:
            if ctx.stop.is_set():
                break
            # 主账号由启动方持租约；其余账号按轮短租，避免长租泄漏把账号锁死。
            lent = False
            if account_id != ctx.account_id:
                from app.runtime import registry as _registry

                lent = _registry.claim_account(account_id, f"monitor:{ctx.task_id}")
                if not lent:
                    log(ctx, f"账号 {account_id} 正在执行其他任务，本轮跳过", "warning")
                    continue
            try:
                urls = list(video_urls)
                if author_urls:
                    res = browser.execute({"type": "read-author-works", "authorUrls": author_urls,
                                           "limit": author_work_limit,
                                           "filterPinnedAuthorWorks": cfg.get("filterPinnedAuthorWorks") is True}, db_path=db_path,
                                          log=lambda m, l="normal": log(ctx, m, l),
                                          stop_check=ctx.stop.is_set,
                                          account_id=account_id, engine=ctx.engine)
                    if res.get("ok"):
                        urls += [str(i.get("url") or "") for i in (res.get("items") or [])
                                 if i.get("url")]
                    else:
                        failure = str(res.get("error") or "读取主播作品失败")
                        log(ctx, f"第 {cycle_no} 轮（{account_id}）：{failure}", "warning")
                if keywords and not urls:
                    # 只有关键词时按关键词搜索（等价于监听这个词下面的新评论）
                    for kw in keywords[:3]:
                        res = browser.execute({"type": "search-videos", "keyword": kw, "limit": 4},
                                              db_path=db_path,
                                              log=lambda m, l="normal": log(ctx, m, l),
                                              stop_check=ctx.stop.is_set,
                                              account_id=account_id, engine=ctx.engine)
                        if res.get("ok"):
                            urls += [str(i.get("url") or "") for i in (res.get("items") or [])
                                     if i.get("url")]
                        else:
                            failure = str(res.get("error") or "搜索失败")
                            log(ctx, f"第 {cycle_no} 轮搜索「{kw}」失败：{failure}", "warning")

                urls = list(dict.fromkeys(urls))[:12]
                stats["videos"] = max(stats["videos"], len(urls))
                if urls:
                    res = browser.execute({"type": "read-comments", "videoUrls": urls,
                                           "limit": comment_limit,
                                           "commentWindowMinutes": _int(cfg.get("commentWindowMinutes"), 0, 0, 4320),
                                           "expandSubComments": cfg.get("expandSubComments") is True}, db_path=db_path,
                                          log=lambda m, l="normal": log(ctx, m, l),
                                          stop_check=ctx.stop.is_set,
                                          account_id=account_id, engine=ctx.engine)
                    if res.get("ok"):
                        comments = res.get("comments") or []
                        stats["read"] += len(comments)
                        part = _match_comments(ctx, comments, keywords, res.get("note"), cfg,
                                               account_id=account_id, limits=limits)
                        for key in ("new", "matched", "filtered", "aiFailed", "actions", "actionFailed"):
                            stats[key] += part.get(key, 0)
                        if cfg.get("enableVideoComment") is True:
                            posted = _video_comment_pass(ctx, cfg, account_id, urls)
                            stats["actions"] += posted["ok"]
                            stats["actionFailed"] += posted["failed"]
                    else:
                        failure = str(res.get("error") or "读取评论失败")
                        log(ctx, f"第 {cycle_no} 轮（{account_id}）读取评论失败：{failure}", "error")
                if live_urls or author_live_urls:
                    live_part, live_failure = _live_pass(
                        ctx, cfg, account_id, live_urls, keywords, limits, cycle_no,
                        author_live_urls=author_live_urls)
                    for key in ("read", "new", "matched", "filtered", "aiFailed",
                                "actions", "actionFailed"):
                        stats[key] += live_part.get(key, 0)
                    if live_failure and not failure:
                        failure = live_failure
                if group_names:
                    group_part, group_failure = _group_pass(
                        ctx, cfg, account_id, group_names, keywords, limits)
                    for key in ("read", "new", "matched", "filtered", "aiFailed",
                                "actions", "actionFailed"):
                        stats[key] += group_part.get(key, 0)
                    if group_failure and not failure:
                        failure = group_failure
            finally:
                if lent:
                    from app.runtime import registry as _registry2

                    _registry2.release_account(account_id, f"monitor:{ctx.task_id}")

        ctx.bump(cycles=1, read=stats["read"], matched=stats["matched"],
                 new=stats["new"], filtered=stats["filtered"], aiFailed=stats["aiFailed"],
                 actions=stats["actions"], actionFailed=stats["actionFailed"])
        store.cycle_end(db_path, cid,
                        "failed" if (failure and not stats["read"]) else "ok",
                        failure or f"读到 {stats['read']} 条，新增 {stats['new']} 条，"
                                   f"命中 {stats['matched']} 条，动作 {stats['actions']} 个", stats)
        _evt(ctx, "stats", stats=dict(ctx.stats))
        _persist_progress(ctx)

        if max_cycles and cycle_no >= max_cycles:
            log(ctx, f"已跑满 {max_cycles} 轮，按配置结束", "normal")
            break
        # 等下一轮（区间内随机）；期间响应停止
        interval = int(round(random.uniform(interval_lo, interval_hi)))
        log(ctx, f"下一轮检查间隔：{interval}s", "normal")
        waited = 0
        while waited < interval and not ctx.stop.is_set():
            time.sleep(min(1.0, interval - waited))
            waited += 1

    if allowed_actions:
        for account_id in accounts:
            live_scope.clear_monitor_scope(ctx.task_id, account_id)
    _finish(ctx, "monitor")


def _video_comment_pass(ctx: Ctx, cfg: dict, account_id: str, urls: list[str]) -> dict:
    """自动发表视频主评：对当前监控的视频本身发一条公开评论。

    同一视频只发一次 —— 由 live_action_results 的 shared-main 记录与写安全门禁
    兜底，这里不另建去重表，避免两套去重逻辑互相打架。
    """
    out = {"ok": 0, "failed": 0}
    if not monitor_engine.scopes_for(cfg).get("post-comment"):
        return out
    from app.execution.live_leadgen import perform

    for url in urls[:3]:
        if ctx.stop.is_set():
            break
        drafted = monitor_engine.video_comment_text(ctx.db_path, cfg, account_id,
                                                    {"title": ""})
        if not drafted.get("ok"):
            out["failed"] += 1
            log(ctx, f"视频主评未发送（{url}）：{drafted.get('reason') or drafted.get('code')}",
                "warning")
            continue
        result = perform(ctx.db_path, "post-comment", account_id=account_id,
                         task_id=ctx.task_id, video_url=url, text=drafted["text"],
                         run_id=ctx.run_id, stopped=ctx.stop.is_set)
        if result.get("ok"):
            out["ok"] += 1
            log(ctx, f"已发表视频主评：{drafted['text'][:30]}")
        elif result.get("code") == "other_account_history":
            log(ctx, f"该视频已有主评记录，本轮跳过（{url}）", "normal")
        else:
            out["failed"] += 1
            log(ctx, f"视频主评未成功（{url}）：{result.get('code') or result.get('error')}",
                "warning")
    return out


def _group_pass(ctx: Ctx, cfg: dict, account_id: str, group_names: list[str],
                keywords: list[str], limits: dict) -> tuple[dict, str]:
    """群成员监控：读完整成员名单，按排除项过滤后当线索入库/互动。

    成员没有评论文本，text 用「[群成员] 昵称」占位 —— 关键词匹配等于按昵称匹配，
    AI 研判看到的也是昵称；去重维度 = 群名:昵称（同一群同一人只入库一次）。
    直播里同款约束：弹幕/成员定位按昵称走，点赞评论/回复评论不适用。
    """
    out = {"read": 0, "new": 0, "matched": 0, "filtered": 0, "aiFailed": 0,
           "actions": 0, "actionFailed": 0}
    res = browser.execute({"type": "read-group-members", "groupNames": group_names},
                          db_path=ctx.db_path,
                          log=lambda m, l="normal": log(ctx, m, l),
                          stop_check=ctx.stop.is_set,
                          account_id=account_id, engine=ctx.engine)
    if not res.get("ok"):
        failure = str(res.get("error") or "读取群成员失败")
        log(ctx, f"群成员读取失败：{failure}", "error")
        return out, failure

    exclude_owner = cfg.get("excludeGroupOwner") is True
    exclude_admins = cfg.get("excludeGroupAdmins") is True
    comments: list[dict] = []
    for g in res.get("groups") or []:
        gname = str(g.get("name") or "")
        if not g.get("found"):
            log(ctx, f"群「{gname}」未找到：请确认账号已加入该群（群名需与消息页一致）", "warning")
            continue
        members = g.get("members") or []
        kept = 0
        seen: set[str] = set()
        for m in members:
            role = str(m.get("role") or "member")
            if exclude_owner and role == "owner":
                continue
            if exclude_admins and role in ("owner", "admin"):
                continue
            nick = str(m.get("nickname") or "").strip()
            if not nick or nick in seen:   # 壳层已去重，这里防御重复成员
                continue
            seen.add(nick)
            kept += 1
            comments.append({
                "text": f"[群成员] {nick}",
                "nickname": nick,
                "videoId": f"{gname}:{nick}",       # 去重维度 = 群:成员
                "videoUrl": "",
                "caption": f"群聊 {gname}",
                "createTime": "",
                "commentId": f"group:{gname}:{nick}",
            })
        log(ctx, f"群「{gname}」：{len(members)} 名成员，过滤后 {kept} 名"
                 + ("（已排除群主/管理员）" if (exclude_owner or exclude_admins) else "")
                 + ("（同名成员已合并）" if kept < len(members)
                    and not (exclude_owner or exclude_admins) else ""),
            "normal")
    if comments:
        part = _match_comments(ctx, comments, keywords, "monitor_group", cfg,
                               account_id=account_id, limits=limits,
                               allow_actions=True, source="monitor_group",
                               skip_action_types={"like-comment", "reply-comment"})
        for key in ("new", "matched", "filtered", "aiFailed", "actions", "actionFailed"):
            out[key] += part.get(key, 0)
    return out, ""


def _parse_fans(text: Any) -> int | None:
    """「1.2万」→ 12000；「8000」→ 8000；读不出来返回 None（不猜）。"""
    match = re.search(r"([\d.]+)\s*([万亿wW]?)", str(text or "").strip())
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    unit = match.group(2).lower()
    if unit == "万":
        value *= 10000
    elif unit == "亿":
        value *= 100000000
    elif unit == "w":
        value *= 10000
    return int(value)


def _fan_tier(count: int | None) -> str | None:
    """UI 档位：1=1000以下 2=1000-1w 3=1w-10w 4=10w-100w 5=100w以上。"""
    if count is None:
        return None
    if count < 1000:
        return "1"
    if count < 10000:
        return "2"
    if count < 100000:
        return "3"
    if count < 1000000:
        return "4"
    return "5"


def _live_user_gate(ctx: "Ctx", cfg: dict, account_id: str, cache: dict,
                    uid: str, nick: str, account_label: str) -> bool | None:
    """建单侧「用户类型 / 粉丝数量」筛选的核对。

    弹幕行本身不带这两样信息，只能按弹幕行给的 uid 打开用户主页核对
    （inspect-profile 同一条只读通道，粉丝数读资料区文本、认证读徽章）。
    返回 True=保留 / False=不符过滤 / None=核对不了（保留，不悄悄丢人）。
    每个 uid 一轮只核对一次；没配筛选时完全不访问主页，零开销。
    """
    type_wanted = str(cfg.get("userTypeFilter") or "0").strip()
    fan_tier = str(cfg.get("userFanCount") or "0").strip()
    if type_wanted in ("", "0") and fan_tier in ("", "0"):
        return True
    if not uid or not account_label:
        return None
    if uid in cache:
        return cache[uid]
    res = browser.execute(
        {"type": "inspect-profile", "recipientId": uid, "commentAuthor": nick,
         "nickname": account_label},
        db_path=ctx.db_path, account_id=account_id, engine=ctx.engine,
        log=lambda m, l="normal": log(ctx, m, l), stop_check=ctx.stop.is_set)
    item = (res.get("item") or {}) if res.get("ok") else {}
    fans = _parse_fans(item.get("fansText"))
    verdict = True
    if fan_tier not in ("", "0"):
        tier = _fan_tier(fans)
        if tier is None:
            # 主页没读出粉丝数：核对不了。宁多勿漏——保留用户，但让日志说清楚
            return None
        verdict = tier == fan_tier
    if verdict and type_wanted == "1":
        verdict = not (item.get("enterprise") or item.get("personal"))
    elif verdict and type_wanted == "2":
        verdict = bool(item.get("enterprise"))
    elif verdict and type_wanted == "3":
        verdict = bool(item.get("personal"))
    cache[uid] = verdict
    return verdict


def _live_pass(ctx: Ctx, cfg: dict, account_id: str, live_urls: list[str],
               keywords: list[str], limits: dict, cycle_no: int,
               author_live_urls: list[str] | None = None) -> tuple[dict, str]:
    """直播监控：读聊天区消息，命中关键词/意向写入线索库，并按配置执行动作。

    动作分两层：
      * 房间级（点赞 / 关注主播 / 参与抽奖）：每轮每个房间各执行一次，对房间本身操作；
      * 评论级（关注观众 / 私信观众）：只对命中的弹幕执行，目标由弹幕昵称现场定位。
    直播里不执行「点赞评论」和「回复评论」——弹幕列表是虚拟滚动，按条定位不可靠。
    """
    out = {"read": 0, "new": 0, "matched": 0, "filtered": 0, "aiFailed": 0,
           "actions": 0, "actionFailed": 0}

    # 「同时监听数」（liveConcurrency）：每轮只开 N 个房间，超出部分排队轮转，
    # 保证所有目标最终都会被监听到，而不是每轮都全量打开。
    pool = list(live_urls) + list(author_live_urls or [])
    conc = max(0, int(cfg.get("liveConcurrency") or 0))
    urls_this_cycle = pool
    if conc and len(pool) > conc:
        start = ((cycle_no - 1) * conc) % len(pool)
        urls_this_cycle = [pool[(start + i) % len(pool)] for i in range(conc)]
        skipped = [u for u in pool if u not in urls_this_cycle]
        log(ctx, f"同时监听数上限 {conc}：本轮监听 {len(urls_this_cycle)}/{len(pool)} 个房间，"
                 f"其余 {len(skipped)} 个排队（后续轮次轮转覆盖）", "normal")
        for u in skipped:  # 排队目标写进明细，页面能看到「开播排队」
            store.live_target_upsert(
                ctx.db_path, task_id=ctx.task_id, target_key=u,
                type_="author_live" if u in set(author_live_urls or []) else "live",
                state="queued", reason="排队：超出同时监听数，后续轮次轮转")

    this_cycle_live = [u for u in urls_this_cycle if u not in set(author_live_urls or [])]
    this_cycle_author = [u for u in urls_this_cycle if u in set(author_live_urls or [])]
    res = browser.execute({"type": "read-live-events", "liveUrls": this_cycle_live,
                           "authorUrls": this_cycle_author},
                          db_path=ctx.db_path,
                          log=lambda m, l="normal": log(ctx, m, l),
                          stop_check=ctx.stop.is_set,
                          account_id=account_id, engine=ctx.engine)
    if not res.get("ok"):
        failure = str(res.get("error") or "读取直播事件失败")
        log(ctx, f"第 {cycle_no} 轮（{account_id}）直播读取失败：{failure}", "error")
        return out, failure

    wanted = {str(x) for x in (cfg.get("liveActionEventTypes") or [])
              if str(x).strip()} or {"enter", "like", "follow", "comment", "gift"}
    # 「采集事件」（liveEventTypes）：进一步限定监听哪些事件类型；为空=全部
    event_filter = {str(x) for x in (cfg.get("liveEventTypes") or []) if str(x).strip()}
    if event_filter:
        wanted = wanted & event_filter if wanted else event_filter
        log(ctx, "按配置只监听事件类型：" + "、".join(sorted(event_filter)), "normal")
    account_name = _account_nickname(ctx.db_path, account_id)
    # 用户类型/粉丝数筛选：只有配置了才逐人开主页核对（inspect-profile，每 uid 缓存一次）
    gate_active = (str(cfg.get("userTypeFilter") or "0").strip() not in ("", "0")
                   or str(cfg.get("userFanCount") or "0").strip() not in ("", "0"))
    gate_cache: dict[str, bool] = {}
    gate_warned = False
    comments: list[dict] = []
    counters: dict[str, int] = {}
    for room in res.get("rooms") or []:
        if ctx.stop.is_set():
            break
        url = str(room.get("finalUrl") or room.get("url") or "")
        label = monitor_engine.room_label(url)
        status = str(room.get("status") or "")
        is_author = url in set(author_live_urls or [])
        # 房间真实状态回写明细页：listening=监听中 / ended=已下播 /
        # queued=主播未开播（排队等开播） / retry=需要登录等可重试问题
        state_map = {"live": ("listening", ""), "unknown": ("listening", ""),
                     "ended": ("ended", "已下播"),
                     "not_live": ("queued", "主播未开播，等待开播后自动采集"),
                     "login_required": ("retry", "需要登录才能看聊天")}
        now_ms = int(time.time() * 1000)
        state, reason = state_map.get(status, ("retry", f"状态未知（{status or '无'}）"))
        store.live_target_upsert(
            ctx.db_path, task_id=ctx.task_id, target_key=url,
            type_="author_live" if is_author else "live",
            name=str(room.get("nickname") or room.get("title") or "") or monitor_engine.room_label(url),
            account_id=account_id, account_name=account_name,
            state=state, reason=reason, last_check_ms=now_ms)
        if status == "ended":
            log(ctx, f"{label} 已下播：本轮不采集", "normal")
            continue
        if status == "not_live":
            log(ctx, f"{label} 主播未开播：本轮不采集（下轮检查间隔由任务配置决定）", "normal")
            continue
        if status == "login_required":
            log(ctx, f"{label} 需要登录才能看聊天：请在该账号窗口登录后重试", "warning")
            store.live_target_upsert(ctx.db_path, task_id=ctx.task_id, target_key=url,
                                     state="retry", reason="需要登录才能看聊天")
            continue
        if status and status not in {"live", "unknown"}:
            log(ctx, f"{label} 状态未知（{status}），本轮跳过", "warning")
            continue
        room_comments = 0
        room_events = 0
        for ev in room.get("events") or []:
            kind = str(ev.get("kind") or "comment")
            if kind not in wanted:
                continue
            room_events += 1
            counters[kind] = counters.get(kind, 0) + 1
            nick = str(ev.get("nickname") or "")
            text = str(ev.get("content") or ev.get("text") or "").strip()
            # 事件明细入库：弹幕先记 pending，研判结果稍后回写；其余类型直接落 outcome=signal
            eid = store.live_event_insert(
                ctx.db_path, task_id=ctx.task_id, target_key=url, kind=kind,
                nickname=nick or "未知用户", content=text, account_id=account_id,
                outcome="signal" if kind != "comment" else "pending")
            if kind != "comment":
                continue
            if not text:
                store.live_event_set_outcome(ctx.db_path, eid, "filtered", note="弹幕内容为空")
                continue
            if gate_active:
                verdict = _live_user_gate(ctx, cfg, account_id, gate_cache,
                                          str(ev.get("uid") or ""), nick, account_name)
                if verdict is False:
                    store.live_event_set_outcome(ctx.db_path, eid, "filtered",
                                                 note="用户类型/粉丝数不符")
                    continue
                if verdict is None and not gate_warned:
                    gate_warned = True
                    log(ctx, "弹幕行没有用户标识（或主页核对失败），用户类型/粉丝数筛选"
                             "核对不了的用户先保留，不悄悄丢弃", "warn")
            room_comments += 1
            comments.append({
                "text": text,
                "nickname": nick or "未知用户",
                "videoId": url,                      # 直播用房间地址做去重维度
                "videoUrl": url,
                "caption": str(room.get("title") or ""),
                "createTime": str(ev.get("at") or ""),
                "commentId": str(ev.get("key") or ""),
                "_eventId": eid,
            })
        store.live_target_upsert(ctx.db_path, task_id=ctx.task_id, target_key=url,
                                 events_delta=room_events)
        log(ctx, f"{label}：在播，聊天区读到 {room_comments} 条消息"
                 + ("" if room_comments else "（直播间没人说话时这里就是 0，不是采集失败）"), "normal")
        # 房间级动作：点赞 / 关注主播 / 参与抽奖（每轮每房间各一次）
        room_part = _live_room_actions(ctx, cfg, account_id, url, label, limits)
        out["actions"] += room_part["ok"]
        out["actionFailed"] += room_part["failed"]
    if counters:
        log(ctx, "直播事件：" + "、".join(f"{k} {v}" for k, v in counters.items()), "normal")
    out["read"] = len(comments)
    part = _match_comments(ctx, comments, keywords, res.get("note"), cfg,
                           account_id=account_id, limits=limits,
                           allow_actions=True, source="monitor_live",
                           skip_action_types={"like-comment", "reply-comment"},
                           track_events=True)
    for key in ("new", "matched", "filtered", "aiFailed", "actions", "actionFailed"):
        out[key] += part.get(key, 0)
    store.live_events_prune(ctx.db_path, ctx.task_id)   # 明细保留最近 2 万条
    return out, ""


LIVE_ROOM_ACTION_LABELS = {"like-live": "直播间点赞", "follow-live": "关注主播",
                           "lottery-live": "参与直播间抽奖"}


def _account_nickname(db_path, account_id: str) -> str:
    """账号昵称：壳用它核对「当前登录的确实是这个账号」再落动作。"""
    try:
        with sqlite3.connect(str(db_path), timeout=10) as con:
            row = con.execute("SELECT nickname, name FROM accounts WHERE id=?",
                              (account_id,)).fetchone()
    except sqlite3.Error:
        return ""
    if not row:
        return ""
    return str(row[0] or row[1] or "").strip()


def _live_room_actions(ctx: Ctx, cfg: dict, account_id: str, live_url: str,
                       label: str, limits: dict) -> dict:
    """对直播间本身执行动作：点赞 / 关注主播 / 参与抽奖。"""
    out = {"ok": 0, "failed": 0}
    allowed = monitor_engine.scopes_for(cfg)
    from app.execution.live_leadgen import perform

    for action in ("like-live", "follow-live", "lottery-live"):
        if action not in allowed or ctx.stop.is_set():
            continue
        kind = {"like-live": "like", "follow-live": "follow", "lottery-live": "like"}[action]
        if not monitor_engine.quota_take(ctx.db_path, account_id, action,
                                        limits.get(kind, 0), cfg):
            log(ctx, f"{label} 跳过{LIVE_ROOM_ACTION_LABELS[action]}：单账号当日额度已用完", "normal")
            continue
        times = allowed.get("like-live", 0) if action == "like-live" else 0
        result = perform(ctx.db_path, action, account_id=account_id, task_id=ctx.task_id,
                         # 点赞次数走签名里的 text 字段（正文字段在房间级动作里不表示文本）
                         video_url=live_url, text=(str(times) if times else ""),
                         run_id=ctx.run_id, stopped=ctx.stop.is_set)
        if result.get("ok"):
            out["ok"] += 1
            note = f"（{result.get('times')} 次）" if result.get("times") else ""
            log(ctx, f"{label} {LIVE_ROOM_ACTION_LABELS[action]}成功{note}")
        elif result.get("code") in {"already_followed", "no_lottery_running", "already_done"}:
            log(ctx, f"{label} {LIVE_ROOM_ACTION_LABELS[action]}跳过：{result.get('error') or result.get('code')}",
                "normal")
        else:
            out["failed"] += 1
            log(ctx, f"{label} {LIVE_ROOM_ACTION_LABELS[action]}未成功："
                     f"{result.get('error') or result.get('code')}", "warning")
    return out


def _match_comments(ctx: Ctx, comments: list[dict], keywords: list[str],
                    note: str | None, cfg: dict | None = None, *,
                    account_id: str = "", limits: dict | None = None,
                    allow_actions: bool = True,
                    source: str = "monitor_comment",
                    skip_action_types: set[str] | None = None,
                    track_events: bool = False) -> dict:
    """把新读到的评论里符合关键词的落成线索（已读过的直接跳过）。

    allow_actions=False 时只落库不执行任何写动作；skip_action_types 用来剔除
    当前场景做不了的动作（例如直播弹幕点不了赞、回复不了某一条）。
    """
    cfg = cfg or {}
    account_id = account_id or ctx.account_id
    limits = limits or {}
    out = {"new": 0, "matched": 0, "filtered": 0, "aiFailed": 0,
           "actions": 0, "actionFailed": 0}
    if not comments:
        if note:
            log(ctx, f"本轮没读到评论：{note}", "warning")
        return out
    # 开了「采集二级评论」之后，数组里会混进楼中楼回复。标上 kind / parentCommentId
    # 再往下走：动作层靠 commentId 定位，日志和线索也能看出这是回复不是主评。
    comments = comment_tree.annotate(comments)
    threshold = _threshold()
    excluded_names = {name.casefold() for name in _str_list(
        _pick(cfg, "excludeCommenters", "excludedCommenters"), limit=200)}
    excluded_keywords = _str_list(
        _pick(cfg, "excludeCommentKeywords", "excludedCommentKeywords"), limit=200)
    regions = _str_list(_pick(cfg, "locationFilterRegions", "regions"), limit=100)
    location_mode = str(_pick(cfg, "locationFilterMode", default="include") or "include").lower()
    use_ai = cfg.get("useAiJudge") is True or cfg.get("useAiJudge") in (1, "true")
    # 「仅根据评论研判」：不给视频标题，避免模型拿视频主题去推断需求
    comment_only = cfg.get("aiJudgeCommentOnly") is True
    ai_payload = monitor_engine.persona_payload(ctx.db_path, cfg, account_id)
    for c in comments:
        if ctx.stop.is_set():
            break
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        vid = str(c.get("videoId") or "")
        nick = str(c.get("nickname") or "未知用户")
        key = f"{vid}|{nick}|{text}"
        if store.seen_has(ctx.db_path, ctx.task_id, key):
            if track_events:
                store.live_event_set_outcome(ctx.db_path, int(c.get("_eventId") or 0),
                                             "dedup", note="本轮已读过该弹幕")
            continue
        store.seen_mark(ctx.db_path, ctx.task_id, [key])
        out["new"] += 1
        location = str(c.get("ipLocation") or c.get("location") or "").strip()
        excluded = (nick.casefold() in excluded_names
                    or any(word in text for word in excluded_keywords))
        if regions:
            in_region = any(region in location for region in regions)
            excluded = excluded or (location_mode == "exclude" and in_region)
            excluded = excluded or (location_mode != "exclude" and not in_region)
        if excluded:
            out["filtered"] += 1
            if track_events:
                store.live_event_set_outcome(ctx.db_path, int(c.get("_eventId") or 0),
                                             "filtered", note="命中排除项（昵称/关键词/地区）")
            continue
        if keywords and not any(kw.casefold() in text.casefold() for kw in keywords):
            out["filtered"] += 1
            if track_events:
                store.live_event_set_outcome(ctx.db_path, int(c.get("_eventId") or 0),
                                             "filtered", note="未命中监控关键词")
            continue
        # 优先命中关键词：包含即判高意向，不调用 AI（页面承诺的行为）
        hit_word = monitor_engine.priority_hit(text, cfg)
        ai_reason = ""
        if use_ai and not hit_word:
            try:
                from app.execution.live_leadgen import classify_intent

                judgment = classify_intent(ctx.db_path, ai_payload, {
                    "nickname": nick, "text": text,
                    "title": "" if comment_only else str(
                        c.get("caption") or c.get("videoTitle") or "")[:300],
                })
            except Exception as exc:  # noqa: BLE001 AI 不可用时按不匹配处理
                out["aiFailed"] += 1
                out["filtered"] += 1
                if track_events:
                    store.live_event_set_outcome(ctx.db_path, int(c.get("_eventId") or 0),
                                                 "ai_failed", note=f"AI 研判失败：{type(exc).__name__}")
                log(ctx, f"@{nick} AI 研判失败，未进入线索：{type(exc).__name__}", "warning")
                continue
            if not judgment.get("matched"):
                out["filtered"] += 1
                if track_events:
                    store.live_event_set_outcome(ctx.db_path, int(c.get("_eventId") or 0),
                                                 "filtered",
                                                 note=f"AI 判定不匹配：{str(judgment.get('reason') or '')[:80]}")
                continue
            ai_reason = str(judgment.get("reason") or "")[:100]
        elif hit_word:
            ai_reason = f"优先关键词「{hit_word}」直接命中"
        out["matched"] += 1
        verdict = classify(text)
        score = (50 if verdict.is_noise else min(100, verdict.base_score)) if (use_ai or hit_word) else (
            0 if verdict.is_noise else min(100, verdict.base_score))
        hit_kw = (hit_word or next((kw for kw in keywords if kw in text),
                                   keywords[0] if keywords else "监控"))
        lead_id = _lead_key(vid, nick, text)
        created = store.insert_lead(
            ctx.db_path, lead_id=lead_id, nickname=nick, comment=text,
            keyword=hit_kw, source=source, task_id=ctx.task_id,
            video_url=str(c.get("videoUrl") or ""), video_id=vid,
            comment_id=str(c.get("commentId") or ""),
            comment_user_id=str(c.get("userId") or ""),
            comment_time=str(c.get("createTime") or c.get("publishedAt") or ""),
            account=account_id, score=score, level=_level(score, threshold),
            status="collected")
        if created:
            log(ctx, f"命中「{hit_kw}」@{nick}：{text[:40]}"
                     + (f"（AI：{ai_reason}）" if ai_reason else f"（意向 {score} 分）" if score else ""))
        if track_events:
            # 目标计数：created=新增线索，否则同一条线索被再次命中（已读过分支挡住，
            # 这里防御性计入 updated）；明细回写放动作执行后，把动作结果一起带上
            store.live_target_bump(ctx.db_path, task_id=ctx.task_id, target_key=vid,
                                   added=1 if created else 0,
                                   updated=0 if created else 1, matched=1)
        if allow_actions:
            acted = _run_actions(ctx, cfg, account_id, c, limits,
                                 skip=skip_action_types or set())
            out["actions"] += acted["ok"]
            out["actionFailed"] += acted["failed"]
            if track_events:
                summary = ""
                if acted["ok"] or acted["failed"]:
                    summary = f"动作 {acted['ok']} 成功 / {acted['failed']} 失败"
                store.live_event_set_outcome(ctx.db_path, int(c.get("_eventId") or 0),
                                             "matched", keyword=hit_kw, note=ai_reason,
                                             actions=summary)
        if monitor_engine._b(cfg, "enableWebhook"):
            push = monitor_engine.notify(cfg, {
                "nickname": nick, "comment": text, "score": score,
                "videoUrl": str(c.get("videoUrl") or "")}, ctx.name)
            if push.get("ok"):
                log(ctx, f"命中已推送到{'钉钉' if cfg.get('webhookType') == 'dingtalk' else '飞书'}")
            else:
                log(ctx, f"命中推送失败：{push.get('code')}"
                         + (f"（{push.get('error')}）" if push.get("error") else ""), "warning")
    log(ctx, f"本轮读到 {len(comments)} 条评论，新增 {out['new']} 条，命中 {out['matched']} 条，"
             f"执行动作 {out['actions']} 个")
    return out


def _gender_age_check(profile: dict, *, gender: str, age_enabled: bool,
                      age_min: int, age_max: int, age_allow_unknown: bool) -> str:
    """性别/年龄判定：返回空串 = 通过，否则为拦截原因。

    gender 取值域与页面选项一致：all / male / female / male_unknown / female_unknown
    （male_unknown = 男或未知都通过；female = 只女，未知不通过）。
    """
    if gender not in ("", "all"):
        pg = profile.get("gender")
        if gender.endswith("_unknown"):
            allowed = pg is None or str(pg) == gender.removesuffix("_unknown")
        else:
            allowed = str(pg) == gender
        if not allowed:
            return f"性别不符合（配置 {gender}，主页 {pg or '未知'}）"
    if age_enabled:
        age = profile.get("age")
        if type(age) is not int:
            if not age_allow_unknown:
                return "年龄未知（未勾选「允许未知年龄」）"
        elif not age_min <= age <= age_max:
            return f"年龄 {age} 不在 {age_min}-{age_max} 区间"
    return ""


def _inspect_profile_cached(ctx: Ctx, account_id: str, uid: str,
                            nickname: str, cache: dict) -> dict:
    """读对方主页资料（inspect-profile 只读探针），同一 uid 一个运行里只开一次。"""
    if uid not in cache:
        res = browser.execute({"type": "inspect-profile", "recipientId": uid,
                               "commentAuthor": nickname},
                              db_path=ctx.db_path, account_id=account_id,
                              log=lambda m, l="normal": log(ctx, m, l),
                              stop_check=ctx.stop.is_set)
        cache[uid] = (res or {}).get("item") or {}
    return cache[uid]


def _profile_action_gate(ctx: Ctx, cfg: dict, account_id: str, uid: str,
                         nickname: str, cache: dict) -> tuple[bool, str]:
    """主页筛选（性别 / 年龄 / 异常年龄）：关注/私信动作前开主页核对资料。

    页面口径：「分享前进入目标用户主页读取资料；未知性别不通过」。没配任何
    筛选时直接放行（不开主页，零开销）；读不到主页资料按未知处理 —— 性别档
    不含 unknown 或年龄未勾「允许未知」就拦下（宁可不互动不乱互动）。
    """
    gender = str(_pick(cfg, "profileActionGenderFilter") or "all").strip().lower()
    age_enabled = cfg.get("profileActionAgeFilterEnabled") is True
    if gender in ("", "all") and not age_enabled:
        return True, ""
    if not uid:
        return False, "该线索没有用户主页标识，无法核对资料"
    profile = _inspect_profile_cached(ctx, account_id, uid, nickname, cache)
    why = _gender_age_check(
        profile, gender=gender, age_enabled=age_enabled,
        age_min=_int(_pick(cfg, "profileActionAgeMin"), 0, 0, 120),
        age_max=_int(_pick(cfg, "profileActionAgeMax"), 120, 0, 120),
        age_allow_unknown=cfg.get("profileActionAgeAllowUnknown") is True)
    return (not why, why)


def _run_actions(ctx: Ctx, cfg: dict, account_id: str, comment: dict,
                 limits: dict, skip: set[str] | None = None) -> dict:
    """把一条命中评论变成真实动作。每个动作都过 perform 的签名与写安全门禁。"""
    out = {"ok": 0, "failed": 0}
    if not monitor_engine.scopes_for(cfg):
        return out
    intents = monitor_engine.plan(cfg, account_id, comment, db_path=ctx.db_path, limits=limits)
    if skip:
        intents = [i for i in intents if i.type not in skip]
        if intents:
            log(ctx, f"直播场景不支持 {'、'.join(sorted(skip))}，已跳过", "normal")
    if not intents:
        return out
    from app.execution.live_leadgen import perform

    nick = str(comment.get("nickname") or "")
    video_url = str(comment.get("videoUrl") or "")
    comment_id = str(comment.get("commentId") or "")
    user_id = str(comment.get("userId") or "")
    text = str(comment.get("text") or "")
    for intent in intents:
        if ctx.stop.is_set():
            break
        kind = {"like-comment": "like", "reply-comment": "reply",
                "follow-user": "follow", "send-dm": "dm"}[intent.type]
        if not monitor_engine.quota_take(ctx.db_path, account_id, intent.type,
                                         limits.get(kind, 0), cfg):
            log(ctx, f"@{nick} 跳过{intent.reason}：单账号当日额度已用完", "normal")
            continue
        # 主页筛选（性别/年龄/异常年龄）：只拦需要进主页核对的动作（关注/私信）。
        # 缓存挂在 ctx 上，同一轮里同一个 uid 只开一次主页。
        if intent.type in {"follow-user", "send-dm"}:
            cache = getattr(ctx, "_profile_cache", None)
            if cache is None:
                cache = ctx._profile_cache = {}
            gate_ok, gate_why = _profile_action_gate(ctx, cfg, account_id, user_id,
                                                     nick, cache)
            if not gate_ok:
                log(ctx, f"@{nick} 跳过{intent.reason}：{gate_why}", "warning")
                continue
        if intent.type == "reply-comment":
            drafted = monitor_engine.reply_text(ctx.db_path, cfg, account_id, comment)
            if not drafted.get("ok"):
                out["failed"] += 1
                log(ctx, f"@{nick} 回复未发送：{drafted.get('reason') or drafted.get('code')}", "warning")
                continue
            result = perform(ctx.db_path, "reply-comment", account_id=account_id,
                             task_id=ctx.task_id, video_url=video_url, text=drafted["text"],
                             comment_id=comment_id, comment_author=nick,
                             recipient_id=user_id, run_id=ctx.run_id,
                             stopped=ctx.stop.is_set)
        elif intent.type == "send-dm":
            drafted = monitor_engine.dm_draft(ctx.db_path, cfg, account_id, comment)
            texts = drafted.get("texts") or [] if drafted.get("ok") else []
            if not texts:
                out["failed"] += 1
                log(ctx, f"@{nick} 私信未发送：{drafted.get('reason') or drafted.get('code') or '私信模板为空'}",
                    "warning")
                continue
            result = {"ok": False}
            for index, body in enumerate(texts):
                if ctx.stop.is_set():
                    break
                result = perform(ctx.db_path, "send-dm", account_id=account_id,
                                 task_id=ctx.task_id, video_url=video_url, text=body,
                                 comment_author=nick,
                                 recipient_id=user_id, run_id=ctx.run_id,
                                 stopped=ctx.stop.is_set)
                if not result.get("ok"):
                    break
                if index + 1 < len(texts):
                    wait = random.uniform(5, 10)  # 页面承诺：多条私信之间随机等 5-10 秒
                    deadline = time.time() + wait
                    while time.time() < deadline and not ctx.stop.is_set():
                        time.sleep(0.5)
        elif intent.type == "like-comment":
            result = perform(ctx.db_path, "like-comment", account_id=account_id,
                             task_id=ctx.task_id, video_url=video_url, text=text,
                             comment_id=comment_id, comment_author=nick, run_id=ctx.run_id,
                             stopped=ctx.stop.is_set)
        else:  # follow-user
            result = perform(ctx.db_path, "follow-user", account_id=account_id,
                             task_id=ctx.task_id, video_url=video_url, text="",
                             # 直播间弹幕没有用户 id，壳按昵称在聊天区定位到人再关注
                             comment_author=nick,
                             recipient_id=user_id, run_id=ctx.run_id,
                             stopped=ctx.stop.is_set)
        if result.get("ok"):
            out["ok"] += 1
            log(ctx, f"@{nick} {intent.reason}成功")
        else:
            out["failed"] += 1
            log(ctx, f"@{nick} {intent.reason}未成功：{result.get('code') or result.get('error')}",
                "warning")
        # 动作之间随机等待（页面默认 300-600 秒），避免连续操作同一账号
        if intent is not intents[-1]:
            delay = monitor_engine.action_delay(cfg)
            deadline = time.time() + delay
            while time.time() < deadline and not ctx.stop.is_set():
                time.sleep(0.5)
    # 首作互动：评论命中后对作者主页首个公开作品做点赞/收藏/评论（同一作者
    # 每个运行只做一次，函数内部去重）。放在常规动作之后，主互动优先。
    try:
        monitor_profile_first(ctx, cfg, account_id, comment)
    except Exception as exc:  # noqa: BLE001 首作互动失败不影响主动作结论
        log(ctx, f"@{nick} 首作互动异常：{type(exc).__name__}: {exc}", "warning")
    return out


def monitor_profile_first(ctx: Ctx, cfg: dict, account_id: str, comment: dict) -> None:
    """首作互动（自动动作）：进入评论作者主页，对首个公开作品点赞/收藏/评论。

    页面口径：「进入对方主页，对首个公开作品评论、点赞、收藏」。同一运行里
    每个作者只互动一次；「过滤置顶」默认关（页面默认值如此），开了才跳过置顶，
    只有一个作品且勾了「仅一个作品时忽略置顶」就用它；评论前勾了
    「先随机浏览」就先打开同主页另一作品停留 5-10 秒（watch-video）。
    评论内容走「自动发表视频主评」同一套文案配置（模板 / AI）。
    """
    uid = str(comment.get("userId") or "").strip()
    nick = str(comment.get("nickname") or "")
    done: set[str] = getattr(ctx, "_first_work_done", None) or set()
    ctx._first_work_done = done
    if not uid or uid in done:
        return
    like_p = _pct(_pick(cfg, "profileFirstWorkLikePercent"), 0.0)
    collect_p = _pct(_pick(cfg, "profileFirstWorkCollectPercent"), 0.0)
    comment_on = (cfg.get("commentOnProfileFirstWork") is True
                  and cfg.get("enableProfileFirstComment") is True)
    if like_p <= 0 and collect_p <= 0 and not comment_on:
        return
    if cfg.get("commentOnProfileFirstWork") is not True:
        return  # 总开关不开，概率配置也不生效（页面「首作互动」开关组）
    done.add(uid)
    from app.execution.live_leadgen import perform

    profile_url = f"https://www.douyin.com/user/{uid}"
    works_res = browser.execute({"type": "read-author-works", "authorUrl": profile_url,
                                 "limit": 12}, db_path=ctx.db_path,
                                account_id=account_id,
                                log=lambda m, l="normal": log(ctx, m, l),
                                stop_check=ctx.stop.is_set, engine=ctx.engine)
    works = [w for w in ((works_res or {}).get("items") or [])
             if isinstance(w, dict) and str(w.get("url") or "").strip()]
    if not works:
        log(ctx, f"@{nick} 首作互动：主页未读到可互动作品", "warning")
        return
    if cfg.get("profileFirstFilterPinned") is True:
        first = next((w for w in works if not w.get("pinned")), None)
    else:
        first = works[0]
    if first is None and cfg.get("profileFirstIgnorePinnedIfOnlyOne") is True and len(works) == 1:
        first = works[0]
    if first is None:
        log(ctx, f"@{nick} 首作互动：首个作品是置顶且已开启「过滤置顶」，未执行")
        return
    first_url = str(first["url"]).strip()
    # 主页首作也有自己的性别/年龄筛选（profileFirst*，与「关注/私信/分享」的
    # profileAction* 是两组独立开关）；核不过就不对该主页做任何首作动作。
    gate_ok, gate_why = _profile_first_gate(ctx, cfg, account_id, uid, nick)
    if not gate_ok:
        log(ctx, f"@{nick} 首作互动跳过：{gate_why}", "warning")
        return
    ok_n = 0
    total = 0
    if random.random() < like_p:
        total += 1
        r = perform(ctx.db_path, "like-video", account_id=account_id,
                    task_id=ctx.task_id, video_url=first_url, text="",
                    comment_author=nick, recipient_id=uid, run_id=ctx.run_id,
                    stopped=ctx.stop.is_set)
        if r.get("ok"):
            ok_n += 1
        else:
            log(ctx, f"@{nick} 首作点赞未成功：{r.get('code') or r.get('error')}", "warning")
    if random.random() < collect_p:
        total += 1
        r = perform(ctx.db_path, "favorite-video", account_id=account_id,
                    task_id=ctx.task_id, video_url=first_url, text="",
                    comment_author=nick, recipient_id=uid, run_id=ctx.run_id,
                    stopped=ctx.stop.is_set)
        if r.get("ok"):
            ok_n += 1
        else:
            log(ctx, f"@{nick} 首作收藏未成功：{r.get('code') or r.get('error')}", "warning")
    if comment_on:
        siblings = [str(w.get("url")).strip() for w in works
                    if str(w.get("url") or "").strip() != first_url]
        if cfg.get("profileFirstRandomBrowseBeforeComment") is True and siblings:
            watch = browser.execute({"type": "watch-video", "videoUrl": random.choice(siblings),
                                     "seconds": random.randint(5, 10), "runId": ctx.task_id},
                                    db_path=ctx.db_path, account_id=account_id,
                                    log=lambda m, l="normal": log(ctx, m, l),
                                    stop_check=ctx.stop.is_set, engine=ctx.engine)
            if not (watch or {}).get("ok"):
                log(ctx, f"@{nick} 首作评论前随机浏览未完成，继续评论", "warning")
        drafted = monitor_engine.video_comment_text(ctx.db_path, cfg, account_id,
                                                    {"title": str(first.get("title") or "")[:300]})
        if not drafted.get("ok"):
            log(ctx, f"@{nick} 首作评论未发送：{drafted.get('reason') or drafted.get('code')}",
                "warning")
        else:
            total += 1
            r = perform(ctx.db_path, "post-comment", account_id=account_id,
                        task_id=ctx.task_id, video_url=first_url,
                        text=str(drafted.get("text") or "")[:500],
                        comment_author=nick, recipient_id=uid, run_id=ctx.run_id,
                        stopped=ctx.stop.is_set)
            if r.get("ok"):
                ok_n += 1
            else:
                log(ctx, f"@{nick} 首作评论未成功：{r.get('code') or r.get('error')}", "warning")
    if total:
        log(ctx, f"@{nick} 首作互动完成：{ok_n}/{total} 项成功"
                 f"（{first_url.rsplit('/', 1)[-1][:16]}）")


def _profile_first_gate(ctx: Ctx, cfg: dict, account_id: str, uid: str,
                        nickname: str) -> tuple[bool, str]:
    """首作互动自己的主页筛选（profileFirstGenderFilter / profileFirstAgeFilter*）。

    与「关注 / 私信 / 分享」的 profileAction* 是两组独立开关，取值域一致。
    没配就放行（不开主页）。
    """
    gender = str(_pick(cfg, "profileFirstGenderFilter") or "all").strip().lower()
    age_enabled = cfg.get("profileFirstAgeFilterEnabled") is True
    if gender in ("", "all") and not age_enabled:
        return True, ""
    cache: dict = getattr(ctx, "_profile_cache", None) or {}
    ctx._profile_cache = cache
    profile = _inspect_profile_cached(ctx, account_id, uid, nickname, cache)
    why = _gender_age_check(
        profile, gender=gender, age_enabled=age_enabled,
        age_min=_int(_pick(cfg, "profileFirstAgeMin"), 0, 0, 120),
        age_max=_int(_pick(cfg, "profileFirstAgeMax"), 120, 0, 120),
        age_allow_unknown=cfg.get("profileFirstAgeAllowUnknown") is True)
    return (not why, why)


# --------------------------------------------------------------------------
# 养号
# --------------------------------------------------------------------------
def _warmup_budget_s(dur_lo: float, dur_hi: float) -> int:
    """本轮养号的时长预算：在页面设置的区间里随机取一个分钟数。"""
    return int(random.uniform(dur_lo, dur_hi) * 60)


def _warmup_scrolls_left(scrolls: int, dwell_ms: int, remaining_s: float) -> int:
    # 为页面加载和可能的一次详情点赞留 25 秒，避免完整一轮跨过截止时间。
    return min(scrolls, max(0, int((remaining_s - 25) / (dwell_ms / 1000 + 0.6))))


def run_warmup(db_path, rec: dict, ctx: Ctx) -> None:
    """养号：按**页面设置**跑推荐流浏览，时长/停留/点赞概率全部以配置为准。

    页面（AccountNurture 启动的 start-task）传的是扁平字段：
        nurtureDurationMin / nurtureDurationMax   —— 养号时长（分钟）
        stayMin / stayMax                        —— 单个视频停留（秒）
        nurtureLikePercent / CollectPercent / SharePercent —— 互动概率（%）
    历史实现只认 cycles/scrolls/dwellMs/likeMax（脚本造任务的写法），
    结果页面上设 60-120 分钟、点赞 10%，实际只跑 1 轮 6 屏、0 点赞 —— 设置等于没生效。
    现在两套键都认，页面字段优先。
    """
    cfg = task_config(rec)

    dur_lo, dur_hi = _pair(cfg, ("nurtureDurationMin", "durationMin", "runMinutesMin"),
                           ("nurtureDurationMax", "durationMax", "runMinutesMax"),
                           60, 120, 1, 480)
    stay_lo, stay_hi = _pair(cfg, ("stayMin", "dwellMinS"), ("stayMax", "dwellMaxS"),
                             8, 25, 1, 60)
    like_p = _pct(_pick(cfg, "nurtureLikePercent", "likePercent"), 0.10)
    collect_p = _pct(_pick(cfg, "nurtureCollectPercent", "collectPercent"), 0.10)
    share_p = _pct(_pick(cfg, "nurtureSharePercent", "sharePercent"), 0.10)
    scrolls = _int(_pick(cfg, "scrolls", "scrollPerRound"), 6, 1, 20)
    # 老配置（脚本/排期造的）只给了轮数，没给时长：照轮数跑，别把 1 轮拉成 1 小时
    rounds_cap = _int(_pick(cfg, "cycles", "rounds", "browseRounds"), 0, 0, 240)
    if not _pick(cfg, "nurtureDurationMin", "durationMin", "runMinutesMin"):
        rounds_cap = rounds_cap or 1

    budget_s = _warmup_budget_s(dur_lo, dur_hi)
    deadline = time.time() + budget_s
    # 可指定起始页（例如某条视频详情页）：不传就走抖音首页推荐流。
    start_url = str(_pick(cfg, "url", "startUrl", "videoUrl", default="") or "").strip()
    short = (ctx.run_id or "")[-6:]
    log(ctx, f"养号参数（运行 {short}）：时长 {dur_lo:g}-{dur_hi:g} 分钟（本次 "
             f"{budget_s // 60} 分 {budget_s % 60} 秒），每屏停留 {stay_lo:g}-{stay_hi:g} 秒，"
             f"点赞概率 {like_p:.0%}，引擎 {ctx.engine or 'shell'}", "normal")
    if collect_p or share_p:
        log(ctx, f"收藏 {collect_p:.0%} / 复制分享链接 {share_p:.0%} 已在页面配置，"
                 f"当前执行器只支持点赞，这两项本轮不执行（不会假装做过）", "warning")

    started = deadline - budget_s
    total_min = max(1, int(round(budget_s / 60.0)))
    _nurture_event(ctx, "nurture-progress", videosWatched=0, liked=0,
                   remainingMin=total_min, totalMin=total_min,
                   action=f"养号中：正在打开推荐流（计划 {total_min} 分钟）")
    # 同一视频只点一次赞：页面重新加载后按钮未必马上带「已赞」态，
    # 重复点会变成「赞→取消赞」的来回操作，既不像真人也会伤账号。
    liked_urls: set[str] = set()
    warned_no_like = False
    i = 0
    while not ctx.stop.is_set():
        if rounds_cap and i >= rounds_cap:
            break
        remaining_s = deadline - time.time()
        if remaining_s <= 0:
            break
        dwell = int(random.uniform(stay_lo, stay_hi) * 1000)
        round_scrolls = _warmup_scrolls_left(scrolls, dwell, remaining_s)
        if round_scrolls == 0:
            break
        i += 1
        ctx.cycles = i
        like_now = 0.0 if (start_url and start_url in liked_urls) else round(like_p * 100, 1)
        cid = store.cycle_begin(db_path, ctx.run_id, "warmup", ctx.task_id, i)
        res = browser.execute({"type": "warmup-watch", "scrolls": round_scrolls,
                               "dwellMs": dwell,
                               "deadlineMs": int(deadline * 1000),
                               **({"url": start_url} if start_url else {}),
                               "likeProbability": like_now,
                               "collectProbability": round(collect_p * 100, 1),
                               "shareProbability": round(share_p * 100, 1),
                               "likeMax": _int(_pick(cfg, "likeMax"), 0, 0, 5)},
                              db_path=db_path,
                              timeout=min(240.0, max(1.0, remaining_s + 12)),
                              log=lambda m, l="normal": log(ctx, m, l),
                              stop_check=ctx.stop.is_set,
                              account_id=ctx.account_id,
                              engine=ctx.engine)
        if not res.get("ok"):
            if ctx.stop.is_set():
                # 手动停止时壳会把在跑的指令中断，返回 ok=False ——
                # 这不是「浏览失败」，记成 failed 会让用户以为养号出错了。
                store.cycle_end(db_path, cid, "stopped", "手动停止", {})
                break
            err = str(res.get("error") or "未知原因")
            _fail(ctx, f"第 {i} 轮浏览失败：{err}")
            store.cycle_end(db_path, cid, "failed", err, {})
            # 执行器没有回执时继续下一轮只会再空等一个超时，造成时长失控。
            break
        acts = res.get("actions") or []
        liked = int(res.get("liked") or 0)
        if liked and start_url:
            liked_urls.add(start_url)
        notes = [str(a.get("like") or "") for a in acts]
        skipped = notes.count("skip-by-probability")
        already = notes.count("already-liked")
        no_effect = notes.count("click-no-effect") + notes.count("click-failed")
        # 除「明确成功/明确跳过」以外，一律算「想点但没点成」。
        # 只认 no-button/need-detail 会漏掉 no-video-link、no-like-after-click 这些
        # 新原因，日志上就会出现「四项全 0、点赞也是 0」的鬼数据。
        tried_ok = {"liked", "liked-in-detail"}
        tried_skip = {"skip-by-probability", "already-liked", "cap-reached",
                      "not-attempted", "write-blocked",
                      "click-no-effect", "click-failed"}
        no_button = sum(1 for n in notes if n not in tried_ok and n not in tried_skip)
        # 被写动作门禁挡下的屏单列：混进「未尝试/无控件」会让用户以为概率没生效。
        blocked_writes = notes.count("write-blocked")
        detail_like = str(res.get("detailLike") or "")
        for act in acts:
            store.insert_account_event(
                db_path, account_id=ctx.account_id, action="watch",
                detail=f"第{i}轮 第{act.get('round')}屏 停留{act.get('dwellMs')}ms"
                       + (f" {act.get('caption')}" if act.get("caption") else ""),
                success=True)
        for _ in range(liked):
            store.insert_account_event(
                db_path, account_id=ctx.account_id, action="like",
                detail=f"第{i}轮 点赞（概率 {like_p:.0%}）", success=True)
        ctx.bump(watched=len(acts), liked=liked)
        store.cycle_end(db_path, cid, "ok",
                        f"浏览 {len(acts)} 屏，点赞 {liked} 次",
                        {"watched": len(acts), "liked": liked})
        # 每轮一条，能对上「10% 概率到底有没有生效」；不再是四条拆着刷屏。
        bits = [f"浏览 {len(acts)} 屏", f"点赞 {liked} 次"]
        if like_p > 0:
            pending = notes.count("not-attempted")
            bits.append(f"跳过 {skipped}｜已赞 {already}｜无效 {no_effect}｜"
                        f"未尝试 {pending}｜无控件 {no_button}")
        if detail_like == "liked":
            where = "视频详情页" if str(res.get("likePath") or "") == "detail" else "信息流"
            bits.append(f"其中 1 个在{where}完成")
        if blocked_writes:
            bits.append(f"写动作被门禁拦下 {blocked_writes}（未点，不是点了没中）")
        bits.append(f"进度 {int(time.time() - started)}/{budget_s} 秒")
        log(ctx, f"第 {i} 轮：" + "，".join(bits))
        if start_url and like_now == 0 and like_p > 0:
            log(ctx, "这条视频本次已点过赞，本轮不再重复点"
                     "（重复点等于取消赞，会被平台当成异常操作）", "normal")
        # 「点不到」只说一次，且说清下一步；以前每轮重复一大段，把进度挤没了。
        # 注意：只要有一屏「该点没点成」就要说，不能用「本轮有跳过」把它盖掉。
        if (like_now > 0 and no_button and not (liked or already)
                and not warned_no_like):
            warned_no_like = True
            reason = {
                "no-video-link": "页面里没有可直接打开的视频链接，点卡片也没进到详情页",
                "no-like-after-click": "点开了卡片，但页面里没出现点赞控件",
                "no-button": "视频详情页也没找到点赞控件（页面结构可能变了）",
                "click-no-effect": ("已点击点赞按钮，但页面没有给出可验证的变化"
                                    "（可能已点上、也可能被页面忽略）；本次不计入点赞数"),
                "detail-error": "打开视频详情页失败",
                "engine-no-like": "当前执行引擎不支持点赞（独立引擎只浏览）",
            }.get(detail_like.split(":")[0], "信息流与详情页都没取到点赞控件")
            log(ctx, f"本轮该点赞的屏没点成：{reason}。本轮只如实记录浏览，"
                     f"不伪造点赞数；后续轮次会继续尝试，仍失败请把这条日志发我。",
                "warning")
        probe = res.get("probe")
        if probe and i == 1:
            names = "、".join(str(n) for n in (probe.get("e2e") or []))
            btns = probe.get("buttons") or []
            desc = "；".join(
                f"{b.get('tag')}.{b.get('cls')}[{b.get('aria')}|{b.get('title')}|{b.get('text')}]"
                for b in btns[:8] if isinstance(b, dict))
            log(ctx, f"页面诊断：{probe.get('url')}｜标题「{probe.get('title') or ''}」｜"
                     f"video 元素 {probe.get('videoCount')} 个｜正文 {probe.get('bodyLen')} 字｜"
                     f"data-e2e：{names[:240]}｜视频锚点：{'、'.join(probe.get('anchors') or [])[:200] or '无'}｜"
                     f"按钮样本：{desc[:300]}")
            if probe.get("cardHtml"):
                log(ctx, f"卡片结构样本：{str(probe.get('cardHtml'))[:600]}")
        _evt(ctx, "stats", stats=dict(ctx.stats))
        _persist_progress(ctx)
        # 真实进度推给页面：已看屏数 / 剩余分钟 / 当前动作（卡片不再一动不动）
        elapsed = int(time.time() - started)
        remain_min = max(0, int(round((budget_s - elapsed) / 60.0)))
        watched_all = int(ctx.stats.get("watched", 0))
        liked_all = int(ctx.stats.get("liked", 0))
        _nurture_event(ctx, "nurture-progress", videosWatched=watched_all,
                       liked=liked_all, remainingMin=remain_min, totalMin=total_min,
                       action=(f"养号中：已浏览 {watched_all} 屏、点赞 {liked_all} 次，"
                               f"第 {i} 轮完成，剩余约 {remain_min} 分钟"))
        _nurture_event(ctx, "nurture-actions", taskId=ctx.task_id,
                       actions={"likes": liked_all, "collections": 0,
                                "linksCopied": 0, "videoShares": 0})
        if res.get("browseError"):
            _fail(ctx, str(res["browseError"]))
            break

    if not rounds_cap and not ctx.stop.is_set() and not ctx.stats.get("failed"):
        ctx.stop.wait(max(0, deadline - time.time()))

    if ctx.stop.is_set():
        # 停止不是失败：只说清停在哪儿，别让用户以为养号跑挂了。
        log(ctx, f"已手动停止（运行 {short}）：跑了 {i} 轮、浏览 "
                 f"{int(ctx.stats.get('watched', 0))} 屏，未满 {budget_s // 60} 分钟设定")
        ctx.stats["stopped"] = 1
    elif ctx.stats.get("failed"):
        log(ctx, f"养号未完成：{i} 轮、浏览 {int(ctx.stats.get('watched', 0))} 屏，"
                 f"用时 {int(time.time() - started)} 秒；"
                 f"原因：{ctx.stats.get('lastError', '浏览失败')}", "error")
    else:
        log(ctx, f"养号已跑满设定时长：{i} 轮、浏览 {int(ctx.stats.get('watched', 0))} 屏，"
                 f"用时 {int(time.time() - started)} 秒", "normal")

    _finish(ctx, "warmup")


# --------------------------------------------------------------------------
# 收尾
# --------------------------------------------------------------------------
def _persist_progress(ctx: Ctx) -> None:
    """把进度写回任务记录：页面列表要显示「运行中 / 已采集 N 条」。"""
    try:
        if ctx.kind == "entity":
            leadgen_pools.update_task_record(ctx.db_path, ctx.task_id, {
                "status": "running", "stats": dict(ctx.stats),
                "progressAt": int(time.time() * 1000)})
        else:
            rec = task_store.get_task(ctx.db_path, ctx.kind, ctx.task_id) or {}
            if rec:
                rec.update({"status": "running", "stats": dict(ctx.stats)})
                task_store.save_task(ctx.db_path, ctx.kind, rec)
    except sqlite3.Error:
        pass


def _finish(ctx: Ctx, kind: str) -> None:
    cost = round(time.time() - ctx.started_at, 1)
    stopped = ctx.stop.is_set()
    failed = int(ctx.stats.get("failed", 0))
    if ctx.stats.get("reason") == "no-targets":
        status = "failed"
    elif stopped:
        status = "stopped"
    elif failed and (kind == "warmup" or not (ctx.stats.get("leads") or ctx.stats.get("matched")
                         or ctx.stats.get("watched"))):
        status = "failed"
    else:
        status = "completed"

    if kind == "entity":
        summary = (f"线索采集结束：{ctx.cycles} 个目标，视频 {ctx.stats.get('videos', 0)} 条，"
                   f"评论 {ctx.stats.get('comments', 0)} 条，新增线索 {ctx.stats.get('leads', 0)} 条，"
                   f"命中 {ctx.stats.get('hit', 0)} 条，用时 {cost}s")
    elif kind == "monitor":
        summary = (f"监控结束：{ctx.cycles} 轮，读到 {ctx.stats.get('read', 0)} 条评论，"
                   f"新增 {ctx.stats.get('new', 0)} 条，命中 {ctx.stats.get('matched', 0)} 条，"
                   f"用时 {cost}s")
    else:
        summary = (f"养号结束（运行 {(ctx.run_id or '')[-6:]}）：{ctx.cycles} 轮，"
                   f"浏览 {ctx.stats.get('watched', 0)} 屏，"
                   f"点赞 {ctx.stats.get('liked', 0)} 次，用时 {cost}s")
        if stopped:
            summary += "（手动停止，未跑满设定时长）"
    if ctx.stats.get("reason") == "no-targets":
        summary += "（任务没有可执行目标，未访问平台）"
    elif status == "failed" and ctx.stats.get("lastError"):
        summary += f"；原因：{ctx.stats['lastError']}"

    log(ctx, summary, "warning" if status != "completed" else "normal")
    _evt(ctx, "finished", status=status, endReason=status, stats=dict(ctx.stats),
         message=summary)
    store.run_end(ctx.db_path, ctx.run_id, status, ctx.stats, summary)

    try:
        if kind == "entity":
            records = leadgen_pools.load_records(ctx.db_path)
            rec = records.get(ctx.task_id) or {}
            runs = [r for r in (rec.get("runs") or []) if isinstance(r, dict)]
            runs.append({
                "automationTaskId": ctx.run_id, "accountId": ctx.account_id,
                "startedAt": int(ctx.started_at * 1000),
                "endedAt": int(time.time() * 1000), "status": status,
                "endReason": status, "stats": dict(ctx.stats),
            })
            leadgen_pools.update_task_record(ctx.db_path, ctx.task_id, {
                "status": status, "runs": runs[-20:], "stats": dict(ctx.stats),
                "endedAt": int(time.time() * 1000), "endReason": status})
        else:
            rec = task_store.get_task(ctx.db_path, kind, ctx.task_id) or {}
            if rec:
                rec.update({"status": status, "stats": dict(ctx.stats),
                            "endedAt": int(time.time() * 1000),
                            "endReason": status, "lastMessage": summary})
                task_store.save_task(ctx.db_path, kind, rec)
    except sqlite3.Error:
        pass

    if kind == "warmup":
        event_bus.publish("nurture-task-finished", {
            "taskId": ctx.task_id, "accountId": ctx.account_id,
            "reason": "manual_stop" if stopped else status,
            "status": status, "message": summary,
            "progress": {"nurture": {
                "videosWatched": int(ctx.stats.get("watched", 0)),
                "actions": {"likes": int(ctx.stats.get("liked", 0)),
                            "collections": 0, "linksCopied": 0}}}})
