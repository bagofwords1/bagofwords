"""Hand-authored synthetic browser fixture, not a model-authoring evaluation."""

import json, uuid, httpx
from pathlib import Path

f = json.loads(Path("/tmp/artifact-fixture.json").read_text())
h = f["headers"]
c = httpx.Client(base_url="http://127.0.0.1:8108", timeout=60)
models = c.get("/api/llm/models", headers=h).json()
if not any(x["model_id"] == "artifact-local-fixture" for x in models):
    p = c.post(
        "/api/llm/providers",
        headers=h,
        json={
            "name": "Local QA provider",
            "provider_type": "openai",
            "credentials": {
                "api_key": "local-fixture-only",
                "base_url": "http://127.0.0.1:8118/v1",
            },
            "models": [
                {
                    "model_id": "artifact-local-fixture",
                    "name": "Local controlled stream",
                    "is_custom": True,
                }
            ],
        },
    )
    p.raise_for_status()
models = c.get("/api/llm/models", headers=h).json()
model = next(x for x in models if x["model_id"] == "artifact-local-fixture")
r = c.post(
    "/api/reports",
    headers=h,
    json={"title": "Document workspace", "files": [], "data_sources": []},
)
r.raise_for_status()
report = r.json()
code = """<script type="text/babel">
function App(){const [file,setFile]=useState(null),[ref,setRef]=useState(null),[progress,setProgress]=useState(0),[text,setText]=useState(''),[busy,setBusy]=useState(false),[done,setDone]=useState(false),[rows,setRows]=useState([]),[error,setError]=useState('');const abort=useRef(null);
 const load=()=>bow.records.collection('summaries').list().then(p=>setRows(p.items));useEffect(()=>{load().catch(e=>setError(e.message))},[]);
 async function upload(){setError('');try{setRef(await bow.files.upload('documents',file,{onProgress:p=>setProgress(Math.round(100*p.loaded/p.total))}))}catch(e){setError(e.message)}}
 async function analyze(){setError('');setText('');setDone(false);setBusy(true);abort.current=new AbortController();try{for await(const e of bow.ai.stream('summarize',{fileId:ref.id},{signal:abort.current.signal})){if(e.type==='text_delta')setText(t=>t+e.text);if(e.type==='completed')setDone(true)}}catch(e){setError(e.message)}finally{setBusy(false)}}
 async function save(){try{await bow.records.collection('summaries').create({summary:text,document:ref.id},{idempotencyKey:crypto.randomUUID()});await load()}catch(e){setError(e.message)}}
 return <main style={{maxWidth:900,margin:'auto',padding:40,fontFamily:'system-ui'}}><p style={{color:'#64748b',fontSize:12}}>LOCAL QA · CONTROLLED MODEL OUTPUT</p><h1 style={{fontSize:32,fontWeight:700}}>Document workspace</h1><p style={{margin:'16px 0'}}>Upload a document, review its summary, and save the result.</p><section style={{border:'1px solid #ddd',padding:20,borderRadius:12}}><input aria-label="Choose document" type="file" accept=".txt,.pdf" onChange={e=>setFile(e.target.files[0])}/><button onClick={upload} disabled={!file} style={{marginLeft:12}}>Upload</button>{progress>0&&<p>Upload: {progress}%</p>}{ref&&<p>Ready: {ref.name}</p>}<button onClick={analyze} disabled={!ref||busy} style={{marginTop:16,padding:10,background:'#2563eb',color:'white',borderRadius:8}}>Summarize</button>{busy&&<button onClick={()=>abort.current.abort()} style={{marginLeft:12}}>Cancel</button>}</section><section style={{marginTop:24,padding:20,border:'1px solid #ddd',borderRadius:12}}><h2>Summary</h2><p aria-live="polite" style={{margin:'12px 0',minHeight:40}}>{text||'Your summary will appear here.'}</p>{done&&<button onClick={save}>Save summary</button>}</section>{error&&<p role="alert">{error}</p>}<h2 style={{marginTop:24}}>Saved summaries</h2>{rows.map(row=><article key={row.id} style={{padding:16,marginTop:12,background:'#f1f5f9',borderRadius:8}}>{row.data.summary}</article>)}</main>}
ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
</script>"""
a = c.post(
    "/api/artifacts",
    headers=h,
    json={
        "report_id": report["id"],
        "title": "Document workspace",
        "content": {"code": code, "visualization_ids": [], "sdk_version": 1},
    },
)
a.raise_for_status()
artifact = a.json()
base = "/api/artifacts/" + artifact["artifact_id"] + "/runtime"
for d in [
    {"name": "documents", "kind": "files"},
    {
        "name": "summaries",
        "fields": {
            "summary": {"type": "string", "required": True},
            "document": {"type": "file"},
        },
    },
    {
        "name": "summarize",
        "kind": "ai",
        "prompt": "Summarize the document.",
        "model_id": model["id"],
        "file_resource": "documents",
    },
]:
    x = c.post(
        base + "/resources",
        headers=h,
        json={
            "action": "create",
            "definition": d,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    x.raise_for_status()
Path("/tmp/artifact-document-fixture.json").write_text(
    json.dumps({"artifact": artifact, "report": report})
)
print("Seeded document workspace using only a local controlled provider")
