<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl', container: 'items-start', margin: 'sm:mt-[8vh]' }">
    <div class="bg-white dark:bg-gray-900 rounded-lg overflow-hidden flex flex-col max-h-[82vh]">
      <!-- Header -->
      <div class="relative px-5 py-4 border-b border-gray-100 dark:border-gray-800 bg-gradient-to-br from-blue-50/80 via-white to-white dark:from-blue-950/30 dark:via-gray-900 dark:to-gray-900">
        <div class="flex items-start justify-between gap-3">
          <div class="flex items-start gap-3 min-w-0">
            <div class="flex items-center justify-center w-9 h-9 rounded-lg bg-blue-500/10 dark:bg-blue-500/15 shrink-0">
              <UIcon name="i-heroicons-sparkles" class="w-5 h-5 text-blue-500" />
            </div>
            <div class="min-w-0">
              <h2 class="text-[15px] font-semibold text-gray-900 dark:text-gray-100">{{ $t('changelog.title') }}</h2>
              <p class="text-[12px] text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('changelog.subtitle') }}</p>
            </div>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <span v-if="currentVersion" class="hidden sm:inline-flex items-center px-2 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-[11px] font-medium text-gray-500 dark:text-gray-400 font-mono">
              v{{ currentVersion }}
            </span>
            <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
          </div>
        </div>
      </div>

      <!-- Body -->
      <div class="flex-1 overflow-y-auto px-5 py-4 min-h-[240px]">
        <!-- Loading -->
        <div v-if="loading" class="py-16 flex justify-center">
          <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
        </div>

        <!-- Error -->
        <div v-else-if="error" class="py-16 flex flex-col items-center text-center gap-2">
          <div class="flex items-center justify-center w-12 h-12 rounded-full bg-red-50 dark:bg-red-500/10">
            <UIcon name="i-heroicons-exclamation-triangle" class="w-6 h-6 text-red-400 dark:text-red-500" />
          </div>
          <p class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('changelog.errorTitle') }}</p>
          <p class="text-xs text-gray-400 dark:text-gray-500">{{ $t('changelog.errorBody') }}</p>
          <button @click="fetchChangelog()" class="mt-1 text-[12px] text-blue-600 dark:text-blue-400 hover:underline px-2 py-1 rounded-md">
            {{ $t('changelog.retry') }}
          </button>
        </div>

        <!-- Empty -->
        <div v-else-if="!versions.length" class="py-16 flex flex-col items-center text-center gap-2">
          <div class="flex items-center justify-center w-12 h-12 rounded-full bg-gray-50 dark:bg-gray-800">
            <UIcon name="i-heroicons-document-text" class="w-6 h-6 text-gray-300 dark:text-gray-600" />
          </div>
          <p class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('changelog.emptyTitle') }}</p>
        </div>

        <!-- Timeline -->
        <ol v-else class="relative ms-2">
          <li
            v-for="(v, i) in versions"
            :key="v.version + i"
            class="relative ps-6 pb-6 last:pb-0 border-s border-gray-150 dark:border-gray-800"
          >
            <!-- Timeline dot -->
            <span
              class="absolute -start-[5px] top-1 w-[9px] h-[9px] rounded-full ring-4 ring-white dark:ring-gray-900"
              :class="i === 0 ? 'bg-blue-500' : 'bg-gray-300 dark:bg-gray-600'"
            />
            <!-- Version header -->
            <div class="flex items-center flex-wrap gap-x-2 gap-y-1 mb-2">
              <span
                class="text-[13px] font-semibold font-mono"
                :class="i === 0 ? 'text-gray-900 dark:text-gray-100' : 'text-gray-700 dark:text-gray-300'"
              >v{{ v.version }}</span>
              <span
                v-if="i === 0"
                class="inline-flex items-center px-1.5 py-0.5 rounded-full bg-emerald-50 dark:bg-emerald-500/15 text-[10px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400"
              >{{ $t('changelog.latest') }}</span>
              <span v-if="v.date" class="text-[11px] text-gray-400 dark:text-gray-500">· {{ v.date }}</span>
            </div>
            <!-- Entries -->
            <ul class="space-y-1.5">
              <li
                v-for="(entry, j) in v.entries"
                :key="j"
                class="flex gap-2 text-[13px] leading-relaxed text-gray-600 dark:text-gray-300"
              >
                <span class="mt-[7px] w-1 h-1 rounded-full bg-gray-300 dark:bg-gray-600 shrink-0" />
                <span class="changelog-entry min-w-0" v-html="renderEntry(entry)" />
              </li>
            </ul>
          </li>
        </ol>
      </div>

      <!-- Footer -->
      <div class="flex items-center justify-between px-5 py-2.5 border-t border-gray-100 dark:border-gray-800">
        <a
          href="https://github.com/bagofwords1/bagofwords/releases"
          target="_blank"
          rel="noopener noreferrer"
          class="inline-flex items-center gap-1 text-[12px] text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 transition-colors"
        >
          <UIcon name="i-heroicons-arrow-top-right-on-square" class="w-3.5 h-3.5" />
          {{ $t('changelog.viewOnGithub') }}
        </a>
        <button
          @click="isOpen = false"
          class="text-[12px] px-3 py-1.5 rounded-md text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors"
        >{{ $t('common.close') }}</button>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

interface ChangelogVersion {
  version: string
  date: string | null
  entries: string[]
}

const versions = ref<ChangelogVersion[]>([])
const currentVersion = ref<string>('')
const loading = ref(false)
const error = ref(false)
const loaded = ref(false)

// Inline-only markdown: entries are single sentences with **bold**, `code`
// and the occasional link — render them without wrapping <p> tags.
const md = new MarkdownIt({ html: false, breaks: false, linkify: true })
const renderEntry = (text: string): string => md.renderInline(text || '')

const fetchChangelog = async () => {
  loading.value = true
  error.value = false
  try {
    const resp: any = await useMyFetch('/api/changelog')
    if (resp?.error?.value) throw resp.error.value
    const body = resp?.data?.value
    versions.value = (body?.versions || []) as ChangelogVersion[]
    currentVersion.value = body?.current_version || ''
    loaded.value = true
  } catch (e) {
    error.value = true
  } finally {
    loading.value = false
  }
}

// Lazy-load: only fetch the (large) changelog the first time the modal opens.
watch(isOpen, (open) => {
  if (open && !loaded.value && !loading.value) fetchChangelog()
})
</script>

<style scoped>
.changelog-entry :deep(strong) {
  font-weight: 600;
  color: #111827;
}
.changelog-entry :deep(code) {
  background: #f3f4f6;
  padding: 1px 5px;
  border-radius: 4px;
  font-family: ui-monospace, monospace;
  font-size: 0.85em;
  color: #374151;
}
.changelog-entry :deep(a) {
  color: #2563eb;
  text-decoration: none;
}
.changelog-entry :deep(a:hover) { text-decoration: underline; }

/* Dark mode overrides. The `.dark` class lives on <html> (Tailwind darkMode:
   'class'), outside this component's scope, so — mirroring InstructionText.vue —
   these are authored as :global and matched by the unique `.changelog-entry`
   class. A `:global(.dark) .changelog-entry` prefix compiles to an invalid
   selector and silently drops, leaving bold text near-black on a dark bg. */
:global(.dark .changelog-entry strong) { color: #f3f4f6; }
:global(.dark .changelog-entry code) { background: #374151; color: #e5e7eb; }
:global(.dark .changelog-entry a) { color: #60a5fa; }
</style>
