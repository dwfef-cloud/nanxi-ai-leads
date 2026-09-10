"""私信发送器抽象层 · P4-D 私信发送执行器
=========================================
定义统一的发送器接口，屏蔽「模拟发送」与「真实 CDP 发送」差异。

- DmSender        抽象基类
- SendResult      发送结果值对象
- MockDmSender    模拟发送器（开发/测试，随机返回成功/频控/失败/验证码）
- CDPDmSender     CDP 发送器（接口层就绪，真实调用待 Electron 联调）

业务层（DmSenderService）只依赖 DmSender 接口，不感知具体实现。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

SendStatus = Literal["success", "failed", "throttled", "need_captcha", "account_banned"]


@dataclass
class SendResult:
    """发送结果 · 与发送器实现解耦"""
    status: str  # SendStatus
    message: str = ""
    detail: dict | None = None


class DmSender(ABC):
    """私信发送器抽象接口"""

    @abstractmethod
    def send_dm(self, account_id: str, user_id: str, content: str) -> SendResult:
        """用指定账号向 user_id 发送私信 content，返回 SendResult"""
        ...


class MockDmSender(DmSender):
    """模拟发送器：随机返回成功/失败/频控，用于开发测试"""

    def __init__(self, success_rate: float = 0.7, throttle_rate: float = 0.15) -> None:
        self.success_rate = success_rate
        self.throttle_rate = throttle_rate

    def send_dm(self, account_id: str, user_id: str, content: str) -> SendResult:
        import random
        import time

        time.sleep(0.3)  # 模拟网络延迟
        r = random.random()
        if r < self.success_rate:
            return SendResult("success", "模拟发送成功")
        elif r < self.success_rate + self.throttle_rate:
            return SendResult("throttled", "模拟触发频控")
        elif r < 0.95:
            return SendResult("failed", "模拟发送失败")
        else:
            return SendResult("need_captcha", "模拟需要验证码")


class CDPDmSender(DmSender):
    """CDP 发送器：通过 HTTP 调用 Electron 桌面端的 CDP 服务

    真实实现留待联调，当前只做接口层，调用时返回未实现提示。
    """

    def __init__(self, cdp_endpoint: str = "http://127.0.0.1:9222") -> None:
        self.cdp_endpoint = cdp_endpoint

    def send_dm(self, account_id: str, user_id: str, content: str) -> SendResult:
        # 接口层：构造请求但不实际执行（等 Electron 联调）
        return SendResult("failed", "CDP 发送器尚未联调，当前仅接口层就绪")
