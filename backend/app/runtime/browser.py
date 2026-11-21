"""浏览器指令层：把「去平台上做一件事」收敛成一个入口。

两条引擎，同一份契约
--------------------
1. 桌面壳（首选）：指令进 /desktop/command 队列，壳在**已登录的 session** 里执行
   （隐藏 BrowserView），结果回 /desktop/result。登录态由用户自己扫码产生，
   后端永远不碰 cookie。
2. Playwright 兜底：本机有 patchright 且已登录过（data/douyin/browser_profile），
   就用独立浏览器跑同样的动作。没登录时直接返回失败原因，不去开一个登录墙页面。

为什么不在这里重试/兜底造假
--------------------------
采集类动作的重试放在 runner 层（按目标重试、并发上限在 registry）。
这里只做一次尝试，并把失败原因原样返回——「未登录」「登录墙」「超时」
「壳没加载执行器」这四种原因必须能被页面和日志看见，否则用户会以为采到了 0 条。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from app.api import desktop_commands

# 指令 → 建议超时（秒）。采集要翻视频读评论，比单页读取长。
TIMEOUTS = {
    "collect-comments": 420.0,
    "search-videos": 180.0,
    "recommend-scan": 300.0,   # 推荐页逐屏翻流找「评论数<200」的视频，逐屏有等待
    "read-source-videos": 240.0,
    # 人式翻阅（滚一段停一下、每三屏多停、再分段上翻）之后单条视频约 30s；
    # 一次读 6 条就 3 分钟，原来 300s 会在末尾被截断成「读不到」，这里放宽到 600s。
    "read-comments": 600.0,
    "inspect-video": 90.0,
    "inspect-profile": 90.0,
    "inspect-share-targets": 120.0,
    "inspect-comment-extras": 120.0,
    "watch-video": 180.0,
    "read-author-works": 180.0,
    "read-group-list": 180.0,
    "read-live-events": 240.0,
    "read-group-members": 240.0,
    "read-dm-conversations": 180.0,
    "read-dm-threads": 300.0,
    "inspect-live-ui": 240.0,
    "warmup-watch": 240.0,
}

# Writes require an explicit local signed scope and the desktop session.
# Payload flags cannot enable an action against a logged-in account.
OFFLINE_ONLY_ACTIONS = frozenset({
    "post-comment", "reply-comment", "send-dm", "send-dm-chat", "favorite-video", "copy-video-link",
    "like-video", "like-comment", "follow-user", "share-profile", "share-comment",
})


def status(db_path) -> dict:
    shell = desktop_commands.agent_alive()
    pw = False
    logged = False
    try:
        from app import douyin_runner
        pw = douyin_runner.available()
        logged = douyin_runner.logged_in()
    except Exception:  # noqa: BLE001 引擎缺依赖不影响壳路径
        pass
    engine = "shell" if shell else ("playwright" if (pw and logged) else "none")
    return {"shell": shell, "playwright": pw, "playwrightLoggedIn": logged,
            "engine": engine,
            "queued": desktop_commands.stats().get("queued", 0)}


def execute(cmd: dict, *, db_path, timeout: float | None = None,
            log: Callable[[str, str], None] | None = None,
            stop_check: Callable[[], bool] | None = None,
            account_id: str = "",
            engine: str = "") -> dict:
    """执行一条浏览器指令，返回 {ok, ...}；永不抛异常给调用方。

    account_id 是「用哪个账号的登录态去执行」：壳会按它路由到对应账号的常驻窗口
    （一个账号一个 persist partition）。不传时壳用默认账号，单账号场景行为不变。

    engine 是「本次运行开始时钉住的引擎」：一次运行中途不能换引擎。壳的心跳有
    30s 窗口，长指令跑着的时候心跳偶尔会断一下，如果每条指令都重新判引擎，就会
    在同一次养号里一半轮次跑在壳里（用户已登录的窗口）、一半跑在独立引擎里
    （另一个浏览器、另一份登录态）。日志里那种「独立引擎执行 warmup-watch：成功」
    夹在壳的日志中间就是这么来的 —— 浏览还算了数，点赞却落在别的会话里。
    """
    kind = str(cmd.get("type") or "").strip()
    if not kind:
        return {"ok": False, "error": "指令缺少 type"}
    if kind in OFFLINE_ONLY_ACTIONS:
        from app.execution.live_scope import command_allowed
        if not command_allowed(cmd, account_id):
            return {"ok": False, "code": "live_writes_disabled", "engine": "none",
                    "error": "此动作未包含在已配置的真机验收范围，真实执行保持关闭"}
        # Acceptance must use the account's Electron session, never a fallback profile.
        if engine not in {"", "shell"} or not desktop_commands.agent_alive():
            return {"ok": False, "code": "desktop_required", "engine": "none",
                    "error": "真机验收必须使用当前桌面账号会话"}
        engine = "shell"
    limit = TIMEOUTS.get(kind, 180.0)
    if kind == "watch-video":
        try:
            seconds = float(cmd.get("seconds", 0))
            if not 0 <= seconds <= 3600:
                raise ValueError
        except (ValueError, TypeError):
            return {"ok": False, "error": "视频停留秒数必须在0到3600之间"}
        limit = max(limit, seconds + 90)
    timeout = float(timeout or limit)

    current = engine if engine in {"shell", "playwright"} else status(db_path)["engine"]
    if current == "shell":
        return _via_shell(cmd, timeout=timeout, log=log, stop_check=stop_check,
                          account_id=account_id)
    if current == "playwright":
        return _via_playwright(cmd, timeout=timeout, log=log)
    return {
        "ok": False,
        "error": ("桌面壳未加载采集执行器：请确认「阿里山AI获客」桌面应用正在运行"
                  "（浏览器环境的页面没有登录态，无法采集）"),
        "engine": "none",
    }


def _via_shell(cmd: dict, *, timeout: float,
               log: Callable[[str, str], None] | None,
               stop_check: Callable[[], bool] | None,
               account_id: str = "") -> dict:
    payload = dict(cmd)
    # 账号随指令走：壳据此选「哪个账号的常驻窗口 + 哪个 session partition」。
    # 不注入的话多账号会全部落在默认登录态上，等于没有隔离。
    if account_id:
        payload.setdefault("accountId", str(account_id))
    payload.setdefault("ts", int(time.time() * 1000))
    cmd_id = desktop_commands.publish(payload)
    # 下发日志只做「看得懂的一条」：以前每条都是「已下发指令 warmup-watch（）」
    # —— 括号里空着、每轮刷一遍，5 分钟养号刷 5 条噪音，还把真正的进度挤走了。
    # 养号每轮的下发不再单独记（轮次结果里已经写清浏览/点赞/用时）。
    cmd_type = str(cmd.get("type") or "")
    if log and cmd_type != "warmup-watch":
        detail = (cmd.get("keyword") or cmd.get("videoUrl")
                  or (cmd.get("videoUrls") or [""])[0] or "")
        label = f"已下发指令 {cmd_type}"
        if detail:
            label += f"（{str(detail)[:60]}）"
        log(label + "，等待壳回传…", "normal")
    res = desktop_commands.wait_result(cmd_id, timeout=timeout, stop_check=stop_check)
    if res is None:
        if stop_check and stop_check():
            return {"ok": False, "error": "已手动停止", "engine": "shell", "stopped": True}
        return {"ok": False, "engine": "shell",
                "error": "等待壳回传超时：壳可能已被关闭，或页面加载卡住"}
    res = dict(res)
    res.setdefault("engine", "shell")
    return res


def _via_playwright(cmd: dict, *, timeout: float,
                    log: Callable[[str, str], None] | None) -> dict:
    from app import douyin_runner

    kind = str(cmd.get("type") or "")
    try:
        if kind in {"collect-comments", "search-videos"}:
            res = douyin_runner.search_videos(str(cmd.get("keyword") or ""),
                                              int(cmd.get("limit") or cmd.get("videoMax") or 8))
        elif kind == "read-comments":
            urls = cmd.get("videoUrls") or ([cmd["videoUrl"]] if cmd.get("videoUrl") else [])
            res = douyin_runner.read_comments(list(urls), int(cmd.get("limit") or 40))
        elif kind == "read-author-works":
            return {"ok": False, "engine": "playwright",
                    "error": "独立引擎暂不支持读取作者主页，请保持桌面壳在线"}
        elif kind == "warmup-watch":
            # 独立引擎的养号实现只有浏览，没有点赞脚本 —— 不能把 0 点赞
            # 归因成「概率没中」，必须在结果里带一句说明，日志才诚实。
            res = douyin_runner.warmup(int(cmd.get("scrolls") or 6),
                                       int(cmd.get("dwellMs") or 1800),
                                       int(cmd.get("likeMax") or 0))
            res = dict(res or {})
            res["detailLike"] = "engine-no-like"
            res.setdefault("note", "独立引擎不执行点赞，本轮只浏览")
        else:
            return {"ok": False, "engine": "playwright", "error": f"未知指令：{kind}"}
    except Exception as exc:  # noqa: BLE001 引擎异常一律转为失败原因
        return {"ok": False, "engine": "playwright",
                "error": f"{type(exc).__name__}: {exc}"}
    res = dict(res or {})
    res.setdefault("engine", "playwright")
    if log:
        log(f"独立引擎执行 {kind}：{'成功' if res.get('ok') else res.get('error')}", "normal")
    return res


def shell_available() -> bool:
    return desktop_commands.agent_alive()


def any_engine() -> bool:
    return status(None)["engine"] != "none"
