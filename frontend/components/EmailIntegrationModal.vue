<template>
  <div class="p-4">
    <div class="flex items-center gap-2 mb-2">
      <UIcon name="i-heroicons-envelope" class="w-5 h-5 text-gray-700" />
      <h1 class="text-lg font-semibold">Email Integration</h1>
    </div>
    <p class="text-sm text-gray-500">
      Give the AI analyst its own mailbox. BOW connects to it outbound‑only to
      send replies and (optionally) receive questions.
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
    </div>

    <!-- Setup form -->
    <div v-else>
      <form @submit.prevent="connect">
        <!-- Auth method selector -->
        <label class="block text-sm font-medium mb-1">How should BOW connect to the mailbox?</label>
        <select v-model="authType" class="w-full border rounded px-2 py-1 mb-4">
          <option value="password">Username &amp; password (on‑prem Exchange / app password)</option>
          <option value="microsoft">Microsoft 365 (OAuth app‑only)</option>
          <option value="google">Google Workspace (service account)</option>
        </select>

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

        <!-- Receive toggle (all methods) -->
        <label class="flex items-center gap-2 mb-3 cursor-pointer">
          <UToggle v-model="inboundEnabled" />
          <span class="text-sm font-semibold text-gray-800">Receive email as a channel (optional)</span>
        </label>

        <div v-if="inboundEnabled">
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
            <UToggle v-model="autoLink" /><span class="text-sm">Auto‑link senders to existing members</span>
          </label>
          <label class="flex items-center gap-2 mb-4 cursor-pointer">
            <UToggle v-model="requireAuthPass" /><span class="text-sm">Require DMARC/DKIM pass (recommended)</span>
          </label>
        </div>

        <!-- Admin setup steps (collapsible, per provider) -->
        <div v-if="authType !== 'password'" class="bg-gray-50 border border-gray-200 rounded-lg p-3 mb-4">
          <button type="button" class="text-sm font-medium text-gray-700 flex items-center gap-1" @click="showSteps = !showSteps">
            <UIcon :name="showSteps ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-4 h-4" />
            Admin setup steps ({{ authType === 'microsoft' ? 'Microsoft 365' : 'Google Workspace' }})
          </button>
          <ol v-if="showSteps" class="list-decimal list-inside text-xs text-gray-600 mt-2 space-y-1">
            <template v-if="authType === 'microsoft'">
              <li>Create the mailbox (a shared mailbox is fine — no license).</li>
              <li>Entra → App registrations → New registration (single tenant). Copy the tenant ID + client ID.</li>
              <li>API permissions → Office 365 Exchange Online → Application → <code>IMAP.AccessAsApp</code> + <code>SMTP.SendAsApp</code> → Grant admin consent.</li>
              <li>Certificates &amp; secrets → New client secret.</li>
              <li>Exchange PowerShell: <code>New-ServicePrincipal</code> then <code>Add-MailboxPermission … -AccessRights FullAccess</code> for this mailbox.</li>
            </template>
            <template v-else>
              <li>Create the mailbox (a licensed Workspace user).</li>
              <li>Google Cloud → new project → enable the Gmail API → create a service account → create a JSON key.</li>
              <li>Admin console → Security → API controls → Domain‑wide delegation → add the SA Client ID with scope <code>https://mail.google.com/</code>.</li>
              <li>Paste the JSON key above.</li>
            </template>
          </ol>
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
      </form>
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

const authType = ref<'password' | 'microsoft' | 'google'>('password')
const showSteps = ref(false)

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

// Inbound
const inboundEnabled = ref(false)
const imapHost = ref('')
const imapPort = ref(993)
const imapUsername = ref('')
const imapPassword = ref('')
const allowedDomains = ref('')
const autoLink = ref(true)
const requireAuthPass = ref(true)

const submitting = ref(false)
const testing = ref(false)
const testingForm = ref(false)

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
  try {
    const res = await useMyFetch('/api/settings/integrations/email/test', { method: 'POST', body: buildBody() })
    const data = res.data.value as any
    if (res.status.value === 'success' && data?.success) {
      toast.add({ title: 'Connection OK', description: `SMTP ${data.smtp || 'ok'}${data.imap ? `, IMAP ${data.imap}` : ''}`, color: 'green' })
    } else {
      const detail = data?.smtp && data.smtp !== 'ok' ? data.smtp : (data?.imap || (res.error.value as any)?.data?.detail || 'Check the credentials')
      toast.add({ title: 'Connection failed', description: detail, color: 'red' })
    }
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
  try {
    const res = await useMyFetch(`/api/settings/integrations/${props.integrationData.id}/test`, { method: 'POST' })
    const data = res.data.value as any
    if (res.status.value === 'success' && data?.success) {
      toast.add({ title: 'Connection OK', description: `SMTP ${data.smtp || 'ok'}${data.imap ? `, IMAP ${data.imap}` : ''}`, color: 'green' })
    } else {
      toast.add({ title: 'Connection failed', description: data?.smtp || data?.imap || 'Check credentials', color: 'red' })
    }
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
