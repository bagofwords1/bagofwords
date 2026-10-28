/*!
 * BOW embedded chat SDK, v1.
 *
 * Served by your own BOW instance at /api/embed/sdk/v1/bow-embed.js — no CDN,
 * no third-party calls. It renders an <iframe> of the BOW chat document and
 * speaks a small postMessage protocol with it; it is a UI shim, not an
 * authorization boundary (the BOW backend decides everything).
 *
 *   <script src="https://bow.internal.example/api/embed/sdk/v1/bow-embed.js"></script>
 *   <script>
 *     const assistant = BOWEmbed.mount({
 *       baseUrl: 'https://bow.internal.example',
 *       embedId: 'emb_…',
 *       mode: 'floating',                 // or 'inline' with `container`
 *       getAssertion: () => fetch('/internal/bow/assertion', { method: 'POST' })
 *                             .then(r => r.json()).then(b => b.assertion),
 *       context:  { recordType: 'incident', recordId: 'INC-1042' },
 *       metadata: { workflow_id: 'incident-review' },
 *     })
 *     assistant.on('ready', () => {})
 *     assistant.updateContext({ recordId: 'INC-1043' })   // replaces, {} clears
 *     assistant.logout()                                   // on host sign-out / user switch
 *     assistant.destroy()
 *   </script>
 *
 * Security notes for integrators:
 *   - `getAssertion` must call YOUR backend, which verifies its own session and
 *     signs a short-lived RS256 JWT with the embed's private key. The key never
 *     belongs in the browser.
 *   - The SDK only accepts messages from the iframe it created, from the BOW
 *     origin, carrying the nonce it generated.
 */
(function (root) {
  'use strict';

  var PROTOCOL = 'bow:';
  var CLOSED_SIZE = { width: 72, height: 72 };
  var OPEN_SIZE = { width: 392, height: 640 };
  var MOBILE_BREAKPOINT = 520;

  function randomNonce() {
    var bytes = new Uint8Array(16);
    if (root.crypto && root.crypto.getRandomValues) root.crypto.getRandomValues(bytes);
    else for (var i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    var out = '';
    for (var j = 0; j < bytes.length; j++) out += ('0' + bytes[j].toString(16)).slice(-2);
    return out;
  }

  function originOf(url) {
    var a = document.createElement('a');
    a.href = url;
    return a.protocol + '//' + a.host;
  }

  function plain(obj) {
    if (!obj || typeof obj !== 'object') return null;
    try { return JSON.parse(JSON.stringify(obj)); } catch (e) { return null; }
  }

  function mount(options) {
    options = options || {};
    if (!options.baseUrl) throw new Error('BOWEmbed.mount: baseUrl is required');
    if (!options.embedId) throw new Error('BOWEmbed.mount: embedId is required');
    var baseUrl = String(options.baseUrl).replace(/\/+$/, '');
    var bowOrigin = originOf(baseUrl);
    var mode = options.mode === 'inline' ? 'inline' : 'floating';
    var nonce = randomNonce();
    var listeners = {};
    var state = { open: !!options.open, auth: 'idle', ready: false, destroyed: false };
    var context = plain(options.context);
    var metadata = plain(options.metadata);
    var position = options.position === 'bottom-left' ? 'bottom-left' : 'bottom-right';

    // ── DOM ──────────────────────────────────────────────────────────────
    var wrapper = document.createElement('div');
    wrapper.setAttribute('data-bow-embed', options.embedId);
    var iframe = document.createElement('iframe');
    iframe.title = options.title || 'Assistant';
    iframe.setAttribute('allow', 'clipboard-write');
    iframe.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
    iframe.style.border = '0';
    iframe.style.background = 'transparent';
    iframe.style.colorScheme = 'normal';
    iframe.src = baseUrl + '/embed/' + encodeURIComponent(options.embedId) + '?mode=' + mode;

    if (mode === 'inline') {
      var container = typeof options.container === 'string' ? document.querySelector(options.container) : options.container;
      if (!container) throw new Error('BOWEmbed.mount: inline mode needs a `container` element or selector');
      wrapper.style.width = '100%';
      wrapper.style.height = '100%';
      iframe.style.width = '100%';
      iframe.style.height = '100%';
      container.appendChild(wrapper);
    } else {
      wrapper.style.position = 'fixed';
      wrapper.style.bottom = (options.offset && options.offset.bottom) || '16px';
      wrapper.style[position === 'bottom-left' ? 'left' : 'right'] = (options.offset && options.offset.side) || '16px';
      wrapper.style.zIndex = String(options.zIndex || 2147483000);
      wrapper.style.transition = 'width 160ms ease, height 160ms ease';
      document.body.appendChild(wrapper);
    }
    wrapper.appendChild(iframe);
    applySize();

    function applySize() {
      if (mode !== 'floating') return;
      var mobile = root.innerWidth < MOBILE_BREAKPOINT;
      if (state.open) {
        if (mobile) {
          wrapper.style.left = '0'; wrapper.style.right = '0'; wrapper.style.bottom = '0';
          wrapper.style.width = '100vw'; wrapper.style.height = '100dvh';
        } else {
          wrapper.style.left = position === 'bottom-left' ? ((options.offset && options.offset.side) || '16px') : '';
          wrapper.style.right = position === 'bottom-right' ? ((options.offset && options.offset.side) || '16px') : '';
          wrapper.style.bottom = (options.offset && options.offset.bottom) || '16px';
          wrapper.style.width = Math.min(OPEN_SIZE.width, root.innerWidth - 32) + 'px';
          wrapper.style.height = Math.min(OPEN_SIZE.height, root.innerHeight - 32) + 'px';
        }
      } else {
        wrapper.style.left = position === 'bottom-left' ? ((options.offset && options.offset.side) || '16px') : '';
        wrapper.style.right = position === 'bottom-right' ? ((options.offset && options.offset.side) || '16px') : '';
        wrapper.style.bottom = (options.offset && options.offset.bottom) || '16px';
        wrapper.style.width = CLOSED_SIZE.width + 'px';
        wrapper.style.height = CLOSED_SIZE.height + 'px';
      }
      iframe.style.width = '100%';
      iframe.style.height = '100%';
    }
    root.addEventListener('resize', applySize);

    // ── protocol ─────────────────────────────────────────────────────────
    function post(type, payload) {
      if (state.destroyed || !iframe.contentWindow) return;
      var msg = { type: PROTOCOL + type, nonce: nonce };
      if (payload) for (var k in payload) if (Object.prototype.hasOwnProperty.call(payload, k)) msg[k] = payload[k];
      iframe.contentWindow.postMessage(msg, bowOrigin);
    }

    function sendInit() {
      post('init', {
        mode: mode,
        context: context,
        metadata: metadata,
        locale: options.locale || null,
        theme: options.theme === 'dark' || options.theme === 'light' ? options.theme : null,
        open: !!state.open
      });
    }

    function emit(name, detail) {
      var fns = listeners[name] || [];
      for (var i = 0; i < fns.length; i++) {
        try { fns[i](detail); } catch (e) { if (root.console) root.console.error('[bow-embed] listener failed', e); }
      }
    }

    function onMessage(event) {
      if (state.destroyed) return;
      if (event.source !== iframe.contentWindow || event.origin !== bowOrigin) return;
      var data = event.data;
      if (!data || typeof data !== 'object' || typeof data.type !== 'string' || data.type.indexOf(PROTOCOL) !== 0) return;
      var type = data.type.slice(PROTOCOL.length);
      if (type === 'hello') { sendInit(); return; }
      if (data.nonce !== nonce) return;
      switch (type) {
        case 'ready':
          state.ready = true;
          emit('ready', {});
          break;
        case 'auth-request':
          handleAuthRequest(data.requestId);
          break;
        case 'state':
          state.open = !!data.open;
          applySize();
          emit(state.open ? 'open' : 'close', {});
          break;
        case 'auth':
          state.auth = data.status;
          if (data.status === 'authenticated') emit('authenticated', { user: data.user || null });
          else if (data.status === 'required' || data.status === 'failed') emit('authRequired', { status: data.status });
          break;
        case 'error':
          emit('error', { code: data.code, message: data.message });
          break;
        case 'logged-out':
          emit('loggedOut', {});
          break;
      }
    }

    function handleAuthRequest(requestId) {
      if (typeof options.getAssertion !== 'function') {
        post('assertion-error', { requestId: requestId, message: 'getAssertion is not configured' });
        emit('authRequired', { status: 'required' });
        return;
      }
      var done = false;
      function fail(err) {
        if (done) return; done = true;
        post('assertion-error', { requestId: requestId, message: (err && err.message) || String(err || 'getAssertion failed') });
        emit('error', { code: 'assertion_failed', message: (err && err.message) || String(err) });
      }
      try {
        Promise.resolve(options.getAssertion()).then(function (assertion) {
          if (done) return; done = true;
          if (typeof assertion !== 'string' || !assertion) return fail(new Error('getAssertion returned no assertion'));
          post('assertion', { requestId: requestId, assertion: assertion });
        }, fail);
      } catch (e) { fail(e); }
    }

    root.addEventListener('message', onMessage);
    iframe.addEventListener('load', sendInit);

    // ── public API ───────────────────────────────────────────────────────
    var api = {
      on: function (name, fn) { (listeners[name] = listeners[name] || []).push(fn); return api; },
      off: function (name, fn) { listeners[name] = (listeners[name] || []).filter(function (f) { return f !== fn; }); return api; },
      open: function () { state.open = true; applySize(); post('open'); return api; },
      close: function () { state.open = false; applySize(); post('close'); return api; },
      updateContext: function (next) { context = plain(next) || {}; post('context', { context: context }); return api; },
      updateMetadata: function (next) { metadata = plain(next) || {}; post('metadata', { metadata: metadata }); return api; },
      setLocale: function (locale) { post('locale', { locale: locale || null }); return api; },
      logout: function () { post('logout'); return api; },
      destroy: function () {
        if (state.destroyed) return;
        state.destroyed = true;
        root.removeEventListener('message', onMessage);
        root.removeEventListener('resize', applySize);
        if (wrapper.parentNode) wrapper.parentNode.removeChild(wrapper);
        listeners = {};
      },
      get state() { return { open: state.open, auth: state.auth, ready: state.ready }; },
      iframe: iframe,
      version: '1'
    };
    return api;
  }

  root.BOWEmbed = { mount: mount, version: '1' };
})(typeof window !== 'undefined' ? window : this);
