<template>
    <!-- /embed/{id}: the document the BOW-served SDK frames inside a customer
         application. No app shell, no app session: identity comes from a host
         assertion exchanged for an embed session (composables/useEmbedSession),
         the host talks over a checked postMessage handshake
         (composables/useEmbedHost), and everything else is the shared compact
         chat. `?preview=1` is the settings page looking at itself. -->
    <div class="h-dvh w-screen overflow-hidden bg-transparent" data-testid="embed-page">
        <EmbedChat
            v-if="bootstrap"
            ref="chatEl"
            :branding="branding"
            :mode="mode"
            :open="open"
            :auth-state="session.authState.value"
            :auth-error="session.authError.value"
            :user-name="session.session.value?.user?.name || ''"
            :agents="session.session.value?.agents || []"
            :allow-agent-selection="!!session.session.value?.allow_agent_selection"
            :selected-agent-ids="selectedAgentIds"
            :messages="messages"
            :is-streaming="isStreaming"
            :history-loaded="historyLoaded"
            @toggle="setOpen(!open)"
            @close="setOpen(false)"
            @send="send"
            @stop="stop"
            @approve="approve"
            @retry-auth="retryAuth"
            @select-agents="selectedAgentIds = $event" />
        <div v-else-if="fatal" class="h-full flex items-center justify-center px-6 text-center text-xs text-gray-500" data-testid="embed-fatal">
            {{ fatal }}
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import EmbedChat from '~/components/embed/EmbedChat.vue'
import { useEmbedSession, EmbedApiError } from '~/composables/useEmbedSession'
import { useEmbedHost, type HostInit } from '~/composables/useEmbedHost'
import {
    applyCompactEvent, newAssistantMessage, newUserMessage, readSseStream, sortByCreated,
} from '~/composables/useCompactChat'

definePageMeta({ layout: false, auth: false })

const route = useRoute()
const config = useRuntimeConfig()
const nuxtApp = useNuxtApp()
const { locale: i18nLocale } = useI18n()
const colorMode = useColorMode()

const embedId = String(route.params.id || '')
const preview = route.query.preview === '1'
const session = useEmbedSession(embedId, config.public.baseURL as string)
const bootstrap = session.bootstrap
const fatal = ref('')

const mode = ref<'floating' | 'inline'>(route.query.mode === 'inline' ? 'inline' : 'floating')
const open = ref(false)
const messages = ref<any[]>([])
const isStreaming = ref(false)
const selectedAgentIds = ref<string[]>([])
const chatEl = ref<any>(null)

// Host-provided task data, replaced wholesale by each update ({} clears).
let hostContext: Record<string, any> | null = null
let hostMetadata: Record<string, any> | null = null
let hostLocale: string | null = null
let hostTheme: 'light' | 'dark' | null = null
let streamAbort: AbortController | null = null
let activeSystemCompletionId: string | null = null
const historyLoaded = ref(false)
let authRequestSeq = 0
const pendingAssertions = new Map<string, { resolve: (a: string) => void; reject: (e: Error) => void }>()

const branding = computed(() => ({
    display_name: embedId,
    accent_color: '#2563eb',
    theme: 'light',
    greeting: '',
    suggestions: [],
    launcher_label: '',
    logo_url: null,
    ...(bootstrap.value?.branding || {}),
}))

// ── host handshake ────────────────────────────────────────────────────────

const host = useEmbedHost(() => bootstrap.value?.allowed_origins || [], {
    onInit(init: HostInit) {
        mode.value = init.mode
        hostContext = init.context
        hostMetadata = init.metadata
        hostLocale = init.locale
        hostTheme = init.theme
        applyLocale()
        applyTheme()
        if (init.mode === 'inline' || init.open) setOpen(true)
    },
    onAssertion(requestId, assertion, error) {
        const pending = pendingAssertions.get(requestId)
        if (!pending) return
        pendingAssertions.delete(requestId)
        if (assertion) pending.resolve(assertion)
        else pending.reject(new Error(error || 'assertion_failed'))
    },
    onContext(context) { hostContext = context },
    onMetadata(metadata) { hostMetadata = metadata },
    onOpen(next) { setOpen(next) },
    async onLogout() {
        await logout()
        host.post('bow:logged-out', {})
    },
    onLocale(locale) { hostLocale = locale; applyLocale() },
})

function requestAssertionFromHost(): Promise<string> {
    const requestId = `a${++authRequestSeq}-${Math.random().toString(36).slice(2, 10)}`
    return new Promise<string>((resolve, reject) => {
        pendingAssertions.set(requestId, { resolve, reject })
        host.post('bow:auth-request', { requestId })
        setTimeout(() => {
            if (pendingAssertions.has(requestId)) {
                pendingAssertions.delete(requestId)
                reject(new EmbedApiError(0, 'host_timeout', 'The host application did not provide a sign-in assertion'))
            }
        }, 30_000)
    })
}

// ── session ───────────────────────────────────────────────────────────────

async function previewSession() {
    // The settings page looking at itself: the admin's own BOW session.
    const { token } = useAuth()
    const { ensureOrganization } = useOrganization()
    const org = await ensureOrganization()
    const res = await fetch(`${config.public.baseURL}/embed/${encodeURIComponent(embedId)}/session/preview`, {
        method: 'POST',
        headers: { Authorization: String(token.value || ''), 'X-Organization-Id': org?.id || '' },
    })
    if (!res.ok) {
        let body: any = null
        try { body = await res.json() } catch { /* ignore */ }
        throw new EmbedApiError(res.status, body?.error_code || `http.${res.status}`, body?.detail || res.statusText)
    }
    return res.json()
}

session.setAuthenticator(async () => {
    if (preview) return previewSession()
    const assertion = await requestAssertionFromHost()
    return session.exchangeAssertion(assertion)
})

watch(session.authState, (state) => {
    host.post('bow:auth', { status: state, user: state === 'authenticated' ? { name: session.session.value?.user?.name || '' } : null })
    if (state === 'authenticated' && !historyLoaded.value) loadHistory()
})

async function ensureSignedIn(): Promise<boolean> {
    if (session.authState.value === 'authenticated' && session.hasToken) return true
    if (session.authState.value === 'disabled') return false
    return session.authenticate()
}

async function retryAuth() {
    historyLoaded.value = false
    await session.authenticate()
}

async function logout() {
    stop()
    await session.logout()
    messages.value = []
    historyLoaded.value = false
    activeSystemCompletionId = null
    // A new employee may sign in next: the panel shows "sign in" until then.
    session.authState.value = 'required'
}

// ── appearance ────────────────────────────────────────────────────────────

const RTL = new Set(['he', 'ar', 'fa', 'ur'])
async function applyLocale() {
    const fixed = bootstrap.value?.branding?.locale as string | undefined
    const wanted = fixed || hostLocale || null
    if (!wanted || wanted === i18nLocale.value) { applyDir(i18nLocale.value); return }
    const load = (nuxtApp as any).$loadLocaleMessages as ((c: string) => Promise<boolean>) | undefined
    if (load && await load(wanted)) {
        // Set the locale directly (not $setLocale): the embed must not persist a
        // choice into this origin's localStorage, which the main app shares.
        ;(i18nLocale as any).value = wanted
    }
    applyDir(i18nLocale.value)
}
function applyDir(code: string) {
    document.documentElement.setAttribute('lang', code)
    document.documentElement.setAttribute('dir', RTL.has(code) ? 'rtl' : 'ltr')
}

function applyTheme() {
    const theme = (bootstrap.value?.branding?.theme as string) || 'light'
    let dark = false
    if (theme === 'dark') dark = true
    else if (theme === 'auto') dark = hostTheme ? hostTheme === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches
    // Forced, not a preference: a preference would be persisted into this
    // origin's storage and leak into the main app for the same browser.
    try { (colorMode as any).forced = true; (colorMode as any).value = dark ? 'dark' : 'light' } catch { /* plugin absent */ }
    document.documentElement.classList.toggle('dark', dark)
}

// ── open / close ──────────────────────────────────────────────────────────

function setOpen(next: boolean) {
    if (mode.value === 'inline') next = true
    const changed = open.value !== next
    open.value = next
    if (changed || next) host.post('bow:state', { open: next })
    if (next) {
        ensureSignedIn()
        nextTick(() => chatEl.value?.focus?.())
    }
}

// ── thread ────────────────────────────────────────────────────────────────

async function loadHistory() {
    try {
        const res = await session.authedFetch('/chat/completions?limit=40')
        const data = await res.json()
        messages.value = sortByCreated(data.completions || [])
        historyLoaded.value = true
        const last = messages.value[messages.value.length - 1]
        if (last && last.role !== 'user' && last.status === 'in_progress') reconnect(last)
    } catch (e) {
        console.warn('[bow-embed] history failed', e)
    }
}

async function reconnect(assistantMsg: any) {
    // A turn was still running when the page (re)loaded: re-attach to it.
    isStreaming.value = true
    activeSystemCompletionId = assistantMsg.id
    streamAbort = new AbortController()
    try {
        const res = await session.authedFetch(`/chat/completions/${encodeURIComponent(assistantMsg.id)}/stream`, { signal: streamAbort.signal })
        await readSseStream(res, (evt) => applyCompactEvent(evt, assistantMsg), streamAbort.signal)
    } catch (e: any) {
        if (e?.name !== 'AbortError') assistantMsg.status = 'error'
    } finally {
        finishStream()
    }
}

async function send(content: string) {
    content = (content || '').trim()
    if (!content || isStreaming.value) return
    if (!(await ensureSignedIn())) return
    isStreaming.value = true
    const userMsg = newUserMessage(content)
    const assistantMsg = reactive(newAssistantMessage())
    messages.value.push(userMsg, assistantMsg)
    streamAbort = new AbortController()
    const idempotencyKey = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    try {
        const body: Record<string, any> = { content, idempotency_key: idempotencyKey }
        if (hostContext && Object.keys(hostContext).length) body.context = hostContext
        if (hostMetadata && Object.keys(hostMetadata).length) body.metadata = hostMetadata
        if (selectedAgentIds.value.length) body.data_source_ids = selectedAgentIds.value
        const res = await session.authedFetch('/chat/completions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
            body: JSON.stringify(body),
            signal: streamAbort.signal,
        })
        await readSseStream(res, (evt) => {
            applyCompactEvent(evt, assistantMsg, userMsg)
            if (evt?.event === 'completion.started' && evt.data?.system_completion_id) {
                activeSystemCompletionId = evt.data.system_completion_id
            }
        }, streamAbort.signal)
    } catch (e: any) {
        if (e?.name === 'AbortError') {
            assistantMsg.status = 'stopped'
        } else {
            assistantMsg.status = 'error'
            host.post('bow:error', { code: e?.code || 'stream_failed', message: e?.message || String(e) })
            console.error('[bow-embed] send failed', e)
        }
    } finally {
        finishStream()
        // History is authoritative (final block content, real ids).
        if (session.authState.value === 'authenticated') await loadHistory()
    }
}

function finishStream() {
    isStreaming.value = false
    streamAbort = null
    activeSystemCompletionId = null
}

async function stop() {
    const id = activeSystemCompletionId
    streamAbort?.abort()
    if (id) {
        try { await session.authedFetch(`/chat/completions/${encodeURIComponent(id)}/cancel`, { method: 'POST' }) } catch { /* already finished */ }
    }
}

async function approve(payload: { message: any; confirmationId: string; approved: boolean }) {
    const completionId = payload.message?.system_completion_id || payload.message?.id
    try {
        await session.authedFetch(
            `/chat/completions/${encodeURIComponent(completionId)}/mcp_tool_confirmations/${encodeURIComponent(payload.confirmationId)}`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ approved: payload.approved }) },
        )
    } catch (e) {
        console.warn('[bow-embed] approval failed', e)
    }
}

// ── lifecycle ─────────────────────────────────────────────────────────────

onMounted(async () => {
    try {
        await session.loadBootstrap()
    } catch (e: any) {
        fatal.value = e?.status === 404 ? 'This assistant is not available.' : 'Could not load the assistant.'
        return
    }
    applyTheme()
    applyLocale()
    if (preview || !host.embedded) {
        // Opened directly (settings preview, or a developer's tab): no host to
        // wait for. Preview authenticates as the signed-in admin.
        if (preview) setOpen(true)
        else session.authState.value = 'required'
    }
    host.start()
})

onBeforeUnmount(() => {
    streamAbort?.abort()
    host.stop()
    session.dispose()
})
</script>
