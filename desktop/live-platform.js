"use strict";
// Existing video/comment probes were observed on 2026-11-15. Native review binds
// exact actions; the separate DM adapter still needs live selector/protocol QA.
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const {EXTRA_FIELDS,signatureFields}=require('./leadgen-extensions');
const { videoIdOf, profileIdOf, validateComment, COMMENT_HOOK_JS, classifyCommentReceipts } = require("./write-commands");
const { trace: diagTrace } = require("./diag-log");
const FIELDS = ["type", "accountId", "leadgenTaskId", "videoUrl", "commentId", "text", "scopeId", "deadlineAt"];
const TYPES = new Set(["post-comment", "reply-comment", "send-dm", "favorite-video", "like-video", "copy-video-link", "like-comment", "follow-user", "share-profile", "share-comment"]);
// 批量跟进一次最多 5000 条线索（与后端 live_scope.BATCH_MAX_PER_ACTION 对齐）。
const BATCH_MAX_CAP = 5000;
// 目标「人」而不是「内容」的动作：批量跟进里这些命令的 videoUrl 是用户主页地址。
const PERSON_ACTIONS = new Set(["follow-user", "send-dm", "share-profile"]);

function targetIdOfCommand(command, scope) {
  if (scope && scope.mode === 'batch' && PERSON_ACTIONS.has(command.type))
    return profileIdOf(command.videoUrl) || (/^[A-Za-z0-9_-]{6,160}$/.test(String(command.recipientId || '')) ? String(command.recipientId) : '');
  return videoIdOf(command.videoUrl);
}

function scopeFile(command) {
  const dir=process.env.LEADS_LEADGEN_SCOPE_DIR;
  const accountFile=dir&&path.join(dir,crypto.createHash('sha256').update(String(command.accountId||'')).digest('hex')+'.json');
  // 监控 / 自动回复的作用域按「任务+账号」命名，目录也不在 LEADGEN 那几个环境变量里。
  // 后端会把文件路径随指令下发（scopeFile），这里必须优先认它 —— 否则运行时发现的
  // 目标一条都发不出去，全部判 live_writes_disabled（2026-11-17 真机踩过）。
  const files=[String(command.scopeFile||'').trim(),accountFile,
    command.type==='like-comment'&&process.env.LEADS_COMMENT_LIKE_SCOPE,
    process.env.LEADS_LEADGEN_SCOPE,process.env.LEADS_LIVE_ACCEPTANCE_SCOPE].filter(Boolean);
  return files.find(file=>{try{const s=JSON.parse(fs.readFileSync(file,'utf8'));
    return s.accountId===command.accountId&&s.leadgenTaskId===command.leadgenTaskId&&s.scopeId===command.scopeId;
  }catch(_){return false;}});
}

/**
 * 谁来证明「页面此刻真的可以点了」。
 *
 * 人工审核 / 直播预告那条链路等的是审核台面板里渲染出首帧（ensureLivePreview）；
 * monitor（自动回复、无人值守）没有那块面板，等它只会永远等不到，
 * 于是每个动作都在最后一步被判 live_preview_not_*。
 * 这里把两种口径分开：monitor 用采集页的「可见且真的渲染了一帧」等价核验（ensureActionable），
 * 其余一律保持原来的首帧要求，审核门槛不变。
 */
async function ensureActable(io, scope) {
  try {
    if (scope && (scope.mode === 'monitor' || scope.mode === 'batch' || scope.mode === 'stream') && typeof io.ensureActionable === 'function')
      return (await io.ensureActionable()) === true;
    if (typeof io.ensureLivePreview !== 'function') return false;
    return (await io.ensureLivePreview()) === true;
  } catch (_) {
    // 核验本身挂掉（页面被隐藏、渲染进程断开）＝ 没确认，不是异常；
    // 让它冒到外层只会变成一句含糊的 *_preparation_failed，看不出卡在「不可点击」。
    return false;
  }
}

function scopeFor(command, file = scopeFile(command)) {
  try {
    if (!file || !TYPES.has(command.type)) return null;
    if(!require('./live-comment-extras').validExtras(command))return null;
    if(command.saveProfileQr&&(command.type!=='share-profile'||command.saveProfileQr!=='1'||typeof command.profileQrRunKey!=='string'||!command.profileQrRunKey.trim()||command.profileQrRunKey.length>160||/[\x00-\x1f/\\]/.test(command.profileQrRunKey)))return null;
    const scope = JSON.parse(fs.readFileSync(file, "utf8"));
    if (typeof scope.secret !== 'string' || scope.secret.length < 32 ||
        typeof scope.expiresAt !== 'number' || !Number.isFinite(scope.expiresAt) || scope.expiresAt * 1000 <= Date.now() ||
        !Number.isSafeInteger(command.deadlineAt) || command.deadlineAt <= Date.now() || command.deadlineAt > Date.now()+110000 ||
        scope.accountId !== command.accountId || scope.leadgenTaskId !== command.leadgenTaskId ||
        scope.scopeId !== command.scopeId || !scope.nickname ||
        !Array.isArray(scope.videoUrls) ||
        // 监控作用域用 "*" 表示「本任务范围内的任意目标」：评论/收件人都是运行时
        // 才出现的，事前写不进具体 URL。批量跟进的目标是线索库里的人，同样如此。
        // 具体目标仍由签名逐条绑定。
        !(scope.videoUrls.includes(command.videoUrl) ||
          ((scope.mode === 'monitor' || scope.mode === 'batch' || scope.mode === 'stream') && scope.videoUrls.includes('*'))) ||
        !Number.isInteger(scope.actions?.[command.type]) || scope.actions[command.type]<1 ||
        scope.actions[command.type] > (scope.mode === 'batch' ? BATCH_MAX_CAP : 100) ||
        !targetIdOfCommand(command, scope)) return null;
    if(Object.hasOwn(scope,'reviewedActions')) {
      if(!Array.isArray(scope.reviewedActions)||!scope.reviewedActions.some(a=>a &&
        a.type===command.type && a.videoUrl===command.videoUrl && a.text===command.text &&
        (a.commentId||'')===(command.commentId||'') && (a.commentAuthor||'')===(command.commentAuthor||'') &&
        (a.recipientId||'')===(command.recipientId||'') && EXTRA_FIELDS.every(k=>(a[k]??'')===(command[k]??''))))return null;
      if(scope.reviewedActions.filter(a=>a.type===command.type).length!==scope.actions[command.type])return null;
    } else if(scope.mode==='monitor' || scope.mode==='batch' || scope.mode==='stream') {
      // 监控 / 批量 / 顺序获客任务：评论和收件人是运行时才出现的，事前没法列白名单。
      // 这里换一套等价约束：作用域绑定任务+账号+有效期、动作次数有上限（上面已校验）、
      // 每条命令的账号/视频/评论/正文/收件人全部进签名（下面统一验签）、
      // 且每个目标只允许一次尝试（claimAttempt 按目标分文件）。
      // 发主评论（post-comment）由明确启动的顺序任务或批量路径放行，监控模式不放行；
      // 批量跟进是用户明确勾了「首作评论」的线索清单，按签名逐条绑定后放行。
      if(!scope.nickname || (command.type==='post-comment' && scope.mode==='monitor')) return null;
      if(scope.mode==='stream' && (!Number.isInteger(scope.interactionLimit) ||
          scope.interactionLimit<1 || scope.interactionLimit>100))return null;
    } else if(['send-dm','follow-user','share-profile','share-comment'].includes(command.type)||EXTRA_FIELDS.some(k=>command[k])||scope.actions[command.type]!==1)return null;
    if(command.type==='like-comment' && scope.mode!=='monitor' && scope.mode!=='stream' && (!Array.isArray(scope.commentTargets)||!scope.commentTargets.some(t=>
      t && t.videoUrl===command.videoUrl && t.commentId===command.commentId &&
      t.authorName===command.commentAuthor && t.content===command.text)))return null;
    if(command.type==='post-comment' && Object.hasOwn(scope,'reviewedComments') &&
      (!Array.isArray(scope.reviewedComments)||!scope.reviewedComments.some(t=>
        t && t.videoUrl===command.videoUrl && t.text===command.text)))return null;
    const fields=signatureFields(command,FIELDS);
    const signature = crypto.createHmac("sha256", scope.secret)
      .update(JSON.stringify(fields.map(key => String(command[key] || "")))).digest("hex");
    const supplied = String(command.acceptanceSignature || "");
    if (signature.length !== supplied.length ||
        !crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(supplied))) return null;
    return { ...scope, file };
  } catch (_) { return null; }
}

function claimAttempt(scope, action, command) {
  const dir = path.join(path.dirname(scope.file), "attempts");
  fs.mkdirSync(dir, { recursive: true });
  if(scope.reviewedActions && !command)return false;
  if(scope.mode==='stream') {
    let total=0,byAction=0;
    for(const name of fs.readdirSync(dir).filter(name=>name.endsWith('.claim'))){
      try {
        const item=JSON.parse(fs.readFileSync(path.join(dir,name),'utf8'));
        if(item.action){total++;if(item.action===action)byAction++;}
      } catch(_) {}
    }
    if(total>=scope.interactionLimit || byAction>=scope.actions[action])return false;
  }
  // 监控模式要处理多个评论/多个收件人，按「目标」分文件；
  // 人工确认路径保持原来的「每个动作只许一次」，行为不变。
  const target=scope.reviewedActions
    ? JSON.stringify([action,command.videoUrl,command.commentId||'',command.recipientId||''])
    : ((scope.mode==='monitor'||scope.mode==='batch'||scope.mode==='stream') && command
        ? JSON.stringify([action,command.videoUrl,command.commentId||'',command.recipientId||'',command.text||''])
        : action);
  const key = crypto.createHash("sha256").update(scope.scopeId + ":" + target).digest("hex");
  try {
    const fd = fs.openSync(path.join(dir, key + ".claim"), "wx");
    fs.writeSync(fd, JSON.stringify({ action, createdAt: Date.now() }));
    fs.closeSync(fd);
    return true;
  } catch (error) {
    if (error.code === "EEXIST") return false;
    throw error;
  }
}

// 当前画面里的视频 id：找屏幕中间那条播放器，再从它所在的 panel（类名 video_<id>）
// 取 id。详情浮层是「上一条/当前/下一条」三段竖排，用 data-e2e=feed-active-video
// 偶尔会拿到邻条，按视口位置判断最稳；都认不出来才退回地址。
const STAGE_ID_JS = `
  const vh = window.innerHeight || 900;
  let stage = null;
  for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect();
    if (r.height > 60 && r.top < vh * 0.7 && r.top + r.height > vh * 0.3) { stage = v; break; }
  }
  let videoId = '';
  if (stage) {
    let el = stage.parentElement;
    for (let d = 0; el && d < 8; d++, el = el.parentElement) {
      const m = /video_([0-9]{6,})/.exec(String(el.className || ''));
      if (m) { videoId = m[1]; break; }
    }
  }
  if (!videoId) {
    const p = String(location.pathname || ''), at = p.indexOf('/video/');
    if (at >= 0) videoId = String(p.slice(at + 7).split('/')[0] || '');
  }
`;

const IDENTITY_JS = `(() => {
${STAGE_ID_JS}
  return {
  url: location.href,
  videoId,
  loginWall: !!document.querySelector('#login-pannel,[data-e2e="login-guide"],.login-mask'),
  verify: !!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]'),
  self: Array.from(document.querySelectorAll('header [class*="userMenuPanel"]'))
    .map(e => e.textContent || '').join(' ')
  };
})()`;

// 同一个判断的轻量版：只回 id，用于「是不是已经停在目标视频上了」。
const CURRENT_VIDEO_JS = `(() => {
${STAGE_ID_JS}
  return videoId;
})()`;

/**
 * 「这条评论在页面上出现了几条、其中几条署我的名」。
 *
 * 为什么需要 before/after 两次采样：抖音评论区里「文本一模一样」太常见（模板文案），
 * 只看「页面上有没有这段文字」会把别人的评论认成自己刚发的 —— 假成功。
 * 提交前拍基线、提交后比较增量，并且要求新增那条的署名就是当前账号，才认。
 */
function ownCommentProbe(text, nickname) {
  return `(() => {
    const normalize = value => String(value || '')
      .split(String.fromCharCode(160)).join(' ').replace(/\\s+/g, ' ').trim();
    const want = normalize(${JSON.stringify(String(text || ""))});
    const me = normalize(${JSON.stringify(String(nickname || ""))});
    const rows = Array.from(document.querySelectorAll('[data-e2e="comment-item"]'));
    const hits = [];
    for (const row of rows) {
      const own = selector => Array.from(row.querySelectorAll(selector))
        .find(e => e.closest('[data-e2e="comment-item"]') === row);
      // 与 douyin_agent 的评论抽取保持同一档退让（含「取首行」兜底）：
      // 新版评论项可能既无 comment-username-1、也无 /user/ 链接、也无 name 类名，
      // 昵称只剩在 innerText 第一行（真机实测：漏了 author 全空，
      // 就永远判不出「这条是不是我发的」）。
      const nameEl = own('[data-e2e="comment-username-1"], a[href*="/user/"], span[class*="name"]');
      const firstLine = String(row.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
      const author = normalize((nameEl && nameEl.innerText) || firstLine)
        .replace(/\\s*(?:作者赞过|作者已赞|作者)$/, '');
      const info = own('.comment-item-info-wrap');
      const cEl = (info && info.nextElementSibling) || own(
        '[data-e2e="comment-level-1"], [class*="comment-content"], [class*="CommentContent"]');
      const content = normalize((cEl && cEl.innerText) || '');
      if (!content || !content.includes(want)) continue;
      hits.push({author, exact: content === want});
    }
    const mine = hits.filter(h => !!me && (h.author === me || h.author.startsWith(me)));
    return {total: hits.length, mine: mine.length, authors: hits.slice(0, 5).map(h => h.author)};
  })()`;
}

function prepareEditor(commentId, expected = null) {
  return `(() => {
    const id = ${JSON.stringify(String(commentId || ""))};
    const visible = e => {const r=e.getBoundingClientRect();return r.width>8 && r.height>8;};
    const editors = Array.from(document.querySelectorAll('.comment-input-inner-container [contenteditable="true"]'));
    if(editors.some(e=>(e.innerText||'').trim()) || Array.from(document.querySelectorAll('.comment-input-inner-container input[type="file"]')).some(e=>e.files?.length) || document.querySelector('.comment-input-inner-container [data-e2e="comment-image-preview"] img,.comment-input-inner-container [class*="upload-preview"] img,.comment-input-inner-container [data-e2e="comment-sticker-preview"]'))return {draft:true};
    let target;
    if (id) {
      const more = document.getElementById('tooltip_' + id);
      const row = more && more.closest('[data-e2e="comment-item"]');
      if (!row) {
        // 目标是楼中楼回复时，那条回复可能还折叠在「展开 N 条回复」里。
        // 先点一轮展开按钮再让外层重试（调用方 10×500ms 轮询），
        // 否则回复评论会一直停在 comment_entry_missing。
        const expanders = Array.from(document.querySelectorAll('button,[role="button"],span,div'))
          .filter(el => el.offsetParent && /展开\\s*(?:\\d+\\s*条)?\\s*(?:回复|评论)/.test(
            String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()))
          .slice(0, 40);
        expanders.forEach(el => { try { el.click(); } catch (_) {} });
        return {found:false, expanding:expanders.length};
      }
      const expected = ${JSON.stringify(expected)};
      if(expected) {
        const own=selector=>Array.from(row.querySelectorAll(selector)).find(e=>e.closest('[data-e2e="comment-item"]')===row);
        const name=own('[data-e2e="comment-username-1"],a[href*="/user/"]');
        const info=own('.comment-item-info-wrap');
        const content=info?.nextElementSibling || own('[data-e2e="comment-level-1"],[class*="comment-content"]');
        if(!name||!content || (name.innerText||'').trim()!==expected.commentAuthor ||
           (content.innerText||'').trim()!==expected.sourceText)return {found:false};
      }
      const candidates = Array.from(row.querySelectorAll('span,div,p'))
        .filter(e => e.closest('[data-e2e="comment-item"]') === row &&
          e.childElementCount === 0 && (e.innerText || '').trim() === '回复' && visible(e));
      if (candidates.length !== 1) return {found:false};
      target = candidates[0];
    } else {
      const nodes = Array.from(document.querySelectorAll('.comment-input-inner-container'));
      if (nodes.length !== 1) return {found:false};
      const inputs=editors.filter(visible);
      const placeholders=Array.from(nodes[0].querySelectorAll('[data-e2e="comment-placeholder"],[class*="comment-input-placeholder"]')).filter(visible);
      target=inputs.length===1?inputs[0]:placeholders.length===1?placeholders[0]:null;
      // 兜底：2026-11-18 真机实测评论输入区里既没有 contenteditable、也没有
      // comment-input-placeholder（container 内可见 class 只有 commentInput-right-ct
      // 和 comment-mainContent），输入框是「点一下才变成可编辑」的占位。
      // 这里退化为「容器内面积最大的非按钮元素」——点它就能激活编辑器。
      // 不会点击上传/发送：button / role=button / input / svg 全部排除。
      // 误点也不会假成功：后面 EDITOR_JS 会校验恰好一个可见 contenteditable。
      if(!target){
        const inner=Array.from(nodes[0].querySelectorAll('*')).filter(e=>{
          if(!visible(e))return false;
          const tag=e.tagName.toLowerCase();
          if(tag==='button'||tag==='input'||tag==='svg'||tag==='path')return false;
          if(e.getAttribute('role')==='button')return false;
          const r=e.getBoundingClientRect();
          return r.width>60&&r.height>10;
        });
        inner.sort((a,b)=>{
          const ra=a.getBoundingClientRect(),rb=b.getBoundingClientRect();
          return rb.width*rb.height-ra.width*ra.height;
        });
        target=inner[0]||nodes[0];
      }
      // The container center may be an upload or send button after adding media.
      if(!target)return {found:false};
    }
    target.scrollIntoView({block:'center'});
    const r=target.getBoundingClientRect();
    return {found:true,x:r.left+r.width/2,y:r.top+r.height/2};
  })()`;
}

const EDITOR_JS = `(() => {
  const nodes = Array.from(document.querySelectorAll('.comment-input-inner-container [contenteditable="true"]'))
    .filter(e => {const r=e.getBoundingClientRect();return r.width>50 && r.height>8;});
  if(nodes.length!==1)return null;
  const e=nodes[0],r=e.getBoundingClientRect();
  const label=document.getElementById(e.getAttribute('aria-describedby'));
  return {x:r.left+r.width/2,y:r.top+r.height/2,text:(e.innerText||'').split(String.fromCharCode(160)).join(' ').trim(),
    label:label?.textContent||'', focused:document.activeElement===e};
})()`;

const SUBMIT_JS = `(() => {
  const editor=document.activeElement;
  const container=editor?.closest('.comment-input-inner-container');
  if(!container || !editor.matches('[contenteditable="true"]'))return null;
  const nodes=container.querySelectorAll('.commentInput-right-ct > div > span:last-child');
  if(nodes.length!==1)return null;
  const el=nodes[0],r=el.getBoundingClientRect();
  if(r.width<8 || r.height<8 || r.top<0 || r.bottom>innerHeight)return null;
  const x=r.left+r.width/2,y=r.top+r.height/2,hit=document.elementFromPoint(x,y);
  return hit && (hit===el || el.contains(hit)) ? {x,y} : null;
})()`;

function stateProbe(action) {
  const e2e = action === "like-video" ? "video-player-digg" : "video-player-collect";
  const negative = action === "like-video" ? "video-player-no-digged" : "video-player-no-collect";
  const positives = action === "like-video" ? ["video-player-digged", "video-player-is-digged"]
    : ["video-player-collected", "video-player-is-collected"];
  return `(() => {
    const states=Array.from(document.querySelectorAll('[data-e2e="${e2e}"]'));
    const els=states
      .filter(e=>{const r=e.getBoundingClientRect();return r.width>8 && r.height>8;});
    let el=els.length===1 ? els[0] : null;
    // Landscape detail pages render their visible toolbar below the player.
    // The uniquely identified player control still supplies its actual state.
    if(!el && states.length===1) {
      const shares=Array.from(document.querySelectorAll('[data-e2e="video-share-icon-container"]'));
      if(shares.length!==1)return null;
      const bar=shares[0].parentElement, items=Array.from(bar.children);
      if(items.length!==4 || items[3]!==shares[0] ||
         items[0].querySelector('svg')?.getAttribute('viewBox')!=='0 0 90 94' ||
         items[2].querySelector('svg')?.getAttribute('viewBox')!=='0 0 100 100')return null;
      el=items[${action === "like-video" ? 0 : 2}];
      if(!el.querySelector('[data-popupid]'))return null;
    }
    if(!el || states.length!==1)return null;
    el.scrollIntoView({block:'center'});
    const state=states[0].getAttribute('data-e2e-state');
    const done=${JSON.stringify(positives)}.includes(state);
    if(state!==${JSON.stringify(negative)} && !done)return null;
    const r=el.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
    if(r.width<8 || r.height<8)return null;
    const hit=document.elementFromPoint(x,y);
    return {already:done,state,x,y,hit:hit===el||el.contains(hit)};
  })()`;
}

async function executeLive(command, io) {
  const scope = scopeFor(command);
  if (!scope) return { ok:false, status:"blocked", code:"live_writes_disabled" };
  let submitted = false;
  let stage = "draft_probe";
  const skipped = code => ({ok:false,status:"skipped",code});
  const active = () => scopeFor(command) && !(io.stopped && io.stopped());
  const identityMatches = async () => {
    const identity = await io.evaluate(IDENTITY_JS);
    const target = videoIdOf(command.videoUrl);
    // 画面里正在播的那条优先：点「下一个」进来时地址没变，只认地址会永远对不上。
    const urlVideo = videoIdOf(identity?.url);
    const here = String(identity?.videoId || '') || urlVideo;
    // 关注/私信/分享的目标是「人」，videoUrl 是主页地址：维持原来的口径（地址里没有视频就算对）。
    const videoOk = target ? here === target : urlVideo === "";
    return !!identity && videoOk &&
      !identity.loginWall && !identity.verify && identity.self.startsWith(scope.nickname + "关注");
  };
  try {
    if(!active())return skipped("scope_expired_or_stopped");
    const urlBefore = io.currentUrl ? io.currentUrl() : "existing-page";
    // A newly created Electron worker has no document ready for script execution.
    // Its empty/about:blank page cannot contain a draft; navigate it first.
    const existingDraft = urlBefore && urlBefore !== "about:blank"
      ? await io.evaluate("Array.from(document.querySelectorAll('[contenteditable=\"true\"],textarea')).some(e=>(e.value||e.innerText||'').trim()) || Array.from(document.querySelectorAll('input[type=file]')).some(e=>e.files?.length>0) || !!document.querySelector('[data-e2e=comment-sticker-preview]')")
      : false;
    if(existingDraft)return skipped("existing_draft_preserved");
    // 已经在目标视频页就别再导航：点「下一个」进来、刚读完评论区时页面本来就是它，
    // 重新导航只会把同一个视频整页重刷一遍（用户看到的就是「同一个视频反复加载」）。
    // 后面 identityMatches() 仍会核对当前账号与当前视频，跳过导航不放宽任何门槛。
    const onTarget = !!videoIdOf(command.videoUrl) &&
      String(await io.evaluate(CURRENT_VIDEO_JS).catch(() => "")) === videoIdOf(command.videoUrl);
    stage = "navigation";
    if(!onTarget) await io.navigate(command.videoUrl);
    stage = "identity_probe";
    let identity = false;
    for (let i=0;i<45;i++) {
      if (!active()) return skipped("scope_expired_or_stopped");
      identity = await identityMatches().catch(()=>false);
      if(identity)break;
      await io.sleep(1000);
    }
    if(!identity || !active())return skipped("page_or_account_not_verified");
    if(['post-comment','reply-comment','send-dm','like-video','follow-user','share-profile','share-comment'].includes(command.type)) {
      if(io.phase)io.phase('waiting_preview');
      if(!await ensureActable(io,scope))return skipped('live_preview_not_visible');
      if(!active() || !await identityMatches())return skipped('stopped_or_identity_changed');
      if(io.phase)io.phase('executing');
    }
    if(['follow-user','share-profile','share-comment'].includes(command.type))return await require('./live-social').executeSocial(command,io,{
      scope,active,claim:()=>claimAttempt(scope,command.type,command),
      // 执行器自己发现「按钮在但点不中」时可以再叫一次可交互核验：
      // 后台窗口被判定遮挡时 elementFromPoint 打不中，重新显示后同一段代码就能点中。
      reactable:()=>ensureActable(io,scope)
    });
    if(command.type==='send-dm')return await require('./live-dm').executeDm(command,io,{
      scope,active,claim:()=>claimAttempt(scope,command.type,command)
    });
    if(command.type==='like-comment')return await require('./comment-like').executeCommentLike(command,io,{
      scope,active,identityMatches,claim:()=>claimAttempt(scope,command.type,command)
    });
    if(command.type === "copy-video-link") {
      stage = "clipboard_preservation";
      const before=io.clipboard.read();
      if(!claimAttempt(scope,command.type,command))return skipped("acceptance_attempt_consumed");
      try {
        io.clipboard.write(command.videoUrl);
        if(io.clipboard.readText()!==command.videoUrl)return skipped("clipboard_mismatch");
        return {ok:true,status:"sent_ok",code:"link_copied_verified",url:command.videoUrl};
      } finally {io.clipboard.restore(before);}
    }
    if(command.type === "like-video" || command.type === "favorite-video") {
      const script=stateProbe(command.type),before=await io.evaluate(script);
      if(!before || !before.hit)return skipped("control_not_verified");
      if(before.already)return skipped("already_done");
      if(!active() || !await identityMatches())return skipped("stopped_or_identity_changed");
      if(!claimAttempt(scope,command.type,command))return skipped("acceptance_attempt_consumed");
      submitted=true;
      await io.click(before.x,before.y);
      for(let i=0;i<12;i++) {
        await io.sleep(700);
        const after=await io.evaluate(script);
        if(after?.already) {
          await io.sleep(900);
          const stable=await io.evaluate(script);
          if(stable?.already && await identityMatches())return {ok:true,status:"sent_ok",code:"state_transition_verified"};
        }
      }
      return {ok:false,status:"uncertain",code:"state_not_confirmed"};
    }
    if(!validateComment(command).ok)return skipped("invalid_comment");
    if(command.type === "reply-comment" && !/^\d{6,}$/.test(command.commentId))return skipped("invalid_reply_target");
    const reviewed=scope.reviewedActions?.find(a=>a.type===command.type&&a.videoUrl===command.videoUrl&&
      (a.commentId||'')===(command.commentId||'')&&a.text===command.text);
    if(command.type==='reply-comment' && reviewed) {
      if(!io.readComments)return skipped('reply_reader_unavailable');
      const page=await io.readComments();
      const matches=(page?.comments||[]).filter(c=>c.commentId===command.commentId &&
        c.nickname===reviewed.commentAuthor && c.text===reviewed.sourceText);
      if(matches.length!==1 || !active() || !await identityMatches())return skipped('reply_source_changed_or_missing');
    }
    let prepared;
    for(let i=0;i<10;i++) {
      if(!active())return skipped('stopped');
      prepared=await io.evaluate(prepareEditor(command.type === "reply-comment" ? command.commentId : "",reviewed));
      if(prepared?.draft||prepared?.found)break;
      await io.sleep(500);
    }
    if(prepared?.draft)return skipped("existing_draft_preserved");
    if(!prepared?.found){
      // 评论入口找不到时取证：是「页面类型不对（图文/直播）」还是「评论输入区选择器变名」。
      try{
        const why=await io.evaluate(`(() => {
          const eds=Array.from(document.querySelectorAll('[contenteditable="true"]')).map(e=>{
            const r=e.getBoundingClientRect();
            return {cls:String(e.className||'').slice(0,60),w:Math.round(r.width),h:Math.round(r.height),txt:String(e.innerText||'').replace(/\\s+/g,' ').slice(0,20)};});
          return {url:location.href.slice(0,110), ready:document.readyState,
            editors:eds.slice(0,6),
            commentContainers:document.querySelectorAll('.comment-input-inner-container').length,
            commentItems:document.querySelectorAll('[data-e2e="comment-item"]').length,
            texts:[...new Set(Array.from(document.querySelectorAll('[class*="comment"]')).map(e=>String(e.className||'').slice(0,60)))].slice(0,8)};
        })()`);
        diagTrace('comment-diag',JSON.stringify(why));
      }catch(e){diagTrace('comment-diag-failed',String((e&&e.message)||e).slice(0,160));}
      return skipped("comment_entry_missing");
    }
    await io.click(prepared.x,prepared.y);
    await io.sleep(500);
    let editor=await io.evaluate(EDITOR_JS);
    if(!editor || editor.text)return skipped("existing_draft_or_editor_missing");
    if(command.type === "reply-comment" && !editor.label.includes("回复"))return skipped("reply_context_missing");
    if(command.type === "post-comment" && editor.label.includes("回复"))return skipped("unexpected_reply_context");
    await io.click(editor.x,editor.y);
    let imageUri='';
    if(command.attachmentPath) {
      stage='image_upload';
      if(!io.uploadImage || !active())return skipped('image_upload_unavailable');
      imageUri=await require('./live-media').attachImage(command,io,active);
      if(!imageUri)return skipped('image_upload_not_verified');
      // Upload controls move focus. Restore the exact editor before typing.
      editor=await io.evaluate(EDITOR_JS);
      if(!editor||editor.text)return skipped('image_editor_changed');
      await io.click(editor.x,editor.y);
    }
    await io.evaluate(COMMENT_HOOK_JS);
    const seq=await io.evaluate("window.__commentReceiptSeq || 0");
    const extras=require('./live-comment-extras');
    if(command.stickerId||command.mentionTargets) {
      stage='native_comment_extras';
      if(!await extras.composeExtras(command,io,active))return skipped('native_extras_not_verified');
    }else await io.type(command.text);
    editor=await io.evaluate(EDITOR_JS);
    if(!editor || !editor.focused || editor.text!==command.text.trim())return skipped("draft_text_mismatch");
    const submit=await io.evaluate(SUBMIT_JS);
    if(!submit || !active() || !await identityMatches())return skipped("submit_not_verified");
    if(command.attachmentPath && !require('./live-media').verifyImage(command))return skipped('reviewed_image_changed');
    if((command.stickerId||command.mentionTargets)&&!extras.matchesDraft(await io.evaluate(extras.DRAFT_JS),command))return skipped('native_extras_not_verified');
    if(!claimAttempt(scope,command.type,command))return skipped("acceptance_attempt_consumed");
    // 提交前先给「页面上已有的同文案评论」拍个快照。
    // 没有这个基线，别人发过同样的模板评论就会被当成「我发出去了」——
    // 用户在 2026-11-19 明确点名过这个假成功隐患。
    const ownBefore = await io.evaluate(ownCommentProbe(command.text,scope.nickname)).catch(()=>null);
    submitted=true;
    await io.click(submit.x,submit.y);
    for(let i=0;i<16;i++) {
      await io.sleep(700);
      const receipts=await io.evaluate(`(window.__commentReceipts||[]).filter(e=>e.seq>${Number(seq)})`);
      const result=classifyCommentReceipts(receipts,{videoId:videoIdOf(command.videoUrl),text:command.text.trim(),commentId:command.commentId||"",imageUri,
        stickerId:command.stickerId||'',mentions:extras.targets(command)});
      // 回执带 cid 且视频/文案全对上，就是平台自己认了这次提交，不需要 DOM 再凑一份证据。
      // 旧写法要求「回执 + 页面可见」双证据，评论区虚拟列表一滚走就判 uncertain，
      // 反而把真成功记成待核验。
      if(result==="sent_ok") {
        const seen=await io.evaluate(ownCommentProbe(command.text,scope.nickname)).catch(()=>null);
        return {ok:true,status:"sent_ok",code:"receipt_verified",
          text:command.text.trim(),videoUrl:command.videoUrl,visible:!!seen&&seen.mine>0,...(imageUri?{imageUri}:{})};
      }
      if(result==="sent_fail") return {ok:false,status:"sent_fail",code:"comment_receipt_failed",
        text:command.text.trim(),videoUrl:command.videoUrl,...(imageUri?{imageUri}:{})};
    }
    // 回执钩子包装的是 window.fetch，只认 URL 里的 /comment/publish|/comment/post。
    // 平台换接口路径、改走 XHR，钩子就永远是空的 —— 真机上已经出现过
    // 「评论其实发出去了、回执却一条没有」，把成功判成 comment_receipt_missing。
    // 所以回执为空时再看一眼页面上到底有没有这段文字：有就是发出去了。
    const ownAfter = await io.evaluate(ownCommentProbe(command.text,scope.nickname)).catch(()=>null);
    const editorState = await io.evaluate(`(() => {
      const ed=document.querySelector('[contenteditable="true"],textarea');
      const btn=document.querySelector('[data-e2e="comment-publish"],[data-e2e="comment-submit"]');
      return {self:!!ed, text:ed?String(ed.value||ed.innerText||'').slice(0,40):'',
        btn:!!btn, disabled:!!(btn&&(btn.disabled||btn.getAttribute('aria-disabled')==='true')),
        guard:!!document.querySelector('#captcha_container,[data-e2e="verify-dialog"]')};
    })()`).catch(()=>null);
    // 判据：①同文案的可见条数比提交前多 ②多出来的那条署名是当前账号。
    // 只满足①不满足② = 页面上多出来的那条是别人发的同款模板，不能算我成功。
    const grew = !!ownBefore && !!ownAfter && ownAfter.total > ownBefore.total;
    const mineGrew = !!ownBefore && !!ownAfter && ownAfter.mine > ownBefore.mine;
    try{require('./diag-log').trace('reply-diag',
      JSON.stringify({receipts:0,before:ownBefore,after:ownAfter,editor:editorState}));}catch(_){}
    if(grew && ownAfter.mine > 0) return {ok:true,status:"sent_ok",code:"visible_own_comment_verified",
      text:command.text.trim(),videoUrl:command.videoUrl,visible:true,...(imageUri?{imageUri}:{})};
    if(grew) return {ok:false,status:"uncertain",code:"comment_visible_but_not_own",
      text:command.text.trim(),videoUrl:command.videoUrl,...(imageUri?{imageUri}:{})};
    return {ok:false,status:"uncertain",code:mineGrew?"comment_author_mismatch":"comment_receipt_missing",
      text:command.text.trim(),videoUrl:command.videoUrl,...(imageUri?{imageUri}:{})};
  } catch (_) {
    return {ok:false,status:submitted?"uncertain":"skipped",code:submitted?"submit_outcome_unknown":stage+"_failed"};
  }
}

module.exports={scopeFor,claimAttempt,ensureActable,executeLive,ownCommentProbe,IDENTITY_JS,prepareEditor,EDITOR_JS,SUBMIT_JS,stateProbe};
