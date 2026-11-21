"""其他模块补齐通道的契约测试（离线，临时库）。

覆盖 2026-11-17 补齐的「数据在库、只缺读写通道」一批功能：
- 线索库删除（已选 / 低意向 / 已触达）与 UID 导入
- 采集库隐藏（删除 / 清空不再从任务明细同步回来）
- 视频主评记录读取与清空
- 作品发布任务删除 / 模板存取
- 短链解析 / 代理测试 / 缓存统计 / AI 生成文案
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.api import ipc_bridge as bridge

DDL = """
CREATE TABLE leads (
    id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'own_comment',
    source_url TEXT NOT NULL DEFAULT '',
    source_keyword TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT 'douyin',
    external_id TEXT NOT NULL DEFAULT '',
    source_task_id TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    video TEXT NOT NULL DEFAULT '',
    video_id TEXT NOT NULL DEFAULT '',
    comment_id TEXT NOT NULL DEFAULT '',
    comment_user_id TEXT NOT NULL DEFAULT '',
    comment_time TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    intent_level TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    customer_need TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'collected',
    account TEXT,
    region TEXT NOT NULL DEFAULT '',
    manual INTEGER NOT NULL DEFAULT 0,
    hue INTEGER NOT NULL DEFAULT 200,
    lost_reason TEXT NOT NULL DEFAULT '',
    conversion_amount REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _seed_leads(db: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db)
    for row in rows:
        conn.execute(
            "INSERT INTO leads (id, nickname, source_keyword, intent_level, source, "
            "region, comment, source_url, video, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'collected','2026-11-17T00:00:00','2026-11-17T00:00:00')",
            row)
    conn.commit()
    conn.close()


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "leads.db"
    conn = sqlite3.connect(db)
    conn.execute(DDL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(bridge, "DB_PATH", db)
    return db


# ── 线索库删除 ────────────────────────────────────────────────

def test_delete_leads_from_history(tmp_db):
    _seed_leads(tmp_db, [
        ("L1", "阿杰", "AI智能体", "A", "own_comment", "深圳", "怎么搭", "", ""),
        ("L2", "李姐", "AI副业", "C", "own_comment", "广州", "想做副业", "", ""),
    ])
    out = bridge.dispatch("delete-leads-from-history", [["L1", "L2"]])
    assert out["success"] and out["deleted"] == 2
    assert bridge.dispatch("get-leads-page", [{"page": 1, "pageSize": 10}])["total"] == 0


def test_delete_leads_requires_ids(tmp_db):
    out = bridge.dispatch("delete-leads-from-history", [[]])
    assert not out["success"]


def test_delete_low_intention_keeps_high(tmp_db):
    _seed_leads(tmp_db, [
        ("L1", "阿杰", "AI智能体", "A", "own_comment", "深圳", "怎么搭", "", ""),
        ("L2", "李姐", "AI副业", "high", "own_comment", "广州", "想做副业", "", ""),
        ("L3", "阿强", "装修", "C", "own_comment", "深圳", "多少钱", "", ""),
        ("L4", "阿明", "获客", "B", "own_comment", "上海", "聊聊", "", ""),
    ])
    out = bridge.dispatch("delete-low-intention-leads", [{}])
    assert out["success"] and out["deleted"] == 2  # C + B
    remain = {i["leadId"] for i in bridge.dispatch(
        "get-leads-page", [{"page": 1, "pageSize": 10}])["items"]}
    assert remain == {"L1", "L2"}


def test_delete_touched_leads_uses_touch_ledger(tmp_db):
    _seed_leads(tmp_db, [
        ("L1", "阿杰", "AI智能体", "A", "own_comment", "深圳", "怎么搭", "", ""),
        ("L2", "李姐", "AI副业", "C", "own_comment", "广州", "想做副业", "", ""),
    ])
    conn = sqlite3.connect(tmp_db)
    conn.execute("CREATE TABLE comment_tasks (id INTEGER PRIMARY KEY, lead_id TEXT, status TEXT)")
    conn.execute("INSERT INTO comment_tasks (lead_id, status) VALUES ('L1', 'replied')")
    conn.commit()
    conn.close()
    out = bridge.dispatch("delete-touched-leads", [{}])
    assert out["success"] and out["deleted"] == 1
    remain = {i["leadId"] for i in bridge.dispatch(
        "get-leads-page", [{"page": 1, "pageSize": 10}])["items"]}
    assert remain == {"L2"}


# ── UID 导入 ─────────────────────────────────────────────────

def test_append_imported_leads_and_dedup(tmp_db):
    items = [
        {"leadId": "imp-1", "userUrl": "https://www.douyin.com/user/100", "nickname": "小张"},
        {"userUrl": "https://www.douyin.com/user/200", "nickname": "小王"},
    ]
    out = bridge.dispatch("append-imported-leads", [items])
    assert out["success"] and out["inserted"] == 2
    page = bridge.dispatch("get-leads-page", [{"page": 1, "pageSize": 10}])
    assert page["total"] == 2
    by_nick = {i["nickname"]: i for i in page["items"]}
    assert by_nick["小张"]["userUrl"].endswith("/user/100")
    # 同一批重导：按稳定 id 去重不重复落行
    out2 = bridge.dispatch("append-imported-leads", [items])
    assert out2["success"] and out2["inserted"] == 0
    assert bridge.dispatch("get-leads-page", [{"page": 1, "pageSize": 10}])["total"] == 2


# ── 采集库隐藏 ───────────────────────────────────────────────

def _seed_library(db: str) -> None:
    _seed_leads(db, [
        ("V1", "用户A", "kw", "C", "own_comment", "深圳", "评1", "u1", "https://www.douyin.com/video/111"),
        ("V2", "用户B", "kw", "C", "own_comment", "广州", "评2", "u2", "https://www.douyin.com/video/222"),
    ])


def test_remove_processed_videos_hides_from_list(tmp_db):
    _seed_library(tmp_db)
    page = bridge.dispatch("get-processed-videos", [{}])
    assert page["total"] == 2
    target = next(i for i in page["items"] if i["title"].endswith("/111"))
    out = bridge.dispatch("remove-processed-videos", [[target]])
    assert out["success"]
    page2 = bridge.dispatch("get-processed-videos", [{}])
    assert page2["total"] == 1
    assert all(not i["title"].endswith("/111") for i in page2["items"])


def test_clear_collected_library_video_and_author(tmp_db):
    _seed_library(tmp_db)
    out = bridge.dispatch("clear-collected-library", [{"type": "video"}])
    assert out["success"] and out["cleared"] == 2
    videos = bridge.dispatch("get-collected-videos-page", [{"page": 1, "pageSize": 20}])
    assert videos["total"] == 0
    # 线索本体不受影响
    assert bridge.dispatch("get-leads-page", [{"page": 1, "pageSize": 10}])["total"] == 2

    out2 = bridge.dispatch("clear-collected-library", [{"type": "author"}])
    assert out2["success"] and out2["cleared"] == 2
    authors = bridge.dispatch("get-collected-authors-page", [{"page": 1, "pageSize": 20}])
    assert authors["total"] == 0


def test_clear_collected_library_requires_type(tmp_db):
    out = bridge.dispatch("clear-collected-library", [{}])
    assert not out["success"]


# ── 视频主评记录 ─────────────────────────────────────────────

def _seed_main_comments(db: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS live_action_results ("
        "id INTEGER PRIMARY KEY, task_id TEXT DEFAULT '', action TEXT DEFAULT '', "
        "keyword TEXT DEFAULT '', title TEXT DEFAULT '', url TEXT DEFAULT '', "
        "text TEXT DEFAULT '', status TEXT DEFAULT '', code TEXT DEFAULT '', "
        "created_ms INTEGER DEFAULT 0, account_id TEXT DEFAULT '', run_id TEXT DEFAULT '')")
    conn.execute(
        "INSERT INTO live_action_results(action,status,url,text,account_id,created_ms) "
        "VALUES('post-comment','sent_ok','https://www.douyin.com/video/111','主评内容A','acc1',1794800000000)")
    conn.execute(
        "INSERT INTO live_action_results(action,status,url,text,account_id,created_ms) "
        "VALUES('like-video','sent_ok','https://www.douyin.com/video/222','','acc1',1794800001000)")
    conn.commit()
    conn.close()


def test_video_main_comments_read(tmp_db):
    _seed_main_comments(tmp_db)
    out = bridge.dispatch("get-video-main-comments", [{"page": 1, "pageSize": 10}])
    assert out["success"] and out["total"] == 1
    item = out["items"][0]
    assert item["content"] == "主评内容A"
    assert item["videoId"] == "111"
    assert item["accountId"] == "acc1"


def test_video_main_comments_keyword_filter(tmp_db):
    _seed_main_comments(tmp_db)
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        "INSERT INTO live_action_results(action,status,url,text,account_id,created_ms) "
        "VALUES('post-comment','sent_ok','https://www.douyin.com/video/333','另一条','acc1',1794800002000)")
    conn.commit()
    conn.close()
    out = bridge.dispatch("get-video-main-comments", [{"page": 1, "pageSize": 10, "keyword": "另一条"}])
    assert out["total"] == 1 and out["items"][0]["content"] == "另一条"


def test_clear_video_main_comments(tmp_db):
    _seed_main_comments(tmp_db)
    out = bridge.dispatch("clear-video-main-comments", [{}])
    assert out["success"] and out["cleared"] == 1  # like-video 不受影响
    assert bridge.dispatch("get-video-main-comments", [{"page": 1, "pageSize": 10}])["total"] == 0


# ── 作品发布：删除 / 模板 ────────────────────────────────────

def test_work_publish_delete(tmp_db):
    rec = task_store_save = None
    from app.api import task_store
    saved = task_store.save_task(bridge.DB_PATH, "publish", {"id": "wp1", "name": "任务一"})
    assert saved.get("success", True)
    out = bridge.dispatch("delete-work-publish-task", ["wp1"])
    assert out["success"] and out["deleted"] == 1
    assert task_store.list_tasks(bridge.DB_PATH, "publish") == []
    # 重复删除：返回 0 但不报错
    out2 = bridge.dispatch("delete-work-publish-tasks", [["wp1"]])
    assert out2["success"] and out2["deleted"] == 0


def test_work_publish_templates_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "DATA_DIR", tmp_path)
    saved = bridge.dispatch("save-work-publish-template", [
        {"name": "模板A", "settings": {"publishOptions": {"whoCanSee": "self"}}}])
    assert isinstance(saved, list) and len(saved) == 1
    rec = saved[0]
    assert rec["name"] == "模板A" and rec["settings"]["publishOptions"]["whoCanSee"] == "self"

    # 同名保存 = 更新
    saved2 = bridge.dispatch("save-work-publish-template", [
        {"name": "模板A", "settings": {"publishOptions": {"whoCanSee": "all"}}}])
    assert len(saved2) == 1 and saved2[0]["settings"]["publishOptions"]["whoCanSee"] == "all"
    assert bridge.dispatch("get-work-publish-templates", [{}]) == saved2

    # 空名称：前端走 catch 分支，这里抛 ValueError
    with pytest.raises(ValueError):
        bridge.dispatch("save-work-publish-template", [{"name": "  "}])

    left = bridge.dispatch("delete-work-publish-template", [rec["id"]])
    assert left == []
    with pytest.raises(ValueError):
        bridge.dispatch("delete-work-publish-template", ["不存在"])


# ── 工具通道 ─────────────────────────────────────────────────

def test_resolve_douyin_video_urls(tmp_db, monkeypatch):
    from app.runtime import monitor_engine

    def fake_resolve(url: str) -> str:
        if "v.douyin.com" in url:
            return f"https://www.douyin.com/video/{url.rstrip('/').split('/')[-1]}"
        return url

    monkeypatch.setattr(monitor_engine, "resolve_video_url", fake_resolve)
    out = bridge.dispatch("resolve-douyin-video-urls", [
        {"urls": ["https://v.douyin.com/abc123/", "https://www.douyin.com/video/999"]}])
    assert out["success"]
    assert out["resolved"][0].endswith("c123")
    assert out["resolved"][1].endswith("/999")


def test_test_account_proxy_validation(tmp_db):
    out = bridge.dispatch("test-account-proxy", [{"proxy": {"type": "http", "host": "", "port": 8080}}])
    assert not out["success"] and "不完整" in out["message"]
    out2 = bridge.dispatch("test-account-proxy", [
        {"proxy": {"type": "socks5", "host": "1.2.3.4", "port": 1080}}])
    assert not out2["success"] and "SOCKS5" in out2["message"]
    out3 = bridge.dispatch("test-account-proxy", [{"proxy": {"type": "http"}}])
    assert not out3["success"]


def test_get_cache_size_returns_bytes(tmp_path, monkeypatch):
    # 2026-11-17 起缓存统计口径扩到「数据目录 + 壳侧网络/图形缓存」，
    # 统计实现移进 app_settings.cache_size（见 tests/test_system_settings.py）。
    from app.api import app_settings

    monkeypatch.setattr(app_settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_settings, "shell_userdata", lambda: None)
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "a.csv").write_text("hello", encoding="utf-8")
    (tmp_path / "data.db").write_bytes(b"x" * 100)  # 数据库不计入
    size = bridge.dispatch("get-cache-size", [{}])
    assert isinstance(size, int) and size == 5


# ── AI 生成文案 ──────────────────────────────────────────────

class _FakeAI:
    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: list[tuple] = []

    def _call_llm(self, prompt: str, system: str = "", timeout: int | None = None) -> str:
        self.calls.append((prompt, system, timeout))
        return self._payload


def test_work_publish_generate(tmp_db, monkeypatch):
    import app.core.dependencies as deps

    fake = _FakeAI(json.dumps([
        {"title": "智能体搭建第一步", "description": "先聊清楚业务流程再动手，效率翻倍。"},
        {"title": "别急着搭智能体", "description": "需求拆不清，工作流就是空中楼阁。"},
    ], ensure_ascii=False))
    monkeypatch.setattr(deps, "get_ai_service", lambda: fake)
    out = bridge.dispatch("work-publish-generate", [
        {"seedTitle": "AI智能体", "seedDesc": "面向装修行业获客", "variation": 50,
         "mediaType": "video", "count": 2}])
    assert out["success"] and len(out["items"]) == 2
    assert out["items"][0]["title"] == "智能体搭建第一步"
    assert "workPublishQuota" in out


def test_work_publish_generate_truncates_image_title(tmp_db, monkeypatch):
    import app.core.dependencies as deps

    long_title = "这是一个超过二十个字的图文标题用来测试截断逻辑是否生效"  # 27 字
    fake = _FakeAI(json.dumps([{"title": long_title, "description": "描述"}], ensure_ascii=False))
    monkeypatch.setattr(deps, "get_ai_service", lambda: fake)
    out = bridge.dispatch("work-publish-generate", [
        {"seedTitle": "主题", "seedDesc": "", "variation": 0, "mediaType": "image", "count": 1}])
    assert out["success"]
    assert len(out["items"][0]["title"]) == 20


def test_work_publish_generate_requires_topic(tmp_db):
    out = bridge.dispatch("work-publish-generate", [
        {"seedTitle": "", "seedDesc": "", "mediaType": "video", "count": 1}])
    assert not out["success"] and "主题" in out["error"]


def test_work_publish_generate_count_mismatch_raises(tmp_db, monkeypatch):
    import app.core.dependencies as deps

    fake = _FakeAI(json.dumps([{"title": "只有一条", "description": "描述"}], ensure_ascii=False))
    monkeypatch.setattr(deps, "get_ai_service", lambda: fake)
    with pytest.raises(ValueError):
        bridge.dispatch("work-publish-generate", [
            {"seedTitle": "主题", "seedDesc": "要求", "mediaType": "video", "count": 3}])
