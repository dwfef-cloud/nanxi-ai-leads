const assert = require("node:assert/strict");
const test = require("node:test");
const { render } = require("./check_warmup_js");

async function runWarmup(deadline, {grid=false, frozen=false, stopAfterMs=0, scrolls=1}={}) {
  const code = render("WARMUP_JS")
    .replace("__SCROLLS__", String(scrolls))
    .replace("__DWELL__", "400")
    .replace("__LIKE_MAX__", "0")
    .replace("__LIKE_PROB__", "100")
    .replace("__DEADLINE__", String(deadline));
  let currentId = "7400000000000000001";
  const started=Date.now();
  const player={paused:false,readyState:4,get currentTime(){return frozen?0:(Date.now()-started)/1000;}};
  const active={getAttribute:()=>currentId,querySelector:selector=>selector==='video'?player:{innerText:'test'}};
  let switches=0;
  const next={click(){switches++;currentId="7400000000000000002";}};
  const document = {
    body: { innerText: "" },
    querySelector(selector) {
      if (selector.includes('feed-active-video')) return grid ? null : active;
      if (selector.includes('video-switch-next-arrow')) return next;
      if (selector.startsWith("[data-e2e=\"video-desc\"]")) return { innerText: "test" };
      return null;
    },
    querySelectorAll() { return []; },
  };
  const window = { innerHeight: 800, scrollBy() {} };
  const location = { href: "https://www.douyin.com/?recommend=1" };
  const timer=stopAfterMs ? setTimeout(()=>{window.__warmupStop=true;},stopAfterMs) : null;
  try {
    const result=await new Function("document", "window", "location", `return ${code}`)(document, window, location);
    return {...result,switches};
  } finally {if(timer)clearTimeout(timer);}
}

test("offline warmup does not count an unverified in-feed like", async () => {
  const result = await runWarmup(Date.now() + 10000);
  assert.equal(result.liked, 0);
  assert.equal(result.actions.length, 1);
  assert.equal(result.actions[0].like, "need-detail");
});

test("offline warmup starts no new screen after deadline", async () => {
  const result = await runWarmup(Date.now() - 1);
  assert.equal(result.liked, 0);
  assert.deepEqual(result.actions, []);
});

test("grid autoplay and a frozen player never count as watched feed videos",async()=>{
  const grid=await runWarmup(Date.now()+30000,{grid:true});
  assert.equal(grid.actions.length,0);assert.match(grid.error,/推荐流/);
  // 单屏静帧只跳过、不算浏览、不整轮失败：后台窗口偶发自动播放被拒很常见，
  // 一屏没确认就中断整轮会让 2 分钟的养号在第 4 屏就死掉（真机连续撞上两次）。
  const frozen=await runWarmup(Date.now()+10000,{frozen:true});
  assert.equal(frozen.actions.length,0);
  assert.equal(frozen.liked,0);
  assert.equal(frozen.error,undefined);
});

test("three frozen screens in a row stop the round instead of looping forever",async()=>{
  const r=await runWarmup(Date.now()+60000,{frozen:true,scrolls:3});
  assert.equal(r.actions.length,0);
  assert.equal(r.liked,0);
  assert.match(String(r.error||""),/连续 3 屏/);
});

test('stop during dwell or opening starts no further video and records no false watch', async()=>{
  for(const grid of [false,true]) {
    const began=Date.now();
    const result=await runWarmup(Date.now()+30000,{grid,stopAfterMs:40});
    assert.equal(result.stopped,true);
    assert.equal(result.actions.length,0);
    assert.equal(result.switches,0);
    assert.ok(Date.now()-began<1000,'停止不应等到停留或加载时长用完');
  }
});

test("comment extractor stays syntactically valid after template evaluation",()=>{
  // 2026-11-16 真机：COMMENT_EXTRACT_JS 里 publishedText 被 const 声明两次，
  // 整段注入脚本 SyntaxError，表现是「评论一条读不到 / Script failed to execute」，
  // 而当时没有任何测试会去求值这个模板。这里补上：模板求值后必须是合法 JS。
  const code=render("COMMENT_EXTRACT_JS")
    .replace(/__COMMENT_WINDOW_MINUTES__/g,"0")
    .replace(/__EXPAND_SUB_COMMENTS__/g,"false");
  assert.doesNotThrow(()=>new Function("return "+code));
});

test("comment browsing restores the standalone video page position",()=>{
  const code=render("COMMENT_EXTRACT_JS")
    .replace(/__COMMENT_WINDOW_MINUTES__/g,"0")
    .replace(/__EXPAND_SUB_COMMENTS__/g,"false")
    .replace(/__COMMENT_SCROLL_ROUNDS__/g,"6")
    .replace(/__COMMENT_LIMIT__/g,"40")
    .replace(/__COMMENT_EXTRACT_CAP__/g,"40");
  assert.match(code,/const videoTop = scroller \? scroller\.scrollTop : 0;/);
  assert.match(code,/const restoreTop = wideLayout \? videoTop : commentTop;/);
  assert.match(code,/scroller\.scrollTop = restoreTop/);
});
