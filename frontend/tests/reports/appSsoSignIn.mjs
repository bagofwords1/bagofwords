// Local stack, real sign-in page; provider endpoints stubbed (no real IdP login).
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
const base=process.env.BOW_BASE_URL||'http://127.0.0.1:3100';
const dir=new URL('../../../media/pr/embedded-chat-reliability/',import.meta.url).pathname;
const browser=await chromium.launch({executablePath:process.env.PW_CHROMIUM_PATH});
const context=await browser.newContext({viewport:{width:1280,height:800},recordVideo:{dir:dir+'video'}});
context.setDefaultTimeout(10000);
let count=0;
let settings={auth:{mode:'sso_only'},oidc_providers:[{name:'entra',label:'Microsoft',brand:'microsoft',enabled:true}],google_oauth:{enabled:false}};
await context.route('**/api/settings',route=>route.fulfill({json:settings}));
await context.route('**/api/auth/**/authorize*',route=>{count++;return route.fulfill({status:400,json:{detail:'Test provider unavailable'}})});
const page=await context.newPage();
const path='/users/sign-in?redirect=%2Fd%2Fexample&app_sso=1';
try {
 await page.goto(base+path);
 if(process.env.SSO_BASELINE){
  await page.getByRole('button',{name:/Microsoft/}).waitFor();
  await page.screenshot({path:dir+'sso-before.png'});
 }
 await page.getByRole('button',{name:/Microsoft/}).waitFor();
 assert.equal(count,1,'An explicit app sign-in should start the only SSO provider');
 assert.equal(new URL(page.url()).searchParams.has('app_sso'),false,'Consume auto-start intent before calling provider');
 await page.getByRole('button',{name:/Microsoft/}).waitFor();
 await page.screenshot({path:dir+'sso-error.png'});
 await page.reload();await page.getByRole('button',{name:/Microsoft/}).waitFor();assert.equal(count,1,'Refresh after failure must not restart SSO');
 for(const suffix of ['', '?redirect=%2Fd%2Fexample', '?redirect=%2Fd%2Fexample&app_sso=1&local=true', '?redirect=%2Fd%2Fexample&app_sso=1&error_code=saml_login_failed']){
  await page.goto(base+'/users/sign-in'+suffix);await page.getByRole('button',{name:/Microsoft/}).waitFor();assert.equal(count,1,'No auto-start for '+suffix);
 }
 settings={...settings,oidc_providers:[...settings.oidc_providers,{name:'other',label:'Other',enabled:true}]};
 await page.goto(base+path);await page.getByRole('button',{name:/Microsoft/}).waitFor();assert.equal(count,1,'Multiple providers keep choice');
 settings={...settings,auth:{mode:'hybrid'},oidc_providers:[settings.oidc_providers[0]]};
 await page.goto(base+path);await page.getByRole('button',{name:/Microsoft/}).waitFor();assert.equal(count,2,'Explicit app handoff also works in hybrid mode');
 settings={...settings,auth:{mode:'local_only'}};
 await page.goto(base+path);await page.getByLabel('Email',{exact:true}).waitFor();assert.equal(count,2,'Local-only never auto-starts SSO');
 settings={...settings,auth:{mode:'sso_only'}};
 await context.unroute('**/api/auth/**/authorize*');
 await context.route('**/api/auth/**/authorize*',route=>route.fulfill({json:{authorization_url:base+'/mock-idp'}}));
 await context.route('**/mock-idp',route=>route.fulfill({contentType:'text/html',body:'<h1>Microsoft sign-in (test identity provider)</h1>'}));
 await page.goto(base+path);await page.waitForURL('**/mock-idp');
 assert.equal(await page.evaluate(()=>sessionStorage.getItem('bow:postSignInRedirect')),'/d/example');
 await page.screenshot({path:dir+'sso-after.png'});
 console.log('PASS: app SSO handoff, single/multiple providers, hybrid/local-only, error/refresh and normal sign-in guards');
} finally {await context.close();await browser.close();}
