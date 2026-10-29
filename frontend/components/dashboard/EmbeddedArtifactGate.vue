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
                    {{ $t('embed.gate.signIn', { name: hostName || 'BOW' }) }}
                </UButton>
                <UButton v-else-if="(status === 'failed' && retryable) || status === 'signed_out'" size="sm" variant="soft"
                         data-testid="embedded-artifact-retry" @click="signIn">
                    {{ $t('embed.gate.retry') }}
                </UButton>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue';

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
const hostName = ref('');
// Who BOW does not know (or knows twice, or deactivated) gains nothing from a retry.
const PERMANENT = ['embed.identity_unmapped', 'embed.identity_ambiguous', 'embed.user_inactive'];
const retryable = ref(true);
let allowedOrigins: string[] = [];
let renewTimer: ReturnType<typeof setTimeout> | null = null;
// Renew this long before expiry, so a running request never straddles it.
const RENEW_LEAD_MS = 5 * 60 * 1000;

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

function accept(session: any) {
    credential.value = {
        token: session.token,
        organizationId: session.organization_id,
        hostApplication: session.host_application,
        viewer: session.viewer || null,
    };
    status.value = 'ready';
    host.post('bow:auth', { status: 'authenticated', user: { name: session.user?.name, email: session.user?.email } });
    if (renewTimer) clearTimeout(renewTimer);
    const wait = new Date(session.expires_at).getTime() - Date.now() - RENEW_LEAD_MS;
    renewTimer = setTimeout(() => renew(), Math.max(wait, 15_000));
}

async function renew() {
    // The host signs the user in again silently; SSO sessions ask for a new sign-in.
    if (mode.value === 'sso') { credential.value = null; fail('sign_in_required', t('embed.gate.expired')); return; }
    await signIn();
}

let inflight: Promise<void> | null = null;
function signIn(): Promise<void> {
    if (inflight) return inflight;
    inflight = (async () => {
        if (mode.value === 'sso') { fail('sign_in_required', t('embed.gate.signInRequired')); return; }
        if (status.value !== 'ready') { status.value = 'signing_in'; message.value = t('embed.gate.signingIn'); }
        try {
            const kind = mode.value === 'idp_token' ? 'id_token' : 'assertion';
            const value = await host.requestCredential(kind);
            accept(await exchange({ [kind]: value }));
        } catch (e: any) {
            credential.value = null;
            retryable.value = !PERMANENT.includes(e?.data?.error_code);
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
            accept(await exchange({ sso_code: String(data.code || ''), code_verifier: verifier }));
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
        hostName.value = boot.name;
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
