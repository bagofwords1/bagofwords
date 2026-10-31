// Live seeded stack: BOW_CHAT_URL, optional BOW_CHAT_EMAIL/PASSWORD.
// Run after tools/agent/seed_org.py and creating/publishing the feedback-loop app.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import {chromium} from 'playwright';
const url=process.env.BOW_CHAT_URL;
if(!url) throw new Error('Set BOW_CHAT_URL to a seeded published app');
const dir=new URL('../../../media/pr/embedded-chat-reliability/',import.meta.url).pathname;
fs.mkdirSync(dir,{recursive:true});
const browser=await chromium.launch({headless:true,executablePath:process.env.PW_CHROMIUM_PATH||undefined});
const context=await browser.newContext({viewport:{width:1280,height:800},recordVideo:process.env.CHAT_VIDEO?{dir:dir+'video',size:{width:1280,height:800}}:undefined});
const page=await context.newPage();
page.setDefaultTimeout(15000);
const errors=[];page.on('pageerror',e=>errors.push(String(e)));
try{
 await page.goto(url);
 const chat=page.frameLocator('iframe');
 if(process.env.CHAT_BASELINE) await chat.getByRole('heading',{name:'Support',exact:true}).waitFor();
 if(process.env.CHAT_BASELINE){await page.screenshot({path:dir+'page-before.png'});console.log('Captured baseline');}
 else{
  await page.getByTestId('app-sign-in-gate').waitFor();
  await page.getByRole('heading',{name:'Support',exact:true}).waitFor();
  assert.equal(await page.locator('iframe').count(),0);
  assert.equal(await page.getByTestId('app-menu').count(),0);
  const signIn=page.getByRole('link',{name:'Sign in',exact:true});
  await page.screenshot({path:dir+'gate-after.png'});
  await signIn.click();
  await page.waitForURL('**/users/sign-in?*');
  assert.equal(new URL(page.url()).searchParams.get('redirect'),new URL(url).pathname);
  if(process.env.BOW_CHAT_EMAIL){
   await page.getByLabel('Email',{exact:true}).fill(process.env.BOW_CHAT_EMAIL);
   await page.getByLabel('Password',{exact:true}).fill(process.env.BOW_CHAT_PASSWORD);
   await page.getByRole('button',{name:'Sign in',exact:true}).click();
   await page.waitForURL(url);
   await page.getByTestId('app-menu').waitFor();await page.getByTestId('app-menu').click();
   await page.getByRole('menuitem',{name:'Edit in my conversation'}).waitFor();
   await page.screenshot({path:dir+'menu-owner.png'});
  }
  const server=http.createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end(`<html><body style="margin:0;font-family:system-ui;background:#f1f5f9"><h2 style="padding:20px">ERP customer portal</h2><button onclick="document.querySelector('iframe').hidden=!document.querySelector('iframe').hidden" style="position:fixed;bottom:12px;right:20px">Support</button><iframe hidden title="Support app" src="${url}?embed=1" style="width:450px;height:600px;float:right;margin:20px;border:1px solid #ddd;border-radius:12px"></iframe></body></html>`)});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try{
   await page.goto(`http://127.0.0.1:${server.address().port}`);
   await page.getByRole('button',{name:'Support',exact:true}).click();
   const embedded=page.frameLocator('iframe[title="Support app"]');
   await embedded.frameLocator('iframe').getByRole('heading',{name:'Support',exact:true}).waitFor();
   assert.equal(await embedded.getByTestId('app-menu').count(),0);
   await page.screenshot({path:dir+'embedded.png'});
   const anonymous=await browser.newContext({viewport:{width:1280,height:800}});
   anonymous.setDefaultTimeout(15000);
   try {
    const guest=await anonymous.newPage();await guest.goto(`http://127.0.0.1:${server.address().port}`);
    await guest.getByRole('button',{name:'Support',exact:true}).click();
    const host=guest.frameLocator('iframe[title="Support app"]');
    await host.getByTestId('app-sign-in-gate').waitFor();
    assert.equal(await host.locator('iframe').count(),0);
    await guest.screenshot({path:dir+'gate-embedded.png'});
    const link=host.getByRole('link',{name:'Sign in',exact:true});await link.waitFor();
    assert.equal(await link.getAttribute('target'),'_blank');
    assert.equal(new URL(await link.getAttribute('href'),url).searchParams.get('redirect'),new URL(url).pathname);
    await guest.goto(url);
    await guest.evaluate(()=>localStorage.setItem('bow.locale','he'));await guest.reload();
    await guest.getByTestId('app-sign-in-gate').waitFor();
    assert.equal(await guest.locator('html').getAttribute('dir'),'rtl');
    await guest.screenshot({path:dir+'gate-he.png'});
    // A public read-only artifact still opens anonymously: replace only its
    // content response, leaving the real public metadata/access path intact.
    await guest.route('**/api/d/*/content',async route=>{
     const response=await route.fetch();const body=await response.json();
     body.content.code='<script type="text/babel">function App(){return <h1>Public overview</h1>} ReactDOM.createRoot(document.getElementById("root")).render(<App/>);</script>';
     body.compiled_js=null;body.compiled_hash=null;
     await route.fulfill({response,json:body});
    });
    await guest.reload();
    await guest.frameLocator('iframe').getByRole('heading',{name:'Public overview'}).waitFor({timeout:15000});
    assert.equal(await guest.getByTestId('app-sign-in-gate').count(),0);
    if(process.env.BOW_CHAT_EMAIL){
     await guest.unroute('**/api/d/*/content');
     await guest.evaluate(()=>localStorage.removeItem('bow.locale'));
     await guest.goto(`http://127.0.0.1:${server.address().port}`);
     await guest.getByRole('button',{name:'Support',exact:true}).click();
     const embeddedGate=guest.frameLocator('iframe[title="Support app"]');
     const popupPromise=guest.waitForEvent('popup');
     await embeddedGate.getByRole('link',{name:'Sign in',exact:true}).click();
     const popup=await popupPromise;
     await popup.getByLabel('Email',{exact:true}).fill(process.env.BOW_CHAT_EMAIL);
     await popup.getByLabel('Password',{exact:true}).fill(process.env.BOW_CHAT_PASSWORD);
     await popup.getByRole('button',{name:'Sign in',exact:true}).click();
     await popup.waitForURL(url);await popup.close();
     // Browser focus delivery differs headless; exercise the real focus handler.
     const appFrame=guest.frames().find(frame=>frame.url().includes('/d/'));
     await appFrame.evaluate(()=>window.dispatchEvent(new Event('focus')));
     await embeddedGate.frameLocator('iframe').getByRole('heading',{name:'Support',exact:true}).waitFor({timeout:15000});
     assert.equal(await embeddedGate.getByTestId('app-sign-in-gate').count(),0);
    }


   } finally {await anonymous.close();}

  }finally{await new Promise(resolve=>server.close(resolve));}
  await page.goto(url);
  await page.evaluate(()=>localStorage.setItem('bow.locale','he'));await page.reload();
  await page.getByTestId('app-menu').waitFor();await page.getByTestId('app-menu').click();
  await page.getByRole('menu').waitFor();assert.equal(await page.locator('html').getAttribute('dir'),'rtl');
  await page.screenshot({path:dir+'menu-he.png'});
  assert.deepEqual(errors,[]);console.log('PASS: published page, menu permissions, sign-in return, embed and RTL');
 }
}finally{await context.close();await browser.close();}
