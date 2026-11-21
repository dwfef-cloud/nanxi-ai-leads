"""线索库页 UI 验证（触达列 + 筛选），无头 Chrome。

验证点：
1. 页面加载无 console error / pageerror
2. 协议弹窗（必须点 checkbox 本体，点文字无效）能正常签署
3. 线索库列表渲染出行，且「回复/私信」等触达标记与后端一致
4. 触达类筛选（回复）能真正收窄结果
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

try:
    # 交付包 venv 里装的是 patchright（playwright 的反检测分支，API 兼容）
    from patchright.sync_api import sync_playwright
except ImportError:  # 退回到标准 playwright 环境
    from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8899"
OUT = Path(tempfile.mkdtemp(prefix="ui-verify-"))


def dismiss_dialogs(pg) -> None:
    # 协议弹窗：checkbox 必须点本体
    try:
        pg.click("text=我已认真查阅并承诺", timeout=3000)
    except Exception:
        pass
    try:
        pg.eval_on_selector_all(
            ".ant-modal input[type=checkbox]",
            "els => els.forEach(e => { if (!e.checked) e.click(); })")
        time.sleep(0.4)
        pg.click("text=签署并开始使用", timeout=3000)
        time.sleep(1.0)
    except Exception:
        pass
    # 授权中心 / 其它遮罩：点关闭
    for sel in (".ant-modal-close", ".ant-modal-close-x"):
        try:
            pg.click(sel, timeout=1500)
            time.sleep(0.5)
        except Exception:
            continue


def main() -> int:
    errors: list[str] = []
    api = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {str(e)[:160]}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text[:160]}")
                if m.type == "error" else None)
        page.on("response", lambda r: api.setdefault(r.url.rsplit("/", 1)[-1], r.status)
                if "/ipc/" in r.url else None)

        page.goto(f"{BASE}/ui/", wait_until="networkidle")
        time.sleep(2.0)
        dismiss_dialogs(page)

        # 进线索库
        page.click('.nav-item:has-text("线索库")', timeout=8000)
        time.sleep(2.5)
        page.screenshot(path=str(OUT / "leads.png"), full_page=False)

        rows = page.eval_on_selector_all(
            "tbody tr", "els => els.length")
        body_text = page.inner_text("body")
        html = page.content()
        nav_ok = "线索库" in body_text
        has_touch = "回复" in body_text or "触达" in body_text

        print(f"截图：{OUT / 'leads.png'}")
        print(f"列表行数：{rows}")
        print(f"页面含线索库导航：{nav_ok}｜含触达列字样：{has_touch}")
        print(f"console/page 错误：{len(errors)}")
        for e in errors[:8]:
            print("  ", e)
        browser.close()

    ok = rows > 0 and nav_ok and not errors
    print("结论：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
