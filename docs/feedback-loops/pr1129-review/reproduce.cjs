const { chromium } = require('/Users/yochze/Desktop/bagofwords/frontend/node_modules/playwright');
const fs = require('fs');
const cp = require('child_process');
const repo = '/private/tmp/bow-pr1129';
const libs = '/Users/yochze/Desktop/bagofwords/frontend/public/libs';
const old = cp.execFileSync('git', ['show', 'HEAD~0:frontend/public/libs/artifact-globals.js'], {cwd: repo, encoding:'utf8'});
const base = cp.execFileSync('git', ['show', 'origin/main:frontend/public/libs/artifact-globals.js'], {cwd: repo, encoding:'utf8'});
(async () => {
 const browser = await chromium.launch({headless:true});
 const out = [];
 for (const mode of ['v11-data-before', 'v11-data-after', 'legacy-base', 'legacy-pr']) {
  const page = await browser.newPage({viewport:{width:800,height:450}});
  const errors=[]; page.on('pageerror', e=>errors.push(e.message));
  await page.setContent('<!doctype html><html><head></head><body><div id="root"></div></body></html>');
  for (const file of ['tailwindcss-3.4.16.js','react-18.development.js','react-dom-18.development.js','echarts-5.min.js']) await page.addScriptTag({path: `${libs}/${file}`});
  if (mode !== 'legacy-base') await page.addScriptTag({path:`${repo}/frontend/public/libs/artifact-tailwind.js`});
  await page.evaluate(()=>{window.__BOW_INFO=false;});
  if(mode==='v11-data-before') await page.evaluate(()=>{window.ARTIFACT_DATA={runtime:{version:11},visualizations:[]};});
  await page.addScriptTag({content: mode === 'legacy-base' ? base : old});
  if(mode==='v11-data-after') await page.evaluate(()=>{window.ARTIFACT_DATA={runtime:{version:11},visualizations:[]};});
  await page.evaluate((mode)=>{
   if(mode.startsWith('v11')) setTheme('nocturne');
   ReactDOM.createRoot(document.getElementById('root')).render(React.createElement('div',{},[
    React.createElement('h1',{key:'h',className:'font-bold'},'Revenue report'),
    React.createElement(KPICard,{key:'k', title:'Revenue', value:fmt(0.42,{pct:true}), delta:0.2, deltaPct:true, deltaLabel:'vs last month', variant:'accent', spark:[1,3,2]}),
    React.createElement(EChart,{key:'e',height:200, option:{xAxis:{type:'category',data:['Jan','Feb']},yAxis:{type:'value'},series:[{type:'line',data:[1,4]}]}})
   ]));
  },mode);
  await page.waitForTimeout(500);
  out.push(await page.evaluate(({mode,errors})=>{
    const chart=echarts.getInstanceByDom(document.querySelector('[_echarts_instance_]'));
    const opt=chart.getOption();
    return {mode, legacy:window.__bowLegacyRuntime, text:document.getElementById('root').innerText, h1Weight:getComputedStyle(document.querySelector('h1')).fontWeight, chart:{smooth:opt.series[0].smooth,showSymbol:opt.series[0].showSymbol,grid:opt.grid[0]},errors};
  },{mode,errors}));
  await page.screenshot({path:`/private/tmp/pr1129-${mode}.png`});
  await page.close();
 }
 console.log(JSON.stringify(out,null,2));
 fs.writeFileSync('/private/tmp/pr1129-results.json',JSON.stringify(out,null,2));
 await browser.close();
})();
