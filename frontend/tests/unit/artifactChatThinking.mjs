import assert from 'node:assert/strict';
import {start, run, sendStarted, finish, pageOf, a} from './support/chatHarness.mjs';
const code = `<script>
function App() {
  var chat = useConversation(); window.__chat = chat;
  var show = React.useState(); window.__showThinking = show[1];
  return React.createElement(ChatMessages, {chat:chat, showThinking:show[0], labels:{thinking:'Thought process'}});
}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
</script>`;
await run('artifactChatThinking', [{name:'thinking stays separate, hidden by default, opt-in collapsed, and survives history', fn:async env => {
  const s = await start(env, {code});
  const req = await sendStarted(s, 'Calculate', 'answer');
  await s.events(req.id, [
    {type:'thinking_delta', block:'a', text:'First '},
    {type:'text_delta', block:'answer', text:'42'},
    {type:'thinking_delta', block:'a', text:'draft'},
    {type:'thinking', block:'a', text:'Checked totals'},
    {type:'thinking_delta', block:'b', text:'Second check'}
  ]);
  const c = await s.until(c => c.messages.at(-1).thinking === 'Checked totals\n\nSecond check', 'separate thinking');
  assert.equal(c.messages.at(-1).text, '42');
  assert.deepEqual(c.messages.at(-1).steps, []);
  assert.equal(await s.frame.locator('[data-testid="bow-chat-thinking"]').count(), 0);
  await s.event(req.id, {type:'thinking', block:'b', text:''});
  await s.until(c => c.messages.at(-1).thinking === 'Checked totals', 'empty replacement clears block');
  await s.event(req.id, {type:'thinking_delta', block:'b', text:'Second check'});
  await s.until(c => c.messages.at(-1).thinking.endsWith('Second check'), 'restored block');
  await s.dom(() => window.__showThinking(true));
  const details = s.frame.locator('[data-testid="bow-chat-thinking"]');
  await details.waitFor();
  assert.equal(await details.getAttribute('open'), null);
  await details.locator('summary').click();
  assert.ok((await details.innerText()).includes('Second check'));
  await s.event(req.id, {type:'thinking_delta', block:'b', text:' complete'});
  await s.until(c => c.messages.at(-1).thinking.endsWith('complete'), 'live update');
  assert.notEqual(await details.getAttribute('open'), null);
  await finish(s, req, pageOf([{...a('answer','42'), thinking:'Checked totals\n\nSecond check complete'}]));
  assert.equal((await s.chat()).messages.at(-1).thinking, 'Checked totals\n\nSecond check complete');
  await s.dom(() => window.__showThinking(false));
  await s.waitFor(() => !document.querySelector('[data-testid="bow-chat-thinking"]'), null, 'hidden again');
  await s.done();
}}]);
