from app.core import mediacrawler_runtime


def test_autostart_is_skipped_when_media_crawler_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(mediacrawler_runtime, "media_crawler_is_ready", lambda: True)

    assert mediacrawler_runtime.start_media_crawler() is None
