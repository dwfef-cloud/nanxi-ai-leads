"""抖音真机引擎登录 / 自检工具。

用法（在 backend 目录下）：
    .venv\\Scripts\\python.exe tools\\douyin_login.py            # 打开浏览器，扫码登录
    .venv\\Scripts\\python.exe tools\\douyin_login.py --status   # 只看引擎与登录状态

登录态落在 backend/data/douyin/ 里（随包可搬），登录一次长期有效。
扫码窗口需要人工操作：引擎不会、也无法绕过验证码。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import douyin_runner  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只查看状态，不打开浏览器")
    ap.add_argument("--timeout", type=float, default=300.0, help="等待扫码的秒数")
    args = ap.parse_args()

    st = douyin_runner.status()
    print(f"引擎可用：{st['available']}")
    print(f"已登录：{st['loggedIn']}")
    print(f"登录态目录：{st['profile']}")

    if args.status:
        return 0 if st["available"] else 1
    if not st["available"]:
        print("\n缺少 patchright：请先执行  .venv\\Scripts\\python.exe -m pip install patchright")
        return 1

    print("\n正在打开抖音登录页，请在弹出的浏览器里用抖音 App 扫码…")
    res = douyin_runner.collect("智能体", video_max=1, comment_limit=5,
                                timeout=max(args.timeout, 60))
    if res.get("ok"):
        print(f"登录并采集成功：{res.get('stats')}")
        print("后续任务会直接复用这份登录态。")
        return 0
    print(f"失败：{res.get('error') or res.get('note')}")
    print("若提示未登录，请确认扫码是否完成；二维码截图见登录态目录。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
