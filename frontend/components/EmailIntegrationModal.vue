<template>
  <div class="p-4">
    <div class="flex items-center gap-2 mb-2">
      <UIcon name="i-heroicons-envelope" class="w-5 h-5 text-gray-700" />
      <h1 class="text-lg font-semibold">AI Mailbox</h1>
    </div>
    <p class="text-sm text-gray-500">
      The AI analyst's own mailbox — it sends answers/replies from here and
      (optionally) receives questions here. This is separate from the
      <strong>SMTP Server</strong>, which only sends system notifications
      (shares, scheduled reports, invites) and is never used by the analyst.
    </p>
    <hr class="my-4" />

    <!-- Connected view -->
    <div v-if="integrated" class="mb-4">
      <p class="text-green-600 mb-4">Email is currently connected.</p>
      <div class="bg-gray-50 rounded-lg p-4 mb-4">
        <h3 class="text-sm font-medium text-gray-700 mb-3">Details</h3>
        <div class="space-y-2 text-sm">
          <div class="flex justify-between"><span class="text-gray-600">From:</span>
            <span class="font-medium">{{ cfg?.from_name }} &lt;{{ cfg?.from_address }}&gt;</span></div>
          <div class="flex justify-between"><span class="text-gray-600">Auth:</span>
            <span class="font-mono text-xs">{{ authLabel(cfg?.auth_type) }}</span></div>
          <div class="flex justify-between"><span class="text-gray-600">Capabilities:</span>
            <span class="font-mono text-xs">{{ (cfg?.capabilities || ['send']).join(' + ') }}</span></div>
          <div v-if="cfg?.inbound_enabled" class="flex justify-between"><span class="text-gray-600">Allowed domains:</span>
            <span class="font-mono text-xs">{{ (cfg?.allowed_domains || []).join(', ') || 'any (auth only)' }}</span></div>
          <div class="flex justify-between"><span class="text-gray-600">Connected:</span>
            <span class="font-medium">{{ formatDate(integrationData?.created_at) }}</span></div>
        </div>
      </div>
      <div class="flex gap-2">
        <UButton color="gray" variant="soft" :loading="testing" @click="test">Test connection</UButton>
        <UButton color="red" variant="soft" @click="disconnect">Disconnect</UButton>
      </div>
      <p v-if="testResult" :class="testResult.ok ? 'text-green-600' : 'text-red-600'" class="text-sm mt-2 flex items-start gap-1">
        <UIcon :name="testResult.ok ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" class="w-4 h-4 shrink-0 mt-0.5" />
        <span>{{ testResult.text }}</span>
      </p>
    </div>

    <!-- Setup form -->
    <div v-else class="md:flex md:gap-6">
      <form @submit.prevent="connect" class="md:flex-1 md:min-w-0">
        <!-- Auth method selector -->
        <label class="block text-sm font-medium mb-2">How should BOW connect to the mailbox?</label>
        <div class="grid grid-cols-3 gap-2 mb-4">
          <button v-for="opt in authOptions" :key="opt.value" type="button" @click="authType = opt.value"
            :class="[
              'flex flex-col items-center justify-center gap-2 border rounded-lg py-3 px-2 transition',
              authType === opt.value
                ? 'border-blue-500 ring-1 ring-blue-500 bg-blue-50'
                : 'border-gray-200 hover:border-gray-300 bg-white',
            ]">
            <img v-if="opt.img" :src="opt.img" :alt="opt.label" class="w-6 h-6" />
            <UIcon v-else :name="opt.icon!" class="w-6 h-6 text-gray-600" />
            <span :class="['text-xs font-medium text-center leading-tight', authType === opt.value ? 'text-blue-700' : 'text-gray-700']">{{ opt.label }}</span>
          </button>
        </div>

        <!-- Mailbox identity (all methods) -->
        <div class="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label class="block text-sm font-medium mb-1">From name</label>
            <input v-model="fromName" type="text" class="w-full border rounded px-2 py-1" placeholder="Acme Analyst" />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">Mailbox address</label>
            <input v-model="fromAddress" type="email" class="w-full border rounded px-2 py-1" placeholder="analyst@acme.com" :required="authType !== 'password'" />
          </div>
        </div>

        <!-- Password fields -->
        <template v-if="authType === 'password'">
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div><label class="block text-sm font-medium mb-1">SMTP host</label>
              <input v-model="smtpHost" type="text" class="w-full border rounded px-2 py-1" placeholder="smtp.acme.com" required /></div>
            <div><label class="block text-sm font-medium mb-1">SMTP port</label>
              <input v-model.number="smtpPort" type="number" class="w-full border rounded px-2 py-1" /></div>
          </div>
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div><label class="block text-sm font-medium mb-1">SMTP username</label>
              <input v-model="smtpUsername" type="text" class="w-full border rounded px-2 py-1" required /></div>
            <div><label class="block text-sm font-medium mb-1">SMTP password</label>
              <input v-model="smtpPassword" type="password" class="w-full border rounded px-2 py-1" required /></div>
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">SMTP security</label>
            <select v-model="smtpSecurity" class="w-full border rounded px-2 py-1">
              <option value="starttls">STARTTLS (587)</option>
              <option value="ssl">SSL/TLS (465)</option>
              <option value="none">None (sandbox/relay)</option>
            </select>
          </div>
        </template>

        <!-- Microsoft 365 fields -->
        <template v-else-if="authType === 'microsoft'">
          <p class="text-xs text-gray-500 mb-2">Hosts default to Office 365. Provide your Entra app (daemon) credentials:</p>
          <div class="mb-3"><label class="block text-sm font-medium mb-1">Directory (tenant) ID</label>
            <input v-model="msTenantId" type="text" class="w-full border rounded px-2 py-1" required /></div>
          <div class="mb-3"><label class="block text-sm font-medium mb-1">Application (client) ID</label>
            <input v-model="msClientId" type="text" class="w-full border rounded px-2 py-1" required /></div>
          <div class="mb-4"><label class="block text-sm font-medium mb-1">Client secret</label>
            <input v-model="msClientSecret" type="password" class="w-full border rounded px-2 py-1" required /></div>
        </template>

        <!-- Google Workspace fields -->
        <template v-else-if="authType === 'google'">
          <p class="text-xs text-gray-500 mb-2">Paste the service‑account JSON key (with domain‑wide delegation authorized for the mailbox):</p>
          <textarea v-model="googleSaJson" rows="6" class="w-full border rounded px-2 py-1 font-mono text-xs" placeholder='{ "type": "service_account", ... }' required></textarea>
        </template>

        <hr class="my-4" />

        <!-- Receive inbound email (always enabled) -->
        <div class="mb-3">
          <span class="text-sm font-semibold text-gray-800">Receive email as a channel</span>
        </div>

        <div>
          <!-- IMAP host/port only needed for the password method; OAuth hosts are defaulted -->
          <template v-if="authType === 'password'">
            <div class="grid grid-cols-2 gap-3 mb-3">
              <div><label class="block text-sm font-medium mb-1">IMAP host</label>
                <input v-model="imapHost" type="text" class="w-full border rounded px-2 py-1" placeholder="imap.acme.com" /></div>
              <div><label class="block text-sm font-medium mb-1">IMAP port</label>
                <input v-model.number="imapPort" type="number" class="w-full border rounded px-2 py-1" /></div>
            </div>
            <div class="grid grid-cols-2 gap-3 mb-3">
              <div><label class="block text-sm font-medium mb-1">IMAP username</label>
                <input v-model="imapUsername" type="text" class="w-full border rounded px-2 py-1" /></div>
              <div><label class="block text-sm font-medium mb-1">IMAP password</label>
                <input v-model="imapPassword" type="password" class="w-full border rounded px-2 py-1" /></div>
            </div>
          </template>
          <div class="mb-3">
            <label class="block text-sm font-medium mb-1">Allowed sender domains</label>
            <input v-model="allowedDomains" type="text" class="w-full border rounded px-2 py-1" placeholder="acme.com, subsidiary.com" />
            <p class="text-xs text-gray-500 mt-1">Comma‑separated. Blank = rely on an internal‑only mailbox + auth checks.</p>
          </div>
          <label class="flex items-center gap-2 mb-2 cursor-pointer">
            <UToggle v-model="autoLink" color="blue" /><span class="text-sm">Auto‑verify members by email — <span class="text-gray-500">off (recommended): first email gets a verification link to click</span></span>
          </label>
          <label class="flex items-center gap-2 mb-4 cursor-pointer">
            <UToggle v-model="requireAuthPass" color="blue" /><span class="text-sm">Require DMARC/DKIM pass (recommended)</span>
          </label>
        </div>

        <div class="flex items-center gap-2">
          <button type="button" :disabled="testingForm" @click="testForm"
            class="border border-gray-300 text-gray-700 text-sm px-3 py-1.5 rounded-md hover:bg-gray-50 disabled:opacity-50">
            {{ testingForm ? 'Testing…' : 'Test connection' }}
          </button>
          <button type="submit" :disabled="submitting" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md disabled:opacity-50">
            {{ submitting ? 'Connecting…' : 'Connect' }}
          </button>
        </div>
        <p v-if="testResult" :class="testResult.ok ? 'text-green-600' : 'text-red-600'" class="text-sm mt-2 flex items-start gap-1">
          <UIcon :name="testResult.ok ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" class="w-4 h-4 shrink-0 mt-0.5" />
          <span>{{ testResult.text }}</span>
        </p>
      </form>

      <!-- Right: contextual setup guide -->
      <aside class="md:w-72 md:shrink-0 mt-6 md:mt-0">
        <div class="bg-gray-50 border border-gray-200 rounded-lg p-4">
          <div class="flex items-center gap-2 mb-3">
            <img v-if="activeOption.img" :src="activeOption.img" :alt="activeOption.label" class="w-5 h-5" />
            <UIcon v-else :name="activeOption.icon!" class="w-5 h-5 text-gray-600" />
            <h3 class="text-sm font-semibold text-gray-800">{{ activeOption.label }} setup</h3>
          </div>

          <!-- Microsoft 365 -->
          <ol v-if="authType === 'microsoft'" class="list-decimal list-outside ps-4 text-xs text-gray-600 space-y-2">
            <li>Create the mailbox (a shared mailbox is fine — no license needed).</li>
            <li>Entra → <strong>App registrations</strong> → New registration (single tenant). Copy the <strong>tenant ID</strong> + <strong>client ID</strong>.</li>
            <li>API permissions → Office 365 Exchange Online → <strong>Application</strong> → <code>IMAP.AccessAsApp</code> + <code>SMTP.SendAsApp</code> → <strong>Grant admin consent</strong>.</li>
            <li>Certificates &amp; secrets → <strong>New client secret</strong>.</li>
            <li>Exchange PowerShell: <code>New-ServicePrincipal</code>, then <code>Add-MailboxPermission … -AccessRights FullAccess</code> for this mailbox.</li>
          </ol>

          <!-- Google Workspace -->
          <ol v-else-if="authType === 'google'" class="list-decimal list-outside ps-4 text-xs text-gray-600 space-y-2">
            <li>Create the mailbox (a licensed Workspace user).</li>
            <li>Google Cloud → new project → enable the <strong>Gmail API</strong> → create a <strong>service account</strong> → create a <strong>JSON key</strong>.</li>
            <li>Admin console → Security → API controls → <strong>Domain‑wide delegation</strong> → add the SA client ID with scope <code>https://mail.google.com/</code>.</li>
            <li>Paste the JSON key into the form.</li>
          </ol>

          <!-- IMAP / Password -->
          <div v-else class="text-xs text-gray-600 space-y-2">
            <p>Connect any mailbox that speaks plain SMTP + IMAP — on‑prem Exchange, a hosting provider, or a personal app password.</p>
            <ul class="list-disc list-outside ps-4 space-y-1">
              <li>Use an <strong>app password</strong> if the provider enforces MFA (basic auth is blocked on Microsoft 365 / Gmail — use those tiles instead).</li>
              <li>SMTP is usually <code>587</code> (STARTTLS) or <code>465</code> (SSL).</li>
              <li>IMAP is usually <code>993</code> (SSL).</li>
              <li>Toggle <strong>Receive email as a channel</strong> to let the analyst answer inbound mail.</li>
            </ul>
          </div>

          <p class="text-[11px] text-gray-400 mt-3">Use <strong>Test connection</strong> before saving to validate credentials.</p>
        </div>
      </aside>
    </div>

    <button class="absolute top-2 end-2 text-gray-400 hover:text-gray-600" @click="$emit('close')">✕</button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'

const props = defineProps<{
  integrated: boolean
  integrationData?: any
  analystName?: string
  prefillDomains?: string[]
}>()
const emit = defineEmits(['close', 'updated'])
const toast = useToast()

const cfg = computed(() => props.integrationData?.platform_config || null)

type AuthType = 'password' | 'microsoft' | 'google'
const authOptions: { value: AuthType; label: string; img?: string; icon?: string }[] = [
  { value: 'google', label: 'Google Workspace', img: '/icons/google.svg' },
  { value: 'microsoft', label: 'Microsoft 365', img: '/icons/microsoft.svg' },
  { value: 'password', label: 'IMAP / Password', icon: 'i-heroicons-envelope' },
]
const authType = ref<AuthType>('password')
const activeOption = computed(() => authOptions.find((o) => o.value === authType.value) || authOptions[2])

// Mailbox identity
const fromName = ref('Bag of words Analyst')
const fromAddress = ref('')

// Password
const smtpHost = ref('')
const smtpPort = ref(587)
const smtpUsername = ref('')
const smtpPassword = ref('')
const smtpSecurity = ref('starttls')

// Microsoft
const msTenantId = ref('')
const msClientId = ref('')
const msClientSecret = ref('')

// Google
const googleSaJson = ref('')

// Inbound — on by default; the analyst answering inbound mail is the headline use case.
const inboundEnabled = ref(true)
const imapHost = ref('')
const imapPort = ref(993)
const imapUsername = ref('')
const imapPassword = ref('')
const allowedDomains = ref('')
const autoLink = ref(false)  // verify-first by default
const requireAuthPass = ref(true)

const submitting = ref(false)
const testing = ref(false)
const testingForm = ref(false)
const testResult = ref<{ ok: boolean; text: string } | null>(null)

function applyTestResult(res: any) {
  const data = res.data?.value as any
  if (res.status.value === 'success' && data?.success) {
    testResult.value = { ok: true, text: `Connection OK — SMTP ${data.smtp || 'ok'}${data.imap ? `, IMAP ${data.imap}` : ''}` }
  } else {
    const detail = (data?.smtp && data.smtp !== 'ok') ? data.smtp
      : (data?.imap && data.imap !== 'ok') ? data.imap
      : ((res.error?.value as any)?.data?.detail || 'Connection failed — check the credentials')
    testResult.value = { ok: false, text: detail }
  }
}

// Prefill From name from the org's AI analyst name, and Allowed domains from the
// signup policy domains — once, when those values become available, without
// clobbering anything the admin has already typed.
let prefilled = false
function applyPrefill() {
  if (prefilled) return
  const hasData = !!props.analystName || (props.prefillDomains?.length || 0) > 0
  if (!hasData) return
  if (props.analystName) fromName.value = props.analystName
  if (props.prefillDomains?.length) allowedDomains.value = props.prefillDomains.join(', ')
  prefilled = true
}
watch(() => [props.analystName, props.prefillDomains], applyPrefill, { immediate: true })

function authLabel(t?: string) {
  return t === 'microsoft' ? 'Microsoft 365 (OAuth)' : t === 'google' ? 'Google Workspace (service account)' : 'Password'
}

function formatDate(d: string | undefined) {
  if (!d) return 'N/A'
  return new Date(d).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function buildBody() {
  const body: any = {
    auth_type: authType.value,
    from_address: fromAddress.value || smtpUsername.value,
    from_name: fromName.value,
    inbound_enabled: inboundEnabled.value,
    auto_link_by_email: autoLink.value,
    require_auth_pass: requireAuthPass.value,
    allowed_domains: allowedDomains.value.split(',').map((d) => d.trim()).filter(Boolean),
  }
  if (authType.value === 'password') {
    Object.assign(body, {
      smtp_host: smtpHost.value, smtp_port: smtpPort.value, smtp_username: smtpUsername.value,
      smtp_password: smtpPassword.value, smtp_security: smtpSecurity.value,
    })
    if (inboundEnabled.value) {
      Object.assign(body, {
        imap_host: imapHost.value, imap_port: imapPort.value,
        imap_username: imapUsername.value, imap_password: imapPassword.value,
      })
    }
  } else if (authType.value === 'microsoft') {
    Object.assign(body, { ms_tenant_id: msTenantId.value, ms_client_id: msClientId.value, ms_client_secret: msClientSecret.value })
  } else if (authType.value === 'google') {
    body.google_service_account_json = googleSaJson.value
  }
  return body
}

async function testForm() {
  testingForm.value = true
  testResult.value = null
  try {
    const res = await useMyFetch('/api/settings/integrations/email/test', { method: 'POST', body: buildBody() })
    applyTestResult(res)
  } finally {
    testingForm.value = false
  }
}

async function connect() {
  submitting.value = true
  try {
    const res = await useMyFetch('/api/settings/integrations/email', { method: 'POST', body: buildBody() })
    if (res.status.value === 'success') {
      toast.add({ title: 'Email connected', description: 'Email integration successful', color: 'green' })
      emit('updated'); emit('close')
    } else {
      toast.add({ title: 'Failed to connect email', description: (res.error.value as any).data?.detail || (res.error.value as any).message, color: 'red' })
    }
  } finally {
    submitting.value = false
  }
}

async function test() {
  if (!props.integrationData?.id) return
  testing.value = true
  testResult.value = null
  try {
    const res = await useMyFetch(`/api/settings/integrations/${props.integrationData.id}/test`, { method: 'POST' })
    applyTestResult(res)
  } finally {
    testing.value = false
  }
}

async function disconnect() {
  if (!props.integrationData?.id) return
  const res = await useMyFetch(`/api/settings/integrations/${props.integrationData.id}`, { method: 'DELETE' })
  if (res.status.value === 'success') {
    toast.add({ title: 'Email disconnected', description: 'Email integration disconnected', color: 'green' })
    emit('updated'); emit('close')
  } else {
    toast.add({ title: 'Failed to disconnect email', description: (res.error.value as any).data?.detail || (res.error.value as any).message, color: 'red' })
  }
}
</script>
