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
        <ul class="space-y-1">
          <li
            v-for="sp in scheduledPrompts"
            :key="sp.id"
            class="flex items-center gap-2.5 px-3 py-2 rounded-lg hover:bg-gray-50 cursor-pointer transition-colors"
            @click="emit('editScheduledPrompt', sp)"
          >
            <div
              class="w-2 h-2 rounded-full flex-shrink-0"
              :class="sp.is_active ? 'bg-green-400' : 'bg-gray-300'"
            />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 truncate">{{ sp.prompt?.content || 'Untitled' }}</div>
              <div class="text-[11px] text-gray-400 mt-0.5">{{ getCronLabel(sp.cron_schedule) }}</div>
            </div>
            <Icon name="heroicons-chevron-right" class="w-3.5 h-3.5 text-gray-300 flex-shrink-0" />
          </li>
        </ul>
      </section>

      <!-- Artifacts -->
      <section v-if="artifactList.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Artifacts</h3>
        <ul class="space-y-1">
          <li
            v-for="art in artifactList"
            :key="art.id"
            class="flex items-center gap-2.5 px-3 py-2 rounded-lg hover:bg-gray-50 cursor-pointer transition-colors"
            @click="emit('openArtifact', art)"
          >
            <Icon
              :name="art.isEdit ? 'heroicons-pencil-square' : 'heroicons-sparkles'"
              class="w-4 h-4 flex-shrink-0"
              :class="art.isEdit ? 'text-blue-400' : 'text-purple-400'"
            />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 truncate">{{ art.title }}</div>
            </div>
            <Icon name="heroicons-chevron-right" class="w-3.5 h-3.5 text-gray-300 flex-shrink-0" />
          </li>
        </ul>
      </section>

      <!-- Queries -->
      <section v-if="queryList.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Queries</h3>
        <ul class="space-y-1">
          <li
            v-for="(q, i) in queryList"
            :key="i"
            class="flex items-center gap-2.5 px-3 py-2 rounded-lg hover:bg-gray-50 cursor-pointer transition-colors"
            @click="q.messageId && emit('scrollToMessage', q.messageId, q.stepId)"
          >
            <Icon name="heroicons-circle-stack" class="w-4 h-4 flex-shrink-0 text-gray-400" />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 truncate">{{ q.label }}</div>
              <div v-if="q.rowCount != null" class="text-[11px] text-gray-400 mt-0.5">{{ q.rowCount.toLocaleString() }} rows</div>
            </div>
            <Icon name="heroicons-chevron-right" class="w-3.5 h-3.5 text-gray-300 flex-shrink-0" />
          </li>
        </ul>
      </section>

      <!-- Instructions -->
      <section v-if="trainingInstructions.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Instructions</h3>
        <ul class="space-y-1">
          <li
            v-for="inst in trainingInstructions"
            :key="inst.instructionId"
            class="flex items-center gap-2.5 px-3 py-2 rounded-lg hover:bg-gray-50 cursor-pointer transition-colors"
            @click="emit('editTrainingInstruction', inst)"
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
            <Icon name="heroicons-chevron-right" class="w-3.5 h-3.5 text-gray-300 flex-shrink-0" />
          </li>
        </ul>
      </section>

    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

interface ArtifactItem {
  id: string
  title: string
  isEdit: boolean
  artifactId?: string
}

interface QueryItem {
  id: string
  label: string
  rowCount?: number
  messageId: string
  stepId: string
}

const props = defineProps<{
  scheduledPrompts: any[]
  artifactList: ArtifactItem[]
  queryList: QueryItem[]
  trainingInstructions: any[]
}>()

const emit = defineEmits([
  'editScheduledPrompt',
  'openArtifact',
  'scrollToMessage',
  'editTrainingInstruction',
])

const hasAnything = computed(() =>
  props.scheduledPrompts.length > 0 ||
  props.artifactList.length > 0 ||
  props.queryList.length > 0 ||
  props.trainingInstructions.length > 0
)

function getCronLabel(cron?: string): string {
  if (!cron) return ''
  const parts = cron.split(' ')
  if (parts.length < 5) return cron
  const [min, hour, , , dow] = parts
  const dayNames: Record<string, string> = { '0': 'Sun', '1': 'Mon', '2': 'Tue', '3': 'Wed', '4': 'Thu', '5': 'Fri', '6': 'Sat' }
  const h = parseInt(hour)
  const ampm = h >= 12 ? 'PM' : 'AM'
  const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h
  const time = `${h12}:${min.padStart(2, '0')} ${ampm}`
  if (dow === '*') return `Daily at ${time}`
  const days = dow.split(',').map((d: string) => dayNames[d] || d).join(', ')
  return `${days} at ${time}`
}
</script>
