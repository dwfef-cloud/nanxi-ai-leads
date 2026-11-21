const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {waitForVisiblePreview}=require('../live-dashboard');

test('first-frame gate rejects stale, hidden, stopped and disconnected previews',async()=>{
  for(const scenario of ['stale','hidden','stopped','ready']){
    let time=1000;
    const result=await waitForVisiblePreview({now:()=>time,stopped:()=>scenario==='stopped',
      sleep:async ms=>{time+=ms;},frame:async()=>({visible:scenario!=='hidden',capturedAt:scenario==='stale'?999:time})},{timeoutMs:500});
    assert.equal(result,scenario==='ready',scenario);
  }
});

test('dashboard projection isolates real runs, keeps short finishes and respects new account owners',async()=>{
  const code=fs.readFileSync(path.resolve(__dirname,'../../ui/assets/LiveDashboardModel.js'),'utf8');
  const {projectLiveCards,liveStatusText}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
  const current={mode:'live',taskKind:'comment_like',taskId:'t',runId:'new',accountId:'a',startedAt:200,
    status:'failed',isSessionFinished:true,stats:{commentLikeSuccess:0,commentLikeUncertain:1}};
  const items=[{...current,runId:'old',startedAt:100},{...current,mode:'offline',runId:'offline',startedAt:300},current];
  const cards=projectLiveCards(items);assert.equal(cards.length,1);assert.equal(cards[0].runId,'new');
  assert.equal(cards[0].displayStats.commentLikeStats.success,0);assert.equal(liveStatusText(cards[0]),'结果待核验');
  assert.equal(projectLiveCards(items,new Set(['a'])).length,0);
  assert.equal(projectLiveCards([current],new Set(),new Set(['new'])).length,0);
  assert.equal(liveStatusText({...current,status:'completed',stats:{commentLikeSkipped:1}}),'未执行');
});
