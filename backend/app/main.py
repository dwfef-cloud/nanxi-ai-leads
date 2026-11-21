from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from app.api.router import api_router
from app.core.api_stats import register_api_stats
from app.core.mediacrawler_runtime import start_media_crawler
from app.core.dependencies import get_backup_service
from app.paths import EXPORT_DIR, ensure

ROOT = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.media_crawler_process = start_media_crawler()
    # 自动备份启动检查（只追加，不修改现有逻辑）
    try:
        backup_service = get_backup_service()
        backup_service.auto_backup_check()
    except Exception as e:
        print(f"[main] 自动备份启动检查失败（不影响启动）: {e}")
    yield
    process = app.state.media_crawler_process
    if process is not None and process.poll() is None:
        process.terminate()


def create_app() -> FastAPI:
    app = FastAPI(title="Douyin Lead System", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # P4-C：API 统计中间件（内存计数，供 /api/monitor/api-stats 读取）
    register_api_stats(app)
    app.include_router(api_router)
    # 导出文件静态目录（随包在 <软件根>/data/exports，可用 EXPORT_DIR 覆盖）
    app.mount("/exports", StaticFiles(directory=str(ensure(EXPORT_DIR))), name="exports")

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse(
            url="http://127.0.0.1:8080/?v=4modules-20261107#/workbench",
            status_code=307,
        )

    return app


app = create_app()
