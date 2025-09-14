<template>
 <div class="mb-2">
    <div class="flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700" @click="toggleCollapsed">

      <!-- Status icon -->
      <Icon v-if="status === 'success'" name="heroicons-check" class="w-3 h-3 mr-1.5 text-green-500" />
      <Icon v-else-if="status === 'error'" name="heroicons-x-mark" class="w-3 h-3 mr-1.5 text-red-500" />
      
      <!-- Action label with shimmer effect for running status -->
      <span v-if="status === 'running'" class="tool-shimmer inline-flex items-center">
        <Spinner class="w-3 h-3 mr-1.5 text-gray-400" />
        Creating dashboard
      </span>
      <span v-else-if="status === 'success'" class="text-gray-700">Dashboard created</span>
      <span v-else class="text-gray-700">Create dashboard</span>
  </div>
</div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import Spinner from '~/components/Spinner.vue'
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