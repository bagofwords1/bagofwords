<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900">
      License
      <p class="text-sm text-gray-500 font-normal mb-8">
        View your license status.
      </p>
    </h2>

    <div v-if="loading" class="py-4">
      <ULoader />
    </div>

    <div v-else class="space-y-6">
      <!-- License Status Card -->
      <div class="rounded-lg border border-gray-200 p-5">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-3">
            <div
              :class="[
                'w-8 h-8 rounded-full flex items-center justify-center',
                isLicensed ? 'bg-green-50' : 'bg-gray-50'
              ]"
            >
              <svg
                v-if="isLicensed"
                class="w-4 h-4 text-green-600"
                fill="none"
                viewBox="0 0 24 24"
                stroke-width="2"
                stroke="currentColor"
              >
                <path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5" />
              </svg>
              <svg
                v-else
                class="w-4 h-4 text-gray-400"
                fill="none"
                viewBox="0 0 24 24"
                stroke-width="2"
                stroke="currentColor"
              >
                <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 12h-15" />
              </svg>
            </div>
            <div>
              <p class="text-sm font-medium text-gray-900">
                {{ isLicensed ? 'Enterprise' : 'Community Edition' }}
              </p>
              <p class="text-xs text-gray-500">
                <template v-if="isLicensed && license?.org_name">
                  {{ license.org_name }}
                </template>
                <template v-else-if="isExpired">
                  License expired
                </template>
                <template v-else>
                  Free and open source
                </template>
              </p>
            </div>
          </div>
          <UBadge
            :color="isLicensed ? 'green' : isExpired ? 'red' : 'gray'"
            variant="subtle"
            size="xs"
          >
            {{ isLicensed ? 'Active' : isExpired ? 'Expired' : 'Community' }}
          </UBadge>
        </div>

        <!-- License Details (if licensed) -->
        <div v-if="isLicensed || isExpired" class="mt-4 pt-4 border-t border-gray-100 space-y-2">
          <div class="flex justify-between text-xs">
            <span class="text-gray-500">License ID</span>
            <span class="font-mono text-gray-700">{{ license?.license_id || '-' }}</span>
          </div>
          <div v-if="expiresAt" class="flex justify-between text-xs">
            <span class="text-gray-500">Expires</span>
            <span :class="isExpiringSoon ? 'text-amber-600' : 'text-gray-700'">
              {{ formatDate(expiresAt) }}
              <template v-if="daysUntilExpiry && daysUntilExpiry > 0">
                ({{ daysUntilExpiry }} days)
              </template>
            </span>
          </div>
        </div>

        <!-- Expiry Warning -->
        <UAlert
          v-if="isExpiringSoon"
          class="mt-4"
          color="amber"
          variant="subtle"
          icon="i-heroicons-exclamation-triangle"
        >
          Your license expires in {{ daysUntilExpiry }} days. Contact us to renew.
        </UAlert>
      </div>

      <!-- Enterprise Info (only show when not licensed) -->
      <div v-if="!isLicensed" class="rounded-lg border border-gray-200 p-5">
        <p class="text-sm text-gray-600 mb-3">
          Unlock audit logs, advanced role-based access control, and data retention policies with an enterprise license.
        </p>
        <p class="text-xs text-gray-500 mb-3">
          Set <code class="bg-gray-100 px-1 py-0.5 rounded text-xs">BOW_LICENSE_KEY</code> and restart to activate.
        </p>
        <a
          href="https://docs.bagofwords/enterprise"
          target="_blank"
          rel="noopener noreferrer"
          class="text-xs text-blue-600 hover:text-blue-700"
        >
          Learn more →
        </a>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({
  auth: true,
  permissions: ['view_settings'],
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
