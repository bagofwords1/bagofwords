/** Trusted transport: fixed operation mapping, per-document channel, no tokens in frames.
 * `recordViews: false` keeps the page's resources working but counts no view
 * (a dashboard's draft preview: editors at work are not readers). */
type RuntimeContext = {
  frames: (HTMLIFrameElement | null)[], artifactId?: string, readOnly?: boolean, recordViews?: boolean,
  /** The editor's own session: what happens in the app is logged for the
   *  agent (services/artifact_activity.py). Never for viewers or embeds. */
  activity?: { reportId?: string | null, version?: number | null } | null,
  /** BOW's own preview: every activity event, for the preview's evidence. */
  onActivity?: (event: ActivityEvent) => void,
}
export type ActivityEvent = { kind: string, op?: string, status?: string, error_code?: string, duration_ms?: number, detail?: Record<string, any> }

/** The verb an SDK call is logged under (`records.update`, `conversations.send`…). */
function operationName(method: string, a: any): string {
  if (method === 'records') return `records.${a.action}`
  if (method === 'upload') return 'files.upload'
  if (method === 'fileGet' || method === 'fileDownload' || method === 'fileDelete') return `files.${method.slice(4).toLowerCase()}`
  if (method === 'ai') return `ai.${a.operation}`
  if (method === 'conversations') return `conversations.${a.op}`
  if (method === 'conversationSend') return 'conversations.send'
  return String(method).slice(0, 40)
}
/** What a request was about, never the values it carried. */
function operationDetail(method: string, a: any): Record<string, any> {
  const detail: Record<string, any> = {}
  if (typeof a?.resource === 'string') detail.resource = a.resource
  if (method === 'conversations' && a.op === 'create' && Array.isArray(a.agents)) detail.agents = a.agents.slice(0, 10)
  return detail
}

/** Batches activity to the editor-only endpoint: every few seconds, at once on
 *  a failure, and on page hide. Stops for good on 401/403/404 (not an editor). */
function activityLog(getContext: () => RuntimeContext, headers: () => Record<string, string>) {
  const sessionId = (crypto.randomUUID?.() || String(Math.random()).slice(2) + Date.now()).replace(/[^A-Za-z0-9_-]/g, '')
  let queue: any[] = [], seq = 0, timer: ReturnType<typeof setTimeout> | null = null, disabled = false
  let windowStart = Date.now(), inWindow = 0
  const flush = (keepalive = false) => {
    if (timer) { clearTimeout(timer); timer = null }
    const ctx = getContext()
    if (disabled || !queue.length || !ctx.artifactId || !ctx.activity) { queue = []; return }
    const events = queue.splice(0, 50)
    fetch(`/api/artifacts/${encodeURIComponent(ctx.artifactId)}/runtime/activity`, {
      method: 'POST', keepalive, credentials: 'omit',
      headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, report_id: ctx.activity.reportId || null, artifact_version: ctx.activity.version ?? null, events }),
    }).then(r => { if ([401, 403, 404].includes(r.status)) disabled = true }).catch(() => {})
    if (queue.length) timer = setTimeout(() => flush(), 1000)
  }
  const log = (event: ActivityEvent) => {
    const ctx = getContext()
    ctx.onActivity?.(event)
    if (disabled || !ctx.activity || !ctx.artifactId) return
    if (Date.now() - windowStart > 60000) { windowStart = Date.now(); inWindow = 0 }
    if (++inWindow > 200) return  // a runaway app does not flood the log
    queue.push({ ...event, seq: ++seq })
    const failed = event.status === 'error' || event.status === 'interrupted' || event.kind === 'error' || event.kind === 'console'
    if (failed) flush()
    else if (!timer) timer = setTimeout(() => flush(), 4000)
  }
  const onHide = () => flush(true)
  return { log, flush, onHide }
}

export function useArtifactRuntime(getContext: () => RuntimeContext) {
  const token = useApiAuthorization()
  const { getErrorMessage } = useErrorMessage()
  const countedViews = new Set<string>()
  const sessions = new Map<Window, { nonce: string, port: MessagePort, controllers: Set<AbortController>, cleanups: (()=>void)[] }>()
  const dispose = () => { for (const value of sessions.values()) { value.cleanups.forEach(fn=>fn()); value.port.close(); value.controllers.forEach(c => c.abort()) } sessions.clear() }
  const headers = () => ({ ...(token.value ? { Authorization: token.value } : {}) })
  const activity = activityLog(getContext, headers)
  const listener = (event: MessageEvent) => {
    const context = getContext()
    if (event.data?.type !== 'BOW_RUNTIME_READY' || !context.artifactId) return
    const frame = context.frames.find(f => f?.contentWindow === event.source)
    if (!frame || !(event.source as Window)) return
    const match = frame.srcdoc.match(/window\.__BOW_RUNTIME_NONCE__="([a-f0-9-]+)"/)
    if (!match || event.data.nonce !== match[1]) return
    const source = event.source as Window
    const old = sessions.get(source)
    if (old?.nonce === match[1]) return
    if (old) { old.cleanups.forEach(fn=>fn()); old.port.close(); old.controllers.forEach(c => c.abort()) }
    const channel = new MessageChannel(), controllers = new Set<AbortController>()
    const entry = { nonce: match[1], port: channel.port1, controllers, cleanups: [] as (()=>void)[] }
    sessions.set(source, entry)
    const base = `/api/artifacts/${encodeURIComponent(context.artifactId)}/runtime`
    const active = new Map<string, AbortController>()
    channel.port1.onmessage = async ({ data: message }) => {
      if (message && message.activity && typeof message.activity === 'object') {
        // From the app itself (clicks, errors, preview calls): untrusted, the server bounds it.
        const e = message.activity
        if (['click', 'error', 'console', 'request', 'stream'].includes(e.kind)) activity.log({
          kind: e.kind, op: typeof e.op === 'string' ? e.op.slice(0, 64) : undefined,
          status: typeof e.status === 'string' ? e.status : undefined,
          error_code: typeof e.error_code === 'string' ? e.error_code.slice(0, 80) : undefined,
          duration_ms: typeof e.duration_ms === 'number' ? e.duration_ms : undefined,
          detail: e.detail && typeof e.detail === 'object' ? e.detail : undefined,
        })
        return
      }
      if (!message || typeof message.id !== 'string' || message.id.length > 50) return
      if (message.cancel) { active.get(message.id)?.abort(); return }
      const now = getContext()
      if (now.artifactId !== context.artifactId || !now.frames.includes(frame) || !frame.srcdoc.includes(`window.__BOW_RUNTIME_NONCE__="${entry.nonce}"`)) { dispose(); return }
      const send = (value: any) => channel.port1.postMessage({id:message.id,...value})
      if(active.size >= 12 || active.has(message.id)) { send({error:{code:'RATE_LIMITED',message:'Too many requests'}}); return }
      const controller = new AbortController(); active.set(message.id,controller); controllers.add(controller)
      const started = performance.now(), opName = operationName(message.method, message.args || {})
      const streaming = message.method === 'ai' || message.method === 'conversationSend'
      const opDetail = operationDetail(message.method, message.args || {})
      try {
        const a = message.args || {}, method = message.method
        if (now.readOnly) throw Object.assign(new Error('Resources are unavailable in preview or view-as'),{code:'FORBIDDEN'})
        const segment = (value: unknown) => { if(typeof value !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(value)) throw new Error('Invalid resource'); return encodeURIComponent(value) }
        let path: string, body: any, verb = 'POST'
        if(method === 'records') { const {resource,...payload}=a; path=`/collections/${segment(resource)}/records`; body=JSON.stringify(payload) }
        else if(method === 'upload') {
          if (!(a.file instanceof Blob) || a.file.size > 10*1024*1024) throw new Error('Invalid file')
          const form=new FormData();form.append('file',a.file,(a.file as File).name || 'file.txt');
          path=`/files/${segment(a.resource)}/upload`;body=form
          send({event:{loaded:0,total:a.file.size}})
        } else if(method === 'fileGet' || method === 'fileDownload' || method === 'fileDelete') {
          path=`/files/${segment(a.id)}${method === 'fileDownload' ? '/content' : ''}`;verb=method === 'fileDelete' ? 'DELETE' : 'GET'
        } else if(method === 'ai') { path=`/ai/${segment(a.operation)}/stream`; body=JSON.stringify(a.input) }
        else if(method === 'conversations') {
          // bow.conversations: the viewer's own conversations with this artifact.
          const query = (values: Record<string, unknown>) => {
            const q = new URLSearchParams()
            for (const [k, v] of Object.entries(values)) if (v != null) q.set(k, String(v).slice(0, 128))
            const text = q.toString(); return text ? `?${text}` : ''
          }
          const conv = a.id == null ? '' : `/${segment(a.id)}`
          switch (a.op) {
            case 'policy': path='/conversations/policy'; verb='GET'; break
            case 'list': path=`/conversations${query({limit:a.limit,before:a.before})}`; verb='GET'; break
            case 'create': path='/conversations'; body=JSON.stringify({title:a.title ?? null,agents:a.agents ?? null}); break
            case 'get': path=`/conversations${conv}`; verb='GET'; break
            case 'rename': path=`/conversations${conv}`; verb='PATCH'; body=JSON.stringify({title:a.title}); break
            case 'archive': path=`/conversations${conv}`; verb='DELETE'; break
            case 'messages': path=`/conversations${conv}/messages${query({limit:a.limit,before:a.before})}`; verb='GET'; break
            case 'stop': path=`/conversations${conv}/messages/${segment(a.messageId)}/stop`; break
            default: throw new Error('Unsupported operation')
          }
          if (!conv && !['policy','list','create'].includes(a.op)) throw new Error('Invalid conversation')
        } else if(method === 'conversationSend') { path=`/conversations/${segment(a.id)}/messages`; body=JSON.stringify({text:String(a.text || '')}) }
        else throw new Error('Unsupported operation')
        const response = method === 'upload' ? await new Promise<Response>((resolve,reject)=>{
          const xhr = new XMLHttpRequest()
          const abort = ()=>xhr.abort()
          xhr.open('POST',base+path)
          for(const [key,value] of Object.entries(headers())) xhr.setRequestHeader(key,value)
          xhr.upload.onprogress = e=>send({event:{loaded:Math.min(a.file.size,e.loaded),total:a.file.size}})
          xhr.onload = ()=>resolve(new Response(xhr.responseText,{status:xhr.status,headers:{'Content-Type':'application/json'}}))
          xhr.onerror = ()=>reject(new Error('Upload interrupted'))
          xhr.onabort = ()=>reject(new DOMException('Upload cancelled','AbortError'))
          xhr.onloadend = ()=>controller.signal.removeEventListener('abort',abort)
          controller.signal.addEventListener('abort',abort,{once:true})
          if(controller.signal.aborted) {reject(new DOMException('Upload cancelled','AbortError'));return}
          xhr.send(body)
        }) : await fetch(base+path,{method:verb,headers:{...headers(),...(typeof body==='string'?{'Content-Type':'application/json'}:{})},body,signal:controller.signal,credentials:'omit'})
        if(!response.ok) { const detail=await response.json().catch(()=>({})); throw Object.assign(new Error(getErrorMessage({data:detail})),{code:(detail.error_code || (response.status === 422 ? 'VALIDATION' : 'UNAVAILABLE')).replace('ARTIFACT_RESOURCE_','')}) }
        if(method === 'ai' || method === 'conversationSend') {
          const reader=response.body!.getReader(), decoder=new TextDecoder();let buffer='',completed=false
          try { while(true) { const {value,done}=await reader.read(); if(done)break;buffer+=decoder.decode(value,{stream:true});if(buffer.length>1048576)throw new Error('Stream limit exceeded');let cut;
            while((cut=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,cut);buffer=buffer.slice(cut+1);if(!line)continue;const e=JSON.parse(line);if(e.type==='error')throw Object.assign(new Error(e.message),{code:e.code});if(completed)throw new Error('Invalid stream');if(e.type==='completed')completed=true;send({event:e});}
          } if(!completed)throw Object.assign(new Error('Stream interrupted'),{code:'INTERRUPTED'});send({result:null})
            activity.log({kind:'stream',op:opName,status:'completed',duration_ms:Math.round(performance.now()-started),detail:opDetail}) }
          finally { await reader.cancel().catch(()=>{});reader.releaseLock() }
        } else { const result=method==='fileDownload'?await response.blob():response.status===204?null:await response.json();if(method==='upload')send({event:{loaded:a.file.size,total:a.file.size}});send({result})
          activity.log({kind:'request',op:opName,status:'ok',duration_ms:Math.round(performance.now()-started),detail:{...opDetail,http_status:response.status}}) }
      } catch(error:any) {
        const code = error.name==='AbortError'?'ABORTED':error.code || 'UNAVAILABLE'
        send({error:{code,message:error.message || 'Request failed'}})
        activity.log({kind:streaming?'stream':'request',op:opName,status:code==='ABORTED'?'aborted':code==='INTERRUPTED'?'interrupted':'error',
          error_code:code,duration_ms:Math.round(performance.now()-started),detail:{...opDetail,message:String(error.message || '').slice(0,300)}})
      }
      finally { active.delete(message.id);controllers.delete(controller) }
    }
    if (!context.readOnly) fetch(base+'/context',{headers:headers(),credentials:'omit'}).then(r=>r.ok?r.json():null).then(value=>{if(value)channel.port1.postMessage({context:value})}).catch(()=>{})
    channel.port1.start();source.postMessage({type:'BOW_RUNTIME_PORT',nonce:match[1]},'*',[channel.port2])
    if (!context.readOnly && context.recordViews !== false) {
      let initialized = false
      const observer = new IntersectionObserver(async entries => {
        if (!initialized || !entries.some(e => e.isIntersecting)) return
        observer.disconnect()
        if(countedViews.has(context.artifactId!)) return
        countedViews.add(context.artifactId!)
        try {
          const surface = /^\/(r|d)\//.test(window.location.pathname) ? 'standalone' : 'embedded'
          const response = await fetch(base + '/view-token?surface=' + surface, {headers:headers(),credentials:'omit'})
          if (!response.ok) return
          const view = await response.json()
          await fetch(base + '/views', {method:'POST',headers:{...headers(),'Content-Type':'application/json'},body:JSON.stringify(view),credentials:'omit'})
        } catch { /* Analytics never blocks the artifact. */ }
      })
      const rendered = (e:MessageEvent) => {
        if(e.source !== source || e.data?.type !== 'ARTIFACT_READY') return
        initialized = true;observer.observe(frame);window.removeEventListener('message',rendered)
      }
      window.addEventListener('message',rendered)
      const timer = window.setTimeout(() => {observer.disconnect();window.removeEventListener('message',rendered)}, 300000)
      entry.cleanups.push(()=>{clearTimeout(timer);observer.disconnect();window.removeEventListener('message',rendered)})
    }
  }
  onMounted(()=>{window.addEventListener('message',listener);window.addEventListener('pagehide',activity.onHide)})
  onUnmounted(()=>{window.removeEventListener('message',listener);window.removeEventListener('pagehide',activity.onHide);activity.flush(true);dispose()})
  watch(token, dispose)
  return { dispose, log: activity.log }
}
