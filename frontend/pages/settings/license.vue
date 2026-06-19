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

      <!-- Enterprise Info (only show when not licensed and not expired) -->
      <div v-if="!isLicensed && !isExpired" class="rounded-lg border border-gray-200 dark:border-gray-700 p-5">
        <p class="text-sm text-gray-700 dark:text-gray-300 font-medium mb-1">
          {{ $t('settings.licensePage.enterpriseTitle') }}
        </p>
        <p class="text-sm text-gray-600 dark:text-gray-400 mb-3">
          {{ $t('settings.licensePage.enterpriseInfo') }}
        </p>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
          <i18n-t keypath="settings.licensePage.activateHint" tag="span">
            <template #envvar>
              <code class="bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded text-xs">BOW_LICENSE_KEY</code>
            </template>
          </i18n-t>
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
definePageMeta({
  auth: true,
  permissions: ['manage_settings'],
  layout: 'settings'
})

const { license, loading, isLicensed, isExpired, expiresAt, daysUntilExpiry, isExpiringSoon } = useEnterprise()

const formatDate = (date: Date) => {
  return date.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric'
  })
}
</script>
