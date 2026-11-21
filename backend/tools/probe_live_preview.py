"""实况大屏链路真机探针（只读事件队列，不抢壳的事件）。

用途：点「开始」后，大屏到底能不能拿到画面所需的 viewKeys / open-live-preview 事件。
输出：每个时间点 consume 通道返回 + 新增事件摘要 + 壳窗口创建日志。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8899"
BACKEND = Path(__file__).resolve().parents[1]
EVENTS_DB = Path(os.getenv("BRIDGE_EVENTS_DB", str(BACKEND.parent / "data" / "bridge_events.db")))


def call(channel: str, *args):
    body = json.dumps({"args": list(args)}).encode()
    req = urllib.request.Request(f"{BASE}/ipc/{channel}", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def max_event_id() -> int:
    con = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True, timeout=5)
    try:
        return int(con.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
    finally:
        con.close()


def new_events(after: int) -> list[tuple]:
    con = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(
            "SELECT id, channel, payload FROM events WHERE id > ? ORDER BY id", (after,)
        ).fetchall()
    finally:
        con.close()


def snapshot(tag: str) -> dict:
    out = {}
    for ch in ("consume-entity-leadgen-live-preview", "get-entity-leadgen-busy-account-ids"):
        try:
            out[ch] = call(ch, {})["data"]
        except Exception as exc:  # noqa: BLE001
            out[ch] = f"ERR {exc}"
    print(f"[{tag}] {json.dumps(out, ensure_ascii=False)[:500]}", flush=True)
    return out


def main() -> int:
    task_id = sys.argv[1] if len(sys.argv) > 1 else "ent_real_1794644472"
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    print("=== 启动前 ===")
    snapshot("before")
    base_id = max_event_id()
    print("event baseline id =", base_id)

    res = call("start-entity-leadgen-task-runtime", {"id": task_id})
    print("=== start 返回 ===")
    print(json.dumps(res, ensure_ascii=False)[:800], flush=True)

    deadline = time.time() + seconds
    seen: set[int] = set()
    step = 0
    while time.time() < deadline:
        step += 1
        time.sleep(3)
        snapshot(f"t+{step * 3}s")
        rows = new_events(base_id)
        for _id, channel, payload in rows:
            if _id in seen:
                continue
            seen.add(_id)
            try:
                p = json.loads(payload)
            except ValueError:
                p = {}
            brief = {k: p.get(k) for k in ("type", "status", "message", "accountId", "viewKeys") if k in p}
            print(f"   evt#{_id} {channel} {json.dumps(brief, ensure_ascii=False)[:220]}", flush=True)
        if rows:
            base_id = max(base_id, rows[-1][0])
        try:
            rt = call("get-runtime-status", {})["data"]
            if not rt.get("active"):
                print("   运行时已无活跃实例，提前结束", flush=True)
                break
        except Exception:  # noqa: BLE001
            pass

    print("=== 结束后 ===")
    snapshot("after")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
