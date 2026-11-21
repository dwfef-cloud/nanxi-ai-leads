"""Build an exact, bounded action list. This module never sends platform writes."""
from __future__ import annotations

import random
import json
import re
import sqlite3

from app import comment_tree
from app.execution import leadgen_config as settings
from app.execution.persistent_gate import PersistentWriteSafetyGate
from app.runtime import browser

# 壳回传的诊断里，只有这些字段是「用户能对账的计数或结论」。
# 页面原文、选择器清单、DOM 片段一律不外传：既避免把页面内容带到界面，
# 也保证诊断信息不会反过来变成一堆噪音。
_DIAG_KEYS = ("items", "count", "videoLinks", "cards", "waitedMs", "tries",
              "titleMissing", "titleSource", "emptyHint", "verifyHint", "matched",
              "pageVerified")


def diag_brief(value):
    """把壳的 diag 收窄成可对账的计数，不保留页面原文。"""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in _DIAG_KEYS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, bool) or isinstance(item, (int, float)):
            out[key] = item
        elif isinstance(item, str):
            out[key] = item[:40]
    return out


def diag_text(value):
    """把 diag 翻成一句人话，接在错误原因后面。"""
    data = value if isinstance(value, dict) else {}
    bits = []
    if isinstance(data.get("items"), (int, float)) and "videoLinks" not in data:
        bits.append(f"壳解析出 {int(data['items'])} 条")
    if isinstance(data.get("videoLinks"), (int, float)):
        bits.append(f"页面视频链接 {int(data['videoLinks'])} 个")
    if isinstance(data.get("cards"), (int, float)):
        bits.append(f"卡片 {int(data['cards'])} 个")
    if isinstance(data.get("waitedMs"), (int, float)) and data["waitedMs"] > 0:
        bits.append(f"等待 {round(float(data['waitedMs']) / 1000)} 秒")
    if data.get("titleMissing"):
        bits.append(f"重试 {int(data.get('tries') or 0)} 次仍读不到标题")
    elif data.get("pageVerified") is False:
        bits.append("页面地址与目标视频不一致")
    elif data.get("titleSource"):
        bits.append(f"标题取自 {data['titleSource']}")
    if data.get("emptyHint"):
        bits.append("平台提示" + str(data["emptyHint"])[:20])
    return "，".join(bits)


def _skipped_summary(skipped, limit=3):
    """把跳过原因按次数聚合，只报前几条 —— 用户要的是「为什么不动」，不是全量清单。"""
    counts = {}
    for item in skipped:
        reason = str((item or {}).get("reason") or "").strip()
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return "、".join(f"{reason}×{n}" for reason, n in ordered)


def _empty_reason(config, candidates, searched, inspected, skipped, totals):
    """准备阶段一条动作都没生成时的原因说明。

    以前固定写「请查看筛选、历史去重和AI判断记录」，可用户既看不到筛选过程，
    也看不到壳回传的诊断 —— 搜索明明返回了 26 个视频链接，界面只说「没有动作」。
    这里按「卡在哪一步」分别给结论，并带上壳回传的计数。
    """
    if not candidates:
        if config["videoSources"] != ["specific"]:
            lines = []
            for item in searched:
                note = f"（{item['note']}）" if item.get("note") else ""
                brief = diag_text(item.get("diag"))
                lines.append(f"「{item['keyword']}」{item['items']} 条{note}"
                             + (f"[{brief}]" if brief else ""))
            label = ("搜索" if config["videoSources"] == ["search"]
                     else settings.SOURCES.get(config["videoSources"][0], "视频入口"))
            head = (f"{label}没有拿到可用视频：" + "；".join(lines) if lines
                    else f"本轮没有读取{label}")
            return head + "。未发送"
        parts = []
        for item in inspected:
            if item.get("title"):
                parts.append("页面已读出标题但未通过后续条件")
                continue
            brief = diag_text(item.get("diag"))
            parts.append("页面已核对为目标视频，但读不到视频标题"
                         + (f"（{brief}）" if brief else ""))
        head = ("指定视频没有读取成功：" + "；".join(dict.fromkeys(parts)) if parts
                else "没有可用的指定视频地址")
        return head + "。未发送"
    reasons = _skipped_summary(skipped)
    stats = (f"读到 {len(candidates)} 个视频候选、{totals['commentsRead']} 条评论，"
             f"筛出 {totals['selectedVideos']} 个可用视频")
    detail = ""
    if "未读到视频标题" in reasons:
        # 标题缺失是「页面读不出来」，不是「筛选/去重/AI 判掉了」——必须区分，
        # 否则用户会去改筛选条件，怎么改都没用。
        briefs = [diag_text(item.get("diag")) for item in inspected if item.get("diag")]
        briefs = [b for b in dict.fromkeys(briefs) if b]
        detail = "（视频页已确认打开，但标题读不到" + ("：" + "；".join(briefs) if briefs else "") + "）"
    return (stats + (f"，但没有一条能执行：{reasons}" if reasons else "，但没有一条能执行")
            + detail + "。未发送")


def build(db_path, payload, account, *, check, progress, comment_cache=None):
    from app.execution.live_leadgen import generate, classify_intent, evidence
    from app.execution.live_workflow import action_key, checked_text, valid_video
    report = settings.inspect(payload, account_id=account["id"])
    if not report["success"]:
        raise ValueError("；".join(report["errors"]))
    config = settings.normalize(payload, account_id=account["id"])
    if not config["keywords"] and config["videoSources"] == ["search"]:
        raise ValueError("此账号尚未领取搜索关键词")
    task_id = payload.get("leadgenTaskId") or payload["taskId"]
    limit = config["interactionLimit"]
    dm_left = config["dmLimit"]
    follow_left = config["followLimit"]
    profile_share_left = config["profileShareLimit"] if config.get("profileShareLimitEnabled") else limit
    profiles, receiver_cursor = {}, {"share-profile": 0, "share-comment": 0}
    sticker_cursor = {"videoComment": 0, "comment": 0}
    actions, skipped, candidates = [], [], []
    searched, inspected = [], []   # 入口两步的真实回执，失败时用来定位「卡在哪」
    gate = PersistentWriteSafetyGate(db_path, namespace="live")
    planned = set()
    totals = {"searchedVideos": 0, "selectedVideos": 0, "commentsRead": 0, "leadsTotal": 0}

    def record(action, candidate, status, reason, text=""):
        evidence(db_path, task_id, action=action, keyword=candidate.get("keyword", ""),
                 title=candidate.get("title", ""), url=candidate.get("videoUrl", ""),
                 text=text, status=status, code=reason, account_id=account["id"], run_id=payload["taskId"])

    def read(command):
        check()
        result = browser.execute({**command, "nickname": account["nickname"], "runId": payload["taskId"]}, db_path=db_path,
                                 account_id=account["id"], engine="shell", stop_check=check.stopped)
        check()
        if not result.get("ok"):
            if result.get("needVerify"):
                raise ValueError("平台需要人工验证，采集窗口已自动弹出，完成验证后重新准备；尚未发送")
            brief = diag_text(result.get("diag"))
            raise ValueError((result.get("error") or "读取平台页面失败，尚未发送")
                             + (f"（{brief}）" if brief else ""))
        return result

    if config["videoSources"] == ["search"]:
        for keyword in config["keywords"]:
            progress("正在搜索关键词：" + keyword + "，尚未发送")
            result = read({"type": "search-videos", "keyword": keyword, "limit": config["videoMax"]})
            usable = [item for item in result.get("items", [])[:config["videoMax"]]
                      if isinstance(item, dict) and valid_video(item.get("url"))]
            searched.append({"keyword": keyword, "items": len(usable),
                             "note": str(result.get("note") or "")[:40],
                             "diag": diag_brief(result.get("diag"))})
            candidates.extend({**item, "videoUrl": item["url"], "keyword": keyword} for item in usable)
    elif config["videoSources"] == ["specific"]:
        for url in settings.video_urls(config.get("specifiedUrls")):
            progress("正在读取视频信息，尚未发送")
            result = read({"type": "inspect-video", "videoUrl": url})
            item = result.get("item") or {}
            # 单遍式流程透传的 feed 卡片信息（_prefillCandidate）：详情浮层经常读不到
            # 发布时间，inspect 没提供的字段用预填值补上，不要拿空值把好数据擦掉。
            prefill = payload.get("_prefillCandidate") or {}
            merged = {k: v for k, v in prefill.items()
                      if k not in ("url", "videoUrl") and v not in (None, "")}
            item = {**merged, **{k: v for k, v in item.items() if v not in (None, "")},
                    "videoUrl": url}
            inspected.append({"title": bool(str(item.get("title") or "").strip()),
                              "diag": diag_brief(result.get("diag"))})
            candidates.append({**item,
                               "keyword": str(payload.get("_sourceKeyword") or "指定视频")})
    else:
        # 关注流 / 推荐流 / 喜欢列表 / 话题页：同样先拿到「候选视频清单」，
        # 后面的筛选、去重、评论读取与搜索来源完全一致，入口只是换了一层。
        source = config["videoSources"][0]
        label = settings.SOURCES.get(source, "视频入口")
        for keyword in (config["keywords"] if source == "hashtag" else [""]):
            progress(f"正在读取{label}" + (f"：{keyword}" if keyword else "") + "，尚未发送")
            result = read({"type": "read-source-videos", "source": source,
                           "keyword": keyword, "limit": config["videoMax"]})
            usable = [item for item in result.get("items", [])[:config["videoMax"]]
                      if isinstance(item, dict) and valid_video(item.get("url"))]
            searched.append({"keyword": keyword or label, "items": len(usable),
                             "note": str(result.get("note") or "")[:40],
                             "diag": diag_brief(result.get("diag"))})
            candidates.extend({**item, "videoUrl": item["url"],
                               "keyword": keyword or label} for item in usable)
    totals["searchedVideos"] = len({item["videoUrl"] for item in candidates})
    seen = set()

    def add(action):
        key = action_key(account["id"], action)
        if key in planned or not gate.can_attempt(key)[0]:
            skipped.append({"type": action["type"], "reason": "已发送、已选择或结果待核验"})
            return False
        if len(actions) >= limit:
            return False
        planned.add(key)
        actions.append(action)
        return True

    def draft(kind, candidate, source=None):
        check()
        fields = {"post-comment": ("videoCommentContent", "videoCommentMode", "videoCommentUseRandomSuffix"),
                  "reply-comment": ("commentContent", "commentReplyMode", "commentUseRandomSuffix"),
                  "send-dm": ("dmContent", None, "dmUseRandomSuffix")}
        field, mode, suffix = fields[kind]
        if mode is None or config.get(mode) == "custom" or config.get("aiReplyMode") is False:
            text = settings.template(config.get(field), (source or {}).get("nickname", ""))
        else:
            generated = generate(db_path, config, {"title": candidate.get("title", ""),
                "discussionContext": candidate.get("discussionContext", ""),
                **({"nickname": source["nickname"], "comment": source["text"]} if source else {})})
            check()
            if not generated.get("ok"):
                record("generate-comment", candidate, "skipped" if generated.get("code") == "not_suitable" else "failed",
                       generated.get("reason") or "AI未生成有效正文")
                if generated.get("code") == "not_suitable":
                    # 2026-11-20 真机：AI 对每条视频都判「话题不相关」→ 动作全部清空，
                    # 用户在实况里只看到视频一条条刷过去、任何互动都没有，
                    # 完全看不出是 AI 在拒绝。这里把拒绝原因打进任务日志。
                    label = {"post-comment": "评论", "reply-comment": "回复", "send-dm": "私信"}.get(kind, kind)
                    progress("AI 判断本视频不适合" + label + "：" + str(generated.get("reason") or "未给出原因")
                             + "；该视频的评论/回复/私信类动作已跳过")
                    return None
                raise ValueError(generated.get("reason") or "AI未生成有效正文")
            text = generated["text"]
        if config.get(suffix):
            text += random.choice([" 🙂", " 👍", " 😊"])
        checked_text(text)
        action = {"type": kind, "videoUrl": candidate["videoUrl"], "keyword": candidate["keyword"],
                "title": candidate.get("title", ""), "text": text,
                **({"commentId": source["commentId"], "commentAuthor": source["nickname"],
                    "sourceText": source["text"], "recipientId": source.get("userId", "")} if source else {})}
        prefix = {"post-comment": "videoComment", "reply-comment": "comment"}.get(kind)
        if prefix and config.get("_" + prefix + "Images") and random.randint(1, 100) <= config[prefix + "AttachmentPercent"]:
            action.update(random.choice(config["_" + prefix + "Images"]))
        if prefix:
            cap = prefix[0].upper() + prefix[1:]
            gif = config.get("enable" + cap + "Expression") and random.randint(1, 100) <= config[prefix + "AttachmentPercent"]
            names = config.get("_" + prefix + "Mentions", []) if random.randint(1, 100) <= config[prefix + "MentionPercent"] else []
            if gif or names:
                extra = read({"type": "inspect-comment-extras", "videoUrl": action["videoUrl"],
                              "mentionQueries": names, "stickerCount": config[prefix + "ExpressionCount"] if gif else 0})
                if gif:
                    stickers = extra.get("stickers", [])
                    count = config[prefix + "ExpressionCount"]
                    if len(stickers) < count or any(not isinstance(s, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", str(s.get("id", "")))
                            or not isinstance(s.get("name"), str) or not s["name"] or len(s["name"]) > 100
                            or not re.fullmatch(r"https://[^\s]{1,1500}", str(s.get("url", ""))) for s in stickers[:count]):
                        raise ValueError("平台GIF表情包不足配置数量或标识无法核对，请调整轮询范围；未发送")
                    sticker = stickers[sticker_cursor[prefix] % count]
                    sticker_cursor[prefix] += 1
                    action.update(stickerId=sticker["id"], stickerName=sticker["name"], stickerUrl=sticker["url"])
                if names:
                    targets = []
                    for name in names:
                        found = [m for m in extra.get("mentions", []) if isinstance(m, dict) and m.get("query") == name]
                        if len(found) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{6,160}", str(found[0].get("id", ""))) or not isinstance(found[0].get("name"), str) or not found[0]["name"] or len(found[0]["name"]) > 100:
                            raise ValueError("@账号未找到唯一的平台用户：" + name + "；请使用准确抖音号，未发送")
                        if not any(m["id"] == found[0]["id"] for m in targets):
                            targets.append(found[0])
                    mentions = " ".join("@" + m["name"] for m in targets)
                    position = config[prefix + "MentionPosition"]
                    if position == "random":
                        # Resolve once while preparing the reviewed action. The
                        # confirmed text and signed position must never reroll.
                        position = random.choice(("before", "after"))
                    action.update(mentionTargets=json.dumps(targets, ensure_ascii=False, separators=(",", ":")),
                                  mentionPosition=position, commentBody=text)
                    action["text"] = mentions + " " + text if position == "before" else text + " " + mentions
                    checked_text(action["text"])
        return action

    def profile_ok(target, candidate, kind):
        uid = target.get("userId", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,160}", str(uid)):
            skipped.append({"type": kind, "reason": "评论没有可靠的用户主页标识"})
            return False
        if uid not in profiles:
            page = read({"type": "inspect-profile", "recipientId": uid, "commentAuthor": target["nickname"]})
            profiles[uid] = page.get("item", {})
        profile = profiles[uid]
        reason = ("主页用户与评论作者不一致" if profile.get("userId") != uid or profile.get("nickname") != target["nickname"]
                  else settings.profile_reason(config, profile))
        if reason:
            skipped.append({"type": kind, "reason": reason})
        return not reason

    def share(kind, target, base):
        field = "profileShareFriends" if kind == "share-profile" else "commentShareFriends"
        names = config[field]
        start = receiver_cursor[kind] % len(names)
        ordered = names[start:] + names[:start]
        action = {"type": kind, **base, "text": "", "commentId": target["commentId"] if kind == "share-comment" else "",
                  "commentAuthor": target["nickname"], "recipientId": target.get("userId", ""), "sourceText": target["text"]}
        result = read({**action, "type": "inspect-share-targets", "shareType": kind, "receiverNames": ordered,
                       "nickname": account["nickname"]})
        receivers = result.get("receivers", [])
        for name in ordered:
            matches = [r for r in receivers if isinstance(r, dict) and r.get("name") == name
                       and r.get("kind") in {"user", "group"} and re.fullmatch(r"[A-Za-z0-9_-]{6,160}", str(r.get("id", "")))]
            if len(matches) == 1:
                receiver_cursor[kind] = names.index(name) + 1
                return add({**action, **({"saveProfileQr": "1", "profileQrRunKey": config["profileQrRunKey"]}
                            if kind == "share-profile" and config.get("profileShareSaveQr") else {}),
                            "shareRecipient": name, "shareRecipientId": matches[0]["id"],
                            "shareRecipientKind": matches[0]["kind"]})
        skipped.append({"type": kind, "reason": "未找到可唯一定位的好友或群聊，未发送"})
        return False

    # ---- 养护 / 主页首作（2026-11-18 接通） --------------------------------
    # 进入对方主页，在关注或私信之前先做轻互动。原语全部真机可用：
    #   作品列表 read-author-works（含置顶标记）、like-video / favorite-video
    #   （壳侧 VIDEO_ACTIONS 与作用域 TYPES 均已支持 favorite-video）。
    # 养护：从前 5 个作品随机取 warmupMin–warmupMax 个，逐个点赞+收藏。
    # 首作：首个（非置顶）作品按概率点赞/收藏；评论可单独开关，无首作时按
    #   profileFirstCommentFallbackMode 兜底（reply=回评对方的评论 / skip=跳过）。
    def profile_url(uid):
        return "https://www.douyin.com/user/" + uid

    works_cache: dict[str, list] = {}
    warmup_done: set[str] = set()

    def author_works(uid):
        if uid not in works_cache:
            page = read({"type": "read-author-works", "authorUrls": [profile_url(uid)], "limit": 12})
            works_cache[uid] = [w for w in page.get("items", []) if isinstance(w, dict) and w.get("url")]
        return works_cache[uid]

    def first_filters_ok(profile):
        """主页首作专属的性别/年龄筛选（与 DM 的 targetGender/ageFilter 互相独立）。"""
        if not isinstance(profile, dict):
            return True
        gender = config.get("profileFirstGenderFilter", "all")
        if gender != "all":
            actual = profile.get("gender")
            base = gender.replace("_unknown", "")
            if actual != base and not (gender.endswith("_unknown") and actual is None):
                return False
        if config.get("profileFirstAgeFilterEnabled"):
            age = profile.get("age")
            if age is None or not config["profileFirstAgeMin"] <= age <= config["profileFirstAgeMax"]:
                return False
        return True

    def room_for(reserve):
        return config["interactionLimit"] - len(actions) - reserve

    def add_profile_action(kind, work, keyword):
        action = {"type": kind, "videoUrl": work["url"], "keyword": keyword,
                  "title": str(work.get("title") or "")[:80], "text": "", "profileTarget": "1"}
        key = action_key(account["id"], action)
        if key in planned or not gate.can_attempt(key)[0]:
            skipped.append({"type": kind, "reason": "该作品已互动或结果待核验"})
            return False
        planned.add(key)
        actions.append(action)
        return True

    def warmup_for(target, base):
        uid = str(target.get("userId") or "")
        if not (config.get("enableWarmup") or config.get("commentOnProfileFirstWork")):
            return
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,160}", uid) or uid in warmup_done:
            return
        warmup_done.add(uid)
        if uid not in profiles:
            page = read({"type": "inspect-profile", "recipientId": uid, "commentAuthor": target["nickname"]})
            profiles[uid] = page.get("item", {})
        if not first_filters_ok(profiles[uid]):
            skipped.append({"type": "profile-warmup", "reason": "主页性别或年龄不在首作筛选范围内"})
            return
        # 给本目标的关注/私信保留额度，避免养护把总上限吃光后关注/私信一条发不出
        reserve = sum(1 for flag, left in (("enableFollow", follow_left), ("enableDM", dm_left))
                      if config.get(flag) and left > 0)
        if config.get("enableWarmup"):
            pool = author_works(uid)[:5]
            picks = random.sample(pool, min(random.randint(config["warmupMin"], config["warmupMax"]), len(pool))) \
                if pool else []
            if not picks:
                skipped.append({"type": "profile-warmup", "reason": "主页未读到可互动作品"})
            elif room_for(reserve + len(picks) * 2) < 0:
                skipped.append({"type": "profile-warmup", "reason": "互动总上限余额不足，未做养护"})
            else:
                done = sum(add_profile_action(kind, work, "主页养护")
                           for work in picks for kind in ("like-video", "favorite-video"))
                if done:
                    progress(f"已为目标主页安排养护互动 {done} 项，尚未发送")
        if config.get("commentOnProfileFirstWork"):
            works = author_works(uid)
            first = next((w for w in works if not w.get("pinned")), None)
            if not first and config.get("profileFirstIgnorePinnedIfOnlyOne") and len(works) == 1:
                first = works[0]
            if not first:
                mode = config.get("profileFirstCommentFallbackMode", "reply")
                if (mode == "reply" and config.get("enableProfileFirstComment")
                        and target.get("commentId") and room_for(reserve) > 0):
                    reply = draft("reply-comment", dict(base), target)
                    key = action_key(account["id"], reply) if reply else ""
                    if reply and key not in planned and gate.can_attempt(key)[0] and len(actions) < limit:
                        planned.add(key)
                        actions.append(reply)
                    elif reply:
                        skipped.append({"type": "reply-comment", "reason": "该评论已回复或结果待核验"})
                else:
                    skipped.append({"type": "profile-first", "reason": "主页未找到可互动的首作"})
                return
            if room_for(reserve) <= 0:
                skipped.append({"type": "profile-first", "reason": "互动总上限余额不足，未做首作互动"})
                return
            if random.randint(1, 100) <= config["profileFirstWorkLikePercent"]:
                add_profile_action("like-video", first, "主页首作")
            if random.randint(1, 100) <= config["profileFirstWorkCollectPercent"]:
                add_profile_action("favorite-video", first, "主页首作")
            if config.get("enableProfileFirstComment"):
                try:
                    comment = draft("post-comment", {"videoUrl": first["url"], "keyword": "主页首作",
                                                     "title": str(first.get("title") or "")[:80]})
                except ValueError as exc:
                    skipped.append({"type": "post-comment", "reason": f"首作评论文案未生成：{exc}"})
                else:
                    if comment:
                        key = action_key(account["id"], comment)
                        if key not in planned and gate.can_attempt(key)[0] and len(actions) < limit:
                            comment["profileTarget"] = "1"
                            siblings = [w["url"] for w in works if w.get("url") and w["url"] != first["url"]]
                            if siblings:
                                comment["browseUrl"] = random.choice(siblings)
                            planned.add(key)
                            actions.append(comment)
                        else:
                            skipped.append({"type": "post-comment", "reason": "该作品已主评或结果待核验"})

    for candidate in candidates:
        check()
        main_rejected = False
        if len(actions) >= limit:
            break
        if candidate["videoUrl"] in seen:
            continue
        seen.add(candidate["videoUrl"])
        reason = settings.video_reason(config, candidate)
        if reason.endswith("平台未提供指标") or reason in {"视频发布时间未知", "未读到视频标题"}:
            metadata = read({"type": "inspect-video", "videoUrl": candidate["videoUrl"]})
            # 只补空缺，不用空值覆盖搜索卡片已经拿到的标题/作者：
            # 详情页读不到标题时回的是空字符串，直接合并会把好数据擦掉。
            patch = {k: v for k, v in (metadata.get("item") or {}).items()
                     if v not in (None, "") and not k.startswith("_")}
            if not str(patch.get("title") or "").strip():
                inspected.append({"title": False,
                                  "diag": diag_brief(metadata.get("diag"))})
            candidate = {**candidate, **patch,
                         "videoUrl": candidate["videoUrl"], "keyword": candidate["keyword"]}
            reason = settings.video_reason(config, candidate)
        if reason:
            record("search-video", candidate, "skipped", reason)
            # 视频级筛选也要进 skippedActions：以前只写证据表，接口里 skippedActions
            # 是空的，于是界面只能说「没有可执行的动作」，用户看不到真正的原因。
            skipped.append({"type": "search-video", "reason": reason})
            # 同时打进任务日志：2026-11-20 真机，设置「只处理 3 天内发布」后每条
            # 视频都被静默跳过，实况里只见视频一条条刷过去、0 互动，原因全不可见。
            progress("视频「" + str(candidate.get("title") or candidate["videoUrl"])[:40]
                     + "」不满足筛选条件，已跳过：" + reason)
            continue
        if config.get("enablePersonaVideoFilter") and config.get("aiReplyMode"):
            decision = classify_intent(db_path, config, candidate, video_only=True)
            check()
            if not decision["matched"]:
                record("search-video", candidate, "skipped", decision["reason"])
                skipped.append({"type": "search-video", "reason": decision["reason"]})
                continue
        totals["selectedVideos"] += 1
        record("search-video", candidate, "candidate", "筛选通过，尚未执行")
        base = {k: candidate.get(k, "") for k in ("videoUrl", "keyword", "title")}
        context_result = (comment_cache or {}).get(candidate["videoUrl"])
        if config.get("enableVideoComment"):
            context_cmd = {"type": "read-comments", "videoUrls": [candidate["videoUrl"]], "limit": 60}
            if config.get("expandSubComments") is True:
                context_cmd["expandSubComments"] = True
            context_result = context_result or read(context_cmd)
            context_rows = [str(row.get("text") or "").strip() for row in context_result.get("comments", [])
                            if isinstance(row, dict) and str(row.get("text") or "").strip()]
            totals["commentsRead"] += len(context_rows)
            if payload.get("_streamMode") and not context_rows:
                reason = "未读到可用评论区上下文，跳过本视频且不发表评论"
                record("read-comments", candidate, "skipped", reason)
                skipped.append({"type": "post-comment", "reason": reason})
                continue
            candidate["discussionContext"] = "\n".join(f"- {text[:240]}" for text in context_rows[:12])[:2400]
        if config.get("enableMainVideoLike") and random.randint(1, 100) <= config["mainVideoLikePercent"]:
            add({"type": "like-video", **base, "text": ""})
        if config.get("enableVideoComment") and len(actions) < limit:
            main_key = action_key(account["id"], {"type": "post-comment", **base})
            already = not gate.can_attempt(main_key)[0]
            shared_key = json.dumps(["shared-main", candidate["videoUrl"]], separators=(",", ":"))
            already = already or gate.state(shared_key) == "uncertain"
            if not config.get("videoCommentAllowOtherAccounts", False):
                with sqlite3.connect(str(db_path)) as con:
                    already = already or bool(con.execute("SELECT 1 FROM live_action_results WHERE url=? "
                        "AND action='post-comment' AND status IN ('sent_ok','uncertain') LIMIT 1",
                        (candidate["videoUrl"],)).fetchone())
            if already:
                skipped.append({"type": "post-comment", "reason": "该视频已主评或结果待核验"})
            else:
                main = draft("post-comment", candidate)
                if main:
                    add(main)
                else:
                    # AI 判定该视频不适合评论时，只砍掉「依赖主评场景」的动作
                    # （回复/私信/关注/分享）；主视频点赞、评论点赞是独立开关的
                    # 纯互动动作，不产出文案也不依赖评论场景成立 —— 用户开了就照做。
                    # 2026-11-20 真机：AI 连续判「话题不相关」把每条视频的动作连
                    # 点赞一起清空，用户设定的主视频点赞 20% 一条没点，
                    # 看起来就是「动作没按设定执行」。
                    actions[:] = [a for a in actions if a["videoUrl"] != candidate["videoUrl"]
                                  or a["type"] == "like-video"]
                    planned = {action_key(account["id"], a) for a in actions}
                    _REJECT_FLAGS = {"reply-comment": "enableComment", "send-dm": "enableDM",
                                     "follow-user": "enableFollow", "share-profile": "enableProfileShare",
                                     "share-comment": "enableCommentShare"}
                    for kind, flag in _REJECT_FLAGS.items():
                        if config.get(flag):
                            skipped.append({"type": kind, "reason": "AI 判定本视频不适合评论，相关动作已跳过"})
                    main_rejected = True
        if len(actions) >= limit or not any(config.get(f) for f in ("enableLike", "enableComment", "enableDM", "enableFollow", "enableProfileShare", "enableCommentShare")):
            continue
        # 采集二级评论：UI 上一直有这个开关（config.expandSubComments），但这里
        # 从来没把它传下去 —— 开了也不生效。2026-11-18 补上，并给每行标 kind，
        # 好让「回复评论 / 点赞评论」知道目标是一条回复还是主评。
        command = {"type": "read-comments", "videoUrls": [candidate["videoUrl"]], "limit": 60}
        if config.get("expandSubComments") is True:
            command["expandSubComments"] = True
        result = context_result or read(command)
        context_already_counted = result is context_result
        targets, incoming = [], []
        self_names = {account["nickname"], *(config.get("interactedUsers") or [])}
        for source in comment_tree.annotate(result.get("comments", [])):
            if not isinstance(source, dict):
                continue
            if not context_already_counted:
                totals["commentsRead"] += 1
            source = {**source, "videoUrl": candidate["videoUrl"], "caption": candidate.get("title", "")}
            reason = settings.comment_reason(config, source, self_names)
            # Keep excluded content as an ordinary lead, never as an action target.
            if reason and not reason.startswith("排除评论词"):
                record("filter-comment", candidate, "skipped", reason, str(source.get("text") or ""))
                continue
            check()
            mode = config["commentIntentMatchMode"]
            decision = {"matched": False, "reason": reason}
            if not reason:
                if mode == "ai" and config.get("aiReplyMode"):
                    decision = classify_intent(db_path, config, {**candidate, **source})
                    check()
                else:
                    matched = mode == "all" or not config["intentionKeywords"] or any(w in source["text"] for w in config["intentionKeywords"])
                    decision = {"matched": matched, "reason": "全部命中" if mode == "all" else "关键词匹配" if matched else "未命中意向词"}
            source["intentMatched"], source["intentReason"] = decision["matched"], decision["reason"]
            incoming.append(source)
            record("classify-comment", candidate, "candidate" if decision["matched"] else "skipped", decision["reason"], source["text"])
            if decision["matched"]:
                targets.append(source)
        # A minimal isolated test DB intentionally has no business leads table.
        with sqlite3.connect(str(db_path)) as con:
            has_leads = bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='leads'").fetchone())
        if incoming and has_leads:
            from app import collector
            collected = collector.ingest_comments(db_path, incoming, candidate["keyword"], task_id, account["nickname"])
            totals["leadsTotal"] += collected["inserted"]
            for source in incoming:
                collector.annotate_intent(db_path, source)
        if not targets:
            skipped.extend({"type": kind, "reason": "没有符合意向和过滤条件且可准确定位的评论"}
                for flag, kind in settings.ACTION_FLAGS.items() if config.get(flag) and kind not in {"post-comment", "like-video"})
        liked = set()
        if config.get("enableLike"):
            quota = random.randint(config["likeMin"], config["likeMax"])
            for target in targets:
                if quota <= 0 or len(actions) >= limit:
                    break
                if add({"type": "like-comment", **base, "text": target["text"],
                        "commentId": target["commentId"], "commentAuthor": target["nickname"], "sourceText": target["text"]}):
                    liked.add(target["commentId"])
                    quota -= 1
        if config.get("enableComment") and not main_rejected:
            quota = random.randint(config["commentMin"], config["commentMax"])
            for target in targets:
                if quota <= 0 or len(actions) >= limit:
                    break
                if target["commentId"] in liked:
                    continue
                key = action_key(account["id"], {"type": "reply-comment", **base, "commentId": target["commentId"]})
                if not gate.can_attempt(key)[0]:
                    continue
                reply = draft("reply-comment", candidate, target)
                if reply and add(reply):
                    quota -= 1
        if config.get("enableCommentShare") and not main_rejected:
            quota = random.randint(config["commentShareMin"], config["commentShareMax"])
            for target in targets:
                if quota <= 0 or len(actions) >= limit:
                    break
                if share("share-comment", target, base):
                    quota -= 1
        if (config.get("enableFollow") or config.get("enableProfileShare")) and not main_rejected:
            for target in targets:
                if len(actions) >= limit:
                    break
                user = {**base, "text": "", "recipientId": target.get("userId", ""),
                        "commentAuthor": target["nickname"], "sourceText": target["text"]}
                if config.get("enableFollow") and follow_left > 0 and profile_ok(target, candidate, "follow-user"):
                    warmup_for(target, base)
                    if add({"type": "follow-user", **user}):
                        follow_left -= 1
                if config.get("enableProfileShare") and profile_share_left > 0 and len(actions) < limit and profile_ok(target, candidate, "share-profile"):
                    if share("share-profile", target, base):
                        profile_share_left -= 1
        if config.get("enableDM") and not main_rejected:
            for target in targets:
                if dm_left <= 0 or len(actions) >= limit:
                    break
                if (config.get("targetGender", "all") != "all" or config.get("ageFilterEnabled")) and not profile_ok(target, candidate, "send-dm"):
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_-]{6,160}", str(target.get("userId") or "")):
                    skipped.append({"type": "send-dm", "reason": "评论没有可靠的用户主页标识"})
                    continue
                warmup_for(target, base)
                dm = draft("send-dm", candidate, target)
                dm.pop("commentId", None)
                if add(dm):
                    dm_left -= 1
    check()
    if not actions and not payload.get("_streamMode"):
        reason = _empty_reason(config, candidates, searched, inspected, skipped, totals)
        # 同一句话同时进运行日志：界面上的 toast 会消失，日志抽屉里要留得下来。
        progress("准备失败：" + reason)
        raise ValueError(reason)
    if actions:
        settings.scope_duration(config, actions)
    return {"actions": actions, "skippedActions": skipped, "effectiveConfig": config,
            "planStats": totals, "warnings": report["warnings"]}
