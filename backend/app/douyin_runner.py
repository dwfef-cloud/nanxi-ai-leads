"""真机引擎的同步门面：把异步的 DouyinController 收成一个可被任务线程调用的接口。

为什么要单独一层
----------------
DouyinController 是 asyncio 的，而任务执行器（task_runner）是普通线程。
这里起一个常驻事件循环线程，用 run_coroutine_threadsafe 串行化调用，
做到"一个浏览器实例、一次只跑一个动作"，避免多任务并发开窗把账号直接送进风控。
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

from app.douyin.browser import DATA_DIR
from app.paths import DATA_DIR as APP_DATA_DIR  # noqa: F401  (保持导入一致性)

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_LOOP_THREAD: threading.Thread | None = None
_CONTROLLER = None


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _LOOP, _LOOP_THREAD
    if _LOOP is not None and _LOOP.is_running():
        return _LOOP
    loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run, name="douyin-engine-loop", daemon=True)
    thread.start()
    _LOOP, _LOOP_THREAD = loop, thread
    return loop


def _call(coro, timeout: float = 180.0) -> Any:
    """在引擎事件循环里执行协程并等结果（同步阻塞调用方线程）。"""
    loop = _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout=timeout)


def _human_sleep(ms: int) -> "asyncio.Future":
    return asyncio.sleep(max(0.2, min(ms / 1000, 15.0)))


async def _get_controller():
    global _CONTROLLER
    from app.douyin.core import DouyinController

    if _CONTROLLER is None:
        _CONTROLLER = DouyinController(headless=False)
    await _CONTROLLER.initialize()
    return _CONTROLLER


def available() -> bool:
    """引擎依赖是否就绪（patchright 已安装即可，浏览器内核随包下载）。"""
    try:
        import patchright  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def profile_dir() -> Path:
    return DATA_DIR / "browser_profile"


def logged_in() -> bool:
    """是否已有可复用的登录态（有持久化 profile 或 storage.json）。"""
    prof = profile_dir()
    if prof.is_dir() and any(prof.iterdir()):
        return True
    return (DATA_DIR / "storage.json").is_file()


def engine_ready() -> bool:
    return available() and logged_in()


def collect(keyword: str, video_max: int = 6, comment_limit: int = 40,
            timeout: float = 600.0) -> dict:
    """真机采集：搜索关键词 → 逐个视频读评论。

    返回 {ok, comments:[{nickname,text,videoId,videoUrl}], stats:{videos}, note}；
    浏览器没登录/触发验证码等一律以 ok=False + error 说明，不抛给上层。
    """
    if not available():
        return {"ok": False, "error": "patchright 未安装，无法使用真机引擎"}
    if not logged_in():
        return {"ok": False, "error": "抖音未登录：请先运行 python tools/douyin_login.py 扫码登录一次"}
    kw = str(keyword or "").strip()
    if not kw:
        return {"ok": False, "error": "关键词为空"}

    async def _job() -> dict:
        try:
            ctrl = await _get_controller()
        except Exception as exc:  # 登录墙 / 浏览器启动失败
            return {"ok": False, "error": f"浏览器启动失败：{type(exc).__name__}: {exc}"}
        stats: dict = {"videos": 0, "keyword": kw}
        try:
            videos = await ctrl.search_videos(kw, limit=max(1, min(video_max, 30)))
            stats["found"] = len(videos)
            out: list[dict] = []
            seen: set[str] = set()
            for v in videos:
                url = str(v.get("url") or "")
                if not url:
                    continue
                stats["videos"] += 1
                comments = await ctrl.get_comments(url, limit=comment_limit)
                for c in comments:
                    text = str(c.get("content") or "").strip()
                    author = str(c.get("author") or "").strip()
                    if not text or len(text) < 2:
                        continue
                    key = f"{author}|{text}"
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "nickname": author or "未知用户",
                        "text": text,
                        "time": c.get("time") or "",
                        "videoId": str(v.get("video_id") or ""),
                        "videoUrl": url,
                    })
                if len(out) >= comment_limit * 3:
                    break
            stats["comments"] = len(out)
            note = "" if out else "视频页没读到评论：可能评论区未加载或选择器需按当前页面校准"
            return {"ok": True, "comments": out, "stats": stats, "note": note}
        except Exception as exc:  # 单次采集失败不拖垮任务
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stats": stats}

    try:
        return _call(_job(), timeout=timeout)
    except Exception as exc:  # 超时 / 事件循环异常
        return {"ok": False, "error": f"引擎超时或异常：{type(exc).__name__}: {exc}"}


def search_videos(keyword: str, limit: int = 8, timeout: float = 240.0) -> dict:
    """搜索关键词 → 视频 + 作者（线索采集的独立引擎路径）。"""
    if not available():
        return {"ok": False, "error": "patchright 未安装，无法使用独立引擎"}
    if not logged_in():
        return {"ok": False, "error": "抖音未登录：请先运行 python tools/douyin_login.py 扫码登录一次"}
    kw = str(keyword or "").strip()
    if not kw:
        return {"ok": False, "error": "关键词为空"}

    async def _job() -> dict:
        try:
            ctrl = await _get_controller()
            videos = await ctrl.search_videos(kw, limit=max(1, min(int(limit), 24)))
            items = []
            for v in videos:
                url = str(v.get("url") or "")
                m = url.rsplit("/", 1)[-1]
                items.append({
                    "videoId": str(v.get("video_id") or m or ""),
                    "url": url,
                    "author": str(v.get("author") or ""),
                    "title": str(v.get("title") or "")[:80],
                })
            return {"ok": True, "items": items, "keyword": kw}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        return _call(_job(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"引擎超时或异常：{type(exc).__name__}: {exc}"}


def read_comments(urls: list[str], limit: int = 40, timeout: float = 300.0) -> dict:
    """读取指定视频的评论（监控的独立引擎路径）。"""
    if not available():
        return {"ok": False, "error": "patchright 未安装，无法使用独立引擎"}
    if not logged_in():
        return {"ok": False, "error": "抖音未登录：请先运行 python tools/douyin_login.py 扫码登录一次"}
    targets = [str(u).strip() for u in (urls or []) if str(u or "").strip()]
    if not targets:
        return {"ok": False, "error": "没有可读取的视频地址"}

    async def _job() -> dict:
        out: list[dict] = []
        try:
            ctrl = await _get_controller()
            for url in targets[:12]:
                comments = await ctrl.get_comments(url, limit=limit)
                vid = url.rsplit("/", 1)[-1]
                for c in comments:
                    text = str(c.get("content") or "").strip()
                    if not text:
                        continue
                    out.append({"nickname": str(c.get("author") or "未知用户"),
                                "text": text, "videoId": vid, "videoUrl": url})
                if len(out) >= limit:
                    break
            return {"ok": True, "comments": out[:limit]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "comments": out}

    try:
        return _call(_job(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"引擎超时或异常：{type(exc).__name__}: {exc}"}


def warmup(scrolls: int = 6, dwell_ms: int = 1800, like_max: int = 0,
           timeout: float = 240.0) -> dict:
    """推荐页浏览（独立引擎）：只滚动与停留，点赞需壳内登录态，这里不做。"""
    if not available():
        return {"ok": False, "error": "patchright 未安装，无法使用独立引擎"}
    if not logged_in():
        return {"ok": False, "error": "抖音未登录：请先运行 python tools/douyin_login.py 扫码登录一次"}

    async def _job() -> dict:
        try:
            ctrl = await _get_controller()
            page = ctrl.page
            await page.goto("https://www.douyin.com/?recommend=1",
                            wait_until="domcontentloaded")
            actions = []
            for i in range(max(1, min(int(scrolls), 20))):
                await _human_sleep(dwell_ms)
                await ctrl._human_scroll("down")  # noqa: SLF001 复用真人滚动
                actions.append({"round": i + 1, "dwellMs": int(dwell_ms), "like": ""})
            return {"ok": True, "actions": actions, "liked": 0,
                    "note": "独立引擎只做浏览，不点赞（点赞走壳内登录态）"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        return _call(_job(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"引擎超时或异常：{type(exc).__name__}: {exc}"}


def reply(video_url: str, author: str, text: str, timeout: float = 300.0) -> dict:
    """在指定视频的评论区回复某作者（预留：灰度第二版启用）。"""
    if not engine_ready():
        return {"ok": False, "error": "引擎未就绪或未登录"}

    async def _job() -> dict:
        try:
            ctrl = await _get_controller()
            ok = await ctrl.reply_comment(video_url, author, text)
            return {"ok": bool(ok)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        return _call(_job(), timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"引擎超时或异常：{type(exc).__name__}: {exc}"}


def close() -> None:
    global _CONTROLLER
    if _CONTROLLER is None:
        return
    try:
        _call(_CONTROLLER.close(), timeout=30)
    except Exception:  # noqa: BLE001
        pass
    _CONTROLLER = None


def status() -> dict:
    return {
        "available": available(),
        "loggedIn": logged_in(),
        "profile": str(profile_dir()),
        "at": int(time.time()),
    }
