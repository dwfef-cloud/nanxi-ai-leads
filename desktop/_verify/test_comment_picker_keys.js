const assert=require('node:assert/strict');
const {test}=require('node:test');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.resolve(__dirname,'../../ui/assets/index-1pPBwPwk.js'),'utf8');
const functions=source.match(/function Zx\(e\)\{.*?(?=const lh=)/)?.[0];
assert.ok(functions,'Bundled sample selection normalizer exists');
const normalize=vm.runInNewContext(functions+';V0',{J3:['main','reply','profile'].map(value=>({value}))});
const values=(keys,kinds=['main','reply','profile'])=>Array.from(normalize({a:keys},kinds).a);
test('current and legacy main comment keys survive selection and reload',()=>{
 const current='main:'+ 'a'.repeat(64);
 assert.deepEqual(values(['local:12',current,'local:12']),['local:12',current]);
});
test('deselecting main kind clears legacy main keys and preserves other kinds',()=>{
 const reply='reply:'+ 'b'.repeat(64);
 assert.deepEqual(values(['local:12',reply],['reply']),[reply]);
});
test('arbitrary malformed and unknown sample keys remain rejected',()=>{
 assert.deepEqual(values(['local:0','local:-1','local:abc','local:12junk','local:01','local:'+'1'.repeat(20),12,null,'main:12','unknown:'+'a'.repeat(64)]),[]);
});
test('per account limit and independent account selection remain intact',()=>{
 const keys=Array.from({length:102},(_,i)=>'local:'+(i+1));
 assert.equal(values(keys).length,100);
 const selected=normalize({a:['local:12'],b:['local:13']},['main']);
 assert.deepEqual(Array.from(selected.a),['local:12']);
 assert.deepEqual(Array.from(selected.b),['local:13']);
});
