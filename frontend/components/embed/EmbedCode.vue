<template>
  <!-- A light code block with an optional language switch and a copy button.
       Code is always left-to-right, also in RTL locales. -->
  <div class="overflow-hidden rounded-lg border border-gray-200 bg-gray-50/60 dark:border-gray-800 dark:bg-gray-900/60" dir="ltr">
    <div class="flex items-center justify-between gap-2 border-b border-gray-200 px-2 py-1 dark:border-gray-800">
      <div class="flex gap-0.5">
        <button v-for="l in langs" :key="l.key" type="button" :data-testid="`code-lang-${l.key}`"
                class="rounded px-2 py-0.5 text-2xs transition-colors"
                :class="l.key === modelValue ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-800 dark:text-white' : 'text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200'"
                @click="emit('update:modelValue', l.key)">{{ l.label }}</button>
      </div>
      <button type="button" class="flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200"
              :aria-label="$t('hostApps.copy')" @click="copy">
        <UIcon :name="copied ? 'i-heroicons-check' : 'i-heroicons-clipboard-document'" class="h-3.5 w-3.5" />
        {{ copied ? $t('hostApps.copied') : $t('hostApps.copy') }}
      </button>
    </div>
    <pre class="overflow-x-auto px-3 py-2.5 font-mono text-2xs leading-5 text-gray-800 dark:text-gray-200" :data-testid="testid">{{ code }}</pre>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{ code: string; langs?: { key: string; label: string }[]; modelValue?: string; testid?: string }>()
const emit = defineEmits<{ (e: 'update:modelValue', value: string): void }>()
const copied = ref(false)
async function copy() {
  try {
    await navigator.clipboard.writeText(props.code)
    copied.value = true
    setTimeout(() => { copied.value = false }, 1500)
  } catch { /* no clipboard */ }
}
</script>
