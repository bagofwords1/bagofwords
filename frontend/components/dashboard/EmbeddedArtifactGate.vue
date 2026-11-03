<template>
    <!-- /d/{ref}?host=…: a published artifact inside a host application.
         Nothing renders until the host has signed its user in. -->
    <div class="h-screen w-full" data-testid="embedded-artifact" :data-embed-status="status">
        <slot v-if="status === 'ready'" />
        <div v-else class="h-full w-full flex items-center justify-center bg-white dark:bg-gray-900 p-6">
            <div class="max-w-sm text-center space-y-3">
                <Spinner v-if="status === 'starting' || status === 'signing_in'" class="w-5 h-5 mx-auto text-gray-400" />
                <p class="text-sm text-gray-600 dark:text-gray-300" data-testid="embedded-artifact-message">{{ message }}</p>
                <UButton v-if="status === 'sign_in_required' && mode === 'sso'" size="sm" color="blue"
                         data-testid="embedded-artifact-sign-in" @click="signInWithPopup">
                    {{ $t('embed.gate.signIn') }}
                </UButton>
                <UButton v-else-if="(status === 'failed' && retryable) || status === 'signed_out'" size="sm" variant="soft"
                         data-testid="embedded-artifact-retry" @click="signIn">
                    {{ $t('embed.gate.retry') }}
                </UButton>
                <UButton v-else-if="status === 'unavailable' && unavailableReason === 'failed'" size="sm" variant="soft"
                         data-testid="embedded-artifact-retry" @click="retryOpen">
                    {{ $t('embed.gate.retry') }}
                </UButton>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue';
import { runtimeIdentity, embedUnavailableMessage, renewalDelayMs, renewalFailure, sessionLifetimeMs, PERMANENT_ERROR_CODES, type EmbedLoadFailure, type EmbedUnavailableReason } from '~/utils/embedSession';

const props = defineProps<{ hostApplication: string }>();
const emit = defineEmits<{ (e: 'context', value: Record<string, any> | null): void }>();

const { t } = useI18n();
const { getErrorMessage } = useErrorMessage();
const config = useRuntimeConfig();
const credential = useEmbedCredential();
const { $setLocale } = useNuxtApp() as any;

type Status = 'starting' | 'signing_in' | 'sign_in_required' | 'ready' | 'failed' | 'signed_out' | 'unavailable';
const status = ref<Status>('starting');
const message = ref(t('embed.gate.starting'));
const mode = ref<'assertion' | 'idp_token' | 'sso' | null>(null);
const retryable = ref(true);
// Why the view could not open the artifact (status 'unavailable' after a sign-in).
const unavailableReason = ref<EmbedUnavailableReason | null>(null);
// What the host was last told about the artifact: unavailable, and why
// (bow:unavailable); null once it heard 'authenticated' since. A renewal does
// not tell it again, so the host's state does not flip every cycle.
let toldUnavailable: EmbedUnavailableReason | null = null;
let signedInUser: { name?: string, email?: string } | null = null;
let allowedOrigins: string[] = [];
let renewTimer: ReturnType<typeof setTimeout> | null = null;
// When the current session really ends, on this page's clock.
let expiresAt = 0;
let renewFailures = 0;
// Bumped by a sign-out (and unmount). A sign-in still in flight from before
// it is dropped when its answer comes: the host signed the user out meanwhile.
let generation = 0;
// Exchanges still in flight (a sign-in or renewal, an SSO code): a sign-out
// waits for them, so the session one returns late is revoked before the host
// hears signed_out and may remove this frame.
const pendingExchanges = new Set<Promise<unknown>>();
function track<T>(p: Promise<T>): Promise<T> {
    pendingExchanges.add(p);
    p.catch(() => {}).finally(() => pendingExchanges.delete(p));
    return p;
}
// Bumped when a sign-in starts: a sign-out still cleaning up then stays
// silent (the host hears this sign-in instead of a stale signed_out).
let signInEpoch = 0;
// The sign-out cleaning up now; a second logout waits for it too.
let signingOut: Promise<void> | null = null;
// Why the frame cannot start although the host may be told (bootstrap
// answered, so its origins are known): said once the host's init arrives.
let startupFailure: { reason: EmbedUnavailableReason, message: string } | null = null;

const api = (path: string) => `${config.public.baseURL}/embed/${encodeURIComponent(props.hostApplication)}${path}`;

const host = useEmbedHost(() => allowedOrigins, {
    onInit: async (init) => {
        if (startupFailure) { host.post('bow:unavailable', startupFailure); return; }
        emit('context', init.context);
        // The artifact frame takes the page's language when it is built (its
        // <html lang>, which the sandbox's own words follow), so the host's
        // language is in place before the sign-in that leads to it.
        // A logout while the catalog loads wins: no sign-in after it.
        const gen = generation;
        if (init.locale) await applyLocale(init.locale);
        if (gen !== generation) return;
        signIn();
    },
    onContext: (context) => emit('context', context),
    onLocale: (locale) => locale && applyLocale(locale),
    onLogout: () => signOut(),
});

async function applyLocale(locale: string) {
    try { await $setLocale?.(locale); } catch { /* unknown locale: keep the current one */ }
}

function fail(next: Status, text: string) {
    status.value = next;
    message.value = text;
    toldUnavailable = null;
    host.post('bow:auth', { status: next, error: text });
}

async function exchange(body: Record<string, string>) {
    const res = await fetch(api('/session'), {
        method: 'POST', credentials: 'omit',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw { status: res.status, data: payload };
    return payload;
}

// Revokes a session nobody will use (signed out, or its sign-in was overtaken
// by a logout or an unmount), so it does not stay valid server-side until it
// expires. keepalive: the host may remove this frame as soon as it hears
// signed_out, and the request must outlive it. A network error or a 5xx is
// tried once more; a 401 means the session is already gone.
function revoke(token: string | undefined): Promise<void> {
    if (!token) return Promise.resolve();
    const send = () => fetch(api('/session'), {
        method: 'DELETE', credentials: 'omit', keepalive: true, headers: { Authorization: `Bearer ${token}` },
    }).then((res) => { if (res.status >= 500) throw new Error(`revoke ${res.status}`); });
    return send().catch(() => new Promise<void>((resolve) => setTimeout(resolve, 1000))
        .then(send).catch(() => { /* it expires server-side */ }));
}

function schedule(next: () => void, delay: number) {
    if (renewTimer) clearTimeout(renewTimer);
    renewTimer = setTimeout(next, delay);
}

async function accept(session: any) {
    const gen = generation;
    const next = {
        token: session.token,
        organizationId: session.organization_id,
        hostApplication: session.host_application,
        userId: session.user?.id ?? null,
        viewer: session.viewer || null,
    };
    const sameViewer = !!credential.value && runtimeIdentity(credential.value, '') === runtimeIdentity(next, '');
    // A silent renewal over an artifact that could not be opened: the view
    // re-checks it (remount below); the host hears again only if that changes.
    const renewingUnavailable = sameViewer && status.value === 'unavailable';
    // Someone else now: the previous viewer's app unmounts before theirs mounts.
    if (credential.value && !sameViewer) {
        credential.value = null;
        status.value = 'signing_in';
        message.value = t('embed.gate.signingIn');
        await nextTick();
        if (gen !== generation) { await revoke(next.token); return; }
    }
    credential.value = next;
    status.value = 'ready';
    signedInUser = { name: session.user?.name, email: session.user?.email };
    if (!renewingUnavailable) {
        toldUnavailable = null;
        host.post('bow:auth', { status: 'authenticated', user: signedInUser });
    }
    renewFailures = 0;
    const lifetime = sessionLifetimeMs(session);
    expiresAt = Date.now() + lifetime;
    // SSO cannot renew silently: it asks for a sign-in when the session really ends.
    schedule(renew, mode.value === 'sso' ? lifetime : renewalDelayMs(lifetime));
}

async function renew() {
    // The host signs the user in again silently.
    if (mode.value === 'sso') { expire(); return; }
    await signIn();
}

function expire() {
    credential.value = null;
    retryable.value = true;
    fail(mode.value === 'sso' ? 'sign_in_required' : 'failed', t('embed.gate.expired'));
}

// The view could not open the artifact (SharedDashboardView's `unavailable`).
// A dead session ends as an expired one does: the host is asked to sign the
// user in again (bow:auth). Anything else is said here, in place, and told to
// the host (bow:unavailable, the SDK's `unavailable` event); the session stays,
// so its renewal shows the artifact once it can be opened.
function artifactUnavailable(failure: EmbedLoadFailure) {
    if (failure === 'session_invalid') { sessionEndedByServer(); return; }
    status.value = 'unavailable';
    unavailableReason.value = failure;
    message.value = embedUnavailableMessage(failure, t);
    if (toldUnavailable === failure) return;  // the host already knows (a renewal's re-check)
    toldUnavailable = failure;
    host.post('bow:unavailable', { reason: failure, message: message.value });
}

// BOW refused the session the page is using (the app was disabled, everyone was
// signed out, it expired): it ends once, however many requests report it. A
// renewal scheduled or in flight is dropped, so it cannot bring it back; the
// host is asked to sign the user in again.
function sessionEndedByServer() {
    if (!credential.value) return;  // already ended
    generation++;
    inflight = null;
    if (renewTimer) { clearTimeout(renewTimer); renewTimer = null; }
    expire();
}

// The view opened the artifact. A host told it was unavailable hears it is shown now.
function artifactOpened() {
    if (toldUnavailable === null) return;
    toldUnavailable = null;
    host.post('bow:auth', { status: 'authenticated', user: signedInUser });
}

// Try again after a failed load (`failed`): the view mounts afresh, same session.
function retryOpen() {
    if (status.value === 'unavailable' && credential.value) status.value = 'ready';
}

defineExpose({ artifactUnavailable, artifactOpened });

let inflight: Promise<void> | null = null;
function signIn(): Promise<void> {
    if (inflight) return inflight;
    signInEpoch++;
    const gen = generation;
    const current: Promise<void> = (async () => {
        if (mode.value === 'sso') { fail('sign_in_required', t('embed.gate.signInRequired')); return; }
        // A renewal is silent, over the artifact or over why it is not shown.
        if (status.value !== 'ready' && status.value !== 'unavailable') { status.value = 'signing_in'; message.value = t('embed.gate.signingIn'); }
        try {
            const kind = mode.value === 'idp_token' ? 'id_token' : 'assertion';
            const value = await host.requestCredential(kind);
            if (gen !== generation) return;
            const session = await exchange({ [kind]: value });
            if (gen !== generation) { await revoke(session?.token); return; }  // signed out meanwhile
            await accept(session);
        } catch (e: any) {
            if (gen !== generation) return;
            // A failed renewal keeps the session it was renewing, and the app:
            // retry quietly until that session really ends.
            const next = renewalFailure({ hasCredential: !!credential.value, errorCode: e?.data?.error_code, failures: renewFailures, remainingMs: expiresAt - Date.now() });
            if (next.kind !== 'fail') {
                renewFailures++;
                schedule(next.kind === 'expire' ? expire : renew, next.delay!);
                return;
            }
            credential.value = null;
            retryable.value = !!next.retryable;
            fail('failed', e?.data ? getErrorMessage(e) : (e?.message || t('embed.gate.failed')));
        }
    })().finally(() => { if (inflight === current) inflight = null; });
    inflight = current;
    track(current);
    return current;
}

// SSO mode: BOW's own sign-in in a popup (a user click opens it). The popup
// posts a one-time code back to this frame, bound to our PKCE challenge.
function b64url(bytes: ArrayBuffer | Uint8Array) {
    const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    return btoa(String.fromCharCode(...arr)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// The SSO attempt in progress (one at a time): a new attempt, a sign-out or
// an unmount ends it and removes its listener, so abandoned popups leave
// nothing behind. Each attempt only ever removes its own listener.
let ssoAttempt: { cleanup: () => void } | null = null;
function endSsoAttempt() {
    ssoAttempt?.cleanup();
    ssoAttempt = null;
}

async function signInWithPopup() {
    endSsoAttempt();
    const attempt = { cleanup: () => {} };
    ssoAttempt = attempt;
    const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
    const challenge = b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
    const state = crypto.randomUUID();
    const gen = generation;
    if (ssoAttempt !== attempt) return;  // a newer attempt started while this one hashed
    const url = `/embed-signin?host=${encodeURIComponent(props.hostApplication)}&challenge=${challenge}&state=${state}`;
    const popup = window.open(url, 'bow-embed-signin', 'popup,width=480,height=640');
    if (!popup) { fail('sign_in_required', t('embed.gate.popupBlocked')); return; }
    status.value = 'signing_in';
    message.value = t('embed.gate.signingIn');
    signInEpoch++;
    const onMessage = async (event: MessageEvent) => {
        if (event.origin !== window.location.origin || event.source !== popup) return;
        const data = event.data;
        if (data?.state !== state) return;
        if (data?.type === 'bow:sso-error') {
            // BOW refused the sign-in (a disabled app, for one): the popup shows
            // why, and the frame stops waiting.
            attempt.cleanup();
            if (gen !== generation) return;
            retryable.value = !PERMANENT_ERROR_CODES.includes(String(data.error_code));
            fail('failed', typeof data.message === 'string' && data.message ? data.message : t('embed.gate.failed'));
            return;
        }
        if (data?.type !== 'bow:sso-code') return;
        attempt.cleanup();
        try { popup.close(); } catch { /* already closed */ }
        await track((async () => {
            try {
                const session = await exchange({ sso_code: String(data.code || ''), code_verifier: verifier });
                if (gen !== generation) { await revoke(session?.token); return; }  // signed out meanwhile
                await accept(session);
            } catch (e: any) {
                if (gen !== generation) return;
                // A disabled app or an unknown person: no retry offered.
                retryable.value = !PERMANENT_ERROR_CODES.includes(String(e?.data?.error_code));
                fail('failed', getErrorMessage(e));
            }
        })());
    };
    window.addEventListener('message', onMessage);
    attempt.cleanup = () => {
        window.removeEventListener('message', onMessage);
        if (ssoAttempt === attempt) ssoAttempt = null;
    };
}

// The host hears signed_out only once this frame has done its part: the
// session revoked, and every exchange still in flight settled (the session
// one returns late is revoked where it lands), including an earlier
// sign-out's. At most 5 s, so a stuck request cannot hold the host.
function signOut(): Promise<void> {
    endSsoAttempt();
    const gen = ++generation;
    const epoch = signInEpoch;
    inflight = null;  // a sign-in from before is dropped; the next one starts afresh
    if (renewTimer) clearTimeout(renewTimer);
    const had = credential.value?.token;
    credential.value = null;
    status.value = 'signed_out';
    message.value = t('embed.gate.signedOut');
    toldUnavailable = null;
    const waits: Promise<unknown>[] = [revoke(had), ...[...pendingExchanges].map((p) => p.catch(() => {}))];
    if (signingOut) waits.push(signingOut);
    const current = (async () => {
        await Promise.race([Promise.all(waits), new Promise((resolve) => setTimeout(resolve, 5000))]);
        // Signed out again (that one tells the host), or signed in again meanwhile.
        if (gen !== generation || epoch !== signInEpoch) return;
        host.post('bow:auth', { status: 'signed_out', error: message.value });
    })();
    signingOut = current;
    current.finally(() => { if (signingOut === current) signingOut = null; });
    return current;
}

onMounted(async () => {
    if (!host.embedded) {
        fail('unavailable', t('embed.gate.notFramed'));
        return;
    }
    try {
        const res = await fetch(api('/bootstrap'), { credentials: 'omit' });
        if (!res.ok) throw { status: res.status, data: await res.json().catch(() => ({})) };
        const boot = await res.json();
        allowedOrigins = boot.allowed_origins || [];
        mode.value = boot.identity_mode;
        if (!boot.enabled) {
            // Its origins are known: tell the host why, after its init.
            status.value = 'unavailable';
            message.value = t("errors['host_app.disabled']");
            startupFailure = { reason: 'not_allowed', message: message.value };
            host.start();
            return;
        }
    } catch (e: any) {
        fail('unavailable', getErrorMessage(e));
        return;
    }
    host.start();
});

onBeforeUnmount(() => {
    endSsoAttempt();
    generation++;
    host.stop();
    if (renewTimer) clearTimeout(renewTimer);
    credential.value = null;
});
</script>
