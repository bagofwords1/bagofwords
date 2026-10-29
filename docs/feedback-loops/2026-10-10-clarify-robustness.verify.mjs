import { chromium } from '@playwright/test';
const [out, variant]=process.argv.slice(2);
const PROMPTS = {
 legacy: `Do not query anything yet. Call the clarify tool exactly once, with exactly these three questions and options, verbatim: 1. text "Which metrics should I include?", multi_select true, options ["Revenue, net", "Orders, gross", "Other…"] 2. text "Which genre?", options ["Rock", "Other genres", "Jazz"] 3. text "באיזו תקופה?", options ["30 יום", "90 יום", "אחר"]`,
 natural: `Do not query anything yet. Call the clarify tool once asking: which metrics to include (pick several: "Revenue, net", "Orders, gross"; the list may not be complete) and which genre (Rock, Other genres, Jazz; the list may not be complete).`,
};
const b=await chromium.launch(); const ctx=await b.newContext({viewport:{width:1300,height:1150}}); const p=await ctx.newPage();
const sent=[]; p.on('request', r => { if (r.method()==='POST' && /\/completions$|clarify_response/.test(r.url())) sent.push(r.url().split('/api')[1].replace(/[0-9a-f-]{36}/g,'…')+'\n  '+(r.postData()||'').slice(0,420)); });
await p.goto('http://localhost:3000/users/sign-in'); await p.fill('#email','admin@example.com'); await p.fill('#password','Password123!');
await p.click('button[type=submit]'); await p.waitForURL(u=>!u.toString().includes('sign-in')); await p.waitForTimeout(3000);
await p.locator('[contenteditable=true], textarea').first().click(); await p.keyboard.type(PROMPTS[variant]); await p.keyboard.press('Enter');
const opt = t => p.locator('button', {has: p.locator('span', {hasText: new RegExp('^'+t+'$')})}).first();
const cls = async t => (await opt(t).getAttribute('class')).includes('sky')?'SELECTED':'-';
await opt('Other genres').waitFor({timeout:300000}).catch(async e=>{await p.screenshot({path:out+'-timeout.png',fullPage:true}); throw e});
const otherLabel = variant==='legacy' ? 'אחר' : 'Other';
for (const t of ['Revenue, net','Orders, gross','Other genres']) await opt(t).click();
if (variant==='legacy') await opt('אחר').click(); else await opt('Other').first().click();
console.log('right after clicks:', await cls('Revenue, net'), await cls('Orders, gross'), await cls('Other genres'));
await p.waitForTimeout(15000);
console.log('15s later:         ', await cls('Revenue, net'), await cls('Orders, gross'), await cls('Other genres'));
const ins=p.locator('input[type=text]'); console.log('"Other" text boxes open:', await ins.count());
const submit=p.getByRole('button',{name:'Submit',exact:true});
console.log('submit enabled before typing:', await submit.isEnabled());
await p.screenshot({path:out+'-picked.png',fullPage:true});
for (let i=0;i<await ins.count();i++) await ins.nth(i).fill('last 2 years');
console.log('submit enabled after typing:', await submit.isEnabled());
await submit.click(); await p.waitForTimeout(5000);
await p.screenshot({path:out+'-submitted.png',fullPage:true});
console.log(sent.slice(1).join('\n')); await b.close();
