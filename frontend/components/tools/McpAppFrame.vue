<template>
  <!-- A <dialog> so full screen can use showModal(): the browser's top layer
       sits above every stacking context (the chat column is one, below the
       sidebar) without moving this node — moving it would reload the app. -->
  <dialog
    v-if="!unavailable"
    ref="containerRef"
    open
    class="mcp-app text-ink"
    :class="[
      fullscreen ? 'mcp-app--fullscreen bg-surface' : 'mt-2',
      !fullscreen && prefersBorder ? 'rounded-lg border border-line overflow-hidden' : '',
    ]"
    :aria-label="title"
    data-testid="mcp-app"
    @cancel.prevent="setDisplayMode('inline')"
  >
    <!-- Fullscreen chrome: the app's name and a way back -->
    <div v-if="fullscreen" class="flex items-center justify-between px-4 py-2 border-b border-line text-chat-2 text-ink-muted">
      <span class="flex items-center gap-1.5 min-w-0">
        <McpIcon class="w-3.5 h-3.5 shrink-0" />
        <span class="truncate">{{ title }}</span>
      </span>
      <UButton
        color="gray"
        variant="ghost"
        size="xs"
        icon="heroicons-arrows-pointing-in"
        data-testid="mcp-app-exit-fullscreen"
        @click="setDisplayMode('inline')"
      >{{ t('tools.mcp.app.exitFullscreen') }}</UButton>
    </div>

    <div v-if="loadError" class="text-chat-2 text-amber-600 dark:text-amber-500 bg-amber-50/60 dark:bg-amber-950/40 rounded px-2 py-1" data-testid="mcp-app-error">
      {{ t('tools.mcp.app.loadFailed') }}<template v-if="loadError !== true">: {{ loadError }}</template>
    </div>
    <div v-else-if="!resource" class="flex items-center gap-1.5 text-chat-2 text-ink-muted px-2 py-3">
      <Spinner class="w-3 h-3" /> {{ t('tools.mcp.app.loading') }}
    </div>

    <iframe
      v-if="resource && !loadError"
      ref="frameRef"
      :key="frameKey"
      :src="frameSrc"
      :sandbox="frameSandbox"
      :allow="resource.allow || undefined"
      :title="title"
      referrerpolicy="no-referrer"
      class="mcp-app__frame block w-full bg-transparent"
      :style="fullscreen ? undefined : { height: `${frameHeight}px` }"
      data-testid="mcp-app-frame"
    />

    <!-- An app asks for something only the user can allow -->
    <div
      v-if="pendingConfirm"
      class="mx-2 my-2 border-s-2 border-amber-300/70 dark:border-amber-600/50 ps-2.5 py-1 space-y-1.5 text-chat-2"
      data-testid="mcp-app-confirm"
    >
      <div class="text-ink-2">
        <template v-if="pendingConfirm.kind === 'tool'">
          {{ t('tools.mcp.app.confirmTool', { tool: pendingConfirm.tool, app: appName }) }}
        </template>
        <template v-else>
          {{ t('tools.mcp.app.confirmLink', { app: appName }) }}
        </template>
      </div>
      <pre
        class="max-h-28 overflow-auto rounded bg-surface-subtle text-chat-2 leading-tight text-ink-muted p-2 m-0 whitespace-pre-wrap break-words font-mono"
        dir="ltr"
      >{{ pendingConfirm.detail }}</pre>
      <div class="flex flex-wrap items-center gap-1.5">
        <UButton
          size="2xs"
          data-testid="mcp-app-confirm-allow"
          @click="resolveConfirm(true)"
        >{{ pendingConfirm.kind === 'tool' ? t('tools.mcp.allowOnce') : t('tools.mcp.app.openLink') }}</UButton>
        <UButton
          size="2xs"
          color="gray"
          variant="ghost"
          data-testid="mcp-app-confirm-deny"
          @click="resolveConfirm(false)"
        >{{ t('tools.mcp.app.cancel') }}</UButton>
      </div>
    </div>

    <div v-if="notice" class="px-2 pb-1.5 pt-1 text-chat-2 text-ink-muted" data-testid="mcp-app-notice">{{ notice }}</div>
  </dialog>
</template>

<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import McpIcon from '~/components/icons/McpIcon.vue'
import Spinner from '~/components/Spinner.vue'
import {
  MCP_APPS_PROTOCOL_VERSION,
  buildHostContext,
  hostCapabilities,
  hostStyleVariables,
  isJsonRpc,
  isNotification,
  isRequest,
  messageText,
  safeExternalUrl,
  sandboxAttr,
  sandboxSrc,
  sandboxTargetOrigin,
  type JsonRpcMessage,
  type McpAppDisplayMode,
} from '~/utils/mcpAppHost'

interface McpAppHost {
  // Post a message into this conversation as the user. Returns false when the
  // chat cannot take one now (a run is in progress).
  sendMessage: (text: string) => boolean
}

const props = defineProps<{
  toolExecutionId: string
  status: string
  connectionName?: string
}>()

const { t, locale } = useResponseI18n()
const colorMode = useColorMode()
const host = inject<McpAppHost | null>('mcpAppHost', null)

interface Resource {
  html: string
  csp: Record<string, string[]>
  allow: string
  prefers_border: boolean | null
  sandbox_url: string
  connection_name?: string
  tool_name?: string
  tool_input: Record<string, any>
  tool_result: Record<string, any>
  can_interact: boolean
}

const containerRef = ref<HTMLElement | null>(null)
const frameRef = ref<HTMLIFrameElement | null>(null)
const resource = ref<Resource | null>(null)
const loadError = ref<string | true | null>(null)
// 404: no app for this call any more (MCP Apps turned off, the call or its
// connection gone). Not an error to show — the tool's text result remains.
const unavailable = ref(false)
const frameHeight = ref(160)
const frameKey = ref(0)
const displayMode = ref<McpAppDisplayMode>('inline')
const fullscreen = computed(() => displayMode.value === 'fullscreen')
const appReady = ref(false)
const notice = ref('')
let toolDataSent = false

const title = computed(() => resource.value?.tool_name
  ? `${resource.value.tool_name}${resource.value.connection_name ? ` · ${resource.value.connection_name}` : ''}`
  : (props.connectionName || 'MCP App'))
// Who is asking, in confirmations: the server the app belongs to.
const appName = computed(() => resource.value?.connection_name || props.connectionName || 'MCP App')
const prefersBorder = computed(() => resource.value?.prefers_border !== false)
const frameSrc = computed(() => resource.value ? sandboxSrc(resource.value.sandbox_url, resource.value.csp) : '')
const frameSandbox = computed(() => resource.value ? sandboxAttr(resource.value.sandbox_url) : 'allow-scripts')
const targetOrigin = computed(() => resource.value ? sandboxTargetOrigin(resource.value.sandbox_url) : '*')

// ── loading ───────────────────────────────────────────────────────────────

async function load() {
  loadError.value = null
  const { data, error } = await useMyFetch(`/mcp_apps/${props.toolExecutionId}/resource`)
  if (error.value) {
    if ((error.value as any)?.statusCode === 404) { unavailable.value = true; return }
    const detail = (error.value as any)?.data?.detail
    loadError.value = typeof detail === 'string' ? detail : true
    return
  }
  resource.value = data.value as Resource
}

// ── messaging ─────────────────────────────────────────────────────────────

function post(msg: JsonRpcMessage) {
  const win = frameRef.value?.contentWindow
  // Plain JSON only: Vue's reactive proxies cannot be structured-cloned, and
  // nothing but JSON-RPC data should cross into the app anyway.
  if (win) win.postMessage(JSON.parse(JSON.stringify(msg)), targetOrigin.value)
}
function respond(id: JsonRpcMessage['id'], result: any) { post({ jsonrpc: '2.0', id, result }) }
function fail(id: JsonRpcMessage['id'], code: number, message: string, data?: any) {
  post({ jsonrpc: '2.0', id, error: { code, message, ...(data !== undefined ? { data } : {}) } })
}
function notify(method: string, params: any = {}) { post({ jsonrpc: '2.0', method, params }) }

function hostContext() {
  return buildHostContext({
    theme: colorMode.value === 'dark' ? 'dark' : 'light',
    locale: String(locale.value || 'en'),
    displayMode: displayMode.value,
    width: containerRef.value?.clientWidth,
    maxHeight: fullscreen.value ? window.innerHeight : 720,
    toolExecutionId: props.toolExecutionId,
    toolName: resource.value?.tool_name,
  })
}

// The tool's input and result go to the app once it says it is initialized,
// and only for a finished call (the spec: tool-input, then tool-result).
function sendToolData() {
  if (!appReady.value || toolDataSent || !resource.value) return
  if (props.status === 'running') return
  toolDataSent = true
  notify('ui/notifications/tool-input', { arguments: resource.value.tool_input || {} })
  if (props.status === 'stopped' || props.status === 'cancelled') {
    notify('ui/notifications/tool-cancelled', { reason: 'The tool call was stopped.' })
  } else {
    notify('ui/notifications/tool-result', resource.value.tool_result || { content: [] })
  }
}

async function rpc(method: string, params: any, confirmed = false) {
  const { data, error } = await useMyFetch(`/mcp_apps/${props.toolExecutionId}/rpc`, {
    method: 'POST',
    body: { method, params, confirmed },
  })
  if (error.value) {
    const status = (error.value as any)?.statusCode
    const detail = (error.value as any)?.data?.detail
    return { error: { code: status === 429 ? -32000 : -32603, message: typeof detail === 'string' ? detail : 'Request failed' } }
  }
  return data.value as { result?: any; error?: { code: number; message: string; data?: any } }
}

// One question at a time; an app asking again while one is open is refused.
interface Confirm { kind: 'tool' | 'link'; tool?: string; detail: string; resolve: (ok: boolean) => void }
const pendingConfirm = ref<Confirm | null>(null)
function askUser(c: Omit<Confirm, 'resolve'>): Promise<boolean> {
  if (pendingConfirm.value) return Promise.resolve(false)
  return new Promise((resolve) => { pendingConfirm.value = { ...c, resolve } })
}
function resolveConfirm(ok: boolean) {
  const c = pendingConfirm.value
  pendingConfirm.value = null
  c?.resolve(ok)
}

async function handleToolsCall(id: JsonRpcMessage['id'], params: any) {
  let res = await rpc('tools/call', params)
  if (res?.error?.message === 'confirmation_required') {
    const d = res.error.data || {}
    const detail = `${d.tool || params?.name}(${JSON.stringify(d.arguments ?? params?.arguments ?? {}, null, 2)})`
    const ok = await askUser({ kind: 'tool', tool: d.tool || params?.name, detail })
    if (!ok) return fail(id, -32000, 'The user declined this action.')
    res = await rpc('tools/call', params, true)
  }
  if (res?.error) return fail(id, res.error.code, res.error.message, res.error.data)
  respond(id, res?.result)
}

async function handleRequest(msg: JsonRpcMessage) {
  const { id, method, params } = msg
  const canInteract = !!resource.value?.can_interact
  switch (method) {
    case 'ui/initialize':
      return respond(id, {
        protocolVersion: MCP_APPS_PROTOCOL_VERSION,
        hostInfo: { name: 'Bag of Words', version: '1' },
        hostCapabilities: hostCapabilities(canInteract),
        hostContext: hostContext(),
      })
    case 'ping':
      return respond(id, {})
    case 'tools/call':
      if (!canInteract) return fail(id, -32000, 'This app is read-only for you.')
      return handleToolsCall(id, params)
    case 'resources/read': {
      if (!canInteract) return fail(id, -32000, 'This app is read-only for you.')
      const res = await rpc('resources/read', params)
      return res?.error ? fail(id, res.error.code, res.error.message) : respond(id, res?.result)
    }
    case 'ui/open-link': {
      const url = safeExternalUrl(params?.url)
      if (!url) return fail(id, -32602, 'Only http and https links can be opened.')
      const ok = await askUser({ kind: 'link', detail: url })
      if (!ok) return fail(id, -32000, 'Link opening denied by user')
      window.open(url, '_blank', 'noopener,noreferrer')
      return respond(id, {})
    }
    case 'ui/message': {
      if (!canInteract || !host) return fail(id, -32000, 'This conversation does not accept messages from apps here.')
      const text = messageText(params)
      if (!text) return fail(id, -32602, 'Only text messages are supported.')
      if (!host.sendMessage(text)) return fail(id, -32000, 'The assistant is busy; try again when it has finished.')
      return respond(id, {})
    }
    case 'ui/update-model-context': {
      if (!canInteract) return fail(id, -32000, 'This app is read-only for you.')
      const { data, error } = await useMyFetch(`/mcp_apps/${props.toolExecutionId}/model_context`, {
        method: 'POST',
        body: { content: params?.content, structuredContent: params?.structuredContent },
      })
      const r = data.value as any
      if (error.value || r?.error) return fail(id, r?.error?.code || -32603, r?.error?.message || 'Could not save the context.')
      notice.value = t('tools.mcp.app.contextShared')
      return respond(id, {})
    }
    case 'ui/request-display-mode': {
      const mode = params?.mode === 'fullscreen' ? 'fullscreen' : 'inline'
      setDisplayMode(mode, false)
      return respond(id, { mode: displayMode.value })
    }
    default:
      return fail(id, -32601, `Method not supported: ${method}`)
  }
}

function handleNotification(msg: JsonRpcMessage) {
  switch (msg.method) {
    case 'ui/notifications/sandbox-proxy-ready':
      if (resource.value) {
        notify('ui/notifications/sandbox-resource-ready', {
          html: resource.value.html,
          allow: resource.value.allow || '',
        })
      }
      return
    case 'ui/notifications/initialized':
      appReady.value = true
      sendToolData()
      return
    case 'ui/notifications/size-changed': {
      // In full screen the frame fills the viewport, so the app reports the
      // viewport back; that must not become its height in the chat.
      if (fullscreen.value) return
      const h = Number(msg.params?.height)
      if (Number.isFinite(h) && h > 0) frameHeight.value = Math.min(Math.max(Math.ceil(h), 60), 720)
      return
    }
    case 'notifications/message':
      // App logging: kept out of the UI, visible to whoever debugs it.
      console.debug('[mcp-app]', msg.params)
      return
    default:
      return
  }
}

function onMessage(ev: MessageEvent) {
  // Only the frame this component owns; the proxy relays the app inside it.
  if (!frameRef.value || ev.source !== frameRef.value.contentWindow) return
  const msg = ev.data
  if (!isJsonRpc(msg)) return
  if (isRequest(msg)) { handleRequest(msg); return }
  if (isNotification(msg)) handleNotification(msg)
}

function setDisplayMode(mode: McpAppDisplayMode, announce = true) {
  if (displayMode.value === mode) return
  displayMode.value = mode
  const el = containerRef.value as HTMLDialogElement | null
  if (el && typeof el.showModal === 'function') {
    // Re-open in the other layer: modal = top layer (full screen), show() =
    // back in the chat flow. The node stays put, so the app keeps its state.
    el.close()
    if (mode === 'fullscreen') el.showModal()
    else el.show()
  }
  if (announce && appReady.value) {
    notify('ui/notifications/host-context-changed', {
      displayMode: mode,
      containerDimensions: hostContext().containerDimensions,
    })
  }
}

watch(() => colorMode.value, () => {
  if (!appReady.value) return
  nextTick(() => notify('ui/notifications/host-context-changed', {
    theme: colorMode.value === 'dark' ? 'dark' : 'light',
    styles: { variables: hostStyleVariables() },
  }))
})
watch(() => props.status, () => sendToolData())

onMounted(() => {
  window.addEventListener('message', onMessage)
  load()
})

onBeforeUnmount(() => {
  // The spec: tell the app before its frame goes away. Nothing waits on the
  // answer here — the component is leaving either way.
  if (appReady.value) post({ jsonrpc: '2.0', id: `teardown-${props.toolExecutionId}`, method: 'ui/resource-teardown', params: { reason: 'The chat closed this view.' } })
  window.removeEventListener('message', onMessage)
  pendingConfirm.value?.resolve(false)
})
</script>

<style scoped>
/* Inline, the dialog is just a block in the chat: undo the UA dialog styles. */
.mcp-app {
  position: static;
  display: block;
  width: auto;
  max-width: none;
  max-height: none;
  margin-inline: 0;
  margin-bottom: 0;
  padding: 0;
  background: transparent;
  color: inherit;
}
.mcp-app:not([class*="border"]) {
  border: 0;
}
.mcp-app--fullscreen {
  position: fixed;
  inset: 0;
  width: 100vw;
  height: 100dvh;
  max-width: 100vw;
  max-height: 100dvh;
  margin: 0;
  display: flex;
  flex-direction: column;
  background: rgb(var(--bow-bg-rgb));
}
.mcp-app--fullscreen::backdrop {
  background: rgb(var(--bow-bg-rgb));
}
.mcp-app--fullscreen .mcp-app__frame {
  flex: 1 1 auto;
  height: auto;
}
.mcp-app__frame {
  border: 0;
  transition: height 0.15s ease;
}
</style>
