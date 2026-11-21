/**
 * 账号 → Electron session partition。
 *
 * 这是多账号隔离的唯一开关：**只有同一个 partition 才共享 cookie / 登录态**。
 * 采集窗口（douyin_agent.js）和登录/实况视图（main.js）必须用同一套规则，
 * 否则「在界面里登录账号 A」和「采集用账号 A 跑」会变成两个不同的浏览器身份。
 *
 * 为什么默认账号走 null（= 默认 session）而不是 persist:douyin-default：
 * 壳里现有的抖音登录态就存在默认 session 里，改名会让所有用户平白重扫一次码。
 * 默认账号沿用，新账号各用独立分区，是零迁移成本的方案。
 *
 * resolvePartition（带绑定的解析，2026-11-14）
 * --------------------------------------------
 * 只用 partitionOf 有一个回归：存量用户的登录态在默认 session，而多账号隔离
 * 上线后任务按 accountId 打开 persist:douyin-<id> —— 空分区里没有 cookie，
 * 抖音直接弹登录墙，任务「正常完成」但 0 条（真机实锤，搜索 0 条 vs 对照 2 条）。
 *
 * resolvePartition 用一张持久化绑定表解决：
 *   - 账号第一次出现：默认分区登录态空闲 → 绑定 default（存量单账号零迁移）；
 *     已被其他账号占用 → 绑定自己的 persist 分区。
 *   - 登录/实况视图与采集窗口都走 resolvePartition，身份永远一致。
 *   - 绑定表落 userData/shell-partition-bindings.json，重启不丢。
 */
"use strict";

const MAX_ID = 40;

/** 纯函数：账号 → 分区名（null = 默认 session）。历史上各处直接依赖它。 */
function partitionOf(accountId) {
  const id = String(accountId == null ? "" : accountId).trim();
  if (!id || id === "default") return null;
  // partition 名只允许安全字符：账号 id 可能来自用户输入或外部数据
  return "persist:douyin-" + id.replace(/[^\w.-]/g, "_").slice(0, MAX_ID);
}

const memBindings = new Map(); // electron 不可用（单测/脚本）时的降级容器
let bindingsLoaded = false;
let bindingsFile = null;
let defaultLoginHint = true; // 探测未完成前按「默认分区可能有登录态」处理，对存量更安全

function bindingsPath() {
  if (bindingsFile !== null) return bindingsFile;
  try {
    const { app } = require("electron");
    bindingsFile = require("path").join(
      app.getPath("userData"), "shell-partition-bindings.json");
  } catch (e) {
    bindingsFile = ""; // 非 Electron 环境：只用内存
  }
  return bindingsFile;
}

function loadBindings() {
  if (bindingsLoaded) return memBindings;
  bindingsLoaded = true;
  const p = bindingsPath();
  if (!p) return memBindings;
  try {
    const raw = JSON.parse(require("fs").readFileSync(p, "utf8"));
    for (const [k, v] of Object.entries(raw || {})) {
      memBindings.set(String(k), v === null ? null : String(v));
    }
  } catch (e) { /* 首次运行没有该文件 */ }
  return memBindings;
}

function saveBindings() {
  const p = bindingsPath();
  if (!p) return;
  try {
    const obj = {};
    for (const [k, v] of memBindings) obj[k] = v;
    require("fs").writeFileSync(p, JSON.stringify(obj, null, 2), "utf8");
  } catch (e) { /* 写不进去只影响重启后的记忆，不影响本次运行 */ }
}

/** 壳启动后异步探测默认 session 是否有抖音登录 cookie，探测完调用一次。 */
function setDefaultLoginHint(hasLogin) {
  defaultLoginHint = !!hasLogin;
}

function defaultTaken(map, selfId) {
  for (const [k, v] of map) {
    if (k !== selfId && v === null) return true;
  }
  return false;
}

/**
 * 账号 → 实际分区（带绑定）。与 partitionOf 的区别：会记住「这个账号用哪个分区」，
 * 保证存量登录态不被多账号隔离隔丢。
 */
function resolvePartition(accountId) {
  const id = String(accountId == null ? "" : accountId).trim();
  if (!id || id === "default") return null;
  const map = loadBindings();
  if (map.has(id)) return map.get(id);
  let chosen = partitionOf(id);
  if (defaultLoginHint && !defaultTaken(map, id)) chosen = null; // 存量登录态继续可用
  map.set(id, chosen);
  saveBindings();
  return chosen;
}

/** 显式绑定（保留给「明确知道账号该用哪个分区」的调用方，如登录流程）。 */
/** 只读探测：这个账号有没有定过分区，定到了哪一个（不会写入、不会占用默认 session）。 */
function bindingOf(accountId) {
  const id = String(accountId == null ? "" : accountId).trim();
  if (!id || id === "default") return { bound: true, partition: null };
  const map = loadBindings();
  if (!map.has(id)) return { bound: false, partition: null };
  return { bound: true, partition: map.get(id) };
}

function bindPartition(accountId, partition) {
  const id = String(accountId == null ? "" : accountId).trim();
  if (!id || id === "default") return;
  loadBindings().set(id, partition === null ? null : String(partition));
  saveBindings();
}

module.exports = { partitionOf, resolvePartition, bindingOf, bindPartition, setDefaultLoginHint };
