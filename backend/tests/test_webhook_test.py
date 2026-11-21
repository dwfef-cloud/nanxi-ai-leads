"""消息中心「测试推送」的契约与安全测试。

断言重点：
  1. 不推送 / 没填地址 / 地址不在白名单 —— 都要给人话，不能假装发成功
  2. 白名单要卡域名**和**路径：http、内网地址、飞书域名配钉钉路径都要拒
  3. 钉钉开加签时必须带 timestamp + sign（否则平台回 310000）
  4. 平台回执要分辨「收到」和「拒收」，拒收要把原因带出来

不发真实网络请求：需要发的那两条用 monkeypatch 替换 urlopen。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import bridge_server
from app import webhook_test as W


@pytest.fixture(scope="module")
def client():
    with TestClient(bridge_server.app) as c:
        yield c


def _post(client, args):
    resp = client.post("/ipc/test-chat-message-webhook", json={"args": args})
    assert resp.status_code == 200, resp.text[:200]
    packet = resp.json()
    assert packet["success"] is True
    return packet["data"]


def test_none_type_sends_nothing(client):
    d = _post(client, [{"webhookType": "none"}])
    assert d["success"] is False
    assert "不推送" in d["message"]


def test_missing_url_is_reported(client):
    d = _post(client, [{"webhookType": "feishu", "webhookUrlFeishu": ""}])
    assert d["success"] is False and "Webhook" in d["message"]


@pytest.mark.parametrize("url", [
    "http://open.feishu.cn/open-apis/bot/v2/hook/abc",      # 非 https
    "https://127.0.0.1/open-apis/bot/v2/hook/abc",          # 内网地址
    "https://open.feishu.cn/robot/send",                    # 域名与路径不匹配
    "https://evil.example.com/open-apis/bot/v2/hook/abc",   # 非官方域名
    "https://open.feishu.cn.evil.com/open-apis/bot/v2/hook/x",  # 后缀伪装
])
def test_url_whitelist_rejects(url):
    assert W._check_url(url) is None


@pytest.mark.parametrize("url,flavor", [
    ("https://open.feishu.cn/open-apis/bot/v2/hook/abc", "feishu"),
    ("https://oapi.dingtalk.com/robot/send?access_token=x", "dingtalk"),
])
def test_url_whitelist_accepts(url, flavor):
    checked = W._check_url(url)
    assert checked and checked[1] == flavor


def test_dingtalk_secret_adds_signature():
    signed = W._sign_dingtalk("https://oapi.dingtalk.com/robot/send?access_token=x", "SECabc")
    assert "timestamp=" in signed and "sign=" in signed
    assert signed.count("?") == 1, "已有 query 时必须用 & 追加"


def test_platform_receipt_is_parsed():
    assert W._verdict("feishu", '{"code":0,"msg":"success"}')[0] is True
    assert W._verdict("dingtalk", '{"errcode":0,"errmsg":"ok"}')[0] is True
    ok, msg = W._verdict("dingtalk", '{"errcode":310000,"errmsg":"sign not match"}')
    assert ok is False and "加签" in msg
    assert W._verdict("feishu", "not-json")[0] is False


def test_success_path_uses_whitelisted_request(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"code":0,"msg":"success"}'

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(W.urllib.request, "urlopen", fake_urlopen)
    d = W.send_test({"webhookType": "feishu",
                     "webhookUrlFeishu": "https://open.feishu.cn/open-apis/bot/v2/hook/abc"})
    assert d["success"] is True and "飞书" in d["message"]
    assert seen["body"]["msg_type"] == "text"
    assert seen["body"]["content"]["text"].startswith("【阿里山AI获客】")
    assert seen["timeout"] == W.TIMEOUT
