"""监控对象 · 群聊列表：读取 / 缓存 / 取消 / 通道校验。无真机、无网络。"""
import sqlite3

import pytest

from app.runtime import browser, group_list
from app.api import ipc_bridge


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "g.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE accounts (id TEXT PRIMARY KEY,nickname TEXT,platform TEXT)")
    con.executemany("INSERT INTO accounts VALUES (?,?,?)",
                    [("a", "甲", "douyin"), ("b", "乙", "douyin"), ("x", "丙", "kuaishou")])
    con.commit()
    con.close()
    monkeypatch.setattr(ipc_bridge, "DB_PATH", path)
    return path


def test_cache_keeps_unchecked_groups(db):
    """「未勾选的也会保留」：重读只做并集，旧群不会因为本次没读到就消失。"""
    assert group_list.save(db, "a", ["交流群", "粉丝群"]) == 2
    group_list.save(db, "a", ["新群"])
    names = [g["name"] for g in group_list.cached(db) if g["accountId"] == "a"]
    assert names == ["交流群", "粉丝群", "新群"] or set(names) == {"交流群", "粉丝群", "新群"}
    # 同账号同名只刷新时间，不产生重复行
    group_list.save(db, "a", ["交流群"])
    assert len([g for g in group_list.cached(db) if g["accountId"] == "a"]) == 3


def test_read_walks_accounts_and_reports_per_account_status(db, monkeypatch):
    calls = []
    def execute(cmd, **kwargs):
        calls.append(cmd)
        if cmd["accountId"] == "a":
            return {"ok": True, "groups": [{"name": "交流群"}, {"name": "交流群"}, {"name": "粉丝群"}],
                    "rows": 12}
        return {"ok": False, "error": "需要登录才能读群聊：请在该账号窗口登录后重试"}
    monkeypatch.setattr(browser, "execute", execute)
    events = []
    out = group_list.read(db, ["a", "b"], emit=events.append)
    assert out["success"] and len(out["results"]) == 2
    first, second = out["results"]
    assert first["status"] == "done" and first["names"] == ["交流群", "粉丝群"]  # 已去重
    assert second["status"] == "error" and "登录" in second["message"]
    assert [e["accountId"] for e in events] == ["a", "a", "b"]  # 开始/结果/下一账号开始
    assert [g["name"] for g in group_list.cached(db)] == sorted(["交流群", "粉丝群"])
    assert all(c["type"] == "read-group-list" for c in calls)


def test_cancel_stops_before_next_account(db, monkeypatch):
    monkeypatch.setattr(browser, "execute", lambda cmd, **k: {
        "ok": True, "groups": [{"name": "第一群"}]})
    out = group_list.read(db, ["a", "b"], cancelled=lambda: True)
    assert out["success"] and out.get("cancelled") is True
    assert out["results"] == []


def test_channel_rejects_missing_or_foreign_accounts(db):
    assert ipc_bridge._read_monitor_group_list({})["success"] is False
    assert ipc_bridge._read_monitor_group_list({"accountIds": ["x"]})["success"] is False
    assert ipc_bridge._monitor_group_cache({})["success"] is True
