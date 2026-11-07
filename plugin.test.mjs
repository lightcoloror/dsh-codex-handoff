import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { registerReverse,createInboxNotifier,publishEndpoint,normalizeConfig } from './plugin.mjs';

const THREAD='11111111-2222-4333-8444-555555555555';

test('endpoint follows actual port, waits for bind, publishes no token and stops on unload', t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'endpoint-'));
  const jobs=path.join(root,'jobs');fs.mkdirSync(jobs);let port;const disposers=[];
  t.after(()=>{for(const d of disposers)d();fs.rmSync(root,{recursive:true,force:true});});
  const ctx={effect:f=>disposers.push(f()),webServer:{getOrigin:()=>{
    if(!port)throw new Error('not bound');return `http://127.0.0.1:${port}`;
  }}};
  const publisher=publishEndpoint(ctx,{jobsDir:jobs,prefix:'/bridge',tokenFile:'private'});
  assert.equal(JSON.parse(fs.readFileSync(publisher.file)).status,'starting');
  port=1251;publisher.refresh();let record=JSON.parse(fs.readFileSync(publisher.file));
  assert.equal(record.base_url,'http://127.0.0.1:1251/bridge');
  assert.equal(record.pid,process.pid);assert.equal(JSON.stringify(record).includes('private'),false);
  port=8877;publisher.refresh();assert.equal(JSON.parse(fs.readFileSync(publisher.file)).base_url,'http://127.0.0.1:8877/bridge');
  const next=publishEndpoint(ctx,{jobsDir:jobs,prefix:'/bridge'});
  publisher.dispose();assert.equal(JSON.parse(fs.readFileSync(next.file)).instance_id,next.instance);
  next.dispose();assert.equal(JSON.parse(fs.readFileSync(next.file)).status,'stopped');
});

test('endpoint rejects a second live process and blocks non-loopback listeners', t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'endpoint-'));
  const jobs=path.join(root,'jobs');fs.mkdirSync(jobs);const ds=[];
  t.after(()=>{for(const d of ds)d();fs.rmSync(root,{recursive:true,force:true});});
  const file=path.join(root,'endpoint.json');
  fs.writeFileSync(file,JSON.stringify({status:'ready',pid:process.ppid,instance_id:'other'}));
  const ctx={effect:f=>ds.push(f()),webServer:{getOrigin:()=> 'http://0.0.0.0:1251'}};
  assert.throws(()=>publishEndpoint(ctx,{jobsDir:jobs}),/another live Harness/);
  fs.unlinkSync(file);
  const publisher=publishEndpoint(ctx,{jobsDir:jobs});
  assert.equal(JSON.parse(fs.readFileSync(publisher.file)).status,'blocked');
  const config=normalizeConfig({dataDir:path.join(root,'data')});
  assert.deepEqual(config.workspacePaths,[]);
  assert.equal(config.jobsDir,path.join(root,'data','.state','jobs'));
});
function fixture(t, fail=false) {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'handoff-'));
  const workspace=path.join(root,'workspace'), jobs=path.join(root,'jobs');
  fs.mkdirSync(workspace); fs.mkdirSync(path.join(workspace,'.tmp')); fs.mkdirSync(jobs);
  const directory=path.join(jobs,'job-one'); fs.mkdirSync(directory); fs.mkdirSync(path.join(directory,'callbacks'));
  const binding={job_id:'job-one',status:'active',session_id:'session-one',thread_id:THREAD,workspace_path:workspace,nonce:'nonce'};
  fs.writeFileSync(path.join(directory,'binding.json'),JSON.stringify(binding));
  let cwd=workspace, definition; const calls=[],disposers=[];
  const ctx={ effect:f=>{const d=f();if(typeof d==='function')disposers.push(d);return d;}, get:()=>({ensure:async()=>cwd}), tools:{register:d=>{definition=d;return()=>{};}},
    subprocess:{spawn: spec=>{calls.push(spec);return {done:Promise.resolve({exitCode:fail?1:0}),collected:{
      stdout:{readFrom:()=>({text:fail?'':`Queued message 11111111-2222-4333-8444-555555555555 for thread ${THREAD}.`})},stderr:{readFrom:()=>({text:fail?'synthetic failure':''})}}};}}};
  const config={jobsDir:jobs,workspacePaths:[workspace],codexHome:root,executable:process.execPath,discoveryFile:'contract',cliPath:'cli.py',enableInboxWakeups:false,wakeupDelayMs:0};
  registerReverse(ctx,config);
  t.after(()=>{for(const d of disposers.reverse())d();fs.rmSync(root,{recursive:true,force:true});});
  return {ctx,config,root,workspace,jobs,directory,binding,calls,call:(args,session='session-one')=>definition.execute(args,{agent:{id:session}}),setCwd:x=>{cwd=x;}};
}
const args={job_id:'job-one',message_id:'result-1',nonce:'nonce',message:'Done; review the specified artifact.'};

test('foreign caller, nonce, workspace, path and target argument denied before native invocation',async t=>{
  const f=fixture(t);
  await assert.rejects(f.call(args,'other-session'),/identity or caller denied/);
  await assert.rejects(f.call({...args,nonce:'wrong'}),/identity or caller denied/);
  await assert.rejects(f.call({...args,job_id:'../outside'}),/invalid identifier/);
  await assert.rejects(f.call({...args,thread:THREAD}),/invalid arguments/);
  f.setCwd(f.root); await assert.rejects(f.call(args),/workspace denied/);
  assert.equal(f.calls.length,0);
});
test('one queue, order-independent durable replay, conflict and pending backpressure, closed-job denial',async t=>{
  const f=fixture(t); const first=JSON.parse((await f.call(args)).text);
  assert.equal(first.status,'queued'); assert.equal(first.replayed,false);
  assert.deepEqual(f.calls[0].argv.slice(1,6),['queue','--resume','--thread',THREAD,'--message']);
  assert.equal(f.calls[0].cwd,f.workspace); assert.equal(f.calls[0].env.TMP,path.join(f.workspace,'.tmp'));
  const replay=JSON.parse((await f.call({message:args.message,nonce:'nonce',message_id:'result-1',job_id:'job-one'})).text);
  assert.equal(replay.replayed,true); assert.equal(replay.native_message_id,first.native_message_id);
  await assert.rejects(f.call({...args,message:'changed'}),/message conflict/);
  await assert.rejects(f.call({...args,message_id:'result-2'}),/awaits Codex acknowledgement/);
  assert.equal(f.calls.length,1);
  fs.writeFileSync(path.join(f.directory,'binding.json'),JSON.stringify({...f.binding,status:'closed'}));
  await assert.rejects(f.call(args),/not active/); assert.equal(f.calls.length,1);
});
test('ambiguous native failure persists uncertainty and cannot be resent or bypassed',async t=>{
  const f=fixture(t,true);
  await assert.rejects(f.call(args),/callback uncertain/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.directory,'callbacks','result-1.json'))).status,'uncertain');
  await assert.rejects(f.call(args),/previous send uncertain/);
  await assert.rejects(f.call({...args,message_id:'bypass'}),/awaits Codex acknowledgement/);
  assert.equal(f.calls.length,1);
});
test('inbox publication is immediate without native queue; replay and one coalesced wakeup',async t=>{
  const f=fixture(t);fs.writeFileSync(path.join(f.directory,'binding.json'),JSON.stringify({...f.binding,delivery_mode:'inbox'}));
  const first=JSON.parse((await f.call(args)).text);
  assert.equal(first.status,'inbox');assert.equal(f.calls.length,0);
  assert.equal(JSON.parse((await f.call({...args})).text).replayed,true);
  const second=path.join(f.jobs,'job-two');fs.mkdirSync(second);fs.mkdirSync(path.join(second,'callbacks'));
  fs.writeFileSync(path.join(second,'binding.json'),JSON.stringify({...f.binding,job_id:'job-two',delivery_mode:'inbox'}));
  const copied=JSON.parse(fs.readFileSync(path.join(f.directory,'callbacks/result-1.json')));copied.job_id='job-two';
  fs.writeFileSync(path.join(second,'callbacks/result-1.json'),JSON.stringify(copied));
  const notifier=createInboxNotifier(f.ctx,f.config,f.jobs);
  await notifier.sweep();assert.equal(f.calls.length,1);
  await notifier.sweep();assert.equal(f.calls.length,1);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.directory,'callbacks/result-1.json'))).status,'inbox');
  assert.match(f.calls[0].argv[6],/DSH INBOX WAKEUP/);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root,'wakeups',`${THREAD}.json`))).jobs.length,2);
  notifier.dispose();
});
test('failed wakeup does not lose inbox delivery and is not blindly resent',async t=>{
  const f=fixture(t,true);fs.writeFileSync(path.join(f.directory,'binding.json'),JSON.stringify({...f.binding,delivery_mode:'inbox'}));
  assert.equal(JSON.parse((await f.call(args)).text).status,'inbox');
  const notifier=createInboxNotifier(f.ctx,f.config,f.jobs);await notifier.sweep();await notifier.sweep();
  assert.equal(f.calls.length,1);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root,'wakeups',`${THREAD}.json`))).status,'uncertain');
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.directory,'callbacks/result-1.json'))).status,'inbox');
  notifier.dispose();
});

async function published(t){
  const f=fixture(t);fs.writeFileSync(path.join(f.directory,'binding.json'),JSON.stringify({...f.binding,delivery_mode:'inbox'}));
  await f.call(args);return f;
}
test('queued delivery with failed resume is explicitly unhealthy and never blindly requeued',async t=>{
  const f=await published(t);const original=f.ctx.subprocess.spawn;
  f.ctx.subprocess.spawn=spec=>{const h=original(spec);h.collected.stderr.readFrom=()=>({text:'WARNING: failed to resume thread on local daemon'});return h;};
  const notifier=createInboxNotifier(f.ctx,f.config,f.jobs);await notifier.sweep();await notifier.sweep();
  const state=JSON.parse(fs.readFileSync(path.join(f.root,'wakeups',THREAD+'.json')));
  assert.equal(state.status,'queued');assert.equal(state.resume_status,'failed');assert.equal(f.calls.length,1);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root,'delivery-health.json'))).needs_attention,true);notifier.dispose();
});
test('old unread messages remain overdue after queue success; already read messages are not republished',async t=>{
  const f=await published(t);const file=path.join(f.directory,'callbacks/result-1.json');let receipt=JSON.parse(fs.readFileSync(file));
  receipt.created_at=new Date(Date.now()-360000).toISOString();fs.writeFileSync(file,JSON.stringify(receipt));
  const notifier=createInboxNotifier(f.ctx,f.config,f.jobs);await notifier.sweep();
  let health=JSON.parse(fs.readFileSync(path.join(f.root,'delivery-health.json')));
  assert.equal(health.needs_attention,true);assert.equal(health.pending_messages,1);
  receipt.inbox_read_at=new Date().toISOString();fs.writeFileSync(file,JSON.stringify(receipt));
  await notifier.sweep();health=JSON.parse(fs.readFileSync(path.join(f.root,'delivery-health.json')));
  assert.equal(health.threads[0].state,'read_awaiting_ack');assert.equal(f.calls.length,1);
  receipt.status='acknowledged';fs.writeFileSync(file,JSON.stringify(receipt));await notifier.sweep();
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root,'delivery-health.json'))).pending_messages,0);notifier.dispose();
});
test('overlapping timer callbacks cannot duplicate a queued wakeup',async t=>{
  const f=await published(t);const notifier=createInboxNotifier(f.ctx,f.config,f.jobs);
  await Promise.all([notifier.sweep(),notifier.sweep(),notifier.sweep()]);assert.equal(f.calls.length,1);notifier.dispose();
});
