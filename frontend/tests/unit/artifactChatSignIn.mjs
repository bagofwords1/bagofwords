import assert from 'node:assert/strict';
import {start, run} from './support/chatHarness.mjs';
const code=`<script>
function App(){var chat=useConversation();window.__chat=chat;return React.createElement(ChatComposer,{chat:chat,placeholder:'Message support'});}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
</script>`;
await run('artifactChatSignIn',[{name:'signed-out composer requests sign-in without passing a redirect or credentials',fn:async env=>{
 const s=await start(env,{code,auto:{policy:{available:false,reason:'auth_required',agents:[]},signIn:null}});
 const action=s.frame.locator('[data-testid="bow-chat-unavailable"] button');
 await action.waitFor();await action.click();
 const req=await s.take('signIn');
 assert.deepEqual(req.args,{});
 assert.equal(await s.frame.locator('[data-testid="bow-chat-input"]').isEnabled(),false);
 await s.done();
}}]);
