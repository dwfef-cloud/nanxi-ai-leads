"""消息中心「测试推送」：把一条测试消息发到用户自己配的飞书 / 钉钉机器人。

页面调用契约（MessageCenter）：
    invoke("test-chat-message-webhook", 整份通知配置)
    返回 {success: bool, message: str} —— 页面对 success/message 直接展示。

安全边界：
- **只允许飞书 / 钉钉的官方机器人域名**。这里是后端代发 HTTP，
  不做白名单就等于给页面开了一个任意地址转发器（SSRF：可以打内网接口、
  可以拿本机服务当跳板）。域名与路径都要对得上。
- 超时 5 秒、不跟随重定向（跟随会把请求带到白名单之外的地址）。
- 未配置 / 不推送时直接返回可读原因，不假装发成功。

钉钉机器人开了「加签」就必须带 timestamp + sign，否则回 errcode 310000，
页面会显示「签名不匹配」——这类失败要说清是签名问题，不能笼统说「发送失败」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

TIMEOUT = 5
_MAX_MESSAGE = 500

# 域名 -> (允许的路径前缀, 请求体构造, 回执判定)
_ALLOWED = {
    "open.feishu.cn": ("/open-apis/bot/v2/hook/", "feishu"),
    "open.larksuite.com": ("/open-apis/bot/v2/hook/", "feishu"),
    "oapi.dingtalk.com": ("/robot/send", "dingtalk"),
}


def _url_for(config: Mapping) -> tuple[str, str]:
    kind = str(config.get("webhookType") or "none").strip().lower()
    if kind == "feishu":
        return kind, str(config.get("webhookUrlFeishu") or "").strip()
    if kind == "dingtalk":
        return kind, str(config.get("webhookUrlDingtalk") or "").strip()
    return kind, ""


def _check_url(url: str) -> tuple[str, str] | None:
    """返回 (host, flavor) 或 None（不在白名单）。"""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    host = (parsed.hostname or "").lower()
    rule = _ALLOWED.get(host)
    if not rule:
        return None
    prefix, flavor = rule
    if not parsed.path.startswith(prefix):
        return None
    return host, flavor


def _sign_dingtalk(url: str, secret: str) -> str:
    """钉钉加签：HmacSHA256(f"{ts}\\n{secret}", secret) -> base64 -> urlencode。"""
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    joiner = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{joiner}timestamp={timestamp}&sign={sign}"


def _payload(flavor: str, text: str) -> bytes:
    if flavor == "feishu":
        body = {"msg_type": "text", "content": {"text": text}}
    else:
        body = {"msgtype": "text", "text": {"content": text}}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _verdict(flavor: str, raw: str) -> tuple[bool, str]:
    """解析回执。飞书 code/StatusCode、钉钉 errcode，字段名两代都兼容。"""
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return False, f"平台返回的不是 JSON：{raw[:120]}"
    if not isinstance(data, dict):
        return False, f"平台返回结构异常：{raw[:120]}"
    if flavor == "feishu":
        code = data.get("code", data.get("StatusCode"))
        if code in (0, "0"):
            return True, "飞书已收到测试消息"
        return False, f"飞书拒收：{data.get('msg') or data.get('StatusMessage') or code}"
    errcode = data.get("errcode")
    if errcode in (0, "0"):
        return True, "钉钉已收到测试消息"
    detail = data.get("errmsg") or errcode
    if str(errcode) == "310000":
        detail = f"{detail}（检查加签密钥是否与机器人一致）"
    return False, f"钉钉拒收：{detail}"


def send_test(config: Any) -> dict:
    if isinstance(config, list):
        config = config[0] if config else {}
    if not isinstance(config, Mapping):
        return {"success": False, "message": "没有收到通知配置"}

    kind, url = _url_for(config)
    if kind in ("", "none"):
        return {"success": False, "message": "当前为「不推送」，无需测试"}
    if not url:
        return {"success": False, "message": "请先填写 Webhook 地址"}

    checked = _check_url(url)
    if not checked:
        return {"success": False,
                "message": "只支持飞书 / 钉钉官方机器人地址（https，且域名与路径必须匹配）"}
    _host, flavor = checked

    if flavor == "dingtalk":
        secret = str(config.get("webhookSecretDingtalk") or "").strip()
        if secret:
            url = _sign_dingtalk(url, secret)

    text = ("【阿里山AI获客】这是一条测试推送。\n"
            f"发送时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "收到即说明通知配置可用，后续任务提醒会推到这里。")
    request = urllib.request.Request(
        url, data=_payload(flavor, text[:_MAX_MESSAGE]),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:  # noqa: S310 - 域名已白名单
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {"success": False, "message": f"平台返回 HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        return {"success": False, "message": f"连不上机器人地址：{exc.reason}"}
    except TimeoutError:
        return {"success": False, "message": f"请求超时（{TIMEOUT}s），检查网络或地址"}
    return dict(zip(("success", "message"), _verdict(flavor, raw)))
