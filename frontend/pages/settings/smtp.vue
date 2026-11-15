<template>
  <div>
    <!-- Which transport system mail is actually leaving through right now. -->
    <div v-if="loaded" :class="['rounded-lg border px-4 py-3 flex items-start gap-3 text-sm', activeBanner.class]">
      <UIcon :name="activeBanner.icon" class="w-4 h-4 shrink-0 mt-0.5" />
      <span data-testid="smtp-active-source"><span class="font-medium">{{ activeBanner.title }}</span> {{ activeBanner.detail }}</span>
    </div>

    <form class="mt-8" @submit.prevent="save">
      <SettingsSection :title="$t('settings.smtpPage.serverSection')" :description="$t('settings.smtpPage.serverSectionHint')">
        <!-- Enable/disable is its own switch: blanking the host to "turn it off"
             used to throw the whole configuration away. -->
        <SettingsRow :label="$t('settings.smtpPage.enableLabel')" :description="enableHint">
          <UToggle v-model="form.enabled" data-testid="smtp-enabled-toggle" />
        </SettingsRow>

        <!-- The fields dim when the server is off: the settings are kept,
             but editing them has no effect until it is switched back on. -->
        <fieldset :disabled="!form.enabled" class="mt-6 min-w-0 border-0 p-0 m-0 transition-opacity" :class="form.enabled ? '' : 'opacity-50'">
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-5">
            <SettingsRow :label="$t('settings.smtpPage.fromName')" stacked for-id="smtp-from-name">
              <UInput id="smtp-from-name" v-model="form.from_name" data-testid="smtp-from-name" placeholder="Acme" />
            </SettingsRow>
            <SettingsRow :label="$t('settings.smtpPage.fromAddress')" stacked for-id="smtp-from-address">
              <template #label-extra><span v-if="form.enabled" class="text-red-600" aria-hidden="true">*</span></template>
              <UInput id="smtp-from-address" v-model="form.from_address" type="email" data-testid="smtp-from-address" placeholder="noreply@acme.com" />
            </SettingsRow>
            <SettingsRow :label="$t('settings.smtpPage.host')" stacked for-id="smtp-host">
              <template #label-extra><span v-if="form.enabled" class="text-red-600" aria-hidden="true">*</span></template>
              <UInput id="smtp-host" v-model="form.host" data-testid="smtp-host" placeholder="smtp.acme.com" />
            </SettingsRow>
            <SettingsRow :label="$t('settings.smtpPage.port')" stacked for-id="smtp-port">
              <UInput id="smtp-port" v-model.number="form.port" type="number" data-testid="smtp-port" />
            </SettingsRow>
            <SettingsRow :label="$t('settings.smtpPage.username')" optional stacked for-id="smtp-username">
              <UInput id="smtp-username" v-model="form.username" data-testid="smtp-username" autocomplete="off" />
            </SettingsRow>
            <SettingsRow :label="$t('settings.smtpPage.password')" optional stacked for-id="smtp-password"
              :description="passwordSet ? $t('settings.smtpPage.passwordKeep') : ''">
              <UInput id="smtp-password" v-model="form.password" type="password" data-testid="smtp-password" autocomplete="new-password"
                :placeholder="passwordSet ? $t('settings.smtpPage.passwordUnchanged') : ''" />
            </SettingsRow>
          </div>
          <p class="mt-2 text-xs text-ink-muted">{{ $t('settings.smtpPage.authHint') }}</p>

          <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-5 mt-5">
            <SettingsRow :label="$t('settings.smtpPage.security')" stacked for-id="smtp-security">
              <USelect id="smtp-security" v-model="form.security" data-testid="smtp-security" :options="securityOptions" option-attribute="label" value-attribute="value" />
            </SettingsRow>
          </div>
          <div v-if="form.security !== 'none'" class="mt-5">
            <SettingsRow :label="$t('settings.smtpPage.validateCerts')" :description="$t('settings.smtpPage.validateCertsHint')">
              <UToggle v-model="form.validate_certs" />
            </SettingsRow>
          </div>
        </fieldset>

        <div v-if="visibleTestResult" data-testid="smtp-test-result"
          class="mt-6 text-sm rounded-lg border px-4 py-3 flex items-start gap-3"
          :class="visibleTestResult.ok
            ? 'border-green-200 dark:border-green-900/60 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-200'
            : 'border-red-200 dark:border-red-900/60 bg-red-50 dark:bg-red-950/40 text-red-800 dark:text-red-200'">
          <UIcon :name="visibleTestResult.ok ? 'i-heroicons-check-circle' : 'i-heroicons-x-circle'" class="w-4 h-4 shrink-0 mt-0.5" />
          <div class="min-w-0">
            <div class="font-medium">{{ visibleTestResult.text }}</div>
            <div v-if="visibleTestResult.detail" class="text-xs mt-0.5 opacity-90 break-words">{{ visibleTestResult.detail }}</div>
          </div>
        </div>

        <SettingsFooter>
          <template #note>
            <span class="inline-flex items-start gap-1.5">
              <UIcon name="i-heroicons-information-circle" class="w-4 h-4 shrink-0" />
              <span><span class="font-medium text-ink-2">{{ $t('settings.smtpPage.mailboxNoteTitle') }}</span> {{ $t('settings.smtpPage.mailboxNote') }}</span>
            </span>
          </template>
          <UTooltip v-if="form.enabled && currentUserEmail" :text="$t('settings.smtpPage.sendsTo', { email: currentUserEmail })">
            <UButton type="button" color="gray" :disabled="busy" :loading="testing" data-testid="smtp-test" @click="test">
              {{ testing ? $t('settings.smtpPage.sendingTest') : $t('settings.smtpPage.sendTest') }}
            </UButton>
          </UTooltip>
          <UButton type="submit" color="blue" :disabled="busy" :loading="saving && !testing" data-testid="smtp-save">
            {{ saving && !testing ? $t('settings.smtpPage.saving') : $t('settings.smtpPage.save') }}
          </UButton>
        </SettingsFooter>
      </SettingsSection>
    </form>

    <!-- Org-wide "connection down" alert emails. Saves on its own, since it
         governs which system emails go out, not the transport. The in-app
         inbox notification is always delivered. -->
    <div class="mt-10 pt-8 border-t border-line">
      <SettingsSection :title="$t('settings.connectionAlerts.heading')" :description="$t('settings.smtpPage.alertsHint')">
        <SettingsRow :label="$t('settings.connectionAlerts.label')" :description="$t('settings.connectionAlerts.description')">
          <UToggle v-model="connAlertsEmail" :disabled="connAlertsSaving"
            data-testid="connection-alerts-toggle" @update:model-value="saveConnAlerts" />
        </SettingsRow>
      </SettingsSection>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, computed, nextTick, onMounted } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsFooter from '~/components/settings/SettingsFooter.vue'

definePageMeta({ auth: true, permissions: ['manage_settings'] })

const toast = useToast()
const { t } = useI18n()
const { data: authData } = useAuth()
const currentUserEmail = computed(() => (authData.value as any)?.email || '')

// Org-wide "connection down/restored" alert emails. Independent of the SMTP
// form: own fetch, saves immediately on toggle. Inbox notifications are
// always on; this only governs the email nudge.
const connAlertsEmail = ref(false)
const connAlertsInitial = ref(false)
const connAlertsSaving = ref(false)

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

const securityOptions = computed(() => [
  { value: 'starttls', label: 'STARTTLS (587)' },
  { value: 'ssl', label: 'SSL/TLS (465)' },
  { value: 'none', label: t('settings.smtpPage.securityNone') },
])

const enableHint = computed(() => {
  if (form.enabled) return t('settings.smtpPage.enableOn')
  if (serverState.global_configured) return t('settings.smtpPage.enableOffGlobal')
  return t('settings.smtpPage.enableOffNone')
})

const activeBanner = computed(() => {
  if (serverState.active_source === 'org_smtp') {
    return {
      class: 'border-green-200 dark:border-green-900/60 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-200',
      icon: 'i-heroicons-check-circle',
      title: t('settings.smtpPage.status.orgTitle'),
      detail: t('settings.smtpPage.status.orgDetail', { host: serverState.host || t('settings.smtpPage.status.yourRelay') }),
    }
  }
  if (serverState.active_source === 'global') {
    return {
      class: 'border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 text-amber-800 dark:text-amber-200',
      icon: 'i-heroicons-information-circle',
      title: t('settings.smtpPage.status.globalTitle'),
      detail: t('settings.smtpPage.status.globalDetail'),
    }
  }
  return {
    class: 'border-red-200 dark:border-red-900/60 bg-red-50 dark:bg-red-950/40 text-red-800 dark:text-red-200',
    icon: 'i-heroicons-exclamation-triangle',
    title: t('settings.smtpPage.status.noneTitle'),
    detail: t('settings.smtpPage.status.noneDetail'),
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
  try {
    const ca = await useMyFetch('/api/organization/connection-alerts')
    const cav = ca.data.value as any
    connAlertsEmail.value = cav?.email_enabled ?? false
    connAlertsInitial.value = connAlertsEmail.value
  } catch { /* leave default (off) */ }
  loaded.value = true
}

async function saveConnAlerts(value: boolean) {
  connAlertsSaving.value = true
  try {
    const res = await useMyFetch('/api/organization/connection-alerts',
      { method: 'PUT', body: { email_enabled: value } })
    if (res.status.value !== 'success') {
      connAlertsEmail.value = connAlertsInitial.value  // revert the switch
      const detail = (res.error.value as any)?.data?.detail || t('settings.connectionAlerts.saveError')
      toast.add({ title: t('settings.connectionAlerts.saveError'), description: detail, color: 'red' })
      return
    }
    connAlertsInitial.value = value
    toast.add({ title: t('settings.saved'), color: 'green' })
  } finally {
    connAlertsSaving.value = false
  }
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
    const detail = (res.error.value as any)?.data?.detail || t('settings.smtpPage.saveFailedDetail')
    toast.add({ title: t('settings.smtpPage.saveFailed'), description: detail, color: 'red' })
    await showResult({ ok: false, text: t('settings.smtpPage.notSaved'), detail })
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
    if (await persist()) toast.add({ title: t('settings.smtpPage.saved'), color: 'green' })
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
      const detail = (res.error.value as any)?.data?.detail || t('settings.smtpPage.testRequestFailed')
      await showResult({ ok: false, text: t('settings.smtpPage.testFailed'), detail })
      return
    }
    const data = res.data.value as any
    // The send just proved which transport is live; refresh the banner with it
    // before stamping the result so both describe the same state.
    await load()
    if (data?.success) {
      await showResult({
        ok: true,
        text: t('settings.smtpPage.testDelivered', { recipient: data.recipient }),
        detail: data.from_address
          ? t('settings.smtpPage.testDeliveredDetailAs', { source: sourceLabel(data.source), from: data.from_address })
          : t('settings.smtpPage.testDeliveredDetail', { source: sourceLabel(data.source) }),
      })
    } else {
      const { text, hint } = failureCopy(data?.stage)
      await showResult({
        ok: false,
        text,
        detail: t('settings.smtpPage.testFailureDetail', { error: data?.error || t('settings.smtpPage.unknownError'), hint, source: sourceLabel(data?.source) }),
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
  const key = ['connect', 'tls', 'auth', 'sender', 'recipient', 'config'].includes(stage || '') ? stage : 'other'
  return {
    text: t(`settings.smtpPage.fail.${key}`),
    hint: t(`settings.smtpPage.fail.${key}Hint`),
  }
}

function sourceLabel(source?: string) {
  if (source === 'org_smtp') return t('settings.smtpPage.source.org')
  if (source === 'global') return t('settings.smtpPage.source.global')
  if (source === 'ai_mailbox') return t('settings.smtpPage.source.mailbox')
  return t('settings.smtpPage.source.none')
}
</script>
