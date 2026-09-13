const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../../..');
const ts = require(path.join(root, 'frontend/node_modules/typescript'));
const source = fs.readFileSync(path.join(root, 'frontend/pages/reports/[id]/index.vue'), 'utf8').match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const ast = ts.createSourceFile('report.ts', source, ts.ScriptTarget.Latest, true);
function fn(name) { return ast.statements.find(n => ts.isFunctionDeclaration(n) && n.name.text === name).getText(ast); }
const js = ts.transpileModule(fn('handleStreamingEvent'), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
(async () => {
 const errors = [];
 for (const scenario of [
  { name: 'own edit with no transcript message', index: -1, messages: [], own: true, status: 'success', expected: 1 },
  { name: 'own edit with no matching tool block', index: 0, messages: [{completion_blocks:[]}], own: true, status: 'success', expected: 1 },
  { name: 'external edit refreshes without takeover', index: -1, messages: [], own: false, status: 'success', expected: 1 },
  { name: 'failed edit preserves version', index: -1, messages: [], own: true, status: 'error', expected: 0 },
 ]) {
  const events=[];
  const context = vm.createContext({ messages:{value:scenario.messages}, hasArtifacts:{value:false}, report_id:'report-a', requestedArtifactId:{value:null},
   checkHasArtifacts:async()=>true,
   window:{dispatchEvent:e=>events.push(e)}, CustomEvent:class {constructor(type, init){this.type=type;this.detail=init.detail;}}, console });
  vm.runInContext(js, context);
  await context.handleStreamingEvent('tool.finished', {tool_name:'edit_artifact',status:scenario.status,result_json:{artifact_id:'version-2'}},scenario.index,{ownStream:scenario.own});
  try { assert.equal(events.length,scenario.expected); if(events.length){assert.equal(events[0].detail.artifact_id,'version-2');assert.equal(events[0].detail.select,scenario.own);} console.log('PASS',scenario.name); }
  catch(e){errors.push(scenario.name);console.log('FAIL',scenario.name,e.message);}
 }
 // Exercise the real reconnect dispatch, including the option passed to its consumer.
 const recovery=fn('recoverStreamAfterError');
 const context=vm.createContext({messages:{value:[{id:'optimistic',status:'in_progress',system_completion_id:'server'}]},startWatchStream:(id,opts)=>{context.options=opts;}});
 vm.runInContext(ts.transpileModule(recovery,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,context);
 await context.recoverStreamAfterError('optimistic');
 try {assert.equal(context.options.ownStream,true);console.log('PASS own reconnect retains ownership');}catch(e){errors.push('reconnect ownership');console.log('FAIL own reconnect retains ownership');}
 if(errors.length) throw new Error(errors.join(', '));
})();
