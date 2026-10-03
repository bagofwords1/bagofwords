"""Hand-authored synthetic browser fixture, not a model-authoring evaluation."""

import json, httpx
from pathlib import Path

h = json.loads(Path("/tmp/artifact-fixture.json").read_text())["headers"]
c = httpx.Client(base_url="http://127.0.0.1:8108", timeout=60)
r = c.post(
    "/api/reports",
    headers=h,
    json={"title": "Weekly project overview", "files": [], "data_sources": []},
)
r.raise_for_status()
report = r.json()
code = """<script type="text/babel">
function App(){const [week,setWeek]=useState('This week');const data=useArtifactData();const items=week==='This week'?[['Design',72],['Engineering',86],['Research',58]]:[['Design',63],['Engineering',70],['Research',49]];return <main style={{maxWidth:900,padding:48,margin:'auto',fontFamily:'system-ui'}}><p style={{fontSize:12,color:'#64748b',letterSpacing:2}}>PROJECT OVERVIEW</p><h1 style={{fontSize:36,fontWeight:700,margin:'12px 0'}}>Weekly progress</h1><p style={{color:'#64748b',marginBottom:28}}>A saved artifact using the existing React globals.</p><select aria-label="Week" value={week} onChange={e=>setWeek(e.target.value)} style={{padding:10,border:'1px solid #ccc',borderRadius:8}}><option>This week</option><option>Last week</option></select><section style={{marginTop:28,padding:24,border:'1px solid #e2e8f0',borderRadius:12}}>{items.map(([name,value])=><div key={name} style={{margin:'20px 0'}}><div style={{display:'flex',justifyContent:'space-between',marginBottom:10}}><span>{name}</span><span>{value}%</span></div><div style={{background:'#e2e8f0',borderRadius:8}}><div style={{height:14,width:value+'%',background:'#2563eb',borderRadius:8}}/></div></div>)}</section></main>}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script>"""
a = c.post(
    "/api/artifacts",
    headers=h,
    json={
        "report_id": report["id"],
        "title": "Weekly project overview",
        "content": {"code": code, "visualization_ids": []},
    },
)
a.raise_for_status()
Path("/tmp/artifact-legacy-fixture.json").write_text(
    json.dumps({"report": report, "artifact": a.json()})
)
print("Seeded legacy-format artifact without resource or SDK metadata")
