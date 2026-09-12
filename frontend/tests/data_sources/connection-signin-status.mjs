import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const source = readFileSync(new URL('../../composables/useConnectionStatus.ts', import.meta.url), 'utf8');
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
const {getEffectiveStatus: status, needsConnectionSignIn, statusDotClass, statusBadgeClass, statusLabelKey} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
const missing = {auth_policy:'user_required',user_status:{has_user_credentials:false,effective_auth:'none',connection:'offline',query_identity:'self'}};
assert.equal(status(missing),'sign_in_required');
for (const connection of ['offline','not_connected','unknown']) {
 for (const type of ['powerbi','sharepoint','postgresql','mssql']) {
  assert.equal(status({...missing,type,user_status:{...missing.user_status,connection}}),'sign_in_required');
 }
}
for (const idx of ['running','failed','completed']) assert.equal(status({...missing,indexing:{status:idx,scope:'org'}}),'sign_in_required');
for (const connection of ['offline','not_connected','error']) {
 assert.equal(status({user_status:{connection,has_user_credentials:true,effective_auth:'user'}}),'error');
 assert.equal(status({user_status:{connection,has_user_credentials:false,effective_auth:'system'}}),'error');
}
assert.equal(status({...missing,user_status:{...missing.user_status,connection:'error'}}),'error');
assert.equal(status({last_connection_status:'not_connected'}),'error');
assert.equal(status({user_status:{effective_auth:'system',has_user_credentials:false,connection:'success'}}),'success');
assert.equal(status({indexing:{status:'running'}}),'indexing');
assert.equal(status({last_connection_status:'success',indexing:{status:'failed'}}),'indexing_failed');
assert.equal(status({}),'unknown');
assert.equal(statusDotClass('sign_in_required'),'bg-gray-400');
assert.match(statusBadgeClass('sign_in_required'),/gray/);
assert.equal(statusLabelKey('sign_in_required'),'data.signInRequired');
console.log('PASS: missing personal access is neutral; stored-credential/system failures stay errors; indexing and unknown unchanged.');

assert.equal(needsConnectionSignIn(missing), true);
assert.equal(needsConnectionSignIn({...missing,user_status:{effective_auth:'system',has_user_credentials:false}}), false);
assert.equal(needsConnectionSignIn({...missing,user_status:{effective_auth:'user',has_user_credentials:true}}), false);
assert.equal(needsConnectionSignIn({auth_policy:'system_only'}), false);
