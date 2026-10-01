/** Trusted transport: fixed operation mapping, per-document channel, no tokens in frames. */
export function useArtifactRuntime(getContext: () => { frames: (HTMLIFrameElement | null)[], artifactId?: string, readOnly?: boolean }) {
  const { token } = useAuth()
  const { getErrorMessage } = useErrorMessage()
  const countedViews = new Set<string>()
  const sessions = new Map<Window, { nonce: string, port: MessagePort, controllers: Set<AbortController>, cleanups: (()=>void)[] }>()
  const dispose = () => { for (const value of sessions.values()) { value.cleanups.forEach(fn=>fn()); value.port.close(); value.controllers.forEach(c => c.abort()) } sessions.clear() }
  const headers = () => ({ ...(token.value ? { Authorization: token.value } : {}) })
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
      if (!message || typeof message.id !== 'string' || message.id.length > 50) return
      if (message.cancel) { active.get(message.id)?.abort(); return }
      const now = getContext()
      if (now.artifactId !== context.artifactId || !now.frames.includes(frame) || !frame.srcdoc.includes(`window.__BOW_RUNTIME_NONCE__="${entry.nonce}"`)) { dispose(); return }
      const send = (value: any) => channel.port1.postMessage({id:message.id,...value})
      if(active.size >= 12 || active.has(message.id)) { send({error:{code:'RATE_LIMITED',message:'Too many requests'}}); return }
      const controller = new AbortController(); active.set(message.id,controller); controllers.add(controller)
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
        if(method === 'ai') {
          const reader=response.body!.getReader(), decoder=new TextDecoder();let buffer='',completed=false
          try { while(true) { const {value,done}=await reader.read(); if(done)break;buffer+=decoder.decode(value,{stream:true});if(buffer.length>1048576)throw new Error('Stream limit exceeded');let cut;
            while((cut=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,cut);buffer=buffer.slice(cut+1);if(!line)continue;const e=JSON.parse(line);if(e.type==='error')throw Object.assign(new Error(e.message),{code:e.code});if(completed)throw new Error('Invalid stream');if(e.type==='completed')completed=true;send({event:e});}
          } if(!completed)throw Object.assign(new Error('Stream interrupted'),{code:'INTERRUPTED'});send({result:null}) }
          finally { await reader.cancel().catch(()=>{});reader.releaseLock() }
        } else { const result=method==='fileDownload'?await response.blob():await response.json();if(method==='upload')send({event:{loaded:a.file.size,total:a.file.size}});send({result}) }
      } catch(error:any) { send({error:{code:error.name==='AbortError'?'ABORTED':error.code || 'UNAVAILABLE',message:error.message || 'Request failed'}}) }
      finally { active.delete(message.id);controllers.delete(controller) }
    }
    fetch(base+'/context',{headers:headers(),credentials:'omit'}).then(r=>r.ok?r.json():null).then(value=>{if(value)channel.port1.postMessage({context:value})}).catch(()=>{})
    channel.port1.start();source.postMessage({type:'BOW_RUNTIME_PORT',nonce:match[1]},'*',[channel.port2])
    if (!context.readOnly) {
      let initialized = false
      const observer = new IntersectionObserver(async entries => {
        if (!initialized || !entries.some(e => e.isIntersecting)) return
        observer.disconnect()
        if(countedViews.has(context.artifactId!)) return
        countedViews.add(context.artifactId!)
        try {
          const surface = window.location.pathname.startsWith('/r/') ? 'standalone' : 'embedded'
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
  onMounted(()=>window.addEventListener('message',listener))
  onUnmounted(()=>{window.removeEventListener('message',listener);dispose()})
  watch(token, dispose)
  return { dispose }
}
