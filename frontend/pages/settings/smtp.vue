<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900 dark:text-white">
      SMTP Server
      <p class="text-sm text-gray-500 dark:text-gray-400 font-normal mb-6">
        The server used to send your organization's <strong>system emails</strong> —
        report shares, scheduled‑report results, and invites. When set, it overrides
        the global SMTP configured in <code>bow-config</code>.
      </p>
    </h2>

    <div class="bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 rounded-lg p-3 my-3 text-xs text-blue-800 dark:text-blue-200 md:w-2/3">
      <strong>SMTP Server vs AI Mailbox.</strong> This SMTP server only sends
      <em>system</em> notifications. It is <em>not</em> used by the AI analyst —
      the analyst's replies and answers always come from the separate
      <strong>AI Mailbox</strong>. Configure them independently.
    </div>

    <form class="md:w-2/3 mt-4" @submit.prevent="save">
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">
        Fill in a host to use a custom SMTP server. Leave the host blank to fall back to the global SMTP from <code>bow-config</code>.
      </p>

      <div class="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label class="block text-sm font-medium mb-1">From name</label>
          <input v-model="form.from_name" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" placeholder="Acme" />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">From address</label>
          <input v-model="form.from_address" type="email" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" placeholder="noreply@acme.com" />
        </div>
      </div>
      <div class="grid grid-cols-2 gap-3 mb-3">
        <div>
          <label class="block text-sm font-medium mb-1">Host</label>
          <input v-model="form.host" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" placeholder="smtp.acme.com" />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Port</label>
          <input v-model.number="form.port" type="number" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" />
        </div>
      </div>
      <div class="grid grid-cols-2 gap-3 mb-1">
        <div>
          <label class="block text-sm font-medium mb-1">Username <span class="text-gray-400 font-normal">(optional)</span></label>
          <input v-model="form.username" type="text" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white" />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Password <span class="text-gray-400 font-normal">(optional)</span></label>
          <input v-model="form.password" type="password" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white"
            :placeholder="passwordSet ? '•••••••• (unchanged)' : ''" />
          <p v-if="passwordSet" class="text-xs text-gray-500 dark:text-gray-400 mt-1">Leave blank to keep the saved password.</p>
        </div>
      </div>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">Leave username &amp; password empty for an open relay that doesn't require authentication.</p>
      <div class="mb-3">
        <label class="block text-sm font-medium mb-1">Security</label>
        <select v-model="form.security" class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1 bg-white dark:bg-gray-900 text-gray-900 dark:text-white">
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
        <button type="button" v-if="form.host" :disabled="testing" @click="test"
          class="border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm px-3 py-1.5 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">
          {{ testing ? 'Testing…' : 'Test connection' }}
        </button>
        <button type="submit" :disabled="saving" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md disabled:opacity-50">
          {{ saving ? 'Saving…' : 'Save' }}
        </button>
      </div>
      <p v-if="testResult" :class="testResult.ok ? 'text-green-600' : 'text-red-600'" class="text-sm mt-2 flex items-start gap-1">
        <UIcon :name="testResult.ok ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" class="w-4 h-4 shrink-0 mt-0.5" />
        <span>{{ testResult.text }}</span>
      </p>
    </form>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, onMounted } from 'vue'

definePageMeta({ auth: true, permissions: ['manage_settings'], layout: 'settings' })

const toast = useToast()

const form = reactive({
  host: '', port: 587, security: 'starttls',
  username: '', password: '', from_address: '', from_name: '', validate_certs: true,
})
const passwordSet = ref(false)
const saving = ref(false)
const testing = ref(false)
const testResult = ref<{ ok: boolean; text: string } | null>(null)

onMounted(async () => {
  try {
    const res = await useMyFetch('/api/organization/smtp')
    const s = res.data.value as any
    if (s) {
      form.host = s.host || ''
      form.port = s.port || 587
      form.security = s.security || 'starttls'
      form.username = s.username || ''
      form.from_address = s.from_address || ''
      form.from_name = s.from_name || ''
      form.validate_certs = s.validate_certs !== false
      passwordSet.value = !!s.password_set
    }
  } catch (e) { /* ignore */ }
})

function payload() {
  const p: any = {
    // Enabled is derived from the host: filled in → used, blank → falls back to global SMTP.
    enabled: !!form.host.trim(), host: form.host.trim(), port: form.port, security: form.security,
    username: form.username, from_address: form.from_address, from_name: form.from_name,
    validate_certs: form.validate_certs,
  }
  if (form.password) p.password = form.password  // only send when (re)setting
  return p
}

async function save() {
  saving.value = true
  testResult.value = null
  try {
    const res = await useMyFetch('/api/organization/smtp', { method: 'PUT', body: payload() })
    if (res.status.value === 'success') {
      toast.add({ title: 'SMTP saved', color: 'green' })
    } else {
      toast.add({ title: 'Failed to save SMTP', description: (res.error.value as any)?.data?.detail || 'Error', color: 'red' })
    }
  } finally {
    saving.value = false
  }
}

async function test() {
  saving.value = true
  testing.value = true
  testResult.value = null
  try {
    // Save first so the test probes the stored config.
    await useMyFetch('/api/organization/smtp', { method: 'PUT', body: payload() })
    passwordSet.value = passwordSet.value || !!form.password
    form.password = ''
    const res = await useMyFetch('/api/organization/smtp/test', { method: 'POST' })
    const data = res.data.value as any
    if (res.status.value === 'success' && data?.success) {
      testResult.value = { ok: true, text: 'Connection OK' }
    } else {
      testResult.value = { ok: false, text: data?.smtp || 'Connection failed — check the settings' }
    }
  } finally {
    saving.value = false
    testing.value = false
  }
}
</script>
