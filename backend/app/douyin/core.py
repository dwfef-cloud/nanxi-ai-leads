"""
Douyin MCP Core — 抖音操作核心模块

基于 Playwright 浏览器自动化，通过 DOM 操作实现：
  - 搜索用户（search_user）
  - 读取私信（read_messages）
  - 发送私信（send_message）
  - 列举会话（list_conversations）

关键依赖:
  - playwright: 浏览器自动化
  - 抖音网页版 (https://www.douyin.com/messages)

注意:
  - 聊天输入框使用 Draft.js，通过 page.evaluate() 注入文本
  - 消息读取通过 DOM 提取，零成本
  - 所有操作需要先登录（由 browser.BrowserManager 管理）
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import sys
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from app.douyin.browser import BrowserManager, MESSAGES_URL, DOUYIN_URL

logger = logging.getLogger("douyin-mcp.core")

# ── Draft.js 文本注入脚本 ────────────────────────────────────────────
#
# 抖音聊天输入框基于 Draft.js，不能直接用 fill() 或 type()。
# 通过模拟 ClipboardEvent('paste') 触发 Draft.js 的 onChange 回调。
#
# 参考: https://blog.csdn.net/qq_28821897/article/details/154570263

DRAFTJS_PASTE_SCRIPT = """text => {
    // 找到 Draft.js 编辑器的可编辑区域
    const editor = document.querySelector('[data-contents="true"]') ||
                   document.querySelector('.DraftEditor-editor [contenteditable="true"]') ||
                   document.querySelector('.DraftEditor-root [contenteditable="true"]');

    if (!editor) {
        // 降级：尝试找任何 contenteditable 元素
        const fallback = document.querySelector('[contenteditable="true"]');
        if (!fallback) {
            return { success: false, error: 'Draft.js editor not found' };
        }
        // 尝试直接设置 innerText
        fallback.innerText = text;
        // 触发 input 事件
        fallback.dispatchEvent(new Event('input', { bubbles: true }));
        return { success: true, method: 'innerText' };
    }

    // 方案 A: 直接设置 innerText + 触发 input
    editor.innerText = text;
    editor.dispatchEvent(new Event('input', { bubbles: true }));

    // 方案 B: 额外触发 paste 事件（兼容 Draft.js）
    const dt = new DataTransfer();
    dt.setData('text/plain', text);
    dt.setData('text/html', text);
    const pasteEvent = new ClipboardEvent('paste', {
        clipboardData: dt,
        bubbles: true,
        cancelable: true,
    });
    editor.dispatchEvent(pasteEvent);

    return { success: true, method: 'paste' };
}
"""


# ══════════════════════════════════════════════════════════════════════
#  DouyinController
# ══════════════════════════════════════════════════════════════════════


class DouyinController:
    """抖音私信操作控制器。

    提供搜索用户、读取私信、发送私信、列举会话四个核心功能。
    所有操作通过 Playwright 控制抖音网页版完成。
    """

    def __init__(self, headless: bool = False) -> None:
        self._browser = BrowserManager(headless=headless)
        self._initialized = False
        # 验证码与风控相关
        self._last_action_time = 0.0  # 上次操作时间，用于频率控制
        self._captcha_detected = False  # 是否检测到验证码
        self._ocr = None  # ddddocr 实例（懒加载）
        self._action_interval = 3.0  # 最小操作间隔（秒），模拟人类节奏
        # 私信/评论属于高风险动作，需要比浏览类操作长得多的间隔
        self._dm_interval = 75.0        # 私信/评论最小间隔（秒）
        self._last_dm_time = 0.0        # 上次私信/评论时间
        self._dm_sent_today = 0         # 本次进程内已发出的私信/评论数
        self._dm_daily_cap = 3          # 单进程每日上限（保守）
        self._cooldown_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "cooldown.json",
        )

    # ── 冷却机制：触发验证码后落盘，下次启动先读，防止忘了又跑 ──────────
    def _read_cooldown(self) -> dict | None:
        """读取冷却文件。返回 None 表示无冷却。"""
        try:
            if not os.path.exists(self._cooldown_file):
                return None
            with open(self._cooldown_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            until = datetime.fromisoformat(data["until"])
            if datetime.now() >= until:
                return None
            return data
        except Exception:
            return None

    def _write_cooldown(self, hours: float, reason: str) -> None:
        """写入冷却文件。"""
        try:
            data = {
                "until": (datetime.now() + timedelta(hours=hours)).isoformat(),
                "reason": reason,
                "setAt": datetime.now().isoformat(),
            }
            with open(self._cooldown_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.warning("⏸  已写入冷却至 %s（%.1f 小时）原因: %s", data["until"][:19], hours, reason)
        except Exception as e:
            logger.debug("写冷却文件失败: %s", e)

    async def _guard_risky_action(self, action_name: str = "私信") -> bool:
        """高风险动作（私信/评论）前的统一闸门。

        检查顺序：冷却文件 → 每日上限 → 验证码 → 间隔 → 浏览预热。
        返回 False 表示不应继续，调用方必须直接 return。
        """
        cd = self._read_cooldown()
        if cd:
            logger.error(
                "⛔ 仍在冷却中（至 %s，原因: %s），本次%s已取消。",
                cd["until"][:19], cd.get("reason", ""), action_name,
            )
            return False

        if self._dm_sent_today >= self._dm_daily_cap:
            logger.error("⛔ 已到达本轮上限 %d 条%s，停止。", self._dm_daily_cap, action_name)
            return False

        if await self._detect_captcha():
            logger.warning("⚠️  操作前即检测到验证码，等待人工完成...")
            if not await self._wait_for_captcha_resolve(timeout=180):
                self._write_cooldown(4, f"{action_name}前验证码未解决")
                return False
            # 验证码刚过，风险分已经很高，直接冷却，今天别再跑
            self._write_cooldown(2, f"{action_name}触发验证码")
            return False

        # 间隔控制（首次不等待，之后保证 _dm_interval ± 30s）
        if self._last_dm_time > 0:
            elapsed = time.time() - self._last_dm_time
            wait = self._dm_interval + random.uniform(-15, 45) - elapsed
            if wait > 0:
                logger.info("⏳ 距离上条%s还需等待 %.0f 秒...", action_name, wait)
                await asyncio.sleep(wait)

        # 操作前浏览预热，让 session 有真人行为轨迹
        await self._browse_warmup(duration=random.uniform(2.0, 4.0))
        return True

    async def _after_risky_action(self, action_name: str = "私信") -> None:
        """高风险动作后的收尾：打时间戳、检测验证码、必要时写冷却。"""
        self._last_dm_time = time.time()
        self._dm_sent_today += 1
        await asyncio.sleep(random.uniform(2.0, 4.0))
        if await self._detect_captcha():
            logger.warning("⚠️  %s后触发验证码，等待人工完成...", action_name)
            self._write_cooldown(4, f"{action_name}后触发验证码")
            await self._wait_for_captcha_resolve(timeout=180)

    async def initialize(self) -> str:
        """初始化浏览器并确保已登录。

        Returns:
            状态信息字符串。
        """
        if self._initialized:
            # 检查浏览器是否还在运行
            try:
                _ = self._browser.page.url
                return "已经初始化"
            except Exception:
                # 浏览器已关闭，重新初始化
                logger.info("浏览器已关闭，重新初始化")
                self._initialized = False
                try:
                    await self._browser.close()
                except Exception:
                    pass

        if not self._initialized:
            await self._browser.start()
            await self._browser.ensure_authenticated()
            self._initialized = True
        return "浏览器初始化完成，已登录 ✅"

    async def close(self) -> None:
        """关闭浏览器，释放资源。"""
        await self._browser.close()
        self._initialized = False

    @property
    def page(self):
        return self._browser.page

    # ── 工具函数 ────────────────────────────────────────────────────

    async def _ensure_messages_page(self) -> None:
        """确保浏览器页面可用。

        注意：抖音网页版已下线独立的 /messages 私信页面（现为 404），
        发送私信通过用户主页的"私信"按钮弹出聊天窗口完成。
        此方法仅确保页面可用，避免停留在 404 页面。
        """
        try:
            current_url = self.page.url
            # 如果当前是旧版私信 404 页面，导航到抖音首页
            if "messages" in current_url or "404" in current_url:
                await self._browser.navigate(DOUYIN_URL)
                await asyncio.sleep(2)
        except Exception:
            pass

    async def _safe_text(self, element_handle, default: str = "") -> str:
        """安全地获取元素文本内容。"""
        try:
            text = await element_handle.inner_text()
            return text.strip() or default
        except Exception:
            return default

    # ══════════════════════════════════════════════════════════════════
    #  Tool 1: search_user
    # ══════════════════════════════════════════════════════════════════

    async def search_user(self, keyword: str) -> str:
        """搜索抖音用户。

        在抖音搜索页面搜索用户，返回匹配的用户列表。

        Args:
            keyword: 搜索关键词（用户名、抖音号等）。

        Returns:
            用户列表文本（昵称、抖音号、粉丝数等）。
        """
        search_url = f"{DOUYIN_URL}/search/{keyword}?type=user"

        logger.info("搜索用户: %s", keyword)
        await self._browser.navigate(search_url)

        # 等待搜索结果渲染
        await asyncio.sleep(3)

        # 切换到"用户"标签页（如果有）
        user_tab_selectors = [
            "span:has-text('用户')",
            "div:has-text('用户')",
            "[class*='tab']:has-text('用户')",
            "a:has-text('用户')",
        ]
        for sel in user_tab_selectors:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3000)
                if el:
                    await self._human_click(el)
                    await asyncio.sleep(2)
                    logger.info("切换到用户标签")
                    break
            except Exception:
                continue

        # 提取用户信息 — 尝试多种 DOM 结构
        results = []

        # 方法 1: 用户卡片通用选择器
        user_cards = await self._query_user_cards()

        if not user_cards:
            # 方法 2: 搜索结果的通用选择
            user_cards = await self._query_search_results()

        if not user_cards:
            return f"未找到用户「{keyword}」的搜索结果"

        for card in user_cards[:10]:  # 最多返回 10 个
            results.append(card)

        output = [f"搜索「{keyword}」的结果 ({len(results)}):\n"]
        for i, r in enumerate(results, 1):
            output.append(
                f"  {i}. {r.get('nickname', '?')} "
                f"(@{r.get('unique_id', '?')})"
            )
            if r.get("desc"):
                output.append(f"     简介: {r['desc']}")
            if r.get("followers"):
                output.append(f"     粉丝: {r['followers']}")
            output.append("")

        return "\n".join(output).strip()

    async def _query_user_cards(self) -> list[dict]:
        """从搜索页面提取用户卡片信息。"""
        results = []

        # 尝试多种卡片选择器
        card_selectors = [
            "[class*='user-card']",
            "[class*='UserCard']",
            "[class*='search-result-item']",
            "[class*='searchResultItem']",
            "li[class*='user']",
            "div[class*='user-item']",
            "[class*='search-result-card']",
            "[class*='searchResultCard']",
            "[class*='account-card']",
            "[class*='search-user']",
            "[class*='user-info']",
            "[class*='user-list'] > *",
            "[class*='search-list'] [class*='item']",
        ]

        cards = []
        for sel in card_selectors:
            try:
                els = await self.page.query_selector_all(sel)
                if els:
                    cards = els
                    break
            except Exception:
                continue

        for card in cards:
            try:
                info = await self._extract_user_info(card)
                if info.get("nickname"):
                    results.append(info)
            except Exception:
                continue

        return results

    async def _query_search_results(self) -> list[dict]:
        """降级方案：从页面中提取所有可能的用户信息。"""
        results = []
        try:
            # 提取所有链接中的用户信息
            links = await self.page.query_selector_all("a[href*='/user/']")
            seen = set()
            for link in links[:20]:
                try:
                    href = await link.get_attribute("href") or ""
                    text = await self._safe_text(link)
                    # 过滤掉导航栏的"我的"链接和空文本
                    if not text or text in ("我的", "我", "首页", "关注", "推荐"):
                        continue
                    if "self" in href.lower():
                        continue
                    if text and href not in seen and len(text) > 1:
                        seen.add(href)
                        sec_uid = href.split("/user/")[-1].split("?")[0]
                        results.append({
                            "nickname": text,
                            "unique_id": sec_uid[:12] + "...",
                            "sec_uid": sec_uid,
                            "desc": "",
                            "followers": "",
                        })
                except Exception:
                    continue
        except Exception:
            pass

        return results

    async def _extract_user_info(self, card) -> dict:
        """从用户卡片元素提取信息。"""
        info = {
            "nickname": "",
            "unique_id": "",
            "sec_uid": "",
            "desc": "",
            "followers": "",
        }

        # 昵称
        for sel in [
            "[class*='nickname']",
            "[class*='Nickname']",
            "[class*='name']",
            "a[href*='/user/']",
            "span",
        ]:
            try:
                el = await card.query_selector(sel)
                if el:
                    text = await self._safe_text(el)
                    if text:
                        info["nickname"] = text
                        break
            except Exception:
                continue

        # 抖音号 (@xxx)
        for sel in [
            "[class*='unique-id']",
            "[class*='uniqueId']",
            "[class*='douyin-id']",
            "span:has-text('@')",
        ]:
            try:
                el = await card.query_selector(sel)
                if el:
                    text = await self._safe_text(el)
                    if text:
                        info["unique_id"] = text.strip("@")
                        break
            except Exception:
                continue

        # 如果找不到 unique_id，尝试从昵称元素提取
        if not info["unique_id"] and info["nickname"]:
            # 很多卡片在昵称下方有 @xxx
            pass

        # 简介
        for sel in ["[class*='desc']", "[class*='signature']", "p"]:
            try:
                el = await card.query_selector(sel)
                if el:
                    text = await self._safe_text(el)
                    if text and text != info["nickname"]:
                        info["desc"] = text
                        break
            except Exception:
                continue

        # 粉丝数
        for sel in [
            "[class*='follower']",
            "[class*='follow-count']",
            "span:has-text('粉丝')",
        ]:
            try:
                el = await card.query_selector(sel)
                if el:
                    info["followers"] = await self._safe_text(el)
                    break
            except Exception:
                continue

        # sec_uid
        try:
            link = await card.query_selector("a[href*='/user/']")
            if link:
                href = await link.get_attribute("href") or ""
                sec_uid = href.split("/user/")[-1].split("?")[0]
                if sec_uid:
                    info["sec_uid"] = sec_uid
        except Exception:
            pass

        return info

    # ══════════════════════════════════════════════════════════════════
    #  Tool 2: list_conversations
    # ══════════════════════════════════════════════════════════════════

    async def list_conversations(self) -> str:
        """列举当前所有的私信会话列表。

        从 /messages 页面左侧会话面板提取会话信息。

        Returns:
            会话列表文本（用户名、最后消息、未读数等）。
        """
        await self._ensure_messages_page()
        await asyncio.sleep(3)  # 等待左侧列表渲染

        conversations = await self._extract_conversations()

        if not conversations:
            return "没有找到会话（可能是没有私信历史，或页面结构变化）"

        output = [f"会话列表 ({len(conversations)}):\n"]
        for i, conv in enumerate(conversations, 1):
            unread = conv.get("unread", "")
            unread_tag = f" [未读: {unread}]" if unread else ""
            output.append(
                f"  {i}. {conv.get('nickname', '?')}{unread_tag}"
            )
            if conv.get("last_message"):
                output.append(f"     最后消息: {conv['last_message'][:80]}")
            output.append("")

        return "\n".join(output).strip()

    async def _extract_conversations(self) -> list[dict]:
        """从 /messages 页面提取会话列表。"""
        results = []

        # 尝试多个会话列表容器的选择器
        container_selectors = [
            "[class*='conversation-list']",
            "[class*='message-list']",
            "[class*='chat-list']",
            "[class*='session-list']",
            "div[class*='list']",
        ]

        container = None
        for sel in container_selectors:
            try:
                container = await self.page.query_selector(sel)
                if container:
                    break
            except Exception:
                continue

        # 会话项选择器
        item_selectors = [
            "[class*='conversation-item']",
            "[class*='message-item']",
            "[class*='chat-item']",
            "[class*='session-item']",
            "li[class*='item']",
            "div[class*='item']",
        ]

        items = []
        if container:
            for sel in item_selectors:
                try:
                    els = await container.query_selector_all(sel)
                    if els:
                        items = els
                        break
                except Exception:
                    continue
        else:
            # 降级：从整个页面找
            for sel in item_selectors:
                try:
                    els = await self.page.query_selector_all(sel)
                    if els:
                        items = els
                        break
                except Exception:
                    continue

        for item in items:
            try:
                conv = await self._extract_conversation_item(item)
                if conv.get("nickname"):
                    results.append(conv)
            except Exception:
                continue

        return results

    async def _extract_conversation_item(self, item) -> dict:
        """从单个会话项提取信息。"""
        conv = {
            "nickname": "",
            "last_message": "",
            "unread": "",
            "timestamp": "",
        }

        # 昵称
        for sel in [
            "[class*='nickname']",
            "[class*='name']",
            "[class*='title']",
            "span",
        ]:
            try:
                el = await item.query_selector(sel)
                if el:
                    text = await self._safe_text(el)
                    if text and len(text) < 30:
                        conv["nickname"] = text
                        break
            except Exception:
                continue

        # 最后消息
        for sel in [
            "[class*='last-message']",
            "[class*='lastMessage']",
            "[class*='content']",
            "[class*='message']",
            "p",
        ]:
            try:
                el = await item.query_selector(sel)
                if el:
                    text = await self._safe_text(el)
                    if text and text != conv["nickname"]:
                        conv["last_message"] = text
                        break
            except Exception:
                continue

        # 未读标记
        for sel in [
            "[class*='unread']",
            "[class*='badge']",
            "[class*='count']",
            "span[class*='num']",
        ]:
            try:
                el = await item.query_selector(sel)
                if el:
                    conv["unread"] = await self._safe_text(el)
                    break
            except Exception:
                continue

        # 时间戳
        for sel in [
            "[class*='time']",
            "[class*='timestamp']",
            "time",
        ]:
            try:
                el = await item.query_selector(sel)
                if el:
                    conv["timestamp"] = await self._safe_text(el)
                    break
            except Exception:
                continue

        return conv

    # ══════════════════════════════════════════════════════════════════
    #  Tool 3: read_messages
    # ══════════════════════════════════════════════════════════════════

    async def read_messages(self, contact: str, limit: int = 20) -> str:
        """读取指定联系人的私信消息。

        Args:
            contact: 联系人昵称（用于在会话列表中定位）。
            limit: 读取的最大消息条数（默认 20）。

        Returns:
            消息列表文本。
        """
        await self._ensure_messages_page()
        await asyncio.sleep(2)

        # 1. 在会话列表中点击指定联系人
        opened = await self._open_conversation(contact)
        if not opened:
            return f"未找到与「{contact}」的会话"

        # 2. 等待消息区域加载
        await asyncio.sleep(2)

        # 3. 提取消息
        messages = await self._extract_messages(limit)

        if not messages:
            return f"与「{contact}」的会话中没有找到消息"

        output = [f"与「{contact}」的私信 (最近 {len(messages)} 条):\n"]
        for msg in messages:
            sender = msg.get("sender", "?")
            text = msg.get("text", "")
            time = msg.get("time", "")
            output.append(f"  [{time}] {sender}: {text}")

        return "\n".join(output)

    async def _open_conversation(self, contact: str) -> bool:
        """在会话列表中点击指定联系人的会话。

        支持精确匹配和模糊匹配。
        """
        conversations = await self._extract_conversations()

        target = None
        for conv in conversations:
            name = conv.get("nickname", "")
            if contact == name or contact in name or name in contact:
                target = conv
                break

        if target is None:
            logger.warning("未找到联系人: %s", contact)
            return False

        # 尝试点击 — 用昵称文本定位
        nickname = target["nickname"]
        try:
            # 方法 1: 用文本定位
            link = await self.page.query_selector(f"text='{nickname}'")
            if link:
                await link.click()
                return True

            # 方法 2: 包含文本
            link = await self.page.query_selector(f"text={nickname}")
            if link:
                await link.click()
                return True

            # 方法 3: 用 XPath
            xpath = f"//*[contains(text(), '{nickname}')]"
            link = await self.page.wait_for_selector(f"xpath={xpath}", timeout=5000)
            if link:
                await link.click()
                return True

        except Exception as exc:
            logger.warning("点击联系人失败: %s", exc)

        return False

    async def _extract_messages(self, limit: int = 20) -> list[dict]:
        """从聊天区域提取消息列表。"""
        messages = []

        # 消息容器选择器
        container_selectors = [
            "[class*='message-list']",
            "[class*='chat-area']",
            "[class*='chat-content']",
            "[class*='message-area']",
        ]

        container = None
        for sel in container_selectors:
            try:
                container = await self.page.query_selector(sel)
                if container:
                    break
            except Exception:
                continue

        # 消息项选择器
        item_selectors = [
            "[class*='message-item']",
            "[class*='MessageItem']",
            "[class*='chat-message']",
            "div[class*='message']",
        ]

        items = []
        if container:
            for sel in item_selectors:
                try:
                    els = await container.query_selector_all(sel)
                    if els:
                        items = els
                        break
                except Exception:
                    continue
        else:
            # 降级：从页面找消息元素
            for sel in item_selectors:
                try:
                    els = await self.page.query_selector_all(sel)
                    if els:
                        items = els
                        break
                except Exception:
                    continue

        if not items:
            # 最后的降级：找包含 data-text 的 span
            try:
                text_spans = await self.page.query_selector_all(
                    "span[data-text='true']"
                )
                for span in text_spans[-limit:]:
                    text = await self._safe_text(span)
                    if text:
                        messages.append({
                            "sender": "对方",
                            "text": text,
                            "time": "",
                        })
                return messages[-limit:]
            except Exception:
                pass

            return messages

        for item in items[-limit:]:
            try:
                msg = await self._extract_message(item)
                if msg.get("text"):
                    messages.append(msg)
            except Exception:
                continue

        return messages[-limit:]

    async def _extract_message(self, item) -> dict:
        """从单个消息元素提取信息。"""
        msg = {
            "sender": "",
            "text": "",
            "time": "",
        }

        # 判断发送者：自己还是对方
        # 抖音中自己的消息通常在右侧，对方在左侧
        class_attr = ""
        try:
            class_attr = await item.get_attribute("class") or ""
        except Exception:
            pass

        is_self = "self" in class_attr or "mine" in class_attr or "right" in class_attr
        msg["sender"] = "我" if is_self else "对方"

        # 消息文本
        for sel in [
            "[class*='content']",
            "[class*='text']",
            "span[data-text='true']",
            "div",
            "span",
        ]:
            try:
                el = await item.query_selector(sel)
                if el:
                    text = await self._safe_text(el)
                    if text:
                        msg["text"] = text
                        break
            except Exception:
                continue

        # 时间戳
        for sel in [
            "[class*='time']",
            "[class*='timestamp']",
            "time",
        ]:
            try:
                el = await item.query_selector(sel)
                if el:
                    msg["time"] = await self._safe_text(el)
                    break
            except Exception:
                continue

        return msg

    # ══════════════════════════════════════════════════════════════════
    #  Tool 4: send_message
    # ══════════════════════════════════════════════════════════════════

    async def send_message(self, user_id: str, text: str) -> str:
        """向指定用户发送私信。

        流程:
          1. 打开 /messages 页面
          2. 通过联系人昵称打开会话（或通过搜索用户打开新会话）
          3. 操作 Draft.js 输入框注入文本
          4. 点击发送按钮

        Args:
            user_id: 用户昵称（用于查找会话或搜索用户）。
                     支持 "昵称|索引" 格式指定搜索结果中的第几个用户（从0开始）。
            text: 消息内容。

        Returns:
            发送结果描述。
        """
        # 解析 user_id 中的索引（格式：昵称|索引）
        target_index = 0
        original_user_id = user_id
        if "|" in user_id:
            parts = user_id.rsplit("|", 1)
            try:
                target_index = int(parts[1])
                user_id = parts[0]
                logger.info("解析到用户索引: %d, 昵称: %s", target_index, user_id)
            except ValueError:
                pass

        # 风控闸门：冷却文件 → 每日上限 → 验证码 → 间隔 → 浏览预热
        if not await self._guard_risky_action("私信"):
            return "⛔ 私信被风控闸门拦截（冷却中或已达本轮上限），本次未发送。"

        await self._ensure_messages_page()
        await asyncio.sleep(2)

        # 1. 尝试在会话列表中找到该用户
        opened = await self._open_conversation(user_id)

        if not opened:
            # 2. 如果不在会话列表中，尝试搜索用户并发送私信
            logger.info("会话列表未找到 %s，尝试搜索并发送私信", user_id)
            result = await self._search_and_send(user_id, text, target_index)
            await self._after_risky_action("私信")
            return result

        # 3. 等待聊天区域加载
        await asyncio.sleep(2)

        # 4. 在输入框中输入文本
        typed = await self._type_message(text)
        if not typed:
            return f"向「{user_id}」发送消息失败：无法操作输入框"

        # 5. 点击发送按钮
        sent = await self._click_send()
        if not sent:
            return (
                f"向「{user_id}」的消息已输入但未能自动发送（输入框内容已设置），"
                f"请手动点击发送按钮"
            )

        await self._after_risky_action("私信")
        return f"✅ 已向「{user_id}」发送消息: {text[:50]}{'...' if len(text) > 50 else ''}"

    async def _search_and_send(self, user_id: str, text: str, index: int = 0) -> str:
        """搜索用户并发送私信的降级方案。

        Args:
            user_id: 搜索关键词（用户昵称）。
            text: 消息内容。
            index: 点击搜索结果中的第几个用户（从0开始）。
        """
        # 导航到搜索页（不依赖 search_user 的返回结果）
        search_url = f"{DOUYIN_URL}/search/{user_id}?type=user"
        logger.info("导航到搜索页: %s", search_url)
        await self._browser.navigate(search_url)
        await asyncio.sleep(5)

        # 尝试切换到用户标签
        user_tab_selectors = [
            "span:has-text('用户')",
            "div:has-text('用户')",
            "[class*='tab']:has-text('用户')",
            "a:has-text('用户')",
        ]
        for sel in user_tab_selectors:
            try:
                el = await self.page.wait_for_selector(sel, timeout=3000)
                if el:
                    await self._human_click(el)
                    await asyncio.sleep(2)
                    logger.info("切换到用户标签")
                    break
            except Exception:
                continue

        # 通过 _query_search_results 获取用户列表（包含 sec_uid）
        try:
            users = await self._query_search_results()
            logger.info("找到 %d 个用户，选择第 %d 个", len(users), index)

            if not users:
                return f"搜索结果中未找到用户"

            if index >= len(users):
                return f"索引 {index} 超出范围（共 {len(users)} 个结果）"

            target_user = users[index]
            sec_uid = target_user.get("sec_uid", "")
            nickname = target_user.get("nickname", user_id)
            logger.info("选择用户: %s (sec_uid: %s)", nickname, sec_uid)

            if not sec_uid:
                return f"无法获取用户 sec_uid"

            # 直接导航到用户主页
            user_home_url = f"{DOUYIN_URL}/user/{sec_uid}"
            logger.info("导航到用户主页: %s", user_home_url)
            await self._browser.navigate(user_home_url)
            await asyncio.sleep(5)

            # 在用户主页找"发私信"按钮
            send_btn_selectors = [
                "button:has-text('发私信')",
                "span:has-text('发私信')",
                "[class*='send-message']",
                "button:has-text('私信')",
            ]
            for sel in send_btn_selectors:
                try:
                    btn = await self.page.wait_for_selector(sel, timeout=3000)
                    if btn:
                        await btn.click()
                        await asyncio.sleep(2)
                        break
                except Exception:
                    continue

            # 输入并发送
            typed = await self._type_message(text)
            if not typed:
                return f"无法操作输入框向「{user_id}」发送消息"

            sent = await self._click_send()
            if sent:
                await asyncio.sleep(1)
                return f"✅ 已向「{user_id}」发送消息: {text[:50]}{'...' if len(text) > 50 else ''}"
            else:
                return f"消息已输入但未能自动发送，请手动点击发送按钮"

        except Exception as exc:
            logger.warning("搜索并发送失败: %s", exc)

        return f"向「{user_id}」发送消息失败"

    async def _type_message(self, text: str) -> bool:
        """在输入框中输入文本。

        通过 page.evaluate() 注入文本，兼容 Draft.js 编辑器。
        降级方案：keyboard.type 逐字符输入。

        Args:
            text: 要输入的文本内容。

        Returns:
            True 如果输入成功。
        """
        try:
            # 等待聊天窗口和输入框加载（增加等待时间）
            await asyncio.sleep(2)

            # 先点击输入框，确保焦点
            input_selectors = [
                "[data-contents='true']",
                ".DraftEditor-editor",
                "[contenteditable='true']",
                ".DraftEditor-root",
                "div[class*='input']",
                "div[class*='editor']",
                "textarea",
                "div[role='textbox']",
                "div[aria-label*='输入']",
                "div[aria-label*='消息']",
            ]

            clicked = False
            input_el = None
            for sel in input_selectors:
                try:
                    el = await self.page.wait_for_selector(sel, timeout=5000)
                    if el:
                        await el.click()
                        await asyncio.sleep(1)
                        clicked = True
                        input_el = el
                        logger.info("找到并点击输入框: %s", sel)
                        break
                except Exception:
                    continue

            if not clicked:
                logger.warning("未找到输入框，尝试列出页面所有可编辑元素...")
                # 调试：列出页面上所有可能的输入元素
                try:
                    all_editables = await self.page.evaluate("""() => {
                        const els = document.querySelectorAll('[contenteditable], textarea, input, [role=textbox]');
                        return Array.from(els).map(el => ({
                            tag: el.tagName,
                            class: el.className,
                            id: el.id,
                            role: el.getAttribute('role'),
                            aria: el.getAttribute('aria-label'),
                            visible: el.offsetParent !== null
                        }));
                    }""")
                    logger.info("页面可编辑元素: %s", all_editables)
                except Exception as e:
                    logger.warning("列出可编辑元素失败: %s", e)
                return False

            # ✅ 首选：真人键盘逐字输入。
            #    ⚠️ 原实现首选 Draft.js 注入（innerText + 派发 input/paste 事件），
            #    不产生任何 keydown/keypress/keyup 事件序列，是极验最强判据之一，
            #    也是此前私信验证码频发的直接原因。注入已降为最后兜底。
            try:
                await self._human_type(text)
                await asyncio.sleep(0.3)
                current = await self.page.evaluate(
                    "() => { const el = document.querySelector("
                    "'[data-contents=\"true\"], .DraftEditor-editor [contenteditable=\"true\"],"
                    " [contenteditable=\"true\"]');"
                    " return el ? (el.innerText || '') : ''; }"
                )
                probe = text.strip()[:4]
                if probe and probe in current:
                    logger.info("真人输入成功: %s", current[:30])
                    return True
                logger.warning("真人输入后内容异常（期望前4字 %s，实际 %s）", probe, current[:30])
            except Exception as e:
                logger.warning("_human_type 失败: %s", e)

            # 兜底1：keyboard.type 逐字符（delay 必须随机，固定值也是机器指纹）
            logger.info("尝试 keyboard.type 逐字符输入...")
            try:
                await self.page.keyboard.press("Control+A")
                await asyncio.sleep(0.2)
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(0.2)
                for ch in text:
                    await self.page.keyboard.type(ch)
                    await asyncio.sleep(random.uniform(0.04, 0.12))
                await asyncio.sleep(0.5)
                logger.info("keyboard.type 输入完成")
                return True
            except Exception as e:
                logger.warning("keyboard.type 失败: %s", e)

            # 兜底2：Draft.js 注入（内容能进，但触发验证码概率很高）
            result = await self.page.evaluate(DRAFTJS_PASTE_SCRIPT, text)
            logger.warning("已使用 Draft.js 注入兜底（高风险，易触发验证码）: %s", result)
            if isinstance(result, dict) and result.get("success"):
                # 冒充人工：注入后停顿一下再发送，别瞬发
                await asyncio.sleep(random.uniform(1.5, 3.0))
                return True

            # 降级方案2：对普通 input/textarea 使用 fill
            try:
                input_el = await self.page.query_selector("textarea, input[type='text']")
                if input_el:
                    await input_el.fill(text)
                    return True
            except Exception:
                pass

            return False

        except Exception as exc:
            logger.warning("输入文本失败: %s", exc)
            return False

    async def _click_send(self) -> bool:
        """点击发送按钮。

        用 _human_click（带鼠标轨迹）替代 el.click()，并在点击前随机停顿，
        模拟"打完再读一遍"的真人节奏。
        """
        # 打完先停一下（真人不会打完瞬发）
        await asyncio.sleep(random.uniform(0.8, 2.5))

        send_selectors = [
            "button:has-text('发送')",
            "[class*='send-btn']",
            "[class*='sendButton']",
            "[class*='send'] button",
            "div[class*='send']",
            "button[type='submit']",
            "//button[contains(text(), '发送')]",
        ]

        for sel in send_selectors:
            try:
                if sel.startswith("//"):
                    el = await self.page.wait_for_selector(f"xpath={sel}", timeout=3000)
                else:
                    el = await self.page.wait_for_selector(sel, timeout=3000)

                if el:
                    await self._human_click(el)
                    return True
            except Exception:
                continue

        # 降级：按 Enter 发送
        try:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await self.page.keyboard.press("Enter")
            return True
        except Exception:
            pass

        return False

    # ══════════════════════════════════════════════════════════════════
    #  搜索视频 & 读取评论（扩展功能）
    # ══════════════════════════════════════════════════════════════════

    async def search_videos(self, keyword: str, limit: int = 10) -> list[dict]:
        """搜索抖音视频。

        Args:
            keyword: 搜索关键词。
            limit: 返回的最大视频数量。

        Returns:
            视频列表，每个视频包含 title, author, url, video_id 等字段。
        """
        # 尝试多种搜索 URL
        search_urls = [
            f"{DOUYIN_URL}/search/{keyword}?type=video",
            f"{DOUYIN_URL}/search/{keyword}",
            f"{DOUYIN_URL}/search/{keyword}?source=normal_search&type=video",
        ]

        videos = []
        for search_url in search_urls:
            logger.info("搜索视频: %s (URL: %s)", keyword, search_url)
            await self._browser.navigate(search_url)
            await asyncio.sleep(5)

            # 检测验证码（搜索页面容易触发极验验证码）
            if await self._detect_captcha():
                logger.warning("搜索页面触发验证码，等待人工完成...")
                resolved = await self._wait_for_captcha_resolve(timeout=180)
                if not resolved:
                    logger.error("验证码未解决，跳过此URL")
                    continue
                # 验证码解决后刷新页面
                await self.page.reload()
                await asyncio.sleep(5)

            # 浏览预热：模拟人类浏览行为
            await self._browse_warmup(duration=2.0)

            # 滚动加载更多结果
            for _ in range(3):
                await self._human_scroll('down', 800)
                await asyncio.sleep(1)

            # 调试：输出页面上所有链接
            try:
                all_links = await self.page.evaluate("""() => {
                    const links = document.querySelectorAll('a');
                    return Array.from(links).map(a => ({
                        href: a.href,
                        text: a.innerText?.substring(0, 50)
                    })).filter(l => l.href && (l.href.includes('/video/') || l.href.includes('/note/')));
                }""")
                logger.info("页面找到 %d 个视频/笔记链接", len(all_links))
                for link in all_links[:5]:
                    logger.info("  链接: %s | 文本: %s", link['href'][:80], link['text'][:30])
            except Exception as e:
                logger.warning("调试链接失败: %s", e)

            # 提取视频链接
            try:
                video_links = await self.page.query_selector_all("a[href*='/video/'], a[href*='/note/']")
                seen = set()
                for link in video_links:
                    try:
                        href = await link.get_attribute("href") or ""
                        if ("/video/" in href or "/note/" in href) and href not in seen:
                            seen.add(href)
                            video_id = href.split("/video/")[-1].split("/note/")[-1].split("?")[0]
                            title = await self._safe_text(link)
                            # 处理各种相对URL格式
                            if href.startswith("http"):
                                video_url = href
                            elif href.startswith("//"):
                                video_url = "https:" + href
                            elif href.startswith("/"):
                                video_url = DOUYIN_URL + href
                            else:
                                video_url = DOUYIN_URL + "/" + href
                            videos.append({
                                "title": title or "(无标题)",
                                "url": video_url,
                                "video_id": video_id,
                            })
                            if len(videos) >= limit:
                                break
                    except Exception:
                        continue
            except Exception as e:
                logger.warning("提取视频链接失败: %s", e)

            if videos:
                break

        logger.info("找到 %d 个视频", len(videos))
        return videos

    async def get_comments(self, video_url: str, limit: int = 50) -> list[dict]:
        """读取指定视频的评论。

        Args:
            video_url: 视频页面 URL。
            limit: 读取的最大评论数量。

        Returns:
            评论列表，每条评论包含 content, author, author_url, time 等字段。
        """
        logger.info("读取视频评论: %s", video_url)
        await self._browser.navigate(video_url)
        await asyncio.sleep(5)

        # 检测验证码
        if await self._detect_captcha():
            logger.warning("视频页面触发验证码，等待人工完成...")
            await self._wait_for_captcha_resolve(timeout=180)
            await self.page.reload()
            await asyncio.sleep(5)

        # 浏览预热
        await self._browse_warmup(duration=1.5)

        # 尝试点击"评论"按钮展开评论区
        try:
            comment_btn_selectors = [
                "[data-e2e='feed-comment-icon']",
                "[class*='comment-icon']",
                "text=评论",
                "div:has-text('评论')",
            ]
            for sel in comment_btn_selectors:
                try:
                    btn = await self.page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await self._human_click(btn)
                        await asyncio.sleep(2)
                        logger.info("点击了评论按钮: %s", sel)
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.debug("点击评论按钮失败: %s", e)

        # 滚动加载评论（增加次数，确保评论区加载）
        for i in range(10):
            await self._human_scroll('down', 500)
            await asyncio.sleep(1.5)
            # 每滚动3次检查一下是否有评论了
            if (i + 1) % 3 == 0:
                try:
                    count = await self.page.evaluate("document.querySelectorAll('[data-e2e=\"comment-item\"]').length")
                    if count > 0:
                        logger.info("滚动 %d 次后找到 %d 条评论，停止滚动", i+1, count)
                        break
                except Exception:
                    pass

        # 使用精确选择器优先：data-e2e="comment-item" 是抖音评论项的标准属性
        comments = []
        comment_selectors = [
            "[data-e2e='comment-item']",
            "[class*='comment-item']:not([class*='avatar'])",
            "[class*='CommentItem']",
        ]

        comment_elements = []
        for sel in comment_selectors:
            try:
                els = await self.page.query_selector_all(sel)
                if els and len(els) > 0:
                    comment_elements = els
                    logger.info("使用评论选择器: %s, 找到 %d 条", sel, len(els))
                    break
            except Exception:
                continue

        # 用 page.evaluate 结构化提取评论（最可靠）
        try:
            js_code = r"""() => {
                const items = document.querySelectorAll('[data-e2e="comment-item"]');
                const results = [];
                items.forEach(item => {
                    const text = item.innerText || '';
                    const lines = text.split('\n').filter(l => l.trim());
                    const timeMatch = text.match(/(\d+分钟前|\d+小时前|\d+天前|\d+月前|刚刚|今天|昨天)/);
                    const time = timeMatch ? timeMatch[0] : '(未知)';
                    const author = lines[0] || '(未知)';
                    // ⚠️ 2026-09-09 修：原逻辑取"第一个非空行"，抖音改版后该位置是 '...'，
                    //    导致所有评论内容都提取成 '...'。改为：优先取内容元素 → 回退取最长有效行。
                    const SKIP = ['分享', '回复', '作者', '展开', '收起', '翻译', '举报', '...', '…'];
                    let content = '';
                    const cEl = item.querySelector(
                        '[data-e2e="comment-level-1"], [class*="comment-content"],' +
                        ' [class*="CommentContent"], p[class*="content"], span[class*="content"]');
                    if (cEl) content = (cEl.innerText || '').trim();

                    if (!content || SKIP.includes(content)) {
                        const cands = lines.slice(1).map(l => l.trim()).filter(t => {
                            if (!t || SKIP.includes(t)) return false;
                            if (t.match(/^\d+$/)) return false;
                            if (timeMatch && t.includes(timeMatch[0])) return false;
                            if (t === author) return false;
                            return true;
                        });
                        if (cands.length) {
                            // 评论正文通常是长度最长的一行
                            content = cands.sort((a, b) => b.length - a.length)[0];
                        }
                    }
                    let author_url = '';
                    const link = item.querySelector('a[href*="/user/"]');
                    if (link) author_url = link.href;
                    if (content || author !== '(未知)') {
                        results.push({author, content, time, author_url});
                    }
                });
                return results;
            }"""
            raw_comments = await self.page.evaluate(js_code)
            for c in raw_comments[:limit]:
                comments.append({
                    "content": c.get('content', ''),
                    "author": c.get('author', '(未知)'),
                    "time": c.get('time', '(未知)'),
                    "author_url": c.get('author_url', ''),
                })
            logger.info("结构化提取到 %d 条评论", len(comments))
            if comments:
                return comments
        except Exception as e:
            logger.warning("结构化提取失败: %s", e)

        if not comment_elements:
            logger.warning("未找到评论元素")
            return comments

        # 降级：逐个元素提取
        for i, item in enumerate(comment_elements[:limit]):
            try:
                text = await item.inner_text()
                lines = [l.strip() for l in text.split('\n') if l.strip()]
                author = lines[0] if lines else '(未知)'
                time_match = re.search(r'(\d+分钟前|\d+小时前|\d+天前|\d+月前|刚刚|今天|昨天)', text)
                time_str = time_match.group(1) if time_match else '(未知)'
                content = lines[1] if len(lines) > 1 else ''
                comments.append({
                    "content": content,
                    "author": author,
                    "time": time_str,
                    "author_url": "",
                })
            except Exception:
                continue

        return comments


    # ══════════════════════════════════════════════════════════════════
    #  人类行为模拟（减少风控检测）
    # ══════════════════════════════════════════════════════════════════

    async def reply_comment(self, video_url: str, comment_author: str, reply_text: str) -> bool:
        """在视频评论区回复指定用户的评论（走完整风控闸门 + 真人输入）。

        Args:
            video_url: 视频页面 URL。
            comment_author: 要回复的评论作者昵称。
            reply_text: 回复内容。

        Returns:
            True 表示回复成功。
        """
        logger.info("回复评论: 作者=%s, 内容=%s", comment_author, reply_text[:50])

        if not await self._guard_risky_action("评论"):
            return False

        try:
            await self._browser.navigate(video_url)
            await asyncio.sleep(random.uniform(3, 5))

            if await self._detect_captcha():
                logger.warning("视频页面触发验证码，等待人工完成...")
                if not await self._wait_for_captcha_resolve(timeout=180):
                    self._write_cooldown(4, "评论前验证码未解决")
                    return False
                self._write_cooldown(2, "评论触发验证码")
                return False

            # 滚动找目标评论（加深：老评论需多次滚动 + 点「查看更多/展开」才能载入）
            found = False
            for _ in range(25):
                # 先尝试点开「查看更多评论 / 展开 / 加载更多」，载入更深的评论
                await self.page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button, div, span, a'));
                    for (const b of btns) {
                        const t = (b.innerText || '').trim();
                        if (t === '查看更多评论' || t === '展开' || t === '加载更多'
                                || t.includes('条回复') || t.includes('条评论')) {
                            try { b.click(); } catch (e) {}
                            return true;
                        }
                    }
                    return false;
                }""")
                found = await self.page.evaluate(
                    """(author) => {
                        const items = document.querySelectorAll(
                            '[data-e2e="comment-item"], [class*="comment-item"]');
                        for (const it of items) {
                            if ((it.innerText || '').includes(author)) return true;
                        }
                        return false;
                    }""",
                    comment_author,
                )
                if found:
                    break
                await self._human_scroll("down", random.randint(400, 800))
                await asyncio.sleep(random.uniform(1.0, 2.0))

            if not found:
                logger.error("未找到 %s 的评论", comment_author)
                return False

            # 点该评论的「回复」
            reply_clicked = await self.page.evaluate(
                """(author) => {
                    const items = document.querySelectorAll(
                        '[data-e2e="comment-item"], [class*="comment-item"]');
                    for (const it of items) {
                        if (!(it.innerText || '').includes(author)) continue;
                        const cands = it.querySelectorAll('span, div, a, button');
                        for (const c of cands) {
                            if ((c.innerText || '').trim() === '回复' && c.offsetParent !== null) {
                                c.scrollIntoView({ block: 'center' });
                                c.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }""",
                comment_author,
            )
            if not reply_clicked:
                logger.error("未找到/无法点击回复按钮")
                return False

            await asyncio.sleep(random.uniform(1.0, 2.0))

            # 定位回复输入框（抖音回复框是 <input>，不是 contenteditable/textarea）
            input_handle = await self.page.evaluate_handle(
                """() => {
                    const all = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'));
                    const vis = all.filter(e => {
                        const r = e.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        const tag = e.tagName.toLowerCase();
                        if (tag === 'input') {
                            const e2e = (e.getAttribute('data-e2e') || '');
                            if (e2e.includes('searchbar')) return false;
                            const t = (e.getAttribute('type') || '').toLowerCase();
                            if (t === 'hidden' || t === 'search') return false;
                        }
                        return true;
                    });
                    if (!vis.length) return null;
                    // 优先 placeholder 含「回复」的（真正的回复输入框）
                    const reply = vis.find(e => (e.getAttribute('placeholder') || '').includes('回复'));
                    return reply || vis[vis.length - 1];
                }"""
            )
            input_el = input_handle.as_element() if input_handle else None
            if not input_el or not await input_el.is_visible():
                logger.error("未找到回复输入框")
                return False

            if not input_el:
                logger.error("未找到回复输入框")
                return False

            await self._human_click(input_el)
            await asyncio.sleep(random.uniform(0.3, 0.8))
            await self._human_type(reply_text)
            await asyncio.sleep(random.uniform(0.8, 2.0))

            # 点「发送」——回复的发送按钮是输入框右侧最右侧的圆形上箭头图标按钮
            # 参考：https://bytesagain.com/skill/douyin-messager
            send_result = await self.page.evaluate(
                """() => {
                    // 找到回复输入框（排除弹幕输入框，弹幕通常在页面上方 y<200）
                    const editables = Array.from(document.querySelectorAll('[contenteditable="true"]'));
                    const input = editables.find(e => {
                        const r = e.getBoundingClientRect();
                        return r.width > 100 && r.height > 10 && r.top > 200;
                    });
                    if (!input) return {error: '未找到回复输入框'};

                    const inputRect = input.getBoundingClientRect();

                    // 在输入框右侧找圆形图标按钮（发送按钮）
                    // 特征：宽高 20-60px，接近正方形，位于输入框右侧区域
                    const allEls = Array.from(document.querySelectorAll('*'));
                    const candidates = [];
                    for (const el of allEls) {
                        const r = el.getBoundingClientRect();
                        if (r.width >= 20 && r.width <= 60 &&
                            r.height >= 20 && r.height <= 60 &&
                            Math.abs(r.width - r.height) <= 15 &&
                            r.left > inputRect.right - 100 &&
                            r.top >= inputRect.top - 30 &&
                            r.top <= inputRect.bottom + 30 &&
                            r.width > 0 && r.height > 0) {
                            // 排除输入框本身
                            if (el !== input && !input.contains(el)) {
                                candidates.push(el);
                            }
                        }
                    }

                    if (candidates.length === 0) return {error: '未找到发送按钮候选'};

                    // 取最右侧的按钮（发送按钮通常在最右侧）
                    candidates.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
                    const btn = candidates[0];
                    const r = btn.getBoundingClientRect();
                    return {
                        x: r.left + r.width / 2,
                        y: r.top + r.height / 2,
                        w: r.width,
                        h: r.height,
                        class: String(btn.className).substring(0, 80),
                        tag: btn.tagName
                    };
                }"""
            )

            if 'x' in send_result:
                logger.info("找到发送按钮: pos=(%.0f,%.0f) size=%dx%d tag=%s class=%s",
                            send_result['x'], send_result['y'],
                            send_result['w'], send_result['h'],
                            send_result['tag'], send_result['class'][:50])
                # 用人类鼠标移动到按钮位置并点击
                await self._human_move_to(send_result['x'], send_result['y'])
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await self.page.mouse.click(send_result['x'], send_result['y'])
                await asyncio.sleep(random.uniform(1.5, 2.5))

                # 验证发送成功：输入框是否被清空（Draft.js 发送成功后会自动清空）
                input_cleared = await self.page.evaluate(
                    """() => {
                        const editables = Array.from(document.querySelectorAll('[contenteditable="true"]'));
                        const input = editables.find(e => {
                            const r = e.getBoundingClientRect();
                            return r.width > 100 && r.height > 10 && r.top > 200;
                        });
                        return input ? input.textContent.length === 0 : false;
                    }"""
                )

                if input_cleared:
                    logger.info("✅ 评论回复已发送（输入框已清空）")
                    await self._after_risky_action("评论")
                    return True
                else:
                    logger.warning("输入框未清空，可能发送失败，尝试 Enter 降级")
            else:
                logger.warning("未找到发送按钮: %s", send_result.get('error', '未知'))

            # 降级：Enter 发送
            logger.info("尝试按 Enter 发送...")
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(random.uniform(1.5, 2.5))

            # 再次验证
            input_cleared = await self.page.evaluate(
                """() => {
                    const editables = Array.from(document.querySelectorAll('[contenteditable="true"]'));
                    const input = editables.find(e => {
                        const r = e.getBoundingClientRect();
                        return r.width > 100 && r.height > 10 && r.top > 200;
                    });
                    return input ? input.textContent.length === 0 : false;
                }"""
            )

            if input_cleared:
                logger.info("✅ 评论回复已发送（Enter 发送，输入框已清空）")
                await self._after_risky_action("评论")
                return True
            else:
                logger.error("❌ 评论回复发送失败（输入框未清空）")
                return False

        except Exception as e:
            logger.error("回复评论出错: %s", e)
            self._last_dm_time = time.time()
            return False

    async def _random_delay(self, min_sec: float = 0.5, max_sec: float = 2.0) -> None:
        """随机延迟，模拟人类反应时间。"""
        delay = min_sec + random.random() * (max_sec - min_sec)
        await asyncio.sleep(delay)

    async def _human_move_to(self, x: float, y: float) -> None:
        """增强版人类鼠标移动：分段变速 + 微小回溯 + 随机停顿 + 垂直抖动。

        基于 DrissionPage 滑块验证码实战经验和人类行为学研究：
        - 前 4/5 加速、后 1/5 减速（非线性加速度）
        - 偶尔微小回溯（模拟人手微调）
        - 移动过程中随机停顿
        - 垂直方向轻微抖动
        - 贝塞尔曲线路径

        Args:
            x: 目标 x 坐标。
            y: 目标 y 坐标。
        """
        try:
            mouse = self.page.mouse

            # 随机起点（模拟从当前位置附近开始）
            start_x = x + random.randint(-150, 150)
            start_y = y + random.randint(-100, 100)

            # 贝塞尔曲线控制点
            control1_x = start_x + (x - start_x) * 0.3 + random.randint(-60, 60)
            control1_y = start_y + (y - start_y) * 0.3 + random.randint(-80, 80)
            control2_x = start_x + (x - start_x) * 0.7 + random.randint(-40, 40)
            control2_y = start_y + (y - start_y) * 0.7 + random.randint(-40, 40)

            # 分段数（距离越远步数越多）
            distance = ((x - start_x) ** 2 + (y - start_y) ** 2) ** 0.5
            steps = max(20, min(60, int(distance / 15)))

            # 随机决定是否有回溯和停顿
            has_overshoot = random.random() < 0.3  # 30%概率有过冲
            has_pause = random.random() < 0.25      # 25%概率有中途停顿
            pause_step = random.randint(steps // 3, steps * 2 // 3) if has_pause else -1

            for i in range(steps + 1):
                t = i / steps

                # 分段变速：前80%加速，后20%减速（非线性）
                if t < 0.8:
                    # 加速阶段：ease-in
                    speed_t = (t / 0.8) ** 1.5 * 0.8
                else:
                    # 减速阶段：ease-out
                    speed_t = 0.8 + (1 - (1 - (t - 0.8) / 0.2) ** 2) * 0.2

                # 三次贝塞尔曲线
                bx = (1-speed_t)**3 * start_x + 3*(1-speed_t)**2 * speed_t * control1_x + \
                     3*(1-speed_t) * speed_t**2 * control2_x + speed_t**3 * x
                by = (1-speed_t)**3 * start_y + 3*(1-speed_t)**2 * speed_t * control1_y + \
                     3*(1-speed_t) * speed_t**2 * control2_y + speed_t**3 * y

                # 垂直方向轻微抖动（模拟人手不稳定）
                if i > 0 and i < steps:
                    by += random.gauss(0, 1.5)

                # 过冲：最后几步稍微超过目标，然后回来
                if has_overshoot and i > steps - 5:
                    overshoot_factor = (i - (steps - 5)) / 5
                    if i < steps - 2:
                        bx += (x - bx) * 0.1 * overshoot_factor
                        by += (y - by) * 0.1 * overshoot_factor
                    else:
                        bx -= (x - bx) * 0.05
                        by -= (y - by) * 0.05

                await mouse.move(bx, by)

                # 随机小延迟（速度变化）
                if t < 0.8:
                    await asyncio.sleep(random.uniform(0.003, 0.012))
                else:
                    await asyncio.sleep(random.uniform(0.008, 0.025))

                # 中途随机停顿
                if i == pause_step:
                    await asyncio.sleep(random.uniform(0.05, 0.15))

        except Exception as e:
            logger.debug("人类鼠标移动失败，使用直接移动: %s", e)
            try:
                await self.page.mouse.move(x, y)
            except Exception:
                pass

    async def _human_click(self, element) -> None:
        """增强版人类点击：先移动 + 犹豫 + 随机位置 + 点击后停留。

        模拟真实人类点击行为：
        - 鼠标先移动到元素附近（不是精确位置）
        - 短暂犹豫（0.1-0.4秒）
        - 点击位置在元素内随机偏移（不是精确中心）
        - 点击后短暂停留（模拟确认）
        - 偶尔有微小的鼠标移动调整

        Args:
            element: Playwright 元素句柄。
        """
        try:
            box = await element.bounding_box()
            if box:
                # 先移动到元素附近（不是精确位置）
                approach_x = box['x'] + box['width'] * random.uniform(0.2, 0.8) + random.randint(-20, 20)
                approach_y = box['y'] + box['height'] * random.uniform(0.2, 0.8) + random.randint(-20, 20)
                await self._human_move_to(approach_x, approach_y)

                # 短暂犹豫（模拟人类确认）
                await self._random_delay(0.1, 0.4)

                # 最终点击位置（元素内随机偏移，不是精确中心）
                click_x = box['x'] + box['width'] * random.uniform(0.25, 0.75)
                click_y = box['y'] + box['height'] * random.uniform(0.25, 0.75)

                # 微小调整移动
                await self._human_move_to(click_x, click_y)

                # 点击前极短停顿
                await asyncio.sleep(random.uniform(0.02, 0.08))

                # 使用鼠标点击（不是 element.click()，更真实）
                await self.page.mouse.click(click_x, click_y)

                # 点击后停留（模拟人类确认点击结果）
                await self._random_delay(0.2, 0.6)
            else:
                await element.click()
        except Exception as e:
            logger.debug("人类点击失败，使用直接点击: %s", e)
            try:
                await element.click()
            except Exception:
                pass

    async def _human_scroll(self, direction: str = "down", amount: int = None) -> None:
        """模拟人类滚动。

        Args:
            direction: 滚动方向，"down" 或 "up"。
            amount: 滚动量，None 则随机。
        """
        if amount is None:
            amount = random.randint(200, 600)

        try:
            # 分多次小滚动，模拟人类滚动习惯
            steps = random.randint(2, 5)
            step_amount = amount // steps
            for _ in range(steps):
                if direction == "down":
                    await self.page.mouse.wheel(0, step_amount + random.randint(-50, 50))
                else:
                    await self.page.mouse.wheel(0, -(step_amount + random.randint(-50, 50)))
                await self._random_delay(0.1, 0.3)
        except Exception as e:
            logger.debug("人类滚动失败: %s", e)
            try:
                await self.page.evaluate(f"window.scrollBy(0, {amount if direction == 'down' else -amount})")
            except Exception:
                pass

    async def _random_mouse_wander(self, duration: float = 1.0) -> None:
        """随机鼠标闲逛，模拟人类在页面上的无意识移动。

        Args:
            duration: 闲逛持续时间（秒）。
        """
        try:
            start_time = asyncio.get_event_loop().time()
            while asyncio.get_event_loop().time() - start_time < duration:
                # 随机移动到页面上的随机位置
                x = random.randint(100, 1800)
                y = random.randint(100, 900)
                await self._human_move_to(x, y)
                await self._random_delay(0.2, 0.5)
        except Exception:
            pass

    async def _human_type(self, text: str, element=None) -> None:
        """模拟人类打字，带随机延迟和偶尔的退格修正。

        Args:
            text: 要输入的文本。
            element: 可选，输入框元素（已聚焦则不需要）。
        """
        try:
            if element:
                await self._human_click(element)
                await self._random_delay(0.2, 0.5)

            # 中文用输入法，真人单字间隔远大于英文，60-200ms 更接近真实分布
            for char in text:
                await self.page.keyboard.type(char)
                await asyncio.sleep(random.uniform(0.06, 0.20))
                # 偶尔停顿，模拟人类思考
                if random.random() < 0.12:
                    await asyncio.sleep(random.uniform(0.25, 0.8))
                # 偶尔打错再退格（5%概率）
                if random.random() < 0.05 and len(text) > 3:
                    await self.page.keyboard.press("Backspace")
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                    await self.page.keyboard.type(char)
                    await asyncio.sleep(random.uniform(0.06, 0.20))
        except Exception as e:
            logger.debug("人类打字失败，使用直接输入: %s", e)
            try:
                await self.page.keyboard.type(text)
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════
    #  验证码检测与处理（基于极验验证码调研）
    # ══════════════════════════════════════════════════════════════════

    async def _detect_captcha(self) -> bool:
        """检测页面是否出现验证码（精确版：只检测可见的弹窗元素）。

        抖音使用极验（GEETEST）行为验证码，常见形态：
        - 3D形状匹配（点击两个形状相同的物体）
        - 滑块拼图
        - 文字点选
        - 图标点选

        注意：只检测可见的弹窗元素，不检查页面HTML内容关键词
        （因为JS代码中常包含captcha字样，会导致误判）。

        Returns:
            True 表示检测到可见的验证码弹窗。
        """
        try:
            page = self.page

            # 只检测可见的验证码弹窗元素（带文本的选择器优先）
            captcha_text_selectors = [
                "text=点击两个形状相同的物体",
                "text=请完成安全验证",
                "text=请完成验证",
                "text=拖动滑块完成验证",
                "text=请依次点击",
                "text=验证失败，请重试",
                "text=点击按钮进行验证",
            ]

            for selector in captcha_text_selectors:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        is_visible = await el.is_visible()
                        if is_visible:
                            # 额外确认：元素在视口内
                            box = await el.bounding_box()
                            if box and box['width'] > 50 and box['height'] > 20:
                                logger.warning("⚠️  检测到验证码弹窗: %s", selector)
                                self._captcha_detected = True
                                return True
                except Exception:
                    continue

            # 检测极验验证码容器（必须可见且有一定尺寸）
            captcha_container_selectors = [
                ".geetest_panel",
                ".geetest_widget",
                "[class*='captcha-container']",
                "[class*='verify-container']",
                "#captcha-verify-panel",
                "[class*='secsdk-captcha']",
            ]

            for selector in captcha_container_selectors:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        is_visible = await el.is_visible()
                        if is_visible:
                            box = await el.bounding_box()
                            if box and box['width'] > 200 and box['height'] > 100:
                                logger.warning("⚠️  检测到验证码容器: %s", selector)
                                self._captcha_detected = True
                                return True
                except Exception:
                    continue

            # 检测页面标题是否为验证码中间页
            try:
                title = await page.title()
                if "验证码" in title or "验证" in title:
                    # 额外确认URL是否包含验证码相关参数
                    url = page.url
                    if "captcha" in url or "verify" in url or "安全验证" in title:
                        logger.warning("⚠️  检测到验证码页面: title=%s", title)
                        self._captcha_detected = True
                        return True
            except Exception:
                pass

            self._captcha_detected = False
            return False

        except Exception as e:
            logger.debug("验证码检测失败: %s", e)
            return False

    async def _wait_for_captcha_resolve(self, timeout: int = 120) -> bool:
        """等待验证码被解决（人机接力机制）。

        当检测到验证码时，暂停自动化操作，等待用户手动完成。
        定期检查验证码是否消失，消失后自动继续。

        Args:
            timeout: 最大等待时间（秒）。

        Returns:
            True 表示验证码已解决，False 表示超时。
        """
        logger.info("=" * 60)
        logger.info("⚠️  检测到验证码！请在浏览器中手动完成验证")
        logger.info("   完成后脚本将自动继续...")
        logger.info("=" * 60)

        # 截图保存验证码
        try:
            import time
            timestamp = int(time.time())
            screenshot_path = f"E:\\captcha_{timestamp}.png"
            await self.page.screenshot(path=screenshot_path)
            logger.info("验证码截图已保存: %s", screenshot_path)
        except Exception as e:
            logger.debug("验证码截图失败: %s", e)

        # 轮询等待验证码消失
        start_time = asyncio.get_event_loop().time()
        check_count = 0
        while asyncio.get_event_loop().time() - start_time < timeout:
            await asyncio.sleep(3)
            check_count += 1
            has_captcha = await self._detect_captcha()
            if not has_captcha:
                logger.info("✅ 验证码已解决，继续执行...")
                self._captcha_detected = False
                # 验证码解决后额外等待，让页面稳定
                await asyncio.sleep(2)
                return True
            if check_count % 10 == 0:
                elapsed = int(asyncio.get_event_loop().time() - start_time)
                logger.info("等待验证码解决中... 已等待 %d 秒", elapsed)

        logger.warning("❌ 验证码等待超时（%d秒）", timeout)
        return False

    async def _ensure_action_interval(self) -> None:
        """全局操作频率控制：确保两次关键操作之间有足够间隔。

        模拟人类操作节奏，避免短时间内大量操作触发风控。
        """
        try:
            import time
            now = time.time()
            elapsed = now - self._last_action_time
            if elapsed < self._action_interval:
                wait_time = self._action_interval - elapsed + random.uniform(0, 2)
                logger.debug("操作频率控制：等待 %.1f 秒", wait_time)
                await asyncio.sleep(wait_time)
            self._last_action_time = time.time()
        except Exception as e:
            logger.debug("操作频率控制失败: %s", e)

    async def _browse_warmup(self, duration: float = 2.0) -> None:
        """浏览预热：进入页面后先模拟人类浏览行为。

        随机滚动、移动鼠标、短暂停留，模拟真实用户浏览页面的行为，
        降低被风控识别为机器人的概率。

        Args:
            duration: 预热持续时间（秒）。
        """
        try:
            logger.debug("浏览预热中...")
            start_time = asyncio.get_event_loop().time()

            while asyncio.get_event_loop().time() - start_time < duration:
                # 随机滚动
                if random.random() < 0.5:
                    await self._human_scroll('down', random.randint(100, 400))
                else:
                    await self._human_scroll('up', random.randint(50, 200))

                # 随机鼠标移动
                x = random.randint(100, 1100)
                y = random.randint(100, 600)
                await self._human_move_to(x, y)

                # 随机停留
                await asyncio.sleep(random.uniform(0.3, 0.8))

            # 最后回到页面顶部附近
            await self.page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
            logger.debug("浏览预热完成")

        except Exception as e:
            logger.debug("浏览预热失败: %s", e)

    async def _safe_action(self, action_func, *args, **kwargs):
        """安全执行操作：先检查验证码 → 频率控制 → 执行操作 → 再检查验证码。

        这是关键操作的统一入口，确保：
        1. 操作前检查是否有未解决的验证码
        2. 操作频率控制
        3. 操作后检查是否触发了新的验证码
        4. 如果触发验证码，等待人工解决

        Args:
            action_func: 要执行的异步操作函数。
            *args, **kwargs: 传递给操作函数的参数。

        Returns:
            操作函数的返回值。
        """
        # 操作前检查验证码
        if await self._detect_captcha():
            await self._wait_for_captcha_resolve()

        # 频率控制
        await self._ensure_action_interval()

        # 执行操作
        result = await action_func(*args, **kwargs)

        # 操作后检查验证码
        await asyncio.sleep(1)  # 等待页面响应
        if await self._detect_captcha():
            await self._wait_for_captcha_resolve()

        return result
