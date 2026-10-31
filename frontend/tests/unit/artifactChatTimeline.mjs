import assert from 'node:assert/strict';
import fs from 'node:fs';
import {start, run, sendStarted, finish, pageOf, a} from './support/chatHarness.mjs';
const expected = ['Checking the source', 'Search repository', 'Here are the instructions'];
const savedTool = {id:'lookup',name:'execute_mcp',title:'Search repository',status:'success'};
const saved = () => a('answer','Here are the instructions', {
 steps:['Checking the source'], tools:[savedTool],
 timeline:[{id:'intro',type:'text',text:expected[0]}, {type:'tool',...savedTool}, {id:'reply',type:'text',text:expected[2]}]
});
async function assertOrder(s) {
 const actual=await s.frame.locator('[data-role="assistant"] [data-testid="bow-chat-text"], [data-role="assistant"] [data-testid="bow-chat-tool"]').allTextContents();
 assert.deepEqual(actual.map(x=>x.trim()),expected,'Every block must exist once, in server order');
}
await run('artifactChatTimeline', [{name:'text and tool updates preserve occurrence order', fn:async env => {
 const s=await start(env);
 const req=await sendStarted(s,'Find the setup guide','answer');
 await s.events(req.id,[
  {type:'text',block:'intro',text:'Checking the source'},
  {type:'tool',id:'lookup',name:'execute_mcp',title:'Search repository',status:'in_progress'},
  {type:'text',block:'reply',text:'Here are the instructions'},
  {type:'tool',id:'lookup',name:'execute_mcp',title:'Search repository',status:'success'}
 ]);
 await s.until(c=>c.messages.at(-1).tools[0]?.status==='success','tool finished');
 if (process.env.CHAT_SCREENSHOT) { fs.mkdirSync(new URL('../../../media/pr/embedded-chat-reliability/',import.meta.url),{recursive:true}); await s.page.screenshot({path:process.env.CHAT_SCREENSHOT}); }
 await assertOrder(s);
 await finish(s,req,pageOf([saved()]));
 await s.until(c=>c.messages.at(-1).created_at===saved().created_at,'history reconciliation applied');
 await assertOrder(s);
 await s.done();
}}, {name:'fresh history and older pages retain every timeline block', fn:async env => {
 const s=await start(env);
 await s.call('select','history');
 const req=await s.take('messages');
 await s.reply(req.id,pageOf([saved()],'older'));
 await s.until(c=>!c.loading && c.messages.length===1,'fresh history loaded');
 if(process.env.CHAT_SCREENSHOT) await s.page.screenshot({path:process.env.CHAT_SCREENSHOT});
 await assertOrder(s);
 await s.call('loadOlder');
 const older=await s.take('messages',e=>e.args.before==='older');
 await s.reply(older.id,pageOf([{...saved(),id:'older-answer'}]));
 await s.until(c=>!c.loadingOlder && c.messages.length===2,'older page loaded');
 const rows=await s.frame.locator('[data-role="assistant"] [data-testid="bow-chat-text"], [data-role="assistant"] [data-testid="bow-chat-tool"]').allTextContents();
 assert.deepEqual(rows.map(x=>x.trim()),expected.concat(expected));
 await s.done();
}}, {name:'legacy history without a timeline still shows text and tools', fn:async env => {
 const s=await start(env);
 await s.call('select','legacy');
 const req=await s.take('messages');
 await s.reply(req.id,pageOf([a('legacy','Legacy answer',{tools:[savedTool]})]));
 await s.until(c=>!c.loading && c.messages.length===1,'legacy loaded');
 assert.equal(await s.frame.locator('[data-testid="bow-chat-text"]').innerText(),'Legacy answer');
 assert.equal(await s.frame.locator('[data-testid="bow-chat-tool"]').innerText(),savedTool.title);
 await s.done();
}}, {name:'artifact authors can rename tool presentation without changing operation identity', fn:async env => {
 const code = `<script>
 ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(ChatMessage,{
  message:{role:'assistant',status:'success',timeline:[{id:'lookup',type:'tool',name:'execute_mcp',title:'Read source',status:'success'}]},
  toolLabel:function(tool){return tool.name==='execute_mcp'?'Repository lookup':null;}
 }));
 </script>`;
 const s=await start(env,{code,waitLoad:false});
 assert.equal(await s.frame.locator('[data-testid="bow-chat-tool"]').innerText(),'Repository lookup');
 await s.done();
}}]);
