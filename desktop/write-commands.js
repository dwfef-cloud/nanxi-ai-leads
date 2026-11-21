function videoIdOf(rawUrl) {
  try {
    const url = new URL(String(rawUrl || ""));
    if (url.protocol !== "https:" || url.hostname !== "www.douyin.com" ||
        url.username || url.password || url.port) return "";
    const match = /^\/video\/([0-9]{6,})\/?$/.exec(url.pathname);
    return match ? match[1] : "";
  } catch (_) { return ""; }
}

// 用户主页地址 → sec_uid。批量跟进的目标是「人」而不是「视频」，
// 所以作用域里的 videoUrl 存的是主页链接，校验时要用这套规则。
function profileIdOf(rawUrl) {
  try {
    const url = new URL(String(rawUrl || ""));
    if (url.protocol !== "https:" || url.hostname !== "www.douyin.com" ||
        url.username || url.password || url.port) return "";
    const match = /^\/user\/([A-Za-z0-9_-]{6,160})\/?$/.exec(url.pathname);
    return match ? match[1] : "";
  } catch (_) { return ""; }
}

function validateComment(command) {
  const videoId = videoIdOf(command && command.videoUrl);
  const text = String((command && command.text) || "").trim();
  if (!videoId) return { ok: false, error: "需要有效的抖音视频链接" };
  if (text.length < 5 || text.length > 200) {
    return { ok: false, error: "评论须为 5 到 200 个字符" };
  }
  return { ok: true, videoId, text };
}

const COMMENT_PROBE_JS = `(() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 100 && r.height > 12 && r.top >= 0 && r.bottom < innerHeight &&
      r.left >= 0 && r.right <= innerWidth;
  };
  const textOf = (el) => [el.getAttribute('placeholder'), el.getAttribute('aria-label'),
    el.getAttribute('data-placeholder')].filter(Boolean).join(' ');
  const editors = Array.from(document.querySelectorAll('textarea,input,[contenteditable="true"]'))
    .filter(el => visible(el) && el.getAttribute('type') !== 'search' &&
      el.getAttribute('type') !== 'file' && ['评论', '说点', '留下'].some(word => textOf(el).includes(word)));
  if (editors.length !== 1) return { found: false, reason: 'comment-editor-ambiguous-or-missing' };
  const editor = editors[0];
  if (!editor) return { found: false, reason: 'comment-editor-not-found' };
  const r = editor.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (!hit || (hit !== editor && !editor.contains(hit) && !hit.contains(editor))) {
    return { found: false, reason: 'comment-editor-obscured' };
  }
  return { found: true, x, y,
    existingText: String(editor.value == null ? editor.innerText || '' : editor.value).trim() };
})()`;

const COMMENT_SUBMIT_JS = `(() => {
  const editor = document.activeElement;
  if (!editor || !editor.matches('textarea,input,[contenteditable="true"]')) return null;
  const r = editor.getBoundingClientRect();
  const candidates = Array.from(document.querySelectorAll('button,[role="button"]'))
    .filter(el => {
      const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
      const b = el.getBoundingClientRect();
      return (t === '发布' || t === '发送' || t === '发表评论') &&
        b.width > 15 && b.height > 15 && b.left >= r.left - 40 &&
        b.left < r.right + 180 && b.top >= r.top - 60 && b.top < r.bottom + 100 &&
        b.right <= innerWidth && b.bottom <= innerHeight &&
        !el.disabled && el.getAttribute('aria-disabled') !== 'true';
    });
  if (candidates.length !== 1) return null;
  const b = candidates[0].getBoundingClientRect();
  const x = b.left + b.width / 2, y = b.top + b.height / 2;
  const hit = document.elementFromPoint(x, y);
  if (!hit || (hit !== candidates[0] && !candidates[0].contains(hit))) return null;
  return { x, y };
})()`;

const COMMENT_HOOK_JS = `(() => {
  if (window.__commentReceiptHook) return true;
  window.__commentReceiptHook = true;
  window.__commentReceipts = [];
  window.__commentReceiptSeq = 0;
  const relevant = (url) => {
    const s = String(url || '');
    return s.includes('/comment/publish') || s.includes('/comment/post');
  };
  const targetOf = (url, payload) => {
    try {
      const query = new URL(String(url || ''), location.href).searchParams;
      let params = query;
      if (payload && typeof payload.get === 'function') params = payload;
      if (typeof payload === 'string') {
        try {
          const object = JSON.parse(payload);
          params = new URLSearchParams(object);
        } catch (_) { params = new URLSearchParams(payload); }
      }
      return { videoId: String(params.get('aweme_id') || query.get('aweme_id') || ''),
        commentId: String(params.get('reply_id') || query.get('reply_id') || '') };
    } catch (_) {}
    return { videoId: '', commentId: '' };
  };
  const add = (url, status, body, target) => {
    if (!relevant(url)) return;
    // Parse before reducing: slicing the original JSON cut real platform
    // responses in the middle and made successful submissions unparseable.
    // Keep only confirmation fields, never the returned user profile.
    let confirmation = '';
    try {
      const parsed = JSON.parse(String(body || ''));
      const c = parsed.comment;
      confirmation = JSON.stringify({ status_code: parsed.status_code,
        ...(c && typeof c === 'object' ? {comment: {
          cid: c.cid, id: c.id, aweme_id: c.aweme_id, reply_id: c.reply_id,
          image_list: Array.isArray(c.image_list) ? c.image_list.slice(0, 1).map(i=>({uri:i?.origin_image?.uri||i?.uri||''})) : [],
          sticker_id:String(c.sticker?.id||c.sticker_id||c.emoji?.id||''),
          text_extra:Array.isArray(c.text_extra)?c.text_extra.slice(0,20).map(m=>({type:m?.type,
            user_id:String(m?.sec_uid||m?.user_id||''),start:m?.start,end:m?.end})):[],
          text: typeof c.text === 'string' ? c.text.slice(0, 500).split(String.fromCharCode(160)).join(' ') : ''
        }} : {}) });
    } catch (_) { /* malformed responses remain uncertain */ }
    window.__commentReceipts.push({ seq: ++window.__commentReceiptSeq,
      targetVideoId: target.videoId, targetCommentId: target.commentId,
      url: String(url).split('?')[0].slice(0, 180), status,
      body: confirmation });
    if (window.__commentReceipts.length > 10) window.__commentReceipts.shift();
  };
  const oldFetch = window.fetch;
  window.fetch = function(...args) {
    const url = String((args[0] && args[0].url) || args[0] || '');
    const target = targetOf(url, args[1] && args[1].body);
    return oldFetch.apply(this, args).then(res => {
      if (relevant(url)) res.clone().text().then(body => add(url, res.status, body, target)).catch(() => {});
      return res;
    });
  };
  const oldOpen = XMLHttpRequest.prototype.open;
  const oldSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(method, url, ...rest) {
    this.__commentUrl = String(url || '');
    return oldOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function(...args) {
    if (relevant(this.__commentUrl)) {
      const target = targetOf(this.__commentUrl, args[0]);
      this.addEventListener('loadend', () => {
        try { add(this.__commentUrl, this.status, this.responseText, target); }
        catch (_) { add(this.__commentUrl, this.status, '', target); }
      }, { once: true });
    }
    return oldSend.apply(this, args);
  };
  return true;
})()`;

function classifyCommentReceipts(entries, expected = null) {
  if (!Array.isArray(entries) || entries.length === 0) return "uncertain";
  for (const entry of entries) {
    const status = Number(entry && entry.status);
    let body;
    try { body = JSON.parse(String((entry && entry.body) || "")); }
    catch (_) { continue; }
    if (!body || typeof body !== "object" || !Number.isInteger(body.status_code)) continue;
    const comment = body.comment;
    if (expected && String(entry.targetVideoId || (comment && comment.aweme_id) || "") !== expected.videoId) continue;
    if (expected && comment && comment.aweme_id && String(comment.aweme_id) !== expected.videoId) continue;
    const parent = value => String(value || "") === "0" ? "" : String(value || "");
    if (expected && parent(entry.targetCommentId) !== parent(expected.commentId)) continue;
    if (expected && comment && parent(comment.reply_id) !== parent(expected.commentId)) continue;
    if (!Number.isInteger(status) || status < 100) return "uncertain";
    if (status < 200 || (status >= 300 && status < 400) || status >= 500) return "uncertain";
    if (status >= 200 && status < 300 && body.status_code === 0 &&
        comment && (typeof comment.cid === 'string' && comment.cid.trim() ||
                    typeof comment.id === 'string' && comment.id.trim()) &&
        (!expected?.imageUri || Array.isArray(comment.image_list) && comment.image_list.length===1 &&
          (comment.image_list[0]?.origin_image?.uri||comment.image_list[0]?.uri)===expected.imageUri) &&
        (!expected?.stickerId || String(comment.sticker_id||comment.sticker?.id||comment.emoji?.id||'')===expected.stickerId) &&
        (!expected?.mentions?.length || mentionsConfirmed(comment.text_extra,expected.mentions,expected.text)) &&
        (!expected || comment.text === expected.text)) return "sent_ok";
    if (status >= 200 && status < 300 && body.status_code === 0) return "uncertain";
    return "sent_fail";
  }
  return "uncertain";
}

function mentionsConfirmed(extras,targets,text) {
  if(!Array.isArray(extras)||!Array.isArray(targets))return false;
  const users=extras.filter(m=>m&&m.type===0);
  return users.length===targets.length&&targets.every((target,i)=>{
    const m=users[i],id=String(m.sec_uid||m.user_id||'');
    return id===target.id&&Number.isInteger(m.start)&&Number.isInteger(m.end)&&m.start>=0&&m.end<=text.length&&m.end>m.start&&
      text.slice(m.start,m.end)==='@'+target.name&&(i===0||m.start>=users[i-1].end);
  });
}

function classifyDiggReceipts(entries) {
  if (!Array.isArray(entries) || entries.length === 0) {
    return { ok: false, failed: false };
  }
  for (const entry of entries) {
    const status = Number(entry && entry.status);
    let body;
    try { body = JSON.parse(String((entry && entry.body) || "")); }
    catch (_) { continue; }
    if (!body || typeof body !== "object" || !Number.isInteger(body.status_code)) continue;
    if (!Number.isInteger(status) || status < 100 || status >= 500) {
      return { ok: false, failed: false, note: "http-error" };
    }
    if (status >= 200 && status < 300 && body.status_code === 0) {
      return { ok: true, failed: false, status };
    }
    if (body.status_code !== 0) {
      return { ok: false, failed: true, code: body.status_code, status };
    }
  }
  return { ok: false, failed: false, note: "no-status-code" };
}

module.exports = {
  videoIdOf, profileIdOf, validateComment, classifyCommentReceipts, classifyDiggReceipts, mentionsConfirmed,
  COMMENT_PROBE_JS, COMMENT_SUBMIT_JS, COMMENT_HOOK_JS,
};
