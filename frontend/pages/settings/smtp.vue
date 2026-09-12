<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900 dark:text-white">
      SMTP Server
      <p class="text-sm text-gray-500 dark:text-gray-400 font-normal mb-6">
        The server used to send your organization's <strong>system emails</strong> —
        report shares, scheduled‑report results, invites, welcome and account emails.
        When enabled, it overrides the SMTP server configured globally.
      </p>
    </h2>

    <!-- Which transport system mail is actually leaving through right now. -->
    <div v-if="loaded" class="md:w-2/3 mb-4 rounded-lg border px-3 py-2.5 flex items-start gap-2 text-xs"
      :class="activeBanner.class">
      <UIcon :name="activeBanner.icon" class="w-4 h-4 shrink-0 mt-px" />
      <span data-testid="smtp-active-source"><strong>{{ activeBanner.title }}</strong> {{ activeBanner.detail }}</span>
    </div>

    <div class="bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 rounded-lg p-3 my-3 text-xs text-blue-800 dark:text-blue-200 md:w-2/3">
      <strong>SMTP Server vs AI Mailbox.</strong> This SMTP server only sends
      <em>system</em> notifications. It is <em>not</em> used by the AI analyst —
      the analyst's replies and answers always come from the separate
      <strong>AI Mailbox</strong>. Configure them independently.
    </div>

    <form class="md:w-2/3 mt-4" @submit.prevent="save">
      <!-- Enable/disable is its own switch: blanking the host to "turn it off"
           used to throw the whole configuration away. -->
      <div class="flex items-start gap-3 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2.5 mb-4">
        <UToggle v-model="form.enabled" data-testid="smtp-enabled-toggle" class="mt-0.5" />
        <div class="text-sm">
          <div class="font-medium text-gray-900 dark:text-white">Use a custom SMTP server</div>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            <template v-if="form.enabled">
              System email is sent through the server below.
            </template>
            <template v-else-if="serverState.global_configured">
              Off — system email falls back to the globally configured SMTP server.
              Your settings below are kept.
            </template>
            <template v-else>
              Off — and no SMTP server is configured globally, so <strong>no system email
              will be sent at all</strong>. Your settings below are kept.
            </template>
          </p>
        </div>
      </div>

      <div class="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label class="block text-sm font-medium mb-1">From name</label>
          <input v-model="form.from_name" type="text" data-testid="smtp-from-name" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" placeholder="Acme" />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">
            From address
            <span v-if="form.enabled" class="text-red-500">*</span>
          </label>
          <input v-model="form.from_address" type="email" data-testid="smtp-from-address" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" placeholder="noreply@acme.com" />
        </div>
      </div>
      <div class="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label class="block text-sm font-medium mb-1">
            Host
            <span v-if="form.enabled" class="text-red-500">*</span>
          </label>
          <input v-model="form.host" type="text" data-testid="smtp-host" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" placeholder="smtp.acme.com" />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Port</label>
          <input v-model.number="form.port" type="number" data-testid="smtp-port" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" />
        </div>
      </div>
      <div class="grid grid-cols-2 gap-3 mb-1">
        <div>
          <label class="block text-sm font-medium mb-1">Username <span class="text-gray-400 font-normal">(optional)</span></label>
          <input v-model="form.username" type="text" data-testid="smtp-username" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Password <span class="text-gray-400 font-normal">(optional)</span></label>
          <input v-model="form.password" type="password" data-testid="smtp-password" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white"
            :placeholder="passwordSet ? '•••••••• (unchanged)' : ''" />
          <p v-if="passwordSet" class="text-xs text-gray-500 dark:text-gray-400 mt-1">Leave blank to keep the saved password.</p>
        </div>
      </div>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">Leave username &amp; password empty for an open relay that doesn't require authentication.</p>
      <div class="mb-3">
        <label class="block text-sm font-medium mb-1">Security</label>
        <select v-model="form.security" data-testid="smtp-security" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white">
          <option value="starttls">STARTTLS (587)</option>
          <option value="ssl">SSL/TLS (465)</option>
          <option value="none">None</option>
        </select>
      </div>
      <label v-if="form.security !== 'none'" class="flex items-center gap-2 mb-4 cursor-pointer">
        <UToggle v-model="form.validate_certs" />
        <span class="text-sm text-gray-700 dark:text-gray-300">Validate TLS certificates</span>
        <span class="text-xs text-gray-400 dark:text-gray-600">— turn off for self-signed / internal-CA relays</span>
      </label>

      <div class="flex items-center gap-2">
        <button type="submit" :disabled="busy" data-testid="smtp-save"
          class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md disabled:opacity-50">
          {{ saving ? 'Saving…' : 'Save' }}
        </button>
        <button type="button" v-if="form.enabled" :disabled="busy" @click="test" data-testid="smtp-test"
          class="border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm px-3 py-1.5 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">
          {{ testing ? 'Sending…' : 'Save & send test email' }}
        </button>
        <span v-if="form.enabled && currentUserEmail" class="text-xs text-gray-400 dark:text-gray-500">
          sends to {{ currentUserEmail }}
        </span>
      </div>

      <div v-if="visibleTestResult" data-testid="smtp-test-result"
        class="text-sm mt-3 rounded-md border px-3 py-2 flex items-start gap-2"
        :class="visibleTestResult.ok
          ? 'border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-950 text-green-800 dark:text-green-200'
          : 'border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950 text-red-800 dark:text-red-200'">
        <UIcon :name="visibleTestResult.ok ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" class="w-4 h-4 shrink-0 mt-0.5" />
        <div>
          <div class="font-medium">{{ visibleTestResult.text }}</div>
          <div v-if="visibleTestResult.detail" class="text-xs mt-0.5 opacity-90 break-words">{{ visibleTestResult.detail }}</div>
        </div>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, computed, nextTick, onMounted } from 'vue'

definePageMeta({ auth: true, permissions: ['manage_settings'], layout: 'settings' })

const toast = useToast()
const { data: authData } = useAuth()
const currentUserEmail = computed(() => (authData.value as any)?.email || '')

const form = reactive({
  enabled: false,
  host: '', port: 587, security: 'starttls',
  username: '', password: '', from_address: '', from_name: '', validate_certs: true,
})
const serverState = reactive({ active_source: 'none', global_configured: false, host: '' })
const loaded = ref(false)
const passwordSet = ref(false)
const saving = ref(false)
const testing = ref(false)
const busy = computed(() => saving.value || testing.value)
const testResult = ref<{ ok: boolean; text: string; detail?: string } | null>(null)

// A green "delivered" line sitting next to fields the admin has since edited is
// the stale-success problem in miniature. Rather than watching the form (which
// races with the state refresh a successful save performs), stamp the result
// with the configuration it describes and show it only while that still matches.
const formSignature = computed(() => JSON.stringify([
  form.enabled, form.host.trim(), form.port, form.security,
  form.username, form.from_address, form.from_name, form.validate_certs,
  !!form.password,
]))
const testedSignature = ref<string | null>(null)
const visibleTestResult = computed(
  () => (testedSignature.value === formSignature.value ? testResult.value : null),
)

/** Show ``result`` against the configuration currently in the form. */
async function showResult(result: { ok: boolean; text: string; detail?: string } | null) {
  // Let any pending reactive updates (e.g. the post-save state refresh) settle
  // first, so the signature we stamp is the one the admin is looking at.
  await nextTick()
  testResult.value = result
  testedSignature.value = result ? formSignature.value : null
}

const activeBanner = computed(() => {
  if (serverState.active_source === 'org_smtp') {
    return {
      class: 'border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-950 text-green-800 dark:text-green-200',
      icon: 'i-heroicons-check-circle',
      title: 'System email uses this SMTP server.',
      detail: `Sending via ${serverState.host || 'your relay'}.`,
    }
  }
  if (serverState.active_source === 'global') {
    return {
      class: 'border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950 text-amber-800 dark:text-amber-200',
      icon: 'i-heroicons-information-circle',
      title: 'System email uses the globally configured SMTP server.',
      detail: 'Enable a custom SMTP server below to send from your own relay instead.',
    }
  }
  return {
    class: 'border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950 text-red-800 dark:text-red-200',
    icon: 'i-heroicons-exclamation-triangle',
    title: 'No SMTP server is configured.',
    detail: 'Invites, report shares and scheduled-report emails are not being sent.',
  }
})

function applyServerState(s: any) {
  form.enabled = !!s.enabled
  form.host = s.host || ''
  form.port = s.port || 587
  form.security = s.security || 'starttls'
  form.username = s.username || ''
  form.from_address = s.from_address || ''
  form.from_name = s.from_name || ''
  form.validate_certs = s.validate_certs !== false
  passwordSet.value = !!s.password_set
  serverState.active_source = s.active_source || 'none'
  serverState.global_configured = !!s.global_configured
  serverState.host = s.host || ''
}

async function load() {
  const res = await useMyFetch('/api/organization/smtp')
  const s = res.data.value as any
  if (s) applyServerState(s)
  loaded.value = true
}

onMounted(async () => {
  try { await load() } catch { loaded.value = true }
})

function payload() {
  const p: any = {
    enabled: form.enabled, host: form.host.trim(), port: form.port, security: form.security,
    username: form.username, from_address: form.from_address, from_name: form.from_name,
    validate_certs: form.validate_certs,
  }
  if (form.password) p.password = form.password  // only send when (re)setting
  return p
}

/** Persist the form. Returns true only when the server actually accepted it. */
async function persist(): Promise<boolean> {
  const res = await useMyFetch('/api/organization/smtp', { method: 'PUT', body: payload() })
  if (res.status.value !== 'success') {
    const detail = (res.error.value as any)?.data?.detail || 'Could not save the SMTP settings'
    toast.add({ title: 'Failed to save SMTP', description: detail, color: 'red' })
    await showResult({ ok: false, text: 'Not saved', detail })
    return false
  }
  const s = res.data.value as any
  if (s) applyServerState(s)
  form.password = ''
  return true
}

async function save() {
  saving.value = true
  await showResult(null)
  try {
    if (await persist()) toast.add({ title: 'SMTP saved', color: 'green' })
  } finally {
    saving.value = false
  }
}

async function test() {
  saving.value = true
  testing.value = true
  await showResult(null)
  try {
    // Save first, and *stop* if the save failed — otherwise the test probes
    // whatever was stored before and reports success for settings that were
    // never persisted.
    if (!await persist()) return

    const res = await useMyFetch('/api/organization/smtp/test', { method: 'POST', body: {} })
    if (res.status.value !== 'success') {
      const detail = (res.error.value as any)?.data?.detail || 'The test request failed'
      await showResult({ ok: false, text: 'Test failed', detail })
      return
    }
    const data = res.data.value as any
    // The send just proved which transport is live; refresh the banner with it
    // before stamping the result so both describe the same state.
    await load()
    if (data?.success) {
      await showResult({
        ok: true,
        text: `Test email delivered to ${data.recipient}`,
        detail: `Sent via ${sourceLabel(data.source)}${data.from_address ? ` as ${data.from_address}` : ''}. `
          + 'Check your inbox to confirm it arrived.',
      })
    } else {
      const { text, hint } = failureCopy(data?.stage)
      await showResult({
        ok: false,
        text,
        detail: `${data?.error || 'Unknown error'} — ${hint} (transport: ${sourceLabel(data?.source)})`,
      })
    }
  } finally {
    saving.value = false
    testing.value = false
  }
}

/** Name the step that failed, and what to look at. "Rejected" is wrong for a
 * server that never answered — the stage is what tells an admin where to look. */
function failureCopy(stage?: string): { text: string; hint: string } {
  switch (stage) {
    case 'connect':
      return {
        text: 'Could not reach the mail server',
        hint: 'check the host and port, and that outbound SMTP is not blocked by a firewall',
      }
    case 'tls':
      return {
        text: 'TLS negotiation failed',
        hint: 'try a different Security setting, or turn off certificate validation for an internal relay',
      }
    case 'auth':
      return {
        text: 'The mail server rejected the credentials',
        hint: 'check the username and password',
      }
    case 'sender':
      return {
        text: 'The mail server refused the From address',
        hint: 'the relay will not send as this address — use one it is authorised for',
      }
    case 'recipient':
      return {
        text: 'The mail server refused to relay to that address',
        hint: 'the relay does not accept mail for this recipient',
      }
    case 'config':
      return {
        text: 'The SMTP settings are incomplete',
        hint: 'fill in the missing field and try again',
      }
    default:
      return {
        text: 'The mail server rejected the message',
        hint: 'see the server response above',
      }
  }
}

function sourceLabel(source?: string) {
  if (source === 'org_smtp') return 'this SMTP server'
  if (source === 'global') return 'the globally configured SMTP server'
  if (source === 'ai_mailbox') return 'the AI mailbox'
  return 'no transport'
}
</script>
