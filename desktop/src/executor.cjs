/**
 * 自动回复执行器接口
 *
 * 定义抖音平台操作的统一接口，当前提供 Mock 实现。
 * 真实实现将在评论自动化开发完成后，通过 CDPClient 对接。
 *
 * 接口方法：
 *  - sendComment(videoId, text)     发送评论
 *  - sendDm(userId, text)            发送私信
 *  - getNotifications()              获取通知/消息列表
 *  - getCommentList(videoId)         获取视频评论列表
 *  - replyComment(commentId, text)   回复指定评论
 */

const { EventEmitter } = require('node:events');

/**
 * 执行器基类（接口定义）
 * 所有真实实现必须继承此类并实现全部方法。
 */
class BaseExecutor extends EventEmitter {
  constructor() {
    super();
    this.name = 'base';
    this.ready = false;
  }

  async init() { throw new Error('init() 未实现'); }
  async sendComment(_videoId, _text) { throw new Error('sendComment() 未实现'); }
  async sendDm(_userId, _text) { throw new Error('sendDm() 未实现'); }
  async getNotifications() { throw new Error('getNotifications() 未实现'); }
  async getCommentList(_videoId) { throw new Error('getCommentList() 未实现'); }
  async replyComment(_commentId, _text) { throw new Error('replyComment() 未实现'); }
}

/**
 * Mock 执行器
 * 用于开发调试，所有方法返回模拟数据，不产生真实操作。
 */
class MockExecutor extends BaseExecutor {
  constructor() {
    super();
    this.name = 'mock';
    this._sentComments = [];
    this._sentDms = [];
    this._notificationSeed = 0;
  }

  async init() {
    this.ready = true;
    this.emit('ready');
    return { ok: true, executor: 'mock', message: 'Mock 执行器已就绪（不会产生真实操作）' };
  }

  /** 模拟发送评论 */
  async sendComment(videoId, text) {
    if (!this.ready) await this.init();
    const record = {
      id: `mock_cmt_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      videoId,
      text,
      status: 'success',
      timestamp: new Date().toISOString(),
      mock: true,
    };
    this._sentComments.push(record);
    this.emit('comment:sent', record);
    return record;
  }

  /** 模拟发送私信 */
  async sendDm(userId, text) {
    if (!this.ready) await this.init();
    const record = {
      id: `mock_dm_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      userId,
      text,
      status: 'success',
      timestamp: new Date().toISOString(),
      mock: true,
    };
    this._sentDms.push(record);
    this.emit('dm:sent', record);
    return record;
  }

  /** 模拟获取通知 */
  async getNotifications() {
    if (!this.ready) await this.init();
    this._notificationSeed++;
    const types = ['comment', 'like', 'follow', 'mention'];
    return Array.from({ length: 5 }, (_, i) => ({
      id: `mock_notif_${this._notificationSeed}_${i}`,
      type: types[i % types.length],
      fromUser: `模拟用户${i + 1}`,
      content: `这是一条模拟${types[i % types.length]}通知 #${this._notificationSeed}`,
      read: false,
      timestamp: new Date(Date.now() - i * 60000).toISOString(),
      mock: true,
    }));
  }

  /** 模拟获取评论列表 */
  async getCommentList(videoId) {
    if (!this.ready) await this.init();
    return Array.from({ length: 10 }, (_, i) => ({
      id: `mock_comment_${videoId}_${i}`,
      videoId,
      author: `评论用户${i + 1}`,
      text: `这是第 ${i + 1} 条模拟评论内容，用于测试评论采集和回复流程。`,
      likes: Math.floor(Math.random() * 100),
      timestamp: new Date(Date.now() - i * 300000).toISOString(),
      mock: true,
    }));
  }

  /** 模拟回复评论 */
  async replyComment(commentId, text) {
    if (!this.ready) await this.init();
    const record = {
      id: `mock_reply_${Date.now()}`,
      commentId,
      text,
      status: 'success',
      timestamp: new Date().toISOString(),
      mock: true,
    };
    this.emit('comment:replied', record);
    return record;
  }

  /** 获取 Mock 统计（调试用） */
  getStats() {
    return {
      sentComments: this._sentComments.length,
      sentDms: this._sentDms.length,
      ready: this.ready,
    };
  }
}

/**
 * CDP 真实执行器（占位，待评论自动化开发完成后实现）
 * 继承 BaseExecutor，预留 CDPClient 注入点。
 */
class CDPExecutor extends BaseExecutor {
  /**
   * @param {import('./cdp-client.cjs').CDPClient} cdpClient
   */
  constructor(cdpClient) {
    super();
    this.name = 'cdp';
    this.cdp = cdpClient;
  }

  async init() {
    if (!this.cdp?.isAttached) {
      throw new Error('CDP 客户端未连接，无法初始化真实执行器');
    }
    this.ready = true;
    return { ok: true, executor: 'cdp', message: 'CDP 执行器已初始化（真实操作）' };
  }

  // 以下方法预留，待实现
  async sendComment(_videoId, _text) {
    throw new Error('CDP sendComment 待实现：请在评论自动化开发完成后对接');
  }
  async sendDm(_userId, _text) {
    throw new Error('CDP sendDm 待实现');
  }
  async getNotifications() {
    throw new Error('CDP getNotifications 待实现');
  }
  async getCommentList(_videoId) {
    throw new Error('CDP getCommentList 待实现');
  }
  async replyComment(_commentId, _text) {
    throw new Error('CDP replyComment 待实现');
  }
}

/**
 * 执行器工厂
 * @param {'mock'|'cdp'} type
 * @param {object} [deps] - { cdpClient }
 * @returns {BaseExecutor}
 */
function createExecutor(type = 'mock', deps = {}) {
  switch (type) {
    case 'cdp':
      return new CDPExecutor(deps.cdpClient);
    case 'mock':
    default:
      return new MockExecutor();
  }
}

module.exports = {
  BaseExecutor,
  MockExecutor,
  CDPExecutor,
  createExecutor,
};
