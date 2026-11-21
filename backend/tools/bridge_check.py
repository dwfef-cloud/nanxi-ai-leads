"""M0 前后端桥接 —— 交付包内一键入口（随包自包含）。

用法（在 nanxi-ai-leads/backend 目录下，用 venv 的 python）：
    python tools/bridge_check.py start      启动同源服务（前台，Ctrl+C 停止）
    python tools/bridge_check.py check      对已运行的 8899 做全通道契约验收（默认，含聚合视图）
    python tools/bridge_check.py channels   列出 demo-bridge 的全部通道及分类
    python tools/bridge_check.py demo-code   生成一枚本地演示码（默认 24h 有效）
    python tools/bridge_check.py activate <码>   用演示码激活，刷新页面即可看到状态变化
    python tools/bridge_check.py license    查看当前本地授权状态
    python tools/bridge_check.py revoke     吊销当前激活状态
    python tools/bridge_check.py reset-license   清空全部演示码与激活状态

路径全部相对本包解析（backend / ../ui），整包搬到任何位置都能直接跑；
需要指向别处的界面产物时用 RENDERER_DIR 覆盖。

注意：演示码为本地自用凭证，非商业授权、不可分发、不解锁任何采集/自动化能力。

页面：
    http://127.0.0.1:8899/ui/            真数据
    http://127.0.0.1:8899/ui/?demo=1     原演示假数据（对照/回退）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
VENV_PY = BACKEND / ".venv" / "Scripts" / "python.exe"
RENDERER = Path(os.getenv("RENDERER_DIR", str(BACKEND.parent / "ui")))
BASE = "http://127.0.0.1:8899"

BLOCKED_HINT = ("save-", "set-", "start-", "stop-", "open-", "destroy-", "run-", "update-")
# 浏览器环境无 Electron BrowserView：这些只做窗口状态管理，改由离线数据视图承载
VIEW_ADAPTER_HINT = frozenset({
    "open-creator-view", "open-chat-view", "reload-creator-view", "reload-chat-view",
    "update-creator-view-bounds", "update-chat-view-bounds",
    "set-creator-view-attached", "set-chat-view-attached", "set-views-visible",
    "set-message-center-session", "destroy-creator-view", "destroy-chat-view",
})


def _get(path: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(BASE + path, timeout=8) as r:
        return r.status, r.read()


def _post(channel: str, args: list | None = None) -> dict:
    req = urllib.request.Request(
        BASE + "/ipc/" + channel,
        data=json.dumps({"args": args or []}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def demo_channels() -> list[str]:
    text = (RENDERER / "demo-bridge.js").read_text(encoding="utf-8")
    return sorted(set(re.findall(r'^\s*"([a-z0-9][a-z0-9\-]{2,60})"\s*:', text, re.M)))


def cmd_start() -> int:
    print(f"[start] {BACKEND}\n[url ] {BASE}/ui/  (加 ?demo=1 回退演示数据)\n")
    return subprocess.run(
        [str(VENV_PY), "-m", "uvicorn", "bridge_server:app", "--host", "127.0.0.1", "--port", "8899"],
        cwd=str(BACKEND),
    ).returncode


def cmd_check() -> int:
    print("=== M0 桥接验收 ===")
    smoke = ["/bridge-info", "/ipc/health", "/api/health", "/ui/",
             "/ui/live-bridge.js", "/ui/bridge-views.js"]
    ok = True
    for path in smoke:
        try:
            status, body = _get(path)
            print(f"  GET {path:<26} {status}  {len(body)} B")
            ok &= status == 200
        except Exception as exc:
            print(f"  GET {path:<26} FAIL {exc}")
            ok = False
    try:
        _, page = _get("/ui/")
        ok &= (b"live-bridge.js" in page and b"bridge-views.js" in page
               and b"demo-bridge.js" in page)
    except Exception:
        ok = False

    channels = demo_channels()
    failed, disabled, uid_leaks = [], 0, []
    for ch in channels:
        try:
            packet = _post(ch, [{}])
            if packet.get("success") is not True:
                failed.append((ch, packet.get("error")))
                continue
            data = packet.get("data")
            if isinstance(data, dict) and data.get("msg") == "功能未开通":
                disabled += 1
            blob = json.dumps(data, ensure_ascii=False)
            for forbidden in ("comment_user_id", "video_id", "comment_id", "external_id", "douyinId"):
                if f'"{forbidden}"' in blob:
                    uid_leaks.append((ch, forbidden))
        except Exception as exc:
            failed.append((ch, str(exc)))

    print(f"\n  通道总数 {len(channels)} ｜ 应答成功 {len(channels) - len(failed)} ｜ 失败 {len(failed)}")
    print(f"  危险通道「功能未开通」 {disabled} 个 ｜ uid 字段泄漏 {len(uid_leaks)} 处")
    for ch, err in failed[:10]:
        print(f"    FAIL {ch}: {err}")
    for ch, field in uid_leaks[:10]:
        print(f"    LEAK {ch}: {field}")

    lead = _post("get-leads-page", [{"page": 1, "pageSize": 3}])["data"]
    print(f"\n  真实线索库：total={lead['total']}，本页 {len(lead['items'])} 条")
    print("  样例昵称：", [x["nickname"] for x in lead["items"][:3]])

    print("\n=== 聚合视图 / 离线视图 ===")
    views_ok = True
    try:
        dash = _post("get-account-data-dashboard")["data"]
        creator = _post("get-creator-aggregate", [{}])["data"]
        chat = _post("get-message-aggregate", [{"limit": 10}])["data"]
        unread = _post("get-chat-unread-state")["data"]
        print(f"  账号数据：{len(dash['days'])} 天 · 评论 {dash['totals']['comments']} · 账号 {len(dash['accounts'])}")
        print(f"  创作聚合：线索 {creator['totals']['leads']} · 视频 {len(creator['videos'])} · 任务 {len(creator['tasks'])}")
        print(f"  消息聚合：会话 {chat['stats']['threads']} · 消息 {chat['stats']['messages']} · 未读 {chat['stats']['unread']}")
        views_ok &= bool(dash["accounts"]) and bool(creator["videos"]) and bool(chat["threads"])
        views_ok &= unread["total"] == chat["stats"]["unread"]
        adapter = _post("open-creator-view", [{"accountId": "x"}])["data"]
        blocked = _post("generate", [{}])["data"]
        # 视图适配模式随方案演进而变：offline-panel（离线数据面板）→ iframe-embed
        # （浏览器内嵌抖音真实页）→ desktop-shell 下由主进程原生 BrowserView 接管。
        views_ok &= adapter.get("mode") in {"offline-panel", "iframe-embed"}
        views_ok &= adapter.get("success") is True
        views_ok &= blocked.get("msg") == "功能未开通"
        print(f"  窗口适配：{adapter.get('mode')} ｜ 危险通道：{blocked.get('msg')} ｜ 未读口径一致：{unread['total'] == chat['stats']['unread']}")
    except Exception as exc:
        views_ok = False
        print(f"  FAIL 聚合视图：{exc}")

    passed = ok and views_ok and not failed and not uid_leaks
    print("\n结论：", "PASS ✅" if passed else "FAIL ❌")
    return 0 if passed else 1


def cmd_channels() -> int:
    for ch in demo_channels():
        if ch in VIEW_ADAPTER_HINT:
            kind = "离线视图"
        elif ch.startswith(BLOCKED_HINT):
            kind = "禁用"
        else:
            kind = "真数据/空态"
        print(f"{kind:<12} {ch}")
    return 0


def _rest(path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def cmd_demo_code() -> int:
    ttl = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    r = _rest("/license/demo-code", {"ttlHours": ttl})
    print("=== 本地演示码（非商业授权 / 不可分发 / 不解锁任何能力）===")
    print(f"  授权码：{r['code']}")
    print(f"  有效期：{ttl} 小时，到期 {r['expiresAt']}")
    print(f"  激活：  python bridge.py activate {r['code']}")
    print(f"  提示：  {r['notice']}")
    return 0 if r.get("success") else 1


def cmd_activate() -> int:
    if len(sys.argv) < 3:
        print("用法：python bridge.py activate <授权码>")
        return 2
    r = _rest("/license/activate", {"code": sys.argv[2]})
    print("=== 激活结果 ===")
    print(f"  {r.get('msg')}")
    if r.get("success"):
        print(f"  授权状态：{r['licenseTierLabel']}")
        print(f"  凭证：    {r['savedCode']}")
        print(f"  解锁能力：{r['capabilitiesUnlocked']}（恒为空）")
        print(f"  到期：    {r.get('expireTime')}")
        print("\n  刷新 http://127.0.0.1:8899/ui/ 即可看到页面授权状态变化")
    return 0 if r.get("success") else 1


def cmd_license() -> int:
    _, raw = _get("/license/status")
    r = json.loads(raw)
    auth = r["auth"]
    print("=== 本地演示授权状态 ===")
    print(f"  已激活：{r['active']}  ｜  当前凭证：{r['activeCode'] or '（无）'}")
    print(f"  展示名称：{auth['licenseTierLabel']}")
    print(f"  校验标记：entitlementVerified={auth['entitlementVerified']}")
    print(f"  解锁能力：{auth['capabilitiesUnlocked']}  ｜  已签发码数：{r['codeCount']}")
    print(f"  说明：{auth['notice']}")
    return 0


def cmd_revoke() -> int:
    print(_rest("/license/revoke")["msg"])
    return 0


def cmd_reset_license() -> int:
    print(_rest("/license/reset")["msg"])
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    handlers = {
        "start": cmd_start, "check": cmd_check, "channels": cmd_channels,
        "demo-code": cmd_demo_code, "activate": cmd_activate, "license": cmd_license,
        "revoke": cmd_revoke, "reset-license": cmd_reset_license,
    }
    if action not in handlers:
        print(__doc__)
        sys.exit(2)
    try:
        sys.exit(handlers[action]())
    except urllib.error.URLError as exc:
        print(f"[bridge] 服务未启动：{exc}\n先执行 python bridge.py start")
        sys.exit(1)
