import threading
from pathlib import Path
from types import SimpleNamespace

from app.api import task_runner
from app.execution import leadgen_plan
from app.execution import leadgen_stream
from app.execution import live_workflow
from app.runtime import browser


BASE_CONFIG = {
    "videoSources": ["search"], "keywords": ["智能体开发"], "videoMax": 2,
    "interactionLimit": 3, "stayMin": 0, "stayMax": 0, "intervalMin": 0, "intervalMax": 0,
    "dmLimit": 0, "followLimit": 0, "profileShareLimit": 0,
}


def _run_stream(monkeypatch, tmp_path, *, config=None, items, processed=(),
                next_video=None, stop_after_waits=None, events=None,
                build_errors=None, logs=None, finish_state=None):
    """跑一次顺序任务到 build 为止，返回（壳收到的指令序列, 每个视频的指定地址）。

    next_video: 点「下一个」的返回值；不给就当作「页面上没有切换控件」，
    搜索模式应恢复原关键词结果页。
    """
    commands: list[dict] = []
    builds: list = []
    cfg = config or dict(BASE_CONFIG)
    next_videos = list(next_video) if isinstance(next_video, list) else None

    def execute(command, **kwargs):
        commands.append(command)
        if events is not None:
            events.append(("browser", command["type"]))
        kind = command["type"]
        if kind == "search-videos":
            return {"ok": True, "items": [({"url": item} if isinstance(item, str) else dict(item)) for item in items]}
        if kind == "open-video":
            return {"ok": True, "mode": "clicked-card"}
        if kind == "next-video":
            if next_videos is not None:
                return next_videos.pop(0) if next_videos else {
                    "ok": False, "error": "当前页没有「下一个」切换控件"}
            return next_video or {"ok": False, "error": "当前页没有「下一个」切换控件"}
        if kind == "read-comments":
            return {"ok": True, "comments": []}
        raise AssertionError(kind)

    def build(_db, payload, *_args, **_kwargs):
        builds.append(payload.get("specifiedUrls"))
        if build_errors:
            exc = build_errors.pop(0)
            if exc is not None:
                raise exc
        return {"actions": [], "planStats": {"selectedVideos": 0}}

    def finish(_ctx, status, stats, summary, reason=""):
        if finish_state is not None:
            finish_state.update(status=status, stats=dict(stats), summary=summary, reason=reason)

    ctx = SimpleNamespace(
        task_id="run", leadgen_id="task", account_id="account", payload={},
        db_path=tmp_path / "lead.db", stop=threading.Event(), pause=threading.Event(),
        stats=dict(task_runner.EMPTY_STATS), run_id="", started_at=0,
    )
    waits = []
    monkeypatch.setattr(browser, "execute", execute)
    monkeypatch.setattr(leadgen_stream, "already_processed",
                        lambda _db, _acc, url: url in set(processed))
    monkeypatch.setattr(live_workflow, "_account", lambda *_: {"id": "account", "nickname": "测试号"})
    monkeypatch.setattr(live_workflow, "_boundary", lambda *_: True)
    monkeypatch.setattr(live_workflow, "_watch", lambda *_a, **_k: {"ok": True})
    def wait_active(_ctx, seconds, **_kwargs):
        waits.append(seconds)
        if events is not None:
            events.append(("wait", seconds))
        if stop_after_waits is not None and len(waits) >= stop_after_waits:
            ctx.stop.set()
            return False
        return True
    monkeypatch.setattr(live_workflow, "_wait_active", wait_active)
    monkeypatch.setattr(leadgen_plan, "build", build)
    monkeypatch.setattr(leadgen_stream.settings, "normalize", lambda *_a, **_k: cfg)
    monkeypatch.setattr(task_runner, "_log", lambda _ctx, message, *_args: logs.append(message)
                        if logs is not None else None)
    monkeypatch.setattr(task_runner, "_action", lambda *_: None)
    monkeypatch.setattr(task_runner, "_finish", finish)
    monkeypatch.setattr(task_runner, "_emit", lambda *_a, **_k: None)
    monkeypatch.setattr(task_runner, "_emit_task", lambda *_a, **_k: None)
    monkeypatch.setattr(task_runner.event_bus, "publish", lambda *_a, **_k: None)

    leadgen_stream.run(ctx, {"mode": "stream", "runTaskId": "run", "keywords": ["智能体开发"],
                             "effectiveConfig": cfg})
    return commands, builds


def test_stream_watches_video_then_reads_comments_before_planning(monkeypatch, tmp_path):
    events = []
    url = "https://www.douyin.com/video/7400000000000000001"
    comments = [{"commentId": "123456789", "text": "想了解更多"}]
    config = {
        "videoSources": ["search"], "keywords": ["智能体开发"], "videoMax": 1,
        "interactionLimit": 1, "stayMin": 15, "stayMax": 30,
        "intervalMin": 0, "intervalMax": 0, "dmLimit": 0, "followLimit": 0,
        "profileShareLimit": 0, "enableMainVideoLike": True,
    }
    ctx = SimpleNamespace(
        task_id="run", leadgen_id="task", account_id="account", payload={},
        db_path=Path("unused.db"), stop=threading.Event(), pause=threading.Event(),
        stats=dict(task_runner.EMPTY_STATS), run_id="", started_at=0,
    )

    def execute(command, **kwargs):
        events.append(("browser", command["type"]))
        if command["type"] == "search-videos":
            return {"ok": True, "items": [{"url": url}]}
        if command["type"] == "open-video":
            assert command["videoUrl"] == url
            return {"ok": True, "mode": "clicked-card"}
        if command["type"] == "read-comments":
            return {"ok": True, "comments": comments}
        raise AssertionError(command["type"])

    def build(*args, comment_cache=None, **kwargs):
        events.append(("build", comment_cache))
        assert comment_cache[url]["comments"] == comments
        return {"actions": [], "planStats": {"selectedVideos": 1, "commentsRead": 1}}

    monkeypatch.setattr(browser, "execute", execute)
    monkeypatch.setattr(leadgen_stream, "already_processed", lambda *_: False)
    monkeypatch.setattr(live_workflow, "_account", lambda *_: {"id": "account", "nickname": "测试号"})
    monkeypatch.setattr(live_workflow, "_boundary", lambda *_: True)
    monkeypatch.setattr(live_workflow, "_watch",
                        lambda *_a, **_k: events.append(("watch", 15, 30)) or {"ok": True})
    monkeypatch.setattr(leadgen_plan, "build", build)
    monkeypatch.setattr(leadgen_stream.settings, "normalize", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(task_runner, "_log", lambda *_: None)
    monkeypatch.setattr(task_runner, "_action", lambda *_: None)
    monkeypatch.setattr(task_runner, "_finish", lambda *_: None)
    monkeypatch.setattr(task_runner, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_runner, "_emit_task", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(task_runner.event_bus, "publish", lambda *_args, **_kwargs: None)

    leadgen_stream.run(ctx, {"mode": "stream", "runTaskId": "run", "keywords": ["智能体开发"],
                             "effectiveConfig": config})

    # 搜完先点搜索结果里的封面进视频，再停留、再读评论：不再一上来就按地址加载
    assert [event[0] for event in events] == ["browser", "browser", "watch", "browser", "build"]
    assert events[0] == ("browser", "search-videos")
    assert events[1] == ("browser", "open-video")
    assert events[2] == ("watch", 15, 30)
    assert events[3] == ("browser", "read-comments")


def test_retryable_generation_failure_skips_one_video_and_continues(monkeypatch, tmp_path):
    from app.execution.live_leadgen import GenerationError

    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2)]
    monkeypatch.setattr(leadgen_stream.random, "sample", lambda seq, count: list(seq)[:count])
    logs = []
    commands, builds = _run_stream(
        monkeypatch, tmp_path, items=urls,
        config={**BASE_CONFIG, "videoMax": 2, "interactionLimit": 1},
        next_video={"ok": True, "videoUrl": urls[1], "title": "下一条"},
        build_errors=[GenerationError("模型连接失败", recoverable=True), None], logs=logs)

    assert len(builds) == 2
    assert builds[1] == urls[1]
    assert any("已跳过本条并继续下一条" in message for message in logs)
    assert commands[-1]["type"] == "read-comments"


def test_nonrecoverable_generation_failure_stops_before_next_video(monkeypatch, tmp_path):
    from app.execution.live_leadgen import GenerationError

    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2)]
    monkeypatch.setattr(leadgen_stream.random, "sample", lambda seq, count: list(seq)[:count])
    finish_state = {}
    commands, builds = _run_stream(
        monkeypatch, tmp_path, items=urls,
        config={**BASE_CONFIG, "videoMax": 2, "interactionLimit": 1},
        build_errors=[GenerationError("模型凭据无效", recoverable=False)],
        finish_state=finish_state)

    assert len(builds) == 1
    assert all(command["type"] != "next-video" for command in commands)
    assert finish_state["status"] == "failed"
    assert finish_state["reason"] == "stream_failed"
    assert finish_state["stats"]["generateFailed"] == 1
    assert "模型凭据无效" in finish_state["summary"]


def test_repeated_recoverable_generation_failures_open_circuit(monkeypatch, tmp_path):
    from app.execution.live_leadgen import GenerationError

    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in range(1, 5)]
    monkeypatch.setattr(leadgen_stream.random, "sample", lambda seq, count: list(seq)[:count])
    finish_state = {}
    commands, builds = _run_stream(
        monkeypatch, tmp_path, items=urls,
        config={**BASE_CONFIG, "videoMax": 4, "interactionLimit": 1},
        next_video=[{"ok": True, "videoUrl": url, "title": "下一条"} for url in urls[1:]],
        build_errors=[GenerationError("模型暂时不可用", recoverable=True) for _ in range(3)],
        finish_state=finish_state)

    assert len(builds) == leadgen_stream.GENERATION_FAIL_LIMIT
    assert finish_state["status"] == "failed"
    assert finish_state["stats"]["generateFailed"] == leadgen_stream.GENERATION_FAIL_LIMIT
    assert "连续 3 个视频 AI 生成失败" in finish_state["summary"]
    assert len([command for command in commands if command["type"] == "read-comments"]) == 3


def test_generation_failure_classification_distinguishes_retryable_from_configuration_errors():
    from urllib.error import HTTPError, URLError

    from app.execution.live_leadgen import _generation_failure_recoverable

    def http_error(code):
        return HTTPError("https://model.example/v1", code, "provider failure", {}, None)

    assert _generation_failure_recoverable(URLError("temporary network failure")) is True
    assert _generation_failure_recoverable(http_error(429)) is True
    assert _generation_failure_recoverable(http_error(503)) is True
    assert _generation_failure_recoverable(http_error(401)) is False
    assert _generation_failure_recoverable(http_error(403)) is False
    assert _generation_failure_recoverable(http_error(404)) is False
    assert _generation_failure_recoverable(RuntimeError("model credential is missing")) is False

    cloudflare = RuntimeError("provider rejected request")
    cloudflare.safe_reason = "Cloudflare error code 1010"
    cloudflare.__cause__ = http_error(403)
    assert _generation_failure_recoverable(cloudflare) is False


def test_stream_deep_fetches_pool_and_skips_processed_videos(monkeypatch, tmp_path):
    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2, 3)]
    monkeypatch.setattr(leadgen_stream.random, "sample", lambda seq, k: list(seq)[:k])
    commands, builds = _run_stream(monkeypatch, tmp_path, items=urls, processed=[urls[0]],
        next_video={"ok": True, "videoUrl": urls[2], "title": "下一条"})
    search = commands[0]
    # 深取：一次多读几倍候选，否则第二轮开始全是「已主评」
    assert search["limit"] == max(BASE_CONFIG["videoMax"] * leadgen_stream.DEEP_FETCH_RATIO, 24)
    # 已处理的那条不进候选，剩下随机抽 videoMax 条（抽取顺序即浏览顺序）
    assert sorted(builds) == sorted([urls[1], urls[2]])


def test_stream_keeps_searching_when_all_results_processed_until_user_stops(monkeypatch, tmp_path):
    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2)]
    commands, builds = _run_stream(monkeypatch, tmp_path, items=urls,
                                   processed=urls, stop_after_waits=2)
    assert [command["type"] for command in commands] == ["search-videos", "search-videos"]
    assert builds == []


def test_stream_continues_with_next_arrow_instead_of_reopening_url(monkeypatch, tmp_path):
    """看完一条点「下一个」接着看：第二条不重新按地址打开，也不会重复加载同一个视频。"""
    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2)]
    feed = "https://www.douyin.com/video/7400000000000000009"
    commands, builds = _run_stream(
        monkeypatch, tmp_path, items=urls,
        next_video={"ok": True, "urlChanged": True, "videoUrl": feed, "title": "下一条"})

    kinds = [c["type"] for c in commands]
    # 第一条点封面进；第二条点「下一个」，中间不再出现第二条 open-video
    assert kinds == ["search-videos", "open-video", "read-comments",
                     "next-video", "read-comments"]
    # 第一条是随机抽的（两条都可能排前），第二条固定是「下一个」切到的 feed
    assert len(builds) == 2 and builds[0] in urls and builds[1] == feed


def test_search_stream_restores_same_keyword_when_next_arrow_missing(monkeypatch, tmp_path):
    """关键词视频流无法原位切换时，重开同关键词结果并点击卡片，不跳详情地址。"""
    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2)]
    commands, builds = _run_stream(
        monkeypatch, tmp_path, items=urls,
        next_video={"ok": False, "error": "当前页没有「下一个」切换控件"})

    assert [c["type"] for c in commands] == [
        "search-videos", "open-video", "read-comments", "next-video",
        "search-videos", "open-video", "read-comments"]
    opened = [c for c in commands if c["type"] == "open-video"]
    assert opened[0]["videoUrl"] == builds[0]
    assert opened[1]["videoUrl"] == builds[1]
    assert opened[1]["preserveSearch"] is True
    assert len(builds) == 2 and builds[0] in urls and builds[1] in urls


def test_stream_accepts_next_switch_when_url_stays(monkeypatch, tmp_path):
    """真机：详情浮层点「下一个」不改地址，只要画面确实换了就认下一条（地址不变不算失败）。"""
    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2)]
    feed = "https://www.douyin.com/video/7400000000000000009"
    commands, builds = _run_stream(
        monkeypatch, tmp_path, items=urls,
        next_video={"ok": True, "urlChanged": False, "videoUrl": feed, "title": "下一条"})

    assert [c["type"] for c in commands] == ["search-videos", "open-video", "read-comments",
                                             "next-video", "read-comments"]
    assert len(builds) == 2 and builds[0] in urls and builds[1] == feed


def test_stream_picks_random_sample_from_fresh_results(monkeypatch, tmp_path):
    """不按排名取前 N 条（永远点第 1 条太规律）：从全部未处理结果里随机抽 videoMax 条。"""
    import random as _random
    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in range(1, 6)]
    calls: dict = {}

    def spy(seq, k):
        calls["pool"] = list(seq)
        calls["k"] = k
        return list(seq)[:k]

    monkeypatch.setattr(leadgen_stream.random, "sample", spy)
    commands, builds = _run_stream(
        monkeypatch, tmp_path, items=urls, config={**BASE_CONFIG, "videoMax": 2},
        next_video={"ok": True, "videoUrl": urls[2], "title": "下一条"})

    assert calls["k"] == 2
    # 抽样池 = 全部未处理结果（不是截断后的前几条）
    assert sorted(it["url"] for it in calls["pool"]) == sorted(urls)
    assert len(builds) == 2 and set(builds) <= set(urls)


def test_already_processed_reads_live_reservations(tmp_path):
    import hashlib
    import sqlite3

    db = tmp_path / "lead.db"
    url = "https://www.douyin.com/video/7400000000000000009"
    with sqlite3.connect(db) as con:
        con.execute("""CREATE TABLE write_reservations (namespace TEXT NOT NULL,
            target_hash TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL, reservation_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(namespace, target_hash))""")
        key = leadgen_stream.post_comment_key("account", url)
        con.execute("INSERT INTO write_reservations (namespace,target_hash,status,reason,updated_at)"
                    " VALUES ('live',?,?,?,0)",
                    (hashlib.sha256(key.encode()).hexdigest(), "sent_ok", ""))
    assert leadgen_stream.already_processed(db, "account", url) is True
    assert leadgen_stream.already_processed(db, "account", url + "0") is False
    assert leadgen_stream.already_processed(db, "other-account", url) is False


# ---------------- 推荐页单遍式（2026-11-20 重构） ----------------

FEED_URLS = [f"https://www.douyin.com/video/74000000000000000{i}" for i in range(1, 9)]

FEED_CONFIG = {
    "videoSources": ["recommend"], "keywords": [], "videoMax": 3,
    "interactionLimit": 5, "stayMin": 10, "stayMax": 20,
    "intervalMin": 30, "intervalMax": 60,
    "dmLimit": 0, "followLimit": 0, "profileShareLimit": 0,
}


def _run_feed(monkeypatch, tmp_path, *, feed_pages, next_videos=None,
              processed=(), build_actions=None, config=None,
              watch_log=None, classify=None, order_log=None,
              stop_after_waits=None):
    """跑一次推荐页单遍式任务，返回（壳收到的指令序列, build 的地址序列, 视频间隔等待秒数序列）。

    feed_pages: read-source-videos 每次调用的返回 items（按调用次序弹出）。
    next_videos: 点「下一个」的返回值序列；耗尽后一律视为失败。
    """
    cfg = config or dict(FEED_CONFIG)
    next_videos = list(next_videos or [])
    events: list[dict] = []
    builds: list = []

    def execute(command, **kwargs):
        events.append(command)
        kind = command["type"]
        if order_log is not None:
            order_log.append(kind)
        if kind == "read-source-videos":
            return {"ok": True, "items": feed_pages.pop(0) if feed_pages else []}
        if kind == "open-video":
            return {"ok": True, "mode": "clicked-card"}
        if kind == "next-video":
            if next_videos:
                return next_videos.pop(0)
            return {"ok": False, "error": "当前页没有「下一个」切换控件"}
        if kind == "read-comments":
            return {"ok": True, "comments": []}
        raise AssertionError(kind)

    def build(_db, payload, *_args, comment_cache=None, **_kwargs):
        builds.append(payload.get("specifiedUrls"))
        return {"actions": list(build_actions or []),
                "planStats": {"selectedVideos": 1}}

    ctx = SimpleNamespace(
        task_id="run", leadgen_id="task", account_id="account", payload={},
        db_path=tmp_path / "lead.db", stop=threading.Event(), pause=threading.Event(),
        stats=dict(task_runner.EMPTY_STATS), run_id="", started_at=0,
    )
    waits: list[int] = []

    monkeypatch.setattr(browser, "execute", execute)
    monkeypatch.setattr(leadgen_stream, "already_processed",
                        lambda _db, _acc, url: url in set(processed))
    monkeypatch.setattr(live_workflow, "_account", lambda *_: {"id": "account", "nickname": "测试号"})
    monkeypatch.setattr(live_workflow, "_boundary", lambda *_: True)
    monkeypatch.setattr(live_workflow, "_watch",
                        lambda _ctx, _a, _s, **_kwargs: (watch_log.append(_s) if watch_log is not None else None)
                        or {"ok": True})
    def wait_active(_ctx, seconds, **_kwargs):
        waits.append(seconds)
        if order_log is not None:
            order_log.append("wait")
        if stop_after_waits is not None and len(waits) >= stop_after_waits:
            ctx.stop.set()
            return False
        return True
    monkeypatch.setattr(live_workflow, "_wait_active", wait_active)
    if classify is not None:
        import app.execution.live_leadgen as live_leadgen
        monkeypatch.setattr(live_leadgen, "classify_intent", classify)
    monkeypatch.setattr(leadgen_plan, "build", build)
    monkeypatch.setattr(leadgen_stream.settings, "normalize", lambda *_a, **_k: cfg)
    for name in ("_log", "_action", "_finish", "_emit", "_emit_task"):
        monkeypatch.setattr(task_runner, name, lambda *_a, **_k: None)
    monkeypatch.setattr(task_runner.event_bus, "publish", lambda *_a, **_k: None)

    leadgen_stream.run(ctx, {"mode": "stream", "runTaskId": "run", "keywords": [],
                             "effectiveConfig": cfg})
    return events, builds, waits


def test_feed_entry_browses_in_place_without_prefetch(monkeypatch, tmp_path):
    """推荐页单遍式：进流只读当前一条（limit=1），随后逐条「下一个」——
    开头不再有把 videoMax 条候选一口气快刷完的采集阶段。"""
    pages = [[{"url": FEED_URLS[0], "title": "第一条"}]]
    nexts = [{"ok": True, "videoUrl": u, "title": f"第{i + 2}条"}
             for i, u in enumerate(FEED_URLS[1:3])]
    events, builds, _waits = _run_feed(monkeypatch, tmp_path, feed_pages=pages, next_videos=nexts)

    fetches = [c for c in events if c["type"] == "read-source-videos"]
    assert fetches[0]["limit"] == 1  # 进流只读一条，不再预抓满清单
    assert [c["type"] for c in events] == [
        "read-source-videos", "open-video", "read-comments",
        "next-video", "read-comments",
        "next-video", "read-comments",
    ]
    assert builds == FEED_URLS[:3]


def test_feed_swipes_past_processed_without_staying(monkeypatch, tmp_path):
    """「下一个」切到已处理过的视频：快速滑过，不停留、不读评论、不进计划。"""
    pages = [[{"url": FEED_URLS[0], "title": "第一条"}]]
    nexts = [{"ok": True, "videoUrl": u, "title": "t"} for u in FEED_URLS[1:4]]
    events, builds, _waits = _run_feed(monkeypatch, tmp_path, feed_pages=pages,
                                       next_videos=nexts, processed=[FEED_URLS[1]])
    kinds = [c["type"] for c in events]
    # 已处理那条夹在两条正常浏览之间：两个 next-video 连着，中间没有 read-comments
    assert kinds == [
        "read-source-videos", "open-video", "read-comments",
        "next-video", "next-video", "read-comments",
        "next-video", "read-comments",
    ]
    assert builds == [FEED_URLS[0], FEED_URLS[2], FEED_URLS[3]]


def test_feed_waits_between_videos_even_without_actions(monkeypatch, tmp_path):
    """AI 拒绝、一条动作都没产生的视频，同样按设置的间隔等待 ——
    旧实现只在动作后等待，拒绝跳过的视频 77 秒就切下一条。"""
    config = {**FEED_CONFIG, "videoMax": 2, "intervalMin": 180, "intervalMax": 300}
    pages = [[{"url": FEED_URLS[0], "title": "第一条"}]]
    nexts = [{"ok": True, "videoUrl": FEED_URLS[1], "title": "第二条"}]
    _events, _builds, waits = _run_feed(monkeypatch, tmp_path, feed_pages=pages,
                                        next_videos=nexts, config=config,
                                        build_actions=[])
    assert len(waits) == 1  # 最后一条之后不再等待
    assert 180 <= waits[0] <= 300


def test_feed_switches_video_before_waiting_between_videos(monkeypatch, tmp_path):
    """互动间隔应发生在已切换到的下一条视频上，避免界面长时间停在上一条。"""
    order = []
    pages = [[{"url": FEED_URLS[0], "title": "第一条"}]]
    nexts = [{"ok": True, "videoUrl": FEED_URLS[1], "title": "第二条"}]
    _run_feed(monkeypatch, tmp_path, feed_pages=pages, next_videos=nexts,
              config={**FEED_CONFIG, "videoMax": 2, "intervalMin": 180, "intervalMax": 300},
              order_log=order)

    assert order.index("next-video") < order.index("wait") < order.index("read-comments", order.index("next-video"))


def test_feed_retries_in_place_when_next_arrow_fails(monkeypatch, tmp_path):
    """持续恢复失败不自行结束；测试仅由模拟的用户停止结束任务。"""
    pages = [[{"url": FEED_URLS[0], "title": "第一条"}]]
    nexts = [{"ok": False, "error": "当前页没有「下一个」切换控件"} for _ in range(8)]
    events, builds, _waits = _run_feed(monkeypatch, tmp_path, feed_pages=pages,
        next_videos=nexts, config={**FEED_CONFIG, "videoMax": 2}, stop_after_waits=4)
    kinds = [c["type"] for c in events]
    assert kinds.count("next-video") >= 3
    assert kinds.count("read-source-videos") >= 2  # 第 3 次失败后回到所选信息流
    assert builds == [FEED_URLS[0]]
    assert all(c["type"] != "open-video" or c.get("videoUrl") == FEED_URLS[0] for c in events)


def test_feed_recovers_after_live_or_non_video_item(monkeypatch, tmp_path):
    """非视频卡片只触发恢复重试；探针确认下一条视频后正常读评论和计数。"""
    pages = [[{"url": FEED_URLS[0], "title": "第一条"}]]
    nexts = [
        {"ok": False, "nonVideo": True, "error": "当前是直播卡片"},
        {"ok": False, "nonVideo": True, "error": "仍未找到视频"},
        {"ok": False, "nonVideo": True, "error": "仍未找到视频"},
        {"ok": True, "recoveredFromNonVideo": True,
         "navigation": "non-video-recovery-wheel", "videoUrl": FEED_URLS[1], "title": "恢复后的第二条"},
    ]
    events, builds, _waits = _run_feed(monkeypatch, tmp_path, feed_pages=pages,
        next_videos=nexts, config={**FEED_CONFIG, "videoMax": 2})

    next_commands = [c for c in events if c["type"] == "next-video"]
    assert len(next_commands) == 4
    assert all(c.get("feedRecovery") is True for c in next_commands)
    assert [c["type"] for c in events] == [
        "read-source-videos", "open-video", "read-comments",
        "next-video", "next-video", "next-video", "read-source-videos",
        "next-video", "read-comments",
    ]
    assert builds == FEED_URLS[:2]


def test_persona_mismatch_swipes_after_preview_window(monkeypatch, tmp_path):
    """人设不符（用户 2026-11-20 口径）：只看预判窗口（3-8 秒）就划走——
    不读评论区、不进计划、不等互动间隔，像真人一样连续快滑。"""
    watches = []
    pages = [[{"url": FEED_URLS[0], "title": "剧情视频"}]]
    nexts = [{"ok": True, "videoUrl": u, "title": "下一条"} for u in FEED_URLS[1:4]]
    config = {**FEED_CONFIG, "enablePersonaVideoFilter": True, "aiReplyMode": "ai"}
    events, builds, waits = _run_feed(monkeypatch, tmp_path, feed_pages=pages,
        next_videos=nexts, config=config, watch_log=watches,
        classify=lambda *_a, **_k: {"matched": False, "reason": "剧情类内容与获客人设无关"})
    # 全程不读评论、不生成计划
    assert [c["type"] for c in events].count("read-comments") == 0
    assert builds == []
    # 每条只看预判窗口
    assert len(watches) == 3 and all(3 <= s <= 8 for s in watches)
    # 快滑之间只有 1-3 秒的小停顿，不等 180-300 秒的互动间隔
    assert waits and all(1 <= s <= 3 for s in waits)


def test_persona_match_keeps_full_flow(monkeypatch, tmp_path):
    """人设相符：预判窗口 + 剩余停留合计仍在设置的 10-20 秒区间，
    之后照常读评论、进计划。"""
    watches = []
    pages = [[{"url": FEED_URLS[0], "title": "获客相关视频"}]]
    nexts = [{"ok": True, "videoUrl": u, "title": "下一条"} for u in FEED_URLS[1:2]]
    config = {**FEED_CONFIG, "videoMax": 2,
              "enablePersonaVideoFilter": True, "aiReplyMode": "ai"}
    events, builds, _waits = _run_feed(monkeypatch, tmp_path, feed_pages=pages,
        next_videos=nexts, config=config, watch_log=watches,
        classify=lambda *_a, **_k: {"matched": True, "reason": "符合"})
    assert [c["type"] for c in events].count("read-comments") == 2
    assert builds == FEED_URLS[:2]
    # 相符视频分两段看：预判窗口 + 剩余停留，每条合计仍在设置的 10-20 秒区间
    assert len(watches) == 4
    assert all(10 <= watches[i] + watches[i + 1] <= 20 for i in (0, 2))


def test_hard_filter_swipes_quickly_without_ai_call(monkeypatch, tmp_path):
    """开屏快判（排除标题词/超时间窗）：3-8 秒划走，不调 AI、不读评论。"""
    watches = []
    ai_calls = []
    pages = [[{"url": FEED_URLS[0], "title": "剧情视频", "publishedText": "2020-01-01"}]]
    nexts = [{"ok": True, "videoUrl": u, "title": "剧情视频", "publishedText": "2020-01-01"}
             for u in FEED_URLS[1:4]]
    config = {**FEED_CONFIG, "recommendVideoTimeFilter": "3d",
              "enablePersonaVideoFilter": True, "aiReplyMode": "ai"}
    events, builds, waits = _run_feed(monkeypatch, tmp_path, feed_pages=pages,
        next_videos=nexts, config=config, watch_log=watches,
        classify=lambda *_a, **_k: ai_calls.append(1) or {"matched": True, "reason": ""})
    assert [c["type"] for c in events].count("read-comments") == 0
    assert builds == []
    assert watches and all(3 <= s <= 8 for s in watches)
    assert ai_calls == []  # 开屏快判是本地筛选，不花 AI 额度
    assert waits and all(1 <= s <= 3 for s in waits)


def test_wait_active_emits_and_clears_ephemeral_countdown(monkeypatch):
    events = []
    ctx = SimpleNamespace(
        stop=threading.Event(), pause=threading.Event(), task_id="run",
        leadgen_id="task", account_id="account",
    )
    monkeypatch.setattr(live_workflow, "_boundary", lambda _ctx: True)
    monkeypatch.setattr(task_runner, "_emit_task",
                        lambda _ctx, kind, **payload: events.append((kind, payload)))
    monkeypatch.setattr(task_runner, "_emit",
                        lambda kind, task_id, account_id, **payload:
                        events.append(("account-" + kind, {"taskId": task_id,
                            "accountId": account_id, **payload})))

    assert live_workflow._wait_active(
        ctx, 0.03, next_action="继续浏览下一条视频并读取评论")

    countdowns = [payload for kind, payload in events if kind == "countdown"]
    assert countdowns[0] == {
        "active": True,
        "action": "继续浏览下一条视频并读取评论",
        "remainingSeconds": 1,
        "paused": False,
    }
    assert countdowns[-1]["active"] is False
    account_countdowns = [payload for kind, payload in events if kind == "account-countdown"]
    assert account_countdowns[0]["taskId"] == "run"
    assert account_countdowns[0]["remainingSeconds"] == 1
    assert account_countdowns[-1]["active"] is False


def test_video_watch_publishes_dwell_countdown(monkeypatch):
    events = []
    ctx = SimpleNamespace(
        stop=threading.Event(), pause=threading.Event(), task_id="run",
        leadgen_id="task", account_id="account", db_path=None,
    )
    monkeypatch.setattr(live_workflow, "_boundary", lambda _ctx: True)
    monkeypatch.setattr(browser, "execute", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(task_runner, "_emit_task",
                        lambda _ctx, kind, **payload: events.append((kind, payload)))
    monkeypatch.setattr(task_runner, "_emit",
                        lambda kind, task_id, account_id, **payload:
                        events.append(("account-" + kind, {"taskId": task_id,
                            "accountId": account_id, **payload})))

    assert live_workflow._watch(
        ctx, {"videoUrl": "https://www.douyin.com/video/7400000000000000001"},
        15, next_action="当前视频停留结束后浏览评论区") == {"ok": True}

    countdowns = [payload for kind, payload in events if kind == "countdown"]
    assert countdowns[0]["action"] == "当前视频停留结束后浏览评论区"
    assert countdowns[0]["remainingSeconds"] == 15
    assert countdowns[0]["active"] is True
    assert countdowns[-1]["active"] is False
    account_countdowns = [payload for kind, payload in events if kind == "account-countdown"]
    assert account_countdowns[0]["remainingSeconds"] == 15
    assert account_countdowns[-1]["active"] is False


def test_ad_video_skips_without_dwell_or_comment_read(monkeypatch, tmp_path):
    ad_url = "https://www.douyin.com/video/7400000000000000008"
    normal_url = "https://www.douyin.com/video/7400000000000000009"
    config = {**BASE_CONFIG, "videoMax": 1, "interactionLimit": 1, "skipAdVideos": True}
    events = []
    monkeypatch.setattr(leadgen_stream.random, "sample", lambda seq, count: list(seq)[:count])
    commands, builds = _run_stream(monkeypatch, tmp_path, config=config,
        items=[{"url": ad_url, "isAd": True, "title": "品牌推广 广告"},
               {"url": normal_url, "title": "普通视频"}], events=events)
    kinds = [command["type"] for command in commands]
    assert kinds.count("read-comments") == 1
    assert kinds.count("open-video") == 1
    assert kinds.index("read-comments") > kinds.index("open-video")
    assert not any(kind == "wait" for kind, _ in events)
    assert not any(kind == "wait" for kind, _ in events)
    assert builds == [normal_url]


def test_search_interval_starts_after_next_video_is_confirmed(monkeypatch, tmp_path):
    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2)]
    events = []
    monkeypatch.setattr(leadgen_stream.random, "sample", lambda seq, count: list(seq)[:count])
    config = {**BASE_CONFIG, "videoMax": 2, "interactionLimit": 2,
              "intervalMin": 30, "intervalMax": 30}
    _run_stream(monkeypatch, tmp_path, config=config, items=urls,
        next_video={"ok": True, "videoUrl": urls[1], "title": "下一条"}, events=events)
    next_index = events.index(("browser", "next-video"))
    wait_index = events.index(("wait", 30))
    comments_index = events.index(("browser", "read-comments"), next_index + 1)
    assert next_index < wait_index < comments_index


def test_empty_comment_list_is_a_normal_video_result(monkeypatch, tmp_path):
    urls = [f"https://www.douyin.com/video/740000000000000000{i}" for i in (1, 2)]
    events = []
    monkeypatch.setattr(leadgen_stream.random, "sample", lambda seq, count: list(seq)[:count])
    commands, builds = _run_stream(monkeypatch, tmp_path,
        config={**BASE_CONFIG, "videoMax": 2, "interactionLimit": 2},
        items=urls, next_video={"ok": True, "videoUrl": urls[1], "title": "下一条"})
    assert [command["type"] for command in commands].count("read-comments") == 2
    assert len(builds) == 2
