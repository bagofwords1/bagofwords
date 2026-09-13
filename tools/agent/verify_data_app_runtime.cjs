// Runtime contracts exercised in Chromium. --baseline proves regressions on PR HEAD.
const fs=require('fs'),path=require('path'),cp=require('child_process'),assert=require('assert/strict');
const root=path.resolve(__dirname,'../..');const {chromium}=require(path.join(root,'frontend/node_modules/playwright'));
const baseline=process.argv.includes('--baseline');const source=baseline?cp.execFileSync('git',['show','3e32c09db2e88815bd2b9910db84ddd71d145d7f:frontend/public/libs/artifact-globals.js'],{cwd:root,encoding:'utf8'}):fs.readFileSync(path.join(root,'frontend/public/libs/artifact-globals.js'),'utf8');
(async()=>{const browser=await chromium.launch();const failures=[];const results=[];
for(const {version,dark} of [0,11].flatMap(version => [false,true].map(dark => ({version,dark})))){
 const page=await browser.newPage({viewport:{width:960,height:700}});
 await page.setContent('<html class="'+(dark?'dark':'')+'"><head></head><body><div id="root"></div></body></html>');
 for(const f of ['tailwindcss-3.4.16.js','react-18.production.min.js','react-dom-18.production.min.js','echarts-5.min.js','artifact-tailwind.js'])await page.addScriptTag({path:path.join(root,'frontend/public/libs',f)});
 await page.evaluate(v=>{window.ARTIFACT_DATA={runtime:{version:v},visualizations:[],params:{declarations:[{name:'region',type:'string',query_ids:['q1','q2']}],values:{region:null},ack:0}};window.__BOW_INFO=false},version);
 await page.addScriptTag({content:source});
 await page.evaluate(()=>ReactDOM.createRoot(document.getElementById('root')).render(React.createElement('main',null,React.createElement('h1',{className:'font-bold'},'Workspace'),React.createElement(EChart,{option:{xAxis:{type:'category',data:['A','B']},yAxis:{type:'value'},series:[{type:'line',data:[3,9]}]}}))));
 await page.waitForFunction(()=>document.querySelector('[_echarts_instance_]'));
 const result=await page.evaluate(()=>{
  const store=window.__paramStore;
  store.setParam('region','Europe',{apply:false});store.apply();store._status({loading:true});
  store._ingest({params:{values:{region:null},ack:0}});
  const loadingDuringUpdate=store.isLoading();const optimisticValue=store.getValues().region;
  store._status({loading:false,error:'Query rejected the selected range'});
  store._ingest({params:{values:{region:'Europe'},ack:1}});
  const errorAfterData=store.getError();
  const option=echarts.getInstanceByDom(document.querySelector('[_echarts_instance_]')).getOption();
  return {legacy:window.__bowLegacyRuntime,loadingDuringUpdate,optimisticValue,errorAfterData,percentage:fmt(.37,{pct:true}),headingWeight:getComputedStyle(document.querySelector('h1')).fontWeight,chartHeight:document.querySelector('[_echarts_instance_]').clientHeight,smooth:option.series[0].smooth,gridLeft:option.grid[0].left};
 });
 for(const [label,test]of Object.entries({pendingRemainsVisible:()=>assert.equal(result.loadingDuringUpdate,true),optimisticValue:()=>assert.equal(result.optimisticValue,'Europe'),queryErrorRemainsVisible:()=>assert.ok(result.errorAfterData),customHeading:()=>assert.equal(result.headingWeight,'700'),legacyChart:()=>{if(!version){assert.equal(result.chartHeight,400);assert.equal(result.smooth,true);assert.equal(result.gridLeft,40);assert.equal(result.percentage,'0.4%')}}})){
  try{test();results.push({version,dark,label,status:'PASS'})}catch(e){failures.push({version,dark,label,actual:result,message:e.message})}
 }
 await page.close();
}
await browser.close();console.log(JSON.stringify({baseline,results,failures},null,2));if(failures.length)process.exitCode=1;
})().catch(e=>{console.error(e);process.exitCode=1});
