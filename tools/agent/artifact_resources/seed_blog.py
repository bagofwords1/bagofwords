"""Hand-authored synthetic browser fixture, not a model-authoring evaluation."""

import json, uuid, httpx
from pathlib import Path

h = json.loads(Path("/tmp/artifact-fixture.json").read_text())["headers"]
c = httpx.Client(base_url="http://127.0.0.1:8108", timeout=60)
r = c.post(
    "/api/reports",
    headers=h,
    json={"title": "Field notes", "files": [], "data_sources": []},
)
r.raise_for_status()
report = r.json()
code = """<script type="text/babel">
function App(){const [rows,setRows]=useState([]),[error,setError]=useState(''),[title,setTitle]=useState(''),[body,setBody]=useState(''),[ctx,setCtx]=useState(bow.context.get());const posts=bow.records.collection('posts');const load=()=>posts.list({limit:20}).then(p=>setRows(p.items));useEffect(()=>{load().catch(e=>setError(e.message));return bow.context.subscribe(setCtx)},[]);const edit=ctx.resources?.some(r=>r.name==='posts'&&r.operations.includes('create'));
async function save(){try{await posts.create({title,body},{idempotencyKey:crypto.randomUUID()});setTitle('');setBody('');await load()}catch(e){setError(e.message)}}
async function publish(row){try{await posts.update(row.id,{status:'published'},{expectedRevision:row.revision,idempotencyKey:crypto.randomUUID()});await load()}catch(e){setError(e.message)}}
return <main style={{maxWidth:880,margin:'auto',padding:48,fontFamily:'system-ui',color:'#1e293b'}}><header style={{borderBottom:'1px solid #cbd5e1',paddingBottom:28,marginBottom:28}}><p style={{fontSize:12,letterSpacing:2,color:'#64748b'}}>TEAM JOURNAL</p><h1 style={{fontSize:42,fontWeight:700,margin:'12px 0'}}>Field notes</h1><p>Updates, ideas, and things we learned along the way.</p></header>{edit&&<section style={{background:'#f1f5f9',padding:24,borderRadius:12,marginBottom:28}}><h2>Write a draft</h2><input aria-label="Post title" placeholder="Title" value={title} onChange={e=>setTitle(e.target.value)} style={{display:'block',padding:10,width:'100%',margin:'12px 0'}}/><textarea aria-label="Post body" placeholder="Your story" value={body} onChange={e=>setBody(e.target.value)} style={{width:'100%',padding:10}}/><button onClick={save} style={{marginTop:12}}>Save draft</button></section>}{error&&<p role="alert">{error}</p>}{rows.map(row=><article key={row.id} style={{marginBottom:24,paddingBottom:24,borderBottom:'1px solid #e2e8f0'}}><h2 style={{fontSize:24,fontWeight:600}}>{row.data.title}</h2><p style={{marginTop:12,whiteSpace:'pre-wrap'}}>{row.data.body}</p>{edit&&row.data.status==='draft'&&<button onClick={()=>publish(row)} style={{marginTop:12}}>Publish post</button>}</article>)}</main>}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script>"""
a = c.post(
    "/api/artifacts",
    headers=h,
    json={
        "report_id": report["id"],
        "title": "Field notes",
        "content": {"code": code, "visualization_ids": [], "sdk_version": 1},
    },
)
a.raise_for_status()
artifact = a.json()
base = "/api/artifacts/" + artifact["artifact_id"] + "/runtime"
d = {
    "name": "posts",
    "fields": {
        "title": {"type": "string", "required": True},
        "body": {"type": "string", "required": True},
        "status": {
            "type": "string",
            "default": "draft",
            "enum": ["draft", "published"],
            "indexed": True,
        },
        "internal_notes": {"type": "string"},
    },
    "permissions": {
        "read": {
            "any_of": [
                {"audience": "owner"},
                {
                    "audience": "public",
                    "equals": {"status": "published"},
                    "fields": ["title", "body", "status"],
                },
            ]
        }
    },
}
x = c.post(
    base + "/resources",
    headers=h,
    json={"action": "create", "definition": d, "idempotency_key": str(uuid.uuid4())},
)
x.raise_for_status()
for data in [
    {
        "title": "A better weekly rhythm",
        "body": "We are keeping our weekly planning simple: agree on the next useful outcome, choose an owner, and write down what changed.",
        "status": "published",
        "internal_notes": "Not visible to public readers",
    },
    {"title": "Next month’s experiment", "body": "This draft is still being reviewed."},
]:
    x = c.post(
        base + "/collections/posts/records",
        headers=h,
        json={"action": "create", "data": data, "idempotency_key": str(uuid.uuid4())},
    )
    x.raise_for_status()
x = c.put(
    "/api/reports/" + report["id"] + "/visibility/artifact",
    headers=h,
    json={"visibility": "public"},
)
x.raise_for_status()
Path("/tmp/artifact-blog-fixture.json").write_text(
    json.dumps({"report": report, "artifact": artifact})
)
print("Seeded synthetic public blog through existing sharing API")
