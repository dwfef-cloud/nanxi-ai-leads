const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {setAutomationLayout,applyViewBounds}=require('../live-preview');
const layoutModule=()=>import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(path.resolve(__dirname,'../../ui/assets/LiveDashboardLayout.js'),'utf8')).toString('base64'));

test('single-account layout fills space and sidebar resizing restores the same page dimensions',async()=>{
  const {dashboardGrid,dashboardPageZoom}=await layoutModule();
  const full={width:1188,height:489},narrow={width:830,height:489};
  assert.deepEqual(dashboardGrid(full,1),[{x:6,y:6,width:1176,height:477}]);
  assert.deepEqual(dashboardGrid(narrow,1),[{x:6,y:6,width:818,height:477}]);
  assert.equal(dashboardPageZoom(dashboardGrid(full,1)[0]),477/800);
  assert.deepEqual(dashboardGrid(full,1),[{x:6,y:6,width:1176,height:477}]);
});

test('multi-account grids fit without overlaps at each column boundary and with sidebar open',async()=>{
  const {dashboardGrid}=await layoutModule();
  for(const width of [1188,830])for(const count of [2,4,5,9,10,16,30]){
    const cells=dashboardGrid({x:264,y:374,width,height:489},count);
    assert.equal(cells.length,count);
    assert.equal(new Set(cells.map(c=>c.x)).size,count<=4?2:count<=9?3:4);
    for(const [i,cell] of cells.entries()){
      assert.ok(cell.x>=264&&cell.y>=374&&cell.x+cell.width<=264+width&&cell.y+cell.height<=863);
      for(const other of cells.slice(i+1))assert.ok(cell.x+cell.width<=other.x||other.x+other.width<=cell.x||
        cell.y+cell.height<=other.y||other.y+other.height<=cell.y);
    }
  }
});

test('every task key uses a full native page and separate bounds and zoom per account',()=>{
  const entries=[];
  for(const [index,key] of ['douyin_a','entity_b','monitor_c','publish_d'].entries()){
    let mode='video',native,zoom;
    const entry={attached:true,surface:{setPresentation:value=>{mode=value;},setPageZoom:value=>{zoom=value;}},
      view:{setBounds:value=>{native=value;},webContents:{getURL:()=> 'https://fixture.invalid/video'}}};
    const bounds={x:264+index*200,y:374,width:194,height:230};
    assert.equal(setAutomationLayout(entry,{viewKey:key,bounds}),true);applyViewBounds(entry,1.25);
    assert.equal(mode,'page');assert.equal(zoom,.25);assert.equal(native.x,Math.round(bounds.x*1.25));
    entries.push(entry);
  }
  const saved={...entries[1].bounds};setAutomationLayout(entries[0],{bounds:{x:0,y:0,width:1176,height:477}});
  assert.deepEqual(entries[1].bounds,saved);
  assert.equal(setAutomationLayout(entries[0],{bounds:{x:NaN,y:0,width:20,height:30}}),false);
  assert.equal(setAutomationLayout(entries[0],{bounds:{x:0,y:0,width:1,height:1}}),false);
});
