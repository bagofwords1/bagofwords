<template>
  <img v-if="src" :src="src" alt="" class="object-contain" :class="sizeClass" />
  <UIcon v-else :name="icon" :class="[sizeClass, 'text-gray-500 dark:text-gray-400']" />
</template>

<script setup lang="ts">
// Brand assets that already ship with the app; the rest use a neutral glyph
// rather than fetching third-party logos.
const props = withDefaults(defineProps<{ destination: string; size?: 'sm' | 'md' }>(), { size: 'sm' })
const ASSETS: Record<string, string> = {
  splunk: '/data_sources_icons/splunk.png',
  s3: '/data_sources_icons/s3.svg',
  gcs: '/icons/google.svg',
  sentinel: '/icons/microsoft.svg',
}
const ICONS: Record<string, string> = {
  datadog: 'i-heroicons-chart-bar-square',
  https: 'i-heroicons-globe-alt',
  syslog: 'i-heroicons-server-stack',
}
const src = computed(() => ASSETS[props.destination])
const icon = computed(() => ICONS[props.destination] || 'i-heroicons-arrow-up-tray')
const sizeClass = computed(() => (props.size === 'md' ? 'w-6 h-6' : 'w-4 h-4'))
</script>
