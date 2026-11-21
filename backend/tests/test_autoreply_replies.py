"""评论回复（watchReplies）契约测试。

真机背景（2026-11-18）：
  * 抖音顶栏通知弹层里只有「赞了你的作品 / 关注了你 / 赞了你的评论」，
    47 条历史里没有一条「回复了你的评论」—— 所以这条链路**不能**靠翻通知做。
  * 事件源在自己作品的评论区楼中楼。真机实测（造了一条样本）：
    我回复了「1A.」的一级评论，另一个账号回复我那条，结果两者的 depth 都是 1、
    parentIdx 都指向同一级评论 —— **二级评论在 DOM 上是扁平一层，不按被回复对象嵌套**。
  * 平台评论列表接口能给出 reply_id，但要签名参数，页面内直接 fetch 会超时（14s 拿不到）。
    所以判据改成：同一楼中楼里、在我最后一次发言之后出现的别人的回复。
  * isAuthor 来自「作者」徽章，只在作品作者本人视角稳定（换第二个账号读同一页时全 false），
    因此昵称兜底必须保留。

这里覆盖判定与编排；壳侧的选择器由 douyin_agent.js 的真机探针兜住。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.runtime import autoreply, browser, runners

SELF = "我自己"
VIDEO = "https://www.douyin.com/video/7123456789012345678"


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "ar.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()
    return path


def _ctx(tmp_path: Path, task_id: str = "ar-reply", account_id: str = "acc-1") -> runners.Ctx:
    return runners.Ctx(kind="warmup", db_path=_db(tmp_path), task_id=task_id,
                       name="测试评论回复", account_id=account_id, run_id="run-1",
                       trigger="test", engine="shell")


def _cfg(**kw) -> dict:
    base = {
        "taskMode": "auto-reply",
        "enableAutoActions": True,
        "watchReplies": True,
        "actionReplyLike": False,
        "actionReplyReply": True,
        "actionReplyFollow": False,
        "actionReplyDm": False,
        "replyTemplateItems": [{"text": "谢谢回复，{nickname}"}],
        "initialLookbackValue": 24,
        "initialLookbackUnit": "hour",
        "actionDelayMin": 0,
        "actionDelayMax": 0,
    }
    base.update(kw)
    return base


def _top(**kw) -> dict:
    """一级评论（楼中楼的那一层）：谁发的不重要，重要的是它下面有我说过话。"""
    base = {
        "nickname": "路人甲", "text": "为什么不用原声", "commentId": "7704666960282698522",
        "userId": "MS4wLjABAAAAtop-uid", "isAuthor": False, "depth": 0,
        "parentIdx": -1, "parentNick": "", "ageMinutes": 10080,
        "videoUrl": VIDEO, "videoId": "7123456789012345678", "caption": "作品标题",
    }
    base.update(kw)
    return base


def _mine(**kw) -> dict:
    """我在楼中楼的发言（真机上昵称带「作者」徽章，已由壳剥掉并存成 isAuthor）。"""
    base = {
        "nickname": SELF, "text": "因为不好听", "commentId": "7123456789012345600",
        "userId": "MS4wLjABAAAAself-uid", "isAuthor": True, "depth": 1,
        "parentIdx": 0, "parentNick": "路人甲", "ageMinutes": 10080,
        "videoUrl": VIDEO, "videoId": "7123456789012345678", "caption": "作品标题",
    }
    base.update(kw)
    return base


def _other(**kw) -> dict:
    """别人在我发言之后的新回复 —— 这才是「有人回复了我的评论」。"""
    base = {
        "nickname": "路人乙", "text": "同意，学到了", "commentId": "7123456789012345601",
        "userId": "MS4wLjABAAAAother-uid", "isAuthor": False, "depth": 1,
        "parentIdx": 0, "parentNick": "路人甲", "ageMinutes": 5,
        "videoUrl": VIDEO, "videoId": "7123456789012345678", "caption": "作品标题",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 开关映射
def test_reply_actions_maps_all_four_switches():
    cfg = _cfg(actionReplyLike=True, actionReplyReply=True,
               actionReplyFollow=True, actionReplyDm=True)
    got = autoreply._reply_actions(cfg)
    assert got == {"like-comment": 1, "reply-comment": 1,
                   "follow-user": 1, "send-dm": 1}


def test_reply_actions_requires_watch_replies():
    """没勾「评论回复」监听就没有动作（页面上 watchReplies 与动作是 and 关系）。"""
    assert autoreply._reply_actions(_cfg(watchReplies=False)) == {}


def test_reply_actions_respects_master_switch():
    assert autoreply._reply_actions(_cfg(enableAutoActions=False)) == {}


def test_reply_actions_empty_when_nothing_selected():
    assert autoreply._reply_actions(_cfg(actionReplyReply=False)) == {}


def test_scope_actions_includes_reply_names():
    """作用域必须把回复链路的动作名摊进去，否则签名直接被判不在范围。"""
    acts = autoreply._scope_actions(
        _cfg(), comment_actions={}, notice_actions={}, watch_dm=False, limit=50,
        reply_actions={"like-comment": 1, "send-dm": 1})
    assert acts == {"like-comment": 100, "send-dm": 100}


# ---------------------------------------------------------------- 编排
def _run(tmp_path, monkeypatch, comments, cfg=None, caps=None, task_id="ar-reply"):
    ctx = _ctx(tmp_path, task_id=task_id)
    effective = cfg or _cfg()
    actions = autoreply._reply_actions(effective)
    monkeypatch.setattr(autoreply, "_own_works",
                        lambda *a, **k: ([{"url": VIDEO}], SELF))
    monkeypatch.setattr(autoreply, "_action_delay", lambda *a, **k: None)
    monkeypatch.setattr(autoreply, "_judge", lambda *a, **k: (True, "命中"))
    monkeypatch.setattr(autoreply, "_comment_text",
                        lambda *a, **k: {"ok": True, "text": "谢谢回复"})
    monkeypatch.setattr(autoreply, "_dm_text", lambda *a, **k: "私信内容")
    monkeypatch.setattr(autoreply, "_notify", lambda *a, **k: None)

    calls: list[str] = []

    def fake_execute(cmd, **kwargs):
        calls.append(cmd["type"])
        if cmd["type"] == "read-comments":
            return {"ok": True, "comments": comments}
        return {"ok": True, "items": []}

    monkeypatch.setattr(browser, "execute", fake_execute)
    steps: list[dict] = []

    def fake_step(db_path, ctx_, cfg_, account_id, action, **kw):
        steps.append({"action": action, **kw})
        return {"action": action, "ok": True, "status": "sent_ok", "code": "sent_ok",
                "text": kw.get("text", "")}

    monkeypatch.setattr(autoreply, "_comment_step", fake_step)
    con = autoreply._connect(ctx.db_path)
    try:
        got = autoreply._replies_pass(
            ctx.db_path, ctx, effective, con, "acc-1",
            autoreply._lookback_seconds(effective), actions,
            caps or {k: 3 for k in actions}, {k: 0 for k in actions}, SELF)
    finally:
        con.close()
    return got, steps, calls


def test_reply_after_my_comment_is_acted(tmp_path, monkeypatch):
    got, steps, calls = _run(tmp_path, monkeypatch, [_top(), _mine(), _other()])
    assert got["acted"] == 1
    assert [s["action"] for s in steps] == ["reply-comment"]
    assert steps[0]["comment_id"] == "7123456789012345601"
    assert steps[0]["comment_author"] == "路人乙"
    assert "read-comments" in calls


def test_reply_in_thread_without_my_comment_is_ignored(tmp_path, monkeypatch):
    """这层楼里我没说过话 → 别人在这里发言跟我没关系。"""
    got, steps, _ = _run(tmp_path, monkeypatch, [_top(), _other()])
    assert got["scanned"] == 0 and got["acted"] == 0
    assert steps == []


def test_reply_older_than_my_last_words_is_ignored(tmp_path, monkeypatch):
    """比我最后一次发言更早的回复，可能是在回别人，不重复处理。"""
    got, steps, _ = _run(tmp_path, monkeypatch,
                         [_top(), _mine(ageMinutes=5), _other(ageMinutes=30)])
    assert got["scanned"] == 0 and steps == []


def test_my_own_later_reply_is_not_processed(tmp_path, monkeypatch):
    """我自己又补了一句 → 不是「别人回复我」，回了就是自问自答。"""
    got, steps, _ = _run(tmp_path, monkeypatch,
                         [_top(), _mine(ageMinutes=60), _mine(commentId="7123456789012345699",
                                                              text="补充一句", ageMinutes=2)])
    assert got["scanned"] == 0 and steps == []


def test_top_level_comment_is_not_a_reply(tmp_path, monkeypatch):
    """一级评论本来就归作品评论链路，这里不能重复处理。"""
    got, steps, _ = _run(tmp_path, monkeypatch, [_top(), _other(depth=0)])
    assert got["scanned"] == 0 and steps == []


def test_reply_without_comment_id_skips_reply_action(tmp_path, monkeypatch):
    """没拿到评论 id 就没法定位到那一条，回复/点赞都不能做。"""
    cfg = _cfg(actionReplyReply=True, actionReplyFollow=True)
    got, steps, _ = _run(tmp_path, monkeypatch,
                         [_top(), _mine(), _other(commentId="")], cfg=cfg)
    assert [s["action"] for s in steps] == ["follow-user"]
    assert got["acted"] == 1


def test_reply_without_uid_skips_dm(tmp_path, monkeypatch):
    cfg = _cfg(actionReplyReply=False, actionReplyDm=True)
    got, steps, _ = _run(tmp_path, monkeypatch,
                         [_top(), _mine(), _other(userId="")], cfg=cfg)
    assert steps == []
    assert got["acted"] == 0


def test_reply_dedup_second_pass(tmp_path, monkeypatch):
    """同一条回复第二轮不能再回一次。"""
    got, steps, _ = _run(tmp_path, monkeypatch, [_top(), _mine(), _other()], task_id="ar-d")
    assert got["acted"] == 1
    got2, steps2, _ = _run(tmp_path, monkeypatch, [_top(), _mine(), _other()], task_id="ar-d")
    assert got2["acted"] == 0 and steps2 == []


def test_reply_outside_lookback_is_skipped(tmp_path, monkeypatch):
    got, steps, _ = _run(tmp_path, monkeypatch,
                         [_top(), _mine(), _other(ageMinutes=99999)])
    assert got["scanned"] == 0 and steps == []


def test_unresolvable_parent_index_is_skipped(tmp_path, monkeypatch):
    """认不出父评论（下标越界）就不动：宁可漏，也不能对错人。"""
    got, steps, _ = _run(tmp_path, monkeypatch,
                         [_top(), _mine(), _other(parentIdx=9)])
    assert got["scanned"] == 0 and steps == []


def test_my_reply_detected_by_nickname_when_badge_missing(tmp_path, monkeypatch):
    """壳侧没读出作者徽章时，昵称对上自己也算我说过话（兜底，不是主判据）。"""
    got, steps, _ = _run(tmp_path, monkeypatch,
                         [_top(), _mine(isAuthor=False), _other()])
    assert got["acted"] == 1 and steps[0]["action"] == "reply-comment"


def test_reply_stops_when_cap_reached(tmp_path, monkeypatch):
    """本轮额度用完就不再动下一个人（上限按动作算，不按事件源算）。"""
    got, steps, _ = _run(tmp_path, monkeypatch,
                         [_top(), _mine(),
                          _other(commentId="7123456789012345602", nickname="路人丙")],
                         caps={"reply-comment": 1})
    assert got["acted"] == 1
    assert len(steps) == 1
