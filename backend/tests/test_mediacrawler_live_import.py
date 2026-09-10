from app.integrations.mediacrawler_importer import adapt_douyin_comments


def test_douyin_comment_records_become_deduplicable_leads() -> None:
    leads = adapt_douyin_comments(
        [
            {
                "comment_id": "comment-1",
                "aweme_id": "video-9",
                "nickname": "咨询用户",
                "content": "这个方案怎么报价？",
            },
            {"comment_id": "comment-2", "content": ""},
        ],
        keyword="装修",
        task_id="task-1",
    )

    assert len(leads) == 1
    assert leads[0].external_id == "douyin-comment-comment-1"
    assert leads[0].source_url == "https://www.douyin.com/video/video-9"
    assert leads[0].note == "这个方案怎么报价？"
