from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

from app.integrations.mediacrawler_adapter import adapt_media_crawler_payload
from app.schemas.lead import LeadCreate


@dataclass(slots=True)
class CrawlJobResult:
    leads: list[LeadCreate]


class MediaCrawlerJob:
    def run(self, payload: dict) -> CrawlJobResult:
        source = self._load_source(payload)
        leads = [adapt_media_crawler_payload(item) for item in source]
        return CrawlJobResult(leads=leads)

    def _load_source(self, payload: dict) -> list[dict]:
        if "source" in payload and isinstance(payload["source"], list):
            return payload["source"]

        generated = self._generate_source(payload)
        if generated:
            return generated

        if "raw_text" in payload and payload["raw_text"]:
            return self._parse_text(payload["raw_text"])

        if "file_path" in payload and payload["file_path"]:
            content = Path(payload["file_path"]).read_text(encoding="utf-8")
            return self._parse_text(content)

        return []

    def _generate_source(self, payload: dict) -> list[dict]:
        keyword = str(payload.get("keyword", "")).strip()
        if not keyword:
            return []

        count = payload.get("count", 10)
        try:
            count_int = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count_int = 10

        source_mode = str(payload.get("source_mode", "search")).strip() or "search"
        tag = str(payload.get("tag", "")).strip()
        note = str(payload.get("note", "")).strip()

        generated: list[dict] = []
        for index in range(1, count_int + 1):
            generated.append(
                {
                    "nickname": f"{keyword}-{index}",
                    "source_url": f"https://www.douyin.com/search/{keyword}?slot={index}",
                    "source_keyword": keyword,
                    "platform": "douyin",
                    "external_id": f"{source_mode}-{keyword}-{index}",
                    "tags": [source_mode, tag] if tag else [source_mode],
                    "note": note or f"自动生成的{keyword}线索 #{index}",
                }
            )
        return generated

    def _parse_text(self, text: str) -> list[dict]:
        content = text.strip()
        if not content:
            return []
        if content.startswith("["):
            data = json.loads(content)
            return data if isinstance(data, list) else []
        return [json.loads(line) for line in content.splitlines() if line.strip()]
