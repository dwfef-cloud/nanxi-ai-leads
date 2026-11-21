"""评论获客运行时资源池 + 任务记录持久化。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import leadgen_pools  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "pool_test.db"


def _init(db, task="t1", kind="keyword", values=None, resume=False):
    return leadgen_pools.init_pool(db, task, kind, values or ["kw1", "kw2", "kw3"], resume=resume)


def test_init_pool_counts(db):
    res = _init(db)
    assert res["total"] == 3 and res["remaining"] == 3 and not res["resumed"]
    # 重复初始化（非 resume）= 重新开始
    res2 = _init(db)
    assert res2["total"] == 3 and res2["remaining"] == 3 and res2["resumed"] is False


def test_slash_separated_keywords_do_not_enter_pool(db):
    """设置页写成「多少钱 / 怎么做 / 怎么联系」时，斜杠不能被当成一个关键词。

    真机 2026-11-19：前端按空白切完是 ['多少钱', '/', '怎么做', '怎么联系']，
    池里因此多了一条 '/'，账号领到它就只搜一个斜杠，整轮零产出。
    """
    from app.execution.leadgen_config import words as normalize_words
    raw = "多少钱 / 怎么做 / 怎么联系"
    assert normalize_words(raw) == ["多少钱", "怎么做", "怎么联系"]
    assert leadgen_pools._clean_values(raw) == ["多少钱", "怎么做", "怎么联系"]
    # 前端已经切好的数组（含纯标点项）也要在入池时被清掉
    res = _init(db, values=["多少钱", "/", "怎么做", "怎么联系"])
    assert res["total"] == 3
    claimed = [leadgen_pools.claim_keyword(db, "t1", f"acc{i}")["keyword"] for i in range(3)]
    assert claimed == ["多少钱", "怎么做", "怎么联系"]


def test_resume_drops_legacy_punctuation_item(db):
    """历史池里的 '/' 在 resume 时被剔除，不再被领去搜索。"""
    leadgen_pools.init_pool(db, "t1", "keyword", ["多少钱", "/", "怎么做"])
    leadgen_pools.release_pool(db, "t1", "keyword", preserve=True)
    res = leadgen_pools.init_pool(db, "t1", "keyword", ["多少钱", "怎么做"], resume=True)
    assert res["total"] == 2
    assert leadgen_pools.claim_keyword(db, "t1", "acc1")["keyword"] in {"多少钱", "怎么做"}
    assert leadgen_pools.claim_keyword(db, "t1", "acc2")["keyword"] in {"多少钱", "怎么做"}


def test_parallel_account_finishes_and_readback_do_not_replace_other_results(db):
    from concurrent.futures import ThreadPoolExecutor
    initial = {"name": "多账号离线测试", "configSnapshot": {}, "startedAt": 1}
    for account in ("a", "b"):
        leadgen_pools.reviewed_run_update(db, "task", account, account+"-run", initial=initial)
    def finish(account):
        stats = {"published": 2, "videoComments": 2, "uncertain": int(account == "a")}
        leadgen_pools.reviewed_run_update(db, "task", account, account+"-run", result={
            "status": "failed" if account == "a" else "completed", "stats": stats,
            "endedAt": 2, "endReason": "test", "message": "离线测试",
            "run": {"automationTaskId": account, "runTaskId": account+"-run", "accountId": account, "stats": stats}})
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(finish, ("a", "b")))
    record = leadgen_pools.load_records(db)["task"]
    assert record["stats"]["published"] == 4 and len(record["runs"]) == 2
    leadgen_pools.reviewed_readback_update(db, "task", "a", "a-run")
    record = leadgen_pools.load_records(db)["task"]
    assert record["stats"]["published"] == 5 and record["stats"]["uncertain"] == 0
    assert record["reviewedAccounts"]["a"]["stats"]["published"] == 3
    assert record["reviewedAccounts"]["b"]["stats"]["published"] == 2
    assert sorted(r["stats"]["published"] for r in record["runs"]) == [2, 3]
    assert record["status"] == "failed"


def test_old_readback_does_not_change_a_new_run_statistics(db):
    initial = {"name": "续跑离线测试", "configSnapshot": {}, "startedAt": 1}
    leadgen_pools.reviewed_run_update(db, "task", "a", "new-run", initial=initial)
    leadgen_pools.reviewed_readback_update(db, "task", "a", "old-run")
    record = leadgen_pools.load_records(db)["task"]
    assert record["stats"] == {} and record["status"] == "running"


def test_claim_round_and_exhaust(db):
    _init(db, values=["a", "b"])
    first = leadgen_pools.claim_keyword(db, "t1", "acc-1")
    assert first["keyword"] == "a" and first["resumed"] is False
    # 同账号重复领取 = 续跑拿同一个
    again = leadgen_pools.claim_keyword(db, "t1", "acc-1")
    assert again["keyword"] == "a" and again["resumed"] is True
    second = leadgen_pools.claim_keyword(db, "t1", "acc-2")
    assert second["keyword"] == "b"
    # 领完：报错且带明确原因
    out = leadgen_pools.claim_keyword(db, "t1", "acc-3")
    assert "error" in out
    # reuseActive：池空但允许共用已领取的关键词
    shared = leadgen_pools.claim_keyword(db, "t1", "acc-3", reuse_active=True)
    assert shared.get("shared") is True and shared.get("keyword") in {"a", "b"}


def test_release_preserve_then_resume(db):
    _init(db, values=["a", "b"])
    leadgen_pools.claim_keyword(db, "t1", "acc-1")
    out = leadgen_pools.release_pool(db, "t1", "keyword", preserve=True)
    assert out["success"] is True and out["preserved"] is True
    # resume 恢复：占用被释放回待领取
    res = _init(db, values=["a", "b"], resume=True)
    assert res["resumed"] is True and res["remaining"] == 2


def test_release_delete(db):
    _init(db)
    leadgen_pools.release_pool(db, "t1", "keyword", preserve=False)
    out = leadgen_pools.claim_keyword(db, "t1", "acc-1")
    assert "error" in out  # 池已被删，等于没有关键词可领


def test_hashtag_pool_token_stable(db):
    r1 = _init(db, kind="hashtag", values=["/hashtag/1", "/hashtag/2"])
    assert r1["token"]
    # resume 不换 token（壳用它标记同一队列）
    r2 = _init(db, kind="hashtag", values=["/hashtag/1"], resume=True)
    assert r2["token"] == r1["token"]


def test_task_record_save_update_load(db):
    rec = {"id": "leadgen_1", "name": "任务A", "status": "running", "runs": [{"accountId": "a"}]}
    assert leadgen_pools.save_task_record(db, rec)["success"] is True
    assert leadgen_pools.update_task_record(
        db, "leadgen_1", {"status": "stopped", "endedAt": 123}
    )["success"] is True
    recs = leadgen_pools.load_records(db)
    assert recs["leadgen_1"]["status"] == "stopped"
    assert recs["leadgen_1"]["runs"] == [{"accountId": "a"}]
    assert recs["leadgen_1"]["endedAt"] == 123


def test_task_record_requires_id(db):
    assert "error" in leadgen_pools.save_task_record(db, {"name": "no id"})


# ---------------------------------------------------------------------------
# 桥接契约：通道注册与返回形状
# ---------------------------------------------------------------------------
@pytest.fixture()
def bridge_db(tmp_path, monkeypatch):
    db = tmp_path / "bridge.db"
    import app.api.ipc_bridge as ib
    monkeypatch.setattr(ib, "DB_PATH", db, raising=False)
    return db


def test_bridge_registered_channels():
    from app.api import ipc_bridge as ib
    for ch in ("init-leadgen-keyword-pool", "init-leadgen-hashtag-pool",
               "init-leadgen-specific-video-pool", "claim-leadgen-keyword",
               "release-leadgen-keyword-pool", "release-leadgen-hashtag-pool",
               "release-leadgen-specific-video-pool", "save-leadgen-task",
               "update-leadgen-task", "get-entity-leadgen-busy-account-ids"):
        assert ch in ib.EXPLICIT, ch


def test_bridge_init_and_claim(bridge_db):
    from app.api import ipc_bridge as ib
    d = ib._make_pool_init("keyword")({"leadgenTaskId": "tt", "keywords": ["k1", "k2"]})
    assert d["success"] is True and d["total"] == 2
    res = ib._claim_leadgen_keyword({"leadgenTaskId": "tt", "accountId": "ac1"})
    assert res["keyword"] == "k1"
    # 通道返回形状：壳只检查 .error，成功时不得带 error 字段
    assert "error" not in d


def test_bridge_save_and_list(bridge_db):
    from app.api import ipc_bridge as ib
    rec = {"id": "leadgen_9", "name": "桥接任务", "status": "running",
           "createdAt": 1000, "runs": [], "stats": {}}
    assert ib._save_leadgen_task(rec)["success"] is True
    tasks = ib._tasks("leadgen")({})
    assert any(t["id"] == "leadgen_9" for t in tasks)
    ib._update_leadgen_task({"id": "leadgen_9", "patch": {"status": "stopped"}})
    tasks = ib._tasks("leadgen")({})
    assert next(t for t in tasks if t["id"] == "leadgen_9")["status"] == "stopped"


def test_bridge_hashtag_returns_token(bridge_db):
    from app.api import ipc_bridge as ib
    res = ib._make_pool_init("hashtag")({"leadgenTaskId": "th", "urls": ["/hashtag/9"]})
    assert res["success"] is True and res["token"]
