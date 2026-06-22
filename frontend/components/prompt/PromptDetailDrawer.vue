<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <div v-if="prompt" class="flex flex-col max-h-[85vh]">
      <!-- Header -->
      <div class="flex items-start justify-between px-5 py-4 border-b border-gray-100">
        <div class="min-w-0">
          <div class="flex items-center gap-1.5 mb-1 flex-wrap">
            <span v-if="prompt.category" class="text-[10px] px-1.5 py-0.5 rounded border text-gray-600 border-gray-200 bg-gray-50">{{ prompt.category }}</span>
            <span v-if="prompt.is_starter" class="text-[10px] px-1.5 py-0.5 rounded border text-amber-700 border-amber-200 bg-amber-50">Starter</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded border text-blue-700 border-blue-200 bg-blue-50 capitalize">{{ prompt.mode }}</span>
          </div>
          <h2 class="text-base font-semibold text-gray-900">{{ prompt.title }}</h2>
        </div>
        <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
      </div>

      <!-- Body -->
      <div class="flex-1 overflow-y-auto px-5 py-4 space-y-5">
        <div>
          <div class="text-xs font-medium text-gray-500 mb-1.5">Prompt</div>
          <p dir="auto" class="text-sm text-gray-800 whitespace-pre-wrap">{{ prompt.text }}</p>
        </div>

        <div v-if="prompt.tags && prompt.tags.length">
          <div class="text-xs font-medium text-gray-500 mb-1.5">Tags</div>
          <div class="flex flex-wrap gap-1">
            <span v-for="tag in prompt.tags" :key="tag" class="text-[11px] px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-600">#{{ tag }}</span>
          </div>
        </div>

        <div>
          <div class="text-xs font-medium text-gray-500 mb-1.5">Agents</div>
          <div v-if="prompt.data_source_ids && prompt.data_source_ids.length" class="flex flex-wrap gap-1.5">
            <span v-for="dsId in prompt.data_source_ids" :key="dsId" class="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">
              <DataSourceIcon v-if="dsType(dsId)" :type="dsType(dsId)" class="w-3 h-3" />
              {{ dsName(dsId) }}
            </span>
          </div>
          <div v-else class="text-xs text-gray-400">No agents attached.</div>
        </div>

        <div class="grid grid-cols-2 gap-3 text-xs">
          <div>
            <div class="text-gray-400">Default schedule</div>
            <div class="text-gray-700">{{ cronLabel || '—' }}</div>
          </div>
          <div>
            <div class="text-gray-400">Default channel</div>
            <div class="text-gray-700 capitalize">{{ channelLabel || '—' }}</div>
          </div>
          <div>
            <div class="text-gray-400">Subscribers</div>
            <div class="text-gray-700">{{ prompt.subscriber_count }}</div>
          </div>
          <div>
            <div class="text-gray-400">Scope</div>
            <div class="text-gray-700 capitalize">{{ prompt.scope }}</div>
          </div>
        </div>
      </div>

      <!-- Footer actions -->
      <div class="px-5 py-4 border-t border-gray-100 flex items-center gap-2">
        <UButton size="xs" color="blue" :loading="running" @click="emit('run', prompt)">Try now</UButton>
        <UButton size="xs" color="gray" variant="soft" @click="emit('subscribe', prompt)">Subscribe</UButton>
        <UButton v-if="prompt.can_assign" size="xs" color="gray" variant="soft" @click="emit('assign', prompt)">Assign</UButton>
        <UButton v-if="prompt.can_manage" size="xs" color="gray" variant="ghost" icon="i-heroicons-pencil-square" @click="emit('edit', prompt)">Edit</UButton>
        <UButton v-if="prompt.can_manage" size="xs" color="red" variant="ghost" icon="i-heroicons-trash" class="ml-auto" :loading="deleting" @click="emit('delete', prompt)" />
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import DataSourceIcon from '@/components/DataSourceIcon.vue'
import { PROMPT_CHANNELS, type PromptResponse } from '@/composables/usePrompts'

const props = defineProps<{
  prompt: PromptResponse | null
  running?: boolean
  deleting?: boolean
  dataSources?: { id: string; name: string; type?: string }[]
}>()
const emit = defineEmits(['run', 'subscribe', 'assign', 'edit', 'delete'])

const isOpen = defineModel<boolean>({ default: false })

const { getCronLabel } = useCronLabel()
const cronLabel = computed(() => getCronLabel(props.prompt?.default_cron || ''))
const channelLabel = computed(() =>
  PROMPT_CHANNELS.find((c) => c.value === props.prompt?.default_channel)?.label || props.prompt?.default_channel || ''
)

function dsName(id: string) {
  return props.dataSources?.find((d) => d.id === id)?.name || id
}
function dsType(id: string) {
  return props.dataSources?.find((d) => d.id === id)?.type || ''
}
</script>
