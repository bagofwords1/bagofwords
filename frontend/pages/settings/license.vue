<template>
  <div class="mt-4">
    <!-- Header -->
    <div class="mb-6">
      <h2 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.licensePage.title') }}</h2>
      <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.licensePage.subtitle') }}</p>
    </div>

    <div v-if="loading" class="py-8 flex justify-center">
      <ULoader />
    </div>

    <div v-else class="space-y-4 max-w-2xl">
      <!-- License Status Card -->
      <div
        :class="[
          'rounded-lg border p-5',
          isExpired ? 'border-red-200 dark:border-red-800 bg-red-50/40 dark:bg-red-950/40' : 'border-gray-200 dark:border-gray-700'
        ]"
      >
        <div class="flex items-start justify-between gap-4">
          <div class="flex items-center gap-3">
            <div
              :class="[
                'w-9 h-9 rounded-full flex items-center justify-center shrink-0',
                isLicensed ? 'bg-green-50 dark:bg-green-950' : isExpired ? 'bg-red-50 dark:bg-red-950' : 'bg-gray-100 dark:bg-gray-800'
              ]"
            >
              <UIcon
                v-if="isLicensed"
                name="i-heroicons-check-badge"
                class="w-5 h-5 text-green-600"
              />
              <UIcon
                v-else-if="isExpired"
                name="i-heroicons-exclamation-circle"
                class="w-5 h-5 text-red-500"
              />
              <UIcon
                v-else
                name="i-heroicons-cube"
                class="w-5 h-5 text-gray-400"
              />
            </div>
            <div>
              <p class="text-sm font-medium text-gray-900 dark:text-white">
                {{ isLicensed ? $t('settings.licensePage.enterprise') : $t('settings.licensePage.community') }}
              </p>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                <template v-if="isLicensed && license?.org_name">
                  {{ license.org_name }}
                </template>
                <template v-else-if="isExpired">
                  {{ $t('settings.licensePage.expired') }}
                </template>
                <template v-else>
                  {{ $t('settings.licensePage.free') }}
                </template>
              </p>
            </div>
          </div>
          <UBadge
            :color="isLicensed ? 'green' : isExpired ? 'red' : 'gray'"
            variant="subtle"
            size="xs"
          >
            {{ isLicensed ? $t('settings.licensePage.badgeActive') : isExpired ? $t('settings.licensePage.badgeExpired') : $t('settings.licensePage.badgeCommunity') }}
          </UBadge>
        </div>

        <!-- License Details (if licensed or expired) -->
        <dl v-if="isLicensed || isExpired" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 space-y-2.5">
          <div class="flex justify-between items-center text-xs">
            <dt class="text-gray-500 dark:text-gray-400">{{ $t('settings.licensePage.fieldTier') }}</dt>
            <dd class="text-gray-700 dark:text-gray-300 capitalize">{{ license?.tier || '-' }}</dd>
          </div>
          <div class="flex justify-between items-center text-xs">
            <dt class="text-gray-500 dark:text-gray-400">{{ $t('settings.licensePage.fieldLicenseId') }}</dt>
            <dd class="font-mono text-gray-700 dark:text-gray-300">{{ license?.license_id || '-' }}</dd>
          </div>
          <div v-if="expiresAt" class="flex justify-between items-center text-xs">
            <dt class="text-gray-500 dark:text-gray-400">{{ $t('settings.licensePage.fieldExpires') }}</dt>
            <dd :class="isExpiringSoon || isExpired ? 'text-amber-600 font-medium' : 'text-gray-700 dark:text-gray-300'">
              {{ formatDate(expiresAt) }}
              <span v-if="daysUntilExpiry !== null && daysUntilExpiry > 0" class="text-gray-400 dark:text-gray-600">
                · {{ $t('settings.licensePage.inDays', { days: daysUntilExpiry }) }}
              </span>
            </dd>
          </div>
        </dl>

        <!-- Expiry Warning -->
        <UAlert
          v-if="isExpiringSoon"
          class="mt-4"
          color="amber"
          variant="subtle"
          icon="i-heroicons-exclamation-triangle"
          :title="$t('settings.licensePage.expiresSoon', { days: daysUntilExpiry })"
        />

        <!-- Expired Notice -->
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
      <div class="rounded-lg border border-gray-200 dark:border-gray-700 p-5">
        <p class="text-sm font-medium text-gray-900 dark:text-white mb-1">
          {{ $t('settings.licensePage.activateTitle') }}
        </p>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-4">
          {{ $t('settings.licensePage.activateDesc') }}
        </p>

        <!-- Where the active key comes from -->
        <div class="flex items-center gap-2 flex-wrap text-xs mb-3">
          <span class="text-gray-500 dark:text-gray-400">{{ $t('settings.licensePage.sourceLabel') }}</span>
          <UBadge :color="sourceColor" variant="subtle" size="xs" data-testid="license-key-source">
            {{ sourceLabel }}
          </UBadge>
          <span v-if="keyStatus?.masked_key" class="font-mono text-gray-600 dark:text-gray-300">
            {{ keyStatus.masked_key }}
          </span>
          <span v-if="keyUpdatedAt" class="text-gray-400 dark:text-gray-600">
            · {{ $t('settings.licensePage.lastUpdated') }} {{ formatDate(keyUpdatedAt) }}
          </span>
        </div>

        <p v-if="keyStatus?.source === 'database' && keyStatus?.config_key_present"
          class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          <i18n-t keypath="settings.licensePage.overridesEnv" tag="span">
            <template #envvar>
              <code class="bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">BOW_LICENSE_KEY</code>
            </template>
          </i18n-t>
        </p>
        <p v-else-if="keyStatus?.source === 'config'" class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          <i18n-t keypath="settings.licensePage.envActive" tag="span">
            <template #envvar>
              <code class="bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">BOW_LICENSE_KEY</code>
            </template>
          </i18n-t>
        </p>
        <p v-else-if="keyStatus?.source === 'none'" class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          {{ $t('settings.licensePage.noKey') }}
        </p>

        <!-- Once a license is in place the key field stays folded away: replacing
             a working (or lapsed) license is a deliberate act, not something to
             fat-finger while reading the page. Community instances get the field
             straight away — there is nothing to protect and everything to gain. -->
        <template v-if="showKeyForm">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
            {{ $t('settings.licensePage.keyLabel') }}
          </label>
          <textarea
            v-model="keyInput"
            rows="3"
            data-testid="license-key-input"
            :placeholder="$t('settings.licensePage.keyPlaceholder')"
            class="w-full border border-gray-300 dark:border-gray-600 rounded px-2 py-1.5 bg-white dark:bg-gray-900 text-gray-900 dark:text-white font-mono text-xs break-all"
          />
        </template>

        <p v-if="saveError" class="text-xs text-red-600 mt-2 flex items-start gap-1">
          <UIcon name="i-heroicons-x-circle" class="w-4 h-4 shrink-0 mt-px" />
          <span data-testid="license-key-error">{{ saveError }}</span>
        </p>

        <div class="flex items-center gap-2 mt-3">
          <button
            v-if="showKeyForm"
            type="button"
            data-testid="license-key-save"
            :disabled="saving || !keyInput.trim()"
            class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md disabled:opacity-50"
            @click="save"
          >
            {{ saving ? $t('settings.licensePage.activating') : $t('settings.licensePage.activate') }}
          </button>
          <button
            v-else
            type="button"
            data-testid="license-key-edit"
            class="border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm px-3 py-1.5 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800"
            @click="startEditing"
          >
            {{ $t('settings.licensePage.update') }}
          </button>
          <button
            v-if="showKeyForm && hasLicense"
            type="button"
            data-testid="license-key-cancel"
            class="text-sm px-3 py-1.5 rounded-md text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            @click="cancelEditing"
          >
            {{ $t('settings.licensePage.cancel') }}
          </button>
          <button
            v-if="keyStatus?.source === 'database'"
            type="button"
            data-testid="license-key-remove"
            :disabled="removing"
            class="border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 text-sm px-3 py-1.5 rounded-md hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
            @click="remove"
          >
            {{ removing ? $t('settings.licensePage.removing') : $t('settings.licensePage.remove') }}
          </button>
        </div>
      </div>

      <!-- Enterprise Info (only show when not licensed and not expired) -->
      <div v-if="!isLicensed && !isExpired" class="rounded-lg border border-gray-200 dark:border-gray-700 p-5">
        <p class="text-sm text-gray-700 dark:text-gray-300 font-medium mb-1">
          {{ $t('settings.licensePage.enterpriseTitle') }}
        </p>
        <p class="text-sm text-gray-600 dark:text-gray-400 mb-3">
          {{ $t('settings.licensePage.enterpriseInfo') }}
        </p>
        <a
          href="https://docs.bagofwords.com/enterprise"
          target="_blank"
          rel="noopener noreferrer"
          class="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700"
        >
          {{ $t('settings.licensePage.learnMore') }}
        </a>
      </div>

      <!-- Renew CTA when expired -->
      <div v-if="isExpired" class="rounded-lg border border-gray-200 dark:border-gray-700 p-5">
        <p class="text-sm text-gray-600 dark:text-gray-400 mb-3">
          {{ $t('settings.licensePage.renewInfo') }}
        </p>
        <a
          href="https://docs.bagofwords.com/enterprise"
          target="_blank"
          rel="noopener noreferrer"
          class="inline-flex items-center gap-1 text-xs text-blue-600 hover:text-blue-700"
        >
          {{ $t('settings.licensePage.learnMore') }}
        </a>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

definePageMeta({
  auth: true,
  permissions: ['manage_settings'],
  layout: 'settings'
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
