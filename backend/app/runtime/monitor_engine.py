"""监控任务动作引擎。

监控页面有 122 个配置字段，但历史实现只消费了其中不到 20 个（监控视频、主播、
间隔、评论数、意向开关、三类过滤）。本模块把「配置 → 可执行意图」这段补齐：

- 目标解析：多账号、失效链接、目标类型（视频 / 主播 / 直播 / 群成员）
- 意向：仅评论研判、优先命中关键词、账号绑定智能体
- 动作计划：点赞 / 回复 / 关注 / 私信 / 视频主评 / 首作互动 / 分享，
  带百分比抽样、单账号每日配额、动作间隔
- 文案：模板模式与 AI 模式、随机后缀、多条私信
- 通知：飞书 / 钉钉 Webhook

只做「算什么、发什么」的纯逻辑与本地落库；真正发出去的动作由
`app.execution.live_leadgen.perform` 走签名 + 写安全门禁，本模块不绕过门禁。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# 页面动作名 ↔ 配置里的开关名
ACTION_TYPES = ("like-comment", "reply-comment", "follow-user", "send-dm",
                "post-comment", "share-profile")


def _b(cfg: dict, key: str, default: bool = False) -> bool:
    value = cfg.get(key, default)
    return value is True or value in (1, "1", "true", "True")


def _i(cfg: dict, key: str, default: int, lo: int = 0, hi: int = 10 ** 6) -> int:
    try:
        value = int(float(cfg.get(key, default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _s(cfg: dict, key: str, default: str = "") -> str:
    value = cfg.get(key)
    return str(value).strip() if value is not None else default


def text_list(cfg: dict, key: str) -> list[str]:
    return _list(cfg, key)


def _list(cfg: dict, key: str) -> list[str]:
    value = cfg.get(key)
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        import re

        return [p for p in re.split(r"[\n,，;；\s]+", value) if p]
    return []


# --------------------------------------------------------------------------
# 目标与账号
# --------------------------------------------------------------------------
def accounts(cfg: dict) -> list[str]:
    """监控账号（可多选）。兼容 [id] 与 [{accountId,name}] 两种存法。"""
    raw = cfg.get("selectedAccounts") or cfg.get("accounts") or []
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        value = (str(item.get("accountId") or item.get("id") or "").strip()
                 if isinstance(item, dict) else str(item).strip())
        if value and value not in out:
            out.append(value)
    single = _s(cfg, "accountId")
    if single and single not in out:
        out.insert(0, single)
    return out


def target_types(cfg: dict) -> set[str]:
    raw = cfg.get("monitorTargetTypes")
    if not isinstance(raw, list) or not raw:
        return {"video"}
    return {str(v) for v in raw}


def _url_rows(cfg: dict, key: str) -> list[dict]:
    """videoUrls / authorUrls 页面存 [{url,name,invalid}]，也兼容纯字符串。"""
    raw = cfg.get(key)
    rows: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            if url:
                rows.append({"url": url, "name": str(item.get("name") or ""),
                             "invalid": bool(item.get("invalid"))})
        elif str(item).strip():
            rows.append({"url": str(item).strip(), "name": "", "invalid": False})
    return rows


def valid_urls(cfg: dict, key: str) -> list[str]:
    """跳过页面已标记失效的链接 —— 运行时不跳会让每轮都去撞同一个死链。"""
    return [r["url"] for r in _url_rows(cfg, key) if not r["invalid"]]


def invalid_count(cfg: dict, key: str) -> int:
    return sum(1 for r in _url_rows(cfg, key) if r["invalid"])


# --------------------------------------------------------------------------
# 直播目标
# --------------------------------------------------------------------------
# 用户是直接粘「分享文案」的（微信里长按复制的那一段）：
#   「4.t r@e.oq :5pm 05/31 【AI学长小马的直播间】 点击打开👉https://v.douyin.com/xxx/ 或长按复制…」
# 以前运行时不解析 liveUrls，这类目标直接被判「没有目标」退出。
_URL_RE = re.compile(r"https?://[^\s，。；、）)\]】\"'<>]+")
_live_resolve_cache: dict[str, str] = {}


def extract_share_url(value: object) -> str:
    """从分享文案里取出第一条链接；已经是纯链接就原样返回。"""
    text = str(value or "").strip()
    if not text:
        return ""
    match = _URL_RE.search(text)
    if not match:
        return ""
    return match.group(0).rstrip(".,;，。；、）)】]")


def _live_row_url(item: object) -> str:
    if isinstance(item, dict):
        return extract_share_url(item.get("url") or item.get("link") or item.get("value") or "")
    return extract_share_url(item)


def live_targets(cfg: dict) -> list[dict]:
    """直播目标（规范化后的链接）。兼容 liveUrls 存纯链接 / {url} / 整段分享文案。"""
    raw = cfg.get("liveUrls")
    rows: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        url = _live_row_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        rows.append({"url": url, "name": name})
    return rows


def group_targets(cfg: dict) -> list[str]:
    """监控群成员（group_members）：手动群名 groupNames + 勾选群 groupSelections 合并去重。"""
    names: list[str] = [v for v in text_list(cfg, "groupNames") if v]
    sel = cfg.get("groupSelections")
    for item in sel if isinstance(sel, list) else []:
        if isinstance(item, dict):
            v = str(item.get("name") or item.get("groupName") or item.get("title") or "").strip()
        else:
            v = str(item).strip()
        if v:
            names.append(v)
    return list(dict.fromkeys(names))


def author_live_targets(cfg: dict) -> list[dict]:
    """监控主播直播（author_live）：liveAuthorUrls 存主播主页链接，兼容 {url} / 分享文案。

    运行时把主页链接下发给壳，壳负责「检测是否开播 → 解析直播间 → 采集」；
    这里只做规范化与去重，解析失败的短链原样保留。
    """
    raw = cfg.get("liveAuthorUrls")
    rows: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        url = _live_row_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
        rows.append({"url": url, "name": name})
    return rows


def is_live_url(url: str) -> bool:
    text = str(url or "").lower()
    return any(host in text for host in
               ("live.douyin.com", "v.douyin.com", "webcast.amemv.com"))


def resolve_live_url(url: str, timeout: float = 8.0) -> str:
    """把分享短链解析成可打开的直播间地址。

    v.douyin.com 会 302 到 webcast.amemv.com/.../reflow/<roomId>?...&user_id=<uid>，
    再重定向到 live.douyin.com/<webRid>。壳里直接打开 reflow 地址即可拿到完整房间
    上下文（只打开 live.douyin.com/<webRid> 会渲染成空壳）。
    解析失败就原样返回，交给壳自己跟随跳转。
    """
    target = str(url or "").strip()
    if not target or not target.startswith("http"):
        return target
    if target in _live_resolve_cache:
        return _live_resolve_cache[target]
    resolved = target
    if "v.douyin.com" in target or "webcast.amemv.com" in target:
        req = urllib.request.Request(target, method="GET", headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"),
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 用户配置的链接
                final = resp.geturl()
            if final and final.startswith("http"):
                resolved = final
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            resolved = target
    _live_resolve_cache[target] = resolved
    return resolved


_video_resolve_cache: dict[str, str] = {}


def resolve_video_url(url: str, timeout: float = 8.0) -> str:
    """把视频分享短链解析成可读评论的真实视频地址。

    视频分享文案里的 v.douyin.com/xxx 会 302 到 www.douyin.com/video/<id>
    （图文是 /note/<id>）。与 resolve_live_url 同款策略：跟随重定向拿最终地址，
    归一成 /video/<id>；解析失败原样返回，交给壳自己打开。
    注意与直播短链区分：解析结果若落在 live.douyin.com 说明用户贴错了栏目，
    保留解析结果让日志能看出问题，不做静默改道。
    """
    target = str(url or "").strip()
    if not target or not target.startswith("http"):
        return target
    if "/video/" in target or "/note/" in target or "live.douyin.com" in target:
        return target                       # 已是真实地址
    if target in _video_resolve_cache:
        return _video_resolve_cache[target]
    resolved = target
    if "v.douyin.com" in target:
        req = urllib.request.Request(target, method="GET", headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0 Safari/537.36"),
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 用户配置的链接
                final = resp.geturl()
            if final and final.startswith("http"):
                match = re.search(r"douyin\.com/(video|note)/(\d+)", final)
                resolved = (f"https://www.douyin.com/{match.group(1)}/{match.group(2)}"
                            if match else final)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            resolved = target
    _video_resolve_cache[target] = resolved
    return resolved


def room_label(url: str) -> str:
    """日志里显示的房间标识（不打印整段分享文案）。"""
    text = str(url or "")
    match = re.search(r"live\.douyin\.com/(\d+)", text)
    if match:
        return f"直播间 {match.group(1)}"
    match = re.search(r"reflow/(\d+)", text)
    if match:
        return f"直播间 {match.group(1)}"
    return text[:60]



# --------------------------------------------------------------------------
# 意向
# --------------------------------------------------------------------------
def priority_hit(text: str, cfg: dict) -> str:
    """优先命中关键词：包含即判高意向，不调用 AI。返回命中的词。"""
    for word in _list(cfg, "aiPriorityKeywords")[:50]:
        if word and word.casefold() in text.casefold():
            return word
    return ""


def persona_payload(db_path, cfg: dict, account_id: str) -> dict:
    """账号绑定智能体优先；未绑定回落到任务级通用人设。"""
    base = {"aiRole": _s(cfg, "aiRole") or "商业需求分析助手",
            "aiGoal": _s(cfg, "aiGoal") or "判断评论是否表达真实需求",
            "aiPrompt": _s(cfg, "aiPrompt")[:2000]}
    bound = cfg.get("accountPersonas")
    persona_id = ""
    if isinstance(bound, dict):
        persona_id = str(bound.get(account_id) or "").strip()
    if not persona_id:
        return base
    try:
        from app import ai_personas

        data = ai_personas.load_task_config(db_path) or {}
        items = data.get("personas") if isinstance(data, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and str(item.get("id")) == persona_id:
                return {
                    "aiRole": str(item.get("role") or item.get("name") or base["aiRole"])[:120],
                    "aiGoal": str(item.get("goal") or base["aiGoal"])[:500],
                    "aiPrompt": str(item.get("prompt") or item.get("description")
                                    or base["aiPrompt"])[:2000],
                    "aiStyle": str(item.get("style") or "")[:200],
                }
    except Exception:  # noqa: BLE001 人设读不到就用通用人设，不中断监控
        return base
    return base


# --------------------------------------------------------------------------
# 文案
# --------------------------------------------------------------------------
RANDOM_SUFFIX = ("", " 哈", "～", " 呢", " 呀", " 吧")


def _fill(template: str, *, nickname: str, text: str) -> str:
    return (template.replace("{nickname}", nickname or "")
                    .replace("{用户昵称}", nickname or "")
                    .replace("{评论}", text).replace("{原评论}", text)).strip()


def _suffix(text: str, enabled: bool) -> str:
    if not enabled or not text:
        return text
    return text + random.choice(RANDOM_SUFFIX)


def reply_text(db_path, cfg: dict, account_id: str, comment: dict) -> dict:
    """回复文案：template 模式直接套模板；ai 模式按人设生成。"""
    nickname = str(comment.get("nickname") or "")
    text = str(comment.get("text") or "")
    mode = _s(cfg, "replyMode", "ai") or "ai"
    template = _s(cfg, "replyTemplate")
    if mode == "template":
        if not template:
            return {"ok": False, "code": "empty_template",
                    "reason": "回复模板为空：已配置为固定模板模式但没有填写模板"}
        body = _suffix(_fill(template, nickname=nickname, text=text),
                       _b(cfg, "commentUseRandomSuffix"))
        return {"ok": True, "text": body} if body else {
            "ok": False, "code": "empty_template", "reason": "回复模板渲染后为空"}
    from app.execution.live_leadgen import GenerationError, generate

    try:
        result = generate(db_path, persona_payload(db_path, cfg, account_id), {
            "nickname": nickname, "comment": text,
            "title": str(comment.get("caption") or comment.get("videoTitle") or "")[:300]})
    except GenerationError as exc:
        return {"ok": False, "code": "ai_failed", "reason": str(exc)}
    if not result.get("ok"):
        return result
    body = _suffix(str(result.get("text") or ""), _b(cfg, "commentUseRandomSuffix"))
    return {"ok": True, "text": body, "reason": result.get("reason", "")}


def dm_texts(cfg: dict) -> list[str]:
    """私信正文：首条模板 + 追加模板，条数由 dmMessageCount 控制。"""
    first = _s(cfg, "dmTemplate")
    extras = [str(x).strip() for x in (cfg.get("dmExtraTemplates") or [])
              if isinstance(x, (str, int)) and str(x).strip()]
    count = _i(cfg, "dmMessageCount", 1, 1, 10)
    texts = [t for t in [first, *extras] if t][:count]
    return texts


def dm_draft(db_path, cfg: dict, account_id: str, comment: dict) -> dict:
    """私信正文：有模板用模板；dmMode=ai 且没模板时按人设生成一条。"""
    texts = dm_texts(cfg)
    if texts:
        return {"ok": True, "texts": texts}
    if (_s(cfg, "dmMode", "ai") or "ai") != "ai":
        return {"ok": False, "code": "empty_template",
                "reason": "私信模板为空：已配置为固定模板模式但没有填写模板"}
    from app.execution.live_leadgen import GenerationError, generate

    try:
        result = generate(db_path, persona_payload(db_path, cfg, account_id), {
            "nickname": str(comment.get("nickname") or ""),
            "comment": str(comment.get("text") or ""),
            "title": str(comment.get("caption") or "")[:300]})
    except GenerationError as exc:
        return {"ok": False, "code": "ai_failed", "reason": str(exc)}
    if not result.get("ok"):
        return result
    body = _suffix(str(result.get("text") or ""), _b(cfg, "commentUseRandomSuffix"))
    if not body:
        return {"ok": False, "code": "ai_failed", "reason": "AI 私信文案为空"}
    return {"ok": True, "texts": [body]}


def video_comment_text(db_path, cfg: dict, account_id: str, video: dict) -> dict:
    mode = _s(cfg, "videoCommentMode", "ai") or "ai"
    content = _s(cfg, "videoCommentContent")
    if mode == "template":
        if not content:
            return {"ok": False, "code": "empty_template",
                    "reason": "视频主评模板为空"}
        return {"ok": True, "text": content}
    from app.execution.live_leadgen import GenerationError, generate

    try:
        result = generate(db_path, persona_payload(db_path, cfg, account_id), {
            "nickname": "", "comment": "", "title": str(video.get("title") or "")[:300]})
    except GenerationError as exc:
        return {"ok": False, "code": "ai_failed", "reason": str(exc)}
    return result


# --------------------------------------------------------------------------
# 配额（单账号每日限制 + 自动重置）
# --------------------------------------------------------------------------
def _logical_day(cfg: dict, now: float | None = None) -> str:
    """按「自动重置时间」切日：00:00 重置时就是自然日。"""
    stamp = time.localtime(now if now is not None else time.time())
    # 只有开启「自动重置动作数量」时才按设定时间切日；关闭时就是自然日 00:00。
    # 休眠跨过重置点的情况天然成立：恢复后按当前时间算，落在新的一天。
    reset = (_s(cfg, "actionCountAutoResetTime", "00:00")
             if _b(cfg, "actionCountAutoResetEnabled") else "00:00") or "00:00"
    try:
        hour, minute = (int(x) for x in reset.split(":")[:2])
    except (ValueError, TypeError):
        hour, minute = 0, 0
    if (stamp.tm_hour, stamp.tm_min) < (hour, minute):
        stamp = time.localtime(time.mktime(stamp) - 86400)
    return time.strftime("%Y-%m-%d", stamp)


def _quota_table(db_path) -> None:
    with sqlite3.connect(str(db_path), timeout=10) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS monitor_action_quota (
            account_id TEXT NOT NULL, action TEXT NOT NULL, day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0, updated_ms INTEGER,
            PRIMARY KEY (account_id, action, day))""")


def quota_used(db_path, account_id: str, action: str, cfg: dict) -> int:
    _quota_table(db_path)
    with sqlite3.connect(str(db_path), timeout=10) as con:
        row = con.execute("SELECT count FROM monitor_action_quota "
                          "WHERE account_id=? AND action=? AND day=?",
                          (account_id, action, _logical_day(cfg))).fetchone()
    return int(row[0]) if row else 0


def quota_take(db_path, account_id: str, action: str, limit: int, cfg: dict) -> bool:
    """占一个额度并记账。limit<=0 表示不限量，但仍然记录已用次数。"""
    _quota_table(db_path)
    day = _logical_day(cfg)
    with sqlite3.connect(str(db_path), timeout=10) as con:
        row = con.execute("SELECT count FROM monitor_action_quota "
                          "WHERE account_id=? AND action=? AND day=?",
                          (account_id, action, day)).fetchone()
        used = int(row[0]) if row else 0
        if limit > 0 and used >= limit:
            return False
        con.execute("INSERT INTO monitor_action_quota(account_id,action,day,count,updated_ms) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(account_id,action,day) "
                    "DO UPDATE SET count=count+1, updated_ms=excluded.updated_ms",
                    (account_id, action, day, 1, int(time.time() * 1000)))
    return True


def limit_of(cfg: dict, kind: str) -> int:
    """kind: like / reply / follow / dm。返回 0 表示不限。"""
    if not _b(cfg, f"auto{kind.title()}LimitEnabled"):
        return 0
    return _i(cfg, f"auto{kind.title()}LimitMin", 10, 1, 10000)


def limit_range(cfg: dict, kind: str) -> tuple[int, int]:
    """单账号动作上限的区间（页面是「开关 + 数字区间」）。0 表示不限。"""
    if not _b(cfg, f"auto{kind.title()}LimitEnabled"):
        return 0, 0
    low = _i(cfg, f"auto{kind.title()}LimitMin", 10, 1, 10000)
    high = _i(cfg, f"auto{kind.title()}LimitMax", 20, 1, 10000)
    return min(low, high), max(low, high)


def draw_limit(cfg: dict, kind: str) -> int:
    """在区间内抽一个本次任务的上限。

    页面原话：「每次启动重新抽取上限」+「每到设定时间清零并重新抽取」，
    所以每次调用都重新抽 —— 调用方负责在启动时/到点时各抽一次并存住。
    """
    low, high = limit_range(cfg, kind)
    if not high:
        return 0
    return random.randint(low, high) if high > low else low


# --------------------------------------------------------------------------
# 动作计划
# --------------------------------------------------------------------------
@dataclass
class Intent:
    type: str
    text: str = ""
    recipient_id: str = ""
    extras: dict = field(default_factory=dict)
    reason: str = ""


def _roll(percent: int) -> bool:
    return percent >= 100 or random.randint(1, 100) <= max(0, percent)


def plan(cfg: dict, account_id: str, comment: dict, db_path=None,
         limits: dict | None = None) -> list[Intent]:
    """按配置给一条命中评论排动作。返回的意图仍需过写安全门禁。"""
    limits = limits or {}
    out: list[Intent] = []
    if _b(cfg, "enableAutoLike"):
        out.append(Intent("like-comment", reason="自动点赞评论"))
    if _b(cfg, "enableAutoReply"):
        out.append(Intent("reply-comment", reason="自动回复评论"))
    if _b(cfg, "enableAutoFollow"):
        out.append(Intent("follow-user", reason="自动关注"))
    if _b(cfg, "enableAutoDM"):
        out.append(Intent("send-dm", reason="自动私信"))
    # 配额已用完的动作本轮不排，避免每条评论都撞一次门禁
    for intent in list(out):
        key = {"like-comment": "like", "reply-comment": "reply",
               "follow-user": "follow", "send-dm": "dm"}[intent.type]
        limit = limits.get(key, 0)
        if limit and db_path is not None and quota_used(db_path, account_id, intent.type, cfg) >= limit:
            out.remove(intent)
    return out


def scopes_for(cfg: dict) -> dict[str, int]:
    """把开关翻译成作用域里允许的动作次数（0 表示不开启）。"""
    out: dict[str, int] = {}
    if _b(cfg, "enableAutoLike"):
        out["like-comment"] = max(1, limit_of(cfg, "like") or 100)
    if _b(cfg, "enableAutoReply"):
        out["reply-comment"] = max(1, limit_of(cfg, "reply") or 100)
    if _b(cfg, "enableAutoFollow"):
        out["follow-user"] = max(1, limit_of(cfg, "follow") or 100)
    if _b(cfg, "enableAutoDM"):
        out["send-dm"] = max(1, limit_of(cfg, "dm") or 100)
    if _b(cfg, "enableVideoComment"):
        out["post-comment"] = 100
    # 首作互动：对评论作者主页首个公开作品点赞/收藏/评论（自动动作）。
    # like/favorite 只受总开关控制；首作评论复用 post-comment 动作名，
    # 与「自动发表视频主评」取较高次数。
    if _b(cfg, "commentOnProfileFirstWork"):
        out["like-video"] = 100
        out["favorite-video"] = 100
        if _b(cfg, "enableProfileFirstComment"):
            out["post-comment"] = max(int(out.get("post-comment") or 0), 100)
    if _b(cfg, "enableProfileShare"):
        out["share-profile"] = 100
    # 直播间的「房间级」动作：对房间本身操作，不针对某条评论。
    # 点赞按次给（每轮每房间最多 likeTimesPerRoom 次）；关注主播与参与抽奖各自一次。
    if _b(cfg, "enableLiveLike"):
        out["like-live"] = max(1, min(100, _i(cfg, "liveLikeTimesPerRoom", 10, 1, 100)))
    if _b(cfg, "enableLiveFollowAnchor"):
        out["follow-live"] = 1
    if _b(cfg, "enableLiveLottery"):
        out["lottery-live"] = 1
    return out


def action_delay(cfg: dict) -> float:
    lo = _i(cfg, "actionDelaySecondsMin", 300, 0, 86400)
    hi = _i(cfg, "actionDelaySecondsMax", 600, lo, 86400)
    return float(random.randint(lo, hi) if hi > lo else lo)


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------
def webhook_payload(cfg: dict, lead: dict, task_name: str) -> dict:
    text = (f"【{task_name}】命中监控线索\n"
            f"用户：{lead.get('nickname', '')}\n"
            f"评论：{lead.get('comment', '')}\n"
            f"视频：{lead.get('videoUrl', '')}\n"
            f"意向：{lead.get('score', '')} 分")
    kind = _s(cfg, "webhookType", "feishu") or "feishu"
    if kind == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    return {"msg_type": "text", "content": {"text": text}}


def dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    """钉钉加签算法（官方文档）：timestamp + "\\n" + secret 做 HMAC-SHA256 再 base64。"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _dingtalk_url(cfg: dict) -> str:
    """钉钉 Webhook 地址：配置了加签密钥时附 timestamp 与 sign（#37 钉钉加签）。

    以前这里只留了个空注释、密钥从未被消费 —— 钉钉机器人开了加签后所有推送
    都会被拒（sign 参数缺失）。
    """
    url = webhook_url(cfg)
    secret = _s(cfg, "webhookSecretDingtalk")
    if not secret:
        return url
    timestamp = int(time.time() * 1000)
    sign = dingtalk_sign(secret, timestamp)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}timestamp={timestamp}&sign={urllib.parse.quote(sign)}"


def webhook_url(cfg: dict) -> str:
    kind = _s(cfg, "webhookType", "feishu") or "feishu"
    return _s(cfg, "webhookUrlDingtalk" if kind == "dingtalk" else "webhookUrlFeishu")


def notify(cfg: dict, lead: dict, task_name: str, timeout: float = 6.0) -> dict:
    """命中推送。失败只返回原因，不影响本轮采集。"""
    if not _b(cfg, "enableWebhook"):
        return {"ok": False, "code": "disabled"}
    kind = _s(cfg, "webhookType", "feishu") or "feishu"
    url = _dingtalk_url(cfg) if kind == "dingtalk" else webhook_url(cfg)
    if not url.startswith("http"):
        return {"ok": False, "code": "no_url", "error": "未填写 Webhook 地址"}
    body = json.dumps(webhook_payload(cfg, lead, task_name), ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 本地配置的地址
            raw = resp.read(2048).decode("utf-8", "ignore")
        ok = resp.status == 200
        if ok:
            # 飞书/钉钉都用 code/errcode 判业务成功
            try:
                data = json.loads(raw) if raw else {}
                code = data.get("code", data.get("errcode", 0))
                ok = code in (0, None)
            except ValueError:
                pass
        return {"ok": ok, "code": "sent" if ok else "rejected"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "code": "transport", "error": type(exc).__name__}
