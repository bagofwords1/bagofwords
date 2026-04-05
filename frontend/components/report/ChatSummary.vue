<template>
  <div class="h-full overflow-y-auto">
    <div class="max-w-xl mx-auto px-4 py-6 space-y-6">

      <!-- Empty state -->
      <div v-if="!hasAnything" class="flex flex-col items-center justify-center h-64 text-gray-400">
        <Icon name="heroicons-document-text" class="w-8 h-8 mb-2" />
        <span class="text-sm">No items yet</span>
      </div>

      <!-- Scheduled Tasks -->
      <section v-if="scheduledPrompts.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Scheduled Tasks</h3>
        <ul class="space-y-1.5">
          <li
            v-for="sp in scheduledPrompts"
            :key="sp.id"
            class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white border border-gray-100 shadow-sm hover:shadow cursor-pointer transition-all"
            @click="emit('editScheduledPrompt', sp)"
          >
            <Icon name="heroicons-clock" class="w-4 h-4 flex-shrink-0 text-gray-400" />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 truncate">{{ sp.prompt?.content || 'Untitled' }}</div>
              <div class="flex items-center gap-2 mt-0.5">
                <span class="text-[11px] text-gray-400">{{ getCronLabel(sp.cron_schedule) }}</span>
                <span
                  class="inline-flex items-center gap-1 text-[11px]"
                  :class="sp.is_active ? 'text-green-500' : 'text-gray-400'"
                >
                  <span class="w-1.5 h-1.5 rounded-full" :class="sp.is_active ? 'bg-green-400' : 'bg-gray-300'" />
                  {{ sp.is_active ? 'Active' : 'Paused' }}
                </span>
              </div>
            </div>
          </li>
        </ul>
      </section>

      <!-- Artifacts -->
      <section v-if="artifactList.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Artifacts</h3>
        <ul class="space-y-1.5">
          <li
            v-for="art in artifactList"
            :key="art.id"
            class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white border border-gray-100 shadow-sm hover:shadow cursor-pointer transition-all"
            @click="emit('openArtifact', { artifactId: art.id })"
          >
            <Icon name="heroicons:squares-plus" class="w-4 h-4 flex-shrink-0 text-blue-500" />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 truncate">{{ art.title || 'Untitled' }}</div>
              <div v-if="art.mode" class="text-[11px] text-gray-400 mt-0.5">{{ art.mode }}</div>
            </div>
          </li>
        </ul>
      </section>

      <!-- Queries -->
      <section v-if="queryExecutions.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Queries</h3>
        <div class="space-y-2">
          <ToolWidgetPreview
            v-for="te in queryExecutions"
            :key="te.id"
            :tool-execution="te"
            :readonly="true"
            :initial-collapsed="true"
          />
        </div>
      </section>

      <!-- Instructions -->
      <section v-if="trainingInstructions.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Instructions</h3>
        <ul class="space-y-1.5">
          <li
            v-for="inst in trainingInstructions"
            :key="inst.instructionId"
            class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white border border-gray-100 shadow-sm hover:shadow cursor-pointer transition-all"
            @click="inst.messageId && emit('scrollToMessage', inst.messageId)"
          >
            <Icon
              :name="inst.isEdit ? 'heroicons-pencil' : 'heroicons-plus-circle'"
              class="w-4 h-4 flex-shrink-0"
              :class="inst.isEdit ? 'text-blue-400' : 'text-green-400'"
            />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 truncate">{{ inst.title }}</div>
              <div class="flex items-center gap-2 mt-0.5">
                <span v-if="inst.category" class="text-[11px] text-gray-400">{{ inst.category }}</span>
                <span v-if="inst.lineCount > 0" class="text-[11px] text-green-600">+{{ inst.lineCount }} lines</span>
              </div>
            </div>
          </li>
        </ul>
      </section>

    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'

const props = defineProps<{
  scheduledPrompts: any[]
  artifactList: any[]
  queryList: any[]
  queryExecutions: any[]
  trainingInstructions: any[]
}>()

const emit = defineEmits([
  'editScheduledPrompt',
  'openArtifact',
  'scrollToMessage',
])

const hasAnything = computed(() =>
  props.scheduledPrompts.length > 0 ||
  props.artifactList.length > 0 ||
  props.queryExecutions.length > 0 ||
  props.trainingInstructions.length > 0
)

function getCronLabel(cron?: string): string {
  if (!cron) return ''
  const parts = cron.split(' ')
  if (parts.length < 5) return cron
  const [min, hour, dom, mon, dow] = parts

  // Handle step values like */2, */5
  const isStep = (v: string) => v.startsWith('*/')
  const stepVal = (v: string) => parseInt(v.slice(2))

  // Every N minutes
  if (isStep(min) && hour === '*') {
    return `Every ${stepVal(min)} minutes`
  }

  // Every N hours
  if (min !== '*' && isStep(hour)) {
    return `Every ${stepVal(hour)} hours`
  }

  // Specific time
  if (min !== '*' && hour !== '*' && !isStep(hour)) {
    const h = parseInt(hour)
    const ampm = h >= 12 ? 'PM' : 'AM'
    const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h
    const time = `${h12}:${min.padStart(2, '0')} ${ampm}`

    if (dow === '*' && dom === '*') return `Daily at ${time}`

    if (dow !== '*') {
      const dayNames: Record<string, string> = { '0': 'Sun', '1': 'Mon', '2': 'Tue', '3': 'Wed', '4': 'Thu', '5': 'Fri', '6': 'Sat' }
      const days = dow.split(',').map((d: string) => dayNames[d] || d).join(', ')
      return `${days} at ${time}`
    }

    return `Monthly on day ${dom} at ${time}`
  }

  return cron
}
</script>
