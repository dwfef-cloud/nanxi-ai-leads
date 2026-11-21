"""独立引擎连通性检查（不依赖桌面壳）。

用途：确认 patchright 能启动、能打开抖音搜索页、能读到视频卡片，
以及当前 profile 的登录状态。真实采集前先跑这个，避免把「引擎问题」
误判成「抖音没内容」。

    .venv/Scripts/python.exe tools/engine_check.py --keyword AI客服
"""
from __future__ import annotations

import argparse
import asyncio
import sys


async def main(keyword: str, headless: bool) -> int:
    from patchright.async_api import async_playwright

    url = f"https://www.douyin.com/search/{keyword}?type=video"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(6000)
            title = await page.title()
            links = await page.evaluate(
                "() => document.querySelectorAll('a[href*=\"/video/\"]').length")
            wall = await page.evaluate(
                "() => !!document.querySelector('#login-pannel, [data-e2e=\"login-guide\"], .login-mask')")
            body_len = await page.evaluate("() => (document.body.innerText || '').length")
            print(f"title={title[:70]!r}")
            print(f"videoLinks={links} loginWall={wall} bodyLen={body_len}")
            if links:
                sample = await page.evaluate(
                    """() => {
                        const a = document.querySelector('a[href*="/video/"]');
                        const wrap = a.closest('li') || a.parentElement;
                        return { href: a.href, text: (wrap ? wrap.innerText : '').slice(0, 120) };
                    }""")
                print("sample=", sample)
            return 0 if links else 2
        finally:
            await browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="AI客服")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.keyword, not args.headed)))
