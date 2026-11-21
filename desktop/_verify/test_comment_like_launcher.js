const test=require('node:test'),assert=require('node:assert/strict');
const {createCommentLikeLauncher}=require('../comment-like-launcher');
function fixture(overrides={}){
 const calls=[];
 const io={runtime:async()=>({taskIds:[]}),preview:async id=>({taskId:id,attempts:1,binding:'reviewed',target:{}}),
 confirm:async()=>true,prepare:async(id,binding)=>{calls.push(['prepare',id,binding]);return 'exact-approved-file';},
 activate:async file=>calls.push(['activate',file]),start:async id=>{calls.push(['start',id]);return {success:true,mode:'live'};},...overrides};
 return {run:createCommentLikeLauncher(io),calls};
}
test('native confirmation launches only the reviewed task through real path',async()=>{
 const {run,calls}=fixture();assert.deepEqual(await run('comment_like_one'),{success:true,mode:'live'});
 assert.deepEqual(calls,[['prepare','comment_like_one','reviewed'],['activate','exact-approved-file'],['start','comment_like_one']]);
});
test('cancel does not create scope or start a platform task',async()=>{
 const {run,calls}=fixture({confirm:async()=>false});assert.equal((await run('comment_like_one')).cancelled,true);assert.deepEqual(calls,[]);
});
test('changed selection or blocked history never activates or starts',async()=>{
 for(const overrides of [{prepare:async()=>{throw Error('selection changed');}},
  {preview:async()=>{throw Error('uncertain history');}},
  {runtime:async()=>({taskIds:['active']})}]){
  const {run,calls}=fixture(overrides);assert.equal((await run('comment_like_one')).success,false);assert.deepEqual(calls,[]);
 }
});
test('second click cannot replace the scope while native review is open',async()=>{
 let release;const reviewed=new Promise(r=>release=r);
 const {run,calls}=fixture({confirm:()=>reviewed});const first=run('comment_like_one');
 assert.equal((await run('comment_like_two')).success,false);release(false);await first;assert.deepEqual(calls,[]);
});
test('invalid task or substituted preview cannot start',async()=>{
 const {run,calls}=fixture({preview:async()=>({taskId:'other',attempts:1,binding:'x'})});
 assert.equal((await run('../scope')).success,false);assert.equal((await run('comment_like_one')).success,false);assert.deepEqual(calls,[]);
});
test('default task projection never promotes offline completions or counters',async()=>{
 const fs=require('node:fs'),path=require('node:path');
 const code=fs.readFileSync(path.resolve(__dirname,'../../ui/assets/CommentLikeMode.js'),'utf8');
 const {projectCommentLikeTask:project}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
 const record={id:'one',status:'completed',executionMode:'offline',simulationStats:{commentLikeSuccess:7},stats:{}};
 assert.equal(project(record).status,'pending');assert.equal(project(record).executionMode,'live');
 assert.deepEqual(project(record).stats,{});
 const modes={offline:{status:'completed',stats:{commentLikeSuccess:7}},live:{status:'failed',startedAt:100,stats:{commentLikeSuccess:0,commentLikeUncertain:1}}};
 assert.equal(project(record,modes).stats.commentLikeUncertain,1);assert.equal(project(record,modes).status,'failed');
 assert.equal(project(record,modes,true).stats.commentLikeSuccess,7);
});

test('worker checks native comment scope while other actions retain their own scope',t=>{
 const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
 const {scopeFor}=require('../live-platform');const dir=fs.mkdtempSync(path.join(os.tmpdir(),'native-like-'));
 const file=path.join(dir,'scope.json'),old=process.env.LEADS_COMMENT_LIKE_SCOPE;
 process.env.LEADS_COMMENT_LIKE_SCOPE=file;
 t.after(()=>{if(old===undefined)delete process.env.LEADS_COMMENT_LIKE_SCOPE;else process.env.LEADS_COMMENT_LIKE_SCOPE=old;});
 const video='https://www.douyin.com/video/7400000000000000001';
 const scope={secret:'x'.repeat(40),scopeId:'native',accountId:'a',leadgenTaskId:'t',nickname:'测试账号',
 expiresAt:Date.now()/1000+60,videoUrls:[video],actions:{'like-comment':1},
 commentTargets:[{videoUrl:video,commentId:'123456789',authorName:'作者',content:'需求'}]};
 const c={type:'like-comment',accountId:'a',leadgenTaskId:'t',videoUrl:video,commentId:'123456789',text:'需求',
 scopeId:'native',deadlineAt:Date.now()+60000,commentAuthor:'作者'};
 c.acceptanceSignature=crypto.createHmac('sha256',scope.secret).update(JSON.stringify(
 ['type','accountId','leadgenTaskId','videoUrl','commentId','text','scopeId','deadlineAt','commentAuthor'].map(k=>String(c[k]||'')))).digest('hex');
 assert.equal(scopeFor(c),null);fs.writeFileSync(file,JSON.stringify(scope));assert.ok(scopeFor(c));
 assert.equal(scopeFor({...c,type:'post-comment'}),null);
});
