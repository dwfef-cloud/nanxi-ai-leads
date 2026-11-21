"""Local comment images are selected before review and bound by content hash."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
EXTRA_FIELDS = ("shareRecipient", "shareRecipientId", "shareRecipientKind",
                "attachmentPath", "attachmentSha256", "attachmentSize", "attachmentName",
                "stickerId", "stickerName", "stickerUrl", "mentionTargets", "mentionPosition", "commentBody",
                "saveProfileQr", "profileQrRunKey")


def image_info(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("请选择本地图片文件")
    path = Path(raw).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError("评论附图仅支持 PNG、JPEG、WebP 文件")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    valid = (path.suffix.lower() == ".png" and data.startswith(b"\x89PNG\r\n\x1a\n")
             or path.suffix.lower() in {".jpg", ".jpeg"} and data.startswith(b"\xff\xd8\xff")
             or path.suffix.lower() == ".webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    if not valid or not 16 <= len(data) <= MAX_BYTES:
        raise ValueError("附图格式无效或超过10MB，请重新选择图片")
    return {"attachmentPath": str(path), "attachmentName": path.name, "attachmentSize": len(data),
            "attachmentSha256": hashlib.sha256(data).hexdigest()}


def library(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 50:
        raise ValueError("附图库需要1到50张本地图片")
    try:
        return list({item["attachmentPath"]: item for item in map(image_info, value)}.values())
    except OSError:
        raise ValueError("附图文件不存在或无法读取，请重新选择") from None


def verify(action):
    mentions = []
    if action.get("mentionTargets"):
        try:
            mentions = json.loads(action["mentionTargets"])
            if (not isinstance(mentions, list) or not 1 <= len(mentions) <= 10
                    or any(not isinstance(m, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{6,160}", str(m.get("id", "")))
                           or not isinstance(m.get("name"), str) or not 1 <= len(m["name"]) <= 100
                           or not isinstance(m.get("query"), str) or not 1 <= len(m["query"]) <= 100 for m in mentions)
                    or len({m["id"] for m in mentions}) != len(mentions)):
                raise ValueError
            names = " ".join("@" + m["name"] for m in mentions)
            body, position = action.get("commentBody"), action.get("mentionPosition")
            if not isinstance(body, str) or position not in {"before", "after"} or action.get("text") != (
                    names + " " + body if position == "before" else body + " " + names):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("已确认@账号信息无效，请重新准备") from None
    elif action.get("commentBody") or action.get("mentionPosition"):
        raise ValueError("@账号清单缺失，请重新准备")
    if action.get("stickerId"):
        if (action.get("attachmentPath") or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", str(action["stickerId"]))
                or not isinstance(action.get("stickerName"), str) or not 1 <= len(action["stickerName"]) <= 100
                or not re.fullmatch(r"https://[^\s]{1,1500}", str(action.get("stickerUrl", "")))):
            raise ValueError("已确认GIF信息无效，请重新准备")
    elif action.get("stickerName") or action.get("stickerUrl"):
        raise ValueError("GIF标识缺失，请重新准备")
    if (mentions or action.get("stickerId")) and action.get("type") not in {"post-comment", "reply-comment"}:
        raise ValueError("GIF与@仅支持主评和回复")
    if action.get("saveProfileQr") and (action.get("type") != "share-profile" or action["saveProfileQr"] != "1"
            or not isinstance(action.get("profileQrRunKey"), str) or not 1 <= len(action["profileQrRunKey"].strip()) <= 160
            or re.search(r"[\x00-\x1f/\\]", action["profileQrRunKey"])):
        raise ValueError("二维码保存参数无效，请重新准备")
    if not action.get("attachmentPath"):
        return
    try:
        actual = image_info(action["attachmentPath"])
    except (OSError, ValueError):
        raise ValueError("已确认附图无法读取，未发送，请重新准备") from None
    if any(actual[k] != action.get(k) for k in actual):
        raise ValueError("已确认附图发生改变，未发送，请重新准备")


def mentions_confirmed(extras, targets, text):
    if not isinstance(extras, list):
        return False
    users = [m for m in extras if isinstance(m, dict) and type(m.get("type")) is int and m["type"] == 0]
    if len(users) != len(targets):
        return False
    utf16 = text.encode("utf-16-le")
    previous = 0
    for m, target in zip(users, targets):
        start, end = m.get("start"), m.get("end")
        if (str(m.get("sec_uid") or m.get("user_id") or "") != target["id"] or type(start) is not int
                or type(end) is not int or not previous <= start < end <= len(utf16) // 2
                or utf16[start * 2:end * 2] != ("@" + target["name"]).encode("utf-16-le")):
            return False
        previous = end
    return True
