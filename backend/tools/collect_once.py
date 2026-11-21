"""单次真机采集试跑（验收链路用，不建任务记录）。

用法
----
    .venv\\Scripts\\python.exe tools\\collect_once.py 装修 --videos 3
    .venv\\Scripts\\python.exe tools\\collect_once.py 装修 --videos 3 --ingest

前提：后端在 8899 运行，桌面壳已打开且抖音处于登录态。
默认只读不写库；加 --ingest 才把评论入库。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8899"


def _post(path: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(argv: list[str]) -> int:
    if not argv or argv[0].startswith("-"):
        print(__doc__)
        return 2
    keyword = argv[0]
    videos = 3
    ingest = False
    timeout = 150.0
    rest = argv[1:]
    for i, arg in enumerate(rest):
        if arg in {"--videos", "-v"} and i + 1 < len(rest):
            videos = int(rest[i + 1])
        elif arg == "--timeout" and i + 1 < len(rest):
            timeout = float(rest[i + 1])
        elif arg == "--ingest":
            ingest = True

    body = {"keyword": keyword, "videoMax": videos, "ingest": ingest,
            "timeout": max(20.0, timeout - 5)}
    print(f"→ 下发采集：关键词「{keyword}」最多 {videos} 条视频，"
          f"入库={'是' if ingest else '否'}，等待上限 {timeout:.0f}s …")
    try:
        result = _post("/desktop/collect-once", body, timeout=timeout + 15)
    except urllib.error.URLError as exc:
        print(f"✗ 连不上后端 8899：{exc.reason}")
        return 1
    except TimeoutError:
        print("✗ 请求超时：壳可能卡在验证码或页面加载")
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("success"):
        print(f"\n✓ 链路通：翻了 {result.get('stats', {}).get('videos', 0)} 条视频，"
              f"抓到 {result.get('commentCount', 0)} 条评论")
        for item in result.get("sample", []):
            print(f"  · {item.get('nickname')}：{item.get('text')}")
        return 0
    print(f"\n✗ 未通过：{result.get('error')}")
    diag = result.get("diag") or {}
    if diag:
        print(f"  诊断：{json.dumps(diag, ensure_ascii=False)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
