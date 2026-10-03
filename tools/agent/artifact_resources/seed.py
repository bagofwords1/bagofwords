"""Seed only synthetic local evidence data through the real artifact APIs."""

import json, uuid
from pathlib import Path
import httpx

seed = json.loads(Path("/tmp/artifact-seed.json").read_text())
admin = seed["admin"]
org = seed["organization"]
client = httpx.Client(base_url="http://127.0.0.1:8108", timeout=60)
login = client.post(
    "/api/auth/jwt/login",
    data={"username": "artifact-author@example.com", "password": "Password123!"},
)
login.raise_for_status()
user = client.get(
    "/api/users/whoami",
    headers={"Authorization": "Bearer " + login.json()["access_token"]},
).json()
headers = {
    "Authorization": "Bearer " + login.json()["access_token"],
    "X-Organization-Id": user["organizations"][0]["id"],
}
r = client.post(
    "/api/reports",
    headers=headers,
    json={"title": "Team notebook", "files": [], "data_sources": []},
)
r.raise_for_status()
report = r.json()
code = """<script type="text/babel">
function App(){
 const [rows,setRows]=useState([]),[text,setText]=useState(''),[error,setError]=useState('');
 const load=()=>bow.records.collection('notes').list({limit:20}).then(p=>setRows(p.items)).catch(e=>setError(e.message));
 useEffect(()=>{load()},[]);
 async function save(){try{await bow.records.collection('notes').create({title:text},{idempotencyKey:crypto.randomUUID()});setText('');await load()}catch(e){setError(e.message)}}
 return <main style={{padding:40,maxWidth:900,margin:'auto'}}><p style={{color:'#64748b'}}>TEAM WORKSPACE</p><h1 style={{fontSize:36,fontWeight:700}}>Team notebook</h1><p style={{margin:'16px 0'}}>Capture ideas and keep the team up to date.</p><div style={{display:'flex',gap:10}}><input aria-label="Note title" value={text} onChange={e=>setText(e.target.value)} style={{border:'1px solid #cbd5e1',padding:10,borderRadius:8,flex:1}}/><button onClick={save} style={{background:'#2563eb',color:'white',padding:'10px 18px',borderRadius:8}}>Add note</button></div>{error&&<p role="alert">{error}</p>}<div style={{marginTop:24}}>{rows.map(r=><article key={r.id} style={{padding:20,border:'1px solid #e2e8f0',borderRadius:12,marginBottom:12}}><h2>{r.data.title}</h2></article>)}</div></main>
}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script>"""
a = client.post(
    "/api/artifacts",
    headers=headers,
    json={
        "report_id": report["id"],
        "title": "Team notebook",
        "content": {"code": code, "visualization_ids": [], "sdk_version": 1},
    },
)
a.raise_for_status()
artifact = a.json()
base = "/api/artifacts/" + artifact["artifact_id"] + "/runtime"
c = client.post(
    base + "/resources",
    headers=headers,
    json={
        "action": "create",
        "idempotency_key": str(uuid.uuid4()),
        "definition": {
            "name": "notes",
            "fields": {"title": {"type": "string", "required": True, "indexed": True}},
            "permissions": {},
        },
    },
)
c.raise_for_status()
for title in [
    "Launch checklist",
    "Interview feedback",
    "Ideas for next week",
    "Documentation improvements",
]:
    q = client.post(
        base + "/collections/notes/records",
        headers=headers,
        json={
            "action": "create",
            "data": {"title": title},
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    q.raise_for_status()
for surface in ["embedded", "embedded", "standalone"]:
    tok = client.get(base + "/view-token", headers=headers, params={"surface": surface})
    tok.raise_for_status()
    q = client.post(base + "/views", headers=headers, json=tok.json())
    q.raise_for_status()
Path("/tmp/artifact-fixture.json").write_text(
    json.dumps({"artifact": artifact, "report": report, "headers": headers})
)
print("Seeded synthetic notebook and analytics fixture")
