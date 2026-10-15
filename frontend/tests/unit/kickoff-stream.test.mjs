// Exercise the page's real SSE consumer with controllable network boundaries.
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
import ts from 'typescript';
const source = fs.readFileSync(process.env.BOW_REPORT_PAGE || new URL('../../pages/reports/[id]/index.vue', import.meta.url), 'utf8');
const start = source.indexOf('async function startStreaming(');
const code = ts.transpileModule(source.slice(start, source.indexOf('// === SSE resume:', start)), {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
const tick = () => new Promise(resolve => setImmediate(resolve));
function setup() {
  const streams = [];
  const ctx = {console, Date, Error, TextDecoder, report_id:'report', kickoffStalled:false,
    lastKickoffByteAt:0, currentController:new AbortController(),
    isStreaming:{value:true}, isCompletionInProgress:{value:true},
    messages:{value:[{id:'temporary',status:'in_progress'}]}, promptBoxRef:{value:null},
    startKickoffWatchdog(){},stopKickoffWatchdog(){},scheduleFollowScroll(){},
    loadReport(){},loadReportSummary(){},loadCompletions(){},recoverStreamAfterError:async()=>false,
    useMyFetch:async()=>({data:{value:new Response(new ReadableStream({start(c){streams.push(c)}}))}}),
    handleStreamingEvent:async(type,payload,idx)=>{
      const m=ctx.messages.value[idx];
      if(type==='completion.started')m.system_completion_id=payload.system_completion_id;
      if(type==='block.upsert')m.content=payload.block.content;
      if(type==='completion.finished'){m.status=payload.status;ctx.isCompletionInProgress.value=false;}
    },
  };
  vm.createContext(ctx);vm.runInContext(code,ctx);
  return {ctx,streams,emit(i,type,data){streams[i].enqueue(new TextEncoder().encode(`event: ${type}\ndata: ${JSON.stringify({data})}\n\n`));}};
}
for(const id of ['canonical-A','canonical-B'])test(`events survive canonical replacement: ${id}`,async()=>{
  const {ctx,streams,emit}=setup();const run=ctx.startStreaming({},'temporary');
  emit(0,'completion.started',{system_completion_id:id});await tick();
  ctx.messages.value=[{id,status:'in_progress'}];
  emit(0,'block.upsert',{block:{content:'The answer'}});
  emit(0,'completion.finished',{status:'success'});streams[0].close();await run;
  assert.equal(ctx.messages.value[0].content,'The answer');
  assert.equal(ctx.messages.value[0].status,'success');
});
for(const ending of ['done','error','eof'])test(`old ${ending} cannot affect a newer stream`,async()=>{
  const {ctx,streams,emit}=setup();const old=ctx.startStreaming({},'temporary');await tick();
  const newer=new AbortController();ctx.currentController=newer;
  ctx.messages.value.push({id:'new',status:'in_progress'});
  const run=ctx.startStreaming({},'new');await tick();
  if(ending==='done')streams[0].enqueue(new TextEncoder().encode('data: [DONE]\n\n'));
  else if(ending==='error')streams[0].error(new Error('old connection lost'));
  else streams[0].close();
  await old;
  assert.equal(ctx.currentController,newer);
  assert.equal(ctx.isStreaming.value,true);
  assert.equal(ctx.isCompletionInProgress.value,true);
  emit(1,'block.upsert',{block:{content:'New answer'}});streams[1].close();await run;
  assert.equal(ctx.messages.value[1].content,'New answer');
});

test('a refresh response cannot overwrite an active stream',async()=>{
  const {ctx}=setup();
  const begin=source.indexOf('async function loadCompletions(');
  const finish=source.indexOf('// === Lazy step-data hydration',begin);
  const loadCode=ts.transpileModule('let completionLoadGeneration = 0;\n'+source.slice(begin,finish),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  let release;
  Object.assign(ctx,{pageLimit:20,completionsLoaded:{value:false},hasMore:{value:false},
    nextTick:async()=>{},observeStepContainers(){},scrollContainer:{value:null},
    useMyFetch:()=>new Promise(resolve=>{release=resolve}),
  });
  vm.runInContext(loadCode,ctx);
  const before=ctx.messages.value;
  const refresh=ctx.loadCompletions();
  release({data:{value:{completions:[]}}});
  await refresh;
  assert.equal(ctx.messages.value,before);
});

for (const overlap of ['steering', 'fan-in']) test(`canonical refresh replaces placeholders before ${overlap}`, async () => {
  const {ctx}=setup();
  const begin=source.indexOf('async function loadCompletions(');
  const finish=source.indexOf('// === Lazy step-data hydration',begin);
  const loadCode=ts.transpileModule('let completionLoadGeneration = 0;\n'+source.slice(begin,finish),{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
  const canonical=[{id:'user-real',role:'user'},{id:'system-real',role:'system'},{id:overlap,role:'user'}];
  Object.assign(ctx,{currentController:null,pageLimit:20,completionsLoaded:{value:false},hasMore:{value:true},
    messages:{value:[{id:'older-history',role:'user'},{id:'user-client',role:'user'},
      {id:'system-client',role:'system',system_completion_id:'system-real'},{id:overlap,role:'user'}]},
    cursorBefore:{value:'older-cursor'},compactionWatermarkId:{value:null},expandedScheduledIds:{value:new Set()},
    manuallyToggledReasoning:{value:new Set()},collapsedReasoning:{value:new Set()},
    nextTick:async()=>{},observeStepContainers(){},scrollContainer:{value:null},
    followScrollToBottom(){},enrichForkedQueries:async()=>{},pinDispatchedQueuedPrompts(){},
    useMyFetch:async()=>({data:{value:{completions:canonical}}}),
  });
  vm.runInContext(loadCode,ctx);
  await ctx.loadCompletions({skipEstimate:true});
  assert.deepEqual(Array.from(ctx.messages.value,m=>m.id),['older-history',...canonical.map(m=>m.id)]);
  assert.equal(ctx.messages.value[1]._render_key,'user-client');
  assert.equal(ctx.messages.value[2]._render_key,'system-client');
  assert.equal(ctx.cursorBefore.value,'older-cursor');
});

test('main chat opts into busy queue receipts and reconciles the queued turn', async () => {
  const {ctx}=setup();
  let submitted;
  const queued={id:'queued-user',role:'user',status:'queued'};
  Object.assign(ctx,{
    queuedFromHere:new Set(),
    useMyFetch:async(_url,options)=>{
      submitted=JSON.parse(options.body);
      return {data:{value:Response.json({completions:[queued]})}};
    },
    loadCompletions:async()=>{ctx.messages.value=[queued];},
  });
  await ctx.startStreaming({prompt:{content:'Please include the revised launch date.'},stream:true},'temporary');
  assert.equal(submitted.queue_if_busy,true);
  assert.equal(submitted.prompt.content,'Please include the revised launch date.');
  assert.equal(ctx.messages.value[0].status,'queued');
  assert.equal(ctx.queuedFromHere.has('queued-user'),true);
  assert.equal(ctx.isStreaming.value,false);
  assert.equal(ctx.currentController,null);
});

for (const refreshFails of [false, true]) test(`queued receipt keeps the active turn stoppable while history ${refreshFails ? 'fails' : 'waits'}`, async () => {
  const {ctx}=setup();
  let refreshResolve;
  let watched;
  Object.assign(ctx,{
    queuedFromHere:new Set(),
    useMyFetch:async()=>({data:{value:Response.json({busy_completion_id:'active-turn',completions:[{id:'queued-followup',role:'user',status:'queued',prompt:{content:'Also compare returning customers.'}}]})}}),
    loadCompletions:()=>new Promise((resolve,reject)=>{refreshResolve=()=>refreshFails ? reject(new Error('offline')) : resolve();}),
    startWatchStream:(id)=>{watched=id;},
  });
  const run=ctx.startStreaming({},'temporary');
  await tick();
  assert.equal(watched,'active-turn');
  assert.equal(ctx.isCompletionInProgress.value,true);
  assert.ok(ctx.messages.value.some(m=>m.role==='system' && m.status==='in_progress' && m.system_completion_id==='active-turn'));
  assert.ok(ctx.messages.value.some(m=>m.id==='queued-followup' && m.status==='queued'));
  refreshResolve();await run;
  assert.equal(ctx.isCompletionInProgress.value,true);
  assert.equal(ctx.currentController,null);
});
