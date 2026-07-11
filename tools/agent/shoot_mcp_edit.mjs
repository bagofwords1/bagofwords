import { chromium } from '@playwright/test';
const BASE='http://localhost:3000';
const CHROME='/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const b=await chromium.launch({executablePath:CHROME});
const p=await (await b.newContext({viewport:{width:1360,height:1040}})).newPage();
p.setDefaultTimeout(60000); p.setDefaultNavigationTimeout(60000);
await p.goto(`${BASE}/users/sign-in`,{waitUntil:'domcontentloaded'});
await p.locator('input[type="text"]').first().fill('admin@example.com');
await p.locator('input[type="password"]').first().fill('Password123!');
await p.locator('button[type="submit"]').first().click();
await p.waitForTimeout(3000);
if(p.url().includes('/onboarding')){ await p.getByRole('button',{name:/skip onboarding/i}).first().click().catch(()=>{}); await p.waitForTimeout(3000); }
await p.goto(`${BASE}/agents`,{waitUntil:'domcontentloaded'});
await p.waitForTimeout(4500);
await p.getByText(/view all/i).nth(1).click({force:true});
await p.waitForTimeout(1500);
// open the connection detail
await p.getByText('X (edit test)', {exact:true}).first().click();
await p.waitForTimeout(2000);
// click Edit
await p.getByRole('button',{name:/^edit$/i}).first().click();
await p.waitForTimeout(2500);
await p.screenshot({path:'../media/pr/mcp-x-edit-filled.png'});
console.log('shot: mcp-x-edit-filled.png');
await b.close();
