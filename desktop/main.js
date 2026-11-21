/**
 * desktop-shell/main.js —— 南溪AI获客 桌面壳
 *
 * 职责（对标原版阿里山的 Electron 形态）：
 *  1. 启动时确保本机桥接服务（127.0.0.1:8899）在跑，不在则用南溪 venv 自动拉起；
 *  2. BrowserWindow 加载界面（/ui/），数据通道由页面自身 fetch 完成；
 *  3. 创作聚合 / 消息聚合：按页面请求用 **BrowserView** 原生嵌抖音页
 *     （creator.douyin.com 创作中心 / www.douyin.com），边界实时跟随 .creator-host/.chat-host；
 *  4. 登录/实况大屏与 douyin_agent 共用账号原生执行页，收起时移回后台宿主；
 *     视图状态通过 view-status 推回页面，驱动「加载中 → ready」。
 *
 * 页面操作沿用各业务执行器既有的账号、准确目标与次数授权；
 * 嵌入/收起页面本身不触发任务，不扩大真实写操作范围，不代登录或验证码。
 *  - 抖音页内新开的第三方页面转到系统浏览器打开。
 */
const {
  app, BrowserWindow, BrowserView, ipcMain, shell, dialog, session,
  Tray, Menu, nativeImage, Notification,
} = require("electron");
const path = require("path");
const { trace } = require("./diag-log");
const http = require("http");
const net = require("net");
const fs = require("fs");
const { spawn, execFileSync } = require("child_process");
const { classifyWindowUrl, blockNonWebNavigation, installProtocolGuard } = require("./external-url");
const { applyViewBounds, cssToViewBounds, setAutomationLayout } = require("./live-preview");
const { observeEmbeddedPage, initializeLoginPage } = require("./worker-surface");
// 系统设置（缩放 / 静音 / 系统代理 / 硬件加速 / 实况自动收起 / 链接打开方式）。
// 硬件加速必须在 app ready 之前决定，所以这里立刻按设置文件应用一次。
const systemSettings = require("./system-settings");
systemSettings.applyBoot(app);
// 开发环境从短生命周期终端启动时，父进程可能先关闭 stdout/stderr。
// EPIPE 若无监听会成为主进程异常并阻塞 Electron 的计时器与指令轮询。
for (const stream of [process.stdout, process.stderr]) {
  if (stream) stream.on("error", (error) => {
    if (error.code !== "EPIPE") throw error;
  });
}
// 账号 → session partition。与采集执行器共用同一份规则：
// 登录视图和采集窗口必须落在同一个分区，否则「界面里登录的号」和
// 「采集用的号」会变成两个不同的浏览器身份。
const { partitionOf, resolvePartition, setDefaultLoginHint } = require("./session-partition");

/**
 * 本机桥接必须绕过代理 —— 一行开关，但少了它采集就整条废掉。
 *
 * 壳如果继承了环境里的 HTTP_PROXY（很多开发机、企业网络、CI 沙箱都会设），
 * Chromium 会把 http://127.0.0.1:8899 也交给代理转发：页面加载直接
 * ERR_CONNECTION_RESET，net 请求全部超时，表现成「界面空白 / 采集点了没反应」。
 * `<-loopback>` 是 Chromium 的保留规则，含义是「把本机回环排除在代理之外」，
 * 不影响用户访问外网要不要走代理。
 */
app.commandLine.appendSwitch("proxy-bypass-list", "<-loopback>");

// Windows 的系统通知必须带 AppUserModelId，否则 Notification.isSupported() 为真
// 但弹不出来（静默失败）。必须在 app ready 之前设置。
if (process.platform === "win32") app.setAppUserModelId("com.nanxi.aileads");

/**
 * 单实例锁 —— 必须的，不是洁癖。
 *
 * 采集指令是「取走即出队」的：GET /desktop/command 拿到哪条，队列里就没哪条了。
 * 同时开两个壳时，后端的指令可能被**另一个实例**取走；那边页面卡住不回传，
 * 这边就一直等回执 —— 表现是「点了开始没反应，等了四分钟才超时」。
 * 排期任务同理，两个实例会各跑一遍，线索账本重复写。
 * 所以第二个实例直接退出，把第一个窗口唤到前台。
 */
if (!app.requestSingleInstanceLock()) {
  console.warn("[desktop-shell] 已有实例在运行，本实例退出（同一时刻只允许一个壳驱动采集）");
  app.exit(0);
}

// 后端与桌面壳同包分发（nanxi-ai-leads/backend）。按壳自身位置解析，整个目录
// 拷到任何路径都能跑；只有需要指向别处的后端实例时才用 BRIDGE_BACKEND_DIR 覆盖。
const BACKEND_DIR = process.env.BRIDGE_BACKEND_DIR || path.resolve(__dirname, "..", "backend");
const BRIDGE_BASE = "http://127.0.0.1:8899";
const UI_URL = BRIDGE_BASE + "/ui/";
process.env.LEADS_PROFILE_QR_ROOT=path.join(app.isPackaged?path.dirname(app.getPath('exe')):path.resolve(__dirname,'..'),'用户主页二维码');
const PYTHON = (() => {
  // venv 不能跨机直接复用（pyvenv.cfg 记录的是原解释器绝对路径）。随包附带 .venv
  // 时优先用它保证依赖版本一致；venv 缺失或被拷坏时退回系统 python，至少能起服务。
  const venvPy = path.join(BACKEND_DIR, ".venv", "Scripts", "python.exe");
  if (fs.existsSync(venvPy)) return venvPy;
  return process.platform === "win32" ? "python" : "python3";
})();

// Separate from developer acceptance scopes; only native confirmation fills
// this session's exact-comment scope. The backend and worker inherit the path.
const commentLikeDataDir = process.env.DATA_DIR || path.resolve(BACKEND_DIR, '..', 'data');
const commentLikeScopePath = path.join(commentLikeDataDir, 'acceptance',
  `desktop-comment-like-${process.pid}-${require('crypto').randomBytes(8).toString('hex')}.json`);
process.env.LEADS_COMMENT_LIKE_SCOPE = commentLikeScopePath;
const commentLikeDb = process.env.LEADS_DB_PATH || path.join(commentLikeDataDir, 'lead_system.db');
const leadgenScopePath = path.join(commentLikeDataDir, 'acceptance',
  `desktop-leadgen-${process.pid}-${require('crypto').randomBytes(8).toString('hex')}.json`);
process.env.LEADS_LEADGEN_SCOPE = leadgenScopePath;
process.env.LEADS_LEADGEN_SCOPE_DIR = leadgenScopePath + '.accounts';
const {createCommentLikeLauncher} = require('./comment-like-launcher');
function commentLikeHelper(args) {
  return new Promise((resolve, reject) => require('child_process').execFile(PYTHON,
    [path.join(BACKEND_DIR, 'tools', 'prepare_comment_like_scope.py'), '--db', commentLikeDb, '--check-history', ...args],
    {cwd:BACKEND_DIR,windowsHide:true,encoding:'utf8',timeout:15000}, (error, stdout) => {
      let value; try { value = JSON.parse(stdout); } catch (_) {}
      if (!error && value?.success) resolve(value);
      else reject(Error(value?.error && !/^[A-Za-z]/.test(value.error) ? value.error :
        '请先读取视频评论并明确选择一条真实主评，以及一个已登录的执行账号'));
    }));
}
function commentLikeIpc(channel, payload, timeout = 20000) {
  return new Promise((resolve, reject) => {
    const req = http.request(BRIDGE_BASE + '/ipc/' + channel,
      {method:'POST',headers:{'Content-Type':'application/json'}}, res => {
        let body='';res.on('data',data=>body+=data);res.on('end',()=>{
          try { const packet=JSON.parse(body);if(!packet.success)throw Error('本地服务响应失败');resolve(packet.data); }
          catch (_) { reject(Error('本地服务暂不可用')); }
        });
      });
    req.on('error',()=>reject(Error('本地服务暂不可用')));
    req.setTimeout(timeout,()=>{req.destroy();reject(Error('启动响应超时，请检查任务状态，不要重复提交'));});
    req.end(JSON.stringify({args:[payload]}));
  });
}
const launchCommentLike = createCommentLikeLauncher({
  runtime:()=>commentLikeIpc('get-comment-like-runtime',{}),
  preview:taskId=>commentLikeHelper(['--task-id',taskId,'--preview']),
  confirm:async selected => {
    const t=selected.target;
    const result=await dialog.showMessageBox(win,{type:'question',title:'确认真机评论点赞',
      message:'使用已登录账号，在抖音评论区真实点赞一次？',
      detail:`执行账号：${selected.nickname}\n视频：${t.videoUrl}\n评论作者：${t.authorName}\n评论：${t.content}\n\n已赞直接跳过。刷新后仍保留才计成功，结果不明确时停止且不重试。`,
      buttons:['取消','开始真机点赞'],defaultId:0,cancelId:0,noLink:true});
    return result.response===1;
  },
  prepare:async(taskId,binding)=>{
    const output=commentLikeScopePath+'.'+require('crypto').randomBytes(8).toString('hex')+'.approved';
    return (await commentLikeHelper(['--task-id',taskId,'--output',output,'--expected-binding',binding])).scopeFile;
  },
  activate:file=>{fs.copyFileSync(file,commentLikeScopePath+'.next');fs.renameSync(commentLikeScopePath+'.next',commentLikeScopePath);},
  start:async(taskId,selected)=>{
    const result=await commentLikeIpc('start-comment-like-live',{taskId,executionMode:'live'});
    if(result?.success)emitLiveDashboard('live-dashboard-focus',{
      taskId,runId:result.runId,accountId:selected.accountId});
    return result;
  },
});

const launchLeadgen = require('./leadgen-launcher').createLeadgenLauncher({
  cleanup:payload=>commentLikeIpc('cancel-leadgen-prepared',payload),
  runtime:()=>commentLikeIpc('get-runtime-status',{}),
  prepare:payload=>new Promise((resolve,reject)=>{
    const output=leadgenScopePath+'.'+require('crypto').randomBytes(8).toString('hex')+'.approved';
    require('child_process').execFile(PYTHON,[path.join(BACKEND_DIR,'tools','prepare_live_stream_scope.py'),
      '--db',commentLikeDb,'--payload-json',JSON.stringify(payload),'--output',output],
      {cwd:BACKEND_DIR,windowsHide:true,encoding:'utf8',timeout:15000},(error,stdout)=>{
        let value;try{value=JSON.parse(stdout);}catch(_){}
        if(!error&&value?.success)resolve(value);else reject(Error(value?.error||'任务配置校验失败'));
      });
  }),
  activate:async(file, taskPayload)=>{
    const selected=JSON.parse(fs.readFileSync(file,'utf8'));
    const result=await commentLikeIpc('activate-leadgen-scope',{scope:selected,taskPayload});
    if(!result?.success)throw Error(result?.error||'顺序任务授权失败，未启动');
  },
  start:async(payload,prepared)=>{
    const result=await commentLikeIpc('start-task',payload);
    if(!result?.success)await commentLikeIpc('cancel-leadgen-prepared',payload);
    if(result?.success)emitLiveDashboard('live-dashboard-focus',{
      taskId:payload.leadgenTaskId,runId:payload.taskId,accountId:payload.accountId});
    return result;
  }
});

function emitLiveDashboard(channel,payload){
  if(win&&!win.isDestroyed())win.webContents.send('view-status',{channel,payload});
}

require('./douyin_agent').setLiveDashboardHooks({
  phase:(cmd,phase)=>emitLiveDashboard('live-dashboard-phase',{
    runId:cmd.runId,taskId:cmd.leadgenTaskId,accountId:cmd.accountId,phase}),
  ready:async(cmd,stopped)=>{
    if(!cmd.runId)return false;
    const {waitForVisiblePreview}=require('./live-dashboard');
    return waitForVisiblePreview({now:Date.now,stopped,
      sleep:ms=>new Promise(resolve=>setTimeout(resolve,ms)),
      frame:async()=>{
        if(!win||win.isDestroyed()||!win.isVisible()||win.isMinimized())return null;
        const entry=resolveAutoView('entity_'+cmd.accountId).entry;
        if(!entry?.attached||!entry.surface)return null;
        const visible=await win.webContents.executeJavaScript(
          'document.visibilityState==="visible" && !!document.querySelector(".dashboard-view")').catch(()=>false);
        const agent=require('./douyin_agent');
        const observed=visible && await observeEmbeddedPage(entry,agent.liveState(cmd.accountId),win,()=>agent.liveState(cmd.accountId));
        return {visible:observed && entry.__displayedRunId===cmd.runId,capturedAt:entry.__displayedFrameAt||0};
      }});
  }
});

/* ---------------------------------------------------------------- 后端自举 */

// 端口是否有人监听（TCP 层，不看 HTTP 语义）。
// 为什么要两层：以前只看 HTTP /bridge-info 且超时 1.5s，后端在跑采集/写库时
// 稍一忙就判「掉线」→ 再 spawn 一个 uvicorn → 新进程抢走 8899，旧进程的
// accept 立刻失效（WinError 64），于是「掉线 → 拉起 → 再掉线」自我循环。
function portOpen(timeout = 800) {
  return new Promise((resolve) => {
    let done = false;
    const sock = net.connect({ host: "127.0.0.1", port: 8899 });
    const finish = (ok) => {
      if (done) return;
      done = true;
      try { sock.destroy(); } catch (e) { /* 已经关了 */ }
      resolve(ok);
    };
    sock.setTimeout(timeout);
    sock.once("connect", () => finish(true));
    sock.once("timeout", () => finish(false));
    sock.once("error", () => finish(false));
  });
}

async function bridgeUp() {
  if (await portOpen()) return true;   // 有人在监听就算活着，别去打扰它
  return new Promise((resolve) => {
    const req = http.get(BRIDGE_BASE + "/bridge-info", (res) => {
      resolve(res.statusCode === 200);
      res.resume();
    });
    req.on("error", () => resolve(false));
    req.setTimeout(4000, () => { req.destroy(); resolve(false); });
  });
}

// 正在拉起中的后端：看门狗每 10s 调一次，桥接启动期（uvicorn 起来要几秒）里
// bridgeUp() 会持续为 false。没有这把锁时，每次巡检都会再 spawn 一个 uvicorn，
// 几个进程同时抢 8899 → 互相把对方顶掉，表现是「后端反复掉线、日志里排期执行器
// 启动了好几次」。同一个进程内只允许有一次拉起在飞。
let backendSpawn = null;
// 自己拉起的后端进程（复用已有后端时为 null）。退出时只收自己拉起的那个，
// 不误杀用户手动 `python -m uvicorn` 起的实例。
let backendProc = null;

async function ensureBackend() {
  if (await bridgeUp()) return { started: false };
  if (backendSpawn) return backendSpawn;
  backendSpawn = (async () => {
    try {
      return await spawnBackend();
    } finally {
      backendSpawn = null;
    }
  })();
  return backendSpawn;
}

async function spawnBackend() {
  if (await bridgeUp()) return { started: false };
  try {
    // 后端的 stdout/stderr 必须落盘：以前是 stdio:"ignore"，后端启动失败时
    // 壳只显示「已自动拉起」然后整页数据空白，日志里一个字都没有。
    const logDir = path.join(BACKEND_DIR, "logs");
    try { fs.mkdirSync(logDir, { recursive: true }); } catch (e) { /* 只读目录就放弃日志 */ }
    const outFd = (() => {
      try { return fs.openSync(path.join(logDir, "uvicorn.out.log"), "a"); }
      catch (e) { return "ignore"; }
    })();
    const errFd = (() => {
      try { return fs.openSync(path.join(logDir, "uvicorn.err.log"), "a"); }
      catch (e) { return "ignore"; }
    })();
    backendProc = spawn(
      PYTHON,
      ["-m", "uvicorn", "bridge_server:app", "--host", "127.0.0.1", "--port", "8899"],
      {
        cwd: BACKEND_DIR,
        stdio: ["ignore", outFd, errFd],
        windowsHide: true,
        env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
      });
    // 不 unref：壳退出时要能顺手收掉自己拉起的后端。
    // 以前是 `.unref()`，壳关了后端还活着 —— 排期照样到点触发，但没有执行器，
    // 于是每条都静默 skipped，看起来像「跑了一整晚什么都没干」。
    backendProc.on("exit", (code) => {
      console.info("[desktop-shell] 后端退出 code=" + code);
      backendProc = null;
    });
  } catch (e) {
    return { started: false, error: String(e) };
  }
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    if (await bridgeUp()) return { started: true };
  }
  return { started: false, error: "后端 30s 内未就绪（详见 backend/logs/uvicorn.err.log）" };
}

/* ------------------------------------------------------------ 视图（BrowserView） */

/**
 * 收掉自己拉起的后端。
 *
 * 为什么必须做：后端子进程以前是 unref 的孤儿。壳关掉后它继续跑排期线程，
 * 但没有采集执行器 → 每条排期都「到点了、没做事」。用户看到执行历史里全是
 * skipped，只会以为系统坏了。所以壳退出 = 后端一起退。
 * 复用外部已存在的后端时 backendProc 为 null，不动它（那是用户自己起的）。
 */
function stopBackend() {
  const proc = backendProc;
  backendProc = null;
  if (!proc || proc.exitCode !== null) return false;
  try {
    if (process.platform === "win32") {
      // python 启动后可能再派生解释器子进程，只 kill 父进程会留残，
      // /T 连整棵子进程树一起收。
      execFileSync("taskkill", ["/pid", String(proc.pid), "/T", "/F"],
        { windowsHide: true, stdio: "ignore", timeout: 3000 });
    } else {
      proc.kill("SIGTERM");
    }
    return true;
  } catch (e) {
    try { proc.kill(); } catch (e2) { /* 已退出 */ }
    return false;
  }
}

const KIND_HOST = { creator: ".creator-host", chat: ".chat-host" };
const KIND_URL = {
  creator: "https://creator.douyin.com/creator-micro/home",
  // 抖音官方网页版消息页（isPopup=1 为无站点导航的嵌入形态，与消息聚合区域匹配）
  chat: "https://www.douyin.com/chat?isPopup=1",
};
const KIND_EVENT = { creator: "creator-view-status", chat: "chat-view-status" };

let win = null;
/** kind → Map(viewKey → { view, visible }) */
const views = { creator: new Map(), chat: new Map() };
const activeKey = { creator: null, chat: null };
let boundsTimer = null;
/** 后端看门狗定时器（app.whenReady 中启动） */
let watchdogTimer = null;

/** 账号登录等自动化视图：viewKey → { view, attached, bounds }（坐标由页面主动推送） */
const autoViews = new Map();

/* ------------------------------------ 实况视图 key 归一 + 自愈（大屏黑屏根因） */
/**
 * 页面有两套 viewKey，含义不同、拼法也不同：
 *
 *   - **账号级**：登录弹窗 / `init-automation-view` 用 `douyin_<账号id>`；
 *   - **任务级**：实况大屏按任务只推坐标，用 `entity_<账号id>` /
 *     `monitor_<账号id>` / `publish_<账号id>`。
 *
 * 以前只做精确匹配：大屏推 `entity_123` → `autoViews.get("entity_123")` 取不到
 * （表里只有 `douyin_123`）→ 直接 return「view not inited」→ 原生视图从没挂上去。
 * 页面里那块 `.window-content / .popup-body` 是写死的 `#000` 占位底色，
 * 于是用户看到的就是「实况大屏永远一片黑」（真机实锤，2026-11-14）。
 *
 * 同一个账号在同一个 session partition 里只该有一份抖音页，所以各前缀都归到
 * 账号 id 上：先精确命中，再按账号后缀找，最后按需补建。
 */
const AUTO_VIEW_KEY_RE = /^([a-z]+)_(.+)$/;
const AUTO_VIEW_PREFIXES = ["entity", "monitor", "publish", "douyin", "dy", "creator", "chat"];

function viewKeyAccount(key) {
  const m = AUTO_VIEW_KEY_RE.exec(String(key || ""));
  if (!m || !AUTO_VIEW_PREFIXES.includes(m[1])) return "";
  return m[2];
}

function resolveAutoView(key) {
  const k = String(key || "");
  for(const [savedKey,entry] of autoViews){
    if(entry.view.webContents.isDestroyed())autoViews.delete(savedKey);
  }
  const hit = autoViews.get(k);
  if (hit) return { key: k, entry: hit };
  const acc = viewKeyAccount(k);
  if (!acc) return { key: k, entry: null };
  for (const [avKey, entry] of autoViews) {
    if (viewKeyAccount(avKey) === acc) return { key: avKey, entry };
  }
  return { key: k, entry: null };
}

/** 建一个账号级抖音视图（登录弹窗与实况大屏共用同一条路径）。 */
function createAutoView(viewKey, accountId) {
  const surface = require('./douyin_agent').getWorkerSurface(accountId);
  const view = surface.view;
  const entry = { view, surface, attached: false, bounds: null, accountId: String(accountId || "") };
  autoViews.set(viewKey, entry);
  return entry;
}

/** 自愈：大屏只推坐标、`init-automation-view` 因故没发（或壳重启过）时补建视图。 */
function ensureAutoViewFor(viewKey) {
  const acc = viewKeyAccount(String(viewKey || ""));
  if (!acc) return null;
  const key = `douyin_${acc}`;
  const existing = autoViews.get(key);
  if (existing) return existing;
  const entry = createAutoView(key, acc);
  diagLive("自愈补建视图", viewKey, "→", key, "url=", entry.view.webContents.getURL());
  return entry;
}

/* ---------------------------- 原生执行页的可见性确认，不再采样/镜像画面 */
let liveFollowTimer = null;

async function syncLiveFollow() {
  if (!win || win.isDestroyed()) return;
  let agent = null;
  try { agent = require("./douyin_agent"); } catch (e) { return; }
  if (typeof agent.liveState !== "function") return;
  for (const [key, entry] of autoViews) {
    if (!entry.attached || !entry.accountId) continue;
    if (entry.view.webContents.isDestroyed()) continue;
    let st = null;
    try { st = agent.liveState(entry.accountId); } catch (e) { continue; }
    await observeEmbeddedPage(entry, st, win, () => agent.liveState(entry.accountId));
  }
}

function startLiveFollow() {
  if (liveFollowTimer) return;
  liveFollowTimer = setInterval(syncLiveFollow, 400);
}

/* ------------------------------------------------ 实况视图诊断日志（可取证） */
const DIAG_DIR = path.join(__dirname, "_verify");

function diagLive(...parts) {
  const line = new Date().toISOString() + "  " + parts.map((p) => String(p)).join(" ");
  try {
    fs.mkdirSync(DIAG_DIR, { recursive: true });
    const file = path.join(DIAG_DIR, "live-view.log");
    let size = 0;
    try { size = fs.statSync(file).size; } catch (e) { /* 还没有文件 */ }
    if (size > 512 * 1024) fs.writeFileSync(file, ""); // 只留最近一段，别无限长
    fs.appendFileSync(file, line + "\n");
  } catch (e) { /* 诊断失败不影响主流程 */ }
}

function protectRemoteView(webContents) {
  webContents.setWindowOpenHandler(({ url }) => {
    const action = classifyWindowUrl(url);
    if (action === "allow") return { action: "allow" };
    if (action === "external") setImmediate(() => {
      shell.openExternal(url).catch((error) => {
        console.warn("[desktop-shell] 无法打开外部网页:", error.message);
      });
    });
    return { action: "deny" };
  });
  webContents.on("will-navigate", blockNonWebNavigation);
  webContents.on("will-redirect", blockNonWebNavigation);
  installProtocolGuard(webContents);
}

function attachAuto(entry) {
  if (win && !win.isDestroyed() && !entry.attached) {
    // 没坐标就先给 1×1：BrowserView 默认铺满整个窗口，会盖住页面变「黑屏 + 点不动」。
    if (!entry.bounds) entry.view.setBounds({ x: 0, y: 0, width: 1, height: 1 });
    entry.surface.attachTo(win);
    entry.__appliedBounds = null; // parking reset the origin; apply the host rectangle again
    entry.attached = true;
  }
  applyViewBounds(entry, win && !win.isDestroyed() ? win.webContents.getZoomFactor() : 1);
}

function detachAuto(entry) {
  if (win && !win.isDestroyed() && entry.attached) {
    entry.surface.park();
  }
  entry.attached = false;
}

function sendStatus(kind, payload) {
  if (win && !win.isDestroyed()) {
    win.webContents.send("view-status", { channel: KIND_EVENT[kind], payload });
  }
}

function parseArgs(argsJson) {
  try {
    const args = JSON.parse(argsJson || "[]");
    const first = Array.isArray(args) ? args[0] : args;
    if (first && typeof first === "object") return first;
    if (typeof first === "string" && first) return { accountId: first };
  } catch (e) { /* 落到默认 */ }
  return {};
}

function viewKeyOf(kind, payload, useActive = false) {
  return String(payload.viewKey || (payload.accountId ? "douyin_" + payload.accountId :
    (useActive && activeKey[kind]) || kind + "_default"));
}

function hostRect(kind) {
  return win.webContents
    .executeJavaScript(
      `(() => { const el = document.querySelector(${JSON.stringify(KIND_HOST[kind])});` +
      ` if (!el) return null; const r = el.getBoundingClientRect();` +
      ` if (r.width < 4 || r.height < 4) return null;` +
      ` return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }; })()`,
      true
    )
    .catch(() => null);
}

async function syncBounds() {
  if (!win || win.isDestroyed()) return;
  const hostZoom = win.webContents.getZoomFactor();
  for (const entry of autoViews.values()) applyViewBounds(entry, hostZoom);
  for (const kind of Object.keys(views)) {
    const key = activeKey[kind];
    if (!key) continue;
    const entry = views[kind].get(key);
    if (!entry || !entry.visible) continue;
    const rect = await hostRect(kind);
    // A modal/account switch may hide this view while hostRect is awaiting IPC.
    if (!win || win.isDestroyed() || activeKey[kind] !== key || !entry.visible) continue;
    if (!rect) { // 宿主不在页面上（路由切走了）→ 收起视图
      win.removeBrowserView(entry.view);
      entry.attached = false;
      continue;
    }
    if (!entry.attached) {
      win.addBrowserView(entry.view);
      entry.attached = true;
    }
    entry.view.setBounds(cssToViewBounds(rect, hostZoom));
  }
}

function showView(kind, viewKey) {
  for (const [k, entry] of views[kind]) {
    if (k === viewKey) continue;
    if (entry.attached && win && !win.isDestroyed()) win.removeBrowserView(entry.view);
    entry.attached = false;
    entry.visible = false;
  }
  activeKey[kind] = viewKey;
  const entry = views[kind].get(viewKey);
  if (entry) {
    entry.visible = true;
    if (kind === "creator" && entry.profileSync) entry.profileSync();
  }
  syncBounds();
}

function ensureView(kind, viewKey) {
  let entry = views[kind].get(viewKey);
  if (entry) return entry;
  // 多账号隔离：chat/creator 视图必须用账号自己的 session partition。
  // 以前不带 partition → 所有账号共用默认 session 的登录态，
  // 消息聚合切换账号永远显示默认分区里登录的那个账号的私信（真机实锤 2026-11-17）。
  // 分区解析与采集窗口/登录视图同一套（resolvePartition），保证身份一致。
  const accountId = viewKeyAccount(viewKey);
  const partition = accountId ? resolvePartition(accountId) : null;
  const webPreferences = {
    contextIsolation: true,
    nodeIntegration: false,
    sandbox: true,
    spellcheck: false,
  };
  if (partition) webPreferences.partition = partition;
  const view = new BrowserView({ webPreferences });
  protectRemoteView(view.webContents);
  view.webContents.loadURL(KIND_URL[kind]).catch(() => {});
  entry = { view, visible: false, attached: false, accountId: String(accountId || "") };
  views[kind].set(viewKey, entry);
  if (kind === "creator") attachProfileSync(entry, viewKey);
  return entry;
}

/* ---------------------------------------------------- 创作者中心资料采集（只读） */

/** 从创作者中心首页 header 读取本人账号资料：昵称 / 抖音号 / 关注 / 粉丝指数 / 获赞。
 *  与阿里山原版一致：登录态存在时每次打开创作者中心刷新一次；读不到就跳过。 */
const PROFILE_SCRAPE_JS = `(() => {
  try {
    let host = null;
    for (const el of document.querySelectorAll("body *")) {
      const t = el.innerText || "";
      if (t.includes("抖音号") && (t.includes("粉丝指数") || t.includes("获赞"))) {
        if (!host || t.length < host.innerText.length) host = el;
      }
    }
    if (!host) return null;
    const text = host.innerText || "";
    const douyinId = (text.match(/抖音号[：:]\\s*([A-Za-z0-9_.-]+)/) || [])[1] || "";
    const following = (text.match(/关注\\s*([\\d.]+[万亿]?)/) || [])[1] || "";
    const followers = (text.match(/粉丝指数\\s*([\\d.]+[万亿]?)/) || [])[1] || "";
    const likes = (text.match(/获赞\\s*([\\d.]+[万亿]?)/) || [])[1] || "";
    let nickname = "";
    for (const line of text.split("\\n")) {
      const s = line.trim();
      if (!s || s.includes("抖音号") || /^(关注|粉丝|获赞|要表|私信|回复)/.test(s) ||
          /^[\\d.]+\\s*$/.test(s)) continue;
      nickname = s; break;
    }
    nickname = (nickname || "").replace(/[▾▼▽▸>]+$/, "").trim();
    return { nickname, douyinId, following, followers, likes };
  } catch (e) { return null; }
})()`;

/** viewKey（douyin_<accountId>）去重防抖 + 回传后端 */
const profileSyncedAt = new Map();

function attachProfileSync(entry, viewKey) {
  const accountId = String(viewKey).replace(/^douyin_/, "");
  const run = async (delayMs) => {
    await new Promise((r) => setTimeout(r, delayMs));
    try {
      const wc = entry.view.webContents;
      if (wc.isDestroyed() || !/creator\.douyin\.com/.test(wc.getURL())) return;
      const profile = await wc.executeJavaScript(PROFILE_SCRAPE_JS, true);
      if (!profile || !profile.nickname) return;
      const last = profileSyncedAt.get(viewKey) || 0;
      const sig = JSON.stringify(profile);
      if (sig === profileSyncedAt.get("sig:" + viewKey) && Date.now() - last < 3600e3) return;
      profileSyncedAt.set(viewKey, Date.now());
      profileSyncedAt.set("sig:" + viewKey, sig);
      const req = http.request(BRIDGE_BASE + "/api/accounts/profile-sync", {
        method: "POST", headers: { "Content-Type": "application/json" },
      }, () => {});
      req.on("error", () => {});
      req.end(JSON.stringify({ accountId, ...profile }));
      console.info("[desktop-shell] 账号资料已同步:", accountId, profile.nickname,
        "粉丝指数", profile.followers);
    } catch (e) { /* 页面尚未就绪等情况：静默跳过 */ }
  };
  entry.view.webContents.on("did-finish-load", () => run(2500));
  entry.view.webContents.on("did-navigate", () => run(2500));
  entry.profileSync = () => run(1500); // 视图重新显示时也可触发（SPA 已加载的情况）
}

function startBoundsLoop() {
  if (boundsTimer) return;
  boundsTimer = setInterval(syncBounds, 400);
}

/* ------------------------------------------------ 系统设置：实况画面自动收起 */
/**
 * 「后台自动关闭实况画面（30 秒）」：窗口失焦 30 秒后把已挂载的实况视图摘下，
 * 回到窗口时再挂回去。实况是原生图层，摘掉后不再解码/合成，省电也省显存；
 * 采集本身在独立窗口里跑，不受影响。
 */
let previewIdleTimer = null;
function installBackgroundPreviewGuard() {
  if (!win || win.isDestroyed()) return;
  win.on("blur", () => {
    if (previewIdleTimer) clearTimeout(previewIdleTimer);
    previewIdleTimer = setTimeout(() => {
      previewIdleTimer = null;
      if (!systemSettings.get().backgroundPreviewAutoClose) return;
      if (!win || win.isDestroyed() || win.isFocused()) return;
      let closed = 0;
      for (const entry of autoViews.values()) {
        if (!entry.attached) continue;
        try {
          detachAuto(entry);
          entry.__autoClosed = true;
          closed += 1;
        } catch (e) { /* 单个视图失败不影响其它 */ }
      }
      if (closed) console.info("[system-settings] 应用在后台超过 30 秒，已收起实况画面:", closed);
    }, 30000);
  });
  win.on("focus", () => {
    if (previewIdleTimer) { clearTimeout(previewIdleTimer); previewIdleTimer = null; }
    for (const entry of autoViews.values()) {
      if (!entry.__autoClosed) continue;
      entry.__autoClosed = false;
      try { attachAuto(entry); } catch (e) { /* 页面已销毁 */ }
    }
  });
}

/** 系统设置运行期生效：缩放 / 静音 / 默认会话代理，按设置文件变化自动应用。 */
function installSystemSettingsWatcher() {
  return systemSettings.startWatcher({
    windows: () => [win, ...sourceWindows].filter((w) => w && !w.isDestroyed()),
    views: () => [
      ...[...views.creator.values()].map((entry) => entry.view),
      ...[...views.chat.values()].map((entry) => entry.view),
      ...[...autoViews.values()].map((entry) => entry.view),
    ],
    // 缩放变化后原生视图要按新 zoom 重新对齐（applyViewBounds 用主窗口 zoom 换算）
    onApplied: () => { try { syncBounds(); } catch (e) { /* 窗口未就绪 */ } },
  });
}

function handleViewChannel(channel, argsJson) {
  if (channel === "get-live-dashboard-frames") {
    return {frames:Object.fromEntries([...autoViews.values()]
      .filter(entry=>entry.__displayedRunId && entry.__displayedFrameAt && !entry.view.webContents.isDestroyed())
      .map(entry=>[entry.__displayedRunId,entry.__displayedFrameAt]))};
  }
  if (channel === "get-view-layout") {
    return { hostZoom: win.webContents.getZoomFactor(), contentSize: win.getContentSize(),
      views: [...autoViews.values()].filter(entry => entry.attached).map(entry => ({
        cssBounds: entry.bounds, nativeBounds: entry.view.getBounds(),
        preview: false, contentMode:'native-worker', presentation:entry.surface.getPresentation(),webContentsId:entry.view.webContents.id,
        zoom: entry.view.webContents.getZoomFactor(),
      })) };
  }
  if (channel === "open-url" || channel === "lead-comment-interact") {
    return openLeadSource(channel, argsJson);
  }
  if (channel === "open-offline-test") return openOfflineTest();
  const m = /^(open|reload|destroy)-(creator|chat)-view$/.exec(channel);
  const attachM = /^set-(creator|chat)-view-attached$/.exec(channel);
  if (m) {
    const [, action, kind] = m;
    const payload = parseArgs(argsJson);
    const viewKey = viewKeyOf(kind, payload, action !== "open");
    const entry = action === "open" ? ensureView(kind, viewKey) : views[kind].get(viewKey);
    if (!entry) return { success: action === "destroy", mode: "browser-view", viewKey,
      code: kind === "chat" ? "CHAT_VIEW_NOT_OPEN" : "CREATOR_VIEW_NOT_OPEN",
      message: kind === "chat" ? "消息窗口尚未打开" : "创作窗口尚未打开" };
    if (action === "open") {
      showView(kind, viewKey);
      sendStatus(kind, { viewKey, status: "ready", mode: "browser-view" });
      return { success: true, mode: "browser-view", viewKey, message: "BrowserView 已挂载，抖音页面由桌面壳原生嵌入，系统不做代操作" };
    }
    if (action === "reload") {
      entry.view.webContents.reload();
      sendStatus(kind, { viewKey, status: "ready", mode: "browser-view" });
      return { success: true, mode: "browser-view", viewKey };
    }
    // destroy
    if (entry.attached && win && !win.isDestroyed()) win.removeBrowserView(entry.view);
    entry.attached = false;
    entry.visible = false;
    if (activeKey[kind] === viewKey) activeKey[kind] = null;
    sendStatus(kind, { viewKey, status: "destroyed", mode: "browser-view" });
    return { success: true, mode: "browser-view", viewKey };
  }
  if (attachM) {
    const [, kind] = attachM;
    const payload = parseArgs(argsJson);
    const viewKey = viewKeyOf(kind, payload, true);
    const entry = views[kind].get(viewKey);
    const attached = payload.attached !== false && payload.attached !== "false";
    if (entry) {
      entry.visible = attached;
      if (attached) { showView(kind, viewKey); } else if (entry.attached && win && !win.isDestroyed()) {
        win.removeBrowserView(entry.view);
        entry.attached = false;
        // Temporary modal hiding retains the selected account for restoration.
      }
    }
    return { success: true, mode: "browser-view", viewKey, attached };
  }
  /* 账号登录弹窗：页面先 init-automation-view 建 BrowserView（登录页=www.douyin.com），
     再以约 100ms 频率 update-automation-bounds 把 .login-view-container 坐标推过来。 */
  if (channel === "init-automation-view") {
    const payload = parseArgs(argsJson);
    const accountId = String(payload.accountId || "").trim();
    const viewKey = String(payload.viewKey || `${payload.platform || "douyin"}_${accountId || "default"}`);
    // 已按账号建过就复用（可能是大屏先自愈建过，或另一条任务同一个号）
    const resolved = resolveAutoView(viewKey);
    let entry = resolved.entry;
    if (!entry) {
      entry = createAutoView(viewKey, accountId);
      diagLive("init 新建视图", viewKey, "account=", accountId || "default",
        "url=", payload.url || "https://www.douyin.com");
    }
    initializeLoginPage(entry.surface, require('./douyin_agent').liveState(accountId), payload.url).catch(()=>{});
    return { success: true, mode: "browser-view", viewKey, key: resolved.key };
  }
  if (channel === "update-automation-bounds") {
    const payload = parseArgs(argsJson);
    const viewKey = String(payload.viewKey || "");
    // 大屏发的是任务级 key（entity_/monitor_/publish_），视图是账号级的 → 归一后再找。
    // 未登记时取该账号现有执行页面，禁止为大屏另开平台页。
    const resolved = resolveAutoView(viewKey);
    const entry = resolved.entry || ensureAutoViewFor(viewKey);
    if (!entry) return { success: true, mode: "browser-view", viewKey, note: "view not inited" };
    const b = payload.bounds || {};
    if (!b.width || !b.height || b.width <= 2 || b.height <= 2) {
      detachAuto(entry); // 弹窗关闭时页面会推 1×1
      return { success: true, mode: "browser-view", viewKey, hidden: true };
    }
    // Keep the original CSS rectangle so zoom changes never compound rounding.
    if (!['x', 'y', 'width', 'height'].every(key => Number.isFinite(b[key]))) {
      return { success: false, message: "无效的视频窗口坐标" };
    }
    setAutomationLayout(entry, payload);
    attachAuto(entry);
    if (!entry.__logged) { // 每个视图只记一次，避免 400ms 频率刷日志
      entry.__logged = true;
      diagLive("视图已挂载", viewKey, "→", resolved.key, JSON.stringify(entry.bounds),
        "url=", entry.view.webContents.getURL().slice(0, 80), "partition=", resolvePartition(entry.accountId) || "默认");
    }
    return { success: true, mode: "browser-view", viewKey, key: resolved.key, attached: true };
  }
  if (channel === "focus-automation-view") {
    // 大屏把某个窗口提到最前：BrowserView 是原生图层，页面里的层级由壳决定。
    const payload = parseArgs(argsJson);
    const viewKey = String(payload.viewKey || "");
    const entry = resolveAutoView(viewKey).entry || ensureAutoViewFor(viewKey);
    if (!entry) return { success: true, mode: "browser-view", viewKey, note: "view not inited" };
    try {
      if (payload.bringToFront !== false) {
        entry.surface.bringToFront();
      }
    } catch (e) { /* 旧版 Electron 没有该 API：忽略 */ }
    return { success: true, mode: "browser-view", viewKey };
  }
  if (channel === "set-account-detail-mode") {
    return { success: true, mode: "browser-view" };
  }
  if (channel === "set-views-visible") {
    let rawArgs = [];
    try { rawArgs = JSON.parse(argsJson || "[]"); } catch (e) { /* 保持空 */ }
    if (!Array.isArray(rawArgs)) rawArgs = [rawArgs];
    const visible = rawArgs[0] !== false;
    const keys = Array.isArray(rawArgs[1]) ? rawArgs[1].map(String) : null;
    const layouts = rawArgs[2]?.boundsByViewKey;
    if (visible && keys && layouts) {
      for (const key of keys) {
        if (!Object.prototype.hasOwnProperty.call(layouts,key)) continue;
        const entry=resolveAutoView(key).entry || ensureAutoViewFor(key);
        if(entry)setAutomationLayout(entry, layouts[key]);
      }
    }
    for (const [k, entry] of autoViews) {
      const want = visible && (!keys || keys.some(key => key === k || resolveAutoView(key).entry === entry));
      if (want) attachAuto(entry); else detachAuto(entry);
    }
    if (!keys) {
      for (const kind of Object.keys(views)) {
        for (const [k, entry] of views[kind]) entry.visible = visible && k === activeKey[kind];
      }
      syncBounds();
    }
    return { success: true, mode: "browser-view" };
  }
  return { success: true, mode: "browser-view", message: "通道已在桌面壳本地处理" };
}

const sourceWindows = new Set();
async function openLeadSource(channel, argsJson) {
  let payload;
  try { const args=JSON.parse(argsJson || '[]');payload=Array.isArray(args)?args[0]:args; }
  catch (_) { return {ok:false,success:false,reason:"来源参数无效"}; }
  if(typeof payload==='string')payload={url:payload};
  const url=String(payload?.url || '');
  if(classifyWindowUrl(url)!=='allow')return {ok:false,success:false,reason:"来源必须是抖音公开网页"};
  // 设置页「手动点击链接的打开方式」：browser=电脑默认浏览器，account=软件账号窗口。
  // 只有手动打开来源（open-url）受此设置影响；线索评论定位必须留在壳内窗口。
  if(channel==='open-url' && systemSettings.get().manualLinkOpenMode==='browser'){
    try{ await shell.openExternal(url); return {ok:true,success:true,via:'system-browser'}; }
    catch(e){ return {ok:false,success:false,reason:"无法调用系统浏览器："+((e&&e.message)||e)}; }
  }
  const partition=resolvePartition(String(payload?.accountId || ''));
  const source=new BrowserWindow({width:1280,height:900,show:true,title:"线索来源 · 阿里山AI获客",
    webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,spellcheck:false,
      ...(partition?{partition}:{})}});
  sourceWindows.add(source);source.on('closed',()=>sourceWindows.delete(source));
  protectRemoteView(source.webContents);
  const loaded=source.loadURL(url).catch(()=>false);
  if(channel==='open-url')return {ok:true,success:true};
  await Promise.race([loaded,new Promise(r=>setTimeout(r,15000))]);
  const target=payload?.locateComment || {};
  if(!target.nickname || !target.content)return {ok:false,reason:"缺少原评论信息，来源窗口已打开"};
  const script=`(() => {
    const wanted=${JSON.stringify({nickname:target.nickname,content:target.content})};
    const rows=Array.from(document.querySelectorAll('[data-e2e="comment-item"]'));
    const matches=rows.filter(row=>{
      const info=Array.from(row.querySelectorAll('.comment-item-info-wrap'))
        .find(e=>e.closest('[data-e2e="comment-item"]')===row);
      const name=info?.querySelector('a[href*="/user/"]')?.innerText?.trim();
      return name===wanted.nickname && info?.nextElementSibling?.innerText?.trim()===wanted.content.trim();
    });
    if(matches.length!==1)return false;
    matches[0].scrollIntoView({block:'center'});matches[0].style.outline='2px solid #4f8cff';return true;
  })()`;
  for(let i=0;i<15 && !source.isDestroyed();i++) {
    const found=await source.webContents.executeJavaScript(script).catch(()=>false);
    if(found)return {ok:true,success:true,replyClicked:false};
    await new Promise(r=>setTimeout(r,500));
  }
  return {ok:false,success:false,reason:"来源已打开，未找到唯一匹配的原评论，请在窗口内核对"};
}

/* ------------------------------------------------------------------- 托盘 */
/**
 * 常驻形态：关窗 ≠ 退出。
 *
 * 采集是「后台轮询 + 隐藏采集窗」在跑，跟主窗口显不显示没关系。所以关窗只隐藏，
 * 托盘留着，排期和采集继续；只有托盘菜单「退出」才真正停（并顺手收掉后端）。
 * 不做这一步的话，`window-all-closed → app.quit()` 会让用户以为「只是关了窗口」，
 * 实际整条自动化已经停了。
 */
let tray = null;
let isQuitting = false;
let trayBalloonShown = false;

function showMainWindow(pane) {
  if (!win || win.isDestroyed()) {
    createWindow(pane);
    return;
  }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  if (pane) {
    // 执行历史是注入式浮层，用 query 触发它自己打开（不改 bundle 路由）
    try { win.loadURL(UI_URL + "?pane=" + encodeURIComponent(pane)); } catch (e) {}
  }
}

function quitApp() {
  isQuitting = true;
  app.quit();
}

/**
 * 桌面通知。优先系统通知（Win10/11 会在通知中心留痕，用户错过也能回看），
 * 失败再退回托盘气泡。托盘图标不闪、不弹窗——常驻的打扰必须最小。
 */
function notifyUser(title, content) {
  if (Notification.isSupported()) {
    try {
      new Notification({ title: title, body: content, silent: false }).show();
      return true;
    } catch (e) { /* 落到气泡 */ }
  }
  try {
    tray.displayBalloon({ title: title, content: content, iconType: "warning" });
    return true;
  } catch (e) {
    return false;
  }
}

function createTray() {
  if (tray) return tray;
  let img = nativeImage.createFromPath(path.join(__dirname, "assets", "icon.png"));
  if (img.isEmpty()) img = nativeImage.createEmpty();
  try {
    tray = new Tray(img);
  } catch (e) {
    console.warn("[desktop-shell] 托盘创建失败:", (e && e.message) || e);
    return null;
  }
  tray.setToolTip("阿里山AI获客 · 后台常驻中（采集与排期继续运行）");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "打开主界面", click: () => showMainWindow() },
    { label: "执行历史", click: () => showMainWindow("history") },
    { type: "separator" },
    {
      label: "显示采集窗口（抖音要求人工验证时用）",
      click: () => {
        try { require("./douyin_agent").showWorker(); } catch (e) { /* 执行器未加载 */ }
      },
    },
    { type: "separator" },
    { label: "退出（停止采集与排期）", click: () => quitApp() },
  ]));
  tray.on("click", () => showMainWindow());
  tray.on("double-click", () => showMainWindow());
  console.info("[desktop-shell] 托盘图标已就绪（关窗后仍常驻）");
  return tray;
}

/* ------------------------------------------------------------------- 窗口 */

let offlineWindow = null;
async function openOfflineTest() {
  if (offlineWindow && !offlineWindow.isDestroyed()) {
    offlineWindow.show(); offlineWindow.focus();
    return { success: true, mode: "offline" };
  }
  const testWindow = new BrowserWindow({
    width: 1180, height: 850, minWidth: 650, minHeight: 600, show: false,
    parent: win || undefined, title: "离线测试 · 阿里山AI获客", backgroundColor: "#0b101b",
    webPreferences: { partition: "offline-testing", contextIsolation: true, sandbox: true, nodeIntegration: false },
  });
  offlineWindow = testWindow;
  testWindow.setMenu(null);
  testWindow.on("closed", () => { if (offlineWindow === testWindow) offlineWindow = null; });
  testWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  testWindow.webContents.on("will-navigate", (event, url) => {
    if (url !== BRIDGE_BASE + "/ui/offline.html") event.preventDefault();
  });
  try {
    await testWindow.loadURL(BRIDGE_BASE + "/ui/offline.html");
    testWindow.show();
    return { success: true, mode: "offline" };
  } catch (_) {
    testWindow.destroy();
    return { success: false, error: "离线面板加载失败，请检查本地服务" };
  }
}

async function createWindow(pane) {
  win = new BrowserWindow({
    width: 1480,
    height: 940,
    minWidth: 1120,
    minHeight: 700,
    show: false,
    backgroundColor: "#0b0e14",
    title: "南溪AI获客（本地桌面版）",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false, // preload 需要 require('electron') 以挂桥
    },
  });

  // 主窗 UI 的报错必须留痕：页面白屏时只看 loadURL 成功与否会误判「正常」，
  // renderer 里抛的异常只有这里能看到（worker 窗口已有「页面控制台」转发）。
  win.webContents.on("console-message", (_e, _level, message, line, sourceId) => {
    trace("主窗控制台:", String(message).slice(0, 400),
          "@", String(sourceId || "").split("/").pop() + ":" + line);
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    trace("主窗渲染进程退出:", details.reason, details.exitCode);
  });

  await win.loadURL(pane ? UI_URL + "?pane=" + encodeURIComponent(pane) : UI_URL);
  win.show();

  /* 关窗只隐藏：常驻采集与排期不因为「看不见界面」而停。
     只有托盘「退出」/ before-quit 才放行真正的关闭。 */
  win.on("close", (e) => {
    if (isQuitting) return;
    e.preventDefault();
    win.hide();
    if (!trayBalloonShown) {
      trayBalloonShown = true;
      notifyUser("阿里山AI获客 仍在后台运行",
        "采集与定时排期会继续执行。要完全退出，请右键托盘图标选择「退出」。");
    }
  });
  startBoundsLoop();
  startLiveFollow();
  // Login and dashboard now reuse the execution surface; no spare platform tab.

  /* 验证模式：--capture 截两张图（界面 / BrowserView 抖音页）后退出 */  if (process.argv.includes("--capture")) {
    const outDir = path.join(__dirname, "_verify");
    fs.mkdirSync(outDir, { recursive: true });
    const shot = async (wc, name) => {
      try {
        const img = await wc.capturePage();
        fs.writeFileSync(path.join(outDir, name), img.toPNG());
        console.info("[desktop-shell] 截图完成:", name);
      } catch (e) {
        console.warn("[desktop-shell] 截图失败:", name, e);
      }
    };
    await new Promise((r) => setTimeout(r, 6000));
    await shot(win.webContents, "1-ui.png");
    // 进入创作聚合页，让页面自己触发 open-creator-view → 主进程建 BrowserView
    const clicked = await win.webContents.executeJavaScript(
      `(() => { const all = [...document.querySelectorAll('*')].filter(e => e.textContent.trim() === '创作聚合' && e.children.length <= 1);` +
      ` const el = all[all.length - 1]; if (!el) return 'not-found';` +
      ` (el.closest('a,[role=button],li,.menu-item,div') || el).click(); return 'clicked'; })()`,
      true
    ).catch((e) => "err:" + e);
    console.info("[desktop-shell] 点击创作聚合:", clicked);
    await new Promise((r) => setTimeout(r, 5000));
    const key = activeKey.creator;
    console.info("[desktop-shell] creator 视图:", key, [...views.creator.keys()]);
    if (key && views.creator.get(key)) {
      await new Promise((r) => setTimeout(r, 9000));
      await shot(views.creator.get(key).view.webContents, "2-browserview-creator.png");
    } else {
      console.warn("[desktop-shell] 创作视图未创建（页面可能未触发 open-creator-view）");
    }
    app.quit();
    return;
  }

  win.on("closed", () => { win = null; });
}

ipcMain.handle("desktop:view-invoke", (_event, channel, argsJson) => {
  if(channel==='open-profile-qr-directory') {
    if(!win||_event.sender!==win.webContents||!String(_event.senderFrame?.url||'').startsWith(UI_URL))
      return {success:false,error:'请从桌面设置打开二维码目录'};
    const directory=require('./profile-qr').rootDirectory();
    fs.mkdirSync(directory,{recursive:true});
    return shell.openPath(directory).then(error=>{if(error)throw Error('二维码目录无法打开');return {success:true};});
  }
  if(channel === 'start-leadgen-real') {
    if(!win || _event.sender !== win.webContents ||
       !String(_event.senderFrame?.url || '').startsWith(UI_URL))
      return {success:false,error:'请从桌面客户端的评论获客页面启动'};
    return launchLeadgen(parseArgs(argsJson));
  }
  if(channel === 'start-comment-like-real') {
    if(!win || _event.sender !== win.webContents ||
       !String(_event.senderFrame?.url || '').startsWith(UI_URL))
      return {success:false,error:'请从桌面客户端的评论点赞页面启动'};
    return launchCommentLike(parseArgs(argsJson).taskId);
  }
  return handleViewChannel(String(channel || ""), argsJson);
});

app.whenReady().then(async () => {
  // 第二实例被锁挡住后，把已在运行的那个窗口唤到前台
  app.on("second-instance", () => showMainWindow());
  // 探测默认 session 是否有抖音登录 cookie，喂给分区绑定：
  // 有 → 存量账号继续绑默认分区（老登录态不丢）；没有 → 新账号各开独立分区。
  // cookies.get 是异步的，指令到达前通常已完成；未完成时按「有登录态」处理更安全。
  try {
    const all = await session.defaultSession.cookies.get({});
    setDefaultLoginHint(all.some((c) => /douyin\.com$/i.test(c.domain || "")));
  } catch (e) { /* 探测失败保持初始 hint=true，对存量更安全 */ }
  const boot = await ensureBackend();
  console.info("[desktop-shell] 桥接服务：", boot.started ? "已自动拉起" : boot.error ? "复用失败：" + boot.error : "已在运行");
  if (!boot.started && boot.error) {
    dialog.showErrorBox("南溪AI获客", "本地数据服务启动失败：\n" + boot.error + "\n\n可手动运行：python bridge.py start");
  }
  // 后端看门狗：运行期每 10s 探测一次桥接，掉线即自动补拉。
  // 壳此后端只保证「启动时在跑」；若后端进程中途意外退出（被误杀/异常崩溃），
  // 壳内整页数据源会静默失联，用户只能重启壳。看门狗把自愈收敛到 10s 内。
  let watchdogBusy = false;
  watchdogTimer = setInterval(async () => {
    if (watchdogBusy) return;
    watchdogBusy = true;
    try {
      if (!(await bridgeUp())) {
        console.warn("[desktop-shell] 看门狗：桥接掉线，自动补拉后端…");
        const reborn = await ensureBackend();
        console.info("[desktop-shell] 看门狗补拉：", reborn.started ? "成功" : reborn.error ? "失败：" + reborn.error : "无需（已在运行）");
      }
    } finally {
      watchdogBusy = false;
    }
  }, 10_000);
  // 允许手动停止看门狗（调试后端时避免反复拉起）：desktop 后端通道里置 backendWatchdog=off
  ipcMain.handle("desktop:watchdog", (_e, on) => {
    if (on === "off" && watchdogTimer) { clearInterval(watchdogTimer); watchdogTimer = null; }
    if (on === "on" && !watchdogTimer) { /* 简化：重开需重启壳 */ }
    return { watchdog: watchdogTimer ? "on" : "off" };
  });
  // 托盘先建：窗口一显示用户就可能点 X，那时必须已经有地方「接住」它。
  createTray();
  try {
    // 采集窗口全程不弹。需要人工过验证时，执行器不再把窗口怼到用户脸上，
    // 而是走这里发一条托盘气泡；用户看到再自己决定要不要打开采集窗口。
    require("./douyin_agent").onNotice((msg) => {
      console.warn("[desktop-shell] 执行器通知:", msg.title, msg.content);
      notifyUser(msg.title || "阿里山AI获客", msg.content || "");
    });
  } catch (e) { /* 执行器未加载 */ }
  // 真机执行器必须在建窗**之前**拉起：
  // 以前它排在 await createWindow() 之后，建窗一旦抛错（BrowserView / 加载失败 /
  // 上一次异常退出留下的锁），整个 whenReady 回调就中断，执行器永远不启动——
  // 表现是「应用开着、后端却收不到任何 /desktop/command 轮询」，采集点了没反应。
  // 执行器只依赖后端 HTTP，不依赖窗口，先起它没有任何副作用。
  try {
    require("./douyin_agent").start();
  } catch (e) {
    console.warn("[desktop-shell] 执行器启动失败:", (e && e.message) || e);
  }
  try {
    await createWindow();
  } catch (e) {
    console.error("[desktop-shell] 建窗失败:", (e && e.stack) || e);
    dialog.showErrorBox("阿里山AI获客", "界面加载失败：\n" + ((e && e.message) || e) +
      "\n\n后端与采集执行器仍在运行，可关闭后重新打开应用。");
  }
  // 系统设置：实况自动收起 + 缩放/静音/代理的运行期生效
  try {
    installBackgroundPreviewGuard();
    installSystemSettingsWatcher();
  } catch (e) {
    console.warn("[desktop-shell] 系统设置应用失败:", (e && e.message) || e);
  }

  // 验收用开关：SHELL_AUTO_QUIT_MS=N 时，N 毫秒后走一次**正常退出**路径。
  // 存在的意义：托盘点「退出」没法在自动化里点，但「退出必须收掉后端」这件事
  // 必须被真机验证过，否则孤儿后端会让排期空转一整夜。平时不设该变量，零影响。
  const autoQuitMs = parseInt(process.env.SHELL_AUTO_QUIT_MS || "0", 10);
  if (autoQuitMs > 0) {
    setTimeout(() => {
      console.info("[desktop-shell] 验收：自动触发正常退出");
      quitApp();
    }, autoQuitMs);
  }
});

app.on("window-all-closed", () => {
  // 常驻模式：关窗只是隐藏，进程要继续活着跑采集与排期。
  // 只有拿不到托盘（图标资源坏了等极端情况）才退回「关窗即退出」，
  // 否则会留下一个看不见、又杀不掉的僵尸进程。
  if (!tray) app.quit();
});

app.on("before-quit", () => {
  isQuitting = true;
  try {
    require("./douyin_agent").stop();
  } catch (e) { /* 忽略 */ }
  // 后端跟着一起收：留孤儿后端会让排期在「没有执行器」的状态下空转到天亮。
  const killed = stopBackend();
  console.info("[desktop-shell] 退出：" + (killed ? "已停止后端" : "后端由外部启动，保留"));
  if (tray) {
    try { tray.destroy(); } catch (e) { /* 忽略 */ }
    tray = null;
  }
});
