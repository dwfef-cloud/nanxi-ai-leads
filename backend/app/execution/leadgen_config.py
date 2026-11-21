"""One contract for the settings page, read-only planning and reviewed execution.

Unknown platform metadata never passes an enabled filter. Unsupported controls
are reported before browser work, rather than being silently ignored.
"""
from __future__ import annotations

import fnmatch
import json
import math
import random
import re
from datetime import datetime, timedelta, timezone

MAX_ACTIONS = 100
MAX_VIDEOS = 200
ACTION_FLAGS = {
    "enableMainVideoLike": "like-video", "enableVideoComment": "post-comment",
    "enableLike": "like-comment", "enableComment": "reply-comment", "enableDM": "send-dm",
    "enableFollow": "follow-user", "enableProfileShare": "share-profile", "enableCommentShare": "share-comment",
}
PENDING = {
    "enableVideoCommentWithoutText": "无文字主评", "enableCommentWithoutText": "无文字回复",
}
SOURCES = {"search": "搜索关键词", "specific": "指定视频", "follow": "关注列表",
           "recommend": "推荐页", "like": "喜欢列表", "hashtag": "话题"}
# 纯标点（不含字母/数字/汉字）不是有效关键词
_PUNCT_ONLY = re.compile(r"^[\W_]+$")


def words(value):
    """Keep every keyword. Quotes preserve a phrase containing spaces.

    分隔符与 leadgen_pools 保持同一套：中英文逗号、顿号、分号、斜杠、竖线、
    任意空白。用户习惯写「多少钱 / 怎么做 / 怎么联系」，旧实现不认斜杠，
    会把「/」当成一个独立关键词（真机 2026-11-19 因此只搜到一个词）。
    """
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = []
        for match in re.finditer(r'"(?:\\.|[^"\\])*"|“[^”\n]+”|[^,，、;；/|｜\s"]+', str(value or "")):
            item = match[0]
            if item.startswith('"'):
                try:
                    item = json.loads(item)
                except ValueError:
                    item = item[1:-1]
            elif item.startswith('“') and item.endswith('”'):
                item = item[1:-1]
            items.append(item.strip())
    return list(dict.fromkeys(v for v in items if v and not _PUNCT_ONLY.match(v)))


def integer(value, label, low=0, high=MAX_ACTIONS):
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(label + "必须是整数") from None
    if isinstance(value, bool) or not math.isfinite(number) or not number.is_integer() or not low <= number <= high:
        raise ValueError(f"{label}必须是 {low} 到 {high} 的整数")
    return int(number)


BOUND_LABELS = {"video": "视频数量", "interactionLimit": "互动总上限", "stay": "视频停留秒数",
                "interval": "互动间隔秒数", "like": "每视频评论点赞数", "comment": "每视频回复数",
                "dmLimit": "私信上限", "followLimit": "关注上限", "profileShareLimit": "主页分享上限",
                "commentShare": "每视频评论分享数", "followDmDelay": "关注/私信后停留秒数"}


def bounds(config, prefix, default, low=0, high=MAX_ACTIONS):
    label = BOUND_LABELS.get(prefix, prefix)
    left = integer(config.get(prefix + "Min", default[0]), label + "最小值", low, high)
    right = integer(config.get(prefix + "Max", default[1]), label + "最大值", low, high)
    if left > right:
        raise ValueError(label + "最小值不能大于最大值")
    return left, right


def video_urls(value):
    ids = re.findall(r"https://www\.douyin\.com/video/(\d{6,})(?!\d)", str(value or ""))
    return list(dict.fromkeys("https://www.douyin.com/video/" + v for v in ids))


def scope_duration(config, actions):
    duration = (600 + len(actions) * 210
                + len({a["videoUrl"] for a in actions}) * config.get("stayMax", 0)
                + max(0, len(actions) - 1) * config.get("intervalMax", 0)
                + sum(a["type"] in {"follow-user", "send-dm"} for a in actions) * config.get("followDmDelayMax", 0))
    if duration > 86400:
        raise ValueError("本次动作和间隔预计超过24小时，请减少数量或拆分任务")
    return duration


def normalize(payload, *, sample=True, account_id=""):
    config = dict(payload)
    for flag in {*ACTION_FLAGS, *PENDING, "aiReplyMode", "enablePersonaVideoFilter", "videoCommentAllowOtherAccounts",
                 "enableVideoCommentImage", "enableCommentImage", "enableVideoCommentExpression", "enableCommentExpression",
                 "enableVideoCommentMention", "enableCommentMention", "ageFilterEnabled", "profileShareLimitEnabled", "profileShareSaveQr",
                 "enableWarmup", "commentOnProfileFirstWork", "enableProfileFirstComment",
                 "skipAdVideos",
                 "profileFirstFilterPinned", "profileFirstIgnorePinnedIfOnlyOne",
                 "profileFirstAgeFilterEnabled", "profileFirstRandomBrowseBeforeComment"}:
        if flag in config and type(config[flag]) is not bool:
            raise ValueError("开关设置格式无效：" + flag)
    sources = config.get("videoSources") or ["search"]
    if not isinstance(sources, list) or len(sources) != 1 or sources[0] not in SOURCES:
        raise ValueError("请选择一个有效的视频入口")
    config["videoSources"] = sources
    config["skipAdVideos"] = config.get("skipAdVideos", True)
    keyword_value = config.get("keywords")
    config["keywords"] = ([keyword_value.strip()] if config.get("useGlobalKeywordPool") and isinstance(keyword_value, str)
                           and keyword_value.strip() else words(keyword_value))
    # 账号专属关键词：设置页「专属关键词（留空则用全局）」以前只在校验兜底里出现，
    # 实际搜索永远走全局词 —— 填了专属词的账号会被静默忽略；全局留空只填专属词时
    # 又会在 build 里报「此账号尚未领取搜索关键词」。这里按执行账号解析一次。
    if account_id:
        assigned = config.get("accountKeywords") or {}
        if isinstance(assigned, dict):
            own = words(assigned.get(account_id))
            if own:
                config["keywords"] = own
    if sources == ["search"] and not config["keywords"]:
        # The page may validate before assigning a keyword from the shared pool.
        assigned = config.get("accountKeywords") or {}
        if not isinstance(assigned, dict):
            raise ValueError("账号专属关键词格式无效")
        if not any(words(v) for v in assigned.values()):
            raise ValueError("请填写全局或账号专属搜索关键词")
    if sources == ["specific"] and not video_urls(config.get("specifiedUrls")):
        raise ValueError("请填写完整的抖音视频链接；短链接需先在页面解析为完整视频地址")
    if len(config["keywords"]) > 200:
        raise ValueError("单次最多配置200个关键词，请拆分任务；不会截断词库")
    for field in ("intentionKeywords", "excludeCommentKeywords", "includeTitleKeywords", "excludeTitleKeywords", "excludeAuthorAccounts"):
        config[field] = words(config.get(field))
    for prefix, default, low, high in (
        ("video", (1, 5), 1, MAX_VIDEOS), ("interactionLimit", (3, 3), 1, MAX_ACTIONS),
        # 停留默认 8-15 秒：公开行为研究口径（单条视频平均观看 8.2 秒、
        # 50.35% 用户完整观看 1-10 秒、仅 3.69% 停 15 秒以上），设置页仍可覆盖。
        ("stay", (8, 15), 0, 3600), ("interval", (0, 0), 0, 86400),
        ("like", (1, 1), 0, MAX_ACTIONS), ("comment", (1, 1), 0, MAX_ACTIONS),
        ("dmLimit", (1, 1), 0, MAX_ACTIONS),
        ("followLimit", (10, 20), 0, MAX_ACTIONS), ("profileShareLimit", (10, 20), 0, MAX_ACTIONS),
        ("commentShare", (1, 3), 0, MAX_ACTIONS), ("followDmDelay", (0, 0), 0, 3600),
    ):
        lo, hi = bounds(config, prefix, default, low, high)
        config[prefix + "Min"], config[prefix + "Max"] = lo, hi
        if prefix in {"interactionLimit", "dmLimit", "followLimit", "profileShareLimit"}:
            config[prefix] = integer(config[prefix], BOUND_LABELS.get(prefix, prefix), low, high) if prefix in config else (
                random.randint(lo, hi) if sample else hi)
    config["mainVideoLikePercent"] = integer(config.get("mainVideoLikePercent", 20), "视频点赞概率", 0, 100)
    # 养护 / 主页首作（2026-11-18 接通）：数量与概率字段与页面默认值一致
    warm_lo = integer(config.get("warmupMin", 1), "养护点赞/收藏数量下限", 1, 5)
    warm_hi = integer(config.get("warmupMax", 1), "养护点赞/收藏数量上限", 1, 5)
    if warm_lo > warm_hi:
        raise ValueError("养护点赞/收藏数量下限不能大于上限")
    config["warmupMin"], config["warmupMax"] = warm_lo, warm_hi
    for field, label, default in (("profileFirstWorkLikePercent", "首作点赞概率", 10),
                                  ("profileFirstWorkCollectPercent", "首作收藏概率", 10)):
        value = config.get(field)
        config[field] = default if value in (None, "") else integer(value, label, 0, 100)
    gender = config.get("profileFirstGenderFilter", "all")
    if gender not in {"all", "male", "female", "male_unknown", "female_unknown"}:
        raise ValueError("主页首作性别筛选无效")
    config["profileFirstGenderFilter"] = gender
    if config.get("profileFirstCommentFallbackMode", "reply") not in {"reply", "skip"}:
        raise ValueError("主页首作评论兜底方式无效")
    config["profileFirstCommentFallbackMode"] = config.get("profileFirstCommentFallbackMode", "reply")
    age_lo = integer(config.get("profileFirstAgeMin", 0), "主页首作年龄下限", 0, 120)
    age_hi = integer(config.get("profileFirstAgeMax", 50), "主页首作年龄上限", 0, 120)
    if age_lo > age_hi:
        raise ValueError("主页首作年龄下限不能大于上限")
    config["profileFirstAgeMin"], config["profileFirstAgeMax"] = age_lo, age_hi
    for field in ("minVideoLike", "minVideoComment", "minVideoCollect", "minVideoShare"):
        config[field] = integer(config.get(field, 0), field, 0, 2**53 - 1)
    mode = config.get("commentIntentMatchMode", "keyword")
    if mode not in {"ai", "keyword", "all"}:
        raise ValueError("评论意向模式必须是AI研判、关键词匹配或全部")
    config["commentIntentMatchMode"] = mode
    for field in ("videoCommentMode", "commentReplyMode"):
        if config.get(field, "ai") not in {"ai", "custom"}:
            raise ValueError("请选择AI生成或自定义模板")
    if not any(config.get(flag) is True for flag in ACTION_FLAGS):
        raise ValueError("请至少开启一项视频赞、主评、评论赞、回复、私信、关注或分享")
    for flag, field in (("enableProfileShare", "profileShareFriends"), ("enableCommentShare", "commentShareFriends")):
        value = config.get(field, "")
        if not isinstance(value, str):
            raise ValueError("分享接收人需要一行一个昵称或群名")
        config[field] = list(dict.fromkeys(v.strip() for v in value.splitlines() if v.strip()))
        if config.get(flag) and (not config[field] or len(config[field]) > 50 or any(len(v) > 100 for v in config[field])):
            raise ValueError("请填写1到50个分享接收人，一行一个昵称或群名")
    config["targetGender"] = config.get("targetGender", "all")
    if config["targetGender"] not in {"all", "male", "female", "male_unknown", "female_unknown"}:
        raise ValueError("请选择有效的性别筛选")
    if config.get("ageFilterEnabled"):
        for key in ("targetAgeMin", "targetAgeMax"):
            config[key] = integer(config.get(key, 0 if key.endswith("Min") else 100), "年龄", 0, 120)
        if config["targetAgeMin"] > config["targetAgeMax"]:
            raise ValueError("年龄最小值不能大于最大值")
    from app.execution.leadgen_media import library
    for enabled, prefix in (("enableVideoComment", "videoComment"), ("enableComment", "comment")):
        config.pop("_" + prefix + "Images", None)
        config.pop("_" + prefix + "Mentions", None)
        config[prefix + "AttachmentPercent"] = integer(config.get(prefix + "AttachmentPercent", 20), "附图概率", 0, 100)
        cap = prefix[0].upper() + prefix[1:]
        if config.get("enable" + cap + "Image") and config.get("enable" + cap + "Expression"):
            raise ValueError("GIF与附图只能选择一种，请重新选择附件模式")
        config[prefix + "ExpressionCount"] = integer(config.get(prefix + "ExpressionCount", 3), "轮询表情包范围", 1, 8)
        config[prefix + "MentionPercent"] = integer(config.get(prefix + "MentionPercent", 100), "@账号概率", 0, 100)
        config[prefix + "MentionPosition"] = config.get(prefix + "MentionPosition", "before")
        if config[prefix + "MentionPosition"] not in {"before", "after", "random"}:
            raise ValueError("@账号位置只能是正文前、正文后或随机前后")
        if config.get(enabled) and config.get("enable" + cap + "Mention"):
            names = config.get(prefix + "MentionNicknames", "")
            if not isinstance(names, str):
                raise ValueError("@账号需要填写昵称或抖音号")
            names = list(dict.fromkeys(v.strip().lstrip("@").strip() for v in re.split(r"[,，\r\n]+", names) if v.strip()))
            if not 1 <= len(names) <= 10 or any(not v or len(v) > 100 for v in names):
                raise ValueError("@账号请填写1到10个昵称或抖音号，用逗号或换行分隔")
            config["_" + prefix + "Mentions"] = names
        if config.get(enabled) and config.get("enable" + prefix[0].upper() + prefix[1:] + "Image"):
            config["_" + prefix + "Images"] = library(config.get(prefix + "ImagePaths"))
    if config.get("enableProfileShare") and config.get("profileShareSaveQr"):
        key = config.get("profileQrRunKey") or config.get("taskId") or ""
        if not isinstance(key, str) or len(key) > 160 or re.search(r"[\x00-\x1f/\\]", key):
            raise ValueError("二维码运行标识无效，请重新启动任务")
        config["profileQrRunKey"] = key
    # Validate time controls even when no matching result has been returned yet.
    for prefix in ("searchVideoTime", "commentTime", "recommendVideoTime", "hashtagVideoTime"):
        time_window(config, prefix)
    regions = config.get("locationFilterRegions", [])
    if not isinstance(regions, list) or any(not isinstance(v, str) for v in regions):
        raise ValueError("请选择有效的评论地区")
    if config.get("locationFilterMode", "include") not in {"include", "exclude"}:
        raise ValueError("地区筛选模式无效")
    return config


def inspect(payload, *, account_id=""):
    errors, warnings = [], []
    try:
        config = normalize(payload, sample=False, account_id=account_id)
    except ValueError as exc:
        return {"success": False, "errors": [str(exc)], "warnings": [], "summary": []}
    source = config["videoSources"][0]
    if source in {"search", "hashtag"}:
        target_videos = config["videoMax"] * max(1, len(config["keywords"]))
    elif source == "specific":
        target_videos = len(video_urls(config.get("specifiedUrls")))
    else:
        target_videos = config["videoMax"]
    budget_min = integer(config.get("interactionLimitMin", 3), "互动总上限最小值", 1, MAX_ACTIONS)
    budget_max = config["interactionLimitMax"]
    if "interactionLimit" in payload:
        budget_min = integer(payload["interactionLimit"], "互动总上限", 1, MAX_ACTIONS)
        budget_max = budget_min
    assignments = config.get("accountPersonas") or {}
    selected = config.get("selectedAccounts") or []
    pending_persona = (not config.get("accountId") and isinstance(assignments, dict) and isinstance(selected, list)
                       and bool(selected) and all(isinstance(a, str) and isinstance(assignments.get(a), str)
                           and assignments[a] not in {"", "none"} for a in selected))
    if source == "hashtag" and not config["keywords"]:
        errors.append("话题来源需要先填写要采集的话题关键词")
    if source in {"like", "hashtag"}:
        # 网格页（喜欢列表 / 话题页）的卡片正面只有点赞数和标题，平台不给发布时间，
        # 设了视频时间范围必然全判成「发布时间未知」。与其让用户看着 0 条猜，
        # 不如直接说清「这条入口不带时间」。
        window_prefix = "hashtagVideoTime" if source == "hashtag" else "recommendVideoTime"
        if time_window(config, window_prefix) is not None:
            warnings.append(SOURCES[source] + "不提供视频发布时间，视频发布时间筛选不会生效；请关闭时间筛选或改用可读取发布时间的入口")
    included_titles = config.get("includeTitleKeywords") or []
    excluded_titles = config.get("excludeTitleKeywords") or []
    title_overlaps = sorted({
        (included, excluded)
        for included in included_titles
        for excluded in excluded_titles
        if included in excluded or excluded in included
    })
    if title_overlaps:
        overlap_text = "、".join(f"包含词“{inc}”/排除词“{exc}”" for inc, exc in title_overlaps[:5])
        impossible_includes = [inc for inc in included_titles
                               if any(exc in inc for exc in excluded_titles)]
        if included_titles and len(impossible_includes) == len(included_titles):
            errors.append("标题包含词与排除词完全抵消（" + overlap_text + "）；没有标题能同时通过，请调整其中一组词")
        else:
            warnings.append("标题包含词与排除词有重叠（" + overlap_text + "）；命中排除词的视频仍会跳过")
    if budget_min < target_videos:
        warnings.append(f"互动总上限下限为{budget_min}次，低于目标视频数{target_videos}；互动次数可能先用完，导致部分目标视频没有互动。可提高互动上限下限或减少视频/关键词数量")
    for field, label in PENDING.items():
        if field.startswith("enableVideoComment") and not config.get("enableVideoComment"):
            continue
        if field in {"enableCommentImage", "enableCommentExpression", "enableCommentMention", "enableCommentWithoutText"} and not config.get("enableComment"):
            continue
        if config.get(field) is True:
            errors.append(label + "尚未接通此任务入口，请关闭后再启动")
    if config.get("enableWarmup") and not (config.get("enableFollow") or config.get("enableDM")):
        errors.append("养护需要先开启关注或私信，请先开启其一")
    if (config.get("commentOnProfileFirstWork") and config.get("enableProfileFirstComment")
            and config.get("videoCommentMode", "ai") == "custom"):
        first_templates = [s.strip() for s in str(config.get("videoCommentContent") or "").splitlines() if s.strip()]
        if not first_templates or any(not 5 <= len(s.replace("{nickname}", "测试用户")) <= 200 for s in first_templates):
            errors.append("首作评论沿用主评模板：主评模板需一行一条，每条5到200字")
    if config.get("commentOnProfileFirstWork") and config.get("enableProfileFirstComment") \
            and config.get("videoCommentMode", "ai") == "ai" and not config.get("aiReplyMode") and not pending_persona:
        warnings.append("未绑定智能体，首作评论将使用主评自定义模板正文")
    if (config.get("enableWarmup") or config.get("commentOnProfileFirstWork")) and config["interactionLimit"] < 6:
        warnings.append(f"养护与主页首作占用互动总上限，当前上限 {config['interactionLimit']}，"
                        f"建议调高，避免关注/私信被养护动作挤掉")
    if source == "search":
        for field, label in {"searchSort": "官方搜索排序", "searchPublishTime": "官方发布时间",
                             "searchDuration": "官方视频时长", "searchScope": "官方搜索范围",
                             "searchFormat": "官方内容形式"}.items():
            if str(config.get(field, "0")) != "0":
                errors.append("当前桌面执行端暂不支持" + label + "筛选，请恢复“不限/综合排序”；不会忽略此条件执行")
    for enabled, mode, field, label in (
        ("enableVideoComment", "videoCommentMode", "videoCommentContent", "主评"),
        ("enableComment", "commentReplyMode", "commentContent", "回复"),
        ("enableDM", None, "dmContent", "私信"),
    ):
        fallback = mode is not None and config.get(mode, "ai") == "ai" and config.get("aiReplyMode") is False and not pending_persona
        if config.get(enabled) and (mode is None or config.get(mode) == "custom" or fallback):
            templates = [s.strip() for s in str(config.get(field) or "").splitlines() if s.strip()]
            if not templates or any(not 5 <= len(s.replace("{nickname}", "测试用户")) <= 200 for s in templates):
                errors.append(label + "模板需一行一条，每条5到200字")
            if fallback:
                warnings.append(label + "未绑定智能体，本次使用自定义模板正文")
    if config.get("taskMode") == "scrape":
        errors.append("纯采集请使用线索采集模块，此入口执行真实互动")
    if config["commentIntentMatchMode"] == "ai" and not config.get("aiReplyMode") and not pending_persona:
        warnings.append("未绑定智能体，意向识别使用关键词匹配")
    if config.get("enablePersonaVideoFilter") and not config.get("aiReplyMode") and not pending_persona:
        errors.append("人设视频筛选需要绑定智能体，请绑定后启动或关闭此筛选")
    if config.get("useGlobalKeywordPool"):
        warnings.append("本次将按顺序逐个处理此账号已领取的关键词")
    if pending_persona:
        warnings.append("所选账号已设置智能体绑定；启动时逐账号核对智能体是否有效，再生成正文")
    if any(config.get(f) for f in ("enableLike", "enableComment", "enableDM", "enableFollow", "enableProfileShare", "enableCommentShare")):
        warnings.append("每视频读取最多60条当前可定位评论；数量不足或筛选未命中会按实际清单执行")
    if config.get("enableFollow") or config.get("enableProfileShare") or config.get("enableDM"):
        warnings.append("性别、年龄按真实主页核对；性别未知仅在选中“男+未知/女+未知/不限”时通过，年龄未知不通过年龄筛选。已关注用户不会取消关注")
    if config.get("enableProfileShare") or config.get("enableCommentShare"):
        warnings.append("分享使用平台原生名片/原评论，接收人需唯一匹配；找不到或重名时跳过，提交结果不明时停止")
    if any(config[k] > 0 for k in ("minVideoLike", "minVideoComment", "minVideoCollect", "minVideoShare")):
        warnings.append("平台未提供的指标不能视为0或已达标；缺少必需指标的视频会跳过")
    dwell_min = target_videos * config["stayMin"]
    dwell_max = target_videos * config["stayMax"]
    interval_count = max(0, target_videos - 1)
    timing_min = dwell_min + interval_count * config["intervalMin"]
    timing_max = dwell_max + interval_count * config["intervalMax"]
    warnings.append("点击启动即按本配置顺序浏览目标；失败不算成功，结果待核验时停止且不重发")
    for enabled, prefix, label in (("enableVideoComment", "videoComment", "主评"), ("enableComment", "comment", "回复")):
        cap = prefix[0].upper() + prefix[1:]
        if config.get(enabled) and config.get("enable" + cap + "Expression"):
            warnings.append(f"{label}GIF按前{config[prefix + 'ExpressionCount']}张顺序轮询，概率{config[prefix + 'AttachmentPercent']}%；平台表情不足会停止准备")
        if config.get(enabled) and config.get("enable" + cap + "Mention"):
            warnings.append(f"{label}@账号概率{config[prefix + 'MentionPercent']}%；解析全部配置账号，重名需改用准确抖音号")
    if config.get("enableProfileShare") and config.get("profileShareSaveQr"):
        warnings.append("分享前保存目标主页原生二维码；保存失败不发送分享，二维码不计入互动成功数")
    return {"success": not errors, "errors": errors, "warnings": warnings,
            "summary": ["视频来源：" + SOURCES[source], f"关键词：{len(config['keywords'])}个（完整保留）",
                        f"本次目标视频数：{target_videos}（平台不足或筛选未命中时按实际结果记录）",
                        f"互动总上限：{budget_min}–{budget_max}次；视频赞、主评、评论赞、回复、私信、关注、分享共用此上限",
                        f"每条视频停留：{config['stayMin']}–{config['stayMax']}秒（按设置区间执行）；视频间隔：{config['intervalMin']}–{config['intervalMax']}秒",
                        f"停留与视频间隔预计：{timing_min}–{timing_max}秒（不含评论加载/阅读与互动处理时间）"],
            "supportedActions": list(ACTION_FLAGS.values()), "pendingControls": PENDING}


def profile_reason(config, profile):
    if profile.get("private") is not False:
        return "主页私密状态未知或不允许互动"
    gender = config.get("targetGender", "all")
    matches = (gender == "all" or profile.get("gender") == gender.removesuffix("_unknown")
               or gender.endswith("_unknown") and profile.get("gender") is None)
    if not matches:
        return "性别不符合条件或未公开"
    if config.get("ageFilterEnabled"):
        age = profile.get("age")
        if type(age) is not int or not config["targetAgeMin"] <= age <= config["targetAgeMax"]:
            return "年龄不符合条件或未公开"
    return ""


def time_window(config, prefix):
    value = str(config.get(prefix + "Filter", "all"))
    if value in {"all", "0", ""}:
        return None
    if value.isdigit():
        return integer(value, "时间筛选秒数", 1, 365 * 86400)
    if value in {"1y", "1mo", "1w"}:
        return {"1y": 365 * 86400, "1mo": 30 * 86400, "1w": 7 * 86400}[value]
    if value == "custom":
        unit = config.get(prefix + "CustomUnit", "day")
        seconds = {"minute": 60, "hour": 3600, "day": 86400}.get(unit)
        if seconds is None:
            raise ValueError("时间单位必须是分钟、小时或天")
        count = integer(config.get(prefix + "CustomValue"), "自定义时间", 1, 525600)
        if count * seconds > 365 * 86400:
            raise ValueError("自定义时间最长365天")
        return count * seconds
    match = re.fullmatch(r"(\d+)([dhm])", value)
    if not match or not 0 < int(match[1]) * {"d": 86400, "h": 3600, "m": 60}[match[2]] <= 365 * 86400:
        raise ValueError("时间筛选值无效")
    return int(match[1]) * {"d": 86400, "h": 3600, "m": 60}[match[2]]


def age_seconds(item, now=None):
    now = now or datetime.now(timezone.utc)
    value = item.get("publishedAt") or item.get("createTime")
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            stamp = float(value)
            if stamp > 1e12:
                stamp /= 1000
            age = now.timestamp() - stamp
            return age if math.isfinite(age) and age >= 0 else None
        if value:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo:
                return max(0, (now - dt).total_seconds())
    except (ValueError, TypeError, OverflowError):
        pass
    text = str(item.get("publishedText") or "").split("·")[0].strip()
    if text == "刚刚":
        return 0
    match = re.fullmatch(r"(\d+)\s*(分钟|小时|天|周|个月|年)前", text)
    if match:
        return int(match[1]) * {"分钟": 60, "小时": 3600, "天": 86400, "周": 604800,
                               "个月": 30 * 86400, "年": 365 * 86400}[match[2]]
    # 详情浮层没有相对时间时兜底抽到的是绝对日期（2024-11-18 / 11-18）：
    # 平台页面按北京时间展示，这里也按北京时间折算成 UTC 年龄。
    dm = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if dm:
        stamp = datetime(int(dm[1]), int(dm[2]), int(dm[3]), tzinfo=timezone(timedelta(hours=8)))
        age = (now - stamp).total_seconds()
        return age if math.isfinite(age) and age >= 0 else None
    dm = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
    if dm:
        stamp = datetime(now.year, int(dm[1]), int(dm[2]), tzinfo=timezone(timedelta(hours=8)))
        if stamp > now:  # 跨年（11-20 看到 12-25 的视频）：按去年算
            stamp = stamp.replace(year=now.year - 1)
        age = (now - stamp).total_seconds()
        return age if math.isfinite(age) and age >= 0 else None
    # 抖音视频信息区常显示「10月24日」或「2026年10月24日」；日期按
    # 页面展示的北京时间当天零点计算，无年份时未来日期归到上一年。
    dm = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if dm:
        stamp = datetime(int(dm[1]), int(dm[2]), int(dm[3]), tzinfo=timezone(timedelta(hours=8)))
        age = (now - stamp).total_seconds()
        return age if math.isfinite(age) and age >= 0 else None
    dm = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", text)
    if dm:
        month, day = int(dm[1]), int(dm[2])
        stamp = datetime(now.year, month, day, tzinfo=timezone(timedelta(hours=8)))
        if stamp > now:
            stamp = datetime(now.year - 1, month, day, tzinfo=timezone(timedelta(hours=8)))
        age = (now - stamp).total_seconds()
        return age if math.isfinite(age) and age >= 0 else None
    return None


def count_of(value):
    if isinstance(value, bool) or value is None:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([万亿wW]?)\s*", str(value))
    return float(match[1]) * {"": 1, "万": 10000, "w": 10000, "W": 10000, "亿": 1e8}[match[2]] if match else None


def author_excluded(config, name):
    return any(fnmatch.fnmatchcase(str(name or ""), pattern) for pattern in config["excludeAuthorAccounts"])


def video_reason(config, item):
    title = str(item.get("title") or "")
    if not title:
        return "未读到视频标题"
    if any(w in title for w in config["excludeTitleKeywords"]):
        return "命中排除标题词"
    if config["includeTitleKeywords"] and not any(w in title for w in config["includeTitleKeywords"]):
        return "未命中标题包含词"
    if author_excluded(config, item.get("author")):
        return "命中排除作者"
    prefix = {"search": "searchVideoTime", "hashtag": "hashtagVideoTime"}.get(
        config["videoSources"][0], "recommendVideoTime")
    window = time_window(config, prefix)
    if window is not None:
        age = age_seconds(item)
        if age is None or age > window:
            return "视频发布时间未知" if age is None else "视频发布时间超出范围"
    labels = {"minVideoLike": "最少点赞数", "minVideoComment": "最少评论数",
              "minVideoCollect": "最少收藏数", "minVideoShare": "最少转发数"}
    for field, keys in {"minVideoLike": ("likeCount", "likeText"), "minVideoComment": ("commentCount",),
                        "minVideoCollect": ("collectCount",), "minVideoShare": ("shareCount",)}.items():
        if config[field]:
            number = next((count_of(item[k]) for k in keys if item.get(k) is not None), None)
            if number is None or number < config[field]:
                return labels[field] + ("：平台未提供指标" if number is None else "：未达到阈值")
    return ""


def comment_reason(config, item, self_names):
    if not item.get("nickname") or not item.get("text") or not re.fullmatch(r"\d{6,}", str(item.get("commentId") or "")):
        return "评论缺少可靠定位信息"
    if item["nickname"] in self_names:
        return "本人或本软件账号"
    if author_excluded(config, item["nickname"]):
        return "命中排除作者"
    excluded = [w for w in config["excludeCommentKeywords"] if w in item["text"]]
    if excluded:
        return "排除评论词：" + "、".join(excluded)
    window = time_window(config, "commentTime")
    if window is not None:
        age = age_seconds(item)
        if age is None or age > window:
            return "评论时间未知" if age is None else "评论时间超出范围"
    regions = config.get("locationFilterRegions") or []
    if regions:
        region = str(item.get("region") or "")
        if not region and "·" in str(item.get("publishedText") or ""):
            region = item["publishedText"].split("·", 1)[1].strip()
        if not region:
            return "评论属地未知"
        hit = any(r == region or r.removesuffix("省").removesuffix("市") == region for r in regions)
        if config.get("locationFilterMode", "include") == "include" and not hit:
            return "评论属地未知" if not region else "评论属地不在包含地区"
        if config.get("locationFilterMode") == "exclude" and hit:
            return "评论属地在排除地区"
    return ""


def template(value, nickname=""):
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("自定义模板为空")
    return random.choice(lines).replace("{nickname}", nickname)
