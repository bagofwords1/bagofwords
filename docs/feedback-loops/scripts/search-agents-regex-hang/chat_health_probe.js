// Sends PROMPT through the real chat UI while polling /health every 500ms;
// prints the worst health latency observed during the run.
// Usage: PROMPT='...' node chat_health_probe.js <tag>   (needs state.json from a logged-in session)
const {chromium}=require('playwright');const fs=require('fs');const tag=process.argv[2]||'run';
const prompt=process.env.PROMPT;
let probes=[],stop=false;
async function poll(){while(!stop){const t=Date.now();let code=0;try{const r=await fetch('http://localhost:8000/health',{signal:AbortSignal.timeout(120000)});code=r.status}catch(e){code=-1}probes.push({t,ms:Date.now()-t,code});await new Promise(r=>setTimeout(r,500));}}
(async()=>{const b=await chromium.launch({executablePath:'/opt/pw-browsers/chromium',args:['--headless=new']});
const ctx=await b.newContext({storageState:'state.json'});const p=await ctx.newPage();
const log=fs.createWriteStream(`${tag}_api.jsonl`);p.on('response',r=>{if(r.url().includes('/api/'))log.write(JSON.stringify({u:r.url(),s:r.status()})+'\n')});
await p.goto('http://localhost:3000/');const ed=p.locator('[contenteditable="true"]').first();await ed.waitFor({timeout:180000});
await p.waitForTimeout(3000);await p.screenshot({path:`${tag}_1.png`});
await ed.click();await p.keyboard.type(prompt);await p.waitForTimeout(800);
await p.screenshot({path:`${tag}_typed.png`});const pr=poll();
await p.locator('button[class*="rounded-full"]').last().click();
await p.waitForURL(/reports\//,{timeout:20000}).catch(async e=>{await p.screenshot({path:`${tag}_fail.png`});throw e});console.log('report',p.url());
const t0=Date.now();
while(Date.now()-t0<400000){await p.waitForTimeout(3000);const busy=await p.locator('[data-testid="stop-button"]').count();if(!busy&&Date.now()-t0>15000)break;}
stop=true;await pr;await p.screenshot({path:`${tag}_2.png`,fullPage:true});
const worst=probes.reduce((a,b)=>b.ms>a.ms?b:a,{ms:0});
console.log(JSON.stringify({probes:probes.length,worst_ms:worst.ms,failed:probes.filter(x=>x.code!==200).length,over1s:probes.filter(x=>x.ms>1000).length,run_s:(Date.now()-t0)/1000}));
fs.writeFileSync(`${tag}_probes.json`,JSON.stringify(probes));await b.close();})();
