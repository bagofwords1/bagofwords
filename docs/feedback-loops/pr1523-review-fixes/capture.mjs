// Run from the repository root. The real iframe/runtime uses a controlled
// host transport, so no model, customer account or credentials are needed.
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {browser, openPage, start, sendStarted, a, u, result, pageOf} from '../../../frontend/tests/unit/support/chatHarness.mjs';

const out = new URL('../../../media/pr/chat-widget-review/', import.meta.url);
fs.mkdirSync(out, {recursive: true});
const base = execFileSync('git', ['show', '48e712425:frontend/public/libs/artifact-globals.js'], {encoding: 'utf8'});
const style = `<style>body{background:#f5f7fb;padding:24px}#panel{background:white;border:1px solid #dfe3ea;border-radius:14px;padding:22px;width:680px;margin:auto}h1{font-size:20px;margin:0 0 6px}#hint{color:#64748b;font-size:13px;margin-bottom:22px}[data-testid=bow-chat-steps]{margin:12px 0}</style>`;
try {
  for (const revision of ['before', 'after']) {
    const context = await browser.newContext({viewport: {width: 800, height: 720}, recordVideo: {dir: out.pathname, size: {width: 800, height: 720}}});
    const env = await openPage(context);
    if (revision === 'before') await env.page.route('**/libs/artifact-globals.js*', route => route.fulfill({body: base, contentType: 'application/javascript', headers: {'Access-Control-Allow-Origin': '*'}}));
    for (const locale of ['en', 'he']) {
      const he = locale === 'he';
      const message = a('answer', he ? '**ההכנסות עלו** ב־12%.' : '**Revenue increased** by 12%.', {
        steps: [he ? 'בדקתי את **נתוני המכירות**.' : 'I checked the **sales data**.'], results: [result('sales')], results_incomplete: true
      });
      const code = `${style}<script>function App(){return React.createElement('section',{id:'panel',dir:${JSON.stringify(he ? 'rtl' : 'ltr')}},
        React.createElement('h1',null,${JSON.stringify(he ? 'עוזר המכירות' : 'Sales assistant')}),
        React.createElement('p',{id:'hint'},'ChatMessages · showResults={false}'),
        React.createElement(ChatMessages,{chat:{messages:[${JSON.stringify(message)}]},showResults:false,labels:{steps:${JSON.stringify(he ? 'שלבי התשובה' : 'Answer steps')}}}));}
        ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));</script>`;
      const s = await start(env, {code, waitLoad: false, viewport: {width: 784, height: 700}});
      await s.frame.locator('summary').click();
      await env.page.screenshot({path: new URL(`${revision}-${locale}.png`, out).pathname});
      await s.done();
    }
    const code = `${style}<script>function App(){var chat=useConversation();window.__chat=chat;return React.createElement('section',{id:'panel'},
      React.createElement('h1',null,'Sales assistant'),React.createElement('p',{id:'hint'},'Stop request fails, then the viewer retries'),
      React.createElement(ChatMessages,{chat}),React.createElement(ChatComposer,{chat}));}
      ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));</script>`;
    const s = await start(env, {code, viewport: {width: 784, height: 700}, auto: {stop: 'hold', messages: pageOf([u('question','Count orders'),a('answer','Counting orders…',{status:'in_progress'})])}});
    await sendStarted(s, 'Count orders', 'answer');
    await s.until(c => c.messages.some(m => m.id === 'answer'), 'stream started');
    await s.call('stop');
    const stop = await s.take('stop');
    await s.fail(stop.id, {code: 'UNAVAILABLE', message: 'Stop did not reach the server. Please retry.'});
    await s.until(c => revision === 'after' ? c.answering && c.error : !c.answering, 'stop response');
    await env.page.screenshot({path: new URL(`${revision}-stop.png`, out).pathname});
    if (revision === 'after') {
      await s.call('stop');
      const retry = await s.take('stop');
      await s.reply(retry.id,{id:'answer',status:'stopped'});
      await s.until(c => !c.answering, 'stopped');
      await env.page.screenshot({path: new URL('after-retry.png', out).pathname});
    }
    await s.done();
    const video = env.page.video();
    await context.close();
    await video.saveAs(new URL(`${revision}.webm`, out).pathname);
    await video.delete();
  }
} finally { await browser.close(); }
