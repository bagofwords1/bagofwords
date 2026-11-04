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
 *       hostApp: 'host_…',                    // Settings → Channels → Connected applications
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
 *     embed.on('unavailable', ({ reason }) => {})  // not_published | not_found | denied | not_allowed | failed (retry)
 *                                                  // | no_response (the frame never answered, see startTimeout)
 *     embed.updateContext({ customerId: 'C-1043' })
 *     embed.logout()      // on your sign-out or user switch
 *     embed.destroy()     // after logout(): removes the frame once it has signed out (at most 6 s)
 *   </script>
 *
 * `height`, in both modes: a number is pixels, at least 1. Floating mode:
 * `width` and `height` size the panel, as a number of pixels or one CSS
 * length such as '40rem' or '80dvh', at least 1. The default is 600 x 720 px,
 * and the viewport caps both, so the panel still fits a small window. A
 * browser without dvh/svh/lvh uses the same size in vh. Inline mode: the
 * width follows the container, and `height` also takes any CSS value as
 * given, such as 'calc(100vh - 80px)' (default '100%'), except a plain
 * length below 1px such as '0px'.
 *
 * Floating mode also takes `zIndex` (a whole number from -2147483648 to
 * 2147483647, or a string of digits such as '1000'; default 2147483000),
 * `accentColor` for the launcher (any CSS colour the browser accepts, such as
 * '#2563eb', 'hsl(220deg 80% 50%)' or 'var(--brand)'; default '#2563eb') and `position`
 * ('bottom-right' or 'bottom-left'). Any size, zIndex or accentColor that is
 * not accepted is ignored with a console warning, and the default is used.
 *
 * startTimeout (ms, default 15000, 0 turns it off): a frame the browser
 * refuses (the artifact doesn't exist or doesn't allow this application, the
 * application is disabled, this origin isn't listed) never runs, so it can't
 * say why. If BOW hasn't answered by then, the SDK reports `unavailable` with
 * reason `no_response`. A late answer still continues normally.
 *
 * destroy() right after logout() waits for the frame to finish signing out
 * before removing it. If your page removes the container or navigates away
 * itself, wait for `authRequired` with status `signed_out` first.
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

  // The floating panel's size. Wide enough that a chat looks as it does in
  // BOW's artifact preview; the viewport still caps both sides.
  var FLOATING_WIDTH = '600px';
  var FLOATING_HEIGHT = '720px';

  // A number is pixels; a string must be one plain CSS length (it goes into
  // the panel's style), and either must be at least 1. Anything else falls
  // back to the default, with a console warning when a value was given.
  var CSS_LENGTH = /^(\d+(?:\.\d+)?)(px|rem|em|vw|vh|dvh|svh|lvh|%)$/i;
  var LENGTH_HINT = 'use a number of pixels or one CSS length such as \'40rem\' (at least 1)';

  // Tell the host an option was refused. Neither an odd value (one with no
  // text form) nor a host console that throws may stop the mount.
  function warnIgnored(name, value, hint, fallback) {
    var shown;
    try { shown = JSON.stringify(String(value)); } catch (e) { shown = '(a value with no text form)'; }
    try {
      if (root.console && root.console.warn) {
        root.console.warn('[bow-embed] ignoring ' + name + ' ' + shown + ': ' + hint + '; using ' + fallback);
      }
    } catch (e) { /* the warning is a courtesy: a broken console must not break the embed */ }
  }

  function cssLength(name, value, fallback) {
    if (value == null) return fallback;
    if (typeof value === 'number' && isFinite(value) && value >= 1) return value + 'px';
    if (typeof value === 'string') {
      var match = CSS_LENGTH.exec(value.trim());
      if (match && Number(match[1]) >= 1) return value.trim();
    }
    warnIgnored(name, value, LENGTH_HINT, fallback);
    return fallback;
  }

  // The same length with dvh/svh/lvh turned into plain vh, for the first of two
  // declarations: a browser that does not know the dynamic units keeps that one.
  function plainVh(length) {
    return length.replace(/[dsl]vh$/i, 'vh');
  }

  // z-index: an integer in CSS's range, as a number or a string of digits;
  // anything else given warns and is the default.
  var Z_INDEX = '2147483000';
  var Z_MIN = -2147483648;
  var Z_MAX = 2147483647;
  function zIndexOf(value) {
    if (value == null) return Z_INDEX;
    var text = typeof value === 'string' ? value.trim() : null;
    var n = typeof value === 'number' ? value : (text !== null && /^-?\d+$/.test(text) ? Number(text) : NaN);
    if (isFinite(n) && Math.floor(n) === n && n >= Z_MIN && n <= Z_MAX) return String(n);
    warnIgnored('zIndex', value, 'use a whole number', Z_INDEX);
    return Z_INDEX;
  }

  // The launcher's colour: any colour the browser accepts. It is set through
  // the one style property, so it cannot add other declarations, and read back:
  // a value the browser dropped (or one that is not a string) warns and is the
  // default.
  var ACCENT = '#2563eb';
  function setAccent(launcher, value) {
    if (value == null) { launcher.style.backgroundColor = ACCENT; return; }
    if (typeof value === 'string') {
      launcher.style.backgroundColor = value.trim();
      if (launcher.style.backgroundColor) return;
    }
    warnIgnored('accentColor', value, 'use a CSS colour such as \'#2563eb\'', ACCENT);
    launcher.style.backgroundColor = ACCENT;
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
    // Cleared by the frame's first message that carries the nonce: proof the
    // BOW page runs and accepted this host (a bare `hello` is not).
    var startTimeout = options.startTimeout == null ? 15000 : Number(options.startTimeout) || 0;
    var startTimer = null;
    var answered = false;
    // logout() sent and `signed_out` not heard yet; destroy() then waits for it.
    var loggingOut = false;
    var teardownTimer = null;

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
      // A number is pixels, at least 1; a string is used as given (calc() works)
      // unless the browser refuses it. A refused value warns and is 100%.
      var inlineHeight = options.height;
      if (inlineHeight == null) wrapper.style.height = '100%';
      else if (typeof inlineHeight === 'number' && isFinite(inlineHeight) && inlineHeight >= 1) wrapper.style.height = inlineHeight + 'px';
      else {
        if (typeof inlineHeight === 'string') wrapper.style.height = inlineHeight;
        // A plain length below 1px (the browser reads '0' back as '0px') would
        // leave an invisible frame, as the number 0 would.
        var shown = typeof inlineHeight === 'string' ? String(wrapper.style.height || '') : '';
        var px = /^\s*(\d*\.?\d+)px\s*$/i.exec(shown);
        if (typeof inlineHeight !== 'string' || !shown || (px && Number(px[1]) < 1)) {
          warnIgnored('height', inlineHeight, 'use a number of pixels (at least 1) or a CSS height', '100%');
          wrapper.style.height = '100%';
        }
      }
      wrapper.appendChild(iframe);
      container.appendChild(wrapper);
    } else {
      var side = options.position === 'bottom-left' ? 'left' : 'right';
      var panelWidth = cssLength('width', options.width, FLOATING_WIDTH);
      var panelHeight = cssLength('height', options.height, FLOATING_HEIGHT);
      var zIndex = zIndexOf(options.zIndex);
      // Each first line uses only vh, which every browser knows; the second
      // (dvh, which follows a mobile browser's toolbar) replaces it where known.
      var widthLines = 'width:min(' + plainVh(panelWidth) + ',calc(100vw - 32px));' +
        (plainVh(panelWidth) !== panelWidth ? 'width:min(' + panelWidth + ',calc(100vw - 32px));' : '');
      wrapper.style.cssText = 'position:fixed;bottom:88px;' + side + ':16px;' + widthLines +
        'height:min(' + plainVh(panelHeight) + ',calc(100vh - 120px));height:min(' + panelHeight + ',calc(100dvh - 120px));' +
        'border-radius:12px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.18);' +
        'background:#fff;display:none;z-index:' + zIndex;
      wrapper.appendChild(iframe);
      launcher = document.createElement('button');
      launcher.type = 'button';
      launcher.setAttribute('aria-label', options.title || 'Open assistant');
      launcher.setAttribute('aria-expanded', 'false');
      launcher.setAttribute('data-bow-embed-launcher', options.artifact);
      launcher.style.cssText = 'position:fixed;bottom:16px;' + side + ':16px;width:56px;height:56px;border-radius:28px;' +
        'border:0;cursor:pointer;color:#fff;box-shadow:0 6px 20px rgba(0,0,0,.2);z-index:' + zIndex;
      setAccent(launcher, options.accentColor);
      launcher.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
      launcher.addEventListener('click', function () { setOpen(!state.open); });
      document.body.appendChild(wrapper);
      document.body.appendChild(launcher);
    }

    function setOpen(open) {
      if (mode !== 'floating' || state.destroyed) return;
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
        if (state.destroyed) return;  // a listener destroyed the embed: the rest hear nothing
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
      if (event.source !== iframe.contentWindow || event.origin !== bowOrigin) return;
      var data = event.data;
      if (!data || typeof data !== 'object' || typeof data.type !== 'string' || data.type.indexOf(PROTOCOL) !== 0) return;
      var type = data.type.slice(PROTOCOL.length);
      if (state.destroyed) {
        // Only the sign-out a destroy() is waiting for, then the frame goes.
        if (loggingOut && type === 'auth' && data.status === 'signed_out' && data.nonce === secret) removeNow();
        return;
      }
      if (type === 'hello') { sendInit(); return; }
      if (data.nonce !== secret) return;
      if (!answered) { answered = true; clearTimer(startTimer); startTimer = null; }
      if (type === 'credential-request') answer(String(data.requestId || ''), data.kind);
      else if (type === 'auth') {
        if (data.status === 'signed_out') loggingOut = false;
        state.status = data.status;
        if (data.status === 'authenticated') { state.user = data.user || null; emit('authenticated', { user: state.user }); }
        else emit('authRequired', { status: data.status, message: data.error || null });
      }
      else if (type === 'unavailable') {
        // Signed in, but the artifact cannot be shown (not published, gone,
        // not shared, not embeddable here). Not a sign-in problem.
        state.status = 'unavailable';
        emit('unavailable', { reason: data.reason || null, message: data.message || null });
      }
    }

    function clearTimer(timer) { if (timer) root.clearTimeout(timer); }

    function removeNow() {
      clearTimer(teardownTimer); teardownTimer = null;
      loggingOut = false;
      root.removeEventListener('message', onMessage);
      if (wrapper.parentNode) wrapper.parentNode.removeChild(wrapper);
    }

    root.addEventListener('message', onMessage);
    iframe.addEventListener('load', sendInit);
    if (startTimeout > 0) {
      startTimer = root.setTimeout(function () {
        startTimer = null;
        if (answered || state.destroyed) return;
        state.status = 'unavailable';
        emit('unavailable', {
          reason: 'no_response',
          message: 'BOW did not answer. The artifact may not exist or may not allow this application, ' +
            'the application may be disabled, or this page\'s origin may not be listed.'
        });
      }, startTimeout);
    }

    // ── public API ───────────────────────────────────────────────────────
    var api = {
      on: function (name, fn) { if (!state.destroyed) (listeners[name] = listeners[name] || []).push(fn); return api; },
      off: function (name, fn) { listeners[name] = (listeners[name] || []).filter(function (f) { return f !== fn; }); return api; },
      open: function () { setOpen(true); return api; },
      close: function () { setOpen(false); return api; },
      updateContext: function (next) { context = plain(next) || {}; post('context', { context: context }); return api; },
      setLocale: function (locale) { post('locale', { locale: locale || null }); return api; },
      logout: function () { if (!state.destroyed) { loggingOut = true; post('logout'); } return api; },
      destroy: function () {
        if (state.destroyed) return;
        state.destroyed = true;
        clearTimer(startTimer); startTimer = null;
        listeners = {};
        if (launcher && launcher.parentNode) launcher.parentNode.removeChild(launcher);
        if (loggingOut && answered) {
          // The frame is still signing out (revoking its session): hide it
          // now, remove it when it says signed_out, or after 6 s at most.
          wrapper.style.display = 'none';
          teardownTimer = root.setTimeout(removeNow, 6000);
          return;
        }
        removeNow();
      },
      get state() { return { status: state.status, user: state.user, open: state.open }; },
      iframe: iframe,
      version: '1'
    };
    return api;
  }

  root.BOWEmbed = { mount: mount, version: '1' };
})(window);
