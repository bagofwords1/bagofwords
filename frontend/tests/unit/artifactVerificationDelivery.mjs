// Exercise the actual query bridge and payload builder with HTTP and iframe
// boundaries replaced. No server or database is required.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const path = new URL('../../components/dashboard/ArtifactFrame.vue', import.meta.url)
const source = fs.readFileSync(process.env.ARTIFACT_FRAME_SOURCE || path, 'utf8')
const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const ast = ts.createSourceFile('frame.ts', script, ts.ScriptTarget.Latest, true)
const functions = new Set(['paramSubsetForQuery', 'runParamQueries', 'sendDataToIframe'])
const selected = ast.statements.filter(s =>
  ts.isFunctionDeclaration(s) && functions.has(s.name?.text) ||
  ts.isVariableStatement(s) && s.declarationList.declarations.some(d => d.name.getText(ast) === 'verificationRequests'))
assert.equal(selected.length, functions.size + 1)
const code = ts.transpileModule(selected.map(s => s.getText(ast)).join('\n'), {compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText
const events = [], messages = [], requests = []
let respond = async (url, options) => {
  const id = String(requests.length + 1)
  requests.push({id, url, options})
  return {data:{value:{status:'success', verification_request_id:id, data:{rows:[{request:id}], columns:[]}}}, error:{value:null}}
}
const state = {
  props:{verificationPreview:true}, paramAckSeq:0, paramRunCounter:0, latestParamRunForQid:{}, verificationRevision:0,
  queryParamSpecs:{value:{sales:[{name:'country',source:'input'}],stock:[{name:'warehouse',source:'input'}]}},
  paramValues:{value:{}}, viewAsMode:{value:'you'}, iframeReady:{value:true}, dataReady:{value:false}, iframeError:{value:null},
  visualizationsData:{value:[{id:'sales-chart',queryId:'sales',rows:[]},{id:'stock-chart',queryId:'stock',rows:[]}]},
  reportData:{value:{}},filesData:{value:[]},effectiveViewerContext:{value:{}},
  window:{location:{origin:'https://preview.test'}},console:{log(){},error(){}},
  iframeRef:{value:{contentWindow:{postMessage(m){messages.push(m)}}}},
  toRaw:v=>v, paramsPayload:()=>({}),artifactRuntime:()=>({}),syncParamsToUrl(){},postParamsStatus(){},
  verificationEvent:(kind,fields)=>events.push({kind,...fields}),useMyFetch:(...args)=>respond(...args),
}
vm.createContext(state)
vm.runInContext(code, state)
const delivered = () => JSON.parse(JSON.stringify(events.filter(e=>e.kind==='data_sent').at(-1).request_ids)).sort()

await state.runParamQueries({country:'CA'})
state.sendDataToIframe()
assert.deepEqual(delivered(),['1'])
await state.runParamQueries({country:'US'})
state.sendDataToIframe()
assert.deepEqual(delivered(),['2'], 'replacement data must not acknowledge superseded requests')
state.sendDataToIframe()
assert.deepEqual(delivered(),['2'], 'resending a payload must retain evidence if an earlier delivery was lost')
await state.runParamQueries({warehouse:'east'})
state.sendDataToIframe()
assert.deepEqual(delivered(),['2','3'], 'full payload includes current results for independent queries')

// A late response cannot replace newer data or contribute an acknowledgement.
const immediate = respond
let finishOld
respond = () => new Promise(resolve=>{finishOld=resolve})
const old = state.runParamQueries({country:'GB'})
respond = immediate
await state.runParamQueries({country:'FR'})
finishOld({data:{value:{status:'success',verification_request_id:'obsolete',data:{rows:[{request:'obsolete'}]}}},error:{value:null}})
await old
state.sendDataToIframe()
assert.deepEqual(delivered(),['3','4'])
assert.equal(messages.at(-1).payload.visualizations[0].rows[0].request,'4')

respond = async()=>({data:{value:{status:'error',verification_request_id:'failed'}},error:{value:null}})
await state.runParamQueries({country:'DE'})
state.sendDataToIframe()
assert.deepEqual(delivered(),['3','4'], 'failed queries cannot acknowledge a new result')
state.visualizationsData.value = state.visualizationsData.value.filter(v=>v.queryId==='sales')
state.sendDataToIframe()
assert.deepEqual(delivered(),['4'], 'removed datasets cannot be acknowledged by later payloads')
console.log('artifactVerificationDelivery: all assertions passed')
