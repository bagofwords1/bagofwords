/* Artifact SDK v1. No credentials, network endpoints or execution persistence. */
// Existing generated apps use randomUUID for mutation keys. Keep that contract
// in insecure self-hosted contexts without weakening randomness.
if (typeof crypto !== 'undefined' && !crypto.randomUUID) {
  crypto.randomUUID = function () {
    var bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    var h = Array.from(bytes, function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    return h.slice(0,8)+'-'+h.slice(8,12)+'-'+h.slice(12,16)+'-'+h.slice(16,20)+'-'+h.slice(20);
  };
}

(function () {
  'use strict';
  var pending = new Map(), port = null, sequence = 0;
  var contextValue = null, contextListeners = [];
  var connectedResolve;
  var connected = new Promise(function (resolve) { connectedResolve = resolve; });
  function sdkError(value) { var e = new Error(value.message || 'Artifact request failed'); e.code = value.code || 'UNAVAILABLE'; return e; }
  window.addEventListener('message', function (event) {
    if (event.source !== window.parent || !event.data || event.data.type !== 'BOW_RUNTIME_PORT' ||
        event.data.nonce !== window.__BOW_RUNTIME_NONCE__ || !event.ports[0] || port) return;
    port = event.ports[0];
    port.onmessage = function (event) {
      var message = event.data || {};
      if(message.context){contextValue=message.context;contextListeners.slice().forEach(function(fn){fn(contextValue)});return;}
      var task = pending.get(message.id);
      if (!task) return;
      if (message.event) { if (task.onEvent) task.onEvent(message.event); return; }
      pending.delete(message.id); task.cleanup();
      if (message.error) task.reject(sdkError(message.error)); else task.resolve(message.result);
    };
    port.start(); connectedResolve();
  });
  if (window.__BOW_RUNTIME_NONCE__) window.parent.postMessage({type: 'BOW_RUNTIME_READY', nonce: window.__BOW_RUNTIME_NONCE__}, '*');
  var fixtureRows = new Map(), fixtureFiles = new Map(), fixtureKeys = new Map();
  function fixtureDefinitions() { return (window.ARTIFACT_DATA || {})._fixture_resources || []; }
  function fixtureData(resource, value, previous) {
    var definition=fixtureDefinitions().find(function(d){return d.name===resource;});
    var data=Object.assign({},previous || {},value);
    if(!definition) return data;
    var fields=definition.fields || {};
    Object.keys(data).forEach(function(k){if(!fields[k])throw sdkError({code:'VALIDATION',message:'Unknown field: '+k});});
    Object.keys(fields).forEach(function(k){
      var f=fields[k];if(data[k]===undefined && f.default!=null)data[k]=f.default;
      var v=data[k];if(v==null){if(f.required)throw sdkError({code:'VALIDATION',message:'Required field: '+k});return;}
      var expected=f.type==='file'?'string':f.type;
      if(typeof v!==expected || (expected==='number' && !Number.isFinite(v)) ||
         (expected==='string' && f.max_length && v.length>f.max_length) ||
         (f.enum && !f.enum.includes(v)))throw sdkError({code:'VALIDATION',message:'Invalid field: '+k});
    });
    return data;
  }
  function fixtureRequest(method, args, options) {
    // Explicit verifier mode only. State dies with the frame; never contacts APIs.
    if(method === 'records') {
      var rows = fixtureRows.get(args.resource) || new Map();fixtureRows.set(args.resource,rows);
      if(args.action === 'list') return {items:Array.from(rows.values()).filter(function(r){return Object.keys(args.filter||{}).every(function(k){return r.data[k]===args.filter[k]})}).slice(0,args.limit||50),nextCursor:null};
      if(args.idempotency_key && fixtureKeys.has(args.idempotency_key)) return fixtureKeys.get(args.idempotency_key);
      var row=rows.get(args.id), result;
      if(args.action !== 'create' && !row) throw sdkError({code:'NOT_FOUND',message:'Fixture record not found'});
      if(args.action === 'get') return row;
      if(row && args.expected_revision !== row.revision) throw sdkError({code:'CONFLICT',message:'Fixture record changed'});
      if(args.action === 'delete'){rows.delete(args.id);result={id:args.id,deleted:true};}
      else {
        var id=row?row.id:'fixture-'+String(++sequence), now=new Date().toISOString();
        row={id:id,data:fixtureData(args.resource,args.data,row?row.data:null),revision:row?row.revision+1:1,createdAt:row?row.createdAt:now,updatedAt:now};
        rows.set(id,row);result={id:id,revision:row.revision};
      }
      fixtureKeys.set(args.idempotency_key,result);return result;
    }
    if(method === 'upload') {var id='fixture-file-'+String(++sequence), meta={id:id,resourceId:args.resource,name:args.file.name,mediaType:args.file.type,size:args.file.size,status:'ready'};fixtureFiles.set(id,{meta:meta,blob:args.file});if(options.onEvent)options.onEvent({loaded:args.file.size,total:args.file.size});return meta;}
    if(method.indexOf('file')===0){var file=fixtureFiles.get(args.id);if(!file)throw sdkError({code:'NOT_FOUND',message:'Fixture file not found'});if(method==='fileGet')return file.meta;if(method==='fileDownload')return file.blob;fixtureFiles.delete(args.id);return {id:args.id,deleted:true};}
    if(method==='conversations'||method==='conversationSend')return fixtureConversation(method,args,options);
    if(method==='ai'){if(options.onEvent){options.onEvent({type:'text_delta',text:'Preview output. '});options.onEvent({type:'text_delta',text:'No model was called.'});options.onEvent({type:'completed',output:'Preview output. No model was called.'});}return null;}
    throw sdkError({code:'UNAVAILABLE',message:'Unsupported fixture operation'});
  }
  // Verifier mode: conversations live in the frame and answer with a fixed text.
  var fixtureThreads = new Map();
  function fixtureConversation(method, args, options) {
    var now = new Date().toISOString(), thread = args.id != null ? fixtureThreads.get(args.id) : null;
    if (method === 'conversationSend') {
      if (!thread) throw sdkError({code:'NOT_FOUND',message:'Conversation not found'});
      thread.messages.push({id:'fixture-msg-'+String(++sequence),role:'user',text:String(args.text||''),status:'success',created_at:now});
      var reply = 'Preview answer. No agent was called.';
      if (options.onEvent) { options.onEvent({type:'started',message_id:'fixture-msg-'+String(++sequence)}); options.onEvent({type:'text_delta',block:'b1',text:reply}); options.onEvent({type:'completed',status:'success'}); }
      thread.messages.push({id:'fixture-msg-'+String(++sequence),role:'assistant',text:reply,status:'success',tools:[],created_at:now});
      return null;
    }
    switch (args.op) {
      case 'policy': return {available:true,agents_mode:'auto',agents:[],model:null};
      case 'list': return {items:Array.from(fixtureThreads.values()).reverse().map(function(t){return {id:t.id,title:t.title,created_at:t.created_at,updated_at:t.created_at};}),next_before:null};
      case 'create': var id='fixture-conv-'+String(++sequence); fixtureThreads.set(id,{id:id,title:args.title||'Preview conversation',created_at:now,agents:args.agents||[],messages:[]}); return {id:id,title:fixtureThreads.get(id).title,created_at:now,updated_at:now,agents:args.agents||[]};
    }
    if (!thread) throw sdkError({code:'NOT_FOUND',message:'Conversation not found'});
    if (args.op === 'get') return {id:thread.id,title:thread.title,created_at:thread.created_at,updated_at:thread.created_at,agents:thread.agents};
    if (args.op === 'rename') { thread.title = String(args.title||thread.title); return {id:thread.id,title:thread.title,created_at:thread.created_at,updated_at:now,agents:thread.agents}; }
    if (args.op === 'archive') { fixtureThreads.delete(args.id); return null; }
    if (args.op === 'messages') return {items:thread.messages.slice(),next_before:null};
    if (args.op === 'stop') return {id:args.messageId,status:'stopped'};
    throw sdkError({code:'UNAVAILABLE',message:'Unsupported fixture operation'});
  }
  function request(method, args, options) {
    options = options || {};
    if(window.__BOW_FIXTURE_MODE__) return Promise.resolve().then(function(){if(options.signal && options.signal.aborted)throw sdkError({code:'ABORTED',message:'Cancelled'});return fixtureRequest(method,args,options);});
    if (!window.__BOW_RUNTIME_NONCE__) {
      // Offline/verification: reads are empty fixtures; live effects unavailable.
      if (method === 'records' && args.action === 'list') return Promise.resolve({items: [], nextCursor: null});
      return Promise.reject(sdkError({code: 'UNAVAILABLE', message: 'Live resources are unavailable in this preview'}));
    }
    return new Promise(function(resolve, reject) {
      var id = String(++sequence), finished = false;
      // An agent's answer can take minutes; everything else is quick.
      var timeout = setTimeout(function () { abort('INTERRUPTED'); }, options.timeoutMs || 130000);
      function cleanup() { finished = true; clearTimeout(timeout); if (options.signal) options.signal.removeEventListener('abort', cancel); }
      function abort(code) { if (finished) return; pending.delete(id); if (port) port.postMessage({id: id, cancel: true}); cleanup(); reject(sdkError({code: code, message: 'Request interrupted'})); }
      function cancel() { abort('ABORTED'); }
      if (options.signal && options.signal.aborted) { cancel(); return; }
      if (options.signal) options.signal.addEventListener('abort', cancel, {once: true});
      pending.set(id, {resolve: resolve, reject: reject, cleanup: cleanup, onEvent: options.onEvent});
      connected.then(function () { if (!finished) port.postMessage({id: id, method: method, args: args}); });
    });
  }
  function recordRequest(resource, action, values, options) {
    options = options || {};
    var args = Object.assign({resource: resource, action: action}, values || {});
    if (options.expectedRevision != null) args.expected_revision = options.expectedRevision;
    if (options.idempotencyKey) args.idempotency_key = options.idempotencyKey;
    if (['create','update','delete'].indexOf(action) >= 0 && !args.idempotency_key)
      return Promise.reject(sdkError({code:'VALIDATION',message:'Supply a stable idempotencyKey for this mutation'}));
    return request('records', args, options);
  }
  var data = {
    getSnapshot: function () { return window.ARTIFACT_DATA; },
    subscribe: function (fn) { window.__artifactDataListeners.push(fn); return function () { var i = window.__artifactDataListeners.indexOf(fn); if (i >= 0) window.__artifactDataListeners.splice(i, 1); }; },
    vizById: window.vizById, useArtifactData: window.useArtifactData,
    useParams: window.useParams, useParamOptions: window.useParamOptions, useFilters: window.useFilters
  };
  async function* streamOf(method, args, opts, timeoutMs) {
    var queue = [], wake, done = false, error = null;
    var controller = new AbortController();
    function cancel(){controller.abort();}
    if(opts && opts.signal) { if(opts.signal.aborted) cancel(); else opts.signal.addEventListener('abort',cancel,{once:true}); }
    var result = request(method,args,{signal:controller.signal,timeoutMs:timeoutMs,onEvent:function(e){queue.push(e);if(wake)wake();}})
      .then(function(){done=true;if(wake)wake();},function(e){error=e;done=true;if(wake)wake();});
    try { while (!done || queue.length) { if (queue.length) yield queue.shift(); else await new Promise(function(resolve){wake=resolve;}); } if(error)throw error; }
    finally { controller.abort();if(opts && opts.signal)opts.signal.removeEventListener('abort',cancel); await result; }
  }
  // Agent answers are Markdown. Rendered to React elements only (never HTML
  // strings), so text from the model can't inject markup: headings, paragraphs,
  // lists, tables, fenced code, **bold**, *italic*, `code` and http(s) links.
  function markdown(text) {
    var h = window.React && window.React.createElement;
    if (!h) return String(text == null ? '' : text);
    if (typeof document !== 'undefined' && !document.getElementById('bow-md-style')) {
      var style = document.createElement('style'); style.id = 'bow-md-style';
      style.textContent = '.bow-md>*+*{margin-top:.6em}.bow-md-table{border-collapse:collapse;font-size:.92em}' +
        '.bow-md-table th,.bow-md-table td{border:1px solid rgba(127,127,127,.3);padding:.25em .6em;text-align:start}' +
        '.bow-md-table th{font-weight:600}.bow-md-list{padding-inline-start:1.3em;list-style:disc}ol.bow-md-list{list-style:decimal}' +
        '.bow-md-code{font-family:ui-monospace,monospace;font-size:.9em;background:rgba(127,127,127,.12);border-radius:3px;padding:0 .25em}' +
        '.bow-md-pre{overflow-x:auto;background:rgba(127,127,127,.1);border-radius:6px;padding:.6em;font-size:.88em}.bow-md-h{font-weight:600}' +
        '.bow-md a{text-decoration:underline}';
      document.head.appendChild(style);
    }
    var key = 0;
    function inline(src) {
      var out = [], re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|\[[^\]]+\]\(https?:\/\/[^\s)]+\))/g, last = 0, m;
      while ((m = re.exec(src))) {
        if (m.index > last) out.push(src.slice(last, m.index));
        var t = m[0];
        if (t[0] === '`') out.push(h('code', {key: key++, className: 'bow-md-code'}, t.slice(1, -1)));
        else if (t.slice(0, 2) === '**') out.push(h('strong', {key: key++}, t.slice(2, -2)));
        else if (t[0] === '*') out.push(h('em', {key: key++}, t.slice(1, -1)));
        else { var mm = /^\[([^\]]+)\]\((.+)\)$/.exec(t); out.push(h('a', {key: key++, href: mm[2], target: '_blank', rel: 'noopener noreferrer'}, mm[1])); }
        last = m.index + t.length;
      }
      if (last < src.length) out.push(src.slice(last));
      return out;
    }
    var lines = String(text == null ? '' : text).replace(/\r/g, '').split('\n'), blocks = [], i = 0;
    var cells = function (row) { return row.trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); }); };
    while (i < lines.length) {
      var line = lines[i];
      if (/^```/.test(line)) { var code = []; i++; while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]); i++;
        blocks.push(h('pre', {key: key++, className: 'bow-md-pre'}, h('code', null, code.join('\n')))); continue; }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
        var head = cells(line), rows = []; i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
        blocks.push(h('table', {key: key++, className: 'bow-md-table'},
          h('thead', null, h('tr', null, head.map(function (c, j) { return h('th', {key: j}, inline(c)); }))),
          h('tbody', null, rows.map(function (r, k) { return h('tr', {key: k}, r.map(function (c, j) { return h('td', {key: j}, inline(c)); })); }))));
        continue;
      }
      var hm = /^(#{1,4})\s+(.*)$/.exec(line);
      if (hm) { blocks.push(h('h' + Math.min(6, hm[1].length + 2), {key: key++, className: 'bow-md-h'}, inline(hm[2]))); i++; continue; }
      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        var ordered = /^\s*\d+\./.test(line), items = [];
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ''));
        blocks.push(h(ordered ? 'ol' : 'ul', {key: key++, className: 'bow-md-list'}, items.map(function (t, j) { return h('li', {key: j}, inline(t)); })));
        continue;
      }
      if (!line.trim()) { i++; continue; }
      var para = [];
      while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*]|\d+\.)\s+|\s*\|)/.test(lines[i])) para.push(lines[i++]);
      if (!para.length) para.push(lines[i++]);
      blocks.push(h('p', {key: key++, className: 'bow-md-p'}, inline(para.join(' '))));
    }
    return h('div', {className: 'bow-md'}, blocks);
  }
  window.bow = {
    version: 1, data: data, markdown: markdown,
    context: { get: function () { return contextValue || {viewer: (window.ARTIFACT_DATA || {}).current_user || null, mode: window.__BOW_RUNTIME_NONCE__ ? 'live' : 'preview', resources: window.__BOW_FIXTURE_MODE__ ? fixtureDefinitions().map(function(d){return {name:d.name,kind:d.kind,operations:['read','create','update','delete']};}) : []}; }, subscribe: function(fn){contextListeners.push(fn);return function(){var i=contextListeners.indexOf(fn);if(i>=0)contextListeners.splice(i,1);};} },
    records: {collection: function (resource) { return {
      list: function (opts) {
        opts = opts || {};
        var limit = opts.limit === undefined ? 50 : opts.limit;
        var order = opts.orderBy === undefined ? '-created_at' : opts.orderBy;
        if (!Number.isInteger(limit) || limit < 1 || limit > 100)
          return Promise.reject(sdkError({code:'VALIDATION',message:'Record list limit must be an integer between 1 and 100. Use nextCursor to load more records.'}));
        if (['id','-id','created_at','-created_at'].indexOf(order) === -1)
          return Promise.reject(sdkError({code:'VALIDATION',message:'Record list orderBy must be id, -id, created_at or -created_at.'}));
        return recordRequest(resource,'list',{filter:opts.filter || {},limit:limit,cursor:opts.cursor || null,order_by:order},opts);
      },
      get: function (id, opts) { return recordRequest(resource,'get',{id:id},opts); },
      create: function (value, opts) { return recordRequest(resource,'create',{data:value},opts); },
      update: function (id, value, opts) { return recordRequest(resource,'update',{id:id,data:value},opts); },
      delete: function (id, opts) { return recordRequest(resource,'delete',{id:id},opts); }
    }; }},
    files: {
      upload: function (resource, file, opts) { opts = opts || {}; return request('upload',{resource:resource,file:file},Object.assign({},opts,{onEvent:function(e){if(opts.onProgress)opts.onProgress(e);}})); },
      get: function (id, opts) { return request('fileGet',{id:id},opts); },
      download: function (id, opts) { return request('fileDownload',{id:id},opts); },
      delete: function (id, opts) { return request('fileDelete',{id:id},opts); }
    },
    // The viewer's own conversations with this artifact, answered by the agents
    // its chat settings allow. send() yields {type:'started'|'tool'|'text_delta'|
    // 'text'|'completed'} events; messages() returns the stored thread.
    conversations: {
      policy: function () { return request('conversations',{op:'policy'}); },
      list: function (opts) { opts = opts || {}; return request('conversations',{op:'list',limit:opts.limit,before:opts.before},opts); },
      create: function (opts) { opts = opts || {}; return request('conversations',{op:'create',title:opts.title,agents:opts.agents},opts); },
      get: function (id, opts) { return request('conversations',{op:'get',id:id},opts); },
      rename: function (id, title, opts) { return request('conversations',{op:'rename',id:id,title:title},opts); },
      archive: function (id, opts) { return request('conversations',{op:'archive',id:id},opts); },
      messages: function (id, opts) { opts = opts || {}; return request('conversations',{op:'messages',id:id,limit:opts.limit,before:opts.before},opts); },
      stop: function (id, messageId, opts) { return request('conversations',{op:'stop',id:id,messageId:messageId},opts); },
      send: function (id, text, opts) { return streamOf('conversationSend',{id:id,text:text},opts,600000); }
    },
    ai: { stream: function (operation, input, opts) { return streamOf('ai',{operation:operation,input:input},opts); } }
  };
})();
