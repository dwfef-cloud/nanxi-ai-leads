"""前后端通道连通性全量探测。

用法（在 backend 目录）：
    .venv/Scripts/python.exe tools/probe_channels.py

安全边界：
- 把真库复制到临时目录，DATA_DIR / LEADS_DB_PATH 全部指向副本，探测过程零污染真库；
- 危险通道（会真起线程 / 改授权 / 装更新 / 清库）只做静态判定，不实际调用。

输出：
- tools/probe_report.json  机器可读明细
- 控制台打印分类摘要
"""
from __future__ import annotations

import collections
import glob
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
UI_ASSETS = BACKEND.parent / "ui" / "assets"
REAL_DB = BACKEND.parent / "data" / "lead_system.db"

# ---- 1. 隔离数据目录（必须在 import 业务模块之前） ----
tmp_dir = Path(tempfile.mkdtemp(prefix="probe_"))
shutil.copy(str(REAL_DB), str(tmp_dir / "lead_system.db"))
os.environ["DATA_DIR"] = str(tmp_dir)
os.environ["LEADS_DB_PATH"] = str(tmp_dir / "lead_system.db")
os.environ["EXPORT_DIR"] = str(tmp_dir / "exports")
os.environ["BACKUP_DIR"] = str(tmp_dir / "backups")
sys.path.insert(0, str(BACKEND))

from app.api import ipc_bridge as B  # noqa: E402
import bridge_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---- 2. 后端注册表 ----
REG: dict[str, list[str]] = {}
for kind in ("EXPLICIT", "EMPTY_CHANNELS", "BLOCKED_CHANNELS", "VIEW_ADAPTER_CHANNELS"):
    table = getattr(B, kind)
    for key in (table.keys() if hasattr(table, "keys") else table):
        REG.setdefault(key, []).append(kind)
RAW_ARGS = B._RAW_ARGS_CHANNELS
BLOCKED_PREFIXES = B._BLOCKED_PREFIXES

# ---- 3. 前端会调用的通道 ----
INVOKE_LITERAL = re.compile(r'invoke\(\s*"([a-zA-Z][\w.-]*)"')
INVOKE_TEMPLATE = re.compile(r"invoke\(\s*`([^`]{0,80})`")
KEBAB = re.compile(r'"([a-z][a-z0-9]*(?:-[a-z0-9]+)+)"')
# 命令式前缀：前端通道名一定以这些动词开头
VERB = re.compile(
    r"^(get|save|set|start|stop|init|release|claim|update|delete|consume|query|refresh|"
    r"activate|license|check|dev|issue|generate|run|open|reload|destroy|list|export|import|"
    r"send|reply|search|fetch|mark|reset|clear|cancel|pause|resume|toggle|upload|download|"
    r"report|test|verify|bind|unbind|apply|submit|switch|batch|count|runtime)-"
)
# CSS/类名后缀噪声
CSSY = re.compile(
    r"-(title|body|meta|footer|head|card|item|list|tabs|tab|tab-panel|tab-switch|actions|"
    r"panel|btn|bar|box|wrap|icon|text|label|line|row|field|lock|result|empty|url|section|"
    r"mark|circle|close|status|container|content|image|video|desc|info|tag|chip|group|grid|"
    r"cell|modal|dialog|drawer|popover|tooltip|hint|errors|area|btn-icon|wrapper|header|"
    r"inner|outer|main|left|right|top|bottom|name|avatar|count|time|date|size|type|value)$"
)
NOISE_EXACT = {
    "comment-item", "feed-comment-icon", "comment-level-1", "comment-username-1",
    "creator-micro", "douyin-web", "view-adapter", "iframe-embed", "live-preview",
    "auto-cache-cleanup", "manual-link-open-mode", "ui-zoom-factor",
    "hardware-acceleration", "use-system-proxy", "all-views-muted",
}

front: dict[str, set[str]] = collections.defaultdict(set)
templates: set[str] = set()
for path in glob.glob(str(UI_ASSETS / "*.js")):
    page = os.path.basename(path).split("-")[0]
    text = open(path, encoding="utf-8", errors="ignore").read()
    for name in INVOKE_LITERAL.findall(text):
        front[name].add(page)
    for tpl in INVOKE_TEMPLATE.findall(text):
        templates.add(tpl)
    for name in KEBAB.findall(text):
        if len(name) >= 6 and VERB.match(name) and not CSSY.search(name) and name not in NOISE_EXACT:
            front[name].add(page)

front_channels = set(front) - NOISE_EXACT

# 壳原生通道（desktop/main.js 的 handleViewChannel 已实现，不经后端，不算缺口）
SHELL_NATIVE = {
    "open-creator-view", "open-chat-view", "reload-creator-view", "reload-chat-view",
    "destroy-creator-view", "destroy-chat-view",
    "set-creator-view-attached", "set-chat-view-attached",
    "init-automation-view", "update-automation-bounds",
    "set-account-detail-mode", "set-views-visible",
}
front_channels -= SHELL_NATIVE

# 前端内部事件名 / 状态字段 / 样式类，不是后端通道（人工核对后固化）
UI_INTERNAL = {
    "batch-account-finished", "batch-advanced-settings", "batch-badge", "batch-block",
    "batch-item-start", "batch-proxy-fields", "batch-run-log", "batch-runtime-changed",
    "batch-schedule-account", "batch-schedule-intention", "batch-schedule-location",
    "batch-schedule-max-leads", "batch-schedule-source", "batch-summary",
    "batch-task-complete", "batch-task-progress", "batch-task-stopped",
    "batch-uniform-success-limit", "batch-follow-tasks-changed",
    "count-error", "dev-test-error", "dev-test-reply", "dev-test-thought", "dev-test-toggle",
    "download-error", "download-progress", "download-success", "import-selection-summary",
    "list-toolbar", "run-breakdown-hero", "run-breakdown-sub", "run-breakdown-who",
    "run-kind-pill", "run-skip-chips", "run-stat", "runtime-config-banner",
    "runtime-state-changed", "set-creator-view-theme", "stop-color", "update-available",
    "license-code", "test-message", "open-link",
}
front_channels -= UI_INTERNAL

# ---- 4. 危险通道：不实际调用 ----
DANGER = {
    "start-task", "stop-task", "activate", "license-reset", "license-revoke",
    "apply-update-and-exit", "run-installer", "start-download", "check-update",
    "clear-all-history", "clear-cache", "clear-collected-library",
    "issue-demo-license-code", "dev-test-persona-ai", "test-chat-message-webhook",
    "start-batch-action", "claim-next", "open-in-account",
}

# ---- 5. 逐通道实测 ----
rows: list[dict] = []


def classify(body: dict, channel: str) -> tuple[str, str]:
    """→ (状态, 说明)"""
    if body.get("success") is True:
        data = body.get("data")
        if isinstance(data, dict):
            inner_ok = data.get("success")
            text = str(data.get("msg") or data.get("message") or data.get("error") or "")
            if inner_ok is False and "未开通" in text:
                return "disabled", text[:120]
            if inner_ok is False and text:
                return "error", text[:160]
        if data in (None, [], {}, "") or (isinstance(data, list) and not data):
            return "empty", "返回空值"
        if isinstance(data, dict) and data.get("items") == [] and not any(
                v for k, v in data.items() if k not in {"items", "total", "page", "pageSize"}):
            return "empty", "空列表"
        return "ok", ""
    err = str(body.get("error") or "")
    if "未开通" in err or "disabled" in err.lower():
        return "disabled", err[:160]
    # 空参探测下的必填校验：属正常契约，不算 bug
    if any(tag in err for tag in ("缺少", "未指定", "必填", "不能为空")):
        return "param_required", err[:160]
    return "error", err[:200]


with TestClient(bridge_server.app) as client:
    for channel in sorted(front_channels | set(REG)):
        in_front = channel in front_channels
        kinds = REG.get(channel, [])
        row = {
            "channel": channel,
            "inFrontend": in_front,
            "pages": sorted(front.get(channel, [])),
            "registeredAs": kinds,
        }
        if channel in DANGER:
            row.update(status="skipped", detail="危险通道，未实际调用")
            rows.append(row)
            continue
        try:
            resp = client.post(f"/ipc/{channel}", json={"args": []})
            if resp.status_code != 200:
                row.update(status="http_error", detail=f"HTTP {resp.status_code}")
            else:
                status, detail = classify(resp.json(), channel)
                row.update(status=status, detail=detail)
        except Exception as exc:  # noqa: BLE001
            row.update(status="exception", detail=f"{type(exc).__name__}: {exc}"[:200])
        rows.append(row)

# ---- 6. 汇总 ----
groups = collections.defaultdict(list)
for row in rows:
    groups[row["status"]].append(row["channel"])

print("=" * 78)
print("通道探测结果（临时库，真库零改动）")
print("=" * 78)
for status in ("ok", "empty", "disabled", "param_required", "error", "exception", "http_error", "skipped"):
    names = groups.get(status, [])
    print(f"{status:14} {len(names):4}")

print("\n=== A. 前端会调、但后端没接通（用户点了会看到“未开通/空白/报错”） ===")
bad = [r for r in rows if r["inFrontend"] and r["status"] in
       ("disabled", "error", "exception", "http_error")]
print(f"  共 {len(bad)} 个")
for row in bad:
    print(f"  {row['channel']:44} {row['status']:9} {row['detail'][:52]:52} ← {','.join(row['pages'])[:40]}")

print("\n=== B. 前端会调、返回空态（页面能开但没内容） ===")
blanks = [r for r in rows if r["inFrontend"] and r["status"] == "empty"]
print(f"  共 {len(blanks)} 个")
for row in blanks:
    print(f"  {row['channel']:44} ← {','.join(row['pages'])[:50]}")

print("\n=== C. 已登记但空参报错（需带参数，非 bug，列出确认契约） ===")
for row in rows:
    if row["status"] == "param_required":
        print(f"  {row['channel']:44} [{','.join(row['registeredAs'])}] {row['detail'][:60]}")

print("\n=== D. 后端登记 / 前端不调用 ===")
for row in rows:
    if row["registeredAs"] and not row["inFrontend"]:
        print(f"  {row['channel']:44} [{','.join(row['registeredAs'])}] {row['status']}")

print("\n--- 动态拼接的 invoke（静态分析无法展开） ---")
for tpl in sorted(templates):
    print("  `" + tpl + "`")

out = BACKEND / "tools" / "probe_report.json"
out.write_text(json.dumps({
    "summary": {k: len(v) for k, v in groups.items()},
    "templates": sorted(templates),
    "rows": rows,
}, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n明细 → {out}")
shutil.rmtree(tmp_dir, ignore_errors=True)
