"""真机采集开关（灰度控制）。

用法（在 backend 目录下）：
    python tools/collector_switch.py status                # 看当前开关/引擎/登录态
    python tools/collector_switch.py on                    # 打开真机采集（点「开始」就真跑）
    python tools/collector_switch.py off                   # 关回本地模式（只处理已入库评论）
    python tools/collector_switch.py engine playwright     # 指定引擎（playwright / electron / auto）

真机采集当前灰度：只读不写（搜索 + 读评论），不点赞、不评论、不私信。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import task_runner  # noqa: E402
from app.paths import DB_PATH  # noqa: E402


def _set(key: str, value: str) -> None:
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS system_settings (category TEXT NOT NULL, "
            "key TEXT NOT NULL, value TEXT, updated_at TEXT, PRIMARY KEY (category, key))")
        con.execute(
            "INSERT INTO system_settings(category,key,value,updated_at) VALUES(?,?,?,datetime('now')) "
            "ON CONFLICT(category,key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            ("collector", key, value))
        con.commit()
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["status", "on", "off", "engine"])
    ap.add_argument("value", nargs="?", default="auto")
    args = ap.parse_args()

    if args.action == "on":
        _set("real", "1")
    elif args.action == "off":
        _set("real", "0")
    elif args.action == "engine":
        if args.value not in {"playwright", "electron", "auto"}:
            print("引擎只能是 playwright / electron / auto")
            return 2
        _set("engine", args.value)

    real = task_runner.real_mode_enabled(DB_PATH)
    engine = task_runner.real_engine(DB_PATH)
    try:
        from app import douyin_runner
        eng = douyin_runner.status()
    except Exception as exc:  # noqa: BLE001
        eng = {"available": False, "loggedIn": False, "profile": str(exc)}
    print(f"真机采集：{'开' if real else '关'}（关闭时只处理已入库评论）")
    print(f"引擎：{engine}")
    print(f"patchright：{'可用' if eng['available'] else '缺失'}；独立登录态：{'已登录' if eng['loggedIn'] else '未登录'}")
    print(f"登录态目录：{eng['profile']}")
    if real and not eng["loggedIn"]:
        print("提示：playwright 引擎需要先扫码登录（tools/douyin_login.py）；"
              "未登录时会自动退回桌面壳内已登录的视图。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
