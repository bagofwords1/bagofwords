<template>
  <div class="p-4">
    <div class="flex items-center gap-2 mb-2">
      <UIcon name="i-heroicons-envelope" class="w-5 h-5 text-gray-700 dark:text-gray-300" />
      <h1 class="text-lg font-semibold">{{ $t('settings.integrations.channels.email.title') }}</h1>
    </div>
    <i18n-t keypath="settings.integrations.channels.email.subtitle" tag="p" class="text-sm text-gray-500 dark:text-gray-400">
      <template #smtpServer><strong>{{ $t('settings.integrations.channels.email.smtpServer') }}</strong></template>
    </i18n-t>
    <hr class="my-4" />

    <!-- Connected view -->
    <div v-if="integrated" class="mb-4">
      <p class="text-green-600 mb-4">{{ $t('settings.integrations.channels.email.connectedNotice') }}</p>
      <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
        <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{{ $t('settings.integrations.channels.common.details') }}</h3>
        <div class="space-y-2 text-sm">
          <div class="flex justify-between"><span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.email.fromLabel') }}</span>
            <span class="font-medium">{{ cfg?.from_name }} &lt;{{ cfg?.from_address }}&gt;</span></div>
          <div class="flex justify-between"><span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.email.authLabel') }}</span>
            <span class="font-mono text-xs">{{ authLabel(cfg?.auth_type) }}</span></div>
          <div class="flex justify-between"><span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.email.capabilitiesLabel') }}</span>
            <span class="font-mono text-xs">{{ (cfg?.capabilities || ['send']).join(' + ') }}</span></div>
          <div v-if="cfg?.inbound_enabled" class="flex justify-between"><span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.email.allowedDomainsLabel') }}</span>
            <span class="font-mono text-xs">{{ (cfg?.allowed_domains || []).join(', ') || $t('settings.integrations.channels.email.anyAuthOnly') }}</span></div>
          <div class="flex justify-between"><span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.common.connectedLabel') }}</span>
            <span class="font-medium">{{ formatDate(integrationData?.created_at) }}</span></div>
        </div>
      </div>
      <div class="flex gap-2">
        <UButton color="gray" variant="soft" :loading="testing" @click="test">{{ $t('settings.integrations.channels.common.testConnection') }}</UButton>
        <UButton color="red" variant="soft" @click="disconnect">{{ $t('settings.integrations.channels.common.disconnect') }}</UButton>
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
        <label class="block text-sm font-medium mb-2">{{ $t('settings.integrations.channels.email.connectHow') }}</label>
        <div class="grid grid-cols-3 gap-2 mb-4">
          <button v-for="opt in authOptions" :key="opt.value" type="button" @click="authType = opt.value"
            :class="[
              'flex flex-col items-center justify-center gap-2 border rounded-lg py-3 px-2 transition',
              authType === opt.value
                ? 'border-blue-500 ring-1 ring-blue-500 bg-blue-50 dark:bg-blue-950'
                : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 bg-white dark:bg-gray-900',
            ]">
            <img v-if="opt.img" :src="opt.img" :alt="opt.label" class="w-6 h-6" />
            <UIcon v-else :name="opt.icon!" class="w-6 h-6 text-gray-600 dark:text-gray-400" />
            <span :class="['text-xs font-medium text-center leading-tight', authType === opt.value ? 'text-blue-700' : 'text-gray-700 dark:text-gray-300']">{{ opt.label }}</span>
          </button>
        </div>

        <!-- Mailbox identity (all methods) -->
        <div class="grid grid-cols-2 gap-3 mb-3">
          <div>
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.fromName') }}</label>
            <input v-model="fromName" type="text" class="w-full border rounded px-2 py-1" :placeholder="$t('settings.integrations.channels.email.fromNamePlaceholder')" />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.mailboxAddress') }}</label>
            <input v-model="fromAddress" type="email" class="w-full border rounded px-2 py-1" placeholder="analyst@acme.com" :required="authType !== 'password'" />
          </div>
        </div>

        <!-- Password fields -->
        <template v-if="authType === 'password'">
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.smtpHost') }}</label>
              <input v-model="smtpHost" type="text" class="w-full border rounded px-2 py-1" placeholder="smtp.acme.com" required /></div>
            <div><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.smtpPort') }}</label>
              <input v-model.number="smtpPort" type="number" class="w-full border rounded px-2 py-1" /></div>
          </div>
          <div class="grid grid-cols-2 gap-3 mb-3">
            <div><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.smtpUsername') }}</label>
              <input v-model="smtpUsername" type="text" class="w-full border rounded px-2 py-1" required /></div>
            <div><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.smtpPassword') }}</label>
              <input v-model="smtpPassword" type="password" class="w-full border rounded px-2 py-1" required /></div>
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.smtpSecurity') }}</label>
            <select v-model="smtpSecurity" class="w-full border rounded px-2 py-1">
              <option value="starttls">{{ $t('settings.integrations.channels.email.smtpSecurityStarttls') }}</option>
              <option value="ssl">{{ $t('settings.integrations.channels.email.smtpSecuritySsl') }}</option>
              <option value="none">{{ $t('settings.integrations.channels.email.smtpSecurityNone') }}</option>
            </select>
          </div>
        </template>

        <!-- Microsoft 365 fields -->
        <template v-else-if="authType === 'microsoft'">
          <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">{{ $t('settings.integrations.channels.email.msHostsHint') }}</p>
          <div class="mb-3"><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.msTenantId') }}</label>
            <input v-model="msTenantId" type="text" class="w-full border rounded px-2 py-1" required /></div>
          <div class="mb-3"><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.msClientId') }}</label>
            <input v-model="msClientId" type="text" class="w-full border rounded px-2 py-1" required /></div>
          <div class="mb-4"><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.msClientSecret') }}</label>
            <input v-model="msClientSecret" type="password" class="w-full border rounded px-2 py-1" required /></div>
        </template>

        <!-- Google Workspace fields -->
        <template v-else-if="authType === 'google'">
          <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">{{ $t('settings.integrations.channels.email.googleHint') }}</p>
          <textarea v-model="googleSaJson" rows="6" class="w-full border rounded px-2 py-1 font-mono text-xs" placeholder='{ "type": "service_account", ... }' required></textarea>
        </template>

        <hr class="my-4" />

        <!-- Receive inbound email (always enabled) -->
        <div class="mb-3">
          <span class="text-sm font-semibold text-gray-800 dark:text-gray-200">{{ $t('settings.integrations.channels.email.receiveAsChannel') }}</span>
        </div>

        <div>
          <!-- IMAP host/port only needed for the password method; OAuth hosts are defaulted -->
          <template v-if="authType === 'password'">
            <div class="grid grid-cols-2 gap-3 mb-3">
              <div><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.imapHost') }}</label>
                <input v-model="imapHost" type="text" class="w-full border rounded px-2 py-1" placeholder="imap.acme.com" /></div>
              <div><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.imapPort') }}</label>
                <input v-model.number="imapPort" type="number" class="w-full border rounded px-2 py-1" /></div>
            </div>
            <div class="grid grid-cols-2 gap-3 mb-3">
              <div><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.imapUsername') }}</label>
                <input v-model="imapUsername" type="text" class="w-full border rounded px-2 py-1" /></div>
              <div><label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.imapPassword') }}</label>
                <input v-model="imapPassword" type="password" class="w-full border rounded px-2 py-1" /></div>
            </div>
          </template>
          <div class="mb-3">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.email.allowedSenderDomains') }}</label>
            <input v-model="allowedDomains" type="text" class="w-full border rounded px-2 py-1" :placeholder="$t('settings.integrations.channels.email.allowedSenderDomainsPlaceholder')" />
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ $t('settings.integrations.channels.email.allowedSenderDomainsHint') }}</p>
          </div>
          <label class="flex items-center gap-2 mb-2 cursor-pointer">
            <UToggle v-model="autoLink" color="blue" /><span class="text-sm">{{ $t('settings.integrations.channels.email.autoVerifyLabel') }} — <span class="text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.email.autoVerifyHint') }}</span></span>
          </label>
          <label class="flex items-center gap-2 mb-4 cursor-pointer">
            <UToggle v-model="requireAuthPass" color="blue" /><span class="text-sm">{{ $t('settings.integrations.channels.email.requireDmarc') }}</span>
          </label>
        </div>

        <div class="flex items-center gap-2">
          <button type="button" :disabled="testingForm" @click="testForm"
            class="border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm px-3 py-1.5 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">
            {{ testingForm ? $t('settings.integrations.channels.common.testing') : $t('settings.integrations.channels.common.testConnection') }}
          </button>
          <button type="submit" :disabled="submitting" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md disabled:opacity-50">
            {{ submitting ? $t('settings.integrations.channels.common.connecting') : $t('settings.integrations.channels.common.connect') }}
          </button>
        </div>
        <p v-if="testResult" :class="testResult.ok ? 'text-green-600' : 'text-red-600'" class="text-sm mt-2 flex items-start gap-1">
          <UIcon :name="testResult.ok ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" class="w-4 h-4 shrink-0 mt-0.5" />
          <span>{{ testResult.text }}</span>
        </p>
      </form>

      <!-- Right: contextual setup guide -->
      <aside class="md:w-72 md:shrink-0 mt-6 md:mt-0">
        <div class="bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-4">
          <div class="flex items-center gap-2 mb-3">
            <img v-if="activeOption.img" :src="activeOption.img" :alt="activeOption.label" class="w-5 h-5" />
            <UIcon v-else :name="activeOption.icon!" class="w-5 h-5 text-gray-600 dark:text-gray-400" />
            <h3 class="text-sm font-semibold text-gray-800 dark:text-gray-200">{{ $t('settings.integrations.channels.email.setupGuideTitle', { label: activeOption.label }) }}</h3>
          </div>

          <!-- Microsoft 365 -->
          <ol v-if="authType === 'microsoft'" class="list-decimal list-outside ps-4 text-xs text-gray-600 dark:text-gray-400 space-y-2">
            <li>{{ $t('settings.integrations.channels.email.msStep1') }}</li>
            <li>{{ $t('settings.integrations.channels.email.msStep2') }}</li>
            <i18n-t keypath="settings.integrations.channels.email.msStep3" tag="li">
              <template #imapScope><code>IMAP.AccessAsApp</code></template>
              <template #smtpScope><code>SMTP.SendAsApp</code></template>
            </i18n-t>
            <li>{{ $t('settings.integrations.channels.email.msStep4') }}</li>
            <i18n-t keypath="settings.integrations.channels.email.msStep5" tag="li">
              <template #cmd1><code>New-ServicePrincipal</code></template>
              <template #cmd2><code>Add-MailboxPermission … -AccessRights FullAccess</code></template>
            </i18n-t>
          </ol>

          <!-- Google Workspace -->
          <ol v-else-if="authType === 'google'" class="list-decimal list-outside ps-4 text-xs text-gray-600 dark:text-gray-400 space-y-2">
            <li>{{ $t('settings.integrations.channels.email.googleStep1') }}</li>
            <li>{{ $t('settings.integrations.channels.email.googleStep2') }}</li>
            <i18n-t keypath="settings.integrations.channels.email.googleStep3" tag="li">
              <template #scope><code>https://mail.google.com/</code></template>
            </i18n-t>
            <li>{{ $t('settings.integrations.channels.email.googleStep4') }}</li>
          </ol>

          <!-- IMAP / Password -->
          <div v-else class="text-xs text-gray-600 dark:text-gray-400 space-y-2">
            <p>{{ $t('settings.integrations.channels.email.imapIntro') }}</p>
            <ul class="list-disc list-outside ps-4 space-y-1">
              <li>{{ $t('settings.integrations.channels.email.imapBullet1') }}</li>
              <li>{{ $t('settings.integrations.channels.email.imapBullet2') }}</li>
              <li>{{ $t('settings.integrations.channels.email.imapBullet3') }}</li>
              <li>{{ $t('settings.integrations.channels.email.imapBullet4') }}</li>
            </ul>
          </div>

          <p class="text-[11px] text-gray-400 mt-3">{{ $t('settings.integrations.channels.email.testBeforeSave') }}</p>
        </div>
      </aside>
    </div>

    <button class="absolute top-2 end-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300" :title="$t('settings.integrations.channels.common.close')" @click="$emit('close')">✕</button>
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
const { t } = useI18n()

const cfg = computed(() => props.integrationData?.platform_config || null)

type AuthType = 'password' | 'microsoft' | 'google'
const authOptions = computed<{ value: AuthType; label: string; img?: string; icon?: string }[]>(() => [
  { value: 'google', label: t('settings.integrations.channels.email.authGoogle'), img: '/icons/google.svg' },
  { value: 'microsoft', label: t('settings.integrations.channels.email.authMicrosoft'), img: '/icons/microsoft.svg' },
  { value: 'password', label: t('settings.integrations.channels.email.authPassword'), icon: 'i-heroicons-envelope' },
])
const authType = ref<AuthType>('password')
const activeOption = computed(() => authOptions.value.find((o) => o.value === authType.value) || authOptions.value[2])

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
    const imapPart = data.imap ? t('settings.integrations.channels.email.connectionOkImap', { imap: data.imap }) : ''
    testResult.value = { ok: true, text: t('settings.integrations.channels.email.connectionOk', { smtp: data.smtp || 'ok', imap: imapPart }) }
  } else {
    const detail = (data?.smtp && data.smtp !== 'ok') ? data.smtp
      : (data?.imap && data.imap !== 'ok') ? data.imap
      : ((res.error?.value as any)?.data?.detail || t('settings.integrations.channels.email.connectionFailed'))
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

function authLabel(authType?: string) {
  return authType === 'microsoft' ? t('settings.integrations.channels.email.authTypeMicrosoft')
    : authType === 'google' ? t('settings.integrations.channels.email.authTypeGoogle')
    : t('settings.integrations.channels.email.authTypePassword')
}

const _df = useFormatDate()
function formatDate(d: string | undefined) {
  if (!d) return t('settings.integrations.channels.common.na')
  return _df.format(d, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
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
      toast.add({ title: t('settings.integrations.channels.email.connectedToast'), description: t('settings.integrations.channels.email.connectedToastDesc'), color: 'green' })
      emit('updated'); emit('close')
    } else {
      toast.add({ title: t('settings.integrations.channels.email.failedConnect'), description: (res.error.value as any).data?.detail || (res.error.value as any).message, color: 'red' })
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
    toast.add({ title: t('settings.integrations.channels.email.disconnectedToast'), description: t('settings.integrations.channels.email.disconnectedToastDesc'), color: 'green' })
    emit('updated'); emit('close')
  } else {
    toast.add({ title: t('settings.integrations.channels.email.failedDisconnect'), description: (res.error.value as any).data?.detail || (res.error.value as any).message, color: 'red' })
  }
}
</script>
