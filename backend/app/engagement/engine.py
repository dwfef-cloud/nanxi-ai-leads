"""D8 常驻引擎 —— patchright 驱动管理 + profile 隔离 + 账号租约（M6 前置）。

设计要点（DESIGN-DOC §3 M6）：
- 每账号一个持久化 profile 目录（patchright user_data_dir），指纹随 profile
  固化，**绝不跨账号复用**；
- 账号租约：任务启动先租约，心跳 30s 续期；持有者崩溃（心跳超时）自动回收；
- 驱动可注入：生产用 PatchrightDriver（延迟 import，缺依赖时报
  EngineUnavailable），测试用 OfflineDriver 完全离线复现。

合规：引擎只驱动「读屏 + 对筛选结果互动」，不做群控、不做代理池（§1.2 范围外）。
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class EngineUnavailable(RuntimeError):
    """patchright 未安装或浏览器内核缺失。"""


class Driver(Protocol):
    """单账号浏览器驱动。实现方必须保证一个 Driver 实例只服务一个 profile。"""

    def open(self, profile_dir: Path) -> None: ...
    def close(self) -> None: ...
    def like(self, video_url: str) -> tuple[int | None, dict | None]: ...
    def comment(self, video_url: str, text: str) -> tuple[int | None, dict | None]: ...


@dataclass
class OfflineDriver:
    """测试驱动：记录全部调用，返回可编程结果。零网络、零浏览器。"""

    results: list[tuple[int | None, dict | None]] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    closed: int = 0
    actions: list[tuple[str, str, str]] = field(default_factory=list)
    _idx: int = 0

    def open(self, profile_dir: Path) -> None:
        self.opened.append(str(profile_dir))

    def close(self) -> None:
        self.closed += 1

    def _next(self) -> tuple[int | None, dict | None]:
        if not self.results:
            return 200, {"ok": True}
        r = self.results[min(self._idx, len(self.results) - 1)]
        self._idx += 1
        return r

    def like(self, video_url: str) -> tuple[int | None, dict | None]:
        self.actions.append(("like", video_url, ""))
        return self._next()

    def comment(self, video_url: str, text: str) -> tuple[int | None, dict | None]:
        self.actions.append(("comment", video_url, text))
        return self._next()


class PatchrightDriver:
    """生产驱动 —— 复刻 douyin_automation_pkg 已验证链路（反检测启动配置 +
    验证码闸门 + 真人输入节奏），同步接口内部桥接 patchright 异步 API。

    - 启动：持久化 context（登录态/指纹随 profile 固化），channel="chrome"
      优先，回退 bundled chromium；args 与已验证包一致；
    - 输入：真人逐字（humanize §5.1：80-200ms/字 + 12% 思考停顿 + 5% 打错
      退格），Draft.js 注入仅作最后兜底且记录告警；
    - 风控：验证码可见弹窗检测 → 等待人工完成（最长 180s）→ 写冷却文件，
      冷却未解除直接拒绝动作；
    - 状态码：200 成功 / 403 风控拦截（冷却/验证码未过）/ 404 未找到目标 /
      429 输入框未清空（发送不确定）。
    """

    COOLDOWN_HOURS_AFTER_CAPTCHA = 2.0
    CAPTCHA_WAIT_S = 180

    # 已验证包 browser.py 的启动参数（patchright + 反检测）
    _LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process,"
        "BlockInsecurePrivateNetworkRequests",
        "--no-sandbox",
        "--disable-setuid-sandbox",
    ]

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._ctx = None
        self._page = None
        self._profile_dir: Path | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── 生命周期 ─────────────────────────────────────────────
    def open(self, profile_dir: Path) -> None:
        try:
            from patchright.async_api import async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise EngineUnavailable(
                "patchright 未安装：pip install patchright && patchright install chromium"
            ) from exc
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._profile_dir = profile_dir
        # patchright 异步对象必须绑定创建时的事件循环：驱动生命周期内
        # 持有一个专用循环，所有同步调用经 _run 派发到它上面。
        self._loop = asyncio.new_event_loop()

        async def _start() -> None:
            pw = await async_playwright().start()
            kwargs: dict = dict(
                user_data_dir=str(profile_dir), headless=self._headless,
                args=self._LAUNCH_ARGS, locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1280, "height": 720},
            )
            try:  # 优先系统 Chrome（与已验证包一致）
                self._ctx = await pw.chromium.launch_persistent_context(
                    channel="chrome", **kwargs)
            except Exception:
                self._ctx = await pw.chromium.launch_persistent_context(**kwargs)
            # 抖音页面会用 <a href="bytedance://..."> 唤起客户端；真实 Chrome
            # 不归 Electron 的导航钩子管，交给 Windows 就会弹「需要使用新应用」。
            # 在文档级拦掉这类点击，不影响任何 https 站内跳转。
            await self._ctx.add_init_script("""
(() => {
  const blocked = /^(bytedance|snssdk[0-9]*|aweme|douyin|toutiao):/i;
  const hit = (u) => { try { return blocked.test(String(u || "")); } catch (_) { return false; } };
  document.addEventListener("click", (e) => {
    const t = e.target;
    const a = t && t.closest ? t.closest("a[href]") : null;
    if (a && hit(a.getAttribute("href"))) { e.preventDefault(); e.stopPropagation(); }
  }, true);
  const open = window.open;
  window.open = function (u) { if (hit(u)) return null; return open.apply(this, arguments); };
})();
""")
            self._pw = pw
            self._page = (self._ctx.pages[0] if self._ctx.pages
                          else await self._ctx.new_page())
            self._page.set_default_timeout(30000)

        self._run(_start())

    def close(self) -> None:
        if self._ctx:
            async def _stop() -> None:
                await self._ctx.close()
                await self._pw.stop()
            try:
                self._run(_stop())
            except Exception:
                pass
        if self._loop:
            try:
                self._loop.close()
            except Exception:
                pass
        self._ctx = self._page = None
        self._pw = None
        self._loop = None

    # ── 点赞（feed 页 / 视频页通用）─────────────────────────
    def like(self, video_url: str) -> tuple[int | None, dict | None]:
        async def _do() -> tuple[int | None, dict | None]:
            page = self._page
            await page.goto(video_url, wait_until="domcontentloaded")
            await self._sleep(3.0, 5.0)
            if await self._captcha_block("点赞"):
                return 403, {"error": "captcha-or-cooldown"}
            # 已点赞（红色实心）则幂等返回成功
            liked = await page.evaluate(
                """() => {
                    const sel = "[data-e2e='feed-like-icon'], [data-e2e='video-player-digg'],"
                        + " [class*='like'][class*='active']";
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    return el.getAttribute('aria-pressed') === 'true'
                        || /active|liked/i.test(el.className || '');
                }""")
            if liked:
                return 200, {"ok": True, "already": True}
            clicked = await page.evaluate(
                """() => {
                    const sels = ["[data-e2e='feed-like-icon']",
                                  "[data-e2e='video-player-digg']",
                                  "[class*='digg'] [class*='icon']"];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.offsetParent !== null) {
                            const r = el.getBoundingClientRect();
                            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
                        }
                    }
                    return null;
                }""")
            if not clicked:
                return 404, {"error": "like-button-not-found"}
            await page.mouse.click(clicked["x"], clicked["y"])
            await self._sleep(1.5, 2.5)
            return 200, {"ok": True}

        return self._run(_do())

    # ── 评论（顶层评论；复刻已验证 reply_comment 的闸门与输入链）──
    def comment(self, video_url: str, text: str) -> tuple[int | None, dict | None]:
        async def _do() -> tuple[int | None, dict | None]:
            page = self._page
            await page.goto(video_url, wait_until="domcontentloaded")
            await self._sleep(3.0, 5.0)
            if await self._captcha_block("评论"):
                return 403, {"error": "captcha-or-cooldown"}
            await self._sleep(1.0, 2.0)

            # 定位评论输入框（真实评论框在页面上部以外的区域，排除弹幕框）
            box = await page.evaluate_handle(
                """() => {
                    const all = Array.from(document.querySelectorAll(
                        'input, textarea, [contenteditable="true"]'));
                    const vis = all.filter(e => {
                        const r = e.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0 || r.top < 80) return false;
                        if (r.width < 100) return false;
                        const tag = e.tagName.toLowerCase();
                        if (tag === 'input') {
                            const ph = e.getAttribute('placeholder') || '';
                            if (!/评论|说点|留下/.test(ph)) return false;
                            const t = (e.getAttribute('type') || '').toLowerCase();
                            if (['hidden','search','file'].includes(t)) return false;
                        }
                        return true;
                    });
                    const pref = vis.find(e =>
                        /评论|说点|留下/.test(e.getAttribute('placeholder') || ''));
                    return pref || vis[0] || null;
                }""")
            input_el = box.as_element() if box else None
            if not input_el:
                return 404, {"error": "comment-input-not-found"}

            await self._human_click(input_el)
            await self._sleep(0.3, 0.8)
            await self._human_type(text)
            await self._sleep(0.8, 2.0)  # §5.1 提交停顿前置：打完先读一遍

            # 发送按钮：输入框右侧圆形图标（复刻已验证定位），失败降级 Enter
            sent = await page.evaluate(
                """(sel) => {
                    const input = document.querySelector(sel);
                    if (!input) return false;
                    const r = input.getBoundingClientRect();
                    const cands = [];
                    for (const el of document.querySelectorAll('*')) {
                        const b = el.getBoundingClientRect();
                        if (b.width >= 20 && b.width <= 60 && b.height >= 20
                                && b.height <= 60
                                && Math.abs(b.width - b.height) <= 15
                                && b.left > r.right - 100
                                && b.top >= r.top - 30 && b.top <= r.bottom + 30
                                && el !== input && !input.contains(el)) {
                            cands.push(el);
                        }
                    }
                    if (!cands.length) return false;
                    cands.sort((a, b) => b.getBoundingClientRect().left
                               - a.getBoundingClientRect().left);
                    const b = cands[0].getBoundingClientRect();
                    window.__sendBtn = {x: b.left + b.width / 2, y: b.top + b.height / 2};
                    return true;
                }""", "input, textarea, [contenteditable='true']")
            if sent:
                pos = await page.evaluate("window.__sendBtn")
                await self._human_move_to(pos["x"], pos["y"])
                await self._sleep(0.2, 0.5)
                await page.mouse.click(pos["x"], pos["y"])
            else:
                await page.keyboard.press("Enter")
            await self._sleep(1.5, 2.5)

            # 验证：输入框被清空 = 发送成功；未清空 = 不确定（429，不重试由上层三态处理）
            cleared = await page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll(
                        'input, textarea, [contenteditable="true"]'));
                    const el = els.find(e => {
                        const r = e.getBoundingClientRect();
                        return r.width > 100 && r.height > 10 && r.top > 80;
                    });
                    return el ? (el.value || el.textContent || '').trim().length === 0
                              : false;
                }""")
            if cleared:
                return 200, {"ok": True}
            return 429, {"error": "input-not-cleared", "uncertain": True}

        return self._run(_do())

    # ── 风控闸门（复刻已验证 _guard/_detect/_wait/_cooldown）────
    def _cooldown_path(self) -> Path:
        return (self._profile_dir or Path(".")) / "cooldown.json"

    def _cooldown_active(self) -> dict | None:
        import json
        p = self._cooldown_path()
        if not p.exists():
            return None
        try:
            from datetime import datetime
            data = json.loads(p.read_text(encoding="utf-8"))
            if datetime.now() >= datetime.fromisoformat(data["until"]):
                return None
            return data
        except Exception:
            return None

    def _write_cooldown(self, hours: float, reason: str) -> None:
        import json
        from datetime import datetime, timedelta
        try:
            data = {"until": (datetime.now() + timedelta(hours=hours)).isoformat(),
                    "reason": reason, "setAt": datetime.now().isoformat()}
            self._cooldown_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    async def _captcha_block(self, action: str) -> bool:
        """冷却中或验证码弹窗可见 → True（调用方直接返回 403）。"""
        cd = self._cooldown_active()
        if cd:
            return True
        visible = await self._page.evaluate(
            """() => {
                const texts = ['请完成安全验证', '请完成验证', '拖动滑块完成验证',
                               '点击两个形状相同的物体', '请依次点击'];
                for (const t of texts) {
                    for (const el of document.querySelectorAll('*')) {
                        if ((el.innerText || '').trim() === t
                                && el.offsetParent !== null) {
                            const r = el.getBoundingClientRect();
                            if (r.width > 50 && r.height > 20) return true;
                        }
                    }
                }
                for (const s of ['.geetest_panel', '.geetest_widget',
                                 '#captcha-verify-panel', "[class*='secsdk-captcha']"]) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 200 && r.height > 100) return true;
                    }
                }
                return false;
            }""")
        if not visible:
            return False
        # 验证码出现：等人工处理（最长 CAPTCHA_WAIT_S），过了也冷却今天别再跑
        deadline = time.monotonic() + self.CAPTCHA_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            if not await self._page.evaluate(
                    "!!document.querySelector('.geetest_panel, #captcha-verify-panel')"):
                self._write_cooldown(self.COOLDOWN_HOURS_AFTER_CAPTCHA,
                                     f"{action}触发验证码（已人工完成）")
                return True
        self._write_cooldown(4.0, f"{action}前验证码未解决")
        return True

    # ── 人类行为（§5.1 参数）─────────────────────────────────
    async def _human_type(self, text: str) -> None:
        for ch in text:
            await self._page.keyboard.type(ch)
            await asyncio.sleep(random.uniform(0.08, 0.20))  # 逐字 80-200ms
            if random.random() < 0.12:  # 思考停顿
                await asyncio.sleep(random.uniform(0.25, 0.8))
            if random.random() < 0.05 and len(text) > 3:  # 打错退格
                await self._page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await self._page.keyboard.type(ch)
                await asyncio.sleep(random.uniform(0.08, 0.20))

    async def _human_click(self, element) -> None:
        try:
            box = await element.bounding_box()
            if not box:
                await element.click()
                return
            x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
            y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
            await self._human_move_to(x, y)
            await asyncio.sleep(random.uniform(0.02, 0.08))
            await self._page.mouse.click(x, y)
        except Exception:
            try:
                await element.click()
            except Exception:
                pass

    async def _human_move_to(self, x: float, y: float) -> None:
        sx = x + random.randint(-120, 120)
        sy = y + random.randint(-80, 80)
        steps = max(12, min(40, int(((x - sx) ** 2 + (y - sy) ** 2) ** 0.5 / 20)))
        for i in range(steps + 1):
            t = i / steps
            ease = t * t * (3 - 2 * t)  # smoothstep
            bx = sx + (x - sx) * ease + random.gauss(0, 1.2)
            by = sy + (y - sy) * ease + random.gauss(0, 1.2)
            await self._page.mouse.move(bx, by)
            await asyncio.sleep(random.uniform(0.004, 0.016))

    @staticmethod
    async def _sleep(a: float, b: float) -> None:
        await asyncio.sleep(random.uniform(a, b))

    def _run(self, coro) -> object:
        """同步桥接：worker 在线程池里调用；驱动持有专用循环，协程派发其上。"""
        if self._loop is None:
            return asyncio.run(coro)
        return self._loop.run_until_complete(coro)


@dataclass
class Lease:
    account_id: int
    profile_dir: Path
    driver: Driver
    acquired_at: int
    heartbeat_at: int


class EngineManager:
    """账号租约管理器。同一账号同时只有一个持有者；心跳超时自动回收。"""

    LEASE_TTL_S = 30  # M6：心跳 30s，崩溃自动回收

    def __init__(self, profiles_root: str | Path,
                 driver_factory: "callable | None" = None) -> None:
        self.profiles_root = Path(profiles_root)
        self._factory = driver_factory or PatchrightDriver
        self._lock = threading.Lock()
        self._leases: dict[int, Lease] = {}

    # ── 租约 ────────────────────────────────────────────────
    def acquire(self, account_id: int, *, now: int | None = None) -> Lease:
        ts = int(time.time()) if now is None else int(now)
        with self._lock:
            self._reclaim_expired_locked(ts)
            held = self._leases.get(account_id)
            if held is not None:
                raise RuntimeError(f"账号 {account_id} 租约已被持有")
            driver = self._factory()
            profile_dir = self.profiles_root / str(account_id)
            driver.open(profile_dir)
            lease = Lease(account_id=account_id, profile_dir=profile_dir,
                          driver=driver, acquired_at=ts, heartbeat_at=ts)
            self._leases[account_id] = lease
            return lease

    def heartbeat(self, account_id: int, *, now: int | None = None) -> bool:
        ts = int(time.time()) if now is None else int(now)
        with self._lock:
            lease = self._leases.get(account_id)
            if lease is None:
                return False
            lease.heartbeat_at = ts
            return True

    def release(self, account_id: int, *, now: int | None = None) -> bool:
        with self._lock:
            lease = self._leases.pop(account_id, None)
        if lease is None:
            return False
        try:
            lease.driver.close()
        except Exception:
            pass  # 关闭失败不阻塞租约回收
        return True

    def lease_of(self, account_id: int, *, now: int | None = None) -> Lease | None:
        ts = int(time.time()) if now is None else int(now)
        with self._lock:
            lease = self._leases.get(account_id)
            if lease and ts - lease.heartbeat_at > self.LEASE_TTL_S:
                self._reclaim_expired_locked(ts)
                return None
            return lease

    def active_leases(self) -> list[dict]:
        with self._lock:
            return [
                {"account_id": l.account_id, "profile_dir": str(l.profile_dir),
                 "acquired_at": l.acquired_at, "heartbeat_at": l.heartbeat_at}
                for l in self._leases.values()
            ]

    # ── 内部 ────────────────────────────────────────────────
    def _reclaim_expired_locked(self, now: int) -> None:
        expired = [aid for aid, l in self._leases.items()
                   if now - l.heartbeat_at > self.LEASE_TTL_S]
        for aid in expired:
            lease = self._leases.pop(aid)
            try:
                lease.driver.close()
            except Exception:
                pass
