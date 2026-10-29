import { chromium } from 'playwright';
const b = await chromium.launch({executablePath:'/opt/pw-browsers/chromium'}).catch(()=>chromium.launch());
const p = await b.newPage();
for (const page of ['attack','legit']) {
  await p.goto(`http://127.0.0.1:8765/__h/${page}.html`); await p.waitForTimeout(2500);
  const f = p.frame({url:/mcp-artifact-app/});
  const r = await f.evaluate(()=>({pwned:document.body.dataset.pwned||null, err:(document.querySelector('.artifact-error')||{}).textContent||null}));
  console.log(page, JSON.stringify(r));
}
await b.close();
