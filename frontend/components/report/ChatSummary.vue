<template>
  <div class="h-full overflow-y-auto bg-white dark:bg-gray-900">
    <!-- Header -->
    <div class="px-4 pt-4 pb-6 bg-gradient-to-b from-amber-50 dark:from-amber-900/15 to-white dark:to-gray-900 flex items-center gap-2">
      <button v-if="showClose" @click="$emit('close')" class="hover:bg-gray-100 dark:hover:bg-gray-700 p-1 rounded">
        <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
      </button>
      <h2 class="text-sm font-semibold text-gray-900 dark:text-white">Summary</h2>
    </div>

    <div class="max-w-xl mx-auto px-4 py-4 space-y-6">

      <!-- Empty state -->
      <div v-if="!hasAnything" class="flex flex-col items-center justify-center h-64 text-gray-400">
        <Icon name="heroicons-document-text" class="w-8 h-8 mb-2" />
        <span class="text-sm">No items yet</span>
      </div>

      <!-- Scheduled Tasks -->
      <section v-if="scheduledPrompts.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Scheduled Tasks</h3>
        <ul class="space-y-1.5">
          <li v-for="sp in scheduledPrompts" :key="sp.id">
            <ScheduledTaskCard :scheduled-prompt="sp" @click="emit('editScheduledPrompt', sp)" />
          </li>
        </ul>
      </section>

      <!-- Artifacts -->
      <section v-if="artifactList.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Artifacts</h3>
        <ul class="space-y-1.5">
          <li
            v-for="art in visibleArtifacts"
            :key="art.id"
            class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow cursor-pointer transition-all"
            @click="emit('openArtifact', { artifactId: art.id })"
          >
            <Icon
              :name="art.mode === 'doc' ? 'heroicons:document-text' : 'heroicons:squares-plus'"
              class="w-4 h-4 flex-shrink-0"
              :class="art.mode === 'doc' ? 'text-emerald-500' : 'text-blue-500'"
            />
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-1.5">
                <span class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ art.title || 'Untitled' }}</span>
                <span v-if="art.id === artifactList[0]?.id" class="inline-flex items-center text-[10px] font-medium text-blue-600 bg-blue-50 dark:bg-blue-950 px-1.5 py-0.5 rounded">Default</span>
              </div>
              <div v-if="art.mode" class="text-[11px] text-gray-400 mt-0.5">{{ art.mode === 'doc' ? 'document' : art.mode }}</div>
            </div>
          </li>
        </ul>
        <button
          v-if="artifactList.length > 3 && !showAllArtifacts"
          class="mt-2 text-xs text-gray-400 hover:text-gray-600 transition-colors"
          @click="showAllArtifacts = true"
        >
          Show {{ artifactList.length - 3 }} more
        </button>
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

      <!-- Instructions (historical: created in this report, regardless of accept state) -->
      <section v-if="instructionsList.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Instructions</h3>
        <ul class="space-y-1.5">
          <li
            v-for="inst in instructionsList"
            :key="inst.id"
            class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow transition-all"
          >
            <Icon
              name="heroicons-cube"
              class="w-4 h-4 flex-shrink-0 text-blue-400"
            />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ inst.title || 'Untitled instruction' }}</div>
              <div class="flex items-center gap-2 mt-0.5">
                <span v-if="inst.category" class="text-[11px] text-gray-400">{{ inst.category }}</span>
                <span
                  v-if="inst.state === 'pending'"
                  class="inline-flex items-center gap-1 text-[11px] text-amber-600"
                >
                  <span class="w-1.5 h-1.5 rounded-full bg-amber-400" />
                  Pending
                </span>
                <span
                  v-else
                  class="inline-flex items-center gap-1 text-[11px] text-green-600"
                >
                  <Icon name="heroicons:check-circle" class="w-3 h-3" />
                  Accepted
                </span>
              </div>
            </div>
          </li>
        </ul>
      </section>

      <!-- Webhooks -->
      <section v-if="webhooks.length > 0">
        <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Webhooks</h3>
        <ul class="space-y-1.5">
          <li
            v-for="w in webhooks"
            :key="w.id"
            class="flex items-center gap-2.5 px-3 py-2.5 rounded-lg bg-white dark:bg-gray-900 border border-gray-100 dark:border-gray-800 shadow-sm hover:shadow cursor-pointer transition-all"
            @click="showWebhookModal = true"
          >
            <Icon :name="webhookIcon(w.source)" class="w-4 h-4 flex-shrink-0 text-gray-400" />
            <div class="flex-1 min-w-0">
              <div class="text-sm text-gray-700 dark:text-gray-300 truncate">{{ w.name }}</div>
              <div class="flex items-center gap-2 mt-0.5">
                <span class="text-[11px] text-gray-400">{{ w.source }} · {{ w.auth_mode }}</span>
                <span v-if="w.classify_enabled" class="inline-flex items-center gap-1 text-[11px] text-blue-500">
                  <Icon name="heroicons-sparkles" class="w-3 h-3" /> AI
                </span>
              </div>
            </div>
          </li>
        </ul>
      </section>

    </div>

    <!-- Configure webhook (pinned at the bottom) -->
    <div v-if="reportId" class="max-w-xl mx-auto px-4 pb-6">
      <button
        @click="showWebhookModal = true"
        class="w-full flex items-center justify-center gap-1.5 px-3 py-2 text-xs text-gray-400 hover:text-gray-600 rounded-lg border border-dashed border-gray-200 dark:border-gray-700 hover:border-gray-300 transition-colors"
      >
        <Icon name="heroicons-plus" class="w-3.5 h-3.5" />
        Configure webhook
      </button>
    </div>

    <WebhookConfigModal
      v-if="reportId"
      v-model="showWebhookModal"
      :report-id="reportId"
      @changed="loadWebhooks"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue'
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'
import WebhookConfigModal from '~/components/report/WebhookConfigModal.vue'

const props = defineProps<{
  scheduledPrompts: any[]
  artifactList: any[]
  queryList: any[]
  queryExecutions: any[]
  // Pending-only: drives session-pill state. Kept for backwards compat with
  // existing callers but no longer rendered directly in the Instructions
  // section — we use reportInstructions for that.
  trainingInstructions: any[]
  // Historical list of instructions created in this report (all states).
  reportInstructions?: any[]
  // The current pending build id, used to mark items as Pending vs Accepted.
  pendingBuildId?: string | null
  showClose?: boolean
  // Report id — enables the Webhooks section + Configure webhook modal.
  reportId?: string
}>()

// ---- Webhooks ----
const showWebhookModal = ref(false)
const webhooks = ref<any[]>([])

function webhookIcon(source: string): string {
  switch ((source || '').toLowerCase()) {
    case 'github': return 'heroicons-code-bracket-square'
    case 'jira': return 'heroicons-bug-ant'
    default: return 'heroicons-bolt'
  }
}

async function loadWebhooks() {
  if (!props.reportId) return
  try {
    const { data } = await useMyFetch(`/reports/${props.reportId}/webhooks`)
    webhooks.value = (data.value as any[]) || []
  } catch { webhooks.value = [] }
}

onMounted(loadWebhooks)
watch(() => props.reportId, loadWebhooks)

const showAllArtifacts = ref(false)
const visibleArtifacts = computed(() =>
  showAllArtifacts.value ? props.artifactList : props.artifactList.slice(0, 3)
)

const emit = defineEmits([
  'editScheduledPrompt',
  'openArtifact',
  'scrollToMessage',
  'close',
])

// Render-ready instruction list: prefer the historical list (so accepted
// instructions don't disappear after approval). Pending state is derived
// from membership in the current pending training build, looked up via the
// `trainingInstructions` (pending-only) list passed alongside.
const pendingIdSet = computed(() => {
  const set = new Set<string>()
  for (const t of (props.trainingInstructions || [])) {
    if (t?.instructionId) set.add(String(t.instructionId))
  }
  return set
})
const instructionsList = computed(() => {
  const raw = Array.isArray(props.reportInstructions) ? props.reportInstructions : []
  const seen = new Set<string>()
  const out: { id: string; title: string; category: string; state: string }[] = []
  for (const i of raw) {
    if (!i || !i.id) continue
    const id = String(i.id)
    if (seen.has(id)) continue
    seen.add(id)
    out.push({
      id,
      title: i.title || '',
      category: i.category || '',
      state: pendingIdSet.value.has(id) ? 'pending' : 'accepted',
    })
  }
  return out
})

const hasAnything = computed(() =>
  props.scheduledPrompts.length > 0 ||
  props.artifactList.length > 0 ||
  props.queryExecutions.length > 0 ||
  instructionsList.value.length > 0
)

</script>
