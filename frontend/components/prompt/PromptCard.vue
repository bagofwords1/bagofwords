<template>
  <div
    class="group border border-gray-100 bg-white rounded-lg p-4 hover:shadow-md hover:border-gray-200 transition-all cursor-pointer flex flex-col"
    @click="emit('open', prompt)"
  >
    <!-- Badges row -->
    <div class="flex items-center gap-1.5 mb-2 flex-wrap">
      <span v-if="prompt.category" class="text-[10px] px-1.5 py-0.5 rounded border text-gray-600 border-gray-200 bg-gray-50">{{ prompt.category }}</span>
      <span v-if="prompt.is_starter" class="text-[10px] px-1.5 py-0.5 rounded border text-amber-700 border-amber-200 bg-amber-50">Starter</span>
      <span v-if="prompt.status === 'draft'" class="text-[10px] px-1.5 py-0.5 rounded border text-gray-700 border-gray-200 bg-gray-50">Draft</span>
      <span v-if="prompt.mode" class="text-[10px] px-1.5 py-0.5 rounded border text-blue-700 border-blue-200 bg-blue-50 capitalize">{{ prompt.mode }}</span>
    </div>

    <!-- Title + text -->
    <div class="text-sm font-semibold text-gray-900 mb-1 line-clamp-1">{{ prompt.title }}</div>
    <div dir="auto" class="text-xs text-gray-500 line-clamp-2 mb-2">{{ prompt.text }}</div>

    <!-- Tags -->
    <div v-if="prompt.tags && prompt.tags.length" class="flex flex-wrap gap-1 mb-2">
      <span v-for="tag in prompt.tags.slice(0, 4)" :key="tag" class="text-[10px] text-gray-400">#{{ tag }}</span>
    </div>

    <!-- Meta -->
    <div class="flex items-center gap-3 text-[11px] text-gray-400 mt-auto pt-2">
      <span v-if="cronLabel" class="inline-flex items-center gap-1">
        <UIcon name="heroicons-clock" class="w-3 h-3" /> {{ cronLabel }}
      </span>
      <span v-if="prompt.default_channel" class="inline-flex items-center gap-1 capitalize">
        <UIcon name="heroicons-paper-airplane" class="w-3 h-3" /> {{ channelLabel }}
      </span>
      <span class="inline-flex items-center gap-1">
        <UIcon name="heroicons-user-group" class="w-3 h-3" /> {{ prompt.subscriber_count }}
      </span>
    </div>

    <!-- Actions -->
    <div class="flex items-center gap-2 mt-3" @click.stop>
      <UButton size="2xs" color="blue" :loading="running" @click="emit('run', prompt)">Try now</UButton>
      <UButton size="2xs" color="gray" variant="soft" @click="emit('subscribe', prompt)">Subscribe</UButton>
      <UButton v-if="prompt.can_assign" size="2xs" color="gray" variant="soft" @click="emit('assign', prompt)">Assign</UButton>
      <UButton v-if="prompt.can_manage" size="2xs" color="gray" variant="ghost" icon="i-heroicons-pencil-square" @click="emit('edit', prompt)" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { PROMPT_CHANNELS, type PromptResponse } from '@/composables/usePrompts'

const props = defineProps<{ prompt: PromptResponse; running?: boolean }>()
const emit = defineEmits(['open', 'run', 'subscribe', 'assign', 'edit'])

const { getCronLabel } = useCronLabel()
const cronLabel = computed(() => getCronLabel(props.prompt.default_cron || ''))
const channelLabel = computed(() =>
  PROMPT_CHANNELS.find((c) => c.value === props.prompt.default_channel)?.label || props.prompt.default_channel
)
</script>
