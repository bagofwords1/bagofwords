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
    if(method==='ai'){if(options.onEvent){options.onEvent({type:'text_delta',text:'Preview output. '});options.onEvent({type:'text_delta',text:'No model was called.'});options.onEvent({type:'completed',output:'Preview output. No model was called.'});}return null;}
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
      var timeout = setTimeout(function () { abort('INTERRUPTED'); }, 130000);
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
  window.bow = {
    version: 1, data: data,
    context: { get: function () { return contextValue || {viewer: (window.ARTIFACT_DATA || {}).current_user || null, mode: window.__BOW_RUNTIME_NONCE__ ? 'live' : 'preview', resources: window.__BOW_FIXTURE_MODE__ ? fixtureDefinitions().map(function(d){return {name:d.name,kind:d.kind,operations:['read','create','update','delete']};}) : []}; }, subscribe: function(fn){contextListeners.push(fn);return function(){var i=contextListeners.indexOf(fn);if(i>=0)contextListeners.splice(i,1);};} },
    records: {collection: function (resource) { return {
      list: function (opts) { opts = opts || {}; return recordRequest(resource,'list',{filter:opts.filter || {},limit:opts.limit || 50,cursor:opts.cursor || null,order_by:opts.orderBy || '-created_at'},opts); },
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
    ai: { stream: async function* (operation, input, opts) {
      var queue = [], wake, done = false, error = null;
      var controller = new AbortController();
      function cancel(){controller.abort();}
      if(opts && opts.signal) { if(opts.signal.aborted) cancel(); else opts.signal.addEventListener('abort',cancel,{once:true}); }
      var result = request('ai',{operation:operation,input:input},{signal:controller.signal,onEvent:function(e){queue.push(e);if(wake)wake();}})
        .then(function(){done=true;if(wake)wake();},function(e){error=e;done=true;if(wake)wake();});
      try { while (!done || queue.length) { if (queue.length) yield queue.shift(); else await new Promise(function(resolve){wake=resolve;}); } if(error)throw error; }
      finally { controller.abort();if(opts && opts.signal)opts.signal.removeEventListener('abort',cancel); await result; }
    }}
  };
})();
