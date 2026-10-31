import assert from 'node:assert/strict';
import {start, run} from './support/chatHarness.mjs';
const code=`<script>
function App(){
 var s=React.useState(false);
 return React.createElement('div',null, React.createElement('p',null,'Chat is ready'), React.createElement('button',{onClick:function(){s[1](!s[0]);}},'Toggle chart'),
 s[0] ? React.createElement(ChatResult,{result:{id:'sales',columns:[{field:'name',type:'string'},{field:'total',type:'number'}],rows:[{name:'A',total:3},{name:'B',total:7}],row_count:2,column_count:2,chart:{type:'bar',x:'name',y:['total']}}}) : null);
}
ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(App));
</script>`;
await run('artifactChatLazyLibraries',[{name:'chat paints without charts; later chart loads once and renders',fn:async env=>{
 const s=await start(env,{code,waitLoad:false});
 assert.equal(await s.frame.evaluate(()=>typeof window.echarts),'undefined');
 await s.frame.getByRole('button',{name:'Toggle chart'}).click();
 await s.frame.locator('canvas').waitFor();
 assert.equal(await s.frame.evaluate(()=>typeof window.echarts.init),'function');
 assert.equal(await s.frame.locator('script[src*="echarts-"]').count(),1);
 await s.frame.getByRole('button',{name:'Toggle chart'}).click();await s.frame.getByRole('button',{name:'Toggle chart'}).click();
 await s.frame.locator('canvas').waitFor();
 assert.equal(await s.frame.locator('script[src*="echarts-"]').count(),1);
 await s.done();
}}]);
