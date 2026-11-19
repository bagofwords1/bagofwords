<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-camera" :label="title || t(status === 'running' ? 'tools.browser.capturing' : 'tools.browser.screenshot')" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded" />
    <div v-if="toolHeaderExpanded" class="mt-2 min-w-0 text-chat-2">
      <div v-if="displayUrl" class="mb-2 text-chat-3 text-ink-muted" dir="ltr">{{ displayUrl }}</div>
      <AuthenticatedImage v-if="fileId" :file-id="fileId" :alt="title || t(status === 'running' ? 'tools.browser.capturing' : 'tools.browser.screenshot')" img-class="max-h-96 w-auto object-contain rounded-lg border border-line" />
      <div v-if="error" class="text-ink-muted break-words">{{ error }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)
const { t } = useResponseI18n()

import { computed } from 'vue'
import AuthenticatedImage from '~/components/AuthenticatedImage.vue'

const props = defineProps<{
  toolExecution: { status: string; result_json?: any; arguments_json?: any }
}>()

const status = computed(() => props.toolExecution?.status || '')
const rj = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const fileId = computed(() => rj.value.screenshot_file_id || '')
const title = computed(() => args.value.title || '')
const displayUrl = computed(() => rj.value.artifact ? '' : (rj.value.url || ''))
const error = computed(() => rj.value.error_message || '')
</script>

<style scoped>
.tool-call-details { overflow-wrap: anywhere; }
</style>
