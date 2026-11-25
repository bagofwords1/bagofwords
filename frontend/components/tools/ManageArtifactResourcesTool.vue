<template>
  <div class="min-w-0">
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-book-open" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>{{ label }}<template v-if="name"> · {{ name }}</template>
      <template v-if="result.revision">r{{ result.revision }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
    <div v-if="toolHeaderExpanded" class="ms-5 mt-2 space-y-2 rounded-md border border-gray-200 dark:border-gray-700 p-3 max-w-xl">
      <p v-if="failed" role="alert">{{ result.error || toolExecution.error_message || t('tools.resource.failed') }}</p>
      <p v-if="result.committed === false">{{ t('tools.resource.noChanges') }}</p>
      <p v-else-if="result.records_preserved">{{ t('tools.resource.preserved') }}</p>
      <dl v-if="result.changed_sections?.length" class="flex gap-2"><dt>{{ t('tools.resource.changed') }}</dt><dd>{{ result.changed_sections.join(', ') }}</dd></dl>
      <details v-if="result.changes && Object.keys(result.changes).length"><summary class="cursor-pointer">{{ t('tools.resource.changed') }}</summary><pre class="mt-2 text-chat-2 whitespace-pre-wrap break-words max-h-64 overflow-auto">{{ JSON.stringify(result.changes, null, 2) }}</pre></details>
      <details v-if="result.definition"><summary class="cursor-pointer">{{ t('artifactResources.schema') }}</summary><pre class="mt-2 text-chat-2 whitespace-pre-wrap break-words max-h-64 overflow-auto">{{ JSON.stringify(result.definition, null, 2) }}</pre></details>
      <UButton v-if="artifactId" size="xs" color="gray" variant="ghost" icon="i-heroicons-circle-stack" @click="inspect = true">{{ t('artifactResources.data') }}</UButton>
    </div>
    <ArtifactResourceExplorer v-if="inspect && artifactId" :artifact-id="artifactId" @close="inspect = false" />
  
    </div>
  </div>
</template>
<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

const props = defineProps<{toolExecution: any}>()
const {t} = useResponseI18n()
const expanded = ref(false), inspect = ref(false)
const result = computed(()=>({...props.toolExecution.result_json, ...props.toolExecution.result_json?.observation}))
const args = computed(()=>props.toolExecution.arguments_json || {})
const name = computed(()=>result.value.name || args.value.resource || args.value.definition?.name)
const artifactId = computed(()=>result.value.resource_artifact_id || args.value.artifact_id)
const failed = computed(()=>props.toolExecution.status === 'error' || result.value.success === false || !!result.value.error)
const label = computed(()=>{
  if(failed.value) return t('tools.resource.failed')
  if(['running','pending','in_progress'].includes(props.toolExecution.status)) return t('tools.resource.working')
  if(props.toolExecution.status === 'stopped') return t('tools.resource.stopped')
  if(result.value.changed_sections?.length === 0) return t('tools.resource.unchanged')
  const action=result.value.action || args.value.action
  return t(`tools.resource.${['create','update','delete'].includes(action) ? action : 'update'}`)
})
</script>
