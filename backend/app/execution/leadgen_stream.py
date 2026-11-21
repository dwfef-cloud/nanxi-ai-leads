"""Sequential live discovery and interaction for an explicitly started task."""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3

from app.execution import leadgen_config as settings
from app.runtime import browser

# 搜索结果「深取」倍数：一次多读几倍候选，把已经处理过的跳掉再取前 videoMax 条。
# 为什么必须这么做：同一个关键词的综合排序是固定的，平台每次返回同一批头部视频；
# 只取前 videoMax 条的话，第二轮开始每条都是「已主评」，整轮 0 动作、0 评论，
# 用户看到的就是「每次都搜到前面那几条」。
DEEP_FETCH_RATIO = 4
# 深取上限要能托住设置页允许的最大值（MAX_VIDEOS=200）：4×200 太浪费，
# 但 60 又会让「填 200 条」的配置永远只能拿到 60 条候选，等于换个地方静默截断。
DEEP_FETCH_CAP = 240

# 单遍式入口：竖向一屏一条的信息流（有「下一个」切换控件），可以刷到哪停到哪。
# 喜欢列表 / 话题页是瀑布流网格，没有逐条切换的控件，仍走「先抓清单再逐条打开」。
FEED_SOURCES = {"recommend", "follow"}
# 预判窗口（秒）：真人刷到不对味的视频 2-5 秒就划走（2026-11-20 行为数据口径），
# 先看这一小段再做人设快判，不符立即切下一条，不再把停留时间看完才发现不符。
PRE_SWIPE_WATCH = (3, 8)
RECOVERY_BACKOFF = (1, 2, 5, 10, 20, 30)
# AI 生成连续失败阈值：单条失败只跳过该视频（模型偶发 5xx/被 WAF 拦一下很常见），
# 连续这么多条都失败就说明模型侧真的不可用，继续刷只会白白耗掉整轮时间。
GENERATION_FAIL_LIMIT = 3

# 与 live_workflow.action_key 同构：主评只按「账号 + 视频」判定去重（commentId 为空）
def post_comment_key(account_id: str, video_url: str) -> str:
    return json.dumps([account_id, "post-comment", video_url, ""], separators=(",", ":"))


class GenerationSkipped(Exception):
    """本视频的 AI 生成失败，但本轮继续：由 perform_plan 抛出、浏览循环消费。

    不直接用 GenerationError 传播，是因为外层把它当「整轮失败」处理；
    连续失败到阈值时 perform_plan 会改抛 GenerationError，再把整轮停下。
    """


def _entry_read_error(result) -> str:
    """入口读取失败时，把壳带回的诊断拼进文案 —— 否则用户只看到一句泛话。

    2026-11-20 真机：推荐流两次「未进入视频信息流」，diag 里有当时页面地址、
    是否登录墙、页面文字量，但错误信息没带上，只能盲猜。
    """
    detail = str(result.get("error") or "读取视频入口失败")
    diag = result.get("diag") or {}
    marks = []
    if isinstance(diag, dict):
        if diag.get("loginWall"):
            marks.append("页面出现登录墙")
        if diag.get("href"):
            marks.append("当时地址 " + str(diag["href"])[:120])
        if diag.get("bodyLen") is not None:
            marks.append("页面文字量 " + str(diag["bodyLen"]))
        if diag.get("cards") is not None:
            marks.append("信息流卡片 " + str(diag["cards"]))
        if diag.get("items") is not None:
            marks.append("已读条数 " + str(diag["items"]))
    if result.get("note"):
        marks.append(str(result["note"]))
    return detail + ("（诊断：" + "；".join(marks) + "）" if marks else "")


def _relative_from_minutes(minutes) -> str:
    """把评论的 ageMinutes（壳侧已折算成分钟）转回「N天前」这类相对时间文案。

    用于发布时间推断：age_seconds 能解析这几种格式，推断值直接当 publishedText 用。
    """
    m = int(minutes)
    if m < 1:
        return "刚刚"
    if m < 60:
        return f"{m}分钟前"
    if m < 1440:
        return f"{m // 60}小时前"
    if m < 10080:
        return f"{m // 1440}天前"
    if m < 43200:
        return f"{m // 10080}周前"
    if m < 525600:
        return f"{m // 43200}个月前"
    return f"{m // 525600}年前"


def infer_publish_time(comments) -> str:
    """从评论区时间分布推断视频发布时间（用户 2026-11-20 口径）。

    详情浮层经常不提供发布时间；而最早一条评论的时间是视频发布时间的
    下界——视频先发布、评论才出现，首评通常紧贴发布时刻。取全部已读
    评论里最大的 ageMinutes 折算回相对时间，填进候选给时间过滤用。
    估不出来（评论区还没评论、或评论全部被时限过滤）就返回空串。
    """
    ages = [c.get("ageMinutes") for c in comments or []
            if isinstance(c, dict) and isinstance(c.get("ageMinutes"), (int, float))]
    if not ages:
        return ""
    return _relative_from_minutes(max(ages))


def already_processed(db_path, account_id: str, video_url: str) -> bool:
    """这条视频对该账号是否已经没有主评机会（已发送或结果待核验）。"""
    key = post_comment_key(str(account_id), str(video_url))
    target = hashlib.sha256(key.encode("utf-8")).hexdigest()
    try:
        with sqlite3.connect(str(db_path), timeout=10) as con:
            row = con.execute("SELECT status FROM write_reservations "
                              "WHERE namespace='live' AND target_hash=?", (target,)).fetchone()
    except sqlite3.Error:
        return False
    return bool(row and str(row[0]) in {"sent_ok", "uncertain"})



def run(ctx, scope):
    from app.api import task_runner as runner
    from app.execution.leadgen_plan import build
    from app.execution.live_leadgen import perform, classify_intent, GenerationError
    from app.execution.leadgen_media import EXTRA_FIELDS
    from app.execution.live_workflow import _account, _boundary, _watch, _wait_active, valid_video

    status, message, reason = "failed", "顺序浏览任务未完成", "stream_failed"
    config = scope.get("effectiveConfig") or settings.normalize(ctx.payload, account_id=ctx.account_id)
    attempted = visited = 0
    gen_fail_streak = 0
    counts = {}
    seen = set()
    skipped_ad = object()
    planned_videos = config["videoMax"] * (len(scope.get("keywords", [])) if config["videoSources"][0] in {"search", "hashtag"} else 1)

    class Check:
        def __call__(self):
            if not _boundary(ctx):
                raise ValueError("任务已停止，剩余视频未继续")

        def stopped(self):
            return ctx.stop.is_set() or ctx.pause.is_set()

    check = Check()

    def finish():
        if ctx.stop.is_set():
            final_status, final_message, final_reason = "stopped", "已停止，剩余视频未继续浏览", "manual_stop"
        else:
            final_status, final_message, final_reason = status, message, reason
        runner._log(ctx, final_message, "normal" if final_status in {"completed", "stopped"} else "error")
        runner._finish(ctx, final_status, dict(ctx.stats), final_message, final_reason)
        runner._emit_task(ctx, "finished", status=final_status, stats=dict(ctx.stats),
                          message=final_message, endReason=final_reason)
        runner._emit("status", ctx.task_id, ctx.account_id, accountId=ctx.account_id,
                     status="finished", finishMessage=final_message)
        runner.event_bus.publish("leadgen-task-finished", {
            "taskId": ctx.task_id, "accountId": ctx.account_id, "reason": final_reason,
            "status": final_status, "message": final_message,
            "progress": {"videos": {"done": visited, "planned": planned_videos},
                         "interactions": {"done": attempted, "planned": config["interactionLimit"]}}})

    try:
        if scope.get("mode") != "stream" or scope.get("runTaskId") != ctx.task_id:
            raise ValueError("顺序任务授权范围与运行任务不一致")
        account = _account(ctx.db_path, ctx.account_id)
        source = config["videoSources"][0]
        # 把「本次实际抽到的计划量」写进日志。设置页填的是随机区间（如视频 1-8、
        # 互动 1-1），壳在启动时抽一个值当本轮上限；不说明的话，用户设了 8 条
        # 却只跑 1 条，看起来就像「没按设定执行」。
        runner._log(ctx, f"本次计划：关键词「{'、'.join(scope.get('keywords') or []) or '无'}」"
                         f"· 打开视频 {config['videoMax']} 条 · 互动上限 {config['interactionLimit']} 次"
                         "（两项都按设置页区间随机抽取，想固定就把上下限填成同一个数）")
        def candidate_stream():
            if source == "search":
                keywords = list(scope.get("keywords") or [])
                remaining = {keyword: config["videoMax"] for keyword in keywords}
                retries = {keyword: 0 for keyword in keywords}
                pool = min(max(config["videoMax"] * DEEP_FETCH_RATIO, 24), DEEP_FETCH_CAP)
                while any(count > 0 for count in remaining.values()):
                    if not _boundary(ctx):
                        return
                    for keyword in keywords:
                        need = remaining[keyword]
                        if need <= 0:
                            continue
                        runner._action(ctx, "正在搜索关键词：" + keyword)
                        try:
                            result = browser.execute({"type": "search-videos", "keyword": keyword,
                                "limit": pool, "runId": ctx.task_id}, db_path=ctx.db_path,
                                account_id=ctx.account_id, engine="shell", stop_check=ctx.stop.is_set)
                        except Exception as exc:
                            result = {"ok": False, "error": str(exc)}
                        if ctx.stop.is_set():
                            return
                        found = [item for item in (result.get("items", []) if isinstance(result, dict) else [])[:pool]
                                 if isinstance(item, dict) and valid_video(item.get("url"))]
                        fresh = [item for item in found if item["url"] not in seen
                                 and not already_processed(ctx.db_path, ctx.account_id, item["url"])]
                        if not isinstance(result, dict) or not result.get("ok") or not fresh:
                            retries[keyword] += 1
                            reason = (str(result.get("error") or result.get("note") or "未找到新的可执行视频")
                                      if isinstance(result, dict) else "搜索命令无响应")
                            delay = RECOVERY_BACKOFF[min(retries[keyword] - 1, len(RECOVERY_BACKOFF) - 1)]
                            runner._log(ctx, f"关键词「{keyword}」暂时没有新的可执行视频（{reason}）；"
                                             f"目标未完成，{delay} 秒后继续搜索", "warning")
                            if not _wait_active(ctx, delay,
                                                next_action=f"重新搜索关键词「{keyword}」"):
                                return
                            continue
                        retries[keyword] = 0
                        pick = min(need, len(fresh))
                        # 不按排名顺序取前 N 条：从未处理结果中随机抽取，抽取顺序即浏览顺序。
                        items = random.sample(fresh, pick)
                        skipped = len(found) - len(fresh)
                        ctx.stats["searchedVideos"] = ctx.stats.get("searchedVideos", 0) + len(items)
                        if skipped:
                            runner._log(ctx, f"关键词“{keyword}”搜到 {len(found)} 条，"
                                             f"跳过已处理（已主评/结果待核验/本轮已看）{skipped} 条")
                        runner._log(ctx, f"关键词“{keyword}”找到 {len(found)} 条（未处理 {len(fresh)} 条），"
                                         f"随机抽 {len(items)} 条浏览")
                        for item in items:
                            before_visited = visited
                            yield {**item, "videoUrl": item["url"], "keyword": keyword}
                            if visited > before_visited:
                                remaining[keyword] -= 1
            elif source == "specific":
                for url in settings.video_urls(config.get("specifiedUrls")):
                    yield {"videoUrl": url, "keyword": "指定视频"}
            else:
                if source in FEED_SOURCES:
                    # 单遍式入口（推荐流 / 关注流）：不在准备阶段预刷清单。
                    # 旧流程在这里一次性滚动抓满 videoMax 条候选（预览窗口开头
                    # 两分钟疯狂刷视频），再回头逐条打开慢慢看 —— 同一个信息流
                    # 过了两遍，用户看到开头的快刷就以为「没按设置执行」。
                    # 现在只 yield 一个入口标记，主循环直接进流：刷到哪、停到哪。
                    yield {"_feedEntry": True, "source": source,
                           "keyword": settings.SOURCES.get(source, source)}
                    return
                terms = scope["keywords"] if source == "hashtag" else [""]
                for term in terms:
                    if not _boundary(ctx):
                        return
                    result = browser.execute({"type": "read-source-videos", "source": source,
                        "keyword": term, "limit": config["videoMax"], "runId": ctx.task_id},
                        db_path=ctx.db_path, account_id=ctx.account_id, engine="shell",
                        stop_check=ctx.stop.is_set)
                    if ctx.stop.is_set():
                        return
                    if not result.get("ok"):
                        raise ValueError(_entry_read_error(result))
                    items = [item for item in result.get("items", [])[:config["videoMax"]]
                             if isinstance(item, dict) and valid_video(item.get("url"))]
                    ctx.stats["searchedVideos"] = ctx.stats.get("searchedVideos", 0) + len(items)
                    label = term or settings.SOURCES.get(source, source)
                    for item in items:
                        yield {**item, "videoUrl": item["url"], "keyword": label}

        # 人式浏览的导航状态：同一个搜索词下，第一条从搜索结果点封面进入，
        # 之后点平台自己的「下一个」顺着往下看——不再每条都把目标网址重新输一遍。
        nav = {"entered": False, "keyword": "", "canNext": source != "specific"}

        def browser_nav(command):
            """把导航异常转成可记录的失败回执，让外层恢复循环接管。"""
            try:
                return browser.execute(command, db_path=ctx.db_path, account_id=ctx.account_id,
                                       engine="shell", stop_check=ctx.stop.is_set)
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        class FlowStop(Exception):
            """浏览过程被停止/暂停打断：走正常收尾，最终状态由 stop 标志判定。"""

        def open_video(url, candidate, preserve_search=False):
            opened = browser_nav({"type": "open-video", "videoUrl": url, "runId": ctx.task_id,
                                  "preserveSearch": preserve_search})
            if ctx.stop.is_set():
                raise FlowStop
            if not isinstance(opened, dict) or not opened.get("ok"):
                detail = opened.get("error") if isinstance(opened, dict) else "壳端无有效回执"
                raise ValueError(detail or "视频页打开失败")
            if "isAd" in opened:
                candidate["isAd"] = opened.get("isAd") is True
            nav.update(entered=True, keyword=candidate["keyword"], canNext=source != "specific")
            if opened.get("mode") == "clicked-card":
                runner._log(ctx, "已在搜索结果里点开这条视频")

        def restore_search_result(candidate, preferred_url=None):
            """恢复到同一关键词结果页，再点未处理的视频卡片，不跳到详情 URL。"""
            nonlocal nav
            keyword = str(candidate.get("keyword") or "").strip()
            pool = min(max(config["videoMax"] * DEEP_FETCH_RATIO, 24), DEEP_FETCH_CAP)
            retry = 0
            while True:
                if not _boundary(ctx):
                    raise FlowStop
                result = browser_nav({"type": "search-videos", "keyword": keyword,
                    "limit": pool, "runId": ctx.task_id})
                if ctx.stop.is_set():
                    raise FlowStop
                found = [item for item in (result.get("items", []) if isinstance(result, dict) else [])
                         if isinstance(item, dict) and valid_video(item.get("url"))]
                preferred = next((item for item in found if item["url"] == preferred_url
                                  and item["url"] not in seen
                                  and not already_processed(ctx.db_path, ctx.account_id, item["url"])), None)
                target = preferred or next((item for item in found if item["url"] not in seen
                                  and not already_processed(ctx.db_path, ctx.account_id, item["url"])), None)
                if isinstance(result, dict) and result.get("ok") and target:
                    target = {**target, "videoUrl": target["url"], "keyword": keyword}
                    try:
                        open_video(target["url"], target, preserve_search=True)
                        runner._log(ctx, f"已回到关键词「{keyword}」搜索结果，继续浏览未处理视频")
                        return target["url"], target
                    except FlowStop:
                        raise
                    except Exception as exc:
                        reason = str(exc)
                else:
                    reason = (str(result.get("error") or result.get("note") or "没有未处理的视频候选")
                              if isinstance(result, dict) else "搜索恢复命令无响应")
                retry += 1
                delay = RECOVERY_BACKOFF[min(retry - 1, len(RECOVERY_BACKOFF) - 1)]
                runner._log(ctx, f"关键词「{keyword}」结果页尚未恢复（{reason}）；"
                                 f"任务保持运行，{delay} 秒后第 {retry + 1} 次重试", "warning")
                if not _wait_active(ctx, delay,
                                    next_action=f"重新搜索「{keyword}」并继续浏览"):
                    raise FlowStop

        def _pre_swipe_reason(candidate):
            """开屏快判：只用在「开屏前手上就有数据」的筛选上。

            标题词、排除作者、发布时间（仅当卡片带了发布时间）这三样不依赖
            详情页读取，能提前定论；最少点赞这类指标 feed 卡片没有，
            交给计划阶段用 inspect 结果判，避免把本可核实的视频误杀。
            """
            title = str(candidate.get("title") or "").strip()
            if not title:
                return ""
            excluded = config.get("excludeTitleKeywords") or []
            included = config.get("includeTitleKeywords") or []
            if any(w in title for w in excluded):
                return "命中排除标题词"
            if included and not any(w in title for w in included):
                return "未命中标题包含词"
            if settings.author_excluded(
                    {"excludeAuthorAccounts": config.get("excludeAuthorAccounts") or []},
                    candidate.get("author")):
                return "命中排除作者"
            if str(candidate.get("publishedText") or "").strip():
                window = settings.time_window(config, "recommendVideoTime")
                if window is not None:
                    age = settings.age_seconds(candidate)
                    if age is not None and age > window:
                        return "视频发布时间超出范围"
            return ""

        def browse(url, candidate, ordinal):
            """两段式浏览（用户 2026-11-20 口径：不对味的视频几秒就划走）。

            第一段「预判窗口」3-8 秒：看一小段后做开屏快判 + AI 标题人设快判，
            不符立即划走并返回 None；相符再看满剩余停留，进入评论区。
            被停止/暂停时抛 FlowStop（保持 stopped 收尾语义）。
            """
            nonlocal visited
            seen.add(url)
            runner._action(ctx, f"正在浏览第{ordinal}个视频：{candidate.get('title') or url}")
            if config.get("skipAdVideos", True) and candidate.get("isAd") is True:
                runner._action(ctx, "检测到广告内容，直接划过")
                runner._log(ctx, f"视频「{str(candidate.get('title') or '')[:60]}」识别为广告，未停留、未读取评论")
                ctx.stats["skippedAds"] = ctx.stats.get("skippedAds", 0) + 1
                return skipped_ad
            pre = 0
            if config["stayMax"]:
                total = random.randint(config["stayMin"], config["stayMax"])
                quick = _pre_swipe_reason(candidate)
                persona_on = bool(config.get("enablePersonaVideoFilter")) and bool(config.get("aiReplyMode"))
                if quick or (persona_on and str(candidate.get("title") or "").strip()):
                    pre = min(random.randint(*PRE_SWIPE_WATCH), total)
                    watched = _watch(ctx, {"videoUrl": url}, pre,
                                     next_action="视频预览结束后判断是否继续浏览")
                    if ctx.stop.is_set():
                        raise FlowStop
                    if not watched.get("ok"):
                        raise ValueError("视频打开或停留失败（"
                                         + str(watched.get("error") or watched.get("reason") or "原因未知")
                                         + "），已停止本轮")
                if quick:
                    reason = quick
                elif pre:
                    try:
                        decision = classify_intent(ctx.db_path, config, candidate, video_only=True)
                    except Exception as exc:
                        # 预判失败不中断本轮：按旧流程看满再看，计划阶段的人设筛选
                        # 会把真正的 AI 错误如实暴露出来。
                        runner._log(ctx, "人设预判失败（" + str(exc)[:80] + "），本条按完整浏览处理", "warning")
                        decision = {"matched": True}
                    if not decision.get("matched"):
                        reason = "与账号人设不符"
                    else:
                        reason = ""
                else:
                    reason = ""
                if reason:
                    runner._action(ctx, "与账号人设不符，快速划过")
                    runner._log(ctx, "视频「" + str(candidate.get("title") or "")[:40]
                                + "」看了 " + str(pre) + " 秒即划过：" + str(reason)[:100])
                    visited += 1
                    ctx.stats["videos"] = visited
                    return None
                # 相符（或无需预判）：看满剩余停留
                remaining = total - pre
                if remaining > 0:
                    watched = _watch(ctx, {"videoUrl": url}, remaining,
                                     next_action="当前视频停留结束后浏览评论区")
                    if ctx.stop.is_set():
                        raise FlowStop
                    if not watched.get("ok"):
                        raise ValueError("视频打开或停留失败（"
                                         + str(watched.get("error") or watched.get("reason") or "原因未知")
                                         + "），已停止本轮")
            runner._action(ctx, "正在展开并浏览本视频评论区")
            comments_page = browser.execute({"type": "read-comments", "videoUrls": [url],
                "limit": 60, "expandSubComments": config.get("expandSubComments") is True,
                "runId": ctx.task_id}, db_path=ctx.db_path, account_id=ctx.account_id,
                engine="shell", stop_check=ctx.stop.is_set)
            if ctx.stop.is_set():
                raise FlowStop
            if not isinstance(comments_page, dict):
                raise ValueError("评论区读取没有返回有效结果，已停止本轮")
            if not comments_page.get("ok"):
                raise ValueError(comments_page.get("error") or "评论区展开或读取失败，已停止本轮")
            visited += 1
            ctx.stats["videos"] = visited
            ctx.stats["commentsRead"] = ctx.stats.get("commentsRead", 0) + len(comments_page.get("comments", []))
            if not comments_page.get("comments"):
                runner._action(ctx, "评论区暂无内容，跳过当前视频")
                runner._log(ctx, f"视频「{str(candidate.get('title') or '')[:60]}」评论区为空，未生成互动动作，继续浏览")
            inferred = infer_publish_time(comments_page.get("comments"))
            if inferred and not str(candidate.get("publishedText") or "").strip():
                candidate["publishedText"] = inferred
                runner._log(ctx, f"详情页没提供发布时间，按评论区时间分布推断为「{inferred}」"
                                 "（最早一条评论的时间≈发布时间）")
            runner._log(ctx, f"本视频评论区浏览完成：读取到 {len(comments_page.get('comments', []))} 条评论")
            return comments_page

        def perform_plan(url, keyword, comments_page, candidate):
            """为本视频生成并执行计划。同一视频内的动作连续执行、不插间隔；
            互动节奏由视频之间的统一间隔控制（见 wait_between_videos）。"""
            nonlocal attempted, gen_fail_streak
            remaining = config["interactionLimit"] - attempted
            per_video = dict(ctx.payload)
            per_video.update(videoSources=["specific"], specifiedUrls=url, _sourceKeyword=keyword,
                             _streamMode=True, interactionLimit=remaining,
                             # feed 卡片 / next-video 已拿到的信息（标题、发布时间等）透传给
                             # build：详情浮层常常读不到发布时间，丢了它用户设的「只处理
                             # N 天内发布」会把每条视频都判成「发布时间未知」全部跳过。
                             _prefillCandidate=candidate,
                             dmLimit=max(0, config["dmLimit"] - counts.get("send-dm", 0)),
                             followLimit=max(0, config["followLimit"] - counts.get("follow-user", 0)),
                             profileShareLimit=max(0, config["profileShareLimit"] - counts.get("share-profile", 0)))
            check()
            try:
                plan = build(ctx.db_path, per_video, account, check=check,
                             progress=lambda text: (runner._action(ctx, text), runner._log(ctx, text)),
                             comment_cache={url: comments_page})
            except GenerationError as exc:
                # 模型侧失败不该把整轮 20 条视频一起废掉：本例只丢这一条的动作，
                # 继续按节奏刷下一条；但连续失败说明模型真的不可用，此时停轮并说清原因。
                ctx.stats["generateFailed"] = ctx.stats.get("generateFailed", 0) + 1
                detail = str(exc) or "AI 生成失败"
                title = str(candidate.get("title") or "")[:40]
                if not exc.recoverable:
                    runner._log(ctx, f"AI 配置或权限错误，停止本轮，不继续消耗视频目标：{detail}", "error")
                    raise
                gen_fail_streak += 1
                if gen_fail_streak >= GENERATION_FAIL_LIMIT:
                    raise GenerationError(
                        f"连续 {gen_fail_streak} 个视频 AI 生成失败，已停止本轮（问题在模型侧，不是抖音侧）："
                        + detail) from exc
                runner._log(ctx, f"视频「{title}」AI 生成失败，已跳过本条并继续下一条：{detail}"
                                 f"（连续失败 {gen_fail_streak}/{GENERATION_FAIL_LIMIT}）", "warning")
                raise GenerationSkipped(detail) from exc
            gen_fail_streak = 0
            ctx.stats["selectedVideos"] = ctx.stats.get("selectedVideos", 0) + plan["planStats"].get("selectedVideos", 0)
            for action in plan["actions"]:
                if attempted >= config["interactionLimit"] or not _boundary(ctx):
                    break
                action["keyword"] = keyword
                if action["type"] in {"post-comment", "reply-comment"}:
                    runner._action(ctx, "已读取本视频讨论，准备执行已配置的评论操作")
                runner._action(ctx, f"正在执行视频互动：{action['type']}")
                result = perform(ctx.db_path, action["type"], account_id=ctx.account_id,
                    task_id=ctx.leadgen_id, video_url=url, text=action.get("text", ""),
                    comment_id=action.get("commentId", ""), comment_author=action.get("commentAuthor", ""),
                    recipient_id=action.get("recipientId", ""),
                    extras={k: action[k] for k in EXTRA_FIELDS if k in action},
                    stopped=ctx.stop.is_set, run_id=ctx.task_id)
                attempted += 1
                counts[action["type"]] = counts.get(action["type"], 0) + 1
                ctx.stats["attempted"] = attempted
                ctx.stats["remaining"] = max(0, config["interactionLimit"] - attempted)
                runner._log(ctx, f"{action['type']}：{result.get('status', 'uncertain')} · {result.get('error') or result.get('code', '')}")
                if result.get("ok") is True and result.get("status") == "sent_ok":
                    field = {"post-comment": "videoComments", "reply-comment": "replies", "send-dm": "dms",
                        "like-comment": "likes", "like-video": "mainVideoLikes", "favorite-video": "mainVideoFavorites",
                        "follow-user": "follows", "share-profile": "profileShares", "share-comment": "commentShares"}[action["type"]]
                    ctx.stats[field] = ctx.stats.get(field, 0) + 1
                    if field == "videoComments":
                        ctx.stats["published"] = ctx.stats.get("published", 0) + 1
                    elif field not in {"mainVideoLikes", "mainVideoFavorites"}:
                        ctx.stats["interacted"] = ctx.stats.get("interacted", 0) + 1
                    ctx.stats["videos"] = visited
                elif result.get("status") == "skipped":
                    ctx.stats["skipped"] = ctx.stats.get("skipped", 0) + 1
                else:
                    field = "uncertain" if result.get("status") == "uncertain" else "failed"
                    ctx.stats[field] = ctx.stats.get(field, 0) + 1
                    outcome = "结果待核验" if field == "uncertain" else "动作失败"
                    raise RuntimeError(outcome + "，已停止且不会自动重试："
                                       + str(result.get("error") or result.get("code") or "未知原因"))
                runner._emit_task(ctx, "stats", stats=dict(ctx.stats))

        def wait_between_videos():
            """视频之间的节奏间隔：在已切换到的下一条视频上等待。

            若先等再切换，屏幕会长时间停在上一条视频上，看起来像卡住；
            因此调用方必须在 next-video 成功或补开新视频之后调用。
            旧实现在每个动作之后等待 —— AI 判「不适合评论」的视频不产生动作，
            结果 77 秒就切下一条，用户设的 3–5 分钟间隔形同虚设。"""
            seconds = random.randint(config["intervalMin"], config["intervalMax"])
            runner._log(ctx, f"已切换到下一条，等待视频间隔 {seconds} 秒")
            return _wait_active(ctx, seconds,
                                next_action="继续浏览下一条视频并读取评论")

        def run_feed(entry):
            """单遍式：推荐流 / 关注流 —— 刷到哪、停到哪。
            旧流程先一次性滚动抓满 videoMax 条候选清单（预览窗口开头疯狂快刷），
            再回头逐条打开慢慢看：同一个信息流过了两遍，用户看到开头的快刷就
            以为「没按设置执行」。现在与真人一致：进流 → 停留 → 读评论 →
            AI 判断 → 互动 → 设定间隔 → 翻下一条。"""
            nonlocal visited, attempted
            label = entry["keyword"] or settings.SOURCES.get(entry["source"], entry["source"])
            feed_nav = {"entered": False}
            skip_streak = 0
            fetch_fail = 0
            entry_fail = 0
            recovery_attempt = 0
            wait_for_next_video = False
            running = True
            runner._log(ctx, f"{label}按单遍式浏览：刷到哪、停到哪，"
                             "每条视频按设置的停留时长与间隔节奏执行")
            while (running and visited < config["videoMax"]
                   and attempted < config["interactionLimit"]):
                if not _boundary(ctx):
                    raise FlowStop
                if not feed_nav["entered"]:
                    # 只读当前这一条：几秒就能进流开看，不再预刷整个清单。
                    runner._action(ctx, f"正在进入{label}信息流")
                    page = browser_nav({"type": "read-source-videos", "source": entry["source"],
                        "keyword": "", "limit": 1, "runId": ctx.task_id})
                    if ctx.stop.is_set():
                        raise FlowStop
                    if not isinstance(page, dict) or not page.get("ok"):
                        entry_fail += 1
                        detail = _entry_read_error(page if isinstance(page, dict) else {})
                        delay = RECOVERY_BACKOFF[min(entry_fail - 1, len(RECOVERY_BACKOFF) - 1)]
                        runner._log(ctx, f"{label}入口暂不可用（{detail}）；任务保持运行，"
                                         f"{delay} 秒后第 {entry_fail} 次重新进入", "warning")
                        if not _wait_active(ctx, delay,
                                            next_action=f"重新进入{label}并继续浏览"):
                            raise FlowStop
                        continue
                    items = [i for i in page.get("items", [])
                             if isinstance(i, dict) and valid_video(i.get("url"))]
                    if not items:
                        entry_fail += 1
                        detail = f"{label}暂时没有读到可浏览的视频"
                        if page.get("note"):
                            detail += f"（{page.get('note')}）"
                        delay = RECOVERY_BACKOFF[min(entry_fail - 1, len(RECOVERY_BACKOFF) - 1)]
                        runner._log(ctx, detail + f"；任务保持运行，{delay} 秒后重新检查入口", "warning")
                        if not _wait_active(ctx, delay,
                                            next_action=f"等待{label}信息流恢复"):
                            raise FlowStop
                        continue
                    entry_fail = 0
                    first = items[0]
                    url, candidate = first["url"], {**first, "videoUrl": first["url"], "keyword": label}
                else:
                    step = browser_nav({"type": "next-video", "runId": ctx.task_id,
                        # 推荐流可能夹有直播卡片；仅此单遍信息流允许壳侧退出/跳过非视频页。
                        "feedRecovery": True, "feedSource": entry["source"],
                        "recoveryAttempt": recovery_attempt + 1})
                    if ctx.stop.is_set():
                        raise FlowStop
                    if isinstance(step, dict) and step.get("ok") and valid_video(step.get("videoUrl")):
                        recovery_attempt = 0
                        url = step["videoUrl"]
                        candidate = {"videoUrl": url, "url": url, "keyword": label,
                                     "title": step.get("title") or "",
                                     "author": step.get("author") or "",
                                     "publishedText": step.get("publishedText") or "",
                                     "isAd": step.get("isAd") is True}
                    else:
                        # 保持当前沉浸流：不能通过重新加载候选地址兜底，否则会切成详情页。
                        fetch_fail += 1
                        recovery_attempt += 1
                        if isinstance(step, dict) and step.get("nonVideo"):
                            runner._log(ctx, "当前内容不是可浏览的视频（直播间或非视频卡片），"
                                        "正在尝试跳过并回到视频流；不会把它计入视频数", "warning")
                        else:
                            step_error = (step.get("error") if isinstance(step, dict) else "恢复命令无响应")
                            runner._log(ctx, "「下一个」没切成新视频（"
                                        + str(step_error or "未检测到新视频")
                                        + "），留在当前页面重试，不打开详情页", "warning")
                        # 多次在原页面恢复失败后，重新打开用户配置的同一信息流入口。
                        # 这是恢复原目标来源，不是跳到无关推荐，也不会打开视频详情 URL。
                        if fetch_fail % 3 == 0:
                            runner._log(ctx, f"原信息流连续 {fetch_fail} 次未恢复，重新进入配置的{label}入口", "warning")
                            page = browser_nav({"type": "read-source-videos", "source": entry["source"],
                                "keyword": "", "limit": 1, "runId": ctx.task_id})
                            if ctx.stop.is_set():
                                raise FlowStop
                            items = [i for i in (page.get("items", []) if isinstance(page, dict) else [])
                                     if isinstance(i, dict) and valid_video(i.get("url"))]
                            if isinstance(page, dict) and page.get("ok") and items:
                                url = items[0]["url"]
                                candidate = {**items[0], "videoUrl": url, "keyword": label}
                                feed_nav["entered"] = True
                                runner._log(ctx, f"已重新进入配置的{label}，从当前信息流继续")
                                fetch_fail = 0
                                recovery_attempt = 0
                            else:
                                detail = _entry_read_error(page if isinstance(page, dict) else {})
                                runner._log(ctx, f"{label}入口仍未恢复（{detail}），继续保留任务并重试", "warning")
                                delay = RECOVERY_BACKOFF[min(recovery_attempt - 1, len(RECOVERY_BACKOFF) - 1)]
                                if not _wait_active(ctx, delay,
                                                    next_action=f"重新进入{label}并恢复视频流"):
                                    raise FlowStop
                                continue
                        else:
                            delay = RECOVERY_BACKOFF[min(recovery_attempt - 1, len(RECOVERY_BACKOFF) - 1)]
                            runner._log(ctx, f"任务未结束；{delay} 秒后进行第 {recovery_attempt + 1} 次视频流恢复", "warning")
                            if not _wait_active(ctx, delay,
                                                next_action=f"恢复{label}视频流并继续浏览"):
                                raise FlowStop
                            continue
                if url in seen or already_processed(ctx.db_path, ctx.account_id, url):
                    skip_streak += 1
                    if skip_streak >= 15:
                        delay = RECOVERY_BACKOFF[min(skip_streak - 15, len(RECOVERY_BACKOFF) - 1)]
                        runner._log(ctx, f"连续 {skip_streak} 条视频已处理；任务保持运行，"
                                         f"{delay} 秒后重新进入配置的信息流继续寻找可执行视频", "warning")
                        if not _wait_active(ctx, delay,
                                            next_action=f"在{label}中继续寻找未处理视频"):
                            raise FlowStop
                        page = browser_nav({"type": "read-source-videos", "source": entry["source"],
                            "keyword": "", "limit": 1, "runId": ctx.task_id})
                        if ctx.stop.is_set():
                            raise FlowStop
                        items = [i for i in (page.get("items", []) if isinstance(page, dict) else [])
                                 if isinstance(i, dict) and valid_video(i.get("url"))]
                        if isinstance(page, dict) and page.get("ok") and items:
                            feed_nav["entered"] = True
                            fetch_fail = 0
                            url, candidate = items[0]["url"], {
                                **items[0], "videoUrl": items[0]["url"], "keyword": label}
                        else:
                            entry_fail += 1
                            runner._log(ctx, f"重新进入{label}暂未成功；保留任务继续恢复", "warning")
                        continue
                    runner._log(ctx, "这条视频已经处理过（已主评或结果待核验），快速滑过")
                    seen.add(url)
                    continue
                skip_streak = 0
                fetch_fail = 0
                if not feed_nav["entered"]:
                    open_video(url, candidate)
                    feed_nav["entered"] = True
                if wait_for_next_video:
                    if not candidate.get("isAd"):
                        wait_for_next_video = False
                        if not wait_between_videos():
                            break
                comments_page = browse(url, candidate, visited + 1)
                if comments_page is skipped_ad:
                    continue
                if comments_page is None:
                    # 预判不符快速划过：真人快滑是连续动作，不等互动间隔
                    if not _wait_active(ctx, random.randint(1, 3),
                                        next_action="快速滑过当前视频并继续浏览"):
                        break
                    continue
                try:
                    perform_plan(url, candidate["keyword"], comments_page, candidate)
                except GenerationSkipped:
                    # 本条没有生成出可执行动作：视频照常计入已浏览，继续按间隔刷下一条。
                    ctx.stats["searchedVideos"] = max(ctx.stats.get("searchedVideos", 0), visited)
                    wait_for_next_video = True
                    continue
                ctx.stats["searchedVideos"] = max(ctx.stats.get("searchedVideos", 0), visited)
                wait_for_next_video = True

        wait_for_next_video = False
        for index, candidate in enumerate(candidate_stream()):
            if candidate.get("_feedEntry"):
                run_feed(candidate)
                break
            if attempted >= config["interactionLimit"] or not _boundary(ctx):
                break
            url = candidate["videoUrl"]
            if url in seen:
                continue
            if (source == "search" and not nav["entered"] and
                    config.get("skipAdVideos", True) and candidate.get("isAd") is True):
                seen.add(url)
                ctx.stats["skippedAds"] = ctx.stats.get("skippedAds", 0) + 1
                runner._action(ctx, "搜索结果标记为广告，直接跳过，不打开视频")
                runner._log(ctx, f"视频「{str(candidate.get('title') or '')[:60]}」搜索结果已标记为广告，未打开、未停留")
                continue

            entered_by_next = False
            if nav["canNext"] and nav["entered"] and candidate["keyword"] == nav["keyword"]:
                step = browser_nav({"type": "next-video", "runId": ctx.task_id})
                if ctx.stop.is_set():
                    break
                if isinstance(step, dict) and step.get("ok") and valid_video(step.get("videoUrl")):
                    url = step["videoUrl"]
                    candidate = {**candidate, "videoUrl": url, "url": url,
                                 "title": step.get("title") or candidate.get("title") or "",
                                 "isAd": (step.get("isAd") is True if "isAd" in step
                                          else candidate.get("isAd") is True)}
                    entered_by_next = True
                else:
                    # 没有「下一个」控件或被验证拦住：退回按地址打开，宁慢不乱。
                    # （只有切换成功、画面确实换了才认下一条；地址变不变不影响，
                    #  抖音的详情浮层本来就不改地址，页面身份由壳按画面里的视频核对。）
                    if source == "search":
                        runner._log(ctx, "当前页面未能切换到下一条；正在恢复同一关键词搜索结果", "warning")
                        url, candidate = restore_search_result(candidate, preferred_url=url)
                        entered_by_next = True
                    else:
                        nav["canNext"] = False
                        step_error = step.get("error") if isinstance(step, dict) else "恢复命令无响应"
                        runner._log(ctx, "「下一个」没切成新视频（"
                                    + str(step_error or "未检测到新视频") + "），改为按地址打开下一条", "warning")
            if url in seen:
                continue
            if already_processed(ctx.db_path, ctx.account_id, url):
                runner._log(ctx, "这条视频已经处理过（已主评或结果待核验），跳过")
                continue
            if not entered_by_next:
                if source == "search":
                    try:
                        open_video(url, candidate, preserve_search=True)
                    except FlowStop:
                        raise
                    except Exception:
                        url, candidate = restore_search_result(candidate, preferred_url=url)
                else:
                    open_video(url, candidate)
            if wait_for_next_video:
                if not candidate.get("isAd"):
                    wait_for_next_video = False
                    if not wait_between_videos():
                        break
            comments_page = browse(url, candidate, index + 1)
            if comments_page is skipped_ad:
                continue
            if comments_page is None:
                # 预判不符快速划过：不等互动间隔
                if not _wait_active(ctx, random.randint(1, 3),
                                    next_action="快速滑过当前视频并继续浏览"):
                    break
                continue
            try:
                perform_plan(url, candidate["keyword"], comments_page, candidate)
            except GenerationSkipped:
                wait_for_next_video = True
                continue
            if attempted >= config["interactionLimit"]:
                break
            # 下一条已确认进入后再倒计时；快速跳过的广告/无效视频不触发间隔。
            wait_for_next_video = True
        if ctx.stop.is_set():
            status, message, reason = "stopped", "已停止，剩余视频未继续浏览", "manual_stop"
        else:
            status, reason = "completed", "completed"
            message = f"顺序浏览完成：浏览 {visited} 个视频，成功评论 {ctx.stats.get('published', 0)} 条"
    except FlowStop:
        # 停止/暂停打断浏览循环：走正常收尾（stopped 状态由 ctx.stop 判定），
        # 与旧代码在各处 break 的行为一致，不算失败。
        pass
    except Exception as exc:
        recorded_action_outcome = "结果待核验" in str(exc) or "动作失败，已停止" in str(exc)
        ctx.stats["failed"] = ctx.stats.get("failed", 0) + (0 if recorded_action_outcome else 1)
        if recorded_action_outcome:
            message = str(exc)
        elif isinstance(exc, (ValueError, GenerationError)):
            # GenerationError 的文案是特意写成「可照着排查」的（HTTP 状态、CF 1010 等），
            # 以前只打类型名，用户只能看到一句「顺序浏览失败（GenerationError）」，
            # 真正该修的模型配置反而被埋掉了。
            message = str(exc) or "AI 生成失败，已停止"
        else:
            message = "顺序浏览失败（" + type(exc).__name__ + "），已停止"
        reason = "stream_failed"
    finally:
        finish()
