"""账号池 · 抖音 CK（Cookie）导入 / 导出。

为什么需要单独一层
------------------
抖音的登录态就是一组 Cookie。桌面壳里每个账号有自己的 Electron 分区
（`session-partition.resolvePartition`），登录态只存在于那个分区里；后端自己
读不到 httpOnly 的 sessionid。所以「粘贴 CK 登录」这件事必须两段：
后端负责解析/校验/落库，壳负责把 Cookie 真的写进对应分区，再由 check-login
验证「这组 CK 到底还能不能用」。

宁可失败，也不假装成功
----------------------
- 解析不出 sessionid / sessionid_ss / sid_guard 的文本一律判为无效 CK
  （只有 ttwid / odin_tt 的游客 Cookie 写进去也只会是登录墙）；
- 壳没运行 → 明确报「桌面壳未运行，CK 无法写入登录态分区」，**不写库冒充导入成功**；
- 导入成功后如果 `skipVerify` 为真，返回 `verified:false`，绝不说「已登录」。

落库口径
--------
Cookie 原文存 `account_cookies` 表（一账号一行），不塞进 accounts 的常用字段，
避免账号列表查询顺手把敏感串带到前端。导出优先读壳分区里的**当前** Cookie
（登录后续期/token 刷新只有壳知道），壳不可用时才回落到导入时的快照，并
在返回里注明来源。
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

# 会话票据：少了这些，写进去也只是游客身份
SESSION_KEYS = ("sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "passport_auth_status")
# 以下属于「有更好、没有也能用」的辅助票据
OPTIONAL_KEYS = ("ttwid", "odin_tt", "passport_csrf_token", "msToken", "s_v_web_id", "UIFID")

MAX_TEXT_BYTES = 2 * 1024 * 1024      # 单个 CK 文件/粘贴文本上限
MAX_BATCH_ITEMS = 200                  # 一次批量导入的账号数上限
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_\-.]{1,64}$")
_SEP_LINE = re.compile(r"^\s*-{3,}\s*$")
_LABEL_LINE = re.compile(r"^\s*(?:#|//)\s*(.{1,40}?)\s*$")


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def _clean_pairs(raw_items: Iterable[Any]) -> list[dict]:
    """把 {name,value} 列表规范化；同名后者覆盖，非法名丢弃。"""
    out: dict[str, dict] = {}
    for item in raw_items:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "")
            if not name or not _COOKIE_NAME.match(name):
                continue
            rec = {"name": name, "value": value}
            for key in ("domain", "path"):
                val = item.get(key)
                if val:
                    rec[key] = str(val)
            if item.get("secure") is not None:
                rec["secure"] = bool(item.get("secure"))
            if item.get("httpOnly") is not None:
                rec["httpOnly"] = bool(item.get("httpOnly"))
            exp = item.get("expirationDate") or item.get("expires")
            if exp:
                try:
                    rec["expirationDate"] = float(exp)
                except (TypeError, ValueError):
                    pass
            rec.setdefault("domain", ".douyin.com")
            rec.setdefault("path", "/")
            out[name] = rec
        elif isinstance(item, str) and "=" in item:
            name, _, value = item.partition("=")
            name = name.strip().lstrip(";").strip()
            if name and _COOKIE_NAME.match(name):
                out[name] = {"name": name, "value": value.strip(),
                             "domain": ".douyin.com", "path": "/"}
    return list(out.values())


def parse_cookie_text(text: Any) -> tuple[list[dict], str]:
    """解析一段 CK 文本 → (cookies, 错误说明)。支持三种写法：

    1. `Cookie: name=value; name2=value2`（浏览器 DevTools 复制）
    2. `name=value; name2=value2`（裸串，也接受每行一对）
    3. Cookie JSON：`[{name,value,...}]` / `{"cookies":[...]}` / `{"name":"value"}`
    """
    raw = str(text or "").strip()
    if not raw:
        return [], "内容为空"
    if len(raw.encode("utf-8", "ignore")) > MAX_TEXT_BYTES:
        return [], "内容过大（上限 2MB）"

    # JSON 分支
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except ValueError:
            return [], "Cookie JSON 解析失败（括号/引号不完整）"
        items: list[Any] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, Mapping):
            if isinstance(data.get("cookies"), list):
                items = data["cookies"]
            elif all(isinstance(v, (str, int, float)) for v in data.values()):
                items = [{"name": k, "value": v} for k, v in data.items()]
        cookies = _clean_pairs(items)
        if not cookies:
            return [], "JSON 里没有可用的 name/value 字段"
        return cookies, ""

    body = raw
    if body.lower().startswith("cookie:"):
        body = body[7:]
    # 去掉注释行（导出时写的 `# 备注`），保留 name=value
    body = "\n".join(line for line in body.splitlines()
                     if not _LABEL_LINE.match(line) or "=" in line)
    body = body.replace("\r", "")
    parts = re.split(r"[;\n]+", body)
    cookies = _clean_pairs([p for p in parts if p.strip()])
    if not cookies:
        return [], "未识别到 name=value 形式的 Cookie"
    return cookies, ""


def _has_session(cookies: Iterable[dict]) -> bool:
    names = {str(c.get("name") or "") for c in cookies}
    return any(k in names for k in ("sessionid", "sessionid_ss", "sid_guard", "sid_tt"))


def build_cookie_string(cookies: Iterable[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))


def split_batch(text: Any) -> list[tuple[str, str]]:
    """批量文本切片。

    分隔规则（按优先级）：整行 `---`；其次空行分块；再次「每个非空行是一条 CK」
    —— 最后这条是给「一行一个账号的 CK 列表」用的。
    """
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return []
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in raw.split("\n"):
        if _SEP_LINE.match(line):
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    if len(blocks) == 1:
        # 没有分隔线：先按空行切
        chunks, buf = [], []
        for line in blocks[0]:
            if line.strip():
                buf.append(line)
            elif buf:
                chunks.append(buf)
                buf = []
        if buf:
            chunks.append(buf)
        if len(chunks) > 1:
            blocks = chunks
        else:
            # 「一行一个账号」只在**每行自己就带会话票据**时才成立。
            # 仅凭「每行都是 name=value」会切错：一段从 DevTools 复制、按行折过的
            # Cookie 头会被切成十几块残缺 CK，导入后全是无效账号。
            lines = [l.strip() for l in chunks[0] if l.strip() and not _LABEL_LINE.match(l)]
            if len(lines) > 1 and all(
                    any(k + "=" in l for k in ("sessionid", "sid_guard", "sid_tt")) for l in lines):
                blocks = [[l] for l in lines]

    out: list[tuple[str, str]] = []
    for block in blocks:
        label = ""
        body: list[str] = []
        for line in block:
            m = _LABEL_LINE.match(line)
            if m and "=" not in line:
                label = label or m.group(1)
                continue
            body.append(line)
        text_block = "\n".join(body).strip()
        if text_block:
            out.append((label, text_block))
    return out


# --------------------------------------------------------------------------
# 落库
# --------------------------------------------------------------------------
def _connect(db_path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def _ensure_table(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS account_cookies (
               account_id TEXT PRIMARY KEY,
               cookie_json TEXT NOT NULL DEFAULT '[]',
               cookie_count INTEGER NOT NULL DEFAULT 0,
               source TEXT NOT NULL DEFAULT 'paste',
               imported_at TEXT NOT NULL DEFAULT '',
               updated_at TEXT NOT NULL DEFAULT ''
           )""")
    con.commit()


def save_snapshot(db_path: Path | str, account_id: str, cookies: list[dict],
                  source: str = "paste") -> None:
    import time
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    con = _connect(db_path)
    try:
        _ensure_table(con)
        con.execute(
            "INSERT INTO account_cookies(account_id,cookie_json,cookie_count,source,imported_at,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
            "cookie_json=excluded.cookie_json, cookie_count=excluded.cookie_count, "
            "source=excluded.source, updated_at=excluded.updated_at",
            (account_id, json.dumps(cookies, ensure_ascii=False), len(cookies), source, stamp, stamp))
        con.commit()
    finally:
        con.close()


def load_snapshot(db_path: Path | str, account_id: str) -> list[dict]:
    con = _connect(db_path)
    try:
        _ensure_table(con)
        row = con.execute("SELECT cookie_json FROM account_cookies WHERE account_id=?",
                          (account_id,)).fetchone()
        if not row:
            return []
        data = json.loads(row["cookie_json"] or "[]")
        return [c for c in data if isinstance(c, dict) and c.get("name")]
    except (sqlite3.Error, ValueError):
        return []
    finally:
        con.close()


def _ensure_account(db_path: Path | str, account_id: str, name: str = "") -> None:
    con = _connect(db_path)
    try:
        row = con.execute("SELECT id FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row:
            return
        con.execute(
            "INSERT INTO accounts(id,name,nickname,platform,status,notes,health_score,"
            "limit_status,daily_outreach,daily_limit,created_at,updated_at) "
            "VALUES(?,?,?,?,'idle','',100,'healthy',0,80,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (account_id, name or f"CK导入-{account_id[-6:]}", "", "douyin"))
        con.commit()
    finally:
        con.close()


# --------------------------------------------------------------------------
# 壳通道
# --------------------------------------------------------------------------
def _shell(action: str, timeout: float = 20.0, **extra) -> dict | None:
    from app.api import ipc_bridge
    return ipc_bridge._shell_task(action, timeout=timeout, **extra)


def _db():
    """和别的通道用同一个库：测试与运行时都由 ipc_bridge.DB_PATH 决定。"""
    from app.api import ipc_bridge
    return ipc_bridge.DB_PATH


def _payload(raw: Any) -> Mapping:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, list) and raw and isinstance(raw[0], Mapping):
        return raw[0]
    return {}


def parse_batch(payload: Any) -> dict:
    """parse-douyin-cookie-batch-text：预览批量文本会拆成哪些账号。"""
    data = _payload(payload)
    blocks = split_batch(data.get("text"))
    if not blocks:
        return {"ok": False, "message": "没有识别到 CK 内容", "items": []}
    items: list[dict] = []
    for index, (label, text_block) in enumerate(blocks[:MAX_BATCH_ITEMS], start=1):
        cookies, error = parse_cookie_text(text_block)
        if not error and not _has_session(cookies):
            error = "缺少 sessionid（不是抖音登录态）"
        item = {
            "label": (label or f"CK导入-{index}")[:40],
            "cookieText": text_block,
            "count": len(cookies),
        }
        if error:
            item["error"] = error
        else:
            names = {c["name"] for c in cookies}
            item["sessionKeys"] = sorted(names & set(SESSION_KEYS))
        items.append(item)
    ok_count = sum(1 for i in items if not i.get("error"))
    return {
        "ok": ok_count > 0,
        "items": items,
        "total": len(items),
        "valid": ok_count,
        "invalid": len(items) - ok_count,
        "message": "" if ok_count else "没有识别到有效 CK",
    }


def import_session(payload: Any) -> dict:
    """import-douyin-cookie-session：把 CK 写进账号分区的登录态。

    前端契约：`{accountId, cookieText, previousStatus, proxy, skipVerify}`
    返回 `{ok, count, status, nickname, verified, message}`
    """
    data = _payload(payload)
    from app.api import account_pool

    DB_PATH = _db()

    account_id = str(data.get("accountId") or "").strip()
    if not account_id:
        return {"ok": False, "message": "缺少账号 id"}
    cookies, error = parse_cookie_text(data.get("cookieText"))
    if error:
        return {"ok": False, "message": f"CK 解析失败：{error}"}
    if not _has_session(cookies):
        return {"ok": False,
                "message": "这组 Cookie 里没有 sessionid/sessionid_ss/sid_guard，只有游客票据，无法登录"}

    _ensure_account(DB_PATH, account_id, str(data.get("name") or ""))

    # 1) 先写壳分区：写不进去就等于没登录，绝不下沉成「导入成功」
    applied = _shell("apply-cookies", timeout=25.0, accountId=account_id, cookies=cookies)
    if applied is None:
        return {"ok": False, "message": "桌面壳未响应，CK 没有写进登录态分区；请确认软件正在运行后重试"}
    if not applied.get("ok", False):
        return {"ok": False, "message": str(applied.get("error") or "CK 写入登录态分区失败")}
    save_snapshot(DB_PATH, account_id, cookies, source=str(data.get("source") or "paste"))

    count = len(cookies)
    if data.get("skipVerify") is True:
        # 不改库里的 status：没验证过就写「已登录」是撒谎，写「未登录」又会把
        # 之前真机验过的好状态抹掉。交给调用方随后的 check-account-pool-status 决定。
        return {"ok": True, "count": count, "verified": False, "status": "verifying",
                "applied": int(applied.get("applied") or count),
                "message": f"已写入 {count} 项 Cookie，待检测登录状态"}

    # 2) 真机验证：让壳用这个账号的分区打开抖音，看是不是登录态
    from app.runtime import browser
    try:
        res = browser.execute({"type": "check-login", "accountId": account_id},
                              db_path=DB_PATH, timeout=90, account_id=account_id)
    except Exception as exc:  # noqa: BLE001 - 检测失败不代表 CK 无效，如实回报
        res = {"ok": False, "error": str(exc)}
    if not res.get("ok"):
        return {"ok": True, "count": count, "verified": False, "status": "verifying",
                "message": f"已写入 {count} 项 Cookie；登录检测未完成：{res.get('error') or '壳未响应'}"}
    logged_in = bool(res.get("loggedIn"))
    status = "online" if logged_in else "offline"
    account_pool.set_login_status(DB_PATH, account_id, status, str(res.get("nickname") or ""))
    return {"ok": True, "count": count, "verified": True, "status": status,
            "nickname": str(res.get("nickname") or ""),
            "message": "CK 有效，登录状态正常" if logged_in else "CK 已写入，但该账号仍未登录（可能已过期）"}


def pick_files(_: Any = None) -> dict:
    """pick-douyin-cookie-import-files：壳里选文件并读文本。"""
    res = _shell("pick-cookie-files", timeout=120.0)
    if res is None:
        return {"ok": False, "message": "桌面壳未响应，无法打开文件选择框"}
    if res.get("canceled"):
        return {"ok": False, "canceled": True, "items": []}
    if not res.get("ok", False):
        return {"ok": False, "message": str(res.get("error") or "读取 CK 文件失败")}
    items: list[dict] = []
    for index, f in enumerate((res.get("files") or [])[:MAX_BATCH_ITEMS], start=1):
        name = str((f or {}).get("name") or f"CK文件-{index}")
        text = str((f or {}).get("text") or "")
        cookies, error = parse_cookie_text(text)
        if not error and not _has_session(cookies):
            error = "缺少 sessionid（不是抖音登录态）"
        label = re.sub(r"\.(txt|json|cookie)$", "", name)[:40] or f"CK导入-{index}"
        item = {"label": label, "fileName": name, "cookieText": text, "count": len(cookies)}
        if error:
            item["error"] = error
        items.append(item)
    ok_count = sum(1 for i in items if not i.get("error"))
    return {"ok": ok_count > 0, "items": items, "total": len(items),
            "message": "" if ok_count else "所选文件都无法识别为抖音 CK"}


def export_sessions(payload: Any) -> dict:
    """export-douyin-cookie-session：从壳分区导出「当前」Cookie 并复制到剪贴板。

    前端契约：单账号 `{accountId}` → `{ok,count,copied}`；
    批量 `{accountIds}` → `{ok,exported,failed,copied}`。
    """
    data = _payload(payload)
    DB_PATH = _db()

    raw_ids = data.get("accountIds") or data.get("accountId") or []
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    ids = [str(i).strip() for i in raw_ids if str(i or "").strip()]
    if not ids:
        return {"ok": False, "message": "没有指定要导出的账号"}
    single = len(ids) == 1

    con = _connect(DB_PATH)
    try:
        names = {str(r["id"]): str(r["name"] or "") for r in con.execute(
            "SELECT id,name FROM accounts").fetchall()}
    finally:
        con.close()

    blocks: list[str] = []
    exported = failed = total = 0
    used_snapshot = 0
    for account_id in ids:
        live = _shell("read-cookies", timeout=25.0, accountId=account_id)
        cookies: list[dict] = []
        if live and live.get("ok"):
            cookies, _err = parse_cookie_text(live.get("cookieText") or "")
        if not cookies:
            cookies = load_snapshot(DB_PATH, account_id)
            if cookies:
                used_snapshot += 1
        if not cookies:
            failed += 1
            continue
        text = build_cookie_string(cookies)
        if single:
            blocks.append(text)
        else:
            label = names.get(account_id) or account_id
            blocks.append(f"# {label}\n{text}")
        exported += 1
        total += len(cookies)

    if not exported:
        return {"ok": False, "message": "所选账号都没有可导出的 CK（壳里未登录且无导入快照）"}

    text_out = "\n---\n".join(blocks)
    copied = False
    if single or data.get("copy") is not False:
        res = _shell("copy-text", timeout=15.0, text=text_out)
        copied = bool(res and res.get("ok"))
    result = {
        "ok": True,
        "count": total,
        "exported": exported,
        "failed": failed,
        "copied": copied,
        "bytes": len(text_out.encode("utf-8", "ignore")),
        "source": "shell" if used_snapshot == 0 else ("snapshot" if used_snapshot == exported else "mixed"),
    }
    if used_snapshot:
        result["note"] = (f"{used_snapshot} 个账号的 CK 来自导入快照（壳分区里读不到当前 Cookie）")
    if not copied:
        result["message"] = "已导出文本，但写入剪贴板失败"
    return result
