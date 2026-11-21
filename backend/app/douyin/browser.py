"""
Douyin MCP Browser — Playwright 浏览器管理模块

职责:
  - 管理 Playwright chromium 浏览器实例生命周期
  - 处理抖音网页版扫码登录流程
  - 持久化/恢复 storage_state（cookie + localStorage）
  - 提供已登录的 BrowserContext 给 core 模块使用

配置路径:
  storage_state 保存在 ~/.douyin_mcp/storage.json
  配置目录: ~/.douyin_mcp/
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
try:  # playwright-stealth 为可选增强，缺包时退化为 patchright 自带的反检测
    from playwright_stealth import Stealth
except Exception:  # noqa: BLE001
    Stealth = None  # type: ignore[assignment]

logger = logging.getLogger("douyin-mcp.browser")

# ── 常量 ──────────────────────────────────────────────────────────────

from app.paths import DATA_DIR as _APP_DATA_DIR

DATA_DIR = _APP_DATA_DIR / "douyin"
STORAGE_STATE_PATH = DATA_DIR / "storage.json"
DOUYIN_URL = "https://www.douyin.com"
MESSAGES_URL = "https://www.douyin.com/im"
QR_TIMEOUT = 120  # 等待扫码超时（秒）

# ── 反检测脚本 — 修改浏览器指纹绕过抖音反爬 ──────────────────────────

STEALTH_SCRIPT = """
// ═══ 1. 隐藏自动化特征 ═══
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 覆盖 chrome 属性（更完整）
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {},
    isMinus: false
};

// 覆盖 permissions
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);

// ═══ 2. 浏览器基本指纹伪装 ═══
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const pluginArray = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' }
        ];
        pluginArray.namedItem = (name) => pluginArray.find(p => p.name === name) || null;
        return pluginArray;
    }
});

Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en']
});

Object.defineProperty(navigator, 'platform', {
    get: () => 'MacIntel'
});

// 硬件信息伪装
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8
});

Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8
});

Object.defineProperty(navigator, 'maxTouchPoints', {
    get: () => 0
});

// ═══ 3. Canvas 指纹伪装 ═══
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    if (type === 'image/png' && this.width === 0 && this.height === 0) {
        return originalToDataURL.apply(this, arguments);
    }
    // 添加微小噪声干扰 Canvas 指纹
    const ctx = this.getContext('2d');
    if (ctx) {
        const imageData = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < imageData.data.length; i += 4) {
            imageData.data[i] = (imageData.data[i] + Math.floor(Math.random() * 2)) % 256;
        }
        ctx.putImageData(imageData, 0, 0);
    }
    return originalToDataURL.apply(this, arguments);
};

// ═══ 4. WebGL 指纹伪装 ═══
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    // UNMASKED_VENDOR_WEBGL
    if (parameter === 37445) {
        return 'Google Inc. (Apple)';
    }
    // UNMASKED_RENDERER_WEBGL
    if (parameter === 37446) {
        return 'ANGLE (Apple, Apple M1, OpenGL 4.1)';
    }
    return getParameter.apply(this, arguments);
};

// ═══ 5. AudioContext 指纹伪装 ═══
const originalOfflineAudioContext = window.OfflineAudioContext;
if (originalOfflineAudioContext) {
    const originalStartRendering = originalOfflineAudioContext.prototype.startRendering;
    originalOfflineAudioContext.prototype.startRendering = function() {
        return originalStartRendering.apply(this, arguments).then(buffer => {
            // 添加微小噪声
            const channelData = buffer.getChannelData(0);
            for (let i = 0; i < channelData.length; i++) {
                channelData[i] += (Math.random() - 0.5) * 0.0000001;
            }
            return buffer;
        });
    };
}

// ═══ 6. 字体列表伪装 ═══
Object.defineProperty(document, 'fonts', {
    value: {
        check: () => true,
        load: () => Promise.resolve([]),
        ready: Promise.resolve({ check: () => true })
    }
});

// ═══ 7. 屏幕信息伪装 ═══
Object.defineProperty(screen, 'width', { get: () => 1920 });
Object.defineProperty(screen, 'height', { get: () => 1080 });
Object.defineProperty(screen, 'availWidth', { get: () => 1920 });
Object.defineProperty(screen, 'availHeight', { get: () => 1055 });
Object.defineProperty(screen, 'colorDepth', { get: () => 30 });
Object.defineProperty(screen, 'pixelDepth', { get: () => 30 });

// ═══ 8. 其他反检测 ═══
// 覆盖 connection 属性
Object.defineProperty(navigator, 'connection', {
    get: () => ({
        effectiveType: '4g',
        rtt: 50,
        downlink: 10,
        saveData: false
    })
});

// 隐藏自动化相关的全局变量
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

// 覆盖 toString 方法，防止检测原生函数
const originalToString = Function.prototype.toString;
Function.prototype.toString = function() {
    if (this === window.chrome.runtime || this === HTMLCanvasElement.prototype.toDataURL) {
        return 'function () { [native code] }';
    }
    return originalToString.apply(this, arguments);
};
"""


# ══════════════════════════════════════════════════════════════════════
#  BrowserManager
# ══════════════════════════════════════════════════════════════════════


class BrowserManager:
    """Playwright 浏览器管理器。

    负责:
      - 启动/关闭浏览器
      - 创建带反检测配置的 BrowserContext
      - 存储/恢复登录态 (storage_state)
      - 引导用户扫码登录
    """

    def __init__(self, headless: bool = False) -> None:
        self._headless = headless
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._cdp_connected: bool = False

    # ── 属性 ────────────────────────────────────────────────────────

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserContext not initialized. Call start() first.")
        return self._context

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Page not initialized. Call start() first.")
        return self._page

    @property
    def is_authenticated(self) -> bool:
        """检查是否已保存登录态。"""
        return STORAGE_STATE_PATH.exists()

    # ── 生命周期 ────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动浏览器并创建 Context。

        优先通过 CDP 连接已有的常驻浏览器（端口 9222），
        连接失败则启动新浏览器并开启调试端口。
        如果存在 storage_state 则恢复登录态，否则打开登录页等待扫码。
        """
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        self._cdp_connected = False

        # 优先尝试连接已有的常驻浏览器（重试3次）
        for attempt in range(3):
            try:
                logger.info("尝试通过 CDP 连接已有浏览器 (http://localhost:9222)... 第%d次", attempt+1)
                self._browser = await self._playwright.chromium.connect_over_cdp("http://localhost:9222", timeout=15000)
                if self._browser.contexts:
                    self._context = self._browser.contexts[0]
                else:
                    self._context = await self._browser.new_context()
                if self._context.pages:
                    self._page = self._context.pages[0]
                else:
                    self._page = await self._context.new_page()
                self._cdp_connected = True
                logger.info("✅ 已连接到已有浏览器，复用登录态")
                return
            except Exception as e:
                logger.warning("CDP连接第%d次失败: %s", attempt+1, e)
                if attempt < 2:
                    await asyncio.sleep(2)
        logger.info("CDP连接失败，启动新浏览器")

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process,BlockInsecurePrivateNetworkRequests",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--remote-debugging-port=9222",
        ]

        if self._headless:
            launch_args.append("--headless=new")

        # ── 使用系统已安装的 Google Chrome ────────────────────────────
        # 优先通过 channel="chrome" 调用系统 Chrome，避免从网络下载 Chromium
        
        # 如果设置了 PLAYWRIGHT_CHROMIUM_PATH 环境变量，优先使用
        chromium_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
        # 使用固定用户数据目录，持久化登录态（cookies + localStorage + 扩展等）
        user_data_dir = str(DATA_DIR / "browser_profile")
        os.makedirs(user_data_dir, exist_ok=True)

        if chromium_path:
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    executable_path=chromium_path,
                    headless=self._headless,
                    args=launch_args,
                    viewport={"width": 1280, "height": 720},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                logger.info("通过 PLAYWRIGHT_CHROMIUM_PATH=%s 启动 Chromium", chromium_path)
            except Exception as exc:
                logger.warning("PLAYWRIGHT_CHROMIUM_PATH 启动失败: %s", exc)
                chromium_path = ""  # 清空，走后续回退

        if not chromium_path:
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=user_data_dir,
                    channel="chrome",
                    headless=False,
                    args=launch_args,
                    viewport={"width": 1280, "height": 720},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                logger.info("通过 channel='chrome' 成功启动系统 Google Chrome")
            except Exception as exc:
                logger.warning(
                    "channel='chrome' 启动失败，尝试 executable_path 回退: %s", exc
                )
                for fallback_path in [
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                        None,  # Playwright bundled Chromium
                    ]:
                    try:
                        kwargs = dict(
                            user_data_dir=user_data_dir,
                            headless=self._headless,
                            args=launch_args,
                            viewport={"width": 1280, "height": 720},
                            locale="zh-CN",
                            timezone_id="Asia/Shanghai",
                        )
                        if fallback_path:
                            kwargs["executable_path"] = fallback_path
                        else:
                            # Docker 容器内 Playwright 自带 Chromium
                            kwargs["headless"] = True
                            kwargs["no_viewport"] = False
                        self._context = await self._playwright.chromium.launch_persistent_context(
                            **kwargs
                        )
                        label = fallback_path if fallback_path else "Playwright bundled Chromium (Docker)"
                        logger.info("通过 %s 成功启动浏览器", label)
                        break
                    except Exception as exc2:
                        logger.warning("回退路径 %s 失败: %s", fallback_path or "bundled Chromium", exc2)
                        continue
                else:
                    raise RuntimeError("所有浏览器启动方式均失败")

        self._browser = self._context.browser

        # 注入反检测脚本
        await self._context.add_init_script(STEALTH_SCRIPT)

        # 拦掉抖音页的 bytedance:// 等唤起链接（Chromium 的 <a> 自定义协议
        # 点击会直接交给 Windows，Playwright 的路由/导航事件拦不到）。
        await self._context.add_init_script("""
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

        self._page = await self._context.new_page()

        # 应用 playwright-stealth 反检测（专业反检测包）
        try:
            if Stealth is not None:
                await Stealth().apply_stealth_async(self._page)
            logger.info("playwright-stealth 反检测已应用")
        except Exception as e:
            logger.warning("playwright-stealth 应用失败: %s", e)

        # 设置默认超时
        self._page.set_default_timeout(30000)

        # 恢复登录态 — 从 storage.json 加载 cookies 并注入
        if self.is_authenticated:
            try:
                import json
                raw = STORAGE_STATE_PATH.read_text()
                state = json.loads(raw)
                cookies = state.get("cookies", [])
                if cookies:
                    await self._context.add_cookies(cookies)
                    logger.info("已从 %s 恢复 %d 个 cookies", STORAGE_STATE_PATH, len(cookies))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("storage_state 读取失败，将重新登录: %s", exc)

        logger.info("浏览器启动完成")

    async def ensure_authenticated(self) -> None:
        """确保当前处于登录状态，未登录则引导扫码。"""
        if not self.is_authenticated:
            logger.info("未检测到登录态，开始扫码登录流程")
            await self._login_flow()
            return

        # 有 storage_state，但有可能过期，快速验证
        await self._page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        if await self._check_login_status():
            logger.info("登录态有效 ✅")
            return

        # 登录态过期，重新扫码
        logger.warning("登录态已过期，需要重新扫码")
        await self._login_flow()

    async def _login_flow(self) -> None:
        """扫码登录流程：
        1. 打开抖音首页
        2. 点击登录按钮
        3. 等待二维码出现
        4. 提示用户扫码
        5. 等待登录成功
        6. 保存 storage_state
        """
        await self._page.goto(DOUYIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # 点击登录按钮 — 尝试多个选择器
        login_selectors = [
            "text=登录",
            ".login-button",
            "[class*='login']",
            "button:has-text('登录')",
            "span:has-text('登录')",
            "a:has-text('登录')",
        ]
        clicked = False
        for sel in login_selectors:
            try:
                btn = await self._page.wait_for_selector(sel, timeout=5000)
                if btn:
                    await btn.click()
                    clicked = True
                    logger.info("点击登录按钮: %s", sel)
                    break
            except Exception:
                continue

        if not clicked:
            # 可能已经弹出登录面板
            logger.info("登录按钮未找到，假设登录面板已显示")

        await asyncio.sleep(2)

        # 等待二维码出现 — 尝试侦测二维码图片
        logger.info("等待二维码加载...")
        try:
            # 多种二维码检测方式
            qr_detected = False
            qr_selectors = [
                "img[src*='qrcode']",
                "img[class*='qrcode']",
                "[class*='qrcode'] img",
                "canvas",
                "img[alt*='扫码']",
            ]
            for qs in qr_selectors:
                try:
                    el = await self._page.wait_for_selector(qs, timeout=8000)
                    if el:
                        qr_detected = True
                        logger.info("二维码元素已找到: %s", qs)
                        break
                except Exception:
                    continue

            if not qr_detected:
                # 截图整个页面给用户看
                logger.info("未检测到标准二维码元素，首屏截图如下")

            # 截屏保存二维码
            screenshot_path = DATA_DIR / "login_qrcode.png"
            await self._page.screenshot(path=str(screenshot_path))
            logger.info("二维码截图已保存到 %s", screenshot_path)

        except Exception as exc:
            logger.warning("二维码检测异常: %s", exc)
            screenshot_path = DATA_DIR / "login_qrcode.png"
            await self._page.screenshot(path=str(screenshot_path))
            logger.info("已保存登录页面截图到 %s", screenshot_path)

        # 提示用户扫码
        print(
            "\n" + "=" * 60,
            "\n  请使用手机抖音 App 扫码登录",
            "\n  二维码截图已保存到:",
            f"\n    {DATA_DIR / 'login_qrcode.png'}",
            "\n",
            "\n  若二维码不可见，请手动打开浏览器并扫码登录。",
            "\n  登录后脚本会自动检测并保存登录态。",
            "\n" + "=" * 60,
            file=sys.stderr,
        )

        # 等待登录成功 — 轮询检测页面是否跳转/变化
        await self._wait_for_login()

        # 保存 storage_state
        await self._save_storage_state()
        logger.info("扫码登录完成，登录态已保存 ✅")

    async def _wait_for_login(self, timeout: int = QR_TIMEOUT) -> None:
        """等待用户扫码登录成功。

        通过检测 URL 变化或登录态元素判断登录是否成功。
        """
        start = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout:
                raise TimeoutError(
                    f"扫码登录超时（{timeout}秒），请重试"
                )

            current_url = self._page.url

            # 登录成功后可能会跳转回首页
            if DOUYIN_URL in current_url and "/passport" not in current_url:
                # 再检查是否有登录态 cookie
                if await self._check_login_status():
                    return

            # 检查是否存在用户头像等登录后元素
            logged_in_selectors = [
                "[class*='user-info']",
                "[class*='avatar']",
                "[class*='userAvatar']",
                "img[class*='avatar']",
            ]
            for sel in logged_in_selectors:
                try:
                    el = await self._page.wait_for_selector(sel, timeout=3000)
                    if el:
                        return
                except Exception:
                    continue

            await asyncio.sleep(2)

    async def _check_login_status(self) -> bool:
        """通过 cookie + DOM 双重检测判断是否已登录。"""
        try:
            # 1. 检测是否有 session cookie
            cookies = await self._context.cookies()
            cookie_names = {c["name"] for c in cookies}
            session_cookies = {"sessionid", "sid_guard", "sid_ucp_virtual"}
            has_session_cookie = bool(session_cookies & cookie_names)

            # 2. DOM 检测：检查页面上是否有"登录"按钮（未登录的标志）
            has_login_button = False
            has_avatar = False
            try:
                if self._page and self._page.url:
                    has_login_button = await self._page.evaluate("""() => {
                        const all = Array.from(document.querySelectorAll('*'));
                        return all.some(el => {
                            const t = (el.innerText || '').trim();
                            const r = el.getBoundingClientRect();
                            return (t === '登录' || t === '登录 / 注册') &&
                                   r.width > 0 && r.height > 0 &&
                                   r.width < 150 && r.height < 60;
                        });
                    }""")
                    # 检查是否有用户头像（已登录标志）
                    has_avatar = await self._page.evaluate("""() => {
                        // 右上角用户头像区域
                        const avatars = document.querySelectorAll('[class*="avatar"], img[class*="avatar"], [class*="user-avatar"]');
                        for (const av of avatars) {
                            const r = av.getBoundingClientRect();
                            if (r.width > 20 && r.height > 20 && r.top < 100 && r.left > window.innerWidth - 200) {
                                return true;
                            }
                        }
                        return false;
                    }""")
            except Exception:
                pass

            # 3. 综合判断：有 session cookie 且没有登录按钮 = 已登录
            if has_session_cookie and not has_login_button:
                logger.info("登录状态验证通过: 有session cookie 且 无登录按钮")
                return True

            # 有用户头像 = 已登录
            if has_avatar:
                logger.info("登录状态验证通过: 检测到用户头像")
                return True

            # 有登录按钮 = 未登录
            if has_login_button:
                logger.warning("检测到登录按钮，未登录")
                return False

            # 只有 cookie 但无法 DOM 验证，保守返回 False
            if has_session_cookie:
                logger.warning("有session cookie 但无法DOM验证，需要进一步确认")
                # 尝试访问首页后再检测
                try:
                    await self._page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=15000)
                    await asyncio.sleep(3)
                    has_login_button2 = await self._page.evaluate("""() => {
                        const all = Array.from(document.querySelectorAll('*'));
                        return all.some(el => {
                            const t = (el.innerText || '').trim();
                            const r = el.getBoundingClientRect();
                            return (t === '登录' || t === '登录 / 注册') &&
                                   r.width > 0 && r.height > 0 &&
                                   r.width < 150 && r.height < 60;
                        });
                    }""")
                    if not has_login_button2:
                        logger.info("访问首页后确认已登录")
                        return True
                except Exception:
                    pass

            return False
        except Exception as e:
            logger.warning("登录状态检测异常: %s", e)
            return False

    async def _save_storage_state(self) -> None:
        """保存当前浏览器上下文的 storage_state 到文件。"""
        state = await self._context.storage_state()
        STORAGE_STATE_PATH.write_text(
            __import__("json").dumps(state, ensure_ascii=False, indent=2)
        )
        logger.info("登录态已保存到 %s", STORAGE_STATE_PATH)

    async def navigate(self, url: str) -> None:
        """导航到指定 URL 并等待页面加载。"""
        await self._page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)

    async def close(self) -> None:
        """清理资源，关闭浏览器。"""
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("浏览器已关闭")
