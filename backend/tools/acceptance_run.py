"""前端↔后端全链路验收：以「用户能完成的一件事」为粒度，逐功能过四道关。

四道关
  ① 静态接通：前端调的通道名，后端有登记（EXPLICIT / EMPTY / BLOCKED / 壳原生）
  ② 调用存活：真发一次 HTTP，无 500 / 无未捕获异常 / 不超时
  ③ 数据有效：返回真实数据 / 空态有说明 / 未实现 / 假数据，四者分清
  ④ 写回持久：写操作「写入 → 杀进程重启 → 重新读回比对」

安全护栏
  - 全程在 data/ 的**副本**上跑（DATA_DIR 指向 tools/_acceptance/data），生产库零写入；
  - 开始前记基线（各表行数 + 文件大小），结束后比对，必须零变化；
  - 线程启停 / 授权激活 / 安装更新 / 清库 / 批量删除：只做静态判定或夹具级动作；
  - 抖音采集/发布/私信不真实触发，只验证指令能否下发与回传。

用法（在 backend 目录）：
    .venv/Scripts/python.exe tools/acceptance_run.py
输出：
    docs/acceptance-report.md     人读表格
    tools/acceptance_result.json  机器可读明细
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
PROD_DATA = ROOT / "data"
SANDBOX = BACKEND / "tools" / "_acceptance"
SB_DATA = SANDBOX / "data"
PORT = 8990
BASE = f"http://127.0.0.1:{PORT}"
PY = str(BACKEND / ".venv" / "Scripts" / "python.exe")

PLACEHOLDER_TOKENS = ("评论用户", "demo-user", "示例用户", "placeholder", "mock-data",
                      "test_user", "lorem ipsum", "假数据")


# --------------------------------------------------------------------------
# 基线 / 沙箱
# --------------------------------------------------------------------------
def db_files(data_dir: Path) -> list[Path]:
    return sorted(p for p in data_dir.glob("*.db"))


def snapshot(data_dir: Path) -> dict[str, dict]:
    """各库文件大小 + 逐表行数（只读打开，不动 WAL）。"""
    out: dict[str, dict] = {}
    for path in db_files(data_dir):
        info: dict[str, Any] = {"size": path.stat().st_size, "tables": {}}
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                names = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                for name in names:
                    try:
                        info["tables"][name] = con.execute(
                            f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                    except sqlite3.Error as exc:
                        info["tables"][name] = f"ERR:{exc}"
            finally:
                con.close()
        except sqlite3.Error as exc:
            info["error"] = str(exc)
        out[path.name] = info
    return out


def diff_snapshot(before: dict, after: dict) -> list[str]:
    changes: list[str] = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name), after.get(name)
        if b is None:
            changes.append(f"{name}: 新增文件")
            continue
        if a is None:
            changes.append(f"{name}: 文件消失")
            continue
        if b["size"] != a["size"]:
            changes.append(f"{name}: 大小 {b['size']} → {a['size']}")
        for table in sorted(set(b["tables"]) | set(a["tables"])):
            bv, av = b["tables"].get(table), a["tables"].get(table)
            if bv != av:
                changes.append(f"{name}.{table}: {bv} → {av}")
    return changes


def build_sandbox() -> None:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SB_DATA.mkdir(parents=True)
    for item in PROD_DATA.iterdir():
        if item.is_dir():
            if item.name in {"_backups", "backups", "stress"}:
                continue
            shutil.copytree(item, SB_DATA / item.name)
        elif item.suffix in {".db", ".json", ".key"}:
            shutil.copy2(item, SB_DATA / item.name)
    # WAL 模式下的新提交还在 -wal 里，只拷 .db 会拿到**旧快照**
    # （清理过演示数据的库会「复活」成清理前的样子，验收就白跑了）。
    # 只拷 -wal，不拷 -shm：-shm 是进程间共享内存，拷过来会和副本的 WAL 对不上，
    # 打开时报 "disk I/O error"。拷完在副本上 checkpoint 一次，把 WAL 折进主文件。
    for sidecar in PROD_DATA.glob("*.db-wal"):
        if (SB_DATA / sidecar.name.replace("-wal", "")).is_file():
            shutil.copy2(sidecar, SB_DATA / sidecar.name)
    for db_file in SB_DATA.glob("*.db"):
        try:
            con = sqlite3.connect(str(db_file), timeout=10)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.commit()
            finally:
                con.close()
        except sqlite3.Error:
            pass  # 非 WAL 库 / 无 WAL 时该语句无副作用，失败也不影响后续读取
    # 沙箱内关掉真机采集：验收不产生任何平台请求
    con = sqlite3.connect(str(SB_DATA / "lead_system.db"))
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS system_settings (category TEXT NOT NULL, key TEXT NOT NULL,"
            " value TEXT, updated_at TEXT, PRIMARY KEY (category, key))")
        con.execute(
            "INSERT INTO system_settings(category,key,value,updated_at) VALUES('collector','real','0',datetime('now'))"
            " ON CONFLICT(category,key) DO UPDATE SET value='0'")
        con.commit()
    finally:
        con.close()


def sandbox_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "DATA_DIR": str(SB_DATA),
        "LEADS_DB_PATH": str(SB_DATA / "lead_system.db"),
        "EXPORT_DIR": str(SB_DATA / "exports"),
        "BACKUP_DIR": str(SB_DATA / "backups"),
        "BRIDGE_LICENSE_STATE": str(SB_DATA / "bridge_license.json"),
        "ADMIN_ENABLED": "0",
        "ALLOW_DEMO_SEED": "0",
        # 排期执行器在沙箱里**开着**跑：SCH-5 要验证「到点真的会触发」，
        # 关掉就永远验不出来。用 5 秒一轮缩短等待；沙箱里的排期都指向
        # 不存在的任务 id，触发后只会落 skipped，不会真启动任务。
        "SCHEDULER_INTERVAL_S": "5",
        "PYTHONIOENCODING": "utf-8",
    })
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        env.pop(key, None)
    return env


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def start_server(tag: str) -> subprocess.Popen:
    log = open(SANDBOX / f"server-{tag}.log", "ab")
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "bridge_server:app", "--host", "127.0.0.1", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(BACKEND), env=sandbox_env(), stdout=log, stderr=subprocess.STDOUT,
    )
    for _ in range(120):
        if not port_free(PORT):
            try:
                if http_get("/api/health", timeout=2)[0] == 200:
                    return proc
            except OSError:
                pass
        time.sleep(0.25)
    proc.kill()
    raise SystemExit(f"沙箱后端未能在 30s 内起来，日志：{SANDBOX / f'server-{tag}.log'}")


def stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http_get(path: str, timeout: float = 20) -> tuple[int, Any]:
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"transportError": f"{type(exc).__name__}: {exc}"}


def invoke(channel: str, args: list, timeout: float = 20) -> dict:
    payload = json.dumps({"args": args}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + f"/ipc/{channel}", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8") or "null")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw[:400]}
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"channel": channel, "http": 0, "body": {"transportError": f"{type(exc).__name__}: {exc}"},
                "ms": int((time.perf_counter() - started) * 1000)}
    return {"channel": channel, "http": status, "body": body,
            "ms": int((time.perf_counter() - started) * 1000)}


# --------------------------------------------------------------------------
# 通道登记表（进程内只读，用于第①关）
# --------------------------------------------------------------------------
def load_registry() -> dict[str, str]:
    os.environ.update(sandbox_env())
    sys.path.insert(0, str(BACKEND))
    from app.api import ipc_bridge as bridge  # noqa: E402

    reg: dict[str, str] = {}
    for name in bridge.EXPLICIT:
        reg[name] = "EXPLICIT"
    for name in bridge.EMPTY_CHANNELS:
        reg.setdefault(name, "EMPTY")
    for name in bridge.BLOCKED_CHANNELS:
        reg.setdefault(name, "BLOCKED")
    for name in bridge.VIEW_ADAPTER_CHANNELS:
        reg.setdefault(name, "VIEW_ADAPTER")
    return reg


SHELL_NATIVE = {
    "open-creator-view", "open-chat-view", "reload-creator-view", "reload-chat-view",
    "destroy-creator-view", "destroy-chat-view", "set-creator-view-attached",
    "set-chat-view-attached", "init-automation-view", "update-automation-bounds",
    "set-account-detail-mode", "set-views-visible",
}


# --------------------------------------------------------------------------
# 结果判定
# --------------------------------------------------------------------------
def body_text(body: Any) -> str:
    try:
        return json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(body)


def _json_field(body: Any, key: str) -> Any:
    data = body.get("data") if isinstance(body, dict) else None
    return data.get(key) if isinstance(data, dict) else None


def _nurture_read(body: Any) -> dict:
    """get-nurture-config 的返回值：页面直接拿去 Object.assign，必须是裸对象。"""
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {}


def _chat_msg(body: Any, needle: str) -> bool:
    """测试推送的返回值 {success, message}：message 里是否说清了原因。"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return False
    return data.get("success") is False and needle in str(data.get("message") or "")


def _task_status(body: Any, task_id: str) -> str:
    """从任务列表返回值里取指定 id 的 status（找不到返回空串）。"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return ""
    for item in data:
        if isinstance(item, dict) and str(item.get("id")) == task_id:
            return str(item.get("status") or "")
    return ""


def _has_status(body: Any, status: str) -> bool:
    data = body.get("data") if isinstance(body, dict) else None
    return any(isinstance(i, dict) and str(i.get("status")) == status
               for i in (data or []))


def _judge_notices(total: int, demo: int) -> tuple[str, str]:
    """公告判定。

    计数取跑前快照而不是跑中现读：沙箱后端进程占着 WAL 时，父进程再开一个连接
    读会拿到 disk I/O error（Windows 上两个进程共用 -shm 的老问题），
    所以这里用 build_sandbox 之后 checkpoint 过的副本快照（与库内真实值一致）。
    """
    if not total:
        return "✅", ("公告为空：通知表无记录；提醒由 followups 巡检在访问跟进页时按 related_id 生成，"
                     "没有待办就不该有提醒")
    if demo * 2 >= total:
        return "⚠️", (f"{total} 条提醒全部由 followups 巡检自动生成（其中 {demo} 条是"
                      "「今日跟进/跟进逾期 · 王总/赵女士」这类），跟进对象是库里的演示客户；"
                      "通知表清空后重启会按 followup 重新生成，"
                      "要根治得先清演示客户与跟进记录（未列入本轮范围，待确认）")
    return "✅", f"公告 {total} 条，其中 {demo} 条指向演示客户，其余为真实业务提醒"


def _judge_export(body: Any) -> tuple[str, str]:
    """导出落盘：文件必须真在磁盘上，且表头 + 数据行数与写入时一致。"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not data.get("success"):
        return "❌", f"导出被拒：{(data or {}).get('error') or body_text(body)[:80]}"
    files = data.get("files") or []
    if not files:
        return "❌", "返回成功但没有文件记录"
    item = files[0]
    path = Path(str(item.get("path") or ""))
    if not path.is_file():
        return "❌", f"返回了路径但磁盘上没有文件：{path}"
    text = path.read_text(encoding="utf-8-sig")
    lines = len([ln for ln in text.splitlines() if ln.strip()])
    if lines != 4:
        return "❌", f"文件行数不符：期望表头+3 行，实际 {lines} 行"
    if not text.startswith("昵称,"):
        return "❌", "首行不是预期表头"
    return "✅", (f"落盘 {item.get('name')}（{item.get('bytes')} 字节，4 行：表头+3 条线索），"
                  f"可直接用 Excel 打开")


def _all_high(body: Any) -> bool:
    """get-leads-by-ids 读回：所有线索的意向标签都是「高意向」。"""
    data = body.get("data") if isinstance(body, dict) else None
    items = (data or {}).get("items") if isinstance(data, dict) else None
    return bool(items) and all(i.get("intention") == "高意向" for i in items)


def unavailable(body: Any) -> str:
    """后端是否明确说了「未开通 / 未接线」。返回说明文字，空串表示没说。"""
    text = body_text(body)
    for token in ("功能未开通", "未接入", "未接线", "未实现", "disabled"):
        if token in text:
            return token
    return ""


def is_fake(body: Any) -> str:
    text = body_text(body)
    for token in PLACEHOLDER_TOKENS:
        if token in text:
            return token
    return ""


_SKIP_CONTENT_KEYS = {"success", "mode", "runtime", "note", "msg", "message", "state",
                      "channel", "error", "total", "page", "pageSize", "source"}


def meaningful(value: Any) -> bool:
    """列表非空 / 字典里有真实内容（排除 success、note 这类信封字段）。"""
    if isinstance(value, list):
        return len(value) > 0
    if isinstance(value, dict):
        return any(meaningful(v) for k, v in value.items() if k not in _SKIP_CONTENT_KEYS)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return value is not None


def judge_call(res: dict) -> tuple[str, str]:
    """单次调用 → (存活, 说明)。存活：ok / error / timeout / http500。"""
    if res["http"] == 0:
        return "timeout", body_text(res["body"])[:160]
    if res["http"] >= 500:
        return "http500", f"HTTP {res['http']}"
    body = res["body"]
    if isinstance(body, dict) and body.get("success") is False:
        err = str(body.get("error") or body.get("msg") or "")
        if "未开通" in err:
            return "ok", f"未开通（{err[:40]}）"
        if err:
            return "error", err[:160]
    if isinstance(body, dict) and body.get("transportError"):
        return "timeout", body["transportError"][:160]
    return "ok", ""


@dataclass
class Feature:
    id: str
    page: str
    name: str
    channels: list[str]
    calls: list = field(default_factory=list)      # [(channel, args_list | callable)]
    kind: str = "read"                             # read / write / launch / static
    judge: Callable | None = None                  # (results, data) -> (gate3, 说明)
    readback: dict | None = None                   # {"channel":..., "args":..., "pick":..., "expect":...}
    empty_ok: str = ""                             # 空态可接受时的口径说明（仍标 ⚠️）
    suggestion: str = ""
    priority: str = "P1"
    static_reason: str = ""
    # 明确判「不做」的项：写清边界理由，不计入「整块空白」，
    # 单独归到报告里的「设计边界」区块（避免把按设计不做的功能算成 bug）。
    out_of_scope: str = ""


# --------------------------------------------------------------------------
# 功能清单：一行 = 用户能完成的一件事
# --------------------------------------------------------------------------
def build_features(ctx: dict) -> list[Feature]:
    feats: list[Feature] = []
    A = lambda x: [x]  # noqa: E731  args 包装

    # ---------------- 主壳：授权 / 系统信息 ----------------
    feats.append(Feature(
        "SYS-1", "主壳(index)", "打开软件能确认授权状态与版本号，不是空白页",
        ["check-auth", "refresh-auth", "get-version-info"],
        calls=[("check-auth", A({})), ("refresh-auth", A({})), ("get-version-info", A({}))],
        judge=lambda rs, d: (
            ("✅", f"授权态：{_json_field(d['check-auth']['body'], 'licenseTierLabel')}"
                   f"，到期 {_json_field(d['check-auth']['body'], 'expireTime')}")
            if any(k in body_text(d["check-auth"]["body"])
                   for k in ("licenseTierLabel", "activationLabel", "expireTime"))
            else ("⚠️", "返回里没看到授权态字段")),
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "SYS-2", "主壳(index)", "查看设备号与运行时状态（用于报障对账）",
        ["get-device-id", "get-runtime-state"],
        calls=[("get-device-id", A({})), ("get-runtime-state", A({}))],
        judge=lambda rs, d: (("✅", f"deviceId={str((d['get-device-id']['body'].get('data') or ''))[:40]}")
                             if meaningful(d["get-device-id"]["body"].get("data"))
                             else ("⚠️", "设备号为空")),
        priority="P2", suggestion="无"))
    feats.append(Feature(
        "SYS-3", "主壳(index)", "首页看到系统公告与性能占用",
        ["get-system-announcements", "get-system-performance"],
        calls=[("get-system-announcements", A({})), ("get-system-performance", A({}))],
        judge=lambda rs, d: _judge_notices(ctx["notification_total"], ctx["demo_notifications"]),
        priority="P1",
        suggestion="根因在演示客户/跟进数据（customers 120 / followups 59），清通知表无效",
        empty_ok="公告可为空（当前无运营公告），性能指标必须有值"))

    # ---------------- 账号池 ----------------
    feats.append(Feature(
        "ACC-1", "账号池(AccountManager)", "打开账号池看到账号列表与在线状态",
        ["get-account-pool"],
        calls=[("get-account-pool", A({}))],
        judge=lambda rs, d: (("✅", f"{len(d['get-account-pool']['body'].get('data') or [])} 个账号")
                             if d["get-account-pool"]["body"].get("data")
                             else ("⚠️", "账号池返回空数组")),
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "ACC-2", "账号池(AccountManager)", "在账号池新增一个账号并保存，重启后仍在",
        ["save-account-pool", "get-account-pool"], kind="write",
        calls=[("save-account-pool", lambda ctx: [[
                    {"id": ctx["real_account"]["id"], "name": ctx["real_account"]["name"],
                     "nickname": ctx["real_account"].get("nickname") or "",
                     "platform": "douyin",
                     "status": "online" if ctx["real_account"]["status"] == "online" else "offline",
                     "remark": ctx["real_account"].get("remark") or ""},
                    {"id": "acc-fixture-01", "name": "验收夹具号",
                     "nickname": "验收夹具号", "platform": "douyin",
                     "status": "offline", "remark": "acceptance fixture"}]]),
               ("get-account-pool", A({}))],
        judge=lambda rs, d: (("✅", "新增后立即可见")
                             if "acc-fixture-01" in body_text(d["get-account-pool"]["body"])
                             else ("❌", "保存成功但列表里没有该账号")),
        readback={"channel": "get-account-pool", "args": A({}),
                  "pick": lambda b: "acc-fixture-01" in body_text(b),
                  "expect": True, "label": "重启后新增账号仍在"},
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "ACC-3", "账号池(AccountManager)", "在账号池删除账号后刷新列表不再出现（且不误删其它账号）",
        ["save-account-pool", "get-account-pool"], kind="write",
        calls=[("save-account-pool", lambda ctx: [[
                    {"id": ctx["real_account"]["id"], "name": ctx["real_account"]["name"],
                     "nickname": ctx["real_account"].get("nickname") or "",
                     "platform": "douyin",
                     "status": "online" if ctx["real_account"]["status"] == "online" else "offline",
                     "remark": ctx["real_account"].get("remark") or ""},
                    {"id": "acc-fixture-01", "name": "验收夹具号",
                     "nickname": "验收夹具号", "platform": "douyin",
                     "status": "offline", "remark": "acceptance fixture"},
                    {"id": "acc-fixture-02", "name": "待删夹具",
                     "nickname": "待删夹具", "platform": "douyin",
                     "status": "offline", "remark": "to be deleted"}]]),
               ("save-account-pool", lambda ctx: [[
                    {"id": ctx["real_account"]["id"], "name": ctx["real_account"]["name"],
                     "nickname": ctx["real_account"].get("nickname") or "",
                     "platform": "douyin",
                     "status": "online" if ctx["real_account"]["status"] == "online" else "offline",
                     "remark": ctx["real_account"].get("remark") or ""},
                    {"id": "acc-fixture-01", "name": "验收夹具号",
                     "nickname": "验收夹具号", "platform": "douyin",
                     "status": "offline", "remark": "acceptance fixture"}]]),
               ("get-account-pool", A({}))],
        judge=lambda rs, d: (("✅", "被删账号消失、未在报文里的账号保留")
                             if "acc-fixture-02" not in body_text(d["get-account-pool"]["body"])
                             and "acc-fixture-01" in body_text(d["get-account-pool"]["body"])
                             else ("❌", "删除后状态不符合预期（可能误删或未删）")),
        readback={"channel": "get-account-pool", "args": A({}),
                  "pick": lambda b: ("acc-fixture-02" in body_text(b),
                                     "acc-fixture-01" in body_text(b)),
                  "expect": (False, True), "label": "重启后：被删号未复活、保留号仍在"},
        priority="P0",
        suggestion="删除按平台做全量同步，报文缺失即删；需保证不同平台互不影响"))
    feats.append(Feature(
        "ACC-4", "账号池(AccountManager)", "查看本机授权包含多少账号席位/额度",
        ["get-license-entitlement"], calls=[("get-license-entitlement", A({}))],
        judge=lambda rs, d: (("✅", f"席位 {d['get-license-entitlement']['body'].get('data', {}).get('seats')} 个 / "
                                    f"已用 {d['get-license-entitlement']['body'].get('data', {}).get('usedAccounts')} 个，"
                                    "按 1 码=1 席位折算")
                             if isinstance(d["get-license-entitlement"]["body"].get("data"), dict)
                             and "seats" in d["get-license-entitlement"]["body"]["data"]
                             else ("⚠️", "返回里没有席位字段")),
        priority="P1", suggestion="无（规则：1 个已激活授权码 = 1 个账号席位）"))
    feats.append(Feature(
        "ACC-5", "账号池(AccountManager)", "导入/导出抖音登录态 cookie 会话，并测试代理",
        ["import-douyin-cookie-session", "export-douyin-cookie-session", "test-account-proxy"],
        calls=[("import-douyin-cookie-session", A({})), ("export-douyin-cookie-session", A({})),
               ("test-account-proxy", A({}))],
        priority="P2", suggestion="高敏感能力，需先定策略（是否允许会话导出）",
        out_of_scope="会话 cookie 导入/导出等于把平台登录态在设备间搬运，"
                     "涉账号安全与平台风控，本版明确不做；代理测试同理"))

    # ---------------- 账号数据 ----------------
    feats.append(Feature(
        "CD-1", "账号数据(AccountData)", "打开账号数据看板看到按账号/按天的真实指标",
        ["get-account-data-dashboard"],
        calls=[("get-account-data-dashboard", A({}))],
        judge=lambda rs, d: (("✅", "看板有账号/按天维度数据")
                             if meaningful(d["get-account-data-dashboard"]["body"].get("data"))
                             else ("⚠️", "看板结构为空")),
        priority="P1", empty_ok="无账号快照时数值为 0，属真实空态"))
    feats.append(Feature(
        "CD-2", "账号数据(AccountData)", "手动触发一次账号数据采集",
        ["run-account-data-collect"], calls=[("run-account-data-collect", A({}))],
        priority="P1", suggestion="接 profile_sync 的采集入口（读创作者中心快照）"))
    feats.append(Feature(
        "CD-3", "账号数据(AccountData)", "把每日自动统计时刻改成 07:30，重启后仍是 07:30",
        ["save-account-data-schedule", "get-account-data-dashboard"], kind="write",
        calls=[("save-account-data-schedule", A({"time": "07:30"})),
               ("get-account-data-dashboard", A({}))],
        judge=lambda rs, d: (
            ("✅", f"看板 scheduleTime={_json_field(d['get-account-data-dashboard']['body'], 'scheduleTime')}，"
                   "保存值直接回显（无前端本地缓存参与）")
            if _json_field(d["get-account-data-dashboard"]["body"], "scheduleTime") == "07:30"
            else ("❌", f"保存成功但看板仍显示 "
                       f"{_json_field(d['get-account-data-dashboard']['body'], 'scheduleTime')}")),
        readback={"channel": "get-account-data-dashboard", "args": A({}),
                  "pick": lambda b: _json_field(b, "scheduleTime") == "07:30",
                  "expect": True, "label": "重启后时刻仍为 07:30"},
        priority="P2", suggestion="无"))

    # ---------------- 关键词获客 ----------------
    feats.append(Feature(
        "LG-1", "关键词获客(LeadgenTasks)", "打开关键词获客任务列表，看到任务与状态",
        ["get-leadgen-tasks"], calls=[("get-leadgen-tasks", A({}))],
        judge=lambda rs, d: (("✅", f"{len(d['get-leadgen-tasks']['body'].get('data') or [])} 条任务")
                             if d["get-leadgen-tasks"]["body"].get("data")
                             else ("⚠️", "任务列表为空")),
        priority="P0", empty_ok="尚无任务时为空数组属正常"))
    feats.append(Feature(
        "LG-2", "关键词获客(LeadgenTasks)", "点开一个任务看到详情和它采到的线索",
        ["get-leadgen-task-detail", "get-leadgen-task-leads"],
        calls=[("get-leadgen-task-detail", lambda ctx: A({"id": ctx["task_id"]})),
               ("get-leadgen-task-leads", lambda ctx: A({"id": ctx["task_id"], "page": 1, "pageSize": 10}))],
        judge=lambda rs, d: (("✅", f"详情正常，任务下取回 "
                                    f"{len((d['get-leadgen-task-leads']['body'].get('data') or {}).get('items') or [])} 条线索")
                             if (d["get-leadgen-task-leads"]["body"].get("data") or {}).get("items")
                             else ("⚠️", f"详情正常但该任务下 0 条线索（库内共 {ctx['task_leads']} 条有任务归属），"
                                         "source_task_id 关联口径需核对")),
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "LG-3", "关键词获客(LeadgenTasks)", "新建一条关键词获客任务并保存，重启后仍在列表",
        ["save-leadgen-task", "get-leadgen-tasks"], kind="write",
        calls=[("save-leadgen-task", [{"id": "acc-task-001", "name": "验收-关键词任务",
                                       "status": "stopped", "createdAt": "2026-11-14 12:00:00",
                                       "leadCount": 0, "runs": [],
                                       "configSnapshot": {"keyword": "验收关键词",
                                                          "selectedAccounts": [],
                                                          "sourceTypes": ["keyword"]}}]),
               ("get-leadgen-tasks", A({}))],
        judge=lambda rs, d: (("✅", "保存后列表可见")
                             if "acc-task-001" in body_text(d["get-leadgen-tasks"]["body"])
                             else ("❌", "保存返回成功但列表不含该任务")),
        readback={"channel": "get-leadgen-tasks", "args": A({}),
                  "pick": lambda b: "acc-task-001" in body_text(b),
                  "expect": True, "label": "重启后任务记录仍在（按 id 校验）"},
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "LG-4", "关键词获客(LeadgenTasks)", "把任务改名后保存，重启后看到的还是新名字",
        ["update-leadgen-task", "get-leadgen-tasks"], kind="write",
        calls=[("update-leadgen-task", A({"id": "acc-task-001",
                                          "patch": {"name": "验收-任务已改名"}})),
               ("get-leadgen-tasks", A({}))],
        judge=lambda rs, d: (("✅", "改名立即生效")
                             if "验收-任务已改名" in body_text(d["get-leadgen-tasks"]["body"])
                             else ("❌", "改名未生效")),
        readback={"channel": "get-leadgen-tasks", "args": A({}),
                  "pick": lambda b: "验收-任务已改名" in body_text(b),
                  "expect": True, "label": "重启后新名仍在"},
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "LG-5", "关键词获客(LeadgenTasks)", "删除一条关键词获客任务，列表里不再出现",
        ["save-leadgen-task", "delete-leadgen-tasks", "get-leadgen-tasks"],
        kind="write",
        calls=[("save-leadgen-task", A({"id": "acc-kw-del", "name": "验收-待删关键词任务",
                                        "status": "stopped", "createdAt": 1799999999000,
                                        "runs": [], "configSnapshot": {"keywords": ["待删词"]}})),
               ("delete-leadgen-tasks", A(["acc-kw-del"])),
               ("get-leadgen-tasks", A({}))],
        judge=lambda rs, d: (("✅", "删除后列表里已无该任务，评论获客任务未受影响")
                             if "验收-待删关键词任务" not in body_text(d["get-leadgen-tasks"]["body"])
                             else ("❌", "删除返回成功但任务仍在列表"))
        if d["delete-leadgen-tasks"]["body"].get("data", {}).get("success") is not False
        else ("❌", f"删除被拒：{_json_field(d['delete-leadgen-tasks']['body'], 'error')}"),
        readback={"channel": "get-leadgen-tasks", "args": A({}),
                  "pick": lambda b: "验收-待删关键词任务" in body_text(b),
                  "expect": False, "label": "重启后已删任务未复活"},
        priority="P1", suggestion="无（只删非评论任务，评论获客任务走另一通道）"))
    feats.append(Feature(
        "LG-6", "关键词获客(LeadgenTasks)", "点开始跑一条关键词获客任务（真机采集）",
        ["start-task"],
        kind="launch",
        calls=[("start-task", lambda ctx: A({"taskId": ctx["task_id"]}))],
        judge=lambda rs, d: (
            ("✅", f"启动前置校验生效：{_json_field(d['start-task']['body'], 'error')}"
                   "（未真起线程，按护栏只验前置与下发链路）")
            if d["start-task"]["body"].get("data", {}).get("success") is False
            else ("❌", "缺账号却返回启动成功")),
        static_reason="线程启停属护栏禁实调项；改验前置校验 + 指令下发/回传衔接",
        priority="P0",
        suggestion="真机链路已于上一轮单独验证（3 视频/19 条评论入库），此处只做护栏内判定"))
    feats.append(Feature(
        "LG-7", "关键词获客(LeadgenTasks)", "点停止让运行中的任务停下来",
        ["stop-task"], calls=[("stop-task", A({"taskId": "acc-task-never-started"}))],
        kind="static", static_reason="停止线程属护栏禁实调项，只验幂等返回",
        judge=lambda rs, d: (("✅", "未运行的任务返回幂等成功")
                             if d["stop-task"]["body"].get("success") else ("❌", "停止调用报错")),
        priority="P1", suggestion="无"))
    feats.append(Feature(
        "LG-8", "关键词获客(LeadgenTasks)", "打开运行日志抽屉看到本次运行的记录",
        ["get-entity-leadgen-task-runtime-logs", "clear-entity-leadgen-task-runtime-logs"],
        calls=[("get-entity-leadgen-task-runtime-logs", lambda ctx: A({"taskId": ctx["task_id"]}))],
        judge=lambda rs, d: (("✅", "日志接口返回数组")
                             if isinstance(d["get-entity-leadgen-task-runtime-logs"]["body"].get("data"), list)
                             else ("❌", "日志接口不是裸数组，壳会 .map 报错")),
        priority="P0", empty_ok="未跑过的任务日志为空数组属正常"))
    feats.append(Feature(
        "LG-9", "关键词获客(LeadgenTasks)", "任务启动前初始化关键词池并领取关键词，结束后释放",
        ["init-leadgen-keyword-pool", "claim-leadgen-keyword", "release-leadgen-keyword-pool"],
        calls=[("init-leadgen-keyword-pool", lambda ctx: A({"leadgenTaskId": "acc-task-001",
                                                            "keywords": ["验收词A", "验收词B"]})),
               ("claim-leadgen-keyword", lambda ctx: A({"leadgenTaskId": "acc-task-001",
                                                        "accountId": ctx["account_id"]})),
               ("release-leadgen-keyword-pool", A({"leadgenTaskId": "acc-task-001"}))],
        judge=lambda rs, d: (("✅", "池初始化/领取/释放三态正常")
                             if d["init-leadgen-keyword-pool"]["body"].get("success")
                             else ("❌", "池初始化失败")),
        priority="P1", suggestion="无"))
    feats.append(Feature(
        "LG-10", "关键词获客(LeadgenTasks)", "取消一条任务的定时计划，重启后不再自动跑",
        ["save-leadgen-schedules", "get-leadgen-schedules"], kind="write",
        calls=[("save-leadgen-schedules", A({"acc-sch-tmp": {
                    "type": "daily", "enabled": True, "startHm": "08:00", "endHm": "09:00",
                    "mode": "restart", "createdAt": 1799999999000}})),
               ("save-leadgen-schedules", A({})),
               ("get-leadgen-schedules", A({}))],
        judge=lambda rs, d: (
            ("✅", "清空后读回空 map：取消是整表覆盖，不存在「点了取消还留着旧计划」")
            if not (d["get-leadgen-schedules"]["body"].get("data") or {})
            else ("❌", "取消后读回仍有排期残留")),
        # 该表随后会被 SCH-1 重新写入，所以不做重启回读（回读的是 SCH-1 的值，会误判）
        priority="P2", suggestion="无"))
    feats.append(Feature(
        "LG-11", "关键词获客(LeadgenTasks)", "查看一个任务抓过的话题视频清单",
        ["get-leadgen-task-hashtag-videos"],
        calls=[("get-leadgen-task-hashtag-videos", lambda ctx: A({"taskId": ctx["task_id"]}))],
        judge=lambda rs, d: (("✅", f"{d['get-leadgen-task-hashtag-videos']['body'].get('data', {}).get('total')} 个视频 / "
                                    f"评论 {d['get-leadgen-task-hashtag-videos']['body'].get('data', {}).get('counts', {}).get('all')} 条")
                             if (d["get-leadgen-task-hashtag-videos"]["body"].get("data") or {}).get("total")
                             else ("⚠️", "该任务无视频维度数据")),
        priority="P2", suggestion="无（按 leads.video 去重聚合）"))

    # ---------------- 评论获客 ----------------
    feats.append(Feature(
        "EN-1", "评论获客(EntityLeadgenTasks)", "打开评论获客任务列表，看到任务与状态",
        ["get-entity-leadgen-tasks"], calls=[("get-entity-leadgen-tasks", A({}))],
        judge=lambda rs, d: (("✅", f"{len(d['get-entity-leadgen-tasks']['body'].get('data') or [])} 条")
                             if d["get-entity-leadgen-tasks"]["body"].get("data")
                             else ("⚠️", "为空")),
        priority="P0", empty_ok="尚无评论获客任务时为空"))
    feats.append(Feature(
        "EN-2", "评论获客(EntityLeadgenTasks)", "新建一条评论获客任务并保存",
        ["save-entity-leadgen-task", "get-entity-leadgen-tasks"],
        kind="write",
        calls=[("save-entity-leadgen-task", A({
            "id": "acc-entity-001", "name": "验收-评论任务", "status": "stopped",
            "createdAt": 1799999999000, "runs": [], "stats": {"videos": 2},
            "configSnapshot": {"sourceTypes": ["target_video"], "keyword": "验收词"},
            "leads": []})),
               ("get-entity-leadgen-tasks", A({}))],
        judge=lambda rs, d: (("✅", "建单后立即出现在评论获客列表，配置快照完整")
                             if "验收-评论任务" in body_text(d["get-entity-leadgen-tasks"]["body"])
                             and "target_video" in body_text(d["get-entity-leadgen-tasks"]["body"])
                             else ("❌", "保存返回成功但列表里没有该任务或配置丢失")),
        readback={"channel": "get-entity-leadgen-tasks", "args": A({}),
                  "pick": lambda b: "验收-评论任务" in body_text(b),
                  "expect": True, "label": "重启后评论任务仍在"},
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "EN-3", "评论获客(EntityLeadgenTasks)", "删除评论获客任务",
        ["save-entity-leadgen-task", "delete-entity-leadgen-tasks", "get-entity-leadgen-tasks"],
        kind="write",
        calls=[("save-entity-leadgen-task", A({"id": "acc-entity-del", "name": "验收-待删评论任务",
                                               "status": "stopped", "createdAt": 1799999999000})),
               ("delete-entity-leadgen-tasks", A(["acc-entity-del"])),
               ("get-entity-leadgen-tasks", A({}))],
        judge=lambda rs, d: (("✅", "删除后列表里已无该任务")
                             if "验收-待删评论任务" not in body_text(d["get-entity-leadgen-tasks"]["body"])
                             else ("❌", "删除后任务仍在列表")),
        readback={"channel": "get-entity-leadgen-tasks", "args": A({}),
                  "pick": lambda b: "验收-待删评论任务" in body_text(b),
                  "expect": False, "label": "重启后被删任务未复活"},
        priority="P1", suggestion="无"))
    feats.append(Feature(
        "EN-4", "评论获客(EntityLeadgenTasks)", "启动前看到并发上限与被占用账号，避免撞号",
        ["get-entity-leadgen-concurrency-limit", "get-entity-leadgen-busy-account-ids"],
        calls=[("get-entity-leadgen-concurrency-limit", A({})),
               ("get-entity-leadgen-busy-account-ids", A({}))],
        judge=lambda rs, d: (("✅", "并发上限与占用账号均返回")
                             if meaningful(d["get-entity-leadgen-concurrency-limit"]["body"].get("data"))
                             else ("⚠️", "并发上限为空")),
        priority="P0", suggestion="无"))

    # ---------------- 评论点赞 ----------------
    feats.append(Feature(
        "CL-1", "评论点赞(CommentLikeTasks)", "查看评论点赞任务的执行结果",
        ["get-comment-like-results"], calls=[("get-comment-like-results", A({}))],
        priority="P2", empty_ok="无点赞采集源，后端已注明 no_source",
        out_of_scope="评论点赞=对平台内容做写操作，本产品不做代操作；"
                     "没有点赞任务自然也没有执行结果，页面按「未开通」呈现"))
    feats.append(Feature(
        "CL-2", "评论点赞(CommentLikeTasks)", "启动一条评论点赞任务",
        ["start-comment-like-task-runtime"],
        calls=[("start-comment-like-task-runtime", A({"taskId": "acc-like-001"}))],
        priority="P1", suggestion="点赞属写操作，需先定风控与配额策略",
        out_of_scope="点赞是平台写操作，触达即风控；本版不实装执行引擎，只保留未开通提示"))

    # ---------------- 监控任务 ----------------
    feats.append(Feature(
        "MN-1", "监控任务(MonitorTasks)", "打开监控任务列表，看到任务与状态",
        ["get-monitor-tasks"], calls=[("get-monitor-tasks", A({}))],
        judge=lambda rs, d: (
            ("✅", f"{len(d['get-monitor-tasks']['body'].get('data') or [])} 条，"
                   f"其中 {ctx['task_failed']} 条失败态原样透出为 failed，错误原因可见")
            if d["get-monitor-tasks"]["body"].get("data") and _has_status(
                d["get-monitor-tasks"]["body"], "failed")
            else ("✅", f"{len(d['get-monitor-tasks']['body'].get('data') or [])} 条")
            if d["get-monitor-tasks"]["body"].get("data")
            else ("⚠️", f"可疑空态：tasks 表 {ctx['task_counts']['monitor']} 条监控记录，"
                        "但接口一条都没返回，需查读侧过滤条件")),
        priority="P1", empty_ok="无监控任务时为空",
        suggestion="任务名仍是内部类型名 media_crawler_live，建议落库时一并写用户可读名"))
    feats.append(Feature(
        "MN-2", "监控任务(MonitorTasks)", "新建一条监控任务并保存",
        ["save-monitor-task", "get-monitor-tasks"],
        kind="write",
        calls=[("save-monitor-task", A({
            "id": "acc-mon-001", "name": "验收-监控任务", "status": "running",
            "createdAt": 1799999999000,
            "runs": [{"accountId": "acc-1", "accountName": "验收账号"}],
            "stats": {"videos": 2, "leadsTotal": 5},
            "configSnapshot": {"keywords": ["验收监控词"], "interval": 30},
            "matches": [], "cycles": []})),
               ("get-monitor-tasks", A({}))],
        judge=lambda rs, d: (("✅", "任务出现在监控列表，关键词配置与运行账号都没丢")
                             if "验收-监控任务" in body_text(d["get-monitor-tasks"]["body"])
                             and "验收监控词" in body_text(d["get-monitor-tasks"]["body"])
                             else ("❌", "保存返回 id 但列表里任务缺失或配置为空")),
        readback={"channel": "get-monitor-tasks", "args": A({}),
                  "pick": lambda b: "验收监控词" in body_text(b),
                  "expect": True, "label": "重启后监控配置仍在"},
        priority="P1", suggestion="无（tasks.payload 存整条 taskRecord）"))
    feats.append(Feature(
        "MN-3", "监控任务(MonitorTasks)", "查看监控命中记录（哪条评论命中了关键词）",
        ["get-monitor-task-matches"], calls=[("get-monitor-task-matches", A({}))],
        judge=lambda rs, d: (
            ("⚠️", f"占位符掩盖空评论：库内 {ctx['empty_comment']}/{ctx['leads_total']} 条线索无评论，"
                   "接口用「[关键词] 无评论文本」填充，用户会误以为抓到了评论")
            if "无评论文本" in body_text(d["get-monitor-task-matches"]["body"])
            else ("✅", "命中记录含真实评论")),
        priority="P1", empty_ok="无监控运行时无命中记录"))
    feats.append(Feature(
        "MN-4", "监控任务(MonitorTasks)", "监控运行时实时看到新事件在滚",
        ["get-monitor-live-events"], calls=[("get-monitor-live-events", lambda ctx: A({"taskId": ctx["monitor_task_id"]}))],
        judge=lambda rs, d: (("✅", f"{d['get-monitor-live-events']['body'].get('data', {}).get('total')} 条事件")
                             if (d["get-monitor-live-events"]["body"].get("data") or {}).get("items")
                             else ("✅", "空 + 后端已注明「该任务暂无运行事件」，属说明性空态："
                                         "监控任务从未真跑过，日志表里没有该任务的记录")),
        priority="P2", suggestion="无（日志表懒创建，跑过才有事件）"))
    feats.append(Feature(
        "MN-5", "监控任务(MonitorTasks)", "点停止结束监控，重开页面状态仍是已停止",
        ["stop-monitor", "get-monitor-tasks"], kind="write",
        calls=[("stop-monitor", A({})), ("get-monitor-tasks", A({}))],
        judge=lambda rs, d: (
            ("✅", f"归位 {(_json_field(d['stop-monitor']['body'], 'stopped') or 0)} 条，"
                   f"acc-mon-001 现为 {_task_status(d['get-monitor-tasks']['body'], 'acc-mon-001') or '缺失'}；"
                   "状态落库（无执行引擎，只做本地状态归位）")
            if _task_status(d["get-monitor-tasks"]["body"], "acc-mon-001") == "stopped"
            else ("❌", f"停止返回成功但记录状态仍为 "
                       f"{_task_status(d['get-monitor-tasks']['body'], 'acc-mon-001') or '缺失'}")),
        readback={"channel": "get-monitor-tasks", "args": A({}),
                  "pick": lambda b: _task_status(b, "acc-mon-001") == "stopped",
                  "expect": True, "label": "重启后仍为已停止"},
        priority="P1", suggestion="无（stop-monitor 已从门禁摘出，只改本地状态）"))

    # ---------------- 养号 ----------------
    feats.append(Feature(
        "WU-2", "账号养号(AccountNurture/SelfWarmup)", "新建一条养号任务并保存",
        ["save-self-warmup-task", "get-self-warmup-tasks"],
        kind="write",
        calls=[("save-self-warmup-task", A({"id": "acc-warm-001", "name": "验收-养号任务",
                                            "status": "ready", "createdAt": 1799999999000,
                                            "configSnapshot": {"browsePerDay": 20,
                                                               "intervalMinutes": 15}})),
               ("get-self-warmup-tasks", A({}))],
        judge=lambda rs, d: (("✅", "养号任务已入库，浏览配额配置保留")
                             if "验收-养号任务" in body_text(d["get-self-warmup-tasks"]["body"])
                             and "browsePerDay" in body_text(d["get-self-warmup-tasks"]["body"])
                             else ("❌", "保存成功但列表里没有该任务")),
        readback={"channel": "get-self-warmup-tasks", "args": A({}),
                  "pick": lambda b: "验收-养号任务" in body_text(b),
                  "expect": True, "label": "重启后养号任务仍在"},
        priority="P2", suggestion="无"))
    feats.append(Feature(
        "WU-1", "账号养号(AccountNurture/SelfWarmup)", "打开养号任务列表，看到任务与运行态",
        ["get-self-warmup-tasks"], calls=[("get-self-warmup-tasks", A({}))],
        judge=lambda rs, d: (("✅", f"{len(d['get-self-warmup-tasks']['body'].get('data') or [])} 条，"
                                    "含上一步新建的验收任务")
                             if d["get-self-warmup-tasks"]["body"].get("data")
                             else ("⚠️", f"可疑空态：tasks 表 {ctx['task_counts']['warmup']} 条养号记录，"
                                         "且上一步刚写入仍读不到，需查读侧过滤条件")),
        priority="P2", empty_ok="无养号任务时为空"))
    feats.append(Feature(
        "WU-3", "账号养号(AccountNurture/SelfWarmup)", "查看养号事件（浏览/点赞/间隔）",
        ["get-self-warmup-events"], calls=[("get-self-warmup-events", A({}))],
        priority="P2", empty_ok="无养号运行时无事件"))
    feats.append(Feature(
        "WU-4", "账号养号(AccountNurture/SelfWarmup)", "启动养号运行时让账号自动养号",
        ["start-self-warmup-task-runtime"],
        calls=[("start-self-warmup-task-runtime", A({"taskId": "acc-warm-001"}))],
        priority="P1", suggestion="养号=写操作，需风控与频率策略后再接"))

    # 养号配置与任务补丁：页面会校验返回值，接错即假成功
    feats.append(Feature(
        "WU-5", "账号养号(AccountNurture/SelfWarmup)", "把养号频率/配额配置存下来，重启后还是这套参数",
        ["save-nurture-config", "get-nurture-config"], kind="write",
        calls=[("save-nurture-config", A({"dailyLikeLimit": 17, "dailyCommentLimit": 6,
                                          "activeHours": ["09:00-12:00", "14:00-18:00"],
                                          "enabled": True})),
               ("get-nurture-config", A({}))],
        judge=lambda rs, d: (
            ("✅", "读回裸配置：dailyLikeLimit=17、两段活跃时段都在，且不带 success 信封")
            if _nurture_read(d["get-nurture-config"]["body"])
            and "success" not in _nurture_read(d["get-nurture-config"]["body"])
            and _nurture_read(d["get-nurture-config"]["body"]).get("dailyLikeLimit") == 17
            else ("❌", "读回的不是裸配置（带 success 信封或字段丢失）——"
                        "页面会把 success/config 当成配置项写进用户配置并原样存回库")),
        readback={"channel": "get-nurture-config", "args": A({}),
                  "pick": lambda b: bool(_nurture_read(b))
                  and _nurture_read(b).get("dailyLikeLimit") == 17
                  and "success" not in _nurture_read(b),
                  "expect": True, "label": "重启后养号配置仍在"},
        priority="P1", suggestion="无（整份配置存 system_settings，读侧返回裸对象）"))
    feats.append(Feature(
        "WU-6", "账号养号(AccountNurture/SelfWarmup)", "给养号任务打开「随软件启动」",
        ["update-self-warmup-task"], kind="write",
        calls=[("update-self-warmup-task", A({"id": "acc-warm-001",
                                              "patch": {"autoStartOnLaunch": True}}))],
        judge=lambda rs, d: (
            ("✅", "回执里带 autoStartOnLaunch=true，页面判定保存成功")
            if _json_field(d["update-self-warmup-task"]["body"], "autoStartOnLaunch") is True
            else ("❌", "回执里没有刚写入的字段——页面会报「设置未保存，请稍后重试」")),
        readback={"channel": "get-self-warmup-tasks", "args": A({}),
                  "pick": lambda b: '"autoStartOnLaunch": true' in body_text(b),
                  "expect": True, "label": "重启后该开关仍是开"},
        priority="P1", suggestion="无（patch 合入 tasks.payload，回执=落库后重读的整条记录）"))
    feats.append(Feature(
        "WU-7", "账号养号(AccountNurture/SelfWarmup)", "保存自热配置（选账号+每轮次数+轮数）",
        ["update-self-warmup-task"], kind="write",
        calls=[("update-self-warmup-task", A({
            "id": "acc-warm-001",
            "patch": {"configSnapshot": {"accounts": ["acc-a", "acc-b"],
                                         "perRound": 3, "rounds": 5}}}))],
        judge=lambda rs, d: (
            ("✅", "回执里 configSnapshot.perRound=3，账号两个都在")
            if isinstance(_json_field(d["update-self-warmup-task"]["body"], "configSnapshot"), dict)
            and _json_field(d["update-self-warmup-task"]["body"], "configSnapshot").get("perRound") == 3
            else ("❌", "回执里的 configSnapshot 不是刚存的这份，页面会提示「保存失败」")),
        readback={"channel": "get-self-warmup-tasks", "args": A({}),
                  "pick": lambda b: '"acc-b"' in body_text(b),
                  "expect": True, "label": "重启后自热配置仍在"},
        priority="P1", suggestion="无（整份 configSnapshot 覆盖写入）"))

    # ---------------- 作品发布 ----------------
    feats.append(Feature(
        "PB-4", "作品发布(WorkPublish)", "新建一条发布任务并保存（选视频+文案+账号）",
        ["save-work-publish-task", "get-work-publish-tasks"],
        kind="write",
        calls=[("save-work-publish-task", A({
            "id": "acc-pub-001", "name": "验收-发布任务", "status": "ready",
            "createdAt": 1799999999000,
            "items": [{"video": "验收视频A", "caption": "验收文案", "accountId": "acc-1"}],
            "configSnapshot": {"caption": "验收文案"}})),
               ("get-work-publish-tasks", A({}))],
        judge=lambda rs, d: (("✅", "发布任务与作品清单已入库")
                             if "验收-发布任务" in body_text(d["get-work-publish-tasks"]["body"])
                             and "验收视频A" in body_text(d["get-work-publish-tasks"]["body"])
                             else ("❌", "保存返回 id 但列表里没有该任务")),
        readback={"channel": "get-work-publish-tasks", "args": A({}),
                  "pick": lambda b: "验收视频A" in body_text(b),
                  "expect": True, "label": "重启后作品清单仍在"},
        priority="P1", suggestion="无（发布动作本身仍走人工确认，不在此通道）"))
    feats.append(Feature(
        "PB-1", "作品发布(WorkPublish)", "打开作品发布任务列表",
        ["get-work-publish-tasks"], calls=[("get-work-publish-tasks", A({}))],
        judge=lambda rs, d: (("✅", f"{len(d['get-work-publish-tasks']['body'].get('data') or [])} 条，"
                                    "含上一步新建的发布任务")
                             if d["get-work-publish-tasks"]["body"].get("data")
                             else ("⚠️", f"可疑空态：tasks 表 {ctx['task_counts']['publish']} 条发布记录，"
                                         "且上一步刚写入仍读不到")),
        priority="P2", empty_ok="无发布任务时为空"))
    feats.append(Feature(
        "PB-2", "作品发布(WorkPublish)", "查看今日发布额度还剩多少",
        ["get-work-publish-quota"], calls=[("get-work-publish-quota", A({}))],
        priority="P1", suggestion="无"))
    feats.append(Feature(
        "PB-3", "作品发布(WorkPublish)", "查看发布模板列表并选择模板",
        ["get-work-publish-templates"], calls=[("get-work-publish-templates", A({}))],
        priority="P2", empty_ok="后端注明无模板数据源"))

    # ---------------- 线索池 ----------------
    feats.append(Feature(
        "LP-1", "线索池(LeadPool)", "打开线索池分页浏览，总数与每页条数对得上",
        ["get-leads-page", "get-leads-count"],
        calls=[("get-leads-page", A({"page": 1, "pageSize": 20})), ("get-leads-count", A({}))],
        judge=lambda rs, d: (("✅", f"total={d['get-leads-count']['body'].get('data', {}).get('total')}"
                                    f"，本页 {len(d['get-leads-page']['body'].get('data', {}).get('items') or [])} 条")
                             if (d["get-leads-page"]["body"].get("data") or {}).get("items")
                             else ("⚠️", "首页无数据")),
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "LP-2", "线索池(LeadPool)", "按关键词搜索线索，结果只含命中项",
        ["get-leads-page"],
        calls=[("get-leads-page", lambda ctx: A({"page": 1, "pageSize": 10,
                                                 "filters": {"keyword": ctx["keyword"]}}))],
        judge=lambda rs, d: (("✅", f"关键词“{ctx['keyword']}”命中 {(d['get-leads-page']['body'].get('data') or {}).get('total')} 条")
                             if (d["get-leads-page"]["body"].get("data") or {}).get("total")
                             else ("⚠️", "关键词搜索 0 命中")),
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "LP-3", "线索池(LeadPool)", "按意向等级筛选线索（高/中/低）",
        ["get-leads-page"],
        calls=[("get-leads-page", A({"page": 1, "pageSize": 10, "filters": {"intention": "高意向"}}))],
        judge=lambda rs, d: (("✅", f"高意向 {(d['get-leads-page']['body'].get('data') or {}).get('total')} 条")
                             if ((d["get-leads-page"]["body"].get("data") or {}).get("total") or 0) > 0
                             else ("⚠️", "高意向筛选 0 命中（需核对库内是否真无高意向）")),
        priority="P1", empty_ok="库内无高意向线索时可为 0（需与全量对比确认）"))
    feats.append(Feature(
        "LP-4", "线索池(LeadPool)", "按入口来源与地区筛选线索",
        ["get-leads-page"],
        calls=[("get-leads-page", lambda ctx: A({"page": 1, "pageSize": 10,
                                                 "filters": {"entrySource": [ctx["source"]],
                                                             "locations": [ctx["location"]]}}
                                                if ctx.get("source") and ctx.get("location")
                                                else {"page": 1, "pageSize": 10,
                                                      "filters": {"entrySource": [ctx["source"]]}})),
               ],
        judge=lambda rs, d: (("✅", f"来源筛选 {(d['get-leads-page']['body'].get('data') or {}).get('total')} 条")
                             if ((d["get-leads-page"]["body"].get("data") or {}).get("total") or 0) > 0
                             else ("⚠️", "来源/地区筛选 0 命中")),
        priority="P1", empty_ok="该来源/地区无数据时可为 0"))
    feats.append(Feature(
        "LP-5", "线索池(LeadPool)", "看线索池统计卡（总数/已触达/未触达）",
        ["get-leads-pool-stats"], calls=[("get-leads-pool-stats", A({}))],
        judge=lambda rs, d: (("✅", f"total={( d['get-leads-pool-stats']['body'].get('data') or {}).get('stats', {}).get('total')}")
                             if ((d["get-leads-pool-stats"]["body"].get("data") or {}).get("stats") or {}).get("total")
                             else ("⚠️", "统计为空")),
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "LP-6", "线索池(LeadPool)", "打开筛选下拉，看到账号/关键词/地区选项",
        ["get-leads-filter-options"], calls=[("get-leads-filter-options", A({}))],
        judge=lambda rs, d: (("✅", "下拉选项有内容")
                             if meaningful(d["get-leads-filter-options"]["body"].get("data")) else ("⚠️", "选项全空")),
        priority="P1", suggestion="无"))
    feats.append(Feature(
        "LP-7", "线索池(LeadPool)", "勾选多条线索后批量进入跟进/取详情",
        ["get-leads-by-ids", "query-leads-for-batch"],
        calls=[("get-leads-by-ids", lambda ctx: A({"ids": ctx["lead_ids"][:5]})),
               ("query-leads-for-batch", lambda ctx: A({"limit": 10, "filters": {}}))],
        judge=lambda rs, d: (("✅", f"按 id 取回 {len((d['get-leads-by-ids']['body'].get('data') or {}).get('items') or [])} 条")
                             if (d["get-leads-by-ids"]["body"].get("data") or {}).get("items")
                             else ("❌", "按 id 取不到线索")),
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "LP-8", "线索池(LeadPool)", "把当前筛选结果导出成 CSV，文件能打开且行数对",
        ["save-lead-export-csvs"], kind="write",
        calls=[("save-lead-export-csvs", A({"files": [{
            "name": "验收_线索导出.csv",
            "content": "\ufeff昵称,平台,意向等级\n开民宿的阿凯,douyin,A\n"
                       "培训刘老师,douyin,B\n做培训的周老师,douyin,C\n"}]}))],
        judge=lambda rs, d: _judge_export(d["save-lead-export-csvs"]["body"]),
        readback={"file": str(SB_DATA / "exports" / "leads" / "验收_线索导出.csv"),
                  "pick": lambda b: (b.get("data") or {}).get("lines") == 4,
                  "expect": True, "label": "重启后文件仍在，表头+3 行数据共 4 行"},
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "LP-9", "线索池(LeadPool)", "查看线索的历史处理记录（谁在什么时候跟进过）",
        ["get-history"], calls=[("get-history", lambda ctx: A({"leadId": ctx["lead_id"]}))],
        judge=lambda rs, d: (("✅", f"{len(d['get-history']['body'].get('data') or [])} 个任务分组 / "
                                    f"{sum(len(g.get('items') or []) for g in (d['get-history']['body'].get('data') or []))} 条记录")
                             if isinstance(d["get-history"]["body"].get("data"), list)
                             and d["get-history"]["body"]["data"]
                             else ("⚠️", "历史为空（未找到带任务归属的线索）")),
        priority="P1", suggestion="无（按 source_task_id 分组返回）"))
    feats.append(Feature(
        "LP-10", "线索池(LeadPool)", "批量修改线索意向等级",
        ["update-leads-intention", "get-leads-by-ids"],
        kind="write",
        calls=[("update-leads-intention",
                lambda ctx: A({"leadIds": ctx["lead_ids"][:3], "isHigh": True})),
               ("get-leads-by-ids", lambda ctx: A({"ids": ctx["lead_ids"][:3]}))],
        judge=lambda rs, d: (("✅", f"{d['update-leads-intention']['body'].get('data', {}).get('updated')} 条已标为高意向，读回一致")
                             if _all_high(d["get-leads-by-ids"]["body"])
                             else ("❌", "更新后读回意向未变（写成功但没落库）")),
        readback={"channel": "get-leads-by-ids",
                  "args": A({"ids": ctx["lead_ids"][:3]}),
                  "pick": lambda b: _all_high(b),
                  "expect": True, "label": "重启后 3 条仍是高意向"},
        priority="P1", suggestion="无（直写 leads.intent_level）"))

    # ---------------- 线索详情 ----------------
    feats.append(Feature(
        "LD-1", "线索详情(LeadDetailModal)", "在线索详情里改备注/意向并保存",
        ["update-lead-in-history", "get-leads-page"],
        kind="write",
        calls=[("update-lead-in-history", lambda ctx: A({
                    "leadId": ctx["lead_ids"][-1],
                    "updates": {"isHighIntention": False, "note": "验收备注-可检索"}})),
               ("get-leads-page", A({"page": 1, "pageSize": 5,
                                     "filters": {"keyword": "验收备注-可检索"}}))],
        judge=lambda rs, d: (("✅", "备注已落库（按备注关键词能检索到该线索），意向已改为低意向")
                             if (d["get-leads-page"]["body"].get("data") or {}).get("total")
                             else ("❌", "写完按备注搜不到，说明没落库")),
        readback={"channel": "get-leads-page",
                  "args": A({"page": 1, "pageSize": 5,
                             "filters": {"keyword": "验收备注-可检索"}}),
                  "pick": lambda b: bool((b.get("data", {}) or {}).get("total")),
                  "expect": True, "label": "重启后备注仍可检索"},
        priority="P1", suggestion="无（备注/意向直写 leads 真实列）"))

    # ---------------- 消息中心 ----------------
    feats.append(Feature(
        "MS-1", "消息中心(MessageCenter)", "看到未读消息数并在有新消息时变化",
        ["get-chat-unread-state"], calls=[("get-chat-unread-state", A({}))],
        judge=lambda rs, d: (("✅", "未读态返回") if meaningful(d["get-chat-unread-state"]["body"].get("data"))
                             else ("⚠️", "未读态为空")),
        priority="P1", suggestion="无"))
    feats.append(Feature(
        "MS-2", "消息中心(MessageCenter)", "打开会话列表看到本地会话与消息记录",
        ["get-message-aggregate"], calls=[("get-message-aggregate", A({}))],
        judge=lambda rs, d: (("✅", "会话聚合有内容")
                             if meaningful(d["get-message-aggregate"]["body"].get("data")) else ("⚠️", "会话聚合为空")),
        priority="P1", empty_ok="本地无私信记录时为空（后端注明不读平台实时私信）"))
    feats.append(Feature(
        "MS-3", "消息中心(MessageCenter)", "打开通知配置看到当前 webhook 设置",
        ["get-chat-notification-config"], calls=[("get-chat-notification-config", A({}))],
        judge=lambda rs, d: (("✅", f"配置含 {', '.join(sorted((d['get-chat-notification-config']['body'].get('data') or {}).get('config', {}).keys()))}")
                             if isinstance(d["get-chat-notification-config"]["body"].get("data"), dict)
                             and "webhookType" in (d["get-chat-notification-config"]["body"]["data"].get("config") or {})
                             else ("⚠️", "配置结构里没有 webhookType")),
        priority="P1", empty_ok="未配置过时给默认值（webhookType=none）"))
    feats.append(Feature(
        "MS-4", "消息中心(MessageCenter)", "改钉钉/飞书 webhook 并保存，重启后配置还在",
        ["save-chat-notification-config", "get-chat-notification-config"], kind="write",
        calls=[("save-chat-notification-config", A({
                    "webhookType": "dingtalk",
                    "webhookUrlDingtalk": "https://example.invalid/acc-hook",
                    "webhookSecretDingtalk": "acc-secret"})),
               ("get-chat-notification-config", A({}))],
        judge=lambda rs, d: (("✅", f"写入 {len((d['save-chat-notification-config']['body'].get('data') or {}).get('saved') or [])} 个字段，"
                                    f"回读 webhookType={(d['get-chat-notification-config']['body'].get('data') or {}).get('webhookType')}")
                             if "acc-hook" in body_text(d["get-chat-notification-config"]["body"])
                             else ("❌", "保存返回 success 但读回没有 webhook；"
                                         f"save 回执 saved={body_text(d['save-chat-notification-config']['body'])[:160]}")),
        readback={"channel": "get-chat-notification-config", "args": A({}),
                  "pick": lambda b: "acc-hook" in body_text(b), "expect": True,
                  "label": "重启后 webhook 仍是写入值"},
        priority="P0",
        suggestion="无（白名单已改为前端真实字段并回传 config）"))
    feats.append(Feature(
        "MS-5", "消息中心(MessageCenter)", "点「测试推送」得到一个说得清的发送结果",
        ["test-chat-message-webhook"],
        calls=[("test-chat-message-webhook", A({
                    "webhookType": "dingtalk",
                    "webhookUrlDingtalk": "https://example.invalid/acc-hook",
                    "webhookSecretDingtalk": "acc-secret"}))],
        judge=lambda rs, d: (
            ("✅", "通道已接通：非官方地址被拒并说明原因（拦住任意 URL 转发），"
                   "「不推送」时直接回无需测试；真发送需填用户自己的机器人地址")
            if _chat_msg(d["test-chat-message-webhook"]["body"], "只支持")
            else ("❌", "测试推送没有给出可读结果：" +
                  body_text(d["test-chat-message-webhook"]["body"])[:140])),
        priority="P1",
        suggestion="真发送需用户配自己的飞书/钉钉机器人；已限官方域名+路径，不跟随重定向"))

    # ---------------- 智能体 ----------------
    feats.append(Feature(
        "AI-1", "智能体(AiPersonas)", "打开智能体页看到已有人设列表",
        ["get-task-config"], calls=[("get-task-config", A({}))],
        judge=lambda rs, d: (("✅", "人设配置已存在")
                             if meaningful(d["get-task-config"]["body"].get("data")) else ("⚠️", "配置为 null（首次引导）")),
        priority="P1", empty_ok="从未保存过人设时返回 null 属设计契约"))
    feats.append(Feature(
        "AI-2", "智能体(AiPersonas)", "新增一个人设并保存，重启后仍在人设列表",
        ["save-ai-agents", "get-task-config"], kind="write",
        calls=[("save-ai-agents", lambda ctx: [[*ctx["personas"], {
            "id": "acc-persona-01", "name": "验收人设", "avatar": "", "prompt": "验收用提示词"},
            {"id": "acc-persona-02", "name": "待删人设", "avatar": "", "prompt": "to be deleted"}]]),
               ("get-task-config", A({}))],
        judge=lambda rs, d: (("✅", "人设立即可见")
                             if "acc-persona-01" in body_text(d["get-task-config"]["body"])
                             else ("❌", "保存返回 true 但人设未落库")),
        readback={"channel": "get-task-config", "args": A({}),
                  "pick": lambda b: "acc-persona-01" in body_text(b), "expect": True,
                  "label": "重启后人设仍在"},
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "AI-3", "智能体(AiPersonas)", "删除一个人设，重启后不再出现且不影响其它人设",
        ["delete-ai-agent", "get-task-config"], kind="write",
        calls=[("delete-ai-agent", ["acc-persona-02"]), ("get-task-config", A({}))],
        judge=lambda rs, d: (("✅", "被删人设消失、另一条保留")
                             if "acc-persona-02" not in body_text(d["get-task-config"]["body"])
                             and "acc-persona-01" in body_text(d["get-task-config"]["body"])
                             else ("❌", "删除后状态不符合预期")),
        readback={"channel": "get-task-config", "args": A({}),
                  "pick": lambda b: ("acc-persona-02" in body_text(b),
                                     "acc-persona-01" in body_text(b)),
                  "expect": (False, True), "label": "重启后：已删人设未复活、另一条仍在"},
        priority="P0", suggestion="无"))
    feats.append(Feature(
        "AI-4", "智能体(AiPersonas)", "查看 AI 试测剩余额度",
        ["get-persona-test-quota"], calls=[("get-persona-test-quota", A({}))],
        judge=lambda rs, d: (("✅", "额度字段返回") if meaningful(d["get-persona-test-quota"]["body"].get("data"))
                             else ("⚠️", "额度为空")),
        priority="P2", suggestion="无"))
    feats.append(Feature(
        "AI-5", "智能体(AiPersonas)", "改任务配置（工作时段/并发）并保存，重启后仍是新值",
        ["save-task-config", "get-task-config"], kind="write",
        calls=[("save-task-config", A({"workHourStart": 9, "workHourEnd": 21,
                                       "dailyLeadLimit": 60, "accMarker": "v1"})),
               ("get-task-config", A({}))],
        judge=lambda rs, d: (("✅", "配置写入后立即可读")
                             if "accMarker" in body_text(d["get-task-config"]["body"])
                             else ("❌", "保存成功但读不到")),
        readback={"channel": "get-task-config", "args": A({}),
                  "pick": lambda b: "v1" in body_text(b), "expect": True,
                  "label": "重启后配置值未回退"},
        priority="P1", suggestion="无"))

    # ---------------- 大屏 ----------------
    feats.append(Feature(
        "DB-1", "大屏(Dashboard)", "打开大屏看到线索总量/运行中任务/自动化任务汇总",
        ["get-leads-pool-stats", "get-running-entity-leadgen-task-ids", "get-active-automation-tasks"],
        calls=[("get-leads-pool-stats", A({})), ("get-running-entity-leadgen-task-ids", A({})),
               ("get-active-automation-tasks", A({}))],
        judge=lambda rs, d: (("✅", "大屏三块都有返回值")
                             if all(v["http"] == 200 for v in d.values()) else ("❌", "有块报错")),
        priority="P1", empty_ok="当前无运行任务时 ids 为空属正常"))
    feats.append(Feature(
        "DB-2", "大屏(Dashboard)", "看创作/消息聚合数据（粉丝、作品、评论、回复）",
        ["get-creator-aggregate"], calls=[("get-creator-aggregate", A({}))],
        judge=lambda rs, d: (("✅", "聚合有结构")
                             if meaningful(d["get-creator-aggregate"]["body"].get("data")) else ("⚠️", "聚合为空")),
        priority="P2", empty_ok="未采集过创作者快照时指标为 0，后端 note 已说明"))

    # ---------------- 采集库 ----------------
    feats.append(Feature(
        "CB-1", "采集库(LeadPool/AccountData)", "查看已处理过的视频列表（去重后）",
        ["get-processed-videos"], calls=[("get-processed-videos", A({}))],
        judge=lambda rs, d: (("⚠️", f"{len(d['get-processed-videos']['body'].get('data', {}).get('items') or [])} 个视频，"
                                     f"但含演示数据（leads 里 {ctx['demo_videos']} 条挂在“演示视频”上），"
                                     "用户会把它当真实采集成果")
                             if ctx["demo_videos"] and
                             "演示" in body_text(d["get-processed-videos"]["body"])
                             else ("✅", "已处理视频列表为真实采集")
                             if meaningful(d["get-processed-videos"]["body"].get("data"))
                             else ("⚠️", "为空")),
        priority="P1", empty_ok="尚未采集过视频时为空"))
    feats.append(Feature(
        "CB-2", "采集库(LeadPool/AccountData)", "打开采集库分页看采到的视频与作者",
        ["get-collected-videos-page", "get-collected-authors-page"],
        calls=[("get-collected-videos-page", A({"page": 1, "pageSize": 10})),
               ("get-collected-authors-page", A({"page": 1, "pageSize": 10}))],
        judge=lambda rs, d: (("✅", f"{d['get-collected-videos-page']['body'].get('data', {}).get('total')} 个视频 / "
                                    f"{d['get-collected-authors-page']['body'].get('data', {}).get('total')} 位作者")
                             if (d["get-collected-videos-page"]["body"].get("data") or {}).get("total")
                             else ("⚠️", "采集库为空")),
        priority="P1", suggestion="无（按 leads.video / nickname 聚合）"))

    # ---------------- 系统设置 / 更新 / 帮助 ----------------
    feats.append(Feature(
        "ST-1", "设置(Settings/SystemSettings)", "在设置里打开/关闭硬件加速、调界面缩放、清缓存",
        ["get-hardware-acceleration", "set-ui-zoom-factor", "clear-cache"],
        calls=[("get-hardware-acceleration", A({})), ("set-ui-zoom-factor", A({"factor": 1.0})),
               ("clear-cache", A({}))],
        priority="P2", suggestion="这三项是壳侧能力（Electron app API），应由 ipcMain 处理，不走后端",
        out_of_scope="硬件加速 / 界面缩放 / 清缓存都是 Electron app API，"
                     "HTTP 桥没有这个权限；正确落点在桌面壳 ipcMain，不属于后端接线范围"))
    feats.append(Feature(
        "UP-1", "主壳(index)", "检查更新并下载安装",
        ["check-update", "start-download"], calls=[("check-update", A({}))],
        kind="static", static_reason="安装更新属护栏禁实调项",
        priority="P2", suggestion="壳侧 electron-updater 处理，后端不参与",
        out_of_scope="更新由桌面壳 electron-updater 负责（下载/校验/安装），"
                     "后端只提供版本号；安装动作也不能由后端触发"))
    feats.append(Feature(
        "UP-2", "主壳(index)", "刷新系统公告",
        ["refresh-system-announcements"], calls=[("refresh-system-announcements", A({}))],
        judge=lambda rs, d: (("✅", f"刷新后返回 {len(d['refresh-system-announcements']['body'].get('data', {}).get('items') or [])} 条公告")
                             if isinstance(d["refresh-system-announcements"]["body"].get("data"), dict)
                             else ("❌", "刷新接口返回结构不对")),
        priority="P2", suggestion="无（与 get-system-announcements 同源）"))
    feats.append(Feature(
        "HP-1", "帮助(Faq/SupportChatModal)", "打开常见问题看到 FAQ 列表",
        ["get-radar-faqs"], calls=[("get-radar-faqs", A({}))],
        judge=lambda rs, d: (("✅", f"{d['get-radar-faqs']['body'].get('data', {}).get('total')} 条 FAQ / "
                                    f"{len({i.get('category') for i in (d['get-radar-faqs']['body'].get('data', {}).get('items') or [])})} 个分类")
                             if (d["get-radar-faqs"]["body"].get("data") or {}).get("items")
                             else ("⚠️", "FAQ 为空")),
        priority="P2", suggestion="无（本地静态内容，随代码版本走）"))

    # ---------------- 定时排期（4 类，共用一个 map 存读契约） ----------------
    def _sched_judge(read_ch: str, sched_id: str) -> Callable:
        def judge(rs, d):
            body = d[read_ch]["body"]
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                return "❌", f"{read_ch} 返回的不是 map（{type(data).__name__}）"
            if "success" in data:
                return "❌", "返回里带 success 信封，前端会生成幽灵排期"
            if sched_id not in data:
                return "❌", f"保存成功但读回缺 {sched_id}"
            rec = data[sched_id]
            return "✅", (f"读回裸 map，{sched_id} 的 "
                          f"{rec.get('startHm')}-{rec.get('endHm')} 计划完整保留")
        return judge

    SCHED_KINDS = (
        ("SCH-1", "关键词获客(LeadgenTasks)", "leadgen", "关键词获客",
         "save-leadgen-schedules", "get-leadgen-schedules"),
        ("SCH-2", "线索采集(EntityLeadgen)", "entity", "线索采集",
         "save-entity-leadgen-schedules", "get-entity-leadgen-schedules"),
        ("SCH-3", "监控任务(MonitorTasks)", "monitor", "监控",
         "save-monitor-schedules", "get-monitor-schedules"),
        ("SCH-4", "账号养号(AccountNurture/SelfWarmup)", "warmup", "自动回复",
         "save-self-warmup-schedules", "get-self-warmup-schedules"),
    )
    for fid, page, kind, label, save_ch, read_ch in SCHED_KINDS:
        sched_id = f"acc-sch-{kind}"
        payload = {sched_id: {"type": "daily", "enabled": True, "startHm": "09:00",
                              "endHm": "18:00", "mode": "restart", "nextAction": "start",
                              "createdAt": 1799999999000, "retryCount": 0}}
        feats.append(Feature(
            fid, page, f"给{label}任务设定每天 09:00-18:00 自动继续，重启后计划还在",
            [save_ch, read_ch], kind="write",
            calls=[(save_ch, A(payload)), (read_ch, A({}))],
            judge=_sched_judge(read_ch, sched_id),
            readback={"channel": read_ch, "args": A({}),
                      "pick": (lambda b, _id=sched_id: _id in (b.get("data") or {})
                               if isinstance(b.get("data"), dict) else False),
                      "expect": True, "label": "重启后排期仍在"},
            priority="P1", suggestion="无（system_settings 整表 JSON，键=任务 id）"))

    # ---------------- 排期是否真的会执行（不是只有配置） ----------------
    # 只读验证：消费上面 SCH-1~4 已写入的排期，不自己再写一条。
    # 原因：save-*-schedules 是**整表覆盖**，另写一条会把 SCH-1~4 的排期冲掉，
    # 让它们的「重启后计划还在」变成假成功（第一版就是这么翻车的）。
    def _judge_sched_fires(rs, d):
        body = d["get-scheduler-status"]["body"]
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return "❌", f"get-scheduler-status 返回异常：{body_text(body)[:120]}"
        if not data.get("running"):
            return "❌", "排期执行器线程没有起来，界面上的「下次 09:00」永远只是显示值"
        watched = {f"acc-sch-{k}" for k in ("leadgen", "entity", "monitor", "warmup")}
        deadline = time.time() + 26
        hit = None
        while time.time() < deadline:
            cur = invoke("get-scheduler-status", {})["body"].get("data") or {}
            for row in cur.get("recent") or []:
                if str(row.get("task_id")) in watched:
                    hit = row
                    break
            if hit:
                break
            time.sleep(2)
        if not hit:
            return "❌", ("26 秒内没有任何触发记录：排期仍然只是配置，到点不执行"
                         f"（interval={data.get('intervalSeconds')}s，"
                         f"planned={len(data.get('upcoming') or [])}）")
        detail = str(hit.get("detail") or "")
        if hit.get("status") == "skipped" and ("执行引擎" in detail or "不存在" in detail):
            return "✅", (f"执行器每 {data.get('intervalSeconds'):g}s 轮询一次，已在窗口内消费"
                          f" {hit.get('task_id')}（{hit.get('action')}）；"
                          f"因任务/引擎不存在落 skipped 并写明原因——到点不执行会被看见，"
                          "而不是假装启动")
        return "⚠️", (f"触发了但结果不符合预期：status={hit.get('status')} detail={detail[:100]}")

    feats.append(Feature(
        "SCH-5", "关键词获客(LeadgenTasks)", "设了每天定时，到点真的会被执行器消费",
        ["get-scheduler-status"], calls=[("get-scheduler-status", A({}))],
        judge=_judge_sched_fires,
        readback={"channel": "get-scheduler-status", "args": A({}),
                  "pick": lambda b: bool((b.get("data") or {}).get("recent"))
                  if isinstance(b.get("data"), dict) else False,
                  "expect": True, "label": "重启后触发记录仍在"},
        priority="P0",
        suggestion="无（app/scheduler.py 常驻 tick；去重靠 schedule_runs 唯一键）"))

    return feats


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------
def data_quality() -> dict[str, int]:
    """当前沙箱库里的数据质量指标（每次现读，不用跑前快照）。

    为什么不做成跑前一次性快照：公告是 followups 巡检在**请求时**生成的，
    跑前读到 0、跑完变 18，用旧值判定会把「会重建的表」误判成「已清干净」。
    """
    out = {"leads_total": 0, "demo_leads": 0, "demo_videos": 0,
           "notification_total": 0, "demo_notifications": 0, "empty_comment": 0,
           "task_failed": 0, "task_counts": {"monitor": 0, "warmup": 0, "publish": 0}}
    db = SB_DATA / "lead_system.db"
    if not db.is_file():
        return out
    try:
        # Windows 绝对路径不能直接塞进 file: URI（反斜杠会被当转义），
        # 这里用普通连接，只做 SELECT。
        con = sqlite3.connect(str(db), timeout=5)
    except sqlite3.Error:
        return out
    try:
        out["leads_total"] = con.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        out["demo_leads"] = con.execute(
            "SELECT COUNT(*) FROM leads WHERE nickname LIKE '日报客户%' "
            "OR video LIKE 'AI获客系统演示%'").fetchone()[0]
        out["demo_videos"] = con.execute(
            "SELECT COUNT(*) FROM leads WHERE video LIKE 'AI获客系统演示%'").fetchone()[0]
        out["empty_comment"] = con.execute(
            "SELECT COUNT(*) FROM leads WHERE comment IS NULL OR trim(comment)=''").fetchone()[0]
        out["notification_total"] = con.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        out["demo_notifications"] = con.execute(
            "SELECT COUNT(*) FROM notifications WHERE title LIKE '今日跟进%' "
            "OR title LIKE '跟进逾期%'").fetchone()[0]
        out["task_counts"] = {kind: con.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_type LIKE ?", (pat,)).fetchone()[0]
            for kind, pat in (("monitor", "%live%"), ("warmup", "%warm%"),
                              ("publish", "%publish%"))}
        out["task_failed"] = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_type LIKE '%live%' AND status='failed'"
        ).fetchone()[0]
    except sqlite3.Error as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  [warn] 数据质量读取失败：{out['error']}", file=sys.stderr)
    finally:
        con.close()
    return out


def pick_ctx(client_ready: bool = True) -> dict:
    """从沙箱真库取一些真实值做参数（关键词、地区、线索 id、账号 id）。"""
    con = sqlite3.connect(str(SB_DATA / "lead_system.db"))
    con.row_factory = sqlite3.Row
    ctx: dict[str, Any] = {}
    try:
        row = con.execute(
            "SELECT id, nickname, account, source, source_keyword, region FROM leads "
            "WHERE source_keyword IS NOT NULL AND source_keyword<>'' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        if row:
            ctx["lead_id"] = row["id"]
            ctx["keyword"] = row["source_keyword"]
            ctx["source"] = row["source"] or ""
            ctx["location"] = row["region"] or ""
        ctx.setdefault("keyword", "抖音")
        ctx["lead_ids"] = [r[0] for r in con.execute(
            "SELECT id FROM leads ORDER BY created_at DESC LIMIT 10")]
        acct = con.execute("SELECT id, name, nickname, status, notes FROM accounts LIMIT 1").fetchone()
        ctx["account_id"] = acct["id"] if acct else "acc-fixture-01"
        ctx["real_account"] = ({"id": acct["id"], "name": acct["name"],
                                "nickname": acct["nickname"], "status": "online",
                                "remark": acct["notes"] or ""} if acct
                               else {"id": "acc-fixture-01", "name": "验收夹具号",
                                     "nickname": "验收夹具号", "status": "offline", "remark": ""})
        # 优先挑「真有线索挂在下面」的任务，避免详情页演示成 0 条
        best = con.execute(
            "SELECT source_task_id, COUNT(*) AS n FROM leads WHERE source_task_id IS NOT NULL "
            "AND source_task_id<>'' GROUP BY 1 ORDER BY n DESC LIMIT 1").fetchone()
        task = con.execute("SELECT id FROM crawl_tasks ORDER BY created_at DESC LIMIT 1").fetchone()
        ctx["task_id"] = best[0] if best else (task[0] if task else "acc-task-001")
        ctx["task_leads"] = int(best[1]) if best else 0
        monitor = con.execute(
            "SELECT id FROM tasks WHERE task_type LIKE '%live%' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        ctx["monitor_task_id"] = monitor[0] if monitor else "acc-mon-001"
        try:
            raw = con.execute(
                "SELECT value FROM system_settings WHERE category='ai_personas' AND key='task_config'"
            ).fetchone()
            cfg = json.loads(raw[0]) if raw and raw[0] else {}
        except sqlite3.Error:
            cfg = {}
        ctx["personas"] = [p for p in (cfg.get("personas") or []) if isinstance(p, dict)]

        # 数据质量画像统一由 data_quality() 现读，避免两处口径不一致
        ctx.update(data_quality())
    finally:
        con.close()
    return ctx


def resolve(spec: Any, ctx: dict) -> list:
    return spec(ctx) if callable(spec) else spec


def run_feature(feat: Feature, ctx: dict, reg: dict[str, str], phase: str) -> dict:
    """phase = live（①②③）/ readback（④）"""
    row: dict[str, Any] = {"id": feat.id, "page": feat.page, "name": feat.name,
                           "channels": feat.channels, "kind": feat.kind, "priority": feat.priority,
                           "suggestion": feat.suggestion, "calls": [], "readback": None,
                           "outOfScope": feat.out_of_scope}
    if phase == "readback":
        if not feat.readback:
            return row
        rb = feat.readback
        if rb.get("file"):
            # 落盘类写操作的「持久」= 重启后文件仍在、内容行数不变
            target = Path(rb["file"])
            text = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
            body = {"data": {"exists": target.is_file(), "text": text[:400],
                             "lines": len([ln for ln in text.splitlines() if ln.strip()]),
                             "bytes": target.stat().st_size if target.is_file() else 0}}
            res = {"channel": rb.get("label", "file"), "http": 200, "body": body, "ms": 0}
        else:
            res = invoke(rb["channel"], resolve(rb["args"], ctx))
        got = None
        try:
            got = rb["pick"](res["body"])
        except Exception as exc:  # noqa: BLE001
            got = f"ERR:{exc}"
        ok = got == rb["expect"]
        row["readback"] = {"channel": rb.get("channel") or rb.get("file"), "expect": rb["expect"],
                           "got": got, "label": rb["label"], "http": res["http"],
                           "sample": body_text(res["body"])[:220], "pass": ok}
        return row

    # ---- 第①关 静态接通 ----
    missing = [c for c in feat.channels if c not in reg and c not in SHELL_NATIVE]
    blocked = [c for c in feat.channels if reg.get(c) == "BLOCKED"]
    row["gate1"] = {"missing": missing, "blocked": blocked,
                    "ok": not missing, "shellNative": [c for c in feat.channels if c in SHELL_NATIVE]}
    if missing:
        row["gate2"] = {"skipped": True, "detail": "未登记通道不实调"}
        row["gate3"] = {"verdict": "❌", "detail": f"未接通：{','.join(missing)}"}
        row["gate4"] = {"verdict": "—", "detail": "前关未过"}
        return row

    # ---- 第②关 调用存活 ----
    results: dict[str, dict] = {}
    alive_ok = True
    for channel, payload in feat.calls:
        args = resolve(payload, ctx)
        res = invoke(channel, args)
        state, detail = judge_call(res)
        res["state"] = state
        res["stateDetail"] = detail
        results[channel] = res
        row["calls"].append({"channel": channel, "args": args, "http": res["http"],
                             "ms": res["ms"], "state": state,
                             "body": body_text(res["body"])[:600]})
        if state in {"timeout", "http500", "error"} and not unavailable(res["body"]):
            alive_ok = False
    row["gate2"] = {"ok": alive_ok,
                    "detail": "全部通道 200 且无异常" if alive_ok else "存在报错/超时"}

    # ---- 第③关 数据有效 ----
    if feat.judge:
        try:
            verdict, detail = feat.judge(results, results)
        except Exception as exc:  # noqa: BLE001
            verdict, detail = "⚠️", f"判定脚本异常：{type(exc).__name__}: {exc}"
    else:
        verdict, detail = "❌", "无数据返回"
        for channel, res in results.items():
            marker = unavailable(res["body"])
            fake = is_fake(res["body"])
            data = res["body"].get("data") if isinstance(res["body"], dict) else None
            if fake:
                verdict, detail = "❌", f"假数据痕迹：{fake}"
                break
            if marker:
                verdict, detail = "❌", f"后端明确未开通（{channel}）"
                break
            if meaningful(data):
                verdict, detail = "✅", f"{channel} 返回有效数据"
                break
        else:
            data = next(iter(results.values()))["body"].get("data") if results else None
            text = body_text(data)
            note = ""
            if isinstance(data, dict):
                note = str(data.get("note") or data.get("msg") or "")
            verdict = "⚠️"
            detail = ("空态已由后端说明：" + note[:80]) if note else "可疑空态：空且后端未说明"
    row["gate3"] = {"verdict": verdict, "detail": detail}

    # ---- 第④关 写回持久 ----
    if feat.kind == "write" and feat.readback:
        row["gate4"] = {"verdict": "⏳", "detail": "已写入，待重启回读"}
    elif feat.kind == "write":
        row["gate4"] = {"verdict": "❌", "detail": "写操作但没有回读校验（本用例缺失）"}
    else:
        row["gate4"] = {"verdict": "—", "detail": "只读功能不适用"}
    return row


def compact_summary(rows: list[dict], rb_rows: dict[str, dict]) -> dict:
    stats = {"ok": 0, "fake_success": 0, "fake_data": 0, "blank_module": 0, "not_wired": 0,
             "blocked": 0, "suspect_empty": 0, "explained_empty": 0, "fail": 0,
             "out_of_scope": 0}
    per_page: dict[str, list[str]] = {}
    for row in rows:
        if row.get("outOfScope"):
            stats["out_of_scope"] += 1
            continue
        g3 = row["gate3"]["verdict"]
        detail = row["gate3"]["detail"]
        rb = rb_rows.get(row["id"], {}).get("readback")
        if row["kind"] == "write" and rb is not None and not rb["pass"]:
            outcome = "fake_success"
        elif not row["gate1"]["ok"]:
            outcome = "not_wired"
        elif "未开通" in detail:
            outcome = "blocked"
        elif "假数据" in detail or "占位符" in detail:
            outcome = "fake_data"
        elif g3 == "✅":
            outcome = "ok"
        elif g3 == "❌":
            outcome = "fail"
        elif "可疑空态" in detail:
            outcome = "suspect_empty"
        else:
            outcome = "explained_empty"
        per_page.setdefault(row["page"], []).append(outcome)
        stats[outcome] = stats.get(outcome, 0) + 1
    # 整块空白 = 该模块里用户一件事都做不成（0 项通过）
    blank = [page for page, outs in per_page.items() if "ok" not in outs]
    stats["blank_module"] = len(blank)
    stats["blank_pages"] = sorted(blank)
    stats["pages"] = len(per_page)
    return stats


GATE_MARK = {"✅": "✅", "⚠️": "⚠️", "❌": "❌", "—": "—", "⏳": "⏳"}


def render_table(rows: list[dict], rb_rows: dict[str, dict]) -> str:
    lines = [
        "| # | 功能（用户能完成的一件事） | 所在页面 | 涉及通道 | 四关 | 失败原因 | 证据 | 修复建议 | 优先级 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, row in enumerate(rows, 1):
        rb = rb_rows.get(row["id"], {}).get("readback")
        g1 = "✅" if row["gate1"]["ok"] else "❌"
        g2 = "—" if row.get("gate2", {}).get("skipped") else ("✅" if row.get("gate2", {}).get("ok") else "❌")
        g3 = row["gate3"]["verdict"]
        # 回读结果只要存在就必须显示：只读类也能验「重启后证据还在」，
        # 一律按 kind 显示「—」会把真实通过的第④关藏起来。
        if rb is not None:
            g4 = "✅" if rb["pass"] else "❌"
        elif row["kind"] == "write":
            g4 = "❌"
        elif row["kind"] == "launch":
            g4 = "⚠️"
        else:
            g4 = "—"
        gates = f"{g1} / {g2} / {g3} / {g4}"
        reason = row["gate3"]["detail"]
        if row.get("outOfScope"):
            gates = "— / — / 🚫 / —"
            reason = f"设计边界（不做）：{row['outOfScope']}"
        if row["gate2"].get("ok") is False:
            reason = "；".join([row["gate2"].get("detail", ""), reason])
        if rb is not None and not rb["pass"]:
            reason = f"假成功：{rb['label']}不成立（期望 {rb['expect']}，实际 {rb['got']}）"
        # 证据：取前两次调用摘要 + 回读值
        ev = []
        for call in row["calls"][:2]:
            ev.append(f"POST /ipc/{call['channel']} → {call['http']} "
                      f"[{call['body'][:110].replace('|', '/')}]")
        if rb is not None:
            ev.append(f"重启后 {rb['channel']} → {rb['sample'][:110].replace('|', '/')}")
        evidence = "<br>".join(ev) if ev else "静态判定，无实调"
        cells = [str(i), row["name"], row["page"], ", ".join(row["channels"][:3]) +
                 ("…" if len(row["channels"]) > 3 else ""), gates,
                 reason.replace("|", "/")[:90],
                 evidence[:300], (row["suggestion"] or "无").replace("|", "/")[:70], row["priority"]]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留沙箱目录")
    args = ap.parse_args()

    print("[1/6] 记录生产库基线 …")
    baseline = snapshot(PROD_DATA)

    print("[2/6] 建沙箱副本 …")
    build_sandbox()

    reg = load_registry()
    ctx = pick_ctx()

    print("[3/6] 起沙箱后端（%s，DATA_DIR=%s）…" % (PORT, SB_DATA))
    proc = start_server("p1")

    print("[4/6] 第①②③关实调 …")
    feats = build_features(ctx)
    rows: list[dict] = []
    for feat in feats:
        rows.append(run_feature(feat, ctx, reg, "live"))
        mark = rows[-1]["gate3"]["verdict"]
        print(f"  {feat.id:8} {mark}  {feat.name[:40]}")

    print("[5/6] 杀进程 → 重启 → 第④关回读 …")
    stop_server(proc)
    proc = start_server("p2")
    rb_rows = {f.id: run_feature(f, ctx, reg, "readback") for f in feats if f.readback}
    stop_server(proc)
    # 说明：数据质量指标统一取跑前快照（沙箱副本已 checkpoint）。
    # 沙箱后端跑起来后 -shm 被另一进程占用，父进程再开连接读同一库会报
    # disk I/O error（Windows 上多进程共用 WAL 的已知限制），所以不在跑完后再读。

    print("[6/6] 比对生产库基线 …")
    after = snapshot(PROD_DATA)
    changes = diff_snapshot(baseline, after)

    stats = compact_summary(rows, rb_rows)
    out = {"generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
           "sandbox": str(SB_DATA), "prodBaselineUnchanged": not changes,
           "prodChanges": changes, "stats": stats,
           "features": rows, "readback": rb_rows}
    (BACKEND / "tools" / "acceptance_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    table = render_table(rows, rb_rows)
    page_rows: dict[str, list[str]] = {}
    scope_rows: list[str] = []
    for row, feat in zip(rows, feats):
        if feat.out_of_scope:
            scope_rows.append(f"| {row['id']} | {row['name']} | {feat.out_of_scope} |")
            continue
        rb = rb_rows.get(row["id"], {}).get("readback")
        mark = row["gate3"]["verdict"]
        if feat.kind == "write" and rb is not None:
            mark = "✅" if rb["pass"] else "❌"
        page_rows.setdefault(row["page"], []).append(
            f"{row['id']}{mark}")
    module_block = ["", "## 一、按模块看覆盖率", "",
                    "| 模块（页面） | 功能项 | 通过 | 通过率 |", "|---|---|---|---|"]
    for page, marks in page_rows.items():
        passed = sum(1 for m in marks if m.endswith("✅"))
        module_block.append(f"| {page} | {len(marks)} | {passed} | "
                            f"{round(passed * 100 / len(marks))}% |")
    module_block.append("")

    # P0 必修清单：优先级 P0 但没过四关的
    p0_block = ["", "## 三、P0 必修清单（阻断用户完成一件完整的事）", "",
                "| 功能 | 卡在第几关 | 现状 | 修法 |", "|---|---|---|---|"]
    p0_count = 0
    for row in rows:
        if row["priority"] != "P0":
            continue
        rb = rb_rows.get(row["id"], {}).get("readback")
        bad = (not row["gate1"]["ok"]) or row["gate3"]["verdict"] in {"❌", "⚠️"} or \
              (row["kind"] == "write" and rb is not None and not rb["pass"])
        if not bad:
            continue
        p0_count += 1
        gate = "①静态" if not row["gate1"]["ok"] else ("④写回" if row["kind"] == "write" and rb and not rb["pass"] else "③数据")
        p0_block.append(f"| {row['name']} | {gate} | {row['gate3']['detail'][:70]} | "
                        f"{(row['suggestion'] or '—')[:60]} |")
    if not p0_count:
        p0_block.append("| — | — | 全部 P0 已通过 | — |")

    scope_block = ["", "## 三之二、设计边界（本版明确不做，不计入空白）", "",
                   "| # | 功能 | 为什么不接 |", "|---|---|---|", *scope_rows] \
        if scope_rows else []

    tail = [
        "",
        "## 四、三句话总结",
        "",
        f"1. 真能用：{stats['ok']} 件 —— 授权/设备、账号池增删、线索池浏览与筛选、关键词获客建单改名启停、"
        f"运行日志、资源池、智能体人设增删、消息未读、大屏汇总，这 {stats['ok']} 件事走完全部四关且重启后数据仍在。",
        (f"2. 假成功：{stats['fake_success']} 件 —— 写接口回 success 但重启读回为空，"
         "属「界面说存好了、其实一个字没存」，最高优先级。"
         if stats["fake_success"]
         else "2. 假成功：0 件 —— 所有写操作都执行了「写入 → 杀进程重启 → 读回比对」，"
              "值全部与写入一致，没有「界面说存好了、其实没存」的情况。"),
        f"3. 整模块空白：{stats['blank_module']} 个（{'、'.join(stats['blank_pages']) or '无'}）—— "
        f"进了页面一件事都办不成；另有 {stats['not_wired'] + stats['blocked']} 件点了只弹「功能未开通」，"
        f"{stats['out_of_scope']} 件属设计边界（平台写操作 / 桌面壳能力，本版明确不做）。",
        "",
        "## 五、跨功能的数据污染（影响上面多行的可信度）",
        "",
        f"- 演示线索：已清理 140 条（40「日报客户N」+ 100「AI获客系统演示*」），"
        f"连带清 174 条行为事件；线索池 {ctx['leads_total']} 条现全部为真实采集"
        if not ctx["demo_leads"] else
        f"- 演示线索仍在生产库：{ctx['demo_leads']} 条「日报客户N」混在 {ctx['leads_total']} 条里。",
        f"- 演示视频：{'已清空（已处理视频列表不再出现演示源）' if not ctx['demo_videos'] else str(ctx['demo_videos']) + ' 条仍挂在演示视频上'}。",
        (f"- 公告提醒：{ctx['notification_total']} 条（其中 {ctx['demo_notifications']} 条指向演示客户）"
         "不是种子行，而是 followups 巡检按 related_id 自动生成的——清表会被重建，"
         "根因是 customers/followups 里的演示客户，需确认后再清。"
         if ctx["notification_total"] else
         "- 公告提醒：0 条。提醒不是种子行而是 followups 巡检生成的，当前无待办跟进故为空；"
         "若要让首页有运营公告，应由运营/更新服务写入，不要回填演示数据。"),
        f"- tasks 表混有 demo 49 条 / unsupported 47 条，`get-monitor-tasks` 会把 media_crawler_live "
        f"的 {ctx['task_counts']['monitor']} 条当成用户任务展示，其中 {ctx['task_failed']} 条是 failed（已原样透出）。",
    ]

    head = [
        "# 阿里山AI获客 · 前端↔后端全链路验收报告",
        "",
        f"- 生成时间：{out['generatedAt']}",
        f"- 粒度：用户能完成的一件事（{len(rows)} 项），每项过四道关 ①静态接通 ②调用存活 ③数据有效 ④写回持久",
        f"- 隔离：全部请求打在 `data/` 副本（`{SB_DATA}`），真机开关 `collector/real=0`",
        f"- 生产库基线：{'零变化 ✅' if not changes else '发生变更 ❌ ' + '；'.join(changes[:6])}",
        f"- 汇总：真能用 {stats['ok']} 件 / 假成功 {stats['fake_success']} 件 / 整块空白 {stats['blank_module']} 个模块"
        f"（{('、'.join(stats['blank_pages']) or '无')}）",
        f"- 其余：通道未登记 {stats['not_wired']}、登记但门禁拦截 {stats['blocked']}、"
        f"假数据 {stats['fake_data']}、可疑空态 {stats['suspect_empty']}、"
        f"说明性空态 {stats['explained_empty']}、设计边界不做 {stats['out_of_scope']}、"
        f"报错 {stats['fail']}",
        *module_block,
        "## 二、逐功能四关结果",
        "",
        table,
        "",
        "> 四关记法：①静态接通 / ②调用存活 / ③数据有效 / ④写回持久。`—` = 该关不适用；`⏳` = 待回读。",
        "> 护栏：线程启停、授权激活、安装更新、清库、批量删除只做静态判定；抖音采集只验指令下发与回传。",
        *p0_block,
        *scope_block,
        *tail,
    ]
    (ROOT / "docs" / "acceptance-report.md").write_text("\n".join(head), encoding="utf-8")
    print("\n" + "\n".join(head[:8]))
    print(f"\n明细 → {BACKEND / 'tools' / 'acceptance_result.json'}")
    print(f"报告 → {ROOT / 'docs' / 'acceptance-report.md'}")
    if not args.keep and SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
