"""互动通知（新增关注 / 作品点赞）契约测试。

真机背景（2026-11-18）：
  * 抖音 PC 网页版没有 /notifications 页面（实测 404），但顶栏「通知」弹层是真的。
  * 弹层每行都能拿到 /user/<sec_uid> 锚点（探针出口以 hasUid 布尔值暴露，不带 uid 本身），
    所以「回关 / 关注 / 私信」能落到具体的人，不需要按昵称猜。
  * 时间只有 18:18 / 昨天 / 星期一 / 11-13 四种形态，不含「今天」这类字样。

这里覆盖判定与编排：哪些通知该动、动作顺序、去重、跳过原因。
壳侧的 DOM 选择器由 douyin_agent.js 的真机探针兜住。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.execution import live_scope
from app.runtime import autoreply, browser, runners


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "ar.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    return path


def _ctx(tmp_path: Path, task_id: str = "ar-notice", account_id: str = "acc-1") -> runners.Ctx:
    return runners.Ctx(kind="warmup", db_path=_db(tmp_path), task_id=task_id,
                       name="测试互动通知", account_id=account_id, run_id="run-1",
                       trigger="test", engine="shell")


def _cfg(**kw) -> dict:
    base = {
        "taskMode": "auto-reply",
        "enableAutoActions": True,
        "watchFollows": True,
        "actionFollowBack": True,
        "actionFollowDm": True,
        "dmTemplateItems": [{"text": "谢谢关注，{nickname}"}],
        "initialLookbackValue": 24,
        "initialLookbackUnit": "hour",
        "actionDelayMin": 0,
        "actionDelayMax": 0,
        # _pair 把 0 当「没配置」并回落 10-30 秒（生产语义：0 秒间隔没意义），
        # 所以测试给一个极小但正的值，避免每个用例白等半分钟。
        "followDmDelayMin": 0.001,
        "followDmDelayMax": 0.001,
    }
    base.update(kw)
    return base


def _item(**kw) -> dict:
    base = {
        "kind": "follow", "nickname": "小明", "userId": "MS4wLjABAAAAtest-uid-01",
        "timeText": "18:18", "text": "小明 关注了你 18:18 回关",
        "followState": "回关", "canFollowBack": True, "aggregatedFrom": 0,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 开关映射
def test_notice_actions_maps_follow_switches():
    got = autoreply._notice_actions(_cfg())
    assert got == {"follow": ["follow-user", "send-dm"]}


def test_notice_actions_maps_like_switches():
    cfg = _cfg(watchFollows=False, watchLikes=True,
               actionLikeFollow=True, actionLikeDm=False)
    assert autoreply._notice_actions(cfg) == {"like": ["follow-user"]}


def test_notice_actions_requires_watch_switch():
    """没勾「监听」就没有动作 —— 动作开关单独打开不算数（页面也是这个口径）。"""
    cfg = _cfg(watchFollows=False, watchLikes=False)
    assert autoreply._notice_actions(cfg) == {}


def test_notice_actions_respects_master_switch():
    cfg = _cfg(enableAutoActions=False)
    assert autoreply._notice_actions(cfg) == {}


def test_notice_actions_empty_when_no_action_selected():
    cfg = _cfg(actionFollowBack=False, actionFollowDm=False)
    assert autoreply._notice_actions(cfg) == {}


# ---------------------------------------------------------------- 作用域动作名
def test_scope_actions_uses_action_names_not_scene_buckets():
    """回归：作用域里必须是 follow-user / send-dm，不能写成场景桶名 follow / like。

    真机踩过：写进桶名后壳的动作白名单把它整条过滤掉，actions 变空，
    每个回关都返回 scope_not_verified，日志只说「不在验收范围」。
    """
    acts = autoreply._scope_actions(
        _cfg(), comment_actions={},
        notice_actions={"follow": ["follow-user", "send-dm"], "like": ["follow-user"]},
        watch_dm=False, limit=50)
    assert acts == {"follow-user": 100, "send-dm": 100}


def test_scope_actions_merges_comment_and_notice():
    """评论场景和通知场景共用同一份动作白名单，不会互相覆盖。"""
    acts = autoreply._scope_actions(
        _cfg(actionMessageReply=True), comment_actions={"like-comment": 1, "send-dm": 1},
        notice_actions={"follow": ["follow-user"]},
        watch_dm=True, limit=50)
    assert set(acts) == {"like-comment", "send-dm", "follow-user", "send-dm-chat"}


# ---------------------------------------------------------------- 时间解析
NOW = time.mktime((2026, 11, 18, 12, 0, 0, 0, 0, -1))   # 星期三 12:00


def test_notice_age_clock_means_today():
    assert autoreply._notice_age_minutes("11:00", NOW) == 60
    assert autoreply._notice_age_minutes("18:18", NOW) > 0     # 比现在晚 → 昨天的


def test_notice_age_relative_words():
    assert autoreply._notice_age_minutes("刚刚", NOW) == 0
    assert autoreply._notice_age_minutes("3小时前", NOW) == 180
    assert autoreply._notice_age_minutes("昨天", NOW) == 1440
    assert autoreply._notice_age_minutes("前天", NOW) == 2880


def test_notice_age_weekday_is_this_week():
    assert autoreply._notice_age_minutes("星期三", NOW) == 0        # 就是今天
    assert autoreply._notice_age_minutes("星期二", NOW) == 1440
    assert autoreply._notice_age_minutes("星期一", NOW) == 2 * 1440


def test_notice_age_absolute_date():
    # 11-13 00:00 距 11-18 12:00 = 5 天 12 小时
    assert autoreply._notice_age_minutes("11-13", NOW) == 5 * 1440 + 720
    assert autoreply._notice_age_minutes("2026-11-18", NOW) == 720


def test_notice_age_unknown_is_none():
    """认不出来就是 None：宁可这一轮不动，也不把旧通知当新通知。"""
    assert autoreply._notice_age_minutes("", NOW) is None
    assert autoreply._notice_age_minutes("很久以前", NOW) is None


# ---------------------------------------------------------------- 场景判定
def test_notice_scene_follow_states():
    assert autoreply._notice_scene(_item(followState="回关")) == "follow_unfollowed"
    assert autoreply._notice_scene(_item(followState="关注")) == "follow_unfollowed"
    assert autoreply._notice_scene(_item(followState="已关注")) == "follow_followed"


def test_notice_scene_mutual_follow_wins():
    """对方是「回关你」＝我先关注他、他回关 → 互关规则优先。"""
    item = _item(text="小明 回关了你 18:18", followState="互相关注")
    assert autoreply._notice_scene(item) == "mutual_follow"


def test_notice_scene_like():
    assert autoreply._notice_scene(_item(kind="like_work", followState="")) == "like"


# ---------------------------------------------------------------- 编排
def _fake_notice_reader(items, calls):
    def fake_execute(cmd, **kwargs):
        calls.append(cmd["type"])
        if cmd["type"] == "read-interaction-notices":
            return {"ok": True, "items": items}
        return {"ok": True}
    return fake_execute


def _run_pass(tmp_path, monkeypatch, items, cfg=None, notice_actions=None):
    ctx = _ctx(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(browser, "execute", _fake_notice_reader(items, calls))
    monkeypatch.setattr(autoreply, "_own_works",
                        lambda *a, **k: ([{"url": "https://www.douyin.com/video/7123456789012345678"}],
                                         "我自己"))
    monkeypatch.setattr(autoreply, "_action_delay", lambda *a, **k: None)
    steps: list[dict] = []

    def fake_step(db_path, ctx_, cfg_, account_id, action, **kw):
        steps.append({"action": action, **kw})
        return {"action": action, "ok": True, "status": "sent_ok", "code": "sent_ok",
                "text": kw.get("text", "")}

    monkeypatch.setattr(autoreply, "_comment_step", fake_step)
    con = autoreply._connect(ctx.db_path)
    effective = cfg or _cfg()
    try:
        got = autoreply._notice_pass(ctx.db_path, ctx, effective, con, "acc-1",
                                     autoreply._lookback_seconds(effective),
                                     notice_actions or autoreply._notice_actions(effective),
                                     {"follow-user": 10, "send-dm": 10},
                                     {"follow-user": 0, "send-dm": 0})
    finally:
        con.close()
    return got, steps, calls, ctx


def test_follow_back_then_dm(tmp_path, monkeypatch):
    """回关在前、私信在后，且都落在同一个主页标识上。"""
    got, steps, calls, _ctx_ = _run_pass(tmp_path, monkeypatch, [_item()])
    assert got == {"scanned": 1, "acted": 2}
    assert [s["action"] for s in steps] == ["follow-user", "send-dm"]
    assert steps[0]["recipient_id"] == steps[1]["recipient_id"] == "MS4wLjABAAAAtest-uid-01"
    assert steps[1]["text"].startswith("谢谢关注")
    assert calls.count("read-interaction-notices") == 1


def test_notice_without_uid_is_skipped(tmp_path, monkeypatch):
    """没有主页标识就不回关：按昵称猜人会关注错人。"""
    got, steps, _calls, _ctx_ = _run_pass(tmp_path, monkeypatch, [_item(userId="")])
    assert got["acted"] == 0
    assert steps == []


def test_notice_outside_lookback_is_skipped(tmp_path, monkeypatch):
    cfg = _cfg(initialLookbackValue=1, initialLookbackUnit="hour")
    got, steps, _calls, _ctx_ = _run_pass(tmp_path, monkeypatch,
                                          [_item(timeText="昨天")], cfg=cfg)
    assert got["scanned"] == 0
    assert steps == []


def test_already_followed_is_skipped(tmp_path, monkeypatch):
    """按钮已是「已关注」：不再回关，也不因为「关注未成功」连带跳过私信。"""
    cfg = _cfg(actionFollowDm=False)
    got, steps, _calls, _ctx_ = _run_pass(
        tmp_path, monkeypatch, [_item(followState="已关注", canFollowBack=False)], cfg=cfg)
    assert got["acted"] == 0
    assert steps == []


def test_same_notice_is_deduped(tmp_path, monkeypatch):
    """同一条通知只处理一次：取关再关注是另一条通知（timeText 不同），不会被误吞。"""
    ctx = _ctx(tmp_path)
    _run_pass(tmp_path, monkeypatch, [_item()])
    got, steps, _calls, _ctx2 = _run_pass(tmp_path, monkeypatch, [_item()])
    assert got["scanned"] == 1
    assert steps == [], "第二次应命中去重，不再回关"
    rows = sqlite3.connect(ctx.db_path).execute(
        "SELECT outcome FROM autoreply_events WHERE outcome='dedup'").fetchall()
    assert len(rows) == 1


def _run_pass_with_step(tmp_path, monkeypatch, items, step_result, cfg=None):
    """跑一轮通知处理，动作执行结果由参数给定（用来构造失败场景）。"""
    ctx = _ctx(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(browser, "execute", _fake_notice_reader(items, calls))
    monkeypatch.setattr(autoreply, "_own_works",
                        lambda *a, **k: ([{"url": "https://www.douyin.com/video/7123456789012345678"}],
                                         "我自己"))
    monkeypatch.setattr(autoreply, "_action_delay", lambda *a, **k: None)
    attempts: list[str] = []

    def fake_step(db_path, ctx_, cfg_, account_id, action, **kw):
        attempts.append(action)
        return dict(step_result, action=action)

    monkeypatch.setattr(autoreply, "_comment_step", fake_step)
    effective = cfg or _cfg(actionFollowDm=False)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._notice_pass(ctx.db_path, ctx, effective, con, "acc-1",
                                     autoreply._lookback_seconds(effective),
                                     autoreply._notice_actions(effective),
                                     {"follow-user": 10}, {"follow-user": 0})
    finally:
        con.close()
    return got, attempts, ctx, effective


def test_env_failure_does_not_burn_notice(tmp_path, monkeypatch):
    """环境类失败（按钮在页面上但点不中）不写 seen：下一轮还要重试这条通知。

    真机背景：同一目标第一次跑 hasPoint=false（后台窗口被判定遮挡），重开壳后
    同一段代码一次点中。写 seen 就等于把这条新关注/新点赞永久丢掉。
    """
    fail = {"ok": False, "status": "skipped", "code": "follow_control_occluded", "text": ""}
    got, attempts, ctx, cfg = _run_pass_with_step(tmp_path, monkeypatch, [_item()], fail)
    assert got["scanned"] == 1 and got["acted"] == 0
    assert attempts == ["follow-user"]

    con = autoreply._connect(ctx.db_path)
    try:
        got2 = autoreply._notice_pass(ctx.db_path, ctx, cfg, con, "acc-1",
                                      autoreply._lookback_seconds(cfg),
                                      autoreply._notice_actions(cfg),
                                      {"follow-user": 10}, {"follow-user": 0})
    finally:
        con.close()
    assert got2["scanned"] == 1, "上一轮是环境失败，这一轮应该重新处理同一条通知"
    assert attempts == ["follow-user", "follow-user"]


def test_permanent_failure_burns_notice(tmp_path, monkeypatch):
    """确定性失败（找不到关注按钮）要写 seen：不能每轮无限重试同一条通知。"""
    fail = {"ok": False, "status": "skipped", "code": "follow_control_not_verified", "text": ""}
    got, attempts, ctx, cfg = _run_pass_with_step(tmp_path, monkeypatch, [_item()], fail)
    assert got["scanned"] == 1 and got["acted"] == 0
    con = autoreply._connect(ctx.db_path)
    try:
        got2 = autoreply._notice_pass(ctx.db_path, ctx, cfg, con, "acc-1",
                                      autoreply._lookback_seconds(cfg),
                                      autoreply._notice_actions(cfg),
                                      {"follow-user": 10}, {"follow-user": 0})
    finally:
        con.close()
    assert got2["scanned"] == 1
    assert attempts == ["follow-user"], "第二次应命中去重，不再重复点"


def test_like_work_follows_and_dms(tmp_path, monkeypatch):
    cfg = _cfg(watchFollows=False, watchLikes=True,
               actionLikeFollow=True, actionLikeDm=True)
    item = _item(kind="like_work", followState="",
                 text="小明 赞了你的作品 18:18", canFollowBack=False)
    got, steps, _calls, _ctx_ = _run_pass(tmp_path, monkeypatch, [item], cfg=cfg)
    assert got["acted"] == 2
    assert [s["action"] for s in steps] == ["follow-user", "send-dm"]


def test_aggregated_like_row_only_touches_identified_user(tmp_path, monkeypatch):
    """「等 N 人赞了你的作品」只处理可辨识的这 1 人，不能当 N 个目标重复动作。"""
    cfg = _cfg(watchFollows=False, watchLikes=True,
               actionLikeFollow=True, actionLikeDm=False)
    item = _item(kind="like_work", nickname="黦yue 等3人", followState="",
                 text="黦yue 等3人赞了你的作品 18:18", aggregatedFrom=3)
    got, steps, _calls, ctx = _run_pass(tmp_path, monkeypatch, [item], cfg=cfg)
    assert got["scanned"] == 1
    assert len(steps) == 1
    note = sqlite3.connect(ctx.db_path).execute(
        "SELECT note FROM autoreply_events WHERE outcome='acted'").fetchone()[0]
    assert "聚合行" in note


def test_dm_cap_exhausted_blocks_second_notice(tmp_path, monkeypatch):
    """私信额度用完：回关照做（自己的额度），私信跳过。"""
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(browser, "execute",
                        _fake_notice_reader([_item(nickname="甲", userId="MS4wLjABAAAA000001"),
                                             _item(nickname="乙", userId="MS4wLjABAAAA000002",
                                                   timeText="18:17")], []))
    monkeypatch.setattr(autoreply, "_own_works",
                        lambda *a, **k: ([{"url": "https://www.douyin.com/video/7123456789012345678"}],
                                         "我自己"))
    monkeypatch.setattr(autoreply, "_action_delay", lambda *a, **k: None)
    steps = []
    monkeypatch.setattr(autoreply, "_comment_step",
                        lambda *a, **k: (steps.append(a[4]) or
                                         {"action": a[4], "ok": True, "status": "sent_ok",
                                          "code": "sent_ok", "text": k.get("text", "")}))
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._notice_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", 86400,
                                     {"follow": ["follow-user", "send-dm"]},
                                     {"follow-user": 10, "send-dm": 1},
                                     {"follow-user": 0, "send-dm": 1})
    finally:
        con.close()
    assert got["acted"] == 2          # 两条各回关一次
    assert steps.count("follow-user") == 2
    assert steps.count("send-dm") == 0


def test_follow_failure_blocks_dm(tmp_path, monkeypatch):
    """页面原话「关注/回关成功后…再发私信」：回关没成功就不发私信。"""
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(browser, "execute", _fake_notice_reader([_item()], []))
    monkeypatch.setattr(autoreply, "_own_works",
                        lambda *a, **k: ([{"url": "https://www.douyin.com/video/7123456789012345678"}],
                                         "我自己"))
    monkeypatch.setattr(autoreply, "_action_delay", lambda *a, **k: None)
    steps = []
    monkeypatch.setattr(autoreply, "_comment_step",
                        lambda *a, **k: (steps.append(a[4]) or
                                         {"action": a[4], "ok": False, "status": "skipped",
                                          "code": "follow_control_not_verified", "text": ""}))
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._notice_pass(ctx.db_path, ctx, _cfg(), con, "acc-1", 86400,
                                     {"follow": ["follow-user", "send-dm"]},
                                     {"follow-user": 10, "send-dm": 10},
                                     {"follow-user": 0, "send-dm": 0})
    finally:
        con.close()
    assert steps == ["follow-user"]
    assert got["acted"] == 0


def test_monitor_scope_allows_follow_user_signature(tmp_path, monkeypatch):
    """回关命令能过签名：监控作用域的 videoUrls 是 "*"，用自己作品当锚点即可。"""
    monkeypatch.setenv("LEADS_MONITOR_SCOPE_DIR", str(tmp_path / "scopes"))
    live_scope.write_monitor_scope(task_id="ar-notice", account_id="acc-1",
                                   actions={"follow-user": 5, "send-dm": 5},
                                   hours=1, nickname="我自己")
    cmd = live_scope.signed_command(
        "follow-user", account_id="acc-1", task_id="ar-notice",
        video_url="https://www.douyin.com/video/7123456789012345678",
        comment_author="小明", recipient_id="MS4wLjABAAAAtest-uid-01")
    assert cmd["type"] == "follow-user"
    assert cmd["recipientId"] == "MS4wLjABAAAAtest-uid-01"
    assert cmd["acceptanceSignature"]


# ---------------------------------------------------------------- #9 作品点赞
def test_like_dm_sends_general_template_without_follow(tmp_path, monkeypatch):
    """#9 作品点赞：只勾「私信」不勾「关注」，点赞通知走通用话术私信（每人仅一次）。"""
    cfg = _cfg(watchFollows=False, watchLikes=True,
               actionLikeFollow=False, actionLikeDm=True)
    got, steps, calls, _ctx_ = _run_pass(
        tmp_path, monkeypatch,
        [_item(kind="like_work", nickname="小丽", timeText="昨天",
               text="小丽 赞了你的作品 昨天", followState="", canFollowBack=False)],
        cfg=cfg, notice_actions=autoreply._notice_actions(cfg))
    assert got == {"scanned": 1, "acted": 1}
    assert [s["action"] for s in steps] == ["send-dm"]
    assert steps[0]["recipient_id"] == "MS4wLjABAAAAtest-uid-01"
    assert steps[0]["text"].startswith("谢谢关注")


def test_like_dm_fires_only_once_per_user(tmp_path, monkeypatch):
    """同一个人多次点赞：通用话术每位用户仅自动触发一次。"""
    cfg = _cfg(watchFollows=False, watchLikes=True,
               actionLikeFollow=False, actionLikeDm=True)
    item = _item(kind="like_work", nickname="小丽", timeText="昨天",
                 text="小丽 赞了你的作品 昨天", followState="", canFollowBack=False)
    _run_pass(tmp_path, monkeypatch, [item], cfg=cfg,
              notice_actions=autoreply._notice_actions(cfg))
    got, steps, _calls, _ctx2 = _run_pass(tmp_path, monkeypatch,
                                          [_item(kind="like_work", nickname="小丽",
                                                 timeText="18:18",
                                                 text="小丽 赞了你的作品 18:18",
                                                 followState="", canFollowBack=False)],
                                          cfg=cfg,
                                          notice_actions=autoreply._notice_actions(cfg))
    assert got["acted"] == 0
    assert steps == []


def test_like_follow_and_dm_are_independent(tmp_path, monkeypatch):
    """#9 开关组：只勾「关注」不勾「私信」→ 只回关不发私信。"""
    cfg = _cfg(watchFollows=False, watchLikes=True,
               actionLikeFollow=True, actionLikeDm=False)
    got, steps, _calls, _ctx_ = _run_pass(
        tmp_path, monkeypatch,
        [_item(kind="like_work", nickname="小丽", timeText="昨天",
               text="小丽 赞了你的作品 昨天", followState="", canFollowBack=False)],
        cfg=cfg, notice_actions=autoreply._notice_actions(cfg))
    assert got == {"scanned": 1, "acted": 1}
    assert [s["action"] for s in steps] == ["follow-user"]
