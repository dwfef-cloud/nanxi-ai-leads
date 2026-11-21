"use strict";
/**
 * 壳侧诊断日志（_verify/agent.log）。
 *
 * 为什么单独一个模块：写动作（关注 / 私信 / 点赞）的现场诊断以前只走
 * console.error，主进程 stdout 在启动器或打包场景下会丢 —— 真机上出了问题
 * 只剩一个错误码，分不清是页面改版、按钮重复、还是页面根本没加载完。
 * 抽成模块后 douyin_agent / live-social / live-dm 写的是同一个文件，
 * 排查时按时间线就能把「下发 → 打开主页 → 定位按钮 → 点击 → 回读」串起来。
 */
const fs = require("fs");
const path = require("path");

const LOG_FILE = path.join(__dirname, "_verify", "agent.log");

function trace(...parts) {
  const line = new Date().toISOString().slice(11, 23) + " " +
    parts.map((p) => (typeof p === "string" ? p : JSON.stringify(p))).join(" ") + "\n";
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, line);
  } catch (e) { /* 只读目录就放弃日志，不影响采集 */ }
  // 主进程日志已经落盘；不再向可能被启动器关闭的 stdout 写入。
}

module.exports = { trace, LOG_FILE };
