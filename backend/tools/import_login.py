"""把桌面壳里已有的抖音登录态导入独立引擎（patchright）的持久化 profile。

为什么需要这一步
----------------
独立引擎（patchright）用的是一个干净的 Chromium profile。抖音对**全新浏览器**
会给验证码中间页，所以它必须先有登录态才能采集。桌面壳（Electron）里已经有
用户自己扫码产生的会话，两者都是 Chromium，cookie 可以直接搬。

安全边界
--------
- 只在本机内存里读 cookie，写进本机 profile，不打印、不外传任何 cookie 值。
- 不修改桌面壳的数据，不代替用户登录，不碰账号密码。
- 拿到的登录态仅用于「读」：搜索、翻视频、读评论。

用法：
    .venv/Scripts/python.exe tools/import_login.py            # 导入并验证
    .venv/Scripts/python.exe tools/import_login.py --verify-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SHELL_DIR = Path.home() / "AppData/Roaming/nanxi-desktop-shell"
TARGET_PROFILE = DATA_DIR / "douyin" / "browser_profile"


def _find_cookie_db() -> Path | None:
    for rel in ("Network/Cookies", "Cookies"):
        path = SHELL_DIR / rel
        if path.is_file():
            return path
    return None


def load_shell_cookies() -> list[dict]:
    """从桌面壳的 Cookies 库读出 douyin 相关 cookie。

    Chromium 在新版本里把值放在 encrypted_value（AES-GCM + DPAPI），
    但本项目实测壳写入的是明文 value 列（sessionid 等关键项都在）。
    这里只读明文列；解密不了的项跳过并在返回里说明数量，绝不猜测。
    """
    db = _find_cookie_db()
    if db is None:
        return []
    tmp = DATA_DIR / "_cookie_snapshot.db"
    shutil.copy2(db, tmp)  # 壳可能正持有锁：读快照
    out: list[dict] = []
    skipped = 0
    con = sqlite3.connect(str(tmp))
    try:
        rows = con.execute(
            "SELECT host_key, name, value, path, expires_utc, is_secure, is_httponly "
            "FROM cookies WHERE host_key LIKE '%douyin%'").fetchall()
    finally:
        con.close()
    try:
        tmp.unlink()
    except OSError:
        pass

    for host, name, value, path, expires_utc, secure, httponly in rows:
        if not value:
            skipped += 1
            continue
        exp = int(expires_utc or 0)
        # Chromium: 微秒 since 1601-01-01
        unix = (exp - 11644473600000000) / 1_000_000 if exp else -1
        if 0 < unix < time.time():
            skipped += 1
            continue
        out.append({
            "name": str(name), "value": str(value), "domain": str(host),
            "path": str(path or "/"), "secure": bool(secure),
            "httpOnly": bool(httponly),
            "expires": int(unix) if unix > 0 else -1,
        })
    return out


def _report(cookies: list[dict]) -> None:
    names = {c["name"] for c in cookies}
    key = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt"}
    print(f"读到 douyin cookie {len(cookies)} 条；关键登录项命中 {len(names & key)}/4"
          f"（{'、'.join(sorted(names & key)) or '无'}）")


async def main(verify_only: bool, keyword: str, headless: bool = True) -> int:
    from patchright.async_api import async_playwright

    if verify_only:
        cookies = []
    else:
        cookies = load_shell_cookies()
        if not cookies:
            print("没有从桌面壳读到可用的 douyin cookie：请先在桌面应用里打开抖音并登录")
            return 2
        _report(cookies)
        TARGET_PROFILE.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(TARGET_PROFILE), headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1440, "height": 900})
        try:
            if cookies:
                await ctx.add_cookies(cookies)
                print(f"已写入 profile：{TARGET_PROFILE}")
            page = await ctx.new_page()
            url = f"https://www.douyin.com/search/{keyword}?type=video"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(6000)
            title = await page.title()
            links = await page.evaluate(
                "() => document.querySelectorAll('a[href*=\"/video/\"]').length")
            wall = await page.evaluate(
                "() => !!document.querySelector('#login-pannel, [data-e2e=\"login-guide\"], .login-mask')")
            print(f"验证：title={title[:40]!r} 视频卡片={links} 登录墙={wall}")
            if links:
                sample = await page.evaluate(
                    """() => {
                        const a = document.querySelector('a[href*="/video/"]');
                        const wrap = a.closest('li') || a.parentElement;
                        const user = wrap && wrap.querySelector('a[href*="/user/"]');
                        return {href: a.href,
                                author: user ? String(user.innerText).trim().slice(0, 20) : '',
                                text: (wrap ? wrap.innerText : '').replace(/\\s+/g,' ').slice(0, 90)};
                    }""")
                print("样例：", json.dumps(sample, ensure_ascii=False))
                return 0
            return 3
        finally:
            await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--keyword", default="AI客服")
    ap.add_argument("--headed", action="store_true", help="有头运行（部分站点对无头更敏感）")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.verify_only, args.keyword, not args.headed)))
