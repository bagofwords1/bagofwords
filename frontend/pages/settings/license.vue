<template>
  <div>
    <SettingsLoading v-if="loading" />

    <div v-else>
      <!-- Status: the one summary card on the page. -->
      <div
        :class="[
          'rounded-lg border p-5',
          isExpired ? 'border-red-200 dark:border-red-900/60 bg-red-50/50 dark:bg-red-950/30' : 'border-line'
        ]"
      >
        <div class="flex items-start justify-between gap-4">
          <div class="flex items-center gap-3">
            <div
              :class="[
                'w-10 h-10 rounded-lg flex items-center justify-center shrink-0',
                isLicensed ? 'bg-green-50 text-green-600 dark:bg-green-950/50 dark:text-green-400'
                  : isExpired ? 'bg-red-50 text-red-600 dark:bg-red-950/50 dark:text-red-400'
                  : 'bg-surface-muted text-ink-muted'
              ]"
            >
              <UIcon :name="isLicensed ? 'i-heroicons-check-badge' : isExpired ? 'i-heroicons-exclamation-circle' : 'i-heroicons-cube'" class="w-5 h-5" />
            </div>
            <div>
              <p class="text-base font-semibold text-ink">
                {{ isLicensed ? $t('settings.licensePage.enterprise') : $t('settings.licensePage.community') }}
              </p>
              <p class="text-sm text-ink-muted">
                <template v-if="isLicensed && license?.org_name">{{ license.org_name }}</template>
                <template v-else-if="isExpired">{{ $t('settings.licensePage.expired') }}</template>
                <template v-else>{{ $t('settings.licensePage.free') }}</template>
              </p>
            </div>
          </div>
          <UBadge :color="isLicensed ? 'green' : isExpired ? 'red' : 'gray'" variant="subtle" size="sm">
            {{ isLicensed ? $t('settings.licensePage.badgeActive') : isExpired ? $t('settings.licensePage.badgeExpired') : $t('settings.licensePage.badgeCommunity') }}
          </UBadge>
        </div>

        <dl v-if="isLicensed || isExpired" class="mt-5 grid grid-cols-1 sm:grid-cols-3 gap-4 pt-5 border-t border-line">
          <div>
            <dt class="text-xs text-ink-muted">{{ $t('settings.licensePage.fieldTier') }}</dt>
            <dd class="mt-1 text-sm font-medium text-ink capitalize">{{ license?.tier || '—' }}</dd>
          </div>
          <div>
            <dt class="text-xs text-ink-muted">{{ $t('settings.licensePage.fieldLicenseId') }}</dt>
            <dd class="mt-1 font-mono text-xs text-ink-2 truncate">{{ license?.license_id || '—' }}</dd>
          </div>
          <div v-if="expiresAt">
            <dt class="text-xs text-ink-muted">{{ $t('settings.licensePage.fieldExpires') }}</dt>
            <dd :class="['mt-1 text-sm font-medium', isExpiringSoon || isExpired ? 'text-amber-700 dark:text-amber-400' : 'text-ink']">
              {{ formatDate(expiresAt) }}
              <span v-if="daysUntilExpiry !== null && daysUntilExpiry > 0" class="block text-xs font-normal text-ink-muted">
                {{ $t('settings.licensePage.inDays', { days: daysUntilExpiry }) }}
              </span>
            </dd>
          </div>
        </dl>

        <UAlert
          v-if="isExpiringSoon"
          class="mt-4"
          color="amber"
          variant="subtle"
          icon="i-heroicons-exclamation-triangle"
          :title="$t('settings.licensePage.expiresSoon', { days: daysUntilExpiry })"
        />
        <UAlert
          v-else-if="isExpired"
          class="mt-4"
          color="red"
          variant="subtle"
          icon="i-heroicons-exclamation-circle"
          :title="$t('settings.licensePage.expiredNotice')"
        />
      </div>

      <!-- License key.
           Always rendered, including when a license is already active: a
           deployment that ships a demo BOW_LICENSE_KEY is licensed on day one,
           and that is exactly the instance whose admin needs to paste the key
           they bought. -->
      <div class="mt-10">
        <SettingsSection :title="$t('settings.licensePage.activateTitle')" :description="$t('settings.licensePage.activateDesc')">
          <!-- Where the active key comes from -->
          <div class="flex items-center gap-2 flex-wrap text-sm">
            <span class="text-ink-muted">{{ $t('settings.licensePage.sourceLabel') }}</span>
            <UBadge :color="sourceColor" variant="subtle" size="xs" data-testid="license-key-source">
              {{ sourceLabel }}
            </UBadge>
            <span v-if="keyStatus?.masked_key" class="font-mono text-xs text-ink-2">{{ keyStatus.masked_key }}</span>
            <span v-if="keyUpdatedAt" class="text-xs text-ink-muted">
              · {{ $t('settings.licensePage.lastUpdated') }} {{ formatDate(keyUpdatedAt) }}
            </span>
          </div>

          <p v-if="keyStatus?.source === 'database' && keyStatus?.config_key_present" class="mt-2 text-sm text-ink-muted">
            <i18n-t keypath="settings.licensePage.overridesEnv" tag="span" scope="global">
              <template #envvar><code class="font-mono text-xs bg-surface-muted text-ink-2 px-1 py-0.5 rounded">BOW_LICENSE_KEY</code></template>
            </i18n-t>
          </p>
          <p v-else-if="keyStatus?.source === 'config'" class="mt-2 text-sm text-ink-muted">
            <i18n-t keypath="settings.licensePage.envActive" tag="span" scope="global">
              <template #envvar><code class="font-mono text-xs bg-surface-muted text-ink-2 px-1 py-0.5 rounded">BOW_LICENSE_KEY</code></template>
            </i18n-t>
          </p>
          <p v-else-if="keyStatus?.source === 'none'" class="mt-2 text-sm text-ink-muted">
            {{ $t('settings.licensePage.noKey') }}
          </p>

          <!-- Once a license is in place the key field stays folded away: replacing
               a working (or lapsed) license is a deliberate act, not something to
               fat-finger while reading the page. Community instances get the field
               straight away — there is nothing to protect and everything to gain. -->
          <div v-if="showKeyForm" class="mt-5">
            <SettingsRow :label="$t('settings.licensePage.keyLabel')" stacked for-id="license-key">
              <UTextarea
                id="license-key"
                v-model="keyInput"
                :rows="3"
                data-testid="license-key-input"
                :placeholder="$t('settings.licensePage.keyPlaceholder')"
                textarea-class="font-mono text-xs break-all"
              />
            </SettingsRow>
          </div>

          <p v-if="saveError" class="text-sm text-red-600 dark:text-red-400 mt-3 flex items-start gap-1.5">
            <UIcon name="i-heroicons-x-circle" class="w-4 h-4 shrink-0 mt-0.5" />
            <span data-testid="license-key-error">{{ saveError }}</span>
          </p>

          <div class="flex items-center gap-2 mt-5">
            <UButton
              v-if="showKeyForm"
              color="blue"
              data-testid="license-key-save"
              :loading="saving"
              :disabled="saving || !keyInput.trim()"
              @click="save"
            >
              {{ saving ? $t('settings.licensePage.activating') : $t('settings.licensePage.activate') }}
            </UButton>
            <UButton v-else color="gray" data-testid="license-key-edit" @click="startEditing">
              {{ $t('settings.licensePage.update') }}
            </UButton>
            <UButton v-if="showKeyForm && hasLicense" color="gray" variant="ghost" data-testid="license-key-cancel" @click="cancelEditing">
              {{ $t('settings.licensePage.cancel') }}
            </UButton>
            <UButton
              v-if="keyStatus?.source === 'database'"
              color="red"
              variant="ghost"
              data-testid="license-key-remove"
              :loading="removing"
              :disabled="removing"
              @click="remove"
            >
              {{ removing ? $t('settings.licensePage.removing') : $t('settings.licensePage.remove') }}
            </UButton>
          </div>
        </SettingsSection>

        <!-- Enterprise info (community) / renew (expired) -->
        <SettingsSection
          v-if="!isLicensed"
          :title="isExpired ? undefined : $t('settings.licensePage.enterpriseTitle')"
          :description="isExpired ? $t('settings.licensePage.renewInfo') : $t('settings.licensePage.enterpriseInfo')"
        >
          <UButton
            to="https://docs.bagofwords.com/enterprise"
            target="_blank"
            color="gray"
            variant="ghost"
            trailing-icon="i-heroicons-arrow-up-right"
            class="-ms-2.5"
          >
            {{ $t('settings.licensePage.learnMore') }}
          </UButton>
        </SettingsSection>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'

definePageMeta({
  auth: true,
  permissions: ['manage_settings']
})

const { license, loading, isLicensed, isExpired, expiresAt, daysUntilExpiry, isExpiringSoon, fetchLicense } = useEnterprise()
const { t } = useI18n()
const toast = useToast()

type LicenseKeyStatus = {
  source: 'database' | 'config' | 'none'
  masked_key: string | null
  config_key_present: boolean
  updated_at: string | null
}

const keyStatus = ref<LicenseKeyStatus | null>(null)
const keyInput = ref('')
const saving = ref(false)
const removing = ref(false)
const saveError = ref<string | null>(null)
const editing = ref(false)

/** A license is in place — valid or lapsed. Either way the key behind it is
 *  real, so the field to replace it is kept behind an explicit edit. */
const hasLicense = computed(() => isLicensed.value || isExpired.value)
const showKeyForm = computed(() => editing.value || !hasLicense.value)

function startEditing() {
  saveError.value = null
  editing.value = true
}

function cancelEditing() {
  editing.value = false
  keyInput.value = ''
  saveError.value = null
}

const sourceLabel = computed(() => {
  const key = keyStatus.value?.source === 'database'
    ? 'sourceDatabase'
    : keyStatus.value?.source === 'config' ? 'sourceConfig' : 'sourceNone'
  return t(`settings.licensePage.${key}`)
})

const sourceColor = computed(() =>
  keyStatus.value?.source === 'database' ? 'blue' : keyStatus.value?.source === 'config' ? 'gray' : 'gray'
)

const keyUpdatedAt = computed(() => {
  const raw = keyStatus.value?.updated_at
  return raw ? new Date(raw) : null
})

async function loadKeyStatus() {
  const res = await useMyFetch('/api/license/key')
  if (res.status.value === 'success') {
    keyStatus.value = res.data.value as LicenseKeyStatus
  }
}

onMounted(loadKeyStatus)

/** Re-read both the key status and the license itself, so every gated surface
 *  in the app re-evaluates without a page reload. */
async function refreshAll(status: LicenseKeyStatus | null) {
  if (status) keyStatus.value = status
  else await loadKeyStatus()
  await fetchLicense()
}

async function save() {
  saving.value = true
  saveError.value = null
  try {
    const res = await useMyFetch('/api/license/key', { method: 'PUT', body: { key: keyInput.value.trim() } })
    if (res.status.value === 'success') {
      keyInput.value = ''
      editing.value = false
      await refreshAll(res.data.value as LicenseKeyStatus)
      toast.add({ title: t('settings.licensePage.savedToast'), color: 'green' })
    } else {
      saveError.value = (res.error.value as any)?.data?.detail || t('settings.licensePage.saveFailed')
    }
  } finally {
    saving.value = false
  }
}

async function remove() {
  if (!confirm(t('settings.licensePage.removeConfirm'))) return
  removing.value = true
  saveError.value = null
  try {
    const res = await useMyFetch('/api/license/key', { method: 'DELETE' })
    if (res.status.value === 'success') {
      editing.value = false
      await refreshAll(res.data.value as LicenseKeyStatus)
      toast.add({ title: t('settings.licensePage.removedToast'), color: 'green' })
    } else {
      saveError.value = (res.error.value as any)?.data?.detail || t('settings.licensePage.saveFailed')
    }
  } finally {
    removing.value = false
  }
}

const _df = useFormatDate()
const formatDate = (date: Date) => {
  return _df.format(date, {
    year: 'numeric',
    month: 'long',
    day: 'numeric'
  })
}
</script>
