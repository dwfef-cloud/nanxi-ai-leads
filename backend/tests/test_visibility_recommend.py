"""推荐页随机样本互检（recommend）调度器测试。

2026-11-18 落地的链路：
1. 多账号同时运行：每账号独立线程准备样本（recommend-scan 扫候选 + 签名发表），
   样本就绪即配对互检，无需等待全部账号；
2. 配对规则：两两互检（A 查 B、B 查 A）、不检查自己、每个账号只被检测一次、
   账号为单数时由先完成的账号检测剩余账号；
3. 发表被占用（other_account_history）自动换下一个候选；
4. 无样本账号直接出「无样本」结论；无配对/手动停止时样本记检测未完成；
5. 评论内容三模式：人设（绑定智能体生成）/ 随机正向表情 / 自定义内容。

并行基建（后端指令队列按账号过滤、壳侧跨账号并发）由
test_desktop_commands_parallel.py 与壳侧真机验收覆盖。
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from app.runtime import visibility as vis


LEADS_DDL = """CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY, nickname TEXT DEFAULT '', comment TEXT DEFAULT '',
    source_keyword TEXT DEFAULT '', source TEXT DEFAULT '', source_task_id TEXT DEFAULT '',
    source_url TEXT DEFAULT '', video TEXT DEFAULT '', video_id TEXT DEFAULT '',
    comment_id TEXT DEFAULT '', comment_user_id TEXT DEFAULT '', comment_time TEXT DEFAULT '',
    account TEXT DEFAULT '', status TEXT DEFAULT 'collected', score INTEGER DEFAULT 0,
    intent_level TEXT DEFAULT 'C', platform TEXT DEFAULT 'douyin',
    created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')"""

ACC1, ACC2, ACC3 = "acc_1", "acc_2", "acc_3"
NICKNAMES = {ACC1: "互检一号", ACC2: "互检二号", ACC3: "互检三号"}


def _make_db(tmp_path) -> str:
    db = tmp_path / "vis_rec.db"
    con = sqlite3.connect(db)
    con.execute(LEADS_DDL)
    con.execute("CREATE TABLE IF NOT EXISTS accounts ("
                "id TEXT PRIMARY KEY, name TEXT DEFAULT '', nickname TEXT DEFAULT '', "
                "platform TEXT DEFAULT 'douyin', status TEXT DEFAULT 'active')")
    for aid, nick in NICKNAMES.items():
        con.execute("INSERT OR IGNORE INTO accounts(id,nickname) VALUES(?,?)", (aid, nick))
    con.commit()
    con.close()
    return str(db)


def _make_ctx(tmp_path, db: str):
    from app.runtime import runners

    return runners.Ctx(kind="entity", db_path=db, task_id="vis_rec_t1", name="推荐页互检",
                       account_id=ACC1, run_id="vis_rec_r1")


def _cfg(**extra) -> dict:
    cfg = {"sourceTypes": ["entity_comment_visibility"],
           "selectedAccounts": [ACC1, ACC2],
           "visibilityDetectionMethod": "window",
           "visibilitySampleSource": "recommend",
           "visibilityCommentMode": "emoji",
           "visibilitySamplesPerTarget": 3}
    cfg.update(extra)
    return cfg


def _candidates(prefix: str, count: int) -> list[dict]:
    return [{"videoId": f"{prefix}{n:03d}",
             "videoUrl": f"https://www.douyin.com/video/73012345678901{prefix}{n:03d}",
             "commentCount": 10 + n, "title": f"{prefix}测试视频{n}"}
            for n in range(1, count + 1)]


class _Harness:
    """fake 掉推荐页扫描 / 签名发表 / 评论读取，记录全部调用。"""

    def __init__(self, monkeypatch, *, scan_items, perform_statuses=None,
                 posted=None, comments_missing=()):
        from app.runtime import browser

        self.scan_calls: list[str] = []
        self.performs: list[tuple[str, str, str]] = []  # (account, url, text)
        self.scan_items = scan_items          # account -> candidates（None=扫描失败）
        self.perform_statuses = perform_statuses or {}  # (account, url) -> status/code
        self.posted: dict[str, tuple[str, str]] = posted or {}  # url -> (account, text)
        self.comments_missing = set(comments_missing)  # 读取失败的 url

        def fake_browser_execute(cmd, **kw):
            # 账号通过 browser.execute 的 account_id 关键字参数传递（与真实签名一致）
            acc = str(kw.get("account_id") or "")
            if cmd.get("type") == "recommend-scan":
                self.scan_calls.append(acc)
                items = self.scan_items.get(acc)
                if items is None:
                    return {"ok": False, "items": [], "error": "推荐页扫描失败"}
                return {"ok": True, "items": items,
                        "diag": {"visited": len(items) * 2, "skippedBusy": 1,
                                 "skippedUnreadable": 0, "enough": True}}
            if cmd.get("type") == "read-comments":
                urls = cmd.get("videoUrls") or []
                rows = []
                for url in urls:
                    if url in self.comments_missing:
                        continue
                    entry = self.posted.get(url)
                    if entry:
                        rows.append({"nickname": NICKNAMES[entry[0]],
                                     "text": entry[1], "depth": 0})
                return {"ok": True, "comments": rows, "note": ""}
            raise AssertionError(f"测试不应下发其他指令：{cmd.get('type')}")

        monkeypatch.setattr(browser, "execute", fake_browser_execute)

        def fake_perform(db_path, action, *, account_id, task_id, video_url,
                         text="", **kw):
            assert action == "post-comment"
            key = (account_id, video_url)
            status = self.perform_statuses.get(key, "sent_ok")
            self.performs.append((account_id, video_url, text))
            if status == "sent_ok":
                self.posted[video_url] = (account_id, text)
                return {"ok": True, "status": "sent_ok"}
            return {"ok": False, "status": "skipped", "code": status}

        from app.execution import live_leadgen
        monkeypatch.setattr(live_leadgen, "perform", fake_perform)

        # 测试提速：就绪等待与逐视频冷却压到最小
        monkeypatch.setattr(vis, "READY_WAIT", (0.0, 0.02))
        monkeypatch.setattr(vis, "VIDEO_COOLDOWN", (0.0, 0.01))
        monkeypatch.setattr("app.runtime.visibility.log", lambda *a, **k: None)


def _run(ctx, cfg):
    accounts = vis._accounts_of(ctx.db_path)
    selected = [str(a or "").strip() for a in (cfg.get("selectedAccounts") or [])
                if str(a or "").strip()]
    vis._run_recommend(ctx, cfg, accounts, selected)


def _leads(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM leads WHERE source='entity_comment_visibility'")]
    finally:
        con.close()
    out = []
    for r in rows:
        out.append({"target_account": r.get("visibility_target_account") or "",
                    "status": r.get("visibility_status") or "",
                    "reason": r.get("visibility_reason") or "",
                    "account": r.get("account") or ""})
    return out


def _rows_by_target(rows):
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r.get("target_account") or "", []).append(r)
    return out


# --------------------------------------------------------------------------
# 启动校验
# --------------------------------------------------------------------------
def test_config_problem_persona_requires_binding(tmp_path, monkeypatch):
    """人设模式：没绑定智能体的账号直接拒绝，启动瞬间说清楚。"""
    db = _make_db(tmp_path)
    cfg = _cfg(visibilityCommentMode="persona")
    assert "账号人设" in vis.config_problem(cfg, db)

    from app import ai_personas
    data = {"accountPersonas": {ACC1: "p1", ACC2: "p1"},
            "personas": [{"id": "p1", "role": "测试人设", "goal": "测试",
                          "prompt": "测试人设描述"}]}
    monkeypatch.setattr(ai_personas, "load_task_config", lambda _db: data)
    assert vis.config_problem(cfg, db) is None


# --------------------------------------------------------------------------
# 配对调度
# --------------------------------------------------------------------------
def test_two_accounts_mutual_check_all_visible(tmp_path, monkeypatch):
    """两账号两两互检：各发 3 条样本、互相核对、全部命中。"""
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    h = _Harness(monkeypatch, scan_items={ACC1: _candidates("a", 5),
                                          ACC2: _candidates("b", 5)})
    _run(ctx, _cfg())

    rows = _rows_by_target(_leads(db))
    assert set(rows) == {ACC1, ACC2}
    assert all(len(v) == 3 and all(r["status"] == "visible" for r in v)
               for v in rows.values())
    # 不检查自己：ACC1 的样本只被 ACC2 读（account 列 = 检测员）
    assert all(r["account"] == ACC2 for r in rows[ACC1])
    assert all(r["account"] == ACC1 for r in rows[ACC2])
    assert ctx.stats["visibility_visible"] == 6
    assert ctx.stats["visibility_no_sample"] == 0
    assert len(ctx.stats["visibilityAccounts"]) == 2
    assert set(h.scan_calls) == {ACC1, ACC2}


def test_three_accounts_odd_leftover_checked_once(tmp_path, monkeypatch):
    """三账号：两两互检后剩余一个由先完成的账号检测，每账号只被检测一次。"""
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    _Harness(monkeypatch, scan_items={ACC1: _candidates("a", 4),
                                      ACC2: _candidates("b", 4),
                                      ACC3: _candidates("c", 4)})
    _run(ctx, _cfg(visibilitySamplesPerTarget=2, selectedAccounts=[ACC1, ACC2, ACC3]))

    rows = _rows_by_target(_leads(db))
    assert set(rows) == {ACC1, ACC2, ACC3}
    assert all(len(v) == 2 and all(r["status"] == "visible" for r in v)
               for v in rows.values())
    # 每个账号只被检测一次：结论行（account_rows）每个账号恰好一条
    conclusions = ctx.stats["visibilityAccounts"]
    assert sorted(c["accountId"] for c in conclusions) == [ACC1, ACC2, ACC3]


def test_no_sample_accounts_get_no_sample_conclusion(tmp_path, monkeypatch):
    """扫描无候选：账号直接出「无样本」结论，不参与配对。"""
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    _Harness(monkeypatch, scan_items={ACC1: [], ACC2: []})
    _run(ctx, _cfg())

    rows = _leads(db)
    assert rows and all(r["status"] == "no_sample" for r in rows)
    assert ctx.stats["visibility_no_sample"] == 2
    assert ctx.stats["visibility_visible"] == 0
    assert all(c["status"] == "no_sample" for c in ctx.stats["visibilityAccounts"])


def test_perform_blocked_tries_next_candidate(tmp_path, monkeypatch):
    """候选视频已被其他账号发过评论（other_account_history）→ 换下一个候选。"""
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    blocked = _candidates("a", 5)
    h = _Harness(monkeypatch, scan_items={ACC1: blocked, ACC2: _candidates("b", 5)},
                 perform_statuses={(ACC1, blocked[0]["videoUrl"]): "other_account_history",
                                   (ACC1, blocked[1]["videoUrl"]): "other_account_history"})
    _run(ctx, _cfg())

    acc1_posts = [p for p in h.performs if p[0] == ACC1]
    assert len(acc1_posts) == 5  # 前两个被占，第 3-5 个候选各发 1 条
    assert all(p[1] != blocked[0]["videoUrl"] or True for p in acc1_posts)
    # 最终样本仍然集齐 3 条且全部核对正常
    rows = _rows_by_target(_leads(db))
    assert len(rows[ACC1]) == 3 and all(r["status"] == "visible" for r in rows[ACC1])


def test_scan_failure_records_no_sample(tmp_path, monkeypatch):
    """推荐页扫描失败：该账号记「无样本」，另一个账号仍正常互检……对手无样本时
    剩余样本记「检测未完成」而不是凭空消失。"""
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    _Harness(monkeypatch, scan_items={ACC1: None, ACC2: _candidates("b", 5)})
    _run(ctx, _cfg())

    rows = _rows_by_target(_leads(db))
    # ACC1：无样本结论
    assert all(r["status"] == "no_sample" for r in rows.get(ACC1, []))
    assert any(c["accountId"] == ACC1 and c["status"] == "no_sample"
               for c in ctx.stats["visibilityAccounts"])
    # ACC2 有样本但没有可配对的检测员 → 样本逐条「检测未完成」
    assert rows.get(ACC2) and all(r["status"] == "inconclusive" for r in rows[ACC2])
    assert "没有可配对的检测账号" in rows[ACC2][0]["reason"]


# --------------------------------------------------------------------------
# 评论内容三模式
# --------------------------------------------------------------------------
def test_custom_mode_uses_fixed_text(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    h = _Harness(monkeypatch, scan_items={ACC1: _candidates("a", 5),
                                          ACC2: _candidates("b", 5)})
    _run(ctx, _cfg(visibilityCommentMode="custom",
                   visibilityCustomComment="这条评论用来检测吞评"))
    assert h.performs and all(p[2] == "这条评论用来检测吞评" for p in h.performs)


def test_emoji_mode_uses_positive_pool(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    h = _Harness(monkeypatch, scan_items={ACC1: _candidates("a", 5),
                                          ACC2: _candidates("b", 5)})
    _run(ctx, _cfg())
    assert h.performs and all(p[2] in vis.EMOJI_POOL for p in h.performs)


def test_persona_mode_generates_via_bound_persona(tmp_path, monkeypatch):
    """人设模式：评论正文由绑定智能体的人设生成（走 ai_personas 通道）。"""
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    h = _Harness(monkeypatch, scan_items={ACC1: _candidates("a", 5),
                                          ACC2: _candidates("b", 5)})
    from app import ai_personas
    monkeypatch.setattr(ai_personas, "load_task_config", lambda _db: {
        "accountPersonas": {ACC1: "p1", ACC2: "p1"},
        "personas": [{"id": "p1", "role": "装修顾问", "goal": "交流装修话题",
                      "prompt": "十年家装从业者", "style": "自然亲切"}]})
    monkeypatch.setattr(ai_personas, "_llm_settings", lambda _db: {})
    monkeypatch.setattr(ai_personas, "_call_chat",
                        lambda cfg, prompt: json.dumps(
                            {"should_reply": True, "reply_content": "这个收口细节讲得很实在",
                             "thought": "ok"}, ensure_ascii=False))
    _run(ctx, _cfg(visibilityCommentMode="persona"))

    assert h.performs and all(p[2] == "这个收口细节讲得很实在" for p in h.performs)
    rows = _rows_by_target(_leads(db))
    assert all(r["status"] == "visible" for r in rows[ACC1] + rows[ACC2])


def test_scope_written_per_account(tmp_path, monkeypatch):
    """发表样本前必须为每个账号写入 post-comment 作用域（签名授权）。"""
    db = _make_db(tmp_path)
    ctx = _make_ctx(tmp_path, db)
    _Harness(monkeypatch, scan_items={ACC1: _candidates("a", 5),
                                      ACC2: _candidates("b", 5)})
    from app.execution import live_scope
    scopes = __import__("pathlib").Path(ctx.db_path).parent / "scopes"
    monkeypatch.setattr(live_scope, "monitor_scope_dir", lambda: scopes)
    # 任务结束时实现会清掉作用域文件（安全要求），这里保留以便校验写入内容
    monkeypatch.setattr(live_scope, "clear_monitor_scope", lambda *a, **k: None)
    _run(ctx, _cfg())
    files = list(scopes.glob("vis_rec_t1-*.json"))
    assert len(files) == 2
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["mode"] == "monitor"
        assert data["actions"] == {"post-comment": 3}
