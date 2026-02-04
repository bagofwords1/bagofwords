<script setup lang="ts">
// Enterprise Upgrade Banner
// Licensed under the Business Source License 1.1

defineProps<{
  feature?: string
  title?: string
  description?: string
}>()

const defaultTitle = 'Enterprise Feature'
const defaultDescription = 'This feature requires an enterprise license. Contact us to learn more about enterprise features.'
</script>

<template>
  <div class="rounded-lg border border-blue-200 bg-blue-50 p-6">
    <div class="flex items-start gap-4">
      <div class="flex-shrink-0">
        <svg class="h-6 w-6 text-blue-600" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
        </svg>
      </div>
      <div class="flex-1">
        <h3 class="text-sm font-semibold text-blue-900">
          {{ title || defaultTitle }}
        </h3>
        <p class="mt-1 text-sm text-blue-700">
          {{ description || defaultDescription }}
        </p>
        <div class="mt-4">
          <a
            href="https://bagofwords.com/pricing"
            target="_blank"
            rel="noopener noreferrer"
            class="inline-flex items-center gap-1 text-sm font-medium text-blue-600 hover:text-blue-800"
          >
            Learn about Enterprise
            <svg class="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25" />
            </svg>
          </a>
        </div>
      </div>
    </div>
  </div>
</template>
