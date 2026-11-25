<template>
 <div class="min-w-0">
    <ToolCallHeader :expandable="false" :tool-execution="toolExecution" icon="heroicons-document-text" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ t('tools.createDashboard.creating') }}</template>
      <template v-else-if="status === 'success'">{{ t('tools.createDashboard.created') }}</template>
      <template v-else>{{ t('tools.createDashboard.create') }}</template></template>
    </ToolCallHeader>
    
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

const { t } = useResponseI18n()

import { ref, computed } from 'vue'
interface ToolExecution { status: string }

const props = defineProps<{
  toolExecution: ToolExecution
}>()

const status = computed(() => props.toolExecution.status)


const isCollapsed = ref(false)

function toggleCollapsed() {
  isCollapsed.value = !isCollapsed.value
}


</script>
