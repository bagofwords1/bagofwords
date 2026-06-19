<template>
  <div
    class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow cursor-pointer transition-all"
    @click="emit('click')"
  >
    <Icon name="heroicons-clock" class="w-4 h-4 flex-shrink-0 text-gray-400" />
    <div class="flex-1 min-w-0">
      <div dir="auto" class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ promptContent || 'Untitled' }}</div>
      <div class="flex items-center gap-2 mt-0.5">
        <span class="text-[11px] text-gray-400">{{ getCronLabel(scheduledPrompt?.cron_schedule) }}</span>
        <span
          class="inline-flex items-center gap-1 text-[11px]"
          :class="isActive ? 'text-green-500' : 'text-gray-400'"
        >
          <span class="w-1.5 h-1.5 rounded-full" :class="isActive ? 'bg-green-400' : 'bg-gray-300'" />
          {{ isActive ? 'Active' : 'Paused' }}
        </span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ScheduledPromptLike {
  id?: string
  cron_schedule?: string
  is_active?: boolean
  prompt?: { content?: string } | null
}

const props = defineProps<{ scheduledPrompt: ScheduledPromptLike }>()
const emit = defineEmits<{ (e: 'click'): void }>()

const { getCronLabel } = useCronLabel()

const promptContent = computed(() => props.scheduledPrompt?.prompt?.content || '')
// Default to active when unspecified (a freshly created task is active).
const isActive = computed(() => props.scheduledPrompt?.is_active !== false)
</script>
