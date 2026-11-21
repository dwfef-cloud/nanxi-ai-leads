"""Isolated desktop acceptance host: no business routers, scheduler or live agent."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.offline_runs import router

app = FastAPI(title="阿里山AI获客 · 离线测试")
app.include_router(router)
app.mount("/ui", StaticFiles(directory=Path(__file__).resolve().parent.parent / "ui"), name="offline-ui")


@app.get("/")
def home():
    return RedirectResponse("/ui/offline.html")
