"""常见问题 · 问题反馈落库。

为什么单独一层
--------------
前端 Faq 页的「问题反馈」只负责收集（描述 + 最多 3 张截图 + 联系方式）并
展示「问题已收到（编号 X）」；编号必须由后端生成并**真实落盘**，否则用户
拿到的编号无处可查，等于假回执。

存储口径与系统设置一致：`DATA_DIR/faq_feedback.json` 存索引，截图按
`DATA_DIR/faq_feedback/<编号>/1.png` 存原图。图片**不进 JSON**（base64 会把
索引撑到几十 MB，读写都变慢），只记文件名与字节数。

前端契约（ui/assets/Faq-*.js）
------------------------------
`submit-faq-question` 入参 `{requestId, content, contact, images:[{base64}]}`
返回 `{success: true, id}` 或 `{success: false, msg}`（msg 直接显示给用户）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.paths import DATA_DIR

FAQ_DIR: Path = Path(os.getenv("FAQ_FEEDBACK_DIR", str(DATA_DIR / "faq_feedback")))
INDEX_PATH: Path = FAQ_DIR / "faq_feedback.json"

MAX_IMAGES = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 与前端「≤3 张 / 5MB」一致
MAX_CONTENT_CHARS = 2000
MAX_CONTACT_CHARS = 120

_DATA_URL = re.compile(r"^data:image/(png|jpe?g|webp|gif);base64,", re.IGNORECASE)
_EXT = {"png": "png", "jpeg": "jpg", "jpg": "jpg", "webp": "webp", "gif": "gif"}

_LOCK = threading.RLock()


def _load_locked() -> dict[str, Any]:
    data: dict[str, Any] = {"records": []}
    try:
        raw = json.loads(INDEX_PATH.read_text("utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("records"), list):
            data = raw
    except (OSError, ValueError):
        data = {"records": []}
    return data


def _save_locked(data: dict[str, Any]) -> None:
    FAQ_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(INDEX_PATH)


def _next_id_locked(records: list[dict[str, Any]], now: datetime) -> str:
    """当日流水号：FAQ-20261117-001。按前缀计数，删除过旧记录也不会撞号。"""
    prefix = f"FAQ-{now:%Y%m%d}-"
    used = 0
    for rec in records:
        rid = str(rec.get("id") or "")
        if rid.startswith(prefix):
            try:
                used = max(used, int(rid[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{used + 1:03d}"


def _decode_image(raw: Any, index: int) -> tuple[str, bytes] | str:
    """返回 (扩展名, 原始字节) 或错误文案。"""
    if not isinstance(raw, str) or not raw.strip():
        return f"第 {index} 张截图内容为空"
    match = _DATA_URL.match(raw.strip())
    if not match:
        return f"第 {index} 张截图格式不支持（仅 png / jpg / webp / gif）"
    payload = raw.strip()[match.end():]
    try:
        blob = base64.b64decode(payload, validate=False)
    except (ValueError, TypeError):
        return f"第 {index} 张截图解码失败"
    if not blob:
        return f"第 {index} 张截图内容为空"
    if len(blob) > MAX_IMAGE_BYTES:
        return f"第 {index} 张截图超过 5MB，请压缩后重试"
    return _EXT[match.group(1).lower()], blob


def submit(payload: Any) -> dict[str, Any]:
    """落一条反馈。返回前端的 {success,id} / {success:false,msg}。"""
    data = payload if isinstance(payload, dict) else {}

    content = str(data.get("content") or "").strip()
    if not content:
        return {"success": False, "msg": "请描述您遇到的问题"}
    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS]

    contact = str(data.get("contact") or "").strip()[:MAX_CONTACT_CHARS]

    images = data.get("images")
    if images is None:
        images = []
    if not isinstance(images, list):
        return {"success": False, "msg": "截图附件格式不正确"}
    if len(images) > MAX_IMAGES:
        return {"success": False, "msg": f"截图最多 {MAX_IMAGES} 张"}

    decoded: list[tuple[str, bytes]] = []
    for i, item in enumerate(images, start=1):
        raw = item.get("base64") if isinstance(item, dict) else item
        result = _decode_image(raw, i)
        if isinstance(result, str):
            return {"success": False, "msg": result}
        decoded.append(result)

    with _LOCK:
        index = _load_locked()
        records: list[dict[str, Any]] = index["records"]
        now = datetime.now()
        faq_id = _next_id_locked(records, now)

        saved_images: list[dict[str, Any]] = []
        if decoded:
            folder = FAQ_DIR / faq_id
            folder.mkdir(parents=True, exist_ok=True)
            for i, (ext, blob) in enumerate(decoded, start=1):
                name = f"{i}.{ext}"
                try:
                    (folder / name).write_bytes(blob)
                except OSError as exc:  # 磁盘满 / 无权限：如实失败，别发假编号
                    return {"success": False, "msg": f"截图保存失败：{exc}"}
                saved_images.append({"file": f"{faq_id}/{name}", "bytes": len(blob)})

        record = {
            "id": faq_id,
            "createdAt": now.isoformat(timespec="seconds"),
            "content": content,
            "contact": contact,
            "images": saved_images,
            "status": "pending",
            "clientRequestId": str(data.get("requestId") or "")[:64],
        }
        records.append(record)
        try:
            _save_locked(index)
        except OSError as exc:
            return {"success": False, "msg": f"反馈保存失败：{exc}"}

    return {"success": True, "id": faq_id}


def list_records() -> list[dict[str, Any]]:
    """最近 100 条（按提交时间倒序），供验收脚本与内部排查。"""
    with _LOCK:
        records = list(_load_locked()["records"])
    return list(reversed(records[-100:]))


def storage_dir() -> Path:
    return FAQ_DIR
