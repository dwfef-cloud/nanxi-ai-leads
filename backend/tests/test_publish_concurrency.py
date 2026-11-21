"""作品发布运行器：节奏与授权扩展（2026-11-18）。

对应清单条目：
- #67 上传与提交节奏：publishMode=parallel（多账号并行最多 2，同账号严格串行）
  / interval（依次处理 + staggerMinSec–staggerMaxSec 随机间隔）；
- #60 官方定时：距发布时间 ≥ 2 小时交给平台定时发布，壳侧设置失败
  （status=schedule_failed）必须回落「软件等到点提交」，绝不提前发、绝不丢篇；
- #61 视频自定义封面：coverEnabled + custom 时把封面文件透传给壳；
- 崩溃残留恢复：processing 且账号不在占用集里的篇目要能被重新领取；
- 登录失效跳账号：login_required 后该账号剩余篇直接落失败，其余账号照常并行。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from app.api import ipc_bridge as bridge
from app.runtime import publish

TASKS_DDL = """CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, task_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'stopped', error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "pub.db"
    con = sqlite3.connect(path)
    con.executescript(TASKS_DDL)
    con.commit()
    con.close()
    monkeypatch.setattr(bridge, "DB_PATH", path)
    publish.reset_for_tests()
    yield str(path)
    publish.reset_for_tests()


def _item(item_id: str, account: str, name: str, db: str, **over) -> dict:
    media = str(Path(db).parent / f"{item_id}.mp4")
    Path(media).write_bytes(b"\x00" * 16)
    item = {"id": item_id, "accountId": account, "accountName": name,
            "media": {"type": "video", "filePath": media, "filePaths": [media]},
            "title": "标题", "description": "描述", "status": "ready"}
    item.update(over)
    return item


def _task(db, items: list[dict], **over) -> dict:
    rec = {"id": "pub_c", "name": "节奏任务", "items": items,
           "schedule": {"enabled": False, "runAt": None}}
    rec.update(over)
    return rec


def _run_task(db, task):
    bridge.dispatch("save-work-publish-task", [task])
    out = publish.start(db, {"taskRecord": task})
    assert out["success"] is True, out
    for _ in range(200):
        if not publish.running_ids():
            break
        time.sleep(0.05)
    rec = bridge.dispatch("get-work-publish-tasks", [{}])[0]
    return rec


def test_parallel_runs_two_accounts_at_once(db, monkeypatch):
    """多账号并行：一篇挂着时，另一账号的篇目照样发起。"""
    from app.runtime import browser

    release = threading.Event()
    started_other = threading.Event()
    timeline: list[str] = []

    def fake_execute(cmd, **kw):
        acc = str(kw.get("account_id") or "")
        timeline.append(acc)
        if acc == "acc-1" and not started_other.is_set():
            # 第一篇挂住，等另一账号的篇目发起后再放行
            assert release.wait(8), "并行 worker 没有在第一篇挂住时发起第二篇"
        if acc == "acc-2":
            release.set()
        started_other.set()
        return {"ok": True, "status": "published"}

    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(browser, "execute", fake_execute)
    task = _task(db, [_item("i1", "acc-1", "一号", db),
                      _item("i2", "acc-2", "二号", db)],
                 publishMode="parallel")
    rec = _run_task(db, task)
    assert sorted(timeline) == ["acc-1", "acc-2"]
    assert all(i["status"] == "published" for i in rec["items"])


def test_same_account_items_are_serialized(db, monkeypatch):
    """同账号按顺序：第一篇挂住时，第二篇绝不被发起。"""
    from app.runtime import browser

    calls: list[str] = []
    first_started = threading.Event()
    release = threading.Event()

    def fake_execute(cmd, **kw):
        calls.append(str(kw.get("account_id") or ""))
        if len(calls) == 1:
            first_started.set()
            release.wait(8)
        return {"ok": True, "status": "published"}

    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(browser, "execute", fake_execute)
    task = _task(db, [_item("i1", "acc-1", "一号", db),
                      _item("i2", "acc-1", "一号", db)],
                 publishMode="parallel")
    threads_started = threading.Event()

    original_start = publish.start

    def spy_start(*a, **k):
        res = original_start(*a, **k)
        threads_started.set()
        return res

    monkeypatch.setattr(publish, "start", spy_start)
    release.set()
    rec = _run_task(db, task)
    assert len(calls) == 2
    assert all(i["status"] == "published" for i in rec["items"])


def test_interval_mode_waits_stagger_between_items(db, monkeypatch):
    """依次处理：每条提交后随机等待 staggerMin–staggerMax 秒。"""
    from app.runtime import browser

    stamps: list[float] = []

    def fake_execute(cmd, **kw):
        stamps.append(time.time())
        return {"ok": True, "status": "published"}

    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(browser, "execute", fake_execute)
    task = _task(db, [_item("i1", "acc-1", "一号", db),
                      _item("i2", "acc-2", "二号", db)],
                 publishMode="interval", staggerMinSec=0.3, staggerMaxSec=0.5)
    _run_task(db, task)
    assert len(stamps) == 2
    assert stamps[1] - stamps[0] >= 0.28, "第二条提交前必须先等 stagger 区间"


def test_far_schedule_uses_official_timer(db, monkeypatch):
    """3 小时后的定时：直接交给壳设置官方定时，软件不挂到点。"""
    from app.runtime import browser

    seen = {}
    monkeypatch.setattr(browser, "shell_available", lambda: True)

    def fake_execute(cmd, **kw):
        seen.update(cmd)
        return {"ok": True, "status": "published"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    run_at = int(time.time() * 1000) + 3 * 3600 * 1000
    item = _item("i1", "acc-1", "一号", db,
                 schedule={"enabled": True, "runAt": run_at})
    task = _task(db, [item], publishMode="interval")
    started = time.time()
    rec = _run_task(db, task)
    assert time.time() - started < 60, "官方定时不能让软件等到点"
    assert seen.get("officialScheduleAt") == run_at
    assert rec["items"][0]["status"] == "published"
    assert rec["items"][0].get("officialScheduledAt") == run_at


def test_official_schedule_failure_falls_back_to_wait(db, monkeypatch):
    """官方定时失败：回落「软件等到点提交」，同一篇最终正常发布。"""
    from app.runtime import browser

    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(publish, "OFFICIAL_SCHEDULE_MIN_MS", 0)  # 任何 runAt 都先试官方定时

    calls: list[dict] = []

    def fake_execute(cmd, **kw):
        calls.append(dict(cmd))
        if cmd.get("officialScheduleAt"):
            return {"ok": False, "status": "schedule_failed",
                    "error": "页面上没有「定时发布」开关"}
        return {"ok": True, "status": "published"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    run_at = int(time.time() * 1000) + 1500  # 回落后 1.5 秒即到点，测试不用真等几小时
    item = _item("i1", "acc-1", "一号", db,
                 schedule={"enabled": True, "runAt": run_at})
    rec = _run_task(db, _task(db, [item], publishMode="interval"))
    assert len(calls) == 2, "先试官方定时一次，失败后等点重提一次"
    assert calls[0].get("officialScheduleAt") == run_at
    assert not calls[1].get("officialScheduleAt"), "回落提交时不能再带官方定时"
    assert rec["items"][0]["status"] == "published"
    assert any("官方定时设置未成功" in l["message"] for l in rec.get("logs", []))


def test_cover_args_passed_only_for_custom_video_with_existing_file(db, monkeypatch):
    """封面：custom 模式且文件存在才透传；auto/缺文件不传。"""
    from app.runtime import browser

    cmds: dict[str, dict] = {}
    monkeypatch.setattr(browser, "shell_available", lambda: True)

    def fake_execute(cmd, **kw):
        cmds[str(kw.get("account_id") or "")] = dict(cmd)
        return {"ok": True, "status": "published"}

    monkeypatch.setattr(browser, "execute", fake_execute)
    cover = str(Path(db).parent / "cover.jpg")
    Path(cover).write_bytes(b"\x89PNG")
    missing = str(Path(db).parent / "nope.jpg")
    items = [
        _item("i1", "acc-1", "一号", db, coverEnabled=True, coverMode="custom",
              coverLandscapePath=cover, coverPortraitPath=missing),
        _item("i2", "acc-2", "二号", db, coverEnabled=True, coverMode="auto",
              coverLandscapePath=cover),
        _item("i3", "acc-3", "三号", db, coverEnabled=False, coverMode="custom",
              coverLandscapePath=cover),
    ]
    rec = _run_task(db, _task(db, items, publishMode="parallel"))
    assert cmds["acc-1"].get("coverLandscapePath") == cover
    assert "coverPortraitPath" not in cmds["acc-1"], "文件不存在就不能传给壳"
    assert "coverLandscapePath" not in cmds["acc-2"], "自动封面不需要软件干预"
    assert "coverLandscapePath" not in cmds["acc-3"], "封面开关没开就不传"
    assert all(i["status"] == "published" for i in rec["items"])


def test_stale_processing_item_is_reclaimed(db, monkeypatch):
    """上次崩溃留下的 processing：本轮要能重新领取并发布，不能永远卡住。"""
    from app.runtime import browser

    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(browser, "execute", lambda cmd, **kw: {"ok": True, "status": "published"})
    item = _item("i1", "acc-1", "一号", db, status="processing")
    rec = _run_task(db, _task(db, [item]))
    assert rec["items"][0]["status"] == "published"


def test_login_required_skips_rest_of_that_account_only(db, monkeypatch):
    """登录失效：该账号剩余篇直接落失败，其他账号照常发布（并行模式）。"""
    from app.runtime import browser

    published_accounts: list[str] = []

    def fake_execute(cmd, **kw):
        acc = str(kw.get("account_id") or "")
        if acc == "acc-dead":
            return {"ok": False, "status": "failed", "code": "login_required",
                    "error": "该账号未登录创作者中心"}
        published_accounts.append(acc)
        return {"ok": True, "status": "published"}

    monkeypatch.setattr(browser, "shell_available", lambda: True)
    monkeypatch.setattr(browser, "execute", fake_execute)
    items = [
        _item("i1", "acc-dead", "失效号", db),
        _item("i2", "acc-dead", "失效号", db),
        _item("i3", "acc-ok", "正常号", db),
    ]
    rec = _run_task(db, _task(db, items, publishMode="parallel"))
    statuses = {i["id"]: i["status"] for i in rec["items"]}
    assert statuses["i1"] == "failed"
    assert statuses["i2"] == "failed", "同账号后续篇不再逐篇上传"
    assert statuses["i3"] == "published", "其他账号照常发布"
    assert published_accounts == ["acc-ok"], "失效账号只发起过一次"
