<template>
  <div class="chat-markdown-table min-w-0 max-w-full">
    <div class="mb-1 flex justify-end gap-1">
      <UButton color="gray" variant="ghost" size="xs" :icon="copied ? 'i-heroicons-check' : 'i-heroicons-square-2-stack'" :aria-label="t(copied ? 'common.copied' : 'common.copy')" :title="t(copied ? 'common.copied' : 'common.copy')" @click="copyTable" />
      <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-arrows-pointing-out" :aria-label="t('tools.common.expand')" :title="t('tools.common.expand')" @click="expanded = true" />
    </div>
    <ChatTableView :node="node" :custom-id="customId" :index-key="indexKey" />
    <UModal v-model="expanded" :ui="{ width: 'sm:max-w-7xl', margin: 'sm:my-8', padding: 'p-3 sm:p-6' }">
      <div class="p-4 sm:p-6">
        <div class="mb-3 flex items-center justify-between gap-3">
          <h2 class="font-semibold text-ink">{{ t('markdownTable.title') }}</h2>
          <div class="flex gap-1">
            <UButton color="gray" variant="ghost" icon="i-heroicons-square-2-stack" :aria-label="t('common.copy')" @click="copyTable" />
            <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark" :aria-label="t('common.close')" @click="expanded = false" />
          </div>
        </div>
        <ChatTableView :node="node" :custom-id="customId" :index-key="`${indexKey}-expanded`" expanded />
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import ChatTableView from './ChatTableView.vue'
defineOptions({ inheritAttrs: false })
const props = defineProps<{ node: any; customId?: string; indexKey?: string | number }>()
const { t } = useI18n()
const expanded = ref(false)
const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | undefined
async function copyTable() {
  try {
    await navigator.clipboard.writeText(props.node.raw || '')
    copied.value = true
    clearTimeout(copyTimer)
    copyTimer = setTimeout(() => { copied.value = false }, 1800)
  } catch {
    useToast().add({ title: t('markdownTable.copyFailed'), color: 'red' })
  }
}
onBeforeUnmount(() => clearTimeout(copyTimer))
</script>
