const test = require('node:test');
const assert = require('node:assert/strict');
const {createCommandControl} = require('../command-control');

test('stop interrupts an in-progress dwell instead of waiting for the serial queue', async () => {
  let cancel = false, interrupted = 0;
  const control = createCommandControl({
    intervalMs:5, check:async()=>({cancelled:cancel}), interrupt:async()=>{interrupted++;},
  });
  try {
    const pending = control.wait(new Promise(()=>{}));
    cancel = true;
    await assert.rejects(pending, {code:'COMMAND_STOPPED'});
    assert.equal(interrupted,1);
    assert.throws(()=>control.check(), {code:'COMMAND_STOPPED'});
  } finally {control.close();}
});

test('transient heartbeat failure never treats a command as resumed or cancelled', async () => {
  let calls=0;
  const control = createCommandControl({intervalMs:5,
    check:async()=>{calls++;throw Error('offline');},
    interrupt:async()=>assert.fail('transport failure is not a stop'),
  });
  try {
    await control.wait(new Promise(resolve=>setTimeout(resolve,25)));
    assert.ok(calls>0);
    assert.equal(control.stopped,false);
  } finally {control.close();}
});

test('a late stop heartbeat after disposal cannot interrupt a newer command', async()=>{
  let reply, began;
  const entered=new Promise(resolve=>{began=resolve;});
  const control=createCommandControl({intervalMs:5,
    check:()=>{began();return new Promise(resolve=>{reply=resolve;});},
    interrupt:async()=>assert.fail('disposed command interrupted another command'),
  });
  await entered;
  control.close();
  reply({cancelled:true});
  await new Promise(resolve=>setTimeout(resolve,10));
  assert.equal(control.stopped,false);
});
