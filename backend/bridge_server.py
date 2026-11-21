"""M0 同源服务：南溪现有 /api 路由 + 新 IPC 桥 + 阿里山 renderer 静态页。

刻意不复用 app.main:app —— 它的 lifespan 会拉起 MediaCrawler 子进程并触发
自动备份（DESIGN-DOC.md §0.5 合规边界）。这里只复用路由表 api_router，
因此 34 个现有路由全部保留，但没有任何启动副作用、不产生平台请求。

启动（工作目录 = backend/）：
    .venv/Scripts/python.exe -m uvicorn bridge_server:app --host 127.0.0.1 --port 8899

访问：
    http://127.0.0.1:8899/ui/            15 页面（真桥接）
    http://127.0.0.1:8899/ui/?demo=1     15 页面（原演示假数据，便于对照/回退）
    http://127.0.0.1:8899/ipc/health     桥接自检
    http://127.0.0.1:8899/api/health     南溪现有后端自检
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Body, FastAPI
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.admin import bootstrap as admin_bootstrap
from app.admin.api import router as admin_router
from app import scheduler
from app.api import license_local, profile_sync
from app.api.accounts_admin import router as accounts_admin_router
from app.api.execution_dashboard import execution_dashboard_router
from app.api.ipc_bridge import DB_PATH, RENDERER_DIR, router as ipc_router
from app.api.pipeline_api import pipeline_router
from app.api.router import api_router
from app.paths import EXPORT_DIR, ensure
from app.engagement.api import engagement_router
from app.engagement.pool_api import pool_router
from app.execution.api import execution_router
from app.api.offline_runs import router as offline_runs_router

app = FastAPI(title="Lead System + M0 Bridge", version="0.1.0+bridge")


@app.middleware("http")
async def _revalidate_desktop_assets(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/ui/"):
        # Extracted bundles retain their historical filenames during local fixes.
        # A fresh desktop navigation must revalidate them instead of running an
        # older cached task-start handler against the new backend.
        response.headers["Cache-Control"] = "no-cache"
    return response

# 现有能力：一个不改，全部 /api/* 保持可用
app.include_router(api_router)

# M0 新增：/ipc/health、/ipc/events、/ipc/{channel}
app.include_router(ipc_router)

# M1 编排器（D22）：/api/pipeline/* 全流水线 + 断点续跑
app.include_router(pipeline_router)

# 账号资料同步（对标阿里山：真实昵称回写 + 创作者中心每日快照）
app.include_router(profile_sync.router)

# W1 执行层（采集→筛选流水线）+ W2 互动层（点赞/评论 + 配额账本 + 账号租约）
app.include_router(execution_router)
app.include_router(offline_runs_router)
app.include_router(engagement_router)
# M6 账号池 v2（D23）：状态机 + 风控事件
app.include_router(pool_router)

# D26 管理面收尾：漏斗 / 合规页对接执行侧真实数据
app.include_router(execution_dashboard_router)

# 账号池管理（/accounts）：多选 / 全选 / 批量删除 / 状态复位。
# 壳是编译产物，塞不进复选框，这里给一个同 token 的轻量管理面。
app.include_router(accounts_admin_router)

# 授权与运营后台（/admin/api/*）：密钥签发 + 多用户使用监控。
# 与业务三库隔离，独立 admin_console.db；ADMIN_ENABLED=0 可整体关闭（交付客户时用）。
ADMIN_ENABLED = os.getenv("ADMIN_ENABLED", "1") != "0"
if ADMIN_ENABLED:
    app.include_router(admin_router)

# 前端静态产物（index.html 仅多加载一行 live-bridge.js，bundle/样式零改动）
# 注意：不给 /ui/ 加 COEP 头 —— 实测宿主带 COEP 会拦掉不带 CORP 的跨源 iframe
#（含 www.douyin.com / creator.douyin.com）；不带 COEP 时两个抖音页 iframe 均可加载。
if RENDERER_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(RENDERER_DIR), html=True), name="ui")

# 南溪管理面（工作台/线索池/互动/客户 + 漏斗/报表/合规）。
# 定位：辅助验证面，非交付主线——真正给用户用的界面是 /ui（阿里山壳）。
# 设置 LEGACY_MANAGE=0 可在正式交付时关闭本挂载。
_MANAGE_DIR = Path(__file__).resolve().parents[1] / "frontend"
if _MANAGE_DIR.is_dir() and os.getenv("LEGACY_MANAGE", "1") != "0":
    app.mount("/manage", StaticFiles(directory=str(_MANAGE_DIR), html=True), name="manage")

# 数据导出下载（export_service 把 CSV 落在 data/exports，返回 /exports/xxx）
_EXPORT_DIR = ensure(EXPORT_DIR)
app.mount("/exports", StaticFiles(directory=str(_EXPORT_DIR)), name="exports")

# 运营后台前端（/admin/）。先注册 /admin 的跳转，再挂静态目录——
# Starlette 按注册顺序匹配，admin_router 已在其前面注册，/admin/api/* 不会被静态目录截走。
if ADMIN_ENABLED:
    _ADMIN_WEB = Path(__file__).resolve().parent / "app" / "admin" / "web"
    if _ADMIN_WEB.is_dir():
        admin_bootstrap()

        @app.get("/admin", include_in_schema=False)
        def admin_redirect() -> RedirectResponse:
            return RedirectResponse(url="/admin/", status_code=307)

        app.mount("/admin", StaticFiles(directory=str(_ADMIN_WEB), html=True), name="admin")


# 账号池管理页静态资源（/accounts/）。路由已在上面注册，静态目录负责页面本身。
_ACCOUNTS_WEB = Path(__file__).resolve().parent / "app" / "api" / "accounts_web"
if _ACCOUNTS_WEB.is_dir():
    app.mount("/accounts", StaticFiles(directory=str(_ACCOUNTS_WEB), html=True),
              name="accounts")


@app.get("/")
def home() -> RedirectResponse:
    return RedirectResponse(url="/ui/", status_code=307)


@app.on_event("startup")
async def _tune_threadpool() -> None:
    """放大同步路由的线程池。

    为什么必须调：本服务同时跑两类同步路由——
      · 高频短请求：桌面壳每 1.2s 一次 /desktop/command（还要拿队列锁）
      · 长等待：采集/探针在 wait_result 里等壳回传，一次几十秒到几分钟
    anyio 默认只有 40 个 token。几个长等待一占，壳的轮询就开始排队；
    排队一久壳侧超时断开，服务端又要处理断开，越排越死——
    实测表现是「探针一发出去，连 /bridge-info 都 3s 超时」。
    放大到 200 后两类请求互不饿死。
    """
    try:
        import anyio.to_thread

        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 200
    except Exception:  # noqa: BLE001 拿不到就用默认值，不影响功能
        pass


@app.on_event("startup")
async def _reap_stale_runtime_runs() -> None:
    """启动时收尾上一个进程遗留的「运行中」记录。

    进程退出（关软件 / taskkill / 崩溃）不会写 ended_ms，runtime_runs 里那行
    就永远停在 running。页面据此显示「运行中」、执行历史堆僵尸记录，用户看到的
    是「养号卡住了」。启动即收尾，宁可写「已中断」也不留假运行。
    """
    try:
        from app.paths import DB_PATH
        from app.runtime import store

        n = store.reap_stale_runs(DB_PATH)
        if n:
            print(f"[bridge] 已收尾 {n} 条上次中断的运行记录（aborted）", flush=True)
    except Exception as exc:  # noqa: BLE001 收尾失败不能挡住服务启动
        print(f"[bridge] 运行记录收尾跳过：{exc}", flush=True)


@app.on_event("shutdown")
def _stop_comment_like_workers() -> None:
    from app.comment_likes import shutdown_all

    shutdown_all()


@app.on_event("startup")
async def _silence_connection_reset() -> None:
    """吞掉 Windows 上的 ConnectionResetError 噪声。

    为什么必须处理：客户端（桌面壳 / 验收脚本）超时或关闭窗口时主动断开，
    asyncio 的 proactor 在收尾阶段必然抛一次 ConnectionResetError，默认走
    loop 的异常处理器**打完整 traceback 到 stderr**。壳每 1.2s 轮询一次、
    断一次就打一屏，几秒内就把事件循环挤满 —— 之后所有请求（连 curl 测
    /bridge-info）全部超时，看起来像「后端挂了」，实际是被自己的日志拖死的。
    这类异常不是服务端故障，静默；其余异常照常打印。
    """
    import asyncio

    loop = asyncio.get_running_loop()
    quiet = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

    def handler(loop_: "asyncio.AbstractEventLoop", context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, quiet):
            return
        loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)


# 构建产物里 index.html 引用了 /vite.svg 作为站点图标。渲染层保护产物不可改，
# 这里补一个同路径响应，避免浏览器控制台出现一条 404（验收线要求零 console 报错）。
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#111827"/>'
    '<path d="M9 21V11h4l3 6 3-6h4v10" stroke="#60A5FA" stroke-width="2.5"'
    ' fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


@app.get("/vite.svg", include_in_schema=False)
def favicon() -> Response:
    return Response(content=_FAVICON, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


# ---- 本地演示授权 REST 入口（与 /ipc/{channel} 等价，便于 CLI / 手工验收） ----
# 明确标注：非商业授权、不可分发、不解锁任何采集/自动化能力。
@app.get("/license/status")
def license_status() -> dict:
    return license_local.status()


@app.post("/license/demo-code")
def license_demo_code(payload: dict = Body(default={})) -> dict:
    hours = payload.get("ttlHours") or payload.get("ttl_hours")
    return license_local.issue_demo_code(
        ttl_hours=int(hours) if hours else None,
        note=str(payload.get("note") or ""),
    )


@app.post("/license/activate")
def license_activate(payload: dict = Body(default={})) -> dict:
    return license_local.activate(payload)


@app.post("/license/revoke")
def license_revoke(payload: dict = Body(default={})) -> dict:
    return license_local.revoke(payload)


@app.post("/license/reset")
def license_reset() -> dict:
    return license_local.reset()


@app.get("/bridge-info")
def bridge_info() -> dict:
    # rendererSource：builtin = 走随包的 ../ui/；external = 被 RENDERER_DIR 覆盖。
    # 排查「界面没出来」时先看这个字段，再看 rendererMounted。
    builtin = Path(RENDERER_DIR).resolve() == (Path(__file__).resolve().parent.parent / "ui").resolve()
    return {
        "mode": "live",
        "ui": "/ui/",
        "demo": "/ui/?demo=1",
        "ipcHealth": "/ipc/health",
        "apiHealth": "/api/health",
        "renderer": str(RENDERER_DIR),
        "rendererSource": "builtin" if builtin else "external",
        "rendererMounted": RENDERER_DIR.is_dir(),
        "mediaCrawlerAutostart": False,
        "scheduler": scheduler.status(DB_PATH)["running"],
        "note": "本服务不启动 MediaCrawler、不触发任何平台请求",
    }


# ---- 定时排期执行器 ----------------------------------------------------------
# 壳只负责算「下次 09:00」并展示，到点没有任何东西去消费它（见 app/scheduler.py
# 顶部说明）。这里拉起常驻 tick；SCHEDULER_OFF=1 可整体关闭（测试与验收沙箱用）。
# 非阻塞：起一个 daemon 线程，不参与 uvicorn 的启动/关闭流程，也不影响端口占用。
if scheduler.start(DB_PATH):
    _sched_interval = scheduler.DEFAULT_INTERVAL_S
    print(f"[scheduler] 定时排期执行器已启动（每 {_sched_interval:g}s 检查一次）")
else:
    print("[scheduler] 定时排期执行器未启动（SCHEDULER_OFF=1）")

# ---- 空闲自动清理缓存 --------------------------------------------------------
# 设置页「自动清理缓存（空闲时）」：低频检查，有任务在跑就跳过。
# 只清后端缓存目录（data/cache、data/tmp），不碰库 / 导出 / 备份 / 账号分区。
from app.api import app_settings as _app_settings  # noqa: E402 - 放在排期之后，避免启动顺序耦合

if _app_settings.start_auto_cleanup(DB_PATH):
    print(f"[cache] 空闲自动清理已启用（每 {_app_settings.AUTO_INTERVAL_S:g}s 检查一次）")
else:
    print("[cache] 空闲自动清理未启用（AUTO_CACHE_CLEANUP_OFF=1 或已启动）")
