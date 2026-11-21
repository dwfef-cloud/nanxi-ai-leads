"""后台控制台 UI 验收（无头 Chrome）：登录 → 各页渲染 → 生成密钥 → 清理。

用法（工作目录 = backend/）：
    .venv/Scripts/python.exe tools/verify_admin.py [--base http://127.0.0.1:8899]

产物：系统临时目录下的 admin-*.png，控制台打印每页行数/报错统计。
退出码：0 = 全部通过；1 = 有 console 报错或页面异常。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

try:  # 包内 venv 装的是 patchright，兼容 playwright
    from patchright.sync_api import sync_playwright  # type: ignore
except ImportError:  # pragma: no cover
    from playwright.sync_api import sync_playwright  # type: ignore

BACKEND = Path(__file__).resolve().parents[1]


def read_password() -> str:
    env = os.getenv("ADMIN_PASSWORD", "").strip()
    if env:
        return env
    f = BACKEND.parent / "data" / "admin_initial_password.txt"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "口令" in line and "：" in line:
                return line.split("：", 1)[1].strip()
    raise SystemExit("找不到后台口令：请设置 ADMIN_PASSWORD 或确认已初始化")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8899")
    args = ap.parse_args()
    pw = read_password()
    out = Path(tempfile.mkdtemp(prefix="admin-verify-"))
    errors: list[str] = []
    bad: list[tuple[int, str]] = []
    shots: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 940})
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: bad.append((r.status, r.url)) if r.status >= 400 else None)

        page.goto(args.base + "/admin/", wait_until="networkidle")
        page.fill("#password", pw)
        page.click("#loginBtn")
        page.wait_for_selector("#appView:not([hidden])", timeout=8000)
        time.sleep(1.2)

        def shot(name: str) -> None:
            f = out / f"{name}.png"
            page.screenshot(path=str(f), full_page=False)
            shots.append(str(f))

        # 概览
        kpis = page.locator("#kpis .kpi").count()
        charts = page.locator("#trendChart svg").count()
        print(f"概览: KPI {kpis} 张, 趋势图 svg {charts} 个")
        shot("1-overview")

        # 密钥管理
        page.click('.nav-item[data-view="licenses"]')
        page.wait_for_selector("#licTable table, #licTable .empty", timeout=8000)
        time.sleep(0.6)
        print("密钥管理:", page.locator("#licTotal").inner_text())
        shot("2-licenses")

        # 生成 2 把（用完删掉，避免留垃圾数据）
        page.click("#licNewBtn")
        page.wait_for_selector("#gPlan", timeout=5000)
        page.select_option("#gPlan", "pro")
        page.fill("#gCount", "2")
        page.fill("#gDays", "365")
        page.fill("#gQuota", "500")
        page.fill("#gName", "UI 自检客户")
        page.click("#gSubmit")
        page.wait_for_selector("#gCodes", timeout=8000)
        codes = [c.strip() for c in page.locator("#gCodes").inner_text().split("\n") if c.strip()]
        print("生成成功:", codes)
        shot("3-generate")
        page.click("#gDone")
        time.sleep(0.8)
        rows = page.locator("#licTable tbody tr").count()
        print("列表行数:", rows)
        shot("4-license-list")

        # 用户监控
        page.click('.nav-item[data-view="instances"]')
        page.wait_for_selector("#instTable table, #instTable .empty", timeout=8000)
        time.sleep(0.5)
        print("用户监控:", page.locator("#instKpis .kpi").count(), "张 KPI")
        shot("5-instances")

        # 用量
        page.click('.nav-item[data-view="usage"]')
        time.sleep(0.8)
        print("用量: 图", page.locator("#usageChart svg").count(), "个")
        shot("6-usage")

        # 日志 / 说明 / 设置
        for view, name in (("audit", "7-audit"), ("guide", "8-guide"), ("settings", "9-settings")):
            page.click(f'.nav-item[data-view="{view}"]')
            time.sleep(0.5)
            shot(name)

        # 亮色主题
        page.click("#themeBtn")
        page.click('.nav-item[data-view="overview"]')
        time.sleep(1)
        print("亮色主题:", page.evaluate("document.documentElement.dataset.theme"))
        shot("10-light")
        page.click("#themeBtn")

        browser.close()

        # 清理：删掉自检生成的密钥
        if codes:
            import json
            import urllib.request

            token = None
            req = urllib.request.Request(
                args.base + "/admin/api/login",
                data=json.dumps({"password": pw}).encode(),
                headers={"Content-Type": "application/json"},
            )
            token = json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]
            for code in codes:
                req = urllib.request.Request(
                    args.base + "/admin/api/licenses/" + code,
                    method="DELETE", headers={"X-Admin-Token": token},
                )
                urllib.request.urlopen(req, timeout=10).read()
            print("已清理自检密钥:", codes)

    print("截图目录:", out)
    for s in shots:
        print("  ", s)
    if bad:
        print("HTTP 4xx/5xx:", bad[:5])
    if errors:
        print("console 报错:", errors[:5])
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
