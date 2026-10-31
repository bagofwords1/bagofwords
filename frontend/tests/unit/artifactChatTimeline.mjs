import assert from 'node:assert/strict';
import fs from 'node:fs';
import {start, run, sendStarted, finish, pageOf, a} from './support/chatHarness.mjs';
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
 const content=await s.frame.locator('[data-testid="bow-chat-message"][data-role="assistant"]').innerText();
 assert.ok(content.indexOf('Checking the source')<content.indexOf('Search repository'));
 assert.ok(content.indexOf('Search repository')<content.indexOf('Here are the instructions'));
 const timeline=(await s.chat()).messages.at(-1).timeline;
 await finish(s,req,pageOf([{...a('answer','Here are the instructions'),timeline}]));
 const reopened=await s.frame.locator('[data-testid="bow-chat-message"][data-role="assistant"]').innerText();
 assert.ok(reopened.indexOf('Search repository')<reopened.indexOf('Here are the instructions'));
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
