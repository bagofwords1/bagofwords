/*!
 * BOW embed SDK, v1: put a published BOW artifact (dashboard, data app,
 * chat assistant) inside your application, signed in as your user.
 *
 * Served by your own BOW at /api/embed/sdk/v1/bow-embed.js (no CDN). It
 * renders an <iframe> of the artifact and answers the frame's sign-in
 * requests. It is a UI shim, not an authorization boundary: BOW decides what
 * the user may see.
 *
 *   <script src="https://bow.example.com/api/embed/sdk/v1/bow-embed.js"></script>
 *   <script>
 *     const embed = BOWEmbed.mount({
 *       baseUrl: 'https://bow.example.com',
 *       hostApp: 'host_…',                    // Settings → Integrations → Host applications
 *       artifact: 'sales-overview',           // the artifact's id or name
 *       container: '#bow',                    // or mode: 'floating'
 *       identity: {
 *         // assertion mode: your backend signs a 60-second RS256 JWT
 *         getAssertion: () => fetch('/bow/assertion', { method: 'POST' }).then(r => r.json()).then(b => b.assertion),
 *         // idp_token mode instead: getIdToken: () => msal.acquireTokenSilent(…).then(r => r.idToken),
 *         // sso mode needs nothing here: the frame offers BOW sign-in.
 *       },
 *       context: { customerId: 'C-1042' },    // advisory: what the user is looking at
 *     })
 *     embed.on('authenticated', ({ user }) => {})
 *     embed.updateContext({ customerId: 'C-1043' })
 *     embed.logout()      // on your sign-out or user switch
 *     embed.destroy()
 *   </script>
 *
 * The private key never belongs in the browser: getAssertion must call your
 * backend, which checks its own session and signs for that user only.
 */
(function (root) {
  'use strict';

  var PROTOCOL = 'bow:';

  function nonce() {
    var bytes = new Uint8Array(16);
    root.crypto.getRandomValues(bytes);
    var out = '';
    for (var i = 0; i < bytes.length; i++) out += ('0' + bytes[i].toString(16)).slice(-2);
    return out;
  }

  function originOf(url) {
    return new URL(url, root.location.href).origin;
  }

  function plain(obj) {
    if (!obj || typeof obj !== 'object') return null;
    try { return JSON.parse(JSON.stringify(obj)); } catch (e) { return null; }
  }

  function element(target) {
    return typeof target === 'string' ? document.querySelector(target) : target;
  }

  function mount(options) {
    options = options || {};
    if (!options.baseUrl) throw new Error('BOWEmbed.mount: baseUrl is required');
    if (!options.hostApp) throw new Error('BOWEmbed.mount: hostApp is required');
    if (!options.artifact) throw new Error('BOWEmbed.mount: artifact is required');
    var baseUrl = String(options.baseUrl).replace(/\/+$/, '');
    var bowOrigin = originOf(baseUrl);
    var mode = options.mode === 'floating' ? 'floating' : 'inline';
    var identity = options.identity || {};
    var secret = nonce();
    var listeners = {};
    var context = plain(options.context);
    var state = { status: 'starting', user: null, destroyed: false, open: mode === 'inline' };

    // ── DOM ──────────────────────────────────────────────────────────────
    var iframe = document.createElement('iframe');
    iframe.title = options.title || 'BOW';
    iframe.setAttribute('allow', 'clipboard-write');
    iframe.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
    iframe.style.border = '0';
    iframe.style.width = '100%';
    iframe.style.height = '100%';
    iframe.style.display = 'block';
    iframe.style.colorScheme = 'normal';
    iframe.src = baseUrl + '/d/' + encodeURIComponent(options.artifact) + '?host=' + encodeURIComponent(options.hostApp);

    var wrapper = document.createElement('div');
    wrapper.setAttribute('data-bow-embed', options.artifact);
    var launcher = null;
    if (mode === 'inline') {
      var container = element(options.container);
      if (!container) throw new Error('BOWEmbed.mount: inline mode needs a `container` element or selector');
      wrapper.style.width = '100%';
      wrapper.style.height = options.height || '100%';
      wrapper.appendChild(iframe);
      container.appendChild(wrapper);
    } else {
      var side = options.position === 'bottom-left' ? 'left' : 'right';
      wrapper.style.cssText = 'position:fixed;bottom:88px;' + side + ':16px;width:min(420px,calc(100vw - 32px));' +
        'height:min(640px,calc(100vh - 120px));border-radius:12px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.18);' +
        'background:#fff;display:none;z-index:' + String(options.zIndex || 2147483000);
      wrapper.appendChild(iframe);
      launcher = document.createElement('button');
      launcher.type = 'button';
      launcher.setAttribute('aria-label', options.title || 'Open assistant');
      launcher.setAttribute('aria-expanded', 'false');
      launcher.setAttribute('data-bow-embed-launcher', options.artifact);
      launcher.style.cssText = 'position:fixed;bottom:16px;' + side + ':16px;width:56px;height:56px;border-radius:28px;' +
        'border:0;cursor:pointer;color:#fff;background:' + (options.accentColor || '#2563eb') + ';' +
        'box-shadow:0 6px 20px rgba(0,0,0,.2);z-index:' + String(options.zIndex || 2147483000);
      launcher.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
      launcher.addEventListener('click', function () { setOpen(!state.open); });
      document.body.appendChild(wrapper);
      document.body.appendChild(launcher);
    }

    function setOpen(open) {
      if (mode !== 'floating') return;
      state.open = !!open;
      wrapper.style.display = state.open ? 'block' : 'none';
      launcher.setAttribute('aria-expanded', String(state.open));
      emit(state.open ? 'open' : 'close', {});
    }

    // ── protocol ─────────────────────────────────────────────────────────
    function post(type, payload) {
      if (state.destroyed || !iframe.contentWindow) return;
      var msg = { type: PROTOCOL + type, nonce: secret };
      if (payload) for (var k in payload) if (Object.prototype.hasOwnProperty.call(payload, k)) msg[k] = payload[k];
      iframe.contentWindow.postMessage(msg, bowOrigin);
    }

    function sendInit() {
      post('init', {
        context: context,
        locale: options.locale || null,
        theme: options.theme === 'dark' || options.theme === 'light' ? options.theme : null
      });
    }

    function emit(name, detail) {
      var fns = (listeners[name] || []).slice();
      for (var i = 0; i < fns.length; i++) {
        try { fns[i](detail); } catch (e) { if (root.console) root.console.error('[bow-embed] listener failed', e); }
      }
    }

    function answer(requestId, kind) {
      var fn = kind === 'id_token' ? identity.getIdToken : identity.getAssertion;
      var name = kind === 'id_token' ? 'getIdToken' : 'getAssertion';
      var done = false;
      function fail(err) {
        if (done) return; done = true;
        var message = (err && err.message) || String(err || name + ' failed');
        post('credential', { requestId: requestId, credential: null, error: message });
        emit('error', { code: 'credential_failed', message: message });
      }
      if (typeof fn !== 'function') return fail(new Error('identity.' + name + ' is not configured'));
      try {
        Promise.resolve(fn()).then(function (value) {
          if (done) return;
          if (typeof value !== 'string' || !value) return fail(new Error(name + ' returned nothing'));
          done = true;
          post('credential', { requestId: requestId, credential: value });
        }, fail);
      } catch (e) { fail(e); }
    }

    function onMessage(event) {
      if (state.destroyed || event.source !== iframe.contentWindow || event.origin !== bowOrigin) return;
      var data = event.data;
      if (!data || typeof data !== 'object' || typeof data.type !== 'string' || data.type.indexOf(PROTOCOL) !== 0) return;
      var type = data.type.slice(PROTOCOL.length);
      if (type === 'hello') { sendInit(); return; }
      if (data.nonce !== secret) return;
      if (type === 'credential-request') answer(String(data.requestId || ''), data.kind);
      else if (type === 'auth') {
        state.status = data.status;
        if (data.status === 'authenticated') { state.user = data.user || null; emit('authenticated', { user: state.user }); }
        else emit('authRequired', { status: data.status, message: data.error || null });
      }
    }

    root.addEventListener('message', onMessage);
    iframe.addEventListener('load', sendInit);

    // ── public API ───────────────────────────────────────────────────────
    var api = {
      on: function (name, fn) { (listeners[name] = listeners[name] || []).push(fn); return api; },
      off: function (name, fn) { listeners[name] = (listeners[name] || []).filter(function (f) { return f !== fn; }); return api; },
      open: function () { setOpen(true); return api; },
      close: function () { setOpen(false); return api; },
      updateContext: function (next) { context = plain(next) || {}; post('context', { context: context }); return api; },
      setLocale: function (locale) { post('locale', { locale: locale || null }); return api; },
      logout: function () { post('logout'); return api; },
      destroy: function () {
        if (state.destroyed) return;
        state.destroyed = true;
        root.removeEventListener('message', onMessage);
        if (wrapper.parentNode) wrapper.parentNode.removeChild(wrapper);
        if (launcher && launcher.parentNode) launcher.parentNode.removeChild(launcher);
        listeners = {};
      },
      get state() { return { status: state.status, user: state.user, open: state.open }; },
      iframe: iframe,
      version: '1'
    };
    return api;
  }

  root.BOWEmbed = { mount: mount, version: '1' };
})(window);
