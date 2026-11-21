/**
 * 账号池 · CK（Cookie）读写 & 作品发布素材选择。
 *
 * 为什么要壳来做
 * -------------
 * 抖音登录态是 httpOnly cookie，只存在 Electron 的 session 分区里，后端读不到也写不进。
 * 所以「粘贴 CK 登录」「导出当前 CK」「选素材文件」这三类事只能在壳内完成，
 * 后端通过 /desktop/command 的 shell-task 下发、等回执。
 *
 * 纪律
 * ----
 * - 写 cookie 必须落到**该账号自己的分区**（session-partition.resolvePartition），
 *   否则 A 账号的 CK 会覆盖默认登录态，B 账号看起来「登录了但不是我」；
 * - 读不到就返回 ok:false + 原因，不返回空数组冒充「这个账号没有 cookie」；
 * - 选素材按页面写死的规则先校验（视频 1 个/≤16GB/≤1 小时，图文 ≤35 张/≤50MB），
 *   不合格直接拒绝并说明是哪一条，别等发布时才失败。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const MAX_COOKIE_FILE_BYTES = 2 * 1024 * 1024;
const MAX_VIDEO_BYTES = 16 * 1024 * 1024 * 1024;
const MAX_IMAGE_BYTES = 50 * 1024 * 1024;
const MAX_IMAGE_COUNT = 35;
const MAX_VIDEO_SECONDS = 3600;

/**
 * 账号 → 写 CK / 读 CK 用的 session。
 *
 * 与采集用的 resolvePartition 有一处关键区别：**绝不把新账号悄悄绑到默认 session**。
 * resolvePartition 为了让存量单账号「打开就能用」，遇到空闲的默认 session 会直接绑定，
 * 那对采集是便利，对导入 CK 却是事故 —— 把 B 账号的 sessionid 写进默认 session，
 * 等于把已经登录的 A 账号顶下线。所以这里只认「已经定过的分区」，没有就用自己的。
 */
function sessionFor(accountId) {
  const { session } = require("electron");
  const sp = require("./session-partition");
  const id = String(accountId || "").trim();
  if (!id) return session.defaultSession;
  const bound = sp.bindingOf(id);
  if (bound.bound) {
    return bound.partition ? session.fromPartition(bound.partition) : session.defaultSession;
  }
  const partition = sp.partitionOf(id);
  sp.bindPartition(id, partition);   // 记住这个账号归这个分区，后续采集/检测保持一致
  return partition ? session.fromPartition(partition) : session.defaultSession;
}

const DOUYIN_DOMAINS = ["douyin.com", "iesdouyin.com"];

/** 把一批 {name,value,...} 写进账号分区。 */
async function applyCookies(accountId, cookies) {
  const list = Array.isArray(cookies) ? cookies : [];
  if (!accountId) return { ok: false, error: "缺少账号 id" };
  if (!list.length) return { ok: false, error: "没有可写入的 Cookie" };
  const ses = sessionFor(accountId);
  let applied = 0;
  const failed = [];
  for (const c of list) {
    const name = String((c && c.name) || "").trim();
    if (!name) continue;
    const domain = String(c.domain || ".douyin.com");
    const detail = {
      url: "https://www.douyin.com",
      name,
      value: String(c.value == null ? "" : c.value),
      domain,
      path: String(c.path || "/"),
      secure: c.secure !== false,
      httpOnly: c.httpOnly === true,
    };
    if (c.expirationDate) detail.expirationDate = Number(c.expirationDate);
    try {
      await ses.cookies.set(detail);
      applied += 1;
    } catch (e) {
      // 单个 cookie 被拒（例如过期时间非法）不应让整批失败，但要回报
      failed.push(`${name}: ${String((e && e.message) || e).slice(0, 80)}`);
    }
  }
  if (!applied) {
    return { ok: false, error: `Cookie 全部写入失败：${failed.slice(0, 3).join("；") || "未知原因"}` };
  }
  return { ok: true, applied, failed: failed.length, failedNames: failed.slice(0, 5) };
}

/** 读账号分区里当前的抖音 Cookie，拼成 `a=b; c=d` 文本。 */
async function readCookies(accountId) {
  const ses = sessionFor(accountId);
  let all = [];
  try {
    all = await ses.cookies.get({});
  } catch (e) {
    return { ok: false, error: `读取 Cookie 失败：${String((e && e.message) || e)}` };
  }
  const picked = all.filter((c) => {
    const domain = String(c.domain || "").replace(/^\./, "");
    return DOUYIN_DOMAINS.some((d) => domain === d || domain.endsWith("." + d));
  });
  if (!picked.length) return { ok: false, error: "该账号分区里没有抖音 Cookie（未登录）" };
  const cookies = picked.map((c) => ({
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path || "/",
    secure: !!c.secure,
    httpOnly: !!c.httpOnly,
    expirationDate: c.expirationDate || c.expirationTime || null,
  }));
  return {
    ok: true,
    count: cookies.length,
    cookieText: cookies.map((c) => `${c.name}=${c.value}`).join("; "),
    cookies,
  };
}

/** 文件选择框：一次读多个 CK 文件（.txt/.json/.cookie）。 */
async function pickCookieFiles() {
  const { dialog } = require("electron");
  const res = await dialog.showOpenDialog({
    title: "选择 CK 文件",
    properties: ["openFile", "multiSelections"],
    filters: [
      { name: "Cookie 文件", extensions: ["txt", "json", "cookie", "log"] },
      { name: "全部文件", extensions: ["*"] },
    ],
  });
  if (!res || res.canceled || !res.filePaths.length) return { ok: false, canceled: true, files: [] };
  const files = [];
  for (const full of res.filePaths.slice(0, 200)) {
    const name = path.basename(full);
    let stat;
    try {
      stat = fs.statSync(full);
    } catch (e) {
      files.push({ name, text: "", error: "文件读取失败" });
      continue;
    }
    if (stat.size > MAX_COOKIE_FILE_BYTES) {
      files.push({ name, text: "", error: "文件超过 2MB" });
      continue;
    }
    try {
      files.push({ name, size: stat.size, text: fs.readFileSync(full, "utf8") });
    } catch (e) {
      files.push({ name, text: "", error: String((e && e.message) || e).slice(0, 80) });
    }
  }
  return { ok: files.some((f) => f.text), files };
}

/** 写剪贴板（导出 CK 用）。 */
function copyText(text) {
  const { clipboard } = require("electron");
  const value = String(text || "");
  if (!value) return { ok: false, error: "没有可复制的文本" };
  clipboard.writeText(value);
  return { ok: clipboard.readText() === value, bytes: value.length };
}

/** 读 MP4/MOV 的 mvhd，拿真实时长（秒）。读不出来返回 null。 */
function mp4DurationSeconds(file) {
  let fd;
  try {
    fd = fs.openSync(file, "r");
    const head = Buffer.alloc(64);
    fs.readSync(fd, head, 0, 64, 0);
    // 顶层 box 依次扫，找 moov → mvhd
    const size = fs.statSync(file).size;
    let offset = 0;
    while (offset + 8 <= size) {
      const buf = Buffer.alloc(16);
      fs.readSync(fd, buf, 0, 16, offset);
      const boxSize = buf.readUInt32BE(0);
      const type = buf.toString("ascii", 4, 8);
      if (boxSize < 8) break;
      if (type === "moov") {
        // 在 moov 里找 mvhd
        let inner = offset + 8;
        const end = offset + boxSize;
        while (inner + 8 <= end) {
          const h = Buffer.alloc(16);
          fs.readSync(fd, h, 0, 16, inner);
          const s2 = h.readUInt32BE(0);
          const t2 = h.toString("ascii", 4, 8);
          if (s2 < 8) break;
          if (t2 === "mvhd") {
            const body = Buffer.alloc(Math.min(s2, 256));
            fs.readSync(fd, body, 0, body.length, inner);
            const version = body.readUInt8(8);
            if (version === 1) {
              const timescale = body.readUInt32BE(28);
              const duration = Number(body.readBigUInt64BE(32));
              return timescale ? duration / timescale : null;
            }
            const timescale = body.readUInt32BE(20);
            const duration = body.readUInt32BE(24);
            return timescale ? duration / timescale : null;
          }
          inner += s2;
        }
        return null;
      }
      offset += boxSize;
    }
    return null;
  } catch (e) {
    return null;
  } finally {
    if (fd !== undefined) {
      try { fs.closeSync(fd); } catch (e) { /* ignore */ }
    }
  }
}

const VIDEO_EXT = ["mp4", "mov", "m4v", "avi", "mkv", "flv", "wmv", "webm"];
const IMAGE_EXT = ["jpg", "jpeg", "png", "webp", "bmp", "heic"];

/** 选作品素材：视频单选多个（每个一段作品），图文多选（一组 ≤35 张）。 */
async function pickPublishMedia(type) {
  const isImage = String(type || "video") === "image";
  const { dialog } = require("electron");
  const res = await dialog.showOpenDialog({
    title: isImage ? "选择图片（每组最多 35 张）" : "选择视频（每个视频一篇作品）",
    properties: ["openFile", "multiSelections"],
    filters: isImage
      ? [{ name: "图片", extensions: IMAGE_EXT }]
      : [{ name: "视频", extensions: VIDEO_EXT }],
  });
  if (!res || res.canceled || !res.filePaths.length) {
    return { success: false, canceled: true, msg: "已取消选择" };
  }
  const paths = res.filePaths;
  if (isImage && paths.length > MAX_IMAGE_COUNT) {
    return { success: false, msg: `一次最多选择 ${MAX_IMAGE_COUNT} 张图片（本次 ${paths.length} 张）` };
  }
  const files = [];
  for (const full of paths) {
    const name = path.basename(full);
    let stat;
    try {
      stat = fs.statSync(full);
    } catch (e) {
      return { success: false, msg: `${name}：文件读取失败` };
    }
    const ext = (path.extname(full).slice(1) || "").toLowerCase();
    if (isImage) {
      if (stat.size > MAX_IMAGE_BYTES) {
        return { success: false, msg: `${name}：单张图片超过 50MB` };
      }
      if (!IMAGE_EXT.includes(ext)) {
        return { success: false, msg: `${name}：不是支持的图片格式` };
      }
      files.push({
        id: `media_${stat.size}_${path.basename(full, path.extname(full)).replace(/[^\w.-]/g, "_").slice(0, 32)}`,
        type: "image",
        filePath: full,
        filePaths: [full],
        fileName: name,
        sizeBytes: stat.size,
      });
    } else {
      if (stat.size > MAX_VIDEO_BYTES) {
        return { success: false, msg: `${name}：视频超过 16GB` };
      }
      if (!VIDEO_EXT.includes(ext)) {
        return { success: false, msg: `${name}：不是支持的视频格式` };
      }
      const seconds = ext === "mp4" || ext === "mov" || ext === "m4v" ? mp4DurationSeconds(full) : null;
      if (seconds && seconds > MAX_VIDEO_SECONDS) {
        return {
          success: false,
          msg: `${name}：时长 ${Math.round(seconds / 60)} 分钟，超过 1 小时上限`,
        };
      }
      files.push({
        id: `media_${stat.size}_${path.basename(full, path.extname(full)).replace(/[^\w.-]/g, "_").slice(0, 32)}`,
        type: "video",
        filePath: full,
        filePaths: [full],
        fileName: name,
        sizeBytes: stat.size,
        durationSeconds: seconds ? Math.round(seconds) : null,
        durationKnown: seconds != null,
      });
    }
  }
  return { success: true, files };
}

/** 选封面：单图，返回同一套素材字段（页面直接塞给 media.cover）。 */
async function pickPublishCover() {
  const res = await pickPublishMedia("image");
  if (!res.success) return res;
  const file = res.files[0];
  if (!file) return { success: false, msg: "没有选择封面图片" };
  return { success: true, file };
}

/** 打开一个本地路径（发布成功后「打开文件位置」之类）。 */
function revealPaths(paths) {
  const { shell } = require("electron");
  const list = (Array.isArray(paths) ? paths : [paths]).filter(Boolean);
  const opened = [];
  for (const p of list) {
    try {
      shell.showItemInFolder(String(p));
      opened.push(p);
    } catch (e) { /* ignore */ }
  }
  return { ok: opened.length > 0, opened };
}

module.exports = {
  applyCookies, readCookies, pickCookieFiles, copyText,
  pickPublishMedia, pickPublishCover, revealPaths, mp4DurationSeconds,
};
