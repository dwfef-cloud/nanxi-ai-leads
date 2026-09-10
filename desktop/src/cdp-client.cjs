/**
 * CDP (Chrome DevTools Protocol) 客户端
 *
 * 通过 Electron 内置的 WebContents.debugger 接口控制浏览器标签页。
 * 支持：页面导航、DOM 评估、元素点击、表单输入、网络拦截等。
 *
 * 设计原则：
 *  - 所有方法返回 Promise，异常向上抛出
 *  - 不直接操作 DOM，通过 Runtime.evaluate 注入脚本
 *  - 采集到的数据通过回调上报给调用方（主进程转发给后端）
 */

const { EventEmitter } = require('node:events');

class CDPClient extends EventEmitter {
  /**
   * @param {import('electron').WebContents} webContents - BrowserView 的 webContents
   */
  constructor(webContents) {
    super();
    this.webContents = webContents;
    this._attached = false;
    this._pending = new Map(); // messageId -> {resolve, reject}
    this._msgId = 0;

    if (webContents.debugger) {
      webContents.debugger.on('message', (_event, method, params) => {
        this._onEvent(method, params);
      });
      webContents.debugger.on('detach', () => {
        this._attached = false;
        this.emit('detached');
      });
    }
  }

  /** 附加调试器 */
  async attach() {
    if (this._attached) return;
    try {
      this.webContents.debugger.attach('1.3');
      this._attached = true;
      this.emit('attached');
    } catch (err) {
      throw new Error(`CDP attach 失败: ${err.message}`);
    }
  }

  /** 分离调试器 */
  detach() {
    if (!this._attached) return;
    try {
      this.webContents.debugger.detach();
    } catch (_) { /* ignore */ }
    this._attached = false;
  }

  get isAttached() {
    return this._attached;
  }

  /**
   * 发送 CDP 命令
   * @param {string} method
   * @param {object} [params]
   * @returns {Promise<object>}
   */
  async send(method, params = {}) {
    if (!this._attached) await this.attach();
    const id = ++this._msgId;
    return new Promise((resolve, reject) => {
      this._pending.set(id, { resolve, reject });
      const timeout = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`CDP 命令超时: ${method}`));
      }, 15000);
      try {
        this.webContents.debugger.sendCommand(method, params, (error, result) => {
          clearTimeout(timeout);
          this._pending.delete(id);
          if (error) reject(new Error(`${method}: ${error.message || JSON.stringify(error)}`));
          else resolve(result);
        });
      } catch (err) {
        clearTimeout(timeout);
        this._pending.delete(id);
        reject(err);
      }
    });
  }

  /** 处理 CDP 事件 */
  _onEvent(method, params) {
    this.emit(method, params);
    // 通用事件转发
    if (method === 'Runtime.consoleAPICalled') {
      this.emit('console', params);
    }
    if (method === 'Runtime.exceptionThrown') {
      this.emit('page-error', params);
    }
  }

  // ─────────── 高层封装 ───────────

  /** 导航到 URL */
  async navigate(url) {
    await this.send('Page.enable');
    await this.send('Page.navigate', { url });
  }

  /** 等待页面加载完成 */
  async waitForLoad(timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('页面加载超时')), timeoutMs);
      const handler = () => {
        clearTimeout(timer);
        this.removeListener('Page.loadEventFired', handler);
        resolve();
      };
      this.on('Page.loadEventFired', handler);
      // 立即检查：如果已经加载完
      this.evaluate('document.readyState').then(state => {
        if (state === 'complete') {
          clearTimeout(timer);
          this.removeListener('Page.loadEventFired', handler);
          resolve();
        }
      }).catch(() => {});
    });
  }

  /**
   * 在页面上下文中执行 JS
   * @param {string|Function} expression
   * @param {boolean} [awaitPromise]
   * @returns {Promise<any>}
   */
  async evaluate(expression, awaitPromise = false) {
    const expr = typeof expression === 'function' ? `(${expression.toString()})()` : expression;
    const result = await this.send('Runtime.evaluate', {
      expression: expr,
      returnByValue: true,
      awaitPromise,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error(`页面执行错误: ${result.exceptionDetails.text || JSON.stringify(result.exceptionDetails)}`);
    }
    return result.result?.value;
  }

  /** 等待元素出现 */
  async waitForSelector(selector, timeoutMs = 10000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const exists = await this.evaluate(`!!document.querySelector(${JSON.stringify(selector)})`);
      if (exists) return true;
      await new Promise(r => setTimeout(r, 300));
    }
    throw new Error(`等待元素超时: ${selector}`);
  }

  /** 点击元素 */
  async click(selector) {
    return this.evaluate(`
      (() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) throw new Error('元素不存在: ' + ${JSON.stringify(selector)});
        el.scrollIntoView({ behavior: 'instant', block: 'center' });
        el.click();
        return true;
      })()
    `);
  }

  /** 输入文本到 input/textarea */
  async type(selector, text) {
    return this.evaluate(`
      (() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) throw new Error('元素不存在');
        el.focus();
        el.value = ${JSON.stringify(text)};
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      })()
    `);
  }

  /** 获取元素文本 */
  async getText(selector) {
    return this.evaluate(`document.querySelector(${JSON.stringify(selector)})?.textContent?.trim() || ''`);
  }

  /** 获取当前页面 URL */
  async getUrl() {
    return this.evaluate('location.href');
  }

  /** 获取页面标题 */
  async getTitle() {
    return this.evaluate('document.title');
  }

  /**
   * 采集评论列表（通用结构，具体选择器由调用方传入）
   * @param {object} opts - { itemSelector, textSelector, authorSelector, timeSelector, maxItems }
   * @returns {Promise<Array<{text, author, time, raw}>>}
   */
  async scrapeComments(opts = {}) {
    const {
      itemSelector = '[data-e2e="comment-item"]',
      textSelector = '[data-e2e="comment-content"]',
      authorSelector = '[data-e2e="comment-user-name"]',
      timeSelector = '[data-e2e="comment-time"]',
      maxItems = 50,
    } = opts;

    return this.evaluate(`
      (() => {
        const items = document.querySelectorAll(${JSON.stringify(itemSelector)});
        const result = [];
        for (let i = 0; i < Math.min(items.length, ${maxItems}); i++) {
          const el = items[i];
          result.push({
            text: el.querySelector(${JSON.stringify(textSelector)})?.textContent?.trim() || '',
            author: el.querySelector(${JSON.stringify(authorSelector)})?.textContent?.trim() || '',
            time: el.querySelector(${JSON.stringify(timeSelector)})?.textContent?.trim() || '',
            raw: el.innerText?.slice(0, 500) || '',
          });
        }
        return result;
      })()
    `);
  }

  /** 滚动页面（加载更多评论） */
  async scrollToBottom() {
    return this.evaluate(`
      new Promise(resolve => {
        window.scrollTo(0, document.body.scrollHeight);
        setTimeout(resolve, 800);
      })
    `, true);
  }
}

module.exports = { CDPClient };
