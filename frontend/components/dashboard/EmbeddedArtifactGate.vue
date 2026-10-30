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
import { EMBED_UNAVAILABLE_MESSAGE_KEYS, embedRuntimeIdentity, renewalDelayMs, renewalFailure, sessionLifetimeMs, type EmbedLoadFailure, type EmbedUnavailableReason } from '~/utils/embedSession';

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

const api = (path: string) => `${config.public.baseURL}/embed/${encodeURIComponent(props.hostApplication)}${path}`;

const host = useEmbedHost(() => allowedOrigins, {
    onInit: (init) => {
        emit('context', init.context);
        if (init.locale) applyLocale(init.locale);
        signIn();
    },
    onContext: (context) => emit('context', context),
    onLocale: (locale) => locale && applyLocale(locale),
    onLogout: () => signOut(),
});

function applyLocale(locale: string) {
    try { $setLocale?.(locale); } catch { /* unknown locale: keep the current one */ }
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

function schedule(next: () => void, delay: number) {
    if (renewTimer) clearTimeout(renewTimer);
    renewTimer = setTimeout(next, delay);
}

async function accept(session: any) {
    const next = {
        token: session.token,
        organizationId: session.organization_id,
        hostApplication: session.host_application,
        viewer: session.viewer || null,
    };
    const sameViewer = !!credential.value && embedRuntimeIdentity(credential.value, '') === embedRuntimeIdentity(next, '');
    // A silent renewal over an artifact that could not be opened: the view
    // re-checks it (remount below); the host hears again only if that changes.
    const renewingUnavailable = sameViewer && status.value === 'unavailable';
    // Someone else now: the previous viewer's app unmounts before theirs mounts.
    if (credential.value && !sameViewer) {
        credential.value = null;
        status.value = 'signing_in';
        message.value = t('embed.gate.signingIn');
        await nextTick();
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
    if (failure === 'session_invalid') { expire(); return; }
    status.value = 'unavailable';
    unavailableReason.value = failure;
    message.value = t(EMBED_UNAVAILABLE_MESSAGE_KEYS[failure]);
    if (toldUnavailable === failure) return;  // the host already knows (a renewal's re-check)
    toldUnavailable = failure;
    host.post('bow:unavailable', { reason: failure, message: message.value });
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
    inflight = (async () => {
        if (mode.value === 'sso') { fail('sign_in_required', t('embed.gate.signInRequired')); return; }
        // A renewal is silent, over the artifact or over why it is not shown.
        if (status.value !== 'ready' && status.value !== 'unavailable') { status.value = 'signing_in'; message.value = t('embed.gate.signingIn'); }
        try {
            const kind = mode.value === 'idp_token' ? 'id_token' : 'assertion';
            const value = await host.requestCredential(kind);
            await accept(await exchange({ [kind]: value }));
        } catch (e: any) {
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
    })().finally(() => { inflight = null; });
    return inflight;
}

// SSO mode: BOW's own sign-in in a popup (a user click opens it). The popup
// posts a one-time code back to this frame, bound to our PKCE challenge.
function b64url(bytes: ArrayBuffer | Uint8Array) {
    const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    return btoa(String.fromCharCode(...arr)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function signInWithPopup() {
    const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
    const challenge = b64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
    const state = crypto.randomUUID();
    const url = `/embed-signin?host=${encodeURIComponent(props.hostApplication)}&challenge=${challenge}&state=${state}`;
    const popup = window.open(url, 'bow-embed-signin', 'popup,width=480,height=640');
    if (!popup) { fail('sign_in_required', t('embed.gate.popupBlocked')); return; }
    status.value = 'signing_in';
    message.value = t('embed.gate.signingIn');
    const onMessage = async (event: MessageEvent) => {
        if (event.origin !== window.location.origin || event.source !== popup) return;
        const data = event.data;
        if (data?.type !== 'bow:sso-code' || data.state !== state) return;
        window.removeEventListener('message', onMessage);
        try { popup.close(); } catch { /* already closed */ }
        try {
            await accept(await exchange({ sso_code: String(data.code || ''), code_verifier: verifier }));
        } catch (e: any) {
            fail('failed', getErrorMessage(e));
        }
    };
    window.addEventListener('message', onMessage);
}

async function signOut() {
    if (renewTimer) clearTimeout(renewTimer);
    const had = credential.value?.token;
    credential.value = null;
    fail('signed_out', t('embed.gate.signedOut'));
    if (had) {
        try {
            await fetch(api('/session'), { method: 'DELETE', credentials: 'omit', headers: { Authorization: `Bearer ${had}` } });
        } catch { /* the token is gone from memory and expires server-side */ }
    }
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
        if (!boot.enabled) { fail('unavailable', t("errors['host_app.disabled']")); return; }
    } catch (e: any) {
        fail('unavailable', getErrorMessage(e));
        return;
    }
    host.start();
});

onBeforeUnmount(() => {
    host.stop();
    if (renewTimer) clearTimeout(renewTimer);
    credential.value = null;
});
</script>
