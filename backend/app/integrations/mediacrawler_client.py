from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_ROOT = Path(r"D:\24\MediaCrawler-main (1)\MediaCrawler-main")


class MediaCrawlerUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class MediaCrawlerClient:
    base_url: str = os.getenv("MEDIA_CRAWLER_API_URL", "http://127.0.0.1:8080")
    project_root: Path = Path(os.getenv("MEDIA_CRAWLER_ROOT", str(DEFAULT_ROOT)))

    def start(self, payload: dict) -> dict:
        return self._request("POST", "/api/crawler/start", payload)

    def stop(self) -> dict:
        return self._request("POST", "/api/crawler/stop")

    def status(self) -> dict:
        return self._request("GET", "/api/crawler/status")

    def logs(self, limit: int = 100) -> list[dict]:
        return self._request("GET", f"/api/crawler/logs?limit={limit}").get("logs", [])

    def comments_since(self, started_at: str, keyword: str) -> list[dict]:
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            started = datetime.now(timezone.utc)
        comments_dir = self.project_root / "data" / "douyin" / "json"
        if not comments_dir.exists():
            return []

        records: list[dict] = []
        for path in comments_dir.glob("*_comments_*.json"):
            if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < started:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict):
                    records.append(item)
        return records

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url.rstrip('/')}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise MediaCrawlerUnavailable(f"MediaCrawler API 返回 {exc.code}: {detail}") from exc
        except (URLError, OSError) as exc:
            raise MediaCrawlerUnavailable(
                "MediaCrawler API 未启动。请先在 MediaCrawler 目录运行: uv run uvicorn api.main:app --port 8080"
            ) from exc
