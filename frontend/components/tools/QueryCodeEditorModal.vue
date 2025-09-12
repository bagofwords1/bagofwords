<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-6xl', height: 'sm:h-[90vh]' }">
    <div class="h-full flex flex-col">
      <div class="px-4 py-3 border-b flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800">Edit code — {{ title }}</div>
        <div v-if="currentStepId" class="ml-4 text-[11px] text-gray-500">step_id: {{ currentStepId }}</div>
        <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50" @click="open = false">Close</button>
      </div>
      <div class="flex-1 flex overflow-hidden min-h-0">
        <!-- Left tabs -->
        <aside class="w-40 border-r">
          <nav class="p-2 text-sm">
            <button
              class="w-full text-left px-2 py-1.5 rounded mb-1 transition-colors"
              :class="activeTab === 'code' ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'"
              @click="activeTab = 'code'"
            >Code</button>
            <button
              class="w-full text-left px-2 py-1.5 rounded transition-colors"
              :class="activeTab === 'visuals' ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'"
              @click="activeTab = 'visuals'"
            >Visuals</button>
          </nav>
        </aside>

        <!-- Right content -->
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div v-if="activeTab === 'code'" class="h-full flex flex-col">
            <!-- Editor section - exactly half height, fixed and non-scrollable -->
            <div class="h-1/2 p-4 flex flex-col border-b">
              <textarea
                v-model="editorCode"
                class="block w-full flex-1 resize-none font-mono text-xs border rounded p-3 text-gray-800 focus:outline-none focus:ring-1 focus:ring-gray-300 min-h-0"
                spellcheck="false"
              />
              <div v-if="errorMsg" class="mt-2 text-xs text-red-600">{{ errorMsg }}</div>
              <div class="mt-3 flex items-center justify-end space-x-2">
                <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50" :disabled="running" @click="previewRun">
                  <span v-if="running && runMode === 'preview'">Running…</span>
                  <span v-else>Run</span>
                </button>
                <button class="px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700" :disabled="running" @click="runNewStep">
                  <span v-if="running && runMode === 'save'">Saving…</span>
                  <span v-else>Save</span>
                </button>
              </div>
            </div>
            <!-- Results section - exactly half height, scrollable -->
            <div class="h-1/2 p-4 flex flex-col min-h-0">
              <div class="text-xs text-gray-600 mb-2 flex-shrink-0" v-if="preview?.info">Rows: {{ preview.info.total_rows?.toLocaleString?.() || preview.info.total_rows }}</div>
              <div class="flex-1 overflow-auto min-h-0">
                <div v-if="preview && preview.columns && preview.rows" class="border rounded">
                  <table class="min-w-full text-xs">
                    <thead class="bg-gray-50 sticky top-0">
                      <tr>
                        <th v-for="col in preview.columns" :key="col.field" class="px-2 py-1 text-left font-medium text-gray-600">{{ col.headerName || col.field }}</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="(row, rIdx) in preview.rows" :key="rIdx" class="border-t">
                        <td v-for="col in preview.columns" :key="col.field" class="px-2 py-1 text-gray-800">
                          {{ row[col.field] }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div v-else class="text-xs text-gray-400">No preview yet.</div>
              </div>
            </div>
          </div>

          <div v-else class="p-4 text-sm text-gray-600 flex-1 overflow-auto">
            Visuals tab — coming soon.
          </div>
        </section>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, computed } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'

interface Props {
  visible: boolean
  queryId?: string | null
  initialCode: string
  title: string
  stepId?: string | null
  toolExecutionId?: string | null
}

const props = defineProps<Props>()
const emit = defineEmits(['close', 'stepCreated'])

const editorCode = ref('')
const running = ref(false)
const errorMsg = ref('')
const preview = ref<any | null>(null)
const queryId = ref<string | null>(null)
const currentStepId = ref<string | null>(null)

const open = computed({
  get: () => props.visible,
  set: (v: boolean) => {
    if (!v) emit('close')
  }
})

const activeTab = ref<'code' | 'visuals'>('code')

// Keep internal queryId in sync with prop while modal is open
watch(() => props.queryId, (v) => {
  if (props.visible && v) {
    queryId.value = v
  }
})

async function syncQueryIdOnOpen() {
  queryId.value = props.queryId || null
  if (!queryId.value && props.stepId) {
    try {
      const s: any = await useMyFetch(`/api/steps/${props.stepId}`)
      const step = s?.data?.value
      if (step?.query_id) queryId.value = step.query_id
    } catch {
      // ignore
    }
  }
}

watch(() => props.visible, async (v) => {
  if (v) {
    editorCode.value = props.initialCode || ''
    errorMsg.value = ''
    preview.value = null
    currentStepId.value = props.stepId || null
    await syncQueryIdOnOpen()
    await loadInitialStepOrDefault()
  }
})

onMounted(async () => {
  if (props.visible) {
    editorCode.value = props.initialCode || ''
    currentStepId.value = props.stepId || null
    await syncQueryIdOnOpen()
    await loadInitialStepOrDefault()
  }
})

async function loadInitialStepOrDefault() {
  try {
    let loadedStep: any = null
    // If a stepId is provided, load it first
    if (props.stepId) {
      const s: any = await useMyFetch(`/api/steps/${props.stepId}`)
      loadedStep = s?.data?.value || null
    }
    // If we have a queryId, fetch the current default step
    let defaultStep: any = null
    if (queryId.value) {
      const resp: any = await useMyFetch(`/api/queries/${queryId.value}/default_step`)
      defaultStep = resp?.data?.value?.step || null
    }
    const step = defaultStep || loadedStep
    if (step) {
      currentStepId.value = step.id
      if (step.query_id && !queryId.value) queryId.value = step.query_id
      if (step.data) preview.value = step.data
      if (!editorCode.value && step.code) editorCode.value = step.code
    }
  } catch (e) {
    // swallow
  }
}

const runMode = ref<'preview' | 'save' | null>(null)

async function previewRun() {
  running.value = true
  runMode.value = 'preview'
  errorMsg.value = ''
  try {
    // Ensure we have a query id at click-time
    if (!queryId.value) {
      await syncQueryIdOnOpen()
    }
    if (!queryId.value) {
      throw new Error('Query not found.')
    }
    const resp: any = await useMyFetch(`/api/queries/${queryId.value}/preview`, {
      method: 'POST',
      body: { code: editorCode.value, title: props.title, type: 'table' }
    })
    const payload = resp?.data?.value
    if (payload?.error) {
      errorMsg.value = payload.error
      preview.value = null
      return
    }
    preview.value = payload?.preview || null
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to run'
  } finally {
    running.value = false
    runMode.value = null
  }
}

async function runNewStep() {
  running.value = true
  runMode.value = 'save'
  errorMsg.value = ''
  try {
    // Ensure we have a query id at click-time
    if (!queryId.value) {
      await syncQueryIdOnOpen()
    }
    if (!queryId.value) {
      throw new Error('Query not found.')
    }
    const resp: any = await useMyFetch(`/api/queries/${queryId.value}/run`, {
      method: 'POST',
      body: {
        code: editorCode.value,
        title: props.title,
        type: 'table',
        tool_execution_id: props.toolExecutionId || null
      }
    })
    const payload = resp?.data?.value
    // Show backend error message if execution failed
    if (payload?.error) {
      errorMsg.value = payload.error
      preview.value = null
      return
    }
    preview.value = payload?.step?.data || null
    if (payload?.step) {
      currentStepId.value = payload.step.id
      emit('stepCreated', payload.step)
    }
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to run'
  } finally {
    running.value = false
    runMode.value = null
  }
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>


