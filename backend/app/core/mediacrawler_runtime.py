from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_ROOT = Path(r"D:\24\MediaCrawler-main (1)\MediaCrawler-main")


def media_crawler_is_ready() -> bool:
    try:
        with urlopen("http://127.0.0.1:8080/api/health", timeout=0.5) as response:
            return response.status == 200
    except (URLError, OSError):
        return False


def start_media_crawler() -> subprocess.Popen[str] | None:
    if os.getenv("MEDIA_CRAWLER_AUTOSTART", "true").lower() in {"0", "false", "no"}:
        return None
    if "PYTEST_CURRENT_TEST" in os.environ or media_crawler_is_ready():
        return None

    root = Path(os.getenv("MEDIA_CRAWLER_ROOT", str(DEFAULT_ROOT)))
    if not (root / "pyproject.toml").exists():
        return None

    cache_dir = Path(os.getenv("MEDIA_CRAWLER_UV_CACHE", str(Path.cwd() / ".cache" / "mediacrawler-uv")))
    return subprocess.Popen(
        ["uv", "run", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "8080"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "UV_CACHE_DIR": str(cache_dir)},
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )
