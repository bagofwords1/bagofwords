const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '../../..');
const {build} = require(root+'/frontend/node_modules/esbuild');
const {parse,compileScript,compileStyle} = require(root+'/frontend/node_modules/@vue/compiler-sfc');
const {chromium} = require(root+'/frontend/node_modules/playwright');
(async()=>{
 const styles=[];
 const bundle=await build({stdin:{contents:`import {createApp,h,ref} from 'vue'; import {createI18n} from 'vue-i18n'; import Ticker from './components/BlockGroupTicker.vue'; import BrowserTool from './components/tools/BrowserTool.vue'; import {computeBlockGroups} from './composables/useBlockGrouping.ts'; import en from '../locales/en.json'; import he from '../locales/he.json';
 const locale=window.qaLocale||'en';
 const make=(id,state,title)=>({id,status:'completed',tool_execution:{id,tool_name:'browser_act',status:'success',arguments_json:{_verification_group_id:id,title},result_json:{success:true,artifact:{version:2},action_id:id,evidence:{update_status:state,queries:[],errors:[]}}}});
 const rows=[make('wait','pending',locale==='he'?'בדיקת מסנני מכירות':'Checking sales filters'),make('ok','data_received',locale==='he'?'בדיקת איפוס מסננים':'Checking filter reset'),make('issue','data_not_acknowledged',locale==='he'?'בדיקת עדכון הנתונים':'Checking data delivery')];
 createApp({setup(){return()=>h('main',{style:'padding:36px;max-width:900px;margin:50px auto;background:white;border:1px solid #e5e7eb;border-radius:16px'},[h('h1',{style:'font-size:22px;font-weight:600;margin-bottom:8px'},locale==='he'?'בדיקת אפליקציית המכירות':'Sales app verification'),h('p',{style:'color:#64748b;font-size:14px;margin-bottom:28px'},locale==='he'?'תצוגת רכיבים עם נתוני בדיקה':'Component preview with seeded verification results'),...rows.map(b=>h('section',{style:'padding:20px 0;border-top:1px solid #e5e7eb'},[h(Ticker,{group:computeBlockGroups([b]).headerAt[b.id],expanded:true}),h('div',{style:'margin-top:12px'},[h(BrowserTool,{toolExecution:b.tool_execution})])]))])}}).use(createI18n({legacy:false,locale,messages:{en,he}})).component('Icon',{props:['name'],setup:p=>()=>h('span',{'data-icon':p.name,style:'display:inline-block'},p.name.includes('clock')?'◷':p.name.includes('chevron')?'⌄':'◎')}).component('Spinner',{render:()=>h('span',{},'◌')}).mount('#qa-root');`,resolveDir:root+'/frontend',loader:'ts'},bundle:true,write:false,format:'iife',plugins:[{name:'vue-fixture',setup(b){b.onLoad({filter:/\.vue$/},args=>{const source=fs.readFileSync(args.path,'utf8');const {descriptor}=parse(source);if(args.path.endsWith('Spinner.vue'))return {contents:"import {h} from 'vue';export default {render:()=>h('span',{},'◌')}",resolveDir:path.dirname(args.path)};const compiled=compileScript(descriptor,{id:'qa',inlineTemplate:true});styles.push(...descriptor.styles.map(s=>compileStyle({source:s.content,id:'qa'}).code));let content=compiled.content;if(!content.includes("from 'vue-i18n'"))content="import {useI18n} from 'vue-i18n';\n"+content;return {contents:content,loader:'ts',resolveDir:path.dirname(args.path)}})}}]});
 const browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1100,height:840}});
 await page.goto('http://localhost:3000/users/sign-in',{waitUntil:'networkidle',timeout:60000});
 await page.evaluate(()=>{document.body.innerHTML='<div id="qa-root"></div>';document.body.style.background='#f8fafc'});
 await page.addStyleTag({content:styles.join('\n')});
 for(const locale of ['en','he']){
 await page.evaluate(l=>{document.querySelector('#qa-root').innerHTML='';window.qaLocale=l;document.documentElement.dir=l==='he'?'rtl':'ltr'},locale);
 await page.addScriptTag({content:bundle.outputFiles[0].text});
 await page.waitForTimeout(700);
 console.log(locale,await page.locator('main').innerText());
 await page.screenshot({path:root+'/media/pr/internal-artifact-verification/review-'+process.argv[2]+'-'+locale+'.png'});
 }
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
