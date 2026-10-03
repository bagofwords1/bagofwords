<template>
  <div class="min-h-screen flex items-center justify-center px-6 py-24">
    <div class="w-full max-w-[360px] text-center">
      <p class="text-[11px] uppercase tracking-widest text-gray-400 dark:text-gray-500">
        {{ $t('errorPage.notFoundTitle') }}
      </p>
      <h1 class="mt-2 text-[22px] leading-tight font-semibold tracking-tight text-gray-900 dark:text-white">
        {{ $t('errorPage.notFoundMessage') }}
      </h1>
      <p class="mt-1.5 text-sm text-gray-500 dark:text-gray-400">
        {{ $t('errorPage.notFoundHint') }}
      </p>

      <NuxtLink to="/" :class="primaryButtonClass">
        {{ $t('errorPage.goHome') }}
      </NuxtLink>
    </div>
  </div>
</template>

<script setup lang="ts">
  // Public: reached from shared reports (/r/:id) by signed-out viewers too.
  definePageMeta({ layout: 'users', auth: false })

  // Same primary button as the auth pages (users/sign-in.vue).
  const primaryButtonClass =
    'mt-7 w-full h-10 inline-flex items-center justify-center rounded-lg text-sm font-medium ' +
    'text-white bg-gray-900 hover:bg-gray-800 dark:text-gray-900 dark:bg-white dark:hover:bg-gray-100 ' +
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 ' +
    'focus-visible:ring-offset-2 dark:focus-visible:ring-white dark:focus-visible:ring-offset-gray-950 transition-colors'
</script>
