"""自动回复运行时。

为什么单独一个运行时
--------------------
自动回复和监控/养号都不一样：
  * 监控是「盯别人的视频/直播间」，目标在任务里写死；
  * 养号是「在推荐流里浏览」；
  * 自动回复是「盯自己账号的互动」（私信、作品评论、评论回复、新增关注），
    收件人与正文都要在运行时才出现，动作又是真的发消息，所以节奏、配额、
    去重、每用户轮次这些约束一条都不能少。

本模块只做「读 → 判 → 回」这条链，不假装能做的事：
  * 作品点赞、新增关注：事件源是顶栏「通知」弹层（2026-11-18 真机实证：
    /notifications、/message、/notification 三个页面仍然 404，但弹层里
    「XX 关注了你 / XX 赞了你的作品」都在，且每行带 /user/<sec_uid> 锚点）。
    「新增关注 → 回关」「作品点赞 → 关注」两条链路都已真机点通；通知行读不到
    主页标识就跳过，不按昵称猜人。
  * 评论回复（有人回复了我的评论）：需要知道自己评论过哪些视频，且 PC 端楼中楼
    展开不稳定，先按「作品评论」同一条读链路处理，读不到就老实跳过。

数据落库
--------
autoreply_events         每次监听到的互动 + 处理结论（matched/filtered/dedup/acted/failed/skipped）
autoreply_conversations  会话指纹（判断「有新消息」而不是每轮全量重读）
autoreply_users          每用户已回复轮次（对应页面上的「每个用户最多回复」）
autoreply_rule_rounds    关键词规则每规则独立轮数（页面口径：各规则独立遵守设置的回复轮数）
autoreply_dm_claim       跨账号私信去重占用表（同一任务内同一线索只由一个账号私信）
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
from typing import Any

from app.execution import live_scope
from app.runtime import browser, store


def _log_cb(ctx):
    """browser.execute 要的是 (message, level) 回调，这里转到任务的运行日志。"""
    from app.runtime import runners

    def callback(message: str, level: str = "normal") -> None:
        runners.log(ctx, message, level)

    return callback

# 私信动作的每日默认上限：页面上「单账号限制数量」没保存到这个任务的快照里时用。
# 宁愿保守（少发）也不要一口气把 300 个会话全回一遍。
DEFAULT_DM_LIMIT = 50
MIN_INTERVAL_S = 15
EVENT_KEEP = 5000
# 这些失败是「这一瞬间的环境问题」（桌面端不在线、页面没打开、编辑器状态不好），
# 下一轮值得再试；不写进去重记录，免得一条消息因为一次手滑就永久丢掉。
_RETRYABLE_CODES = frozenset({
    "desktop_required", "live_writes_disabled", "scope_not_verified", "attempt_consumed",
    "conversation_not_found", "conversation_not_opened", "composer_not_usable",
    "editor_not_unique", "draft_not_confirmed", "conversation_page_not_verified",
    "send_outcome_unknown", "stopped",
    # 按钮在页面上但点不中（后台窗口被判定遮挡，elementFromPoint 打不中）：
    # 壳已经会先重新显示再试一次，仍是这个码说明是环境态，下一轮值得重来。
    "follow_control_occluded",
})

# 「目标已经是要的状态」不是失败：这条评论本来就点过赞、这个视频本来就收藏过。
# 以前一律按「未成功」记 warn，运维看到满屏告警、无从判断哪条真出了问题。
_ALREADY_CODES = frozenset({"already_liked", "already_done", "already_followed"})


def _connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS autoreply_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, account_id TEXT,"
                "source TEXT, conversation TEXT, nickname TEXT, content TEXT,"
                "outcome TEXT, note TEXT, actions TEXT, created_ms INTEGER)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ar_events_task "
                "ON autoreply_events(task_id, created_ms)")
    con.execute("CREATE TABLE IF NOT EXISTS autoreply_conversations ("
                "task_id TEXT, account_id TEXT, title TEXT, fingerprint TEXT,"
                "last_ms INTEGER, handled_ms INTEGER, PRIMARY KEY(task_id, account_id, title))")
    con.execute("CREATE TABLE IF NOT EXISTS autoreply_users ("
                "account_id TEXT, user_key TEXT, rounds INTEGER DEFAULT 0, last_ms INTEGER,"
                "PRIMARY KEY(account_id, user_key))")
    con.execute("CREATE TABLE IF NOT EXISTS autoreply_rule_rounds ("
                "task_id TEXT, account_id TEXT, rule_id TEXT, user_key TEXT,"
                "rounds INTEGER DEFAULT 0, last_ms INTEGER,"
                "PRIMARY KEY(task_id, account_id, rule_id, user_key))")
    con.execute("CREATE TABLE IF NOT EXISTS autoreply_dm_claim ("
                "task_id TEXT, user_key TEXT, account_id TEXT, claimed_ms INTEGER,"
                "PRIMARY KEY(task_id, user_key))")
    con.commit()
    return con


def _event(db_path, *, task_id: str, account_id: str, source: str, conversation: str = "",
           nickname: str = "", content: str = "", outcome: str = "pending",
           note: str = "", actions: Any = None) -> None:
    con = _connect(db_path)
    try:
        con.execute("INSERT INTO autoreply_events (task_id, account_id, source, conversation,"
                    " nickname, content, outcome, note, actions, created_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (task_id, account_id, source, conversation, nickname, content[:1000],
                     outcome, note[:500], json.dumps(actions or [], ensure_ascii=False),
                     int(time.time() * 1000)))
        con.execute("DELETE FROM autoreply_events WHERE task_id=? AND id NOT IN"
                    " (SELECT id FROM autoreply_events WHERE task_id=? ORDER BY id DESC LIMIT ?)",
                    (task_id, task_id, EVENT_KEEP))
        con.commit()
    finally:
        con.close()


def _account_label(db_path, account_id: str) -> str:
    """账号昵称：消息中心「X 收到 Y 的新私信」里的 X。"""
    try:
        with sqlite3.connect(str(db_path), timeout=10) as con:
            row = con.execute("SELECT nickname, name FROM accounts WHERE id=?",
                              (account_id,)).fetchone()
    except sqlite3.Error:
        return ""
    return str((row[0] if row else "") or (row[1] if row else "") or "").strip()


def _emit_chat_new_message(db_path, account_id: str, sender: str) -> None:
    """私信实时事件（消息中心「X 收到 Y 的新私信」提示的源头）。

    事件走桥接事件总线持久化排队：页面关了也不丢，重新打开消息中心后
    事件照样送达（对应「后台提醒，离开页面继续监听」的承诺）。
    """
    try:
        from app.api import event_bus
        event_bus.publish("chat-new-message", {
            "accountId": account_id,
            "receiverNickname": _account_label(db_path, account_id),
            "sender": sender,
        })
    except Exception:  # noqa: BLE001 提示事件失败不影响采集本身
        pass


def _emit_webhook_failure(result: dict) -> None:
    """Webhook 推送失败实时事件（消息中心「新消息 Webhook 推送失败」提示）。"""
    try:
        from app.api import event_bus
        event_bus.publish("chat-webhook-result", {
            "success": False,
            "message": str(result.get("error") or result.get("code") or "推送失败"),
        })
    except Exception:  # noqa: BLE001
        pass


def _conversation_fingerprint(con: sqlite3.Connection, task_id: str, account_id: str,
                              title: str) -> str:
    row = con.execute("SELECT fingerprint FROM autoreply_conversations"
                      " WHERE task_id=? AND account_id=? AND title=?",
                      (task_id, account_id, title)).fetchone()
    return str(row["fingerprint"] or "") if row else ""


def _mark_conversation(con: sqlite3.Connection, task_id: str, account_id: str,
                       title: str, fingerprint: str, handled: bool = False) -> None:
    now = int(time.time() * 1000)
    con.execute("INSERT INTO autoreply_conversations (task_id, account_id, title, fingerprint,"
                " last_ms, handled_ms) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(task_id, account_id, title) DO UPDATE SET"
                " fingerprint=excluded.fingerprint, last_ms=excluded.last_ms,"
                " handled_ms=COALESCE(excluded.handled_ms, autoreply_conversations.handled_ms)",
                (task_id, account_id, title, fingerprint, now, now if handled else None))


def _user_rounds(con: sqlite3.Connection, account_id: str, user_key: str) -> int:
    row = con.execute("SELECT rounds FROM autoreply_users WHERE account_id=? AND user_key=?",
                      (account_id, user_key)).fetchone()
    return int(row["rounds"] or 0) if row else 0


def _bump_user(con: sqlite3.Connection, account_id: str, user_key: str) -> None:
    con.execute("INSERT INTO autoreply_users (account_id, user_key, rounds, last_ms)"
                " VALUES (?,?,1,?) ON CONFLICT(account_id, user_key) DO UPDATE SET"
                " rounds=autoreply_users.rounds+1, last_ms=excluded.last_ms",
                (account_id, user_key, int(time.time() * 1000)))


def _rule_rounds(con: sqlite3.Connection, task_id: str, account_id: str,
                 rule_id: str, user_key: str) -> int:
    """关键词规则的独立轮数（页面原话：各规则独立遵守设置的回复轮数）。"""
    row = con.execute("SELECT rounds FROM autoreply_rule_rounds"
                      " WHERE task_id=? AND account_id=? AND rule_id=? AND user_key=?",
                      (task_id, account_id, rule_id, user_key)).fetchone()
    return int(row["rounds"] or 0) if row else 0


def _bump_rule(con: sqlite3.Connection, task_id: str, account_id: str,
               rule_id: str, user_key: str) -> None:
    con.execute("INSERT INTO autoreply_rule_rounds (task_id, account_id, rule_id, user_key,"
                " rounds, last_ms) VALUES (?,?,?,?,1,?)"
                " ON CONFLICT(task_id, account_id, rule_id, user_key) DO UPDATE SET"
                " rounds=autoreply_rule_rounds.rounds+1, last_ms=excluded.last_ms",
                (task_id, account_id, rule_id, user_key, int(time.time() * 1000)))


def _dm_cross_claim(con: sqlite3.Connection, cfg: dict, task_id: str,
                    user_key: str, account_id: str) -> str:
    """跨账号私信去重（dmCrossAccountDedupe）：同一任务内同一线索只由一个账号私信。

    返回已占用该线索的账号 id（≠ 本账号时应放弃私信）；未开启返回空串。
    占用在发送前写入：两个账号同时抢同一条线索时，先到先得；发送失败不回退占用
    （宁可这一轮不发，也不能两个账号各发一遍）。
    """
    if not _enabled(cfg, "dmCrossAccountDedupe"):
        return ""
    row = con.execute("SELECT account_id FROM autoreply_dm_claim"
                      " WHERE task_id=? AND user_key=?",
                      (task_id, user_key)).fetchone()
    if row:
        return str(row["account_id"] or "")
    con.execute("INSERT OR IGNORE INTO autoreply_dm_claim"
                " (task_id, user_key, account_id, claimed_ms) VALUES (?,?,?,?)",
                (task_id, user_key, account_id, int(time.time() * 1000)))
    return ""


def _user_key(nickname: str) -> str:
    """用户维度去重键：用昵称哈希，不落平台 uid（出口守卫会拦 uid 类字段）。"""
    return hashlib.sha256(str(nickname or "").encode("utf-8")).hexdigest()[:16]


def _pair(cfg: dict, low: str, high: str, default: tuple[float, float]) -> tuple[float, float]:
    def num(key, fallback):
        try:
            value = float(cfg.get(key))
            return value if value > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    lo = num(low, default[0])
    hi = num(high, default[1])
    return (min(lo, hi), max(lo, hi))


def _enabled(cfg: dict, key: str, default: bool = False) -> bool:
    value = cfg.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off", "no"}
    return bool(value)


def _names(raw) -> list[str]:
    if isinstance(raw, str):
        parts = raw.replace("，", ",").replace("\n", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = []
    return [p.strip() for p in parts if p.strip()]


def _lookback_seconds(cfg: dict) -> float:
    try:
        value = float(cfg.get("initialLookbackValue") or 24)
    except (TypeError, ValueError):
        value = 24.0
    unit = str(cfg.get("initialLookbackUnit") or "hour").lower()
    if unit.startswith("min"):
        return max(60.0, value * 60)
    if unit.startswith("day"):
        return value * 86400
    return value * 3600


def _template_pool(cfg: dict) -> list[str]:
    """私信话术池：模板项优先，其次单条模板。AI 模式返回空池。"""
    items = cfg.get("dmTemplateItems")
    pool: list[str] = []
    if isinstance(items, list):
        for item in items:
            text = item.get("text") if isinstance(item, dict) else item
            if isinstance(text, str) and text.strip():
                pool.append(text.strip())
    extra = cfg.get("dmExtraTemplates")
    if isinstance(extra, list):
        pool.extend(str(x).strip() for x in extra if str(x).strip())
    single = str(cfg.get("dmTemplate") or "").strip()
    if single:
        pool.append(single)
    return pool


def _render(text: str, *, nickname: str, content: str) -> str:
    return (text.replace("{nickname}", nickname or "").replace("{content}", content or "")
            .strip())


def _reply_mode(cfg: dict, channel: str) -> str:
    """这条链路用 AI、模板，还是「专业模式」规则。

    私信多一层：页面上的「回复方式」是 dmReplyMode，取值 professional / rules /
    其它。professional 走 dmProfessionalMessages 规则集合（第 26、27 条），
    rules 走 dmReplyRules；都不是时才看 dmSuggestionMode 的 AI / 模板。

    页面真正的开关字段是 commentSuggestionMode / dmSuggestionMode（评论与私信
    各一个），两个都没配时回落到 suggestionMode —— 见前端
    `Gu(e,t)= t?e.dmSuggestionMode:e.commentSuggestionMode`。

    此前这里读的是 replyMode / dmSendMode：前者前端根本不存在（于是评论恒走 AI，
    用户选「自定义模板」也不生效），后者是「每次一条 / 连续多条」的开关
    （rotate / sequence），被当模式用之后私信恒走模板、AI 私信从来没发出去过。
    """
    if channel == "dm" and str(cfg.get("dmReplyMode") or "").strip().lower() == "professional":
        return "professional"
    # 「关键词规则」是 dmReplyMode 的第二个取值（页面三选一：通用话术 / 关键词规则 /
    # 专业模式，取证自 SelfWarmup 打包：o(b,{value:"rules"}...)），此前只认 professional，
    # 选「关键词规则」会静默落回模板模式、规则列表从未被消费。
    if channel == "dm" and str(cfg.get("dmReplyMode") or "").strip().lower() == "rules":
        return "rules"
    key = "dmSuggestionMode" if channel == "dm" else "commentSuggestionMode"
    mode = str(cfg.get(key) or "").strip().lower()
    if mode in {"ai", "template"}:
        return mode
    return "ai" if str(cfg.get("suggestionMode") or "").strip().lower() == "ai" else "template"


def _dm_expression(cfg: dict, text: str) -> str:
    """私信随机表情：页面「添加随机表情」开关（dmUseRandomSuffix），AI 与模板都生效。"""
    if not text or not _enabled(cfg, "dmUseRandomSuffix"):
        return text
    return text + random.choice(["", "", "😊", "🙌", "✨"])


# ------------------------------------------------------------------ 专业模式规则
# 页面口径（SelfWarmupDmProfessional）：
#   dmProfessionalMessages 是规则集合，每组的 mode 是 keyword / scene / sequence；
#   dmProfessionalMatchOrder 只有两个取值 ——
#     "custom"：数组顺序就是优先级（页面上的「上移、下移」直接换数组元素位置）；
#     其它：按页面固定顺序 scene（其中 mutual_follow 先于 follow）→ keyword → sequence。
#   sequence 组是「首次触达兜底」，无论怎么排都固定在最后。
#   匹配是从上到下、命中即停：命中一组就按该组设置处理，本次不再看后面的规则。
#
# 这些字段名和取值域都是从 ui/assets 里抽出来的（vh/fV 两个函数），不是猜的 ——
# 之前按猜测的字段名写，导致评论恒走 AI、私信 AI 从未生效，不能再犯。
_SCENE_VALUES = {
    "comment", "reply", "like", "mutual_follow", "follow_followed",
    "follow_unfollowed", "message_followed", "message_unfollowed",
}
# 旧配置里的两个值：follow = 收到关注（不区分我是否关注对方），mutual_follow = 回关
_LEGACY_FOLLOW_SCENES = {"follow", "mutual_follow"}


def _split_keywords(raw) -> list[str]:
    """关键词分隔：页面提示是「逗号、顿号或换行」，实际还会粘分号/竖线。"""
    text = str(raw or "")
    for sep in ("，", "、", "；", ";", "|", "｜", "\r", "\n"):
        text = text.replace(sep, ",")
    return [k.strip() for k in text.split(",") if k.strip()]


def _professional_rules(cfg: dict) -> list[dict]:
    """按页面口径把规则排成「从上到下」的优先级顺序。

    sequence（首次触达）永远在最后：页面原话是「首次触达固定在最后兜底」，
    即使用户把它挪到中间也不该抢在前面。
    """
    raw = cfg.get("dmProfessionalMessages")
    rules = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    if not rules:
        return []
    tail = [r for r in rules if str(r.get("mode") or "") == "sequence"]
    head = [r for r in rules if str(r.get("mode") or "") != "sequence"]
    if str(cfg.get("dmProfessionalMatchOrder") or "") == "custom":
        return head + tail
    scenes = [r for r in head if str(r.get("mode") or "") == "scene"]
    # 固定顺序下 follow 类场景里「回关（mutual_follow）」排在「关注（follow）」前，
    # 这是前端的原始实现，不是我们另加的规则。
    follows = [r for r in scenes if str(r.get("scene") or "") in _LEGACY_FOLLOW_SCENES]
    ordered_follows = ([r for r in follows if str(r.get("scene")) == "mutual_follow"]
                       + [r for r in follows if str(r.get("scene")) == "follow"])
    slot = 0
    merged = []
    for rule in scenes:
        if str(rule.get("scene") or "") in _LEGACY_FOLLOW_SCENES:
            merged.append(ordered_follows[slot])
            slot += 1
        else:
            merged.append(rule)
    keywords = [r for r in head if str(r.get("mode") or "") == "keyword"]
    return merged + keywords + tail


def _rule_messages(rule: dict) -> list[str]:
    """取一条规则下的消息文案：页面里是 items 数组，兼容早期的单条 item。"""
    items = rule.get("items")
    if not isinstance(items, list):
        items = [rule.get("item")] if isinstance(rule.get("item"), dict) else []
    out: list[str] = []
    for item in items:
        text = item.get("text") if isinstance(item, dict) else item
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
    return out


def _rule_matches(rule: dict, *, content: str, scene: str, followed, touched: bool) -> bool:
    """这一组规则是否命中。followed 为 None 表示「不知道对方有没有关注我」。"""
    mode = str(rule.get("mode") or "sequence")
    if mode == "keyword":
        keywords = _split_keywords(rule.get("keywords"))
        if not keywords:
            return False
        haystack = content or ""
        if rule.get("ignoreCase") is not False:
            haystack = haystack.casefold()
            keywords = [k.casefold() for k in keywords]
        if not any(k and k in haystack for k in keywords):
            return False
        # 「对方是否已关注本账号」：不知道就不命中，宁可这一轮不发也不要发错人。
        peer = str(rule.get("peerFollowFilter") or "all")
        if peer == "followed" and followed is not True:
            return False
        if peer == "unfollowed" and followed is not False:
            return False
        return True
    if mode == "scene":
        want = str(rule.get("scene") or "")
        want = {"follow": "follow_followed", "mutual_follow": "follow_followed"}.get(want, want)
        if want not in _SCENE_VALUES or want != scene:
            return False
        # 「此前是否收到过关键词回复」：用该用户在本账号下有没有被自动回复过判断。
        history = str(rule.get("keywordHistoryFilter") or "all")
        if history == "yes" and not touched:
            return False
        if history == "no" and touched:
            return False
        return True
    # sequence：前面的规则都没命中，且「同一任务、同一账号此前从未自动触达该用户」
    return not touched


def _match_professional_rule(cfg: dict, *, content: str, scene: str = "",
                             followed=None, touched: bool = False) -> tuple[dict | None, str]:
    """从上到下匹配，命中即停。返回 (命中规则, 说明)。"""
    rules = _professional_rules(cfg)
    if not rules:
        return None, "专业模式未配置任何规则"
    for index, rule in enumerate(rules):
        if _rule_matches(rule, content=content, scene=scene,
                         followed=followed, touched=touched):
            mode = str(rule.get("mode") or "sequence")
            if mode == "sequence":
                return rule, "命中首次触达兜底（此前未自动触达该用户）"
            if mode == "keyword":
                return rule, f"第 {index + 1} 组关键词规则命中"
            return rule, f"第 {index + 1} 组场景规则命中（{rule.get('scene')}）"
    return None, "前面的规则都未命中，且没有可用的首次触达兜底"


def _professional_text(cfg: dict, rule: dict, *, nickname: str, content: str) -> str:
    """命中规则后取正文：该组自己的消息集合按轮询取一条。"""
    pool = _rule_messages(rule)
    if not pool:
        return ""
    text = _render(random.choice(pool), nickname=nickname, content=content)
    # 组内消息项可以单独勾「加随机表情」，全局开关打开时也生效。
    if _enabled(cfg, "dmUseRandomSuffix"):
        text = _dm_expression(cfg, text)
    return text


# ------------------------------------------------------------------ 关键词规则
# 页面口径（SelfWarmup 的 dmReplyRules 组件，字段名取证自打包资源，不是猜的）：
#   规则 = {id, name, trigger, matchMode, keywords/pattern, ignoreCase,
#           replyEnabled(默认真), followEnabled(默认假), templateItems}
#   trigger：message（收到私信）/ follow（收到关注通知）/ mutual_follow（确认互关）；
#   matchMode：keyword（关键词，逗号/顿号/换行分隔）| regex（正则，pattern 字段）；
#   数组顺序就是优先级（页面的上移/下移直接换数组元素位置），从上到下命中即停；
#   命中后按消息框顺序全部发送，每个框内随机选一行话术，整组算一轮；
#   各规则独立遵守设置的回复轮数（autoreply_rule_rounds）；
#   未命中时按 dmRuleFallback 兜底：skip = 仅记录不回复；general = 使用通用话术
#   （通用话术每位用户仅自动触发一次，与回复轮数设置无关）。
def _dm_rules(cfg: dict) -> list[dict]:
    raw = cfg.get("dmReplyRules")
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _dm_rule_matches(rule: dict, *, content: str) -> bool:
    """「收到私信」类规则是否命中。关注通知类规则只用于通知链路，这里不匹配。"""
    if str(rule.get("trigger") or "message") != "message":
        return False
    if str(rule.get("matchMode") or "keyword") == "regex":
        pattern = str(rule.get("pattern") or "").strip()
        if not pattern:
            return False
        flags = re.IGNORECASE if rule.get("ignoreCase") else 0
        try:
            return re.search(pattern, content or "", flags) is not None
        except re.error:
            return False          # 用户写了个非法正则：不命中，也不让任务崩
    keywords = _split_keywords(rule.get("keywords"))
    if not keywords:
        return False
    haystack = content or ""
    if rule.get("ignoreCase"):
        haystack = haystack.casefold()
        keywords = [k.casefold() for k in keywords]
    return any(k and k in haystack for k in keywords)


def _match_dm_rule(cfg: dict, *, content: str = "",
                   trigger: str = "message") -> tuple[dict | None, int]:
    """按数组顺序从上到下匹配，命中即停。返回 (命中规则, 下标)。"""
    for index, rule in enumerate(_dm_rules(cfg)):
        if str(rule.get("trigger") or "message") != trigger:
            continue
        if trigger == "message":
            if _dm_rule_matches(rule, content=content):
                return rule, index
            continue
        # 关注通知类规则：命中条件就是场景本身（mutual_follow 要求确认互关，
        # 由调用方传入的 trigger 保证），规则本身没有额外过滤条件。
        return rule, index
    return None, -1


def _dm_rule_boxes(rule: dict) -> list[list[str]]:
    """规则的消息框列表：每个框一行一条话术，发送时每框随机选一行。"""
    items = rule.get("templateItems")
    if not isinstance(items, list):
        items = [rule.get("item")] if isinstance(rule.get("item"), dict) else []
    boxes: list[list[str]] = []
    for item in items:
        text = item.get("text") if isinstance(item, dict) else item
        if isinstance(text, str):
            lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
            if lines:
                boxes.append(lines)
    return boxes


def _rule_group_texts(cfg: dict, rule: dict, *, nickname: str, content: str) -> list[str]:
    """命中规则后的整组消息：每个框随机选一行，按框顺序全部发送（整组算一轮）。"""
    texts: list[str] = []
    for pool in _dm_rule_boxes(rule):
        text = _render(random.choice(pool), nickname=nickname, content=content).strip()
        if _enabled(cfg, "dmUseRandomSuffix"):
            text = _dm_expression(cfg, text).strip()
        if text:
            texts.append(text)
    return texts


def _template_boxes(cfg: dict) -> list[list[str]]:
    """通用话术的消息框（连续多条模式用）：每个框一行一条，每框随机选一行。"""
    boxes: list[list[str]] = []
    items = cfg.get("dmTemplateItems")
    for item in items if isinstance(items, list) else []:
        text = item.get("text") if isinstance(item, dict) else item
        if isinstance(text, str):
            lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
            if lines:
                boxes.append(lines)
    return boxes


def _general_group(cfg: dict, nickname: str, content: str) -> list[str]:
    """通用话术的消息组。

    dmSendMode = rotate（每次一条，默认）：从话术池随机取一条 —— 与既有行为一致；
    dmSendMode = sequence（连续多条）：每个消息框随机选一行，按框顺序整组发送
    （页面原话：「添加两条消息就会依次发送两条；每个框内随机选一行」）。
    """
    if str(cfg.get("dmSendMode") or "rotate") == "sequence":
        texts: list[str] = []
        for pool in _template_boxes(cfg):
            text = _render(random.choice(pool), nickname=nickname, content=content).strip()
            if _enabled(cfg, "dmUseRandomSuffix"):
                text = _dm_expression(cfg, text).strip()
            if text:
                texts.append(text)
        return texts
    pool = _template_pool(cfg)
    if not pool:
        return []
    return [_dm_expression(cfg, _render(random.choice(pool),
                                        nickname=nickname, content=content)).strip()]


def _dm_text(db_path, cfg: dict, account_id: str, nickname: str, content: str, *,
             scene: str = "", followed=None, touched: bool = False) -> str:
    """生成一条私信正文：模板模式按池轮询，AI 模式走模型，专业模式走规则。

    页面原话（与评论同一条口径）：「私信只使用 AI 生成，并在回复会话时结合最近
    聊天记录；本轮最多尝试 5 次，不降级固定话术。」所以 AI 失败就返回空、由调用方
    记跳过等下轮 —— 以前这里只试 1 次然后偷偷回落模板，用户会以为 AI 在回。
    """
    from app.runtime import monitor_engine

    mode = _reply_mode(cfg, "dm")
    if mode == "professional":
        rule, _why = _match_professional_rule(
            cfg, content=content, scene=scene, followed=followed, touched=touched)
        if not rule:
            return ""
        return _professional_text(cfg, rule, nickname=nickname, content=content)
    if mode == "rules":
        # 单条兼容口（评论回复链路用）：命中规则取整组的第一条；未命中按兜底走
        # 通用话术，兜底为「仅记录」时返回空。私信主链路用的是整组版（_dm_pass）。
        rule, _index = _match_dm_rule(cfg, content=content)
        if rule:
            texts = _rule_group_texts(cfg, rule, nickname=nickname, content=content)
            return texts[0] if texts else ""
        if str(cfg.get("dmRuleFallback") or "skip") != "general":
            return ""
        pool = _template_pool(cfg)
        if not pool:
            return ""
        return _dm_expression(cfg, _render(random.choice(pool),
                                           nickname=nickname, content=content))
    if mode == "ai":
        for _ in range(MAX_DM_AI_TRIES):
            try:
                draft = monitor_engine.dm_draft(db_path, cfg, account_id,
                                                {"nickname": nickname, "text": content})
            except Exception:  # noqa: BLE001 模型抖动只影响本次尝试
                draft = {}
            texts = draft.get("texts") if isinstance(draft, dict) else None
            usable = [str(t).strip() for t in texts if str(t).strip()] \
                if isinstance(texts, list) else []
            if usable:
                return _dm_expression(
                    cfg, _render(random.choice(usable), nickname=nickname, content=content))
            time.sleep(0.8)
        return ""
    pool = _template_pool(cfg)
    if not pool:
        return ""
    return _dm_expression(cfg, _render(random.choice(pool), nickname=nickname, content=content))


def _judge(db_path, cfg: dict, nickname: str, content: str) -> tuple[bool, str]:
    """意向研判：排除名单 → 关键词 → AI。返回 (是否通过, 说明)。"""
    from app.runtime import monitor_engine

    excluded = {n.casefold() for n in _names(cfg.get("excludeUsers"))}
    if nickname.casefold() in excluded:
        return False, "在排除用户名单里"
    keywords = _names(cfg.get("keywords"))
    hit = [k for k in keywords if k.casefold() in (content or "").casefold()]
    if keywords and not hit:
        return False, "未命中关键词"
    if hit:
        return True, f"命中关键词：{hit[0]}"
    if not _enabled(cfg, "useAiJudge", default=False):
        return True, "未开启 AI 研判，按监听全部回应"
    try:
        from app.execution import live_leadgen

        result = live_leadgen.classify_intent(db_path, monitor_engine.persona_payload(
            db_path, cfg, ""), {"nickname": nickname, "text": content, "title": ""})
        if result.get("matched"):
            return True, str(result.get("reason") or "AI 判定有沟通意向")[:120]
        return False, str(result.get("reason") or "AI 判定无意向")[:120]
    except Exception as exc:  # noqa: BLE001 AI 不可用时不放行（避免乱发）
        return False, f"AI 研判失败，本轮跳过：{type(exc).__name__}"


def run(db_path, rec: dict, ctx) -> None:
    """自动回复主循环。ctx 是 runners.Ctx（stop 事件、日志、统计都在里面）。"""
    from app.runtime import monitor_engine, runners

    cfg = runners.task_config(rec)
    account_id = ctx.account_id
    watch_dm = _enabled(cfg, "watchMessages", default=True)
    exclude_groups = _enabled(cfg, "excludeGroupChats", default=True)
    max_cycles = 0
    try:
        max_cycles = int(cfg.get("runCycleLimit") or 0)
    except (TypeError, ValueError):
        max_cycles = 0
    lo, hi = _pair(cfg, "intervalSecondsMin", "intervalSecondsMax", (20.0, 60.0))
    interval = max(MIN_INTERVAL_S, lo)
    interval_hi = max(interval, hi)
    lookback = _lookback_seconds(cfg)
    limit = DEFAULT_DM_LIMIT
    if not watch_dm:
        runners.log(ctx, "未勾选「私信」监听：本轮只做作品评论（评论链路未启用时不会有动作）", "warn")
    if not _enabled(cfg, "enableAutoActions", default=True):
        runners.log(ctx, "自动回应总开关是关闭的：只记录监听到的互动，不发送任何消息", "warn")

    # 写作用域：自动回复的动作必须由本任务自己的作用域授权。
    # 评论目标是运行时才出现的，所以作用域给「本任务范围内任意目标」，
    # 具体对谁操作由每条命令的签名绑定（壳侧还要按目标做一次性 claim）。
    comment_actions = _comment_actions(cfg) if _enabled(cfg, "watchComments") else {}
    # 「评论回复」的事件源不是通知弹层，而是自己作品的评论区楼中楼（见 _replies_pass）。
    reply_actions = _reply_actions(cfg)
    # 「新增关注」「作品点赞」的事件源是顶栏通知弹层（不是 /notifications 页面）。
    notice_actions = _notice_actions(cfg)
    # notice_actions 的键是「场景桶」（follow / like），值是动作名；
    # 作用域和限额要的都是动作名本身（见 _scope_actions）。
    notice_action_names = {a for acts in notice_actions.values() for a in acts}
    actions: dict[str, int] = _scope_actions(cfg, comment_actions=comment_actions,
                                             notice_actions=notice_actions,
                                             watch_dm=watch_dm, limit=limit,
                                             reply_actions=reply_actions)
    # 关键词规则勾了「关注用户」：私信链路命中规则后要对发消息的人执行关注。
    # 壳的动作白名单按动作名整条过滤，作用域里必须带 follow-user（2026-11-18
    # 通知链路踩过：桶名写进去全被过滤成 scope_not_verified）。
    rule_follow = _dm_rule_follow_wanted(cfg, watch_dm)
    if rule_follow:
        actions["follow-user"] = max(int(actions.get("follow-user") or 0), 100)
    nickname = ""
    if comment_actions or notice_actions or reply_actions or rule_follow:
        # 顺带取自己账号昵称：壳侧核对「页头昵称+关注」时会读作用域里的 nickname，
        # 空串会直接被判 live_writes_disabled —— 通知类动作（回关/关注）也走这条校验，
        # 所以不能只在评论链路取（2026-11-18 真机踩过：签名过了，壳仍然全部拒绝）。
        _works, nickname = _own_works(db_path, ctx, account_id)
        if not nickname:
            runners.log(ctx, "没读到自己账号昵称：回关/关注会因作用域缺身份发不出去", "warn")
    if actions:
        live_scope.write_monitor_scope(task_id=ctx.task_id, account_id=account_id,
                                       actions=actions, hours=12.0, nickname=nickname)
        detail = "、".join(f"{k} ≤ {v}" for k, v in actions.items())
        runners.log(ctx, f"已生成本任务的写作用域（12 小时：{detail}）")
    else:
        runners.log(ctx, "本次没有开启「私信自动回复」或评论动作，不会发送任何内容")

    sources = []
    if watch_dm:
        sources.append("私信")
    if comment_actions:
        sources.append(f"作品评论（{'、'.join(_ACTION_LABEL.get(k, k) for k in comment_actions)}）")
    elif _enabled(cfg, "watchComments", default=False):
        sources.append("作品评论（已勾选但没开任何动作，只记录不回复）")
    if reply_actions:
        sources.append(f"评论回复（{'、'.join(_ACTION_LABEL.get(k, k) for k in reply_actions)}）")
    if notice_actions:
        sources.append("互动通知（" + "、".join(
            _NOTICE_SCENE_LABEL[k] + "→" + "/".join(_ACTION_LABEL.get(a, a)
                                                    for a in notice_actions[k])
            for k in notice_actions) + "）")
    elif _enabled(cfg, "watchLikes", default=False) or _enabled(cfg, "watchFollows", default=False):
        sources.append("点赞/新增关注（已勾选但没开任何动作，只记录不执行）")
    runners.log(ctx, f"自动回复启动：账号 {account_id}，监听 {'、'.join(sources) or '（未勾选任何监听源）'}，"
                     f"间隔 {int(interval)}-{int(interval_hi)} 秒，"
                     f"{'不限轮数' if not max_cycles else str(max_cycles) + ' 轮'}，"
                     f"通知回溯 {int(lookback // 60)} 分钟（按配置，私信不受该窗口限制）")
    if notice_actions:
        # 事件源是顶栏「通知」弹层（2026-11-18 真机实证：/notifications 页面 404，
        # 但弹层里「XX 关注了你 / 赞了你的作品」都在，且每行都带 /user/<sec_uid> 锚点）。
        runners.log(ctx, "「新增关注」「作品点赞」从顶栏通知弹层读取；"
                         "回关要落到具体的人，读不到主页标识的通知行一律跳过（不按昵称猜）")

    con = _connect(db_path)
    cycle = 0
    sent = 0
    scanned = 0
    acted_comments = 0
    acted_notices = 0
    acted_replies = 0
    # 限额按「动作」算，不按「事件源」算：页面上的「关注 / 私信」各只有一行上限，
    # 评论场景和通知场景共用同一份额度，分开算会让实际次数翻倍。
    cap_keys = sorted(set(comment_actions) | notice_action_names | set(reply_actions)
                      | ({"follow-user"} if rule_follow else set()))
    caps = _draw_caps(cfg, {key: 1 for key in cap_keys})
    used: dict[str, int] = {key: 0 for key in caps}
    reset_at = _next_reset_at(cfg)
    started = time.time()
    try:
        while not ctx.stop.is_set():
            cycle += 1
            if max_cycles and cycle > max_cycles:
                runners.log(ctx, f"已达到运行轮数上限（{max_cycles} 轮），任务收尾")
                break
            stats = {"cycle": cycle, "sent": sent, "scanned": scanned}
            try:
                if watch_dm:
                    got = _dm_pass(db_path, ctx, cfg, con, account_id, exclude_groups,
                                   lookback, limit, actions, caps, used)
                    sent += got["sent"]
                    scanned += got["scanned"]
                    stats.update({"sent": sent, "scanned": scanned})
                if comment_actions:
                    got = _comment_pass(db_path, ctx, cfg, con, account_id, lookback,
                                        actions,
                                        {k: v for k, v in caps.items() if k in comment_actions},
                                        used, nickname)
                    acted_comments += got["acted"]
                    scanned += got["scanned"]
                    stats.update({"sent": sent, "scanned": scanned,
                                  "commentActed": acted_comments})
                if reply_actions:
                    got = _replies_pass(db_path, ctx, cfg, con, account_id, lookback,
                                        reply_actions,
                                        {k: v for k, v in caps.items() if k in reply_actions},
                                        used, nickname)
                    acted_replies += got["acted"]
                    scanned += got["scanned"]
                    stats.update({"sent": sent, "scanned": scanned,
                                  "replyActed": acted_replies})
                if notice_actions:
                    got = _notice_pass(db_path, ctx, cfg, con, account_id, lookback,
                                       notice_actions, caps, used)
                    acted_notices += got["acted"]
                    scanned += got["scanned"]
                    stats.update({"sent": sent, "scanned": scanned,
                                  "noticeActed": acted_notices})
            except Exception as exc:  # noqa: BLE001 单轮失败不拖垮整个任务
                runners.log(ctx, f"第 {cycle} 轮处理异常：{type(exc).__name__}: {exc}", "error")
            runners._evt(ctx, "stats", **stats)  # noqa: SLF001 与 runner 同模块约定
            # 自动重置：到点清零本任务已用次数并重新抽取上限（任务详情/历史事件不动）
            if reset_at and time.time() >= reset_at:
                used = {key: 0 for key in used}
                caps = _draw_caps(cfg, {key: 1 for key in cap_keys})
                reset_at = _next_reset_at(cfg)
                runners.log(ctx, "已到自动重置时间：点赞/回复/关注/私信计数清零，上限已重新抽取"
                                 f"（{_caps_text(caps)}）")
            if max_cycles and cycle >= max_cycles:
                break
            wait = random.uniform(interval, interval_hi)
            runners.log(ctx, f"第 {cycle} 轮结束，{int(wait)} 秒后进入下一轮"
                             f"（本次已回复 {sent} 条私信、{acted_comments} 条评论、"
                             f"处理 {acted_notices} 条互动通知）")
            deadline = time.time() + wait
            while time.time() < deadline and not ctx.stop.is_set():
                time.sleep(0.5)
    finally:
        con.close()
        if actions:
            live_scope.clear_monitor_scope(ctx.task_id, account_id)
        runners.log(ctx, f"自动回复结束：共 {cycle} 轮、扫描 {scanned} 条新消息、"
                         f"回复 {sent} 条私信 / {acted_comments} 条评论 / "
                         f"{acted_replies} 条评论回复，"
                         f"用时 {int(time.time() - started)} 秒")


def _dm_pass(db_path, ctx, cfg: dict, con: sqlite3.Connection, account_id: str,
             exclude_groups: bool, lookback: float, limit: int,
             actions: dict, caps: dict | None = None,
             used: dict | None = None) -> dict:
    """一轮私信处理：读会话列表 → 有新内容的会话读消息 → 判断是否需要回复 → 发送。

    lookback 仅为保持签名稳定而保留：按页面原话「私信不受影响」，私信链路不按时间窗口过滤。
    caps/used 是「关注」动作的任务级额度（与评论/通知链路共用一行上限），测试可不传。
    """
    from app.runtime import monitor_engine, runners

    res = browser.execute({"type": "read-dm-conversations", "accountId": account_id,
                           "scrollScreens": 1}, db_path=db_path, account_id=account_id,
                          log=_log_cb(ctx), stop_check=ctx.stop.is_set)
    if not res.get("ok"):
        runners.log(ctx, f"读取私信会话列表失败：{res.get('error') or '未知原因'}", "warn")
        return {"sent": 0, "scanned": 0}
    conversations = [c for c in (res.get("conversations") or []) if c.get("title")]
    if not conversations:
        runners.log(ctx, "私信会话列表为空（该账号暂无会话，或页面未渲染完）")
        return {"sent": 0, "scanned": 0}

    # 按页面原话：「私信不受影响」——回溯窗口只约束通知链路，私信靠会话指纹 + seen 判新旧。
    pending: list[dict] = []
    for item in conversations:
        title = str(item.get("title") or "").strip()
        fingerprint = f"{item.get('time') or ''}|{item.get('preview') or ''}"
        if _conversation_fingerprint(con, ctx.task_id, account_id, title) == fingerprint:
            continue
        _mark_conversation(con, ctx.task_id, account_id, title, fingerprint)
        pending.append(item)
    con.commit()
    if not pending:
        runners.log(ctx, f"共 {len(conversations)} 个会话，没有新消息")
        return {"sent": 0, "scanned": 0}

    # 每轮最多读 8 个会话：每次打开会话 ~3 秒，读太多会让一轮拖到十几分钟
    batch = pending[:8]
    runners.log(ctx, f"发现 {len(pending)} 个会话有新内容，本轮读取 {len(batch)} 个")
    threads = browser.execute({"type": "read-dm-threads", "accountId": account_id,
                               "titles": [t["title"] for t in batch], "limit": 12},
                              db_path=db_path, account_id=account_id,
                              log=_log_cb(ctx), stop_check=ctx.stop.is_set)
    if not threads.get("ok"):
        runners.log(ctx, f"读取会话消息失败：{threads.get('error') or '未知原因'}", "warn")
        return {"sent": 0, "scanned": 0}

    sent = 0
    scanned = 0
    for thread in threads.get("threads") or []:
        if ctx.stop.is_set():
            break
        title = str(thread.get("title") or "")
        if not thread.get("found"):
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="dm",
                   conversation=title, outcome="skipped",
                   note=str(thread.get("reason") or "会话未找到"))
            continue
        if thread.get("isGroup") and exclude_groups:
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="dm",
                   conversation=title, outcome="skipped", note="群聊（已勾选排除群聊）")
            continue
        messages = thread.get("messages") or []
        if not messages:
            continue
        last = messages[-1]
        scanned += 1
        content = str(last.get("text") or "").strip()
        # 对方主页标识（壳侧会话面板锚点）：关键词规则勾「关注用户」时用它下发
        # follow-user；读不到就是空串，关注这步跳过、私信照发（不按昵称猜人）。
        uid = str(thread.get("uid") or "").strip()
        if last.get("fromMe"):
            # 最后一句是我说的：等对方回，不重复追问
            continue
        if not content:
            continue
        event_key = hashlib.sha256(f"{title}|{content}".encode("utf-8")).hexdigest()[:16]
        if store.seen_has(db_path, ctx.task_id, f"dm|{event_key}"):
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="dm",
                   conversation=title, nickname=title, content=content, outcome="dedup",
                   note="该消息已处理过")
            continue
        # 新私信实时提示：只对「真的没见过」的消息发，去重过的不再重复提醒
        _emit_chat_new_message(db_path, account_id, title)

        def done(outcome: str, note: str = "", actions=None, retryable: bool = False):
            """收尾一条消息：默认记为已处理；可重试的技术问题不记，留给下一轮。"""
            if not retryable:
                store.seen_mark(db_path, ctx.task_id, [f"dm|{event_key}"])
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="dm",
                   conversation=title, nickname=title, content=content,
                   outcome=outcome, note=note, actions=actions)

        ok, reason = _judge(db_path, cfg, title, content)
        if not ok:
            done("filtered", reason)
            runners.log(ctx, f"@{title} 跳过私信回复：{reason}")
            continue

        max_rounds = 0
        try:
            max_rounds = int(cfg.get("actionUserMaxRounds") or 0)
        except (TypeError, ValueError):
            max_rounds = 0
        user_key = _user_key(title)
        reply_mode = _reply_mode(cfg, "dm")
        if reply_mode == "template":
            # 通用话术：每位用户仅自动触发一次（页面原话：「通用话术每位用户仅自动
            # 触发一次，与回复轮数设置无关」，跨通知共用、重启继承记录）。
            if _user_rounds(con, account_id, user_key) >= 1:
                done("skipped", "通用话术对该用户已自动触发过（每位用户仅一次，重启继承）")
                continue
        elif reply_mode != "rules" and max_rounds and _user_rounds(
                con, account_id, user_key) >= max_rounds:
            # 关键词规则的轮数是「各规则独立」的（见规则命中分支），不共用用户级门。
            done("skipped", f"该用户已达回复上限（{max_rounds} 轮）")
            continue

        if not _enabled(cfg, "actionMessageReply", default=False):
            done("matched", f"{reason}；未勾选「私信自动回复」，不发送")
            continue
        if "send-dm-chat" not in actions:
            done("skipped", "缺少私信写作用域，未发送", retryable=True)
            continue
        if not monitor_engine.quota_take(db_path, account_id, "send-dm", limit, cfg):
            done("skipped", f"单账号当日私信额度已用完（{limit} 条）")
            runners.log(ctx, f"@{title} 跳过私信回复：当日额度已用完")
            continue

        # 跨账号私信去重（dmCrossAccountDedupe）：同一任务内同一线索只由一个账号私信。
        holder = _dm_cross_claim(con, cfg, ctx.task_id, user_key, account_id)
        if holder and holder != account_id:
            done("skipped", f"跨账号私信去重：该用户已由账号 {holder} 私信")
            runners.log(ctx, f"@{title} 跳过私信回复：跨账号去重（已由 {holder} 处理）")
            continue

        # 组装整组消息：页面口径「私信命中后按消息顺序全部发送，每个消息框内随机
        # 选一行话术，整组算一轮」——所以配额、轮数、通用一次都在组级记账。
        group_texts: list[str] = []
        group_note = ""
        rule_id = ""
        matched_rule: dict | None = None
        if reply_mode == "rules":
            rule, rule_index = _match_dm_rule(cfg, content=content)
            if rule is None:
                fallback = str(cfg.get("dmRuleFallback") or "skip")
                if fallback != "general":
                    done("matched", "未命中关键词规则（兜底：仅记录，不回复）")
                    continue
                if _user_rounds(con, account_id, user_key) >= 1:
                    done("matched", "未命中关键词规则；通用话术对该用户已触发过（仅自动触发一次）")
                    continue
                group_texts = _general_group(cfg, title, content)
                group_note = "未命中规则，按兜底使用通用话术；"
            else:
                rid = str(rule.get("id") or f"rule-{rule_index + 1}")
                if max_rounds and _rule_rounds(con, ctx.task_id, account_id, rid,
                                               user_key) >= max_rounds:
                    done("skipped", f"规则「{rule.get('name') or rid}」对该用户已达回复轮数上限"
                                    f"（{max_rounds} 轮）")
                    continue
                group_texts = _rule_group_texts(cfg, rule, nickname=title, content=content)
                rule_id = rid
                matched_rule = rule
        else:
            if reply_mode == "template":
                # 通用话术组：rotate 单条 / sequence（连续多条）按框顺序整组
                group_texts = _general_group(cfg, title, content)
            else:
                # 专业模式要判「是否首次触达」：同一任务同一账号此前从未自动触达该用户，
                # 才允许落到第五组（首次触达兜底）。rounds 只在发送成功后 +1，正好是这个语义。
                touched = _user_rounds(con, account_id, user_key) > 0
                text = _dm_text(db_path, cfg, account_id, title, content, touched=touched)
                group_texts = [text] if text else []
        if not group_texts:
            mode_now = reply_mode
            if mode_now == "ai":
                done("skipped", f"AI 未生成可用私信（本轮已尝试 {MAX_DM_AI_TRIES} 次），"
                                "按页面口径不降级固定话术，等下一轮", retryable=True)
                runners.log(ctx, f"@{title} 私信 AI 本轮 {MAX_DM_AI_TRIES} 次未出稿，"
                                 "不降级固定话术", "warn")
            elif mode_now == "professional":
                _rule, why = _match_professional_rule(
                    cfg, content=content, touched=touched)
                done("skipped", f"专业模式无规则命中：{why}；按页面口径不发送")
                runners.log(ctx, f"@{title} 专业模式未命中规则：{why}")
            elif mode_now == "rules":
                done("skipped", "命中规则但没有可发送的话术（规则的消息框为空）")
            else:
                done("skipped", "没有可用的私信话术（模板为空）")
            continue
        group_texts = [t[:500] for t in group_texts]

        # 关键词规则勾了「关注用户」：命中规则后先关注发消息的人，再发私信组。
        # 关注动作失败不影响私信发送（页面上两个动作互相独立）；读不到对方主页
        # 标识就跳过关注（不按昵称猜人），私信照发。
        follow_receipt: dict | None = None
        if (reply_mode == "rules" and rule_id and matched_rule is not None
                and _enabled(cfg, "enableAutoActions", default=True)
                and matched_rule.get("followEnabled")):
            rule_name = str(matched_rule.get("name") or rule_id)
            if not uid:
                runners.log(ctx, f"@{title} 没读到对方主页标识，规则「{rule_name}」"
                                 "的「关注用户」本轮不执行（私信照发）", "warn")
            elif "follow-user" not in actions:
                runners.log(ctx, f"@{title} 作用域里没有关注动作，规则「{rule_name}」"
                                 "的「关注用户」未执行（重启任务后生效）", "warn")
            else:
                cap = int((caps or {}).get("follow-user") or 0)
                if cap and int((used or {}).get("follow-user") or 0) >= cap:
                    runners.log(ctx, f"@{title} 关注已达本次任务上限（{cap}），"
                                     f"规则「{rule_name}」的「关注用户」跳过", "warn")
                else:
                    follow_receipt = _comment_step(db_path, ctx, cfg, account_id,
                                                   "follow-user", video_url="chat",
                                                   comment_id="", comment_author=title,
                                                   recipient_id=uid, text="", delay=False)
                    if follow_receipt["ok"]:
                        if used is not None:
                            used["follow-user"] = int(used.get("follow-user") or 0) + 1
                        runners.log(ctx, f"@{title} 规则「{rule_name}」关注用户成功")
                    else:
                        runners.log(ctx, f"@{title} 规则「{rule_name}」关注用户未成功"
                                         f"（{follow_receipt.get('code')}），继续发送私信", "warn")

        receipts: list[dict] = []
        ok_count = 0
        lo, hi = _pair(cfg, "dmMessageIntervalMin", "dmMessageIntervalMax", (5.0, 10.0))
        for position, text in enumerate(group_texts):
            if ctx.stop.is_set():
                receipts.append({"action": "send-dm-chat", "ok": False,
                                 "status": "stopped", "code": "stopped"})
                break
            if position == 0:
                # 每次自动操作前等待（频率控制）：整组发送前等一次，
                # 组内消息之间按「连续消息间隔」走。
                _action_delay(cfg, ctx)
            else:
                wait = random.uniform(max(2.0, lo), max(3.0, hi))
                deadline = time.time() + wait
                while time.time() < deadline and not ctx.stop.is_set():
                    time.sleep(0.5)
            if ctx.stop.is_set():
                receipts.append({"action": "send-dm-chat", "ok": False,
                                 "status": "stopped", "code": "stopped"})
                break
            try:
                cmd = live_scope.signed_command("send-dm-chat", account_id=account_id,
                                                task_id=ctx.task_id, video_url="chat",
                                                text=text, comment_author=title)
            except ValueError as exc:
                receipts.append({"action": "send-dm-chat", "ok": False, "status": "skipped",
                                 "code": "scope_not_verified", "error": str(exc)})
                break
            result = browser.execute(cmd, db_path=db_path, account_id=account_id,
                                     log=_log_cb(ctx), stop_check=ctx.stop.is_set,
                                     timeout=240.0)
            receipts.append({
                "action": "send-dm-chat",
                "ok": bool(result.get("ok")),
                "status": result.get("status") or ("sent_ok" if result.get("ok") else "failed"),
                "code": result.get("code") or ("sent" if result.get("ok") else "unknown"),
            })
            if receipts[-1]["ok"]:
                ok_count += 1
        first = receipts[0] if receipts else {}
        if ok_count:
            sent += ok_count
            _bump_user(con, account_id, user_key)
            if rule_id:
                _bump_rule(con, ctx.task_id, account_id, rule_id, user_key)
            con.commit()
            preview = "、".join(group_texts[:2])
            done("acted", f"{reason}；{group_note}"
                          f"已发送 {ok_count}/{len(group_texts)} 条：{preview[:60]}",
                 actions=([follow_receipt] if follow_receipt else []) + receipts)
            runners.log(ctx, f"@{title} 已自动回复私信（{receipts[0]['code']}，"
                             f"{ok_count}/{len(group_texts)} 条）")
            _notify(db_path, cfg, ctx, title, content, group_texts[0])
        else:
            # 页面类失败（会话没打开/编辑器不可用/桌面端不在线）下次还能再试；
            # uncertain 绝不重试，否则可能给对方发两遍同样的话。
            retryable = first.get("status") != "uncertain" and first.get("code") in _RETRYABLE_CODES
            # 环境类问题记 skipped（不是「发出去但失败」），下次还能再试；
            # 真失败才记 failed，方便页面按「失败」筛。
            outcome = "skipped" if (first.get("status") == "uncertain" or retryable) else "failed"
            done(outcome, f"发送未确认：{first.get('code')}",
                 actions=([follow_receipt] if follow_receipt else []) + receipts,
                 retryable=retryable)
            runners.log(ctx, f"@{title} 私信发送未确认：{first.get('code')}"
                             f"（{'不重试' if first.get('status') == 'uncertain' else '已跳过'}）",
                        "warn")
        # 两条私信之间留间隔：页面连续发送容易被判刷屏
        lo, hi = _pair(cfg, "dmMessageIntervalMin", "dmMessageIntervalMax", (5.0, 10.0))
        wait = random.uniform(max(2.0, lo), max(3.0, hi))
        deadline = time.time() + wait
        while time.time() < deadline and not ctx.stop.is_set():
            time.sleep(0.5)
    return {"sent": sent, "scanned": scanned}


def _notify(db_path, cfg: dict, ctx, title: str, content: str, reply: str) -> None:
    from app.runtime import monitor_engine, runners

    if not _enabled(cfg, "enableWebhook", default=False):
        return
    try:
        result = monitor_engine.notify(cfg, {"nickname": title, "comment": content,
                                             "video": "", "url": "", "level": "B",
                                             "score": 0, "source": "autoreply"}, ctx.name)
        if not result.get("ok"):
            # 推送失败要立刻让用户知道（消息中心红字），不能只躺在运行日志里
            _emit_webhook_failure(result)
    except Exception as exc:  # noqa: BLE001 推送失败不影响回复
        runners.log(ctx, f"消息推送失败：{type(exc).__name__}: {exc}", "warn")
        _emit_webhook_failure({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------
# 作品评论链路
# ---------------------------------------------------------------------------
# 事件源（2026-11-17 真机确认）：抖音 PC 网页版没有互动通知页，但
#   ① 自己作品列表：/user/self 主页（read-author-works）
#   ② 每条作品的评论：视频页评论区（read-comments，带 commentId/userId/ageMinutes）
# 两个只读通道都在跑，所以「有人在我视频下留言」是**有真实事件源**的。
# 「有人回复了我的评论」仍然没有：那需要知道自己评论过哪些别人的视频，
# PC 端楼中楼展开不稳定，本模块如实跳过，不拿评论区全量冒充。
SELF_WORKS_URL = "https://www.douyin.com/user/self"
WORKS_TTL_S = 1800.0        # 作品列表半小时刷一次（新发作品最多半小时内被发现）
COMMENT_VIDEOS_PER_PASS = 4  # 每轮最多读 4 个作品的评论区
COMMENT_LIMIT_PER_VIDEO = 30
COMMENT_ACTIONS_PER_PASS = 3  # 每轮最多处理 3 条新评论（每条要开页面，别让一轮拖太久）
MAX_COMMENT_AI_TRIES = 5     # 页面原话：评论回复只走 AI，本轮最多尝试 5 次
MAX_DM_AI_TRIES = 5          # 私信同一条口径：「本轮最多尝试 5 次，不降级固定话术」

_ACTION_LABEL = {
    "like-comment": "点赞评论",
    "reply-comment": "回复评论",
    "follow-user": "关注对方",
    "send-dm": "私信对方",
}
_COMMENT_ACTION_FIELDS = (
    ("like-comment", "actionCommentLike"),
    ("reply-comment", "actionCommentReply"),
    ("follow-user", "actionCommentFollow"),
    ("send-dm", "actionCommentDm"),
)
# 动作 -> 单账号上限的种类（页面上的「点赞 / 回复 / 关注 / 私信」四行）
_ACTION_LIMIT_KIND = {
    "like-comment": "like",
    "reply-comment": "reply",
    "follow-user": "follow",
    "send-dm": "dm",
}
_COMMENT_FIELDS = ("type", "accountId", "leadgenTaskId", "videoUrl", "commentId", "text",
                   "scopeId", "deadlineAt", "commentAuthor", "recipientId")


def _comment_actions(cfg: dict) -> dict[str, int]:
    """本任务要执行哪些评论动作（只认页面上的四个开关）。"""
    return {key: 1 for key, field in _COMMENT_ACTION_FIELDS if _enabled(cfg, field)}


def _draw_caps(cfg: dict, comment_actions: dict[str, int]) -> dict[str, int]:
    """每个动作在**本次任务**内的上限；0 表示不限。

    页面口径：「每次启动重新抽取上限」「到设定时间清零并重新抽取」
    —— 所以这里是内存态，重启任务自然从零开始。
    """
    from app.runtime import monitor_engine

    caps: dict[str, int] = {}
    for action in comment_actions:
        kind = _ACTION_LIMIT_KIND.get(action, "")
        caps[action] = monitor_engine.draw_limit(cfg, kind) if kind else 0
    return caps


def _caps_text(caps: dict[str, int]) -> str:
    if not caps:
        return "无动作"
    return "、".join(f"{_ACTION_LABEL.get(k, k)}{'不限' if not v else '≤' + str(v)}"
                     for k, v in caps.items())


def _next_reset_at(cfg: dict) -> float:
    """下一次自动重置的时间点；没开启返回 0。"""
    if not _enabled(cfg, "actionCountAutoResetEnabled"):
        return 0.0
    raw = str(cfg.get("actionCountAutoResetTime") or "00:00")
    try:
        hour, minute = (int(x) for x in raw.split(":")[:2])
    except (TypeError, ValueError):
        hour, minute = 0, 0
    hour = min(23, max(0, hour))
    minute = min(59, max(0, minute))
    now = time.localtime()
    target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))
    if target <= time.time():
        target += 86400
    return target


def _own_works(db_path, ctx, account_id: str) -> tuple[list[dict], str]:
    """自己最近的作品 + 账号昵称（昵称要写进作用域，壳会拿它核对登录身份）。"""
    cache = getattr(_own_works, "_cache", {})
    key = (ctx.task_id, account_id)
    now = time.time()
    hit = cache.get(key)
    if hit and now - hit[0] < WORKS_TTL_S:
        return hit[1], hit[2]
    from app.runtime import runners

    res = browser.execute({"type": "read-author-works", "accountId": account_id,
                           "authorUrls": [SELF_WORKS_URL], "limit": 6},
                          db_path=db_path, account_id=account_id, log=_log_cb(ctx),
                          stop_check=ctx.stop.is_set)
    if not res.get("ok"):
        runners.log(ctx, f"读取自己作品列表失败：{res.get('error') or '未知原因'}", "warn")
        return (hit[1], hit[2]) if hit else ([], "")
    items = [i for i in (res.get("items") or []) if isinstance(i, dict) and i.get("url")]
    nickname = str(res.get("author") or "")
    if items:
        cache[key] = (now, items, nickname)
        _own_works._cache = cache
    return items, nickname


def _comment_pool(cfg: dict) -> list[str]:
    """评论回复模板：一行一条，自动轮询。"""
    pool: list[str] = []
    items = cfg.get("replyTemplateItems")
    if isinstance(items, list):
        for item in items:
            text = item.get("text") if isinstance(item, dict) else item
            if isinstance(text, str) and text.strip():
                pool.append(text.strip())
    single = str(cfg.get("replyTemplate") or "").strip()
    if single:
        pool.append(single)
    return pool


def _comment_expression(cfg: dict, text: str) -> str:
    """评论侧随机表情：与评论获客共用同一套表情池。"""
    from app.runtime import monitor_engine

    on = _enabled(cfg, "enableCommentExpression") or _enabled(cfg, "commentUseRandomSuffix")
    if not on or not text:
        return text
    try:
        return monitor_engine._suffix(text, True)  # noqa: SLF001 同包内共用表情池
    except Exception:  # noqa: BLE001 表情只是锦上添花，不能影响发送
        return text


def _comment_age_minutes(raw: str, now: float | None = None) -> int | None:
    """把评论时间文案换成「多少分钟前」。

    页面侧的解析只认「N 分钟/小时/天前 · 昨天 · 今天」，真机上评论时间常是
    「11月05日 20:42」这种绝对日期，会被判成「时间无法确认」而整批丢弃
    （2026-11-17 实测：4 个作品的评论全被滤掉，看起来像没评论）。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    stamp = time.localtime(now if now is not None else time.time())
    if "刚刚" in text or "今天" in text:
        return 0
    if "昨天" in text:
        return 24 * 60
    rel = re.search(r"(\d+)\s*(分钟|小时|天|周|月|年)前", text)
    if rel:
        unit = {"分钟": 1, "小时": 60, "天": 1440, "周": 10080,
                "月": 43200, "年": 525600}.get(rel.group(2))
        return int(rel.group(1)) * unit if unit else None
    absolute = re.search(r"(?:(\d{4})[年\-/])?(\d{1,2})[月\-/](\d{1,2})日?", text)
    if not absolute:
        return None
    year = int(absolute.group(1)) if absolute.group(1) else stamp.tm_year
    try:
        target = time.mktime((year, int(absolute.group(2)), int(absolute.group(3)),
                              0, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return None
    if not absolute.group(1) and target > (now if now is not None else time.time()):
        target = time.mktime((year - 1, int(absolute.group(2)), int(absolute.group(3)),
                              0, 0, 0, 0, 0, -1))   # 没写年份且比现在晚 → 去年
    return max(0, int(((now if now is not None else time.time()) - target) // 60))


def _comment_text(db_path, cfg: dict, account_id: str, nickname: str, content: str,
                  cursor: int) -> dict:
    """评论回复正文。

    页面原话：「评论回复只使用 AI 生成；本轮最多尝试 5 次，仍未成功则记录失败
    并等待下轮处理，**不降级固定话术**」。所以 AI 模式失败就是失败，绝不偷偷
    换成模板 —— 那会让用户以为 AI 在回，实际是模板在回。
    """
    mode = _reply_mode(cfg, "comment")
    if mode != "ai":
        pool = _comment_pool(cfg)
        if not pool:
            return {"ok": False, "code": "empty_template",
                    "reason": "评论回复模板为空：已选择自定义模板但一行都没填"}
        body = _render(pool[cursor % len(pool)], nickname=nickname, content=content)
        return {"ok": True, "text": _comment_expression(cfg, body), "mode": "template"}
    from app.runtime import monitor_engine

    last = "AI 未返回内容"
    for _ in range(MAX_COMMENT_AI_TRIES):
        draft = monitor_engine.reply_text(db_path, {**cfg, "replyMode": "ai"}, account_id,
                                          {"nickname": nickname, "text": content})
        body = str(draft.get("text") or "").strip() if draft.get("ok") else ""
        if body:
            return {"ok": True, "text": _comment_expression(cfg, body), "mode": "ai"}
        last = str(draft.get("reason") or draft.get("code") or last)
        time.sleep(0.8)
    return {"ok": False, "code": "ai_failed",
            "reason": f"AI 生成失败（本轮已尝试 {MAX_COMMENT_AI_TRIES} 次）：{last}"}


def _action_delay(cfg: dict, ctx) -> None:
    """每次自动操作前的随机等待（页面：频率控制 · 每次自动操作前等待）。"""
    lo, hi = _pair(cfg, "actionDelayMin", "actionDelayMax", (20.0, 30.0))
    wait = random.uniform(lo, hi)
    deadline = time.time() + wait
    while time.time() < deadline and not ctx.stop.is_set():
        time.sleep(0.5)


def _comment_step(db_path, ctx, cfg: dict, account_id: str, action: str, *,
                  video_url: str, comment_id: str, comment_author: str,
                  recipient_id: str, text: str, delay: bool = True) -> dict:
    """下发一条评论相关写动作，返回回执。delay=False 跳过操作前等待（整组消息的组内间隔自己管）。"""
    try:
        cmd = live_scope.signed_command(action, account_id=account_id, task_id=ctx.task_id,
                                        video_url=video_url, text=text,
                                        comment_id=comment_id, comment_author=comment_author,
                                        recipient_id=recipient_id)
    except ValueError as exc:
        # 签名失败只有一句「不在验收范围」，看不出到底缺哪一项。把作用域现状带上：
        # 这类失败真机上往往只是「动作没写进作用域」或「锚点视频不在白名单」。
        try:
            sc = live_scope.load_scope(action, account_id=account_id, task_id=ctx.task_id)
        except Exception:  # noqa: BLE001 诊断本身不能把动作变成异常
            sc = {}
        detail = (f"（作用域{'已加载' if sc else '缺失'}，动作={sorted((sc.get('actions') or {}))}，"
                  f"目标={'任意' if '*' in (sc.get('videoUrls') or []) else (sc.get('videoUrls') or [])}）")
        return {"action": action, "ok": False, "status": "skipped",
                "code": "scope_not_verified", "error": str(exc) + detail}
    _action_delay(cfg, ctx) if delay else None   # 每次自动操作前随机等待，别让页面看着像脚本
    if ctx.stop.is_set():
        return {"action": action, "ok": False, "status": "stopped", "code": "stopped"}
    result = browser.execute(cmd, db_path=db_path, account_id=account_id,
                             log=_log_cb(ctx), stop_check=ctx.stop.is_set, timeout=180.0)
    return {"action": action,
            "ok": bool(result.get("ok")),
            "status": result.get("status") or ("sent_ok" if result.get("ok") else "failed"),
            "code": result.get("code") or ("sent_ok" if result.get("ok") else "unknown"),
            "text": text[:120]}


def _comment_pass(db_path, ctx, cfg: dict, con: sqlite3.Connection, account_id: str,
                  lookback: float, actions: dict, caps: dict, used: dict,
                  nickname: str) -> dict:
    """一轮作品评论处理：读自己作品 → 读评论 → 判新 → 研判 → 四个动作。"""
    from app.runtime import runners

    comment_actions = [k for k in caps]
    if not comment_actions:
        return {"scanned": 0, "acted": 0}
    works, self_name = _own_works(db_path, ctx, account_id)
    self_name = self_name or nickname
    if not works:
        runners.log(ctx, "没读到自己作品列表，本轮跳过作品评论", "warn")
        return {"scanned": 0, "acted": 0}
    urls = [str(w["url"]) for w in works[:COMMENT_VIDEOS_PER_PASS]]
    minutes = int(max(1, min(60 * 24 * 60, lookback // 60)))   # 最多 60 天
    # 时间窗口一律在后端判定：页面的年龄解析不认「11月05日 20:42」这种绝对日期，
    # 会让整批评论被当成「时间无法确认」丢掉（真机踩过）。
    res = browser.execute({"type": "read-comments", "accountId": account_id,
                           "videoUrls": urls, "limit": COMMENT_LIMIT_PER_VIDEO,
                           "commentWindowMinutes": 0},
                          db_path=db_path, account_id=account_id, log=_log_cb(ctx),
                          stop_check=ctx.stop.is_set, timeout=300.0)
    if not res.get("ok"):
        runners.log(ctx, f"读取作品评论失败：{res.get('error') or '未知原因'}", "warn")
        return {"scanned": 0, "acted": 0}
    comments: list[dict] = []
    too_old = unknown = 0
    for item in res.get("comments") or []:
        age = item.get("ageMinutes")
        if not isinstance(age, (int, float)):
            age = _comment_age_minutes(str(item.get("publishedText") or ""))
        if age is None:
            unknown += 1          # 页面原话：时间无法确认的先不处理
            continue
        if age > minutes:
            too_old += 1
            continue
        comments.append({**item, "ageMinutes": age})
    if not comments:
        runners.log(ctx, f"读了 {len(urls)} 个作品的评论区：{len(res.get('comments') or [])} 条里"
                         f"没有 {minutes} 分钟内的新评论（超出窗口 {too_old}，时间无法确认 {unknown}）")
        return {"scanned": 0, "acted": 0}

    scanned = 0
    acted = 0
    handled = 0
    cursor = 0
    for item in comments:
        if ctx.stop.is_set() or handled >= COMMENT_ACTIONS_PER_PASS:
            break
        nick = str(item.get("nickname") or "").strip()
        content = str(item.get("text") or "").strip()
        video_url = str(item.get("videoUrl") or "").strip()
        comment_id = str(item.get("commentId") or "").strip()
        user_id = str(item.get("userId") or "").strip()
        if not content or not video_url:
            continue
        # 系统提示（群聊卡片等）不是用户留言
        if content.startswith("群聊 ·") or content.startswith("置顶"):
            continue
        # 自己发的评论/自己的回复：真机上作者回复的昵称是「昵称 作者」，
        # 所以要用前缀比对，否则会把「自己回自己」当成新评论再回一遍。
        # isAuthor 是壳侧读出的作者徽章，比昵称前缀可靠（对方可能改名）。
        if item.get("isAuthor"):
            continue
        if self_name and (nick == self_name or nick.startswith(self_name)):
            continue
        # 楼中楼里的子回复交给「评论回复」链路（watchReplies）处理。
        # 不在这里切开的话，同一条回复会被两条链路各回一次。
        if int(item.get("depth") or 0) > 0:
            continue
        scanned += 1
        event_key = hashlib.sha256(
            f"{item.get('videoId') or video_url}|{comment_id or nick + '|' + content}"
            .encode("utf-8")).hexdigest()[:16]
        caption = str(item.get("caption") or "")[:60]

        def done(outcome: str, note: str = "", receipts=None, retryable: bool = False):
            if not retryable:
                store.seen_mark(db_path, ctx.task_id, [f"cm|{event_key}"])
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="comment",
                   conversation=caption, nickname=nick, content=content,
                   outcome=outcome, note=note, actions=receipts)

        if store.seen_has(db_path, ctx.task_id, f"cm|{event_key}"):
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="comment",
                   conversation=caption, nickname=nick, content=content,
                   outcome="dedup", note="该评论已处理过")
            continue

        ok, reason = _judge(db_path, cfg, nick, content)
        if not ok:
            done("filtered", reason)
            runners.log(ctx, f"@{nick} 的评论跳过：{reason}")
            continue

        receipts: list[dict] = []
        text_reply = ""
        skipped_reason = ""
        for action in comment_actions:
            cap = int(caps.get(action) or 0)
            if cap and int(used.get(action) or 0) >= cap:
                skipped_reason = f"{_ACTION_LABEL.get(action, action)}已达本次任务上限（{cap}）"
                continue
            if action == "send-dm" and not user_id:
                skipped_reason = "评论里没拿到用户主页标识，私信无法定位收件人"
                continue
            text = ""
            if action == "like-comment":
                # 壳侧把 text 当作「这条评论的正文」，用来核对点的确实是这一条
                # （comment-like 要求 commentId + 作者 + 正文三者同时匹配）。
                text = content[:300]
            elif action == "reply-comment":
                draft = _comment_text(db_path, cfg, account_id, nick, content, cursor)
                if not draft.get("ok"):
                    receipts.append({"action": action, "ok": False, "status": "failed",
                                     "code": draft.get("code"), "error": draft.get("reason")})
                    skipped_reason = str(draft.get("reason") or "")
                    continue
                text = str(draft["text"])[:300]
                text_reply = text
                cursor += 1
            elif action == "send-dm":
                text = _dm_text(db_path, cfg, account_id, nick, content)[:300]
                if not text:
                    skipped_reason = "私信话术为空"
                    continue
            receipt = _comment_step(db_path, ctx, cfg, account_id, action,
                                    video_url=video_url, comment_id=comment_id,
                                    comment_author=nick, recipient_id=user_id, text=text)
            receipts.append(receipt)
            if receipt["ok"]:
                used[action] = int(used.get(action) or 0) + 1
                handled += 1
                acted += 1
                runners.log(ctx, f"@{nick} {_ACTION_LABEL.get(action, action)}成功"
                                 f"（{receipt['code']}）")
            elif str(receipt.get("code") or "") in _ALREADY_CODES:
                # 已经是目标状态：算「已处理」，但不算本轮的自动动作次数。
                handled += 1
                runners.log(ctx, f"@{nick} {_ACTION_LABEL.get(action, action)}无需动作："
                                 f"{receipt.get('code')}（目标已是该状态，不重复执行）")
            else:
                runners.log(ctx, f"@{nick} {_ACTION_LABEL.get(action, action)}未成功："
                                 f"{receipt.get('code')}", "warn")
            if ctx.stop.is_set():
                break

        if not receipts:
            done("matched", f"{reason}；{skipped_reason or '没有可执行的动作'}")
            continue
        success = [r for r in receipts if r["ok"]]
        if success:
            note = f"{reason}；" + "、".join(
                f"{_ACTION_LABEL.get(r['action'], r['action'])}"
                f"{('：' + r['text']) if r.get('text') and r['action'] in ('reply-comment', 'send-dm') else ''}"
                for r in success)
            done("acted", note, receipts)
            _notify(db_path, cfg, ctx, nick, content, text_reply)
        else:
            retryable = any(r.get("status") != "uncertain"
                            and r.get("code") in _RETRYABLE_CODES for r in receipts)
            already = [r for r in receipts
                       if str(r.get("code") or "") in _ALREADY_CODES]
            if already and len(already) == len(receipts):
                # 每条都只是「本来就是这个状态」：记 skipped，不记 failed。
                done("skipped", f"{reason}；目标已是该状态，未重复执行", receipts)
                continue
            done("skipped" if retryable or any(
                r.get("status") == "uncertain" for r in receipts) else "failed",
                 f"{reason}；" + (skipped_reason or "动作未确认"), receipts,
                 retryable=retryable)
    return {"scanned": scanned, "acted": acted}


# --------------------------------------------------------------------------
# 评论回复：有人回复了我的评论
# --------------------------------------------------------------------------
def _replies_pass(db_path, ctx, cfg: dict, con: sqlite3.Connection, account_id: str,
                  lookback: float, actions: dict, caps: dict, used: dict,
                  nickname: str) -> dict:
    """一轮「评论回复」处理：进我的作品 → 读评论区楼中楼 → 找「别人回复了我的评论」。

    为什么必须进作品，而不是翻通知：抖音顶栏通知弹层只有「赞了你的作品 / 关注了你 /
    赞了你的评论」三类，47 条历史里一条「回复了你的评论」都没有（2026-11-18 实证）。
    真实的回复关系就在自己作品的评论区里。

    判定要同时满足，缺一不可：
      1. 子项 depth > 0：确实挂在某条评论下面，不是一级评论
      2. 同一楼中楼里有我发的评论 —— 我没在这层楼里说过话，别人发言就与我无关
      3. 子项不是我，且比我最后一次发言更晚（age 更小）：别人回我的前提是我先说过话
    """
    from app.runtime import runners

    if not actions:
        return {"scanned": 0, "acted": 0}
    works, self_name = _own_works(db_path, ctx, account_id)
    self_name = self_name or nickname
    if not works:
        runners.log(ctx, "没读到自己作品列表，本轮跳过评论回复", "warn")
        return {"scanned": 0, "acted": 0}
    urls = [str(w["url"]) for w in works[:COMMENT_VIDEOS_PER_PASS]]
    # 时间窗口一律在后端判：页面那套年龄解析不认「11月05日 20:42」这种绝对日期。
    res = browser.execute({"type": "read-comments", "accountId": account_id,
                           "videoUrls": urls, "limit": REPLY_LIMIT_PER_VIDEO,
                           "expandSubComments": True, "commentWindowMinutes": 0},
                          db_path=db_path, account_id=account_id, log=_log_cb(ctx),
                          stop_check=ctx.stop.is_set, timeout=300.0)
    if not res.get("ok"):
        runners.log(ctx, f"读取作品评论区失败：{res.get('error') or '未知原因'}", "warn")
        return {"scanned": 0, "acted": 0}

    raw = [c for c in (res.get("comments") or []) if isinstance(c, dict)]
    minutes = int(max(1, min(60 * 24 * 60, lookback // 60)))

    def mine_of(c: dict) -> bool:
        """这条评论是不是我发的。

        isAuthor 来自页面的「作者」徽章，只在作品作者本人视角稳定 ——
        换第二个账号读同一页时徽章读不到、isAuthor 全 false（真机实测）。
        所以昵称兜底必须留着。
        """
        if c.get("isAuthor"):
            return True
        return bool(self_name and str(c.get("nickname") or "").strip() == self_name)

    def age_of(c: dict):
        age = c.get("ageMinutes")
        if not isinstance(age, (int, float)):
            age = _comment_age_minutes(str(c.get("publishedText") or ""))
        return age

    # 按一级评论分组。抖音的二级评论在 DOM 上是**扁平一层**：我回复了「1A.」，
    # 别人回复我，两者 depth 都是 1、parent 都是「1A.」（2026-11-18 真机实测），
    # 所以「父评论是不是我」判不出「谁回复谁」。平台评论接口有 reply_id，但要
    # 签名参数，页面内直接 fetch 会超时（14s 拿不到）。
    # 可用的判据：同一楼中楼里，**在我最后一次发言之后**出现的新回复就是有人回了我 ——
    # 别人回我的前提是我先在这层楼里说过话。
    groups: dict[int, list[dict]] = {}
    toomany = 0
    for item in raw:
        if int(item.get("depth") or 0) <= 0:
            continue
        parent_idx = item.get("parentIdx")
        if not isinstance(parent_idx, int) or parent_idx < 0 or parent_idx >= len(raw):
            toomany += 1
            continue
        groups.setdefault(parent_idx, []).append(item)

    replies: list[dict] = []
    for parent_idx, siblings in groups.items():
        mine_ages = [a for a in (age_of(c) for c in siblings if mine_of(c))
                     if isinstance(a, (int, float))]
        if not mine_ages:
            # 这层楼里我没说过话 → 别人在这里发言不是「回复我」
            continue
        last_mine_age = min(mine_ages)     # age 越小越新 = 我最后一次发言
        for item in siblings:
            if mine_of(item):
                continue
            age = age_of(item)
            if age is None or age > minutes:
                continue
            # 比我最后一次发言更早的，可能是回复别人（或就在回我上一句），不重复处理
            if age >= last_mine_age:
                continue
            replies.append({**item, "ageMinutes": age,
                            "parentText": str(raw[parent_idx].get("text") or "")[:120]})
    if not replies:
        runners.log(ctx, f"读了 {len(urls)} 个作品的楼中楼：这轮没有「别人回复我的评论」"
                         f"（子回复总数 {sum(1 for c in raw if int(c.get('depth') or 0) > 0)}，"
                         f"其中在我发言之后的新回复 0 条，认不出父评论的 {toomany} 条）")
        return {"scanned": 0, "acted": 0}

    scanned = 0
    acted = 0
    handled = 0
    cursor = 0
    reply_actions = list(actions.keys())
    for item in replies:
        if ctx.stop.is_set() or handled >= REPLY_ACTIONS_PER_PASS:
            break
        nick = str(item.get("nickname") or "").strip()
        content = str(item.get("text") or "").strip()
        video_url = str(item.get("videoUrl") or "").strip()
        comment_id = str(item.get("commentId") or "").strip()
        user_id = str(item.get("userId") or "").strip()
        if not content or not video_url:
            continue
        if content.startswith("群聊 ·") or content.startswith("置顶"):
            continue
        scanned += 1
        caption = str(item.get("caption") or "")[:60]
        event_key = hashlib.sha256(
            f"{item.get('videoId') or video_url}|{comment_id or nick + '|' + content}"
            .encode("utf-8")).hexdigest()[:16]

        def done(outcome: str, note: str = "", receipts=None, retryable: bool = False):
            if not retryable:
                store.seen_mark(db_path, ctx.task_id, [f"rp|{event_key}"])
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="reply",
                   conversation=caption, nickname=nick, content=content,
                   outcome=outcome, note=note, actions=receipts)

        if store.seen_has(db_path, ctx.task_id, f"rp|{event_key}"):
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="reply",
                   conversation=caption, nickname=nick, content=content,
                   outcome="dedup", note="这条回复已处理过")
            continue

        ok, reason = _judge(db_path, cfg, nick, content)
        if not ok:
            done("filtered", reason)
            runners.log(ctx, f"@{nick} 的回复跳过：{reason}")
            continue

        receipts: list[dict] = []
        text_reply = ""
        skipped_reason = ""
        for action in reply_actions:
            cap = int(caps.get(action) or 0)
            if cap and int(used.get(action) or 0) >= cap:
                skipped_reason = f"{_ACTION_LABEL.get(action, action)}已达本次任务上限（{cap}）"
                continue
            if action in ("reply-comment", "like-comment") and not comment_id:
                # 回复/点赞都要落到具体某一条评论上；壳侧按 commentId 定位，
                # 没有它就只能在评论区里瞎点，宁可不做。
                skipped_reason = "回复行没拿到评论 id，无法定位到那一条"
                continue
            if action == "send-dm" and not user_id:
                skipped_reason = "回复里没拿到用户主页标识，私信无法定位收件人"
                continue
            text = ""
            if action == "like-comment":
                text = content[:300]      # 壳侧用它核对点的确实是这一条
            elif action == "reply-comment":
                draft = _comment_text(db_path, cfg, account_id, nick, content, cursor)
                if not draft.get("ok"):
                    receipts.append({"action": action, "ok": False, "status": "failed",
                                     "code": draft.get("code"), "error": draft.get("reason")})
                    skipped_reason = str(draft.get("reason") or "")
                    continue
                text = str(draft["text"])[:300]
                text_reply = text
                cursor += 1
            elif action == "send-dm":
                text = _dm_text(db_path, cfg, account_id, nick, content)[:300]
                if not text:
                    skipped_reason = "私信话术为空"
                    continue
            receipt = _comment_step(db_path, ctx, cfg, account_id, action,
                                    video_url=video_url, comment_id=comment_id,
                                    comment_author=nick, recipient_id=user_id, text=text)
            receipts.append(receipt)
            if receipt["ok"]:
                used[action] = int(used.get(action) or 0) + 1
                handled += 1
                acted += 1
                runners.log(ctx, f"@{nick} 的回复：{_ACTION_LABEL.get(action, action)}成功"
                                 f"（{receipt['code']}）")
            elif str(receipt.get("code") or "") in _ALREADY_CODES:
                handled += 1
                runners.log(ctx, f"@{nick} 的回复：{_ACTION_LABEL.get(action, action)}无需动作："
                                 f"{receipt.get('code')}")
            else:
                runners.log(ctx, f"@{nick} 的回复：{_ACTION_LABEL.get(action, action)}未成功："
                                 f"{receipt.get('code')}", "warn")
            if ctx.stop.is_set():
                break

        if not receipts:
            done("matched", f"{reason}；{skipped_reason or '没有可执行的动作'}")
            continue
        success = [r for r in receipts if r["ok"]]
        if success:
            note = f"{reason}；" + "、".join(
                f"{_ACTION_LABEL.get(r['action'], r['action'])}"
                f"{('：' + r['text']) if r.get('text') and r['action'] in ('reply-comment', 'send-dm') else ''}"
                for r in success)
            done("acted", note, receipts)
            _notify(db_path, cfg, ctx, nick, content, text_reply)
        else:
            retryable = any(r.get("status") != "uncertain"
                            and r.get("code") in _RETRYABLE_CODES for r in receipts)
            already = [r for r in receipts if str(r.get("code") or "") in _ALREADY_CODES]
            if already and len(already) == len(receipts):
                done("skipped", f"{reason}；目标已是该状态，未重复执行", receipts)
                continue
            done("skipped" if retryable or any(
                r.get("status") == "uncertain" for r in receipts) else "failed",
                 f"{reason}；" + (skipped_reason or "动作未确认"), receipts,
                 retryable=retryable)
    return {"scanned": scanned, "acted": acted}


# --------------------------------------------------------------------------
# 互动通知：新增关注 / 作品点赞
# --------------------------------------------------------------------------
# 事件源 = 顶栏「通知」弹层。抖音 PC 网页版没有 /notifications 页面（实测 404），
# 但顶栏那个铃铛点开是真的：每行都带 kind（关注了你 / 赞了你的作品 / 赞了你的评论 …）
# 和 /user/<sec_uid> 锚点。2026-11-18 真机实测 26 条全量识别。
_NOTICE_SCENE_LABEL = {"follow": "新增关注", "like": "作品点赞"}
# 场景桶 → (监听开关, 关注动作开关, 私信动作开关)，字段名取自 ui/assets（不是猜的）
_NOTICE_SCENE_FIELDS = {
    "follow": ("watchFollows", "actionFollowBack", "actionFollowDm"),
    "like": ("watchLikes", "actionLikeFollow", "actionLikeDm"),
}
NOTICE_LIMIT = 40
NOTICE_ACTIONS_PER_PASS = 2
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _notice_actions(cfg: dict) -> dict[str, list[str]]:
    """本任务要执行的互动通知动作：{场景桶: [动作, ...]}。

    页面原话：「勾选『私信』后，收到符合条件的新增关注会向对方发送配置内容；
    是否回关由『自动回关』决定」—— 两个开关互相独立，回关不是私信的前置条件。
    """
    if not _enabled(cfg, "enableAutoActions", default=True):
        return {}
    out: dict[str, list[str]] = {}
    for bucket, (watch, follow_field, dm_field) in _NOTICE_SCENE_FIELDS.items():
        if not _enabled(cfg, watch, default=False):
            continue
        acts: list[str] = []
        if _enabled(cfg, follow_field, default=False):
            acts.append("follow-user")
        if _enabled(cfg, dm_field, default=False):
            acts.append("send-dm")
        if acts:
            out[bucket] = acts
    return out


def _dm_rule_follow_wanted(cfg: dict, watch_dm: bool) -> bool:
    """关键词规则勾了「关注用户」且私信回复开启 → 私信链路需要 follow-user 作用域。"""
    return (watch_dm and _reply_mode(cfg, "dm") == "rules"
            and _enabled(cfg, "actionMessageReply", default=False)
            and any(rule.get("followEnabled") for rule in _dm_rules(cfg)))


def _scope_actions(cfg: dict, *, comment_actions: dict, notice_actions: dict,
                   watch_dm: bool, limit: int,
                   reply_actions: dict | None = None) -> dict[str, int]:
    """写作用域里允许的动作次数。

    notice_actions 的键是场景桶（follow / like），值是动作名；作用域认的是
    follow-user / send-dm 这种动作名，桶名写进去会被壳的动作白名单整条过滤掉，
    结果是每个回关都返回 scope_not_verified（2026-11-18 真机踩过）。
    """
    names = (comment_actions.keys() | {a for acts in notice_actions.values() for a in acts}
             | set((reply_actions or {}).keys()))
    out = {key: 100 for key in names}
    if watch_dm and _enabled(cfg, "actionMessageReply", default=False):
        out["send-dm-chat"] = max(1, min(limit, 100))
    return out


# 「评论回复」场景（有人回复了我的评论）对应的开关。
# 实证自页面打包：`{key:"reply", subtitle:"有人回复了你的评论", watchKey:"watchReplies"}`
# 下挂四个动作 —— 自动点赞评论 / 自动回复 / 自动关注对方 / 自动私信对方。
_REPLY_ACTION_FIELDS = (
    ("like-comment", "actionReplyLike"),
    ("reply-comment", "actionReplyReply"),
    ("follow-user", "actionReplyFollow"),
    ("send-dm", "actionReplyDm"),
)
REPLY_LIMIT_PER_VIDEO = 60
REPLY_ACTIONS_PER_PASS = 2


def _reply_actions(cfg: dict) -> dict[str, int]:
    """「评论回复」链路要执行哪些动作。

    watchReplies 在页面上默认是勾选态（`!== false` 才生效），但 actionReplyReply
    必须显式开 —— 两者是 and 关系，所以不配动作时这里返回空，不会自己乱回。
    """
    if not _enabled(cfg, "enableAutoActions", default=True):
        return {}
    if not _enabled(cfg, "watchReplies", default=True):
        return {}
    return {key: 1 for key, field in _REPLY_ACTION_FIELDS if _enabled(cfg, field)}


def _notice_age_minutes(raw: str, now: float | None = None) -> int | None:
    """通知行的时间文案 → 「多少分钟前」；认不出来就是 None（不猜）。

    弹层里的时间只有这几种形态：18:18 / 昨天 / 星期一 / 11-13（偶尔带年份）。
    它们都不含「今天」这种字样，套评论那套解析会把整批通知判成「时间无法确认」。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    stamp = time.localtime(now if now is not None else time.time())
    moment = now if now is not None else time.time()
    if "刚刚" in text:
        return 0
    rel = re.search(r"(\d+)\s*(分钟|小时|天)前", text)
    if rel:
        return int(rel.group(1)) * {"分钟": 1, "小时": 60, "天": 1440}[rel.group(2)]
    if "前天" in text:
        return 2 * 1440
    if "昨天" in text:
        return 1440
    week = re.search(r"星期([一二三四五六日天])", text)
    if week:
        # 「星期一」表示本周一：今天是周三就是 2 天前，今天就是周一就是刚发生。
        return ((stamp.tm_wday - _WEEKDAY_INDEX[week.group(1)]) % 7) * 1440
    clock = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if clock:
        diff = (stamp.tm_hour * 60 + stamp.tm_min) - (int(clock.group(1)) * 60 + int(clock.group(2)))
        # 显示的时间比现在还晚 → 是昨天的（弹层不写日期）
        return diff if diff >= 0 else diff + 1440
    absolute = re.search(r"(?:(\d{4})[-/])?(\d{1,2})[-/](\d{1,2})", text)
    if not absolute:
        return None
    year = int(absolute.group(1)) if absolute.group(1) else stamp.tm_year
    try:
        target = time.mktime((year, int(absolute.group(2)), int(absolute.group(3)),
                              0, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return None
    if not absolute.group(1) and target > moment:
        target = time.mktime((year - 1, int(absolute.group(2)), int(absolute.group(3)),
                              0, 0, 0, 0, 0, -1))   # 没写年份且比现在晚 → 去年
    return max(0, int((moment - target) // 60))


_FOLLOW_SCENE_VALUES = {"mutual_follow", "follow_followed", "follow_unfollowed"}


def _notice_dm_texts(db_path, con: sqlite3.Connection, cfg: dict, *, task_id: str,
                     account_id: str, scene: str, followed, nickname: str,
                     content: str, user_key: str) -> tuple[list[str], str, str]:
    """通知链路的私信话术（关键词规则模式优先，通用话术兜底）。

    页面口径：「关注通知优先使用已确认互关的专用规则，其次是普通关注规则，最后是
    通用话术；评论、点赞等触发的主动私信仍用通用话术。通用话术在同一任务、同一
    账号、同一用户之间仅自动触发一次，跨通知共用，重启继承记录。」
    返回 (整组消息, 命中规则 id, 跳过原因)；texts 为空时 skip_reason 说明原因。
    """
    mode = _reply_mode(cfg, "dm")
    if mode == "rules":
        if scene in _FOLLOW_SCENE_VALUES:
            trigger = "mutual_follow" if scene == "mutual_follow" else "follow"
            rule, index = _match_dm_rule(cfg, trigger=trigger)
            if rule is not None:
                rid = str(rule.get("id") or f"rule-{index + 1}")
                try:
                    rounds_cap = int(cfg.get("actionUserMaxRounds") or 0)
                except (TypeError, ValueError):
                    rounds_cap = 0
                if rounds_cap and _rule_rounds(con, task_id, account_id, rid,
                                               user_key) >= rounds_cap:
                    return [], "", (f"规则「{rule.get('name') or rid}」对该用户已达回复"
                                    f"轮数上限（{rounds_cap} 轮）")
                return _rule_group_texts(cfg, rule, nickname=nickname,
                                         content=content), rid, ""
            if str(cfg.get("dmRuleFallback") or "skip") != "general":
                return [], "", "未命中关注规则（兜底：仅记录，不回复）"
        if _user_rounds(con, account_id, user_key) >= 1:
            return [], "", "通用话术对该用户已触发过（每位用户仅一次，跨通知共用）"
        note = "未命中关注规则，按兜底使用通用话术" if scene in _FOLLOW_SCENE_VALUES else ""
        return _general_group(cfg, nickname, content), "", note
    if mode == "template":
        # 通用话术：每位用户仅自动触发一次，跨通知共用、重启继承记录。
        if _user_rounds(con, account_id, user_key) >= 1:
            return [], "", "通用话术对该用户已自动触发过（每位用户仅一次，跨通知共用）"
        return _general_group(cfg, nickname, content), "", ""
    touched = _user_rounds(con, account_id, user_key) > 0
    text = _dm_text(db_path, cfg, account_id, nickname, content, scene=scene,
                    followed=followed, touched=touched)
    return ([text] if text else [], "", "")


def _notice_scene(item: dict) -> str:
    """通知行 → 专业模式规则里的「触发场景」。

    取值与前端一致：like / mutual_follow / follow_followed / follow_unfollowed。
    对方是「回关你」＝我先关注了他，他回关 → mutual_follow；
    否则看按钮状态：已关注/互相关注 → 我关注过他；回关/关注 → 我还没关注他。
    """
    kind = str(item.get("kind") or "")
    if kind == "like_work":
        return "like"
    if kind != "follow":
        return ""
    text = str(item.get("text") or "")
    # 通知原文是「XX 回关了你」或「XX 回关你」；两种都判为互关。
    if re.search(r"回关(了)?你", text) or "互相关注" in text:
        return "mutual_follow"
    state = str(item.get("followState") or "")
    return "follow_followed" if state in {"已关注", "互相关注"} else "follow_unfollowed"


def _notice_pass(db_path, ctx, cfg: dict, con: sqlite3.Connection, account_id: str,
                 lookback: float, notice_actions: dict, caps: dict, used: dict) -> dict:
    """一轮互动通知处理：新增关注 → 回关 / 私信；作品点赞 → 关注 / 私信。

    没有主页标识（userId）的通知行一律跳过：回关必须落到具体的人，
    按昵称猜人会在重名或改过名的账号上关注错对象。
    """
    from app.runtime import runners

    if not notice_actions:
        return {"scanned": 0, "acted": 0}
    res = browser.execute({"type": "read-interaction-notices", "accountId": account_id,
                           "limit": NOTICE_LIMIT, "scrolls": 0},
                          db_path=db_path, account_id=account_id, log=_log_cb(ctx),
                          stop_check=ctx.stop.is_set, timeout=240.0)
    if not res.get("ok"):
        runners.log(ctx, f"读取互动通知失败：{res.get('error') or '未知原因'}", "warn")
        return {"scanned": 0, "acted": 0}
    items = [i for i in (res.get("items") or []) if isinstance(i, dict)]
    if not items:
        runners.log(ctx, "通知弹层里没读到互动行（可能没有新通知）")
        return {"scanned": 0, "acted": 0}

    # 写命令的签名要求带一个「本任务范围内的视频地址」；通知本身没有视频，
    # 用自己最近一条作品当锚点（监控作用域里 videoUrls 是 "*"，任意目标都放行）。
    works, _self_name = _own_works(db_path, ctx, account_id)
    anchor = str((works[0] or {}).get("url") or "") if works else ""
    if not anchor:
        runners.log(ctx, "读不到自己的作品地址：互动通知的动作无法签名，本轮跳过", "warn")
        return {"scanned": 0, "acted": 0}

    minutes = int(max(1, min(60 * 24 * 30, lookback // 60)))   # 最多 30 天
    excluded = {n.casefold() for n in _names(cfg.get("excludeUsers"))}
    scanned = acted = handled = 0
    too_old = unknown = no_uid = 0
    for item in items:
        if ctx.stop.is_set() or handled >= NOTICE_ACTIONS_PER_PASS:
            break
        kind = str(item.get("kind") or "")
        bucket = "like" if kind == "like_work" else ("follow" if kind == "follow" else "")
        if bucket not in notice_actions:
            continue
        nick = str(item.get("nickname") or "").strip()
        if not nick:
            continue
        if nick.casefold() in excluded:
            continue
        uid = str(item.get("userId") or "").strip()
        if not uid:
            no_uid += 1
            continue
        age = _notice_age_minutes(str(item.get("timeText") or ""))
        if age is None:
            unknown += 1
            continue
        if age > minutes:
            too_old += 1
            continue
        scanned += 1
        raw_text = str(item.get("text") or "")[:160]
        label = _NOTICE_SCENE_LABEL.get(bucket, bucket)
        scene = _notice_scene(item)
        # 聚合行（「等 N 人赞了你的作品」）只有最后一个能辨识：只对他一个人动作，
        # 不能把「等 N 人」当成 N 个目标 —— 那 N-1 个人连主页标识都没有。
        aggregated = int(item.get("aggregatedFrom") or 0)
        event_key = hashlib.sha256(
            f"{bucket}|{uid}|{item.get('timeText') or ''}".encode("utf-8")).hexdigest()[:16]

        def done(outcome: str, note: str = "", receipts=None, *, mark: bool = True) -> None:
            # mark=False 用于「环境类失败」：这类失败不是这条通知的结论，下一轮还要再来。
            # 一律写 seen 的话，一次窗口没渲染好就等于把这条新关注/新点赞永久丢掉。
            if mark:
                store.seen_mark(db_path, ctx.task_id, [f"nt|{event_key}"])
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="notice",
                   conversation=label, nickname=nick, content=raw_text,
                   outcome=outcome, note=note, actions=receipts)

        if store.seen_has(db_path, ctx.task_id, f"nt|{event_key}"):
            _event(db_path, task_id=ctx.task_id, account_id=account_id, source="notice",
                   conversation=label, nickname=nick, content=raw_text,
                   outcome="dedup", note="该通知已处理过")
            continue

        acts = notice_actions[bucket]
        receipts: list[dict] = []
        skipped: list[str] = []
        followed = False
        if "follow-user" in acts:
            cap = int(caps.get("follow-user") or 0)
            if cap and int(used.get("follow-user") or 0) >= cap:
                skipped.append(f"关注已达本次任务上限（{cap}）")
            elif bucket == "follow" and item.get("canFollowBack") is False:
                skipped.append("对方已是「已关注/互相关注」，无需回关")
            else:
                receipt = _comment_step(db_path, ctx, cfg, account_id, "follow-user",
                                        video_url=anchor, comment_id="", comment_author=nick,
                                        recipient_id=uid, text="")
                receipts.append(receipt)
                if receipt["ok"]:
                    used["follow-user"] = int(used.get("follow-user") or 0) + 1
                    followed = True
                    handled += 1
                    acted += 1
                    runners.log(ctx, f"@{nick} "
                                     + ("回关成功" if bucket == "follow" else "关注成功"))
                else:
                    skipped.append(f"关注未成功（{receipt.get('code')}）")
        if "send-dm" in acts:
            dm_receipts: list[dict] = []
            if "follow-user" in acts and not followed:
                # 页面原话：「关注/回关成功后，等待 X-Y 秒再进入主页发私信」
                skipped.append("关注/回关未成功，未发私信")
            else:
                cap = int(caps.get("send-dm") or 0)
                if cap and int(used.get("send-dm") or 0) >= cap:
                    skipped.append(f"私信已达本次任务上限（{cap}）")
                else:
                    if followed:
                        low, high = _pair(cfg, "followDmDelayMin", "followDmDelayMax", (10.0, 30.0))
                        gap = random.uniform(low, high)
                        deadline = time.time() + gap
                        while not ctx.stop.is_set():
                            left = deadline - time.time()
                            if left <= 0:
                                break
                            time.sleep(min(0.5, left))
                    if ctx.stop.is_set():
                        break
                    dm_user_key = _user_key(nick)
                    # 跨账号私信去重（dmCrossAccountDedupe）：通知链路同样只让一个账号私信。
                    holder = _dm_cross_claim(con, cfg, ctx.task_id, dm_user_key, account_id)
                    if holder and holder != account_id:
                        skipped.append(f"跨账号私信去重：该用户已由账号 {holder} 私信")
                    else:
                        texts, dm_rule_id, why_skip = _notice_dm_texts(
                            db_path, con, cfg, task_id=ctx.task_id, account_id=account_id,
                            scene=scene, followed=True if bucket == "follow" else None,
                            nickname=nick, content=raw_text, user_key=dm_user_key)
                        if not texts:
                            skipped.append(why_skip or "私信话术为空（模板未配置、AI 未返回或专业模式无命中规则）")
                        else:
                            dm_ok = 0
                            lo_i, hi_i = _pair(cfg, "dmMessageIntervalMin",
                                               "dmMessageIntervalMax", (5.0, 10.0))
                            for position, text in enumerate(texts[:5]):
                                if position:
                                    gap = random.uniform(max(2.0, lo_i), max(3.0, hi_i))
                                    deadline = time.time() + gap
                                    while not ctx.stop.is_set():
                                        left = deadline - time.time()
                                        if left <= 0:
                                            break
                                        time.sleep(min(0.5, left))
                                if ctx.stop.is_set():
                                    break
                                receipt = _comment_step(db_path, ctx, cfg, account_id,
                                                        "send-dm", video_url=anchor,
                                                        comment_id="", comment_author=nick,
                                                        recipient_id=uid, text=text[:300],
                                                        delay=position == 0)
                                dm_receipts.append(receipt)
                                if receipt["ok"]:
                                    dm_ok += 1
                            if dm_ok:
                                used["send-dm"] = int(used.get("send-dm") or 0) + 1
                                handled += 1
                                acted += 1
                                _bump_user(con, account_id, dm_user_key)
                                if dm_rule_id:
                                    _bump_rule(con, ctx.task_id, account_id, dm_rule_id,
                                               dm_user_key)
                                # 必须立刻提交：下面的记账（seen_mark / _event）各开新连接，
                                # 这条连接持着写锁会让它们等满 30 秒然后报 database is locked。
                                con.commit()
                                runners.log(ctx, f"@{nick} 私信成功：{texts[0][:40]}"
                                                 f"（{dm_ok}/{len(texts[:5])} 条）")
                if dm_receipts:
                    receipts.extend(dm_receipts)

        if aggregated:
            skipped.append(f"聚合行（等 {aggregated} 人）：只处理可辨识的这 1 人")
        if not receipts:
            done("matched", "；".join(skipped) or "没有可执行的动作")
            continue
        success = [r for r in receipts if r["ok"]]
        if success:
            done("acted", "；".join(
                [f"{label}处理成功"] + [
                    f"{_ACTION_LABEL.get(r['action'], r['action'])}"
                    f"{('：' + str(r.get('text') or '')) if r.get('text') else ''}"
                    for r in success] + skipped), receipts)
        else:
            retryable = any(r.get("status") != "uncertain" and r.get("code") in _RETRYABLE_CODES
                            for r in receipts)
            already = [r for r in receipts if str(r.get("code") or "") in _ALREADY_CODES]
            if already and len(already) == len(receipts):
                done("skipped", "；".join(skipped + ["目标已是该状态，未重复执行"]), receipts)
            else:
                # 环境类失败不写 seen：下一轮还要重试这条通知，不能因为一次遮挡就丢掉。
                done("skipped" if retryable else "failed",
                     "；".join(skipped + ["动作未确认"]), receipts, mark=not retryable)

    runners.log(ctx, f"互动通知：读到 {len(items)} 条，窗口内 {scanned} 条，处理 {handled} 条"
                     f"（超窗 {too_old}、时间不明 {unknown}、无主页标识 {no_uid}）")
    return {"scanned": scanned, "acted": acted}
