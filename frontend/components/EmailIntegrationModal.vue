<template>
  <div class="p-4">
    <div class="flex items-center gap-2 mb-2">
      <UIcon name="i-heroicons-envelope" class="w-5 h-5 text-gray-700" />
      <h1 class="text-lg font-semibold">Email Integration</h1>
    </div>
    <p class="text-sm text-gray-500">
      Use a mailbox as your organization's outbound email, and optionally let
      users email the AI analyst and get answers back.
    </p>
    <hr class="my-4" />

    <!-- Connected view -->
    <div v-if="integrated" class="mb-4">
      <p class="text-green-600 mb-4">Email is currently connected.</p>

      <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4">
        <h3 class="text-sm font-medium text-blue-800 mb-2">What this does</h3>
        <ul class="text-sm text-blue-700 space-y-1 list-disc list-inside">
          <li>Outbound email (notifications, shares, scheduled reports) is sent from this mailbox, overriding the global SMTP config.</li>
          <li v-if="cfg?.inbound_enabled">Users can email <strong>{{ cfg?.from_address }}</strong> and get answers from the analyst.</li>
          <li v-else>Receiving is off — add IMAP details below to turn the analyst into an email contact.</li>
        </ul>
      </div>

      <div class="bg-gray-50 rounded-lg p-4 mb-4">
        <h3 class="text-sm font-medium text-gray-700 mb-3">Details</h3>
        <div class="space-y-2 text-sm">
          <div class="flex justify-between">
            <span class="text-gray-600">From:</span>
            <span class="font-medium">{{ cfg?.from_name }} &lt;{{ cfg?.from_address }}&gt;</span>
          </div>
          <div class="flex justify-between">
            <span class="text-gray-600">Capabilities:</span>
            <span class="font-mono text-xs">{{ (cfg?.capabilities || []).join(' + ') || 'send' }}</span>
          </div>
          <div class="flex justify-between">
            <span class="text-gray-600">SMTP:</span>
            <span class="font-mono text-xs">{{ cfg?.smtp_host }}:{{ cfg?.smtp_port }}</span>
          </div>
          <div v-if="cfg?.inbound_enabled" class="flex justify-between">
            <span class="text-gray-600">IMAP:</span>
            <span class="font-mono text-xs">{{ cfg?.imap_host }}:{{ cfg?.imap_port }}</span>
          </div>
          <div v-if="cfg?.inbound_enabled" class="flex justify-between">
            <span class="text-gray-600">Allowed domains:</span>
            <span class="font-mono text-xs">{{ (cfg?.allowed_domains || []).join(', ') || 'any (auth only)' }}</span>
          </div>
          <div class="flex justify-between">
            <span class="text-gray-600">Connected:</span>
            <span class="font-medium">{{ formatDate(integrationData?.created_at) }}</span>
          </div>
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
        <h3 class="text-sm font-semibold text-gray-800 mb-2">Outbound email (required)</h3>

        <div class="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label class="block text-sm font-medium mb-1">From name</label>
            <input v-model="fromName" type="text" class="w-full border rounded px-2 py-1" placeholder="Acme Analyst" />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">From address</label>
            <input v-model="fromAddress" type="email" class="w-full border rounded px-2 py-1" placeholder="analyst@acme.com" />
            <p class="text-xs text-gray-500 mt-1">Defaults to the SMTP username.</p>
          </div>
        </div>

        <div class="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label class="block text-sm font-medium mb-1">SMTP host</label>
            <input v-model="smtpHost" type="text" class="w-full border rounded px-2 py-1" placeholder="smtp.acme.com" required />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">SMTP port</label>
            <input v-model.number="smtpPort" type="number" class="w-full border rounded px-2 py-1" required />
          </div>
        </div>

        <div class="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label class="block text-sm font-medium mb-1">SMTP username</label>
            <input v-model="smtpUsername" type="text" class="w-full border rounded px-2 py-1" required />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">SMTP password</label>
            <input v-model="smtpPassword" type="password" class="w-full border rounded px-2 py-1" required />
            <p class="text-xs text-gray-500 mt-1">App password for Gmail/M365 mailboxes.</p>
          </div>
        </div>

        <div class="mb-4">
          <label class="block text-sm font-medium mb-1">SMTP security</label>
          <select v-model="smtpSecurity" class="w-full border rounded px-2 py-1">
            <option value="starttls">STARTTLS (587)</option>
            <option value="ssl">SSL/TLS (465)</option>
            <option value="none">None (sandbox/relay)</option>
          </select>
        </div>

        <hr class="my-4" />

        <label class="flex items-center gap-2 mb-3 cursor-pointer">
          <UToggle v-model="inboundEnabled" />
          <span class="text-sm font-semibold text-gray-800">Receive email as a channel (optional)</span>
        </label>
        <p class="text-xs text-gray-500 mb-3">
          Turn the analyst into an email contact. Inbound mail is polled over IMAP,
          checked against DMARC/DKIM and your allowed domains, then routed to the agent.
        </p>

        <div v-if="inboundEnabled">
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div>
              <label class="block text-sm font-medium mb-1">IMAP host</label>
              <input v-model="imapHost" type="text" class="w-full border rounded px-2 py-1" placeholder="imap.acme.com" :required="inboundEnabled" />
            </div>
            <div>
              <label class="block text-sm font-medium mb-1">IMAP port</label>
              <input v-model.number="imapPort" type="number" class="w-full border rounded px-2 py-1" />
            </div>
          </div>
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div>
              <label class="block text-sm font-medium mb-1">IMAP username</label>
              <input v-model="imapUsername" type="text" class="w-full border rounded px-2 py-1" :required="inboundEnabled" />
            </div>
            <div>
              <label class="block text-sm font-medium mb-1">IMAP password</label>
              <input v-model="imapPassword" type="password" class="w-full border rounded px-2 py-1" :required="inboundEnabled" />
            </div>
          </div>
          <div class="mb-3">
            <label class="block text-sm font-medium mb-1">Allowed sender domains</label>
            <input v-model="allowedDomains" type="text" class="w-full border rounded px-2 py-1" placeholder="acme.com, subsidiary.com" />
            <p class="text-xs text-gray-500 mt-1">Comma-separated. Leave blank to rely on an internal-only mailbox + auth checks.</p>
          </div>
          <label class="flex items-center gap-2 mb-2 cursor-pointer">
            <UToggle v-model="autoLink" />
            <span class="text-sm">Auto-link senders to existing members (like Teams)</span>
          </label>
          <label class="flex items-center gap-2 mb-4 cursor-pointer">
            <UToggle v-model="requireAuthPass" />
            <span class="text-sm">Require DMARC/DKIM pass (recommended)</span>
          </label>
        </div>

        <button type="submit" :disabled="submitting" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md disabled:opacity-50">
          {{ submitting ? 'Connecting…' : 'Connect' }}
        </button>
      </form>
    </div>

    <button class="absolute top-2 end-2 text-gray-400 hover:text-gray-600" @click="$emit('close')">✕</button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'

const props = defineProps<{
  integrated: boolean
  integrationData?: any
}>()
const emit = defineEmits(['close', 'updated'])
const toast = useToast()

const cfg = computed(() => props.integrationData?.platform_config || null)

// Outbound
const fromName = ref('Bag of words Analyst')
const fromAddress = ref('')
const smtpHost = ref('')
const smtpPort = ref(587)
const smtpUsername = ref('')
const smtpPassword = ref('')
const smtpSecurity = ref('starttls')

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

function formatDate(dateString: string | undefined) {
  if (!dateString) return 'N/A'
  return new Date(dateString).toLocaleDateString('en-US', {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
  })
}

async function connect() {
  submitting.value = true
  try {
    const body: any = {
      smtp_host: smtpHost.value,
      smtp_port: smtpPort.value,
      smtp_username: smtpUsername.value,
      smtp_password: smtpPassword.value,
      smtp_security: smtpSecurity.value,
      from_address: fromAddress.value || smtpUsername.value,
      from_name: fromName.value,
      auto_link_by_email: autoLink.value,
      require_auth_pass: requireAuthPass.value,
      allowed_domains: allowedDomains.value
        .split(',').map((d) => d.trim()).filter(Boolean),
    }
    if (inboundEnabled.value) {
      body.imap_host = imapHost.value
      body.imap_port = imapPort.value
      body.imap_username = imapUsername.value
      body.imap_password = imapPassword.value
    }
    const res = await useMyFetch('/api/settings/integrations/email', { method: 'POST', body })
    if (res.status.value === 'success') {
      toast.add({ title: 'Email connected', description: 'Email integration successful', color: 'green' })
      emit('updated')
      emit('close')
    } else {
      toast.add({
        title: 'Failed to connect email',
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red',
      })
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
    emit('updated')
    emit('close')
  } else {
    toast.add({
      title: 'Failed to disconnect email',
      description: (res.error.value as any).data?.detail || (res.error.value as any).message,
      color: 'red',
    })
  }
}
</script>
