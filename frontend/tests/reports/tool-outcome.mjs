import { readFileSync } from 'node:fs'
import ts from 'typescript'
import assert from 'node:assert/strict'
const code = ts.transpileModule(readFileSync('utils/toolOutcome.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.ESNext}}).outputText
const {toolOutcome} = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
const cases = [
 [{outcome:'cancelled'},'error','','','cancelled'],
 [{outcome:'approval_expired',approval:{timed_out:true},blocked_by_policy:'ask'},'error','','stopped','approval_expired'],
 [{approval:{cancelled:true,timed_out:false}},'error','','','cancelled'],
 [{approval:{timed_out:true},blocked_by_policy:'ask'},'error','','stopped','cancelled'],
 [{approval:{timed_out:true},blocked_by_policy:'ask'},'error','','success','approval_expired'],
 [{approval:{approved:false},blocked_by_policy:'ask'},'error','','','denied'],
 [{error_message:'Remote operation timed out; check state.',success:false},'error','','stopped','execution_timed_out'],
 [{success:false},'error','','','failed'],
 [{success:true},'success','','','success'],
 [{},'running','awaiting_approval','','awaitingApproval'],
 [{},'running','calling_tool','','running'],
 [{},'running','calling_tool','stopped','cancelled'],
 [{},'cancelled','','','cancelled'],
]
for(const [result,status,stage,run,expected] of cases) assert.equal(toolOutcome(result,status,stage,run),expected)
console.log(`${cases.length} tool outcome contracts passed`)
