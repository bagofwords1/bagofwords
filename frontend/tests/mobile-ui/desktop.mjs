import { chromium } from '@playwright/test';
const EXE='/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const ROOT='tests/mobile-ui/output';
const RID='5b2019eb-6094-41c4-96f6-95e20c46aa59';
const b=await chromium.launch({headless:true,executablePath:EXE});
async function shot(path,file,auth,wait){
  const ctx=await b.newContext({viewport:{width:1280,height:800},...(auth?{storageState:`${ROOT}/state.json`}:{})});
  const p=await ctx.newPage();
  await p.goto('http://localhost:3000'+path,{waitUntil:'commit',timeout:90000});
  await p.waitForTimeout(wait);
  const info=await p.evaluate(()=>({ hamburger:!!document.querySelector('button[aria-label="Open menu"]'), sidebar:!!document.getElementById('separator-sidebar'), sidebarVisible: (()=>{const s=document.getElementById('separator-sidebar'); if(!s) return false; const r=s.getBoundingClientRect(); return r.x>=0 && r.width>0;})() }));
  console.log('DESKTOP',path,JSON.stringify(info));
  await p.screenshot({path:`${ROOT}/desktop-${file}.png`});
  await ctx.close();
}
await shot('/dashboards','dashboards',true,3000);
await shot('/r/'+RID,'artifact',false,5000);
await b.close();
