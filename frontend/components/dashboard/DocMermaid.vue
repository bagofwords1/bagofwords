<template>
  <div class="doc-mermaid my-6">
    <!-- Successful diagram -->
    <div
      v-if="svg && !failed"
      class="flex justify-center overflow-x-auto rounded-lg border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 p-4"
      v-html="svg"
    />
    <!-- Loading -->
    <div v-else-if="!failed" class="flex items-center justify-center py-8">
      <Spinner class="w-4 h-4 text-gray-300" />
    </div>
    <!-- Fallback: show source, never a broken page -->
    <div v-else class="rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50 dark:bg-gray-900 p-4">
      <div class="mb-1 text-[11px] uppercase tracking-wide text-gray-400">{{ $t('docViewer.diagramFailed') }}</div>
      <pre class="text-xs text-gray-500 dark:text-gray-400 overflow-x-auto">{{ code }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import Spinner from '~/components/Spinner.vue'

// keepLastGood: while the code is being live-edited (doc editor preview), a
// parse failure keeps the last successful diagram instead of flashing the
// source fallback on every intermediate keystroke.
const props = defineProps<{ code: string; keepLastGood?: boolean }>()

const svg = ref<string>('')
const failed = ref(false)
let seq = 0

async function render() {
  if (!props.keepLastGood) {
    failed.value = false
    svg.value = ''
  }
  const myTicket = ++seq
  try {
    const mermaid = (await import('mermaid')).default
    const isDark = document.documentElement.classList.contains('dark')
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: isDark ? 'dark' : 'neutral',
      fontFamily: 'ui-sans-serif, system-ui, sans-serif',
    })
    const id = `doc-mermaid-${Math.random().toString(36).slice(2)}`
    const { svg: rendered } = await mermaid.render(id, props.code)
    if (myTicket === seq) { svg.value = rendered; failed.value = false }
  } catch (e) {
    if (myTicket === seq && !(props.keepLastGood && svg.value)) failed.value = true
  }
}

onMounted(render)
watch(() => props.code, render)
</script>

<style scoped>
.doc-mermaid :deep(svg) { max-width: 100%; height: auto; }
</style>
