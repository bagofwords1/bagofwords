<template>
  <div v-if="guideSteps.length" class="p-3 rounded border border-blue-200 dark:border-blue-900 bg-blue-50/40 dark:bg-blue-950/20" data-testid="setup-guide">
    <button type="button" class="flex items-center justify-between w-full text-start" @click="open = !open">
      <span class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('data.setupGuideTitle') }}</span>
      <UIcon :name="open ? 'i-heroicons-chevron-up' : 'i-heroicons-chevron-down'" class="w-4 h-4 text-ink-muted" />
    </button>
    <ol v-show="open" class="mt-2 space-y-3">
      <li v-for="(step, i) in guideSteps" :key="i" class="flex gap-2">
        <span class="flex-none w-5 h-5 rounded-full bg-blue-600 text-white text-2xs font-semibold flex items-center justify-center mt-0.5">{{ i + 1 }}</span>
        <div class="min-w-0 flex-1">
          <div class="text-xs font-medium text-gray-800 dark:text-gray-200">{{ stepText(step, i, 'title') }}</div>
          <div v-if="stepText(step, i, 'body')" class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 whitespace-pre-line">{{ stepText(step, i, 'body') }}</div>
          <div v-if="step.code" class="relative mt-1.5">
            <pre class="text-2xs leading-snug bg-gray-900 text-gray-100 rounded-md p-2 pe-16 overflow-x-auto max-h-56 rtl-no-flip" dir="ltr"><code>{{ step.code }}</code></pre>
            <button type="button" class="absolute top-1.5 end-1.5 text-2xs px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600 text-gray-100" @click="copyStep(step.code, i)">
              {{ copiedStep === i ? $t('data.setupGuideCopied') : (copyFailedStep === i ? $t('data.setupGuideCopyFailed') : $t('data.setupGuideCopy')) }}
            </button>
          </div>
        </div>
      </li>
    </ol>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = withDefaults(defineProps<{
  type: string
  steps?: any[] | null
  defaultOpen?: boolean
}>(), { defaultOpen: true })

const { t, te } = useI18n()

const guideSteps = computed<any[]>(() => Array.isArray(props.steps) ? props.steps : [])
const open = ref(props.defaultOpen)
const copiedStep = ref<number | null>(null)
const copyFailedStep = ref<number | null>(null)

function stepText(step: any, i: number, key: 'title' | 'body'): string {
  const k = `data.setupGuides.${props.type}.${i}.${key}`
  return te(k) ? t(k) : String(step?.[key] || '')
}

async function copyStep(code: string, i: number) {
  // navigator.clipboard is missing over plain http, so fall back to execCommand.
  let ok = false
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(code)
      ok = true
    }
  } catch {}
  if (!ok) {
    try {
      const ta = document.createElement('textarea')
      ta.value = code
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.top = '-1000px'
      document.body.appendChild(ta)
      try {
        ta.select()
        ok = document.execCommand('copy')
      } finally {
        document.body.removeChild(ta)
      }
    } catch {}
  }
  if (ok) {
    copyFailedStep.value = null
    copiedStep.value = i
    setTimeout(() => { if (copiedStep.value === i) copiedStep.value = null }, 1500)
  } else {
    copiedStep.value = null
    copyFailedStep.value = i
    setTimeout(() => { if (copyFailedStep.value === i) copyFailedStep.value = null }, 2500)
  }
}
</script>
