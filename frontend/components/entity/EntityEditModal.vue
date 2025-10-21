<template>
  <UModal v-model="localOpen" :ui="{ width: 'sm:max-w-6xl', height: 'sm:h-[90vh]' }">
    <div class="h-full flex flex-col">
      <div class="px-4 py-3 bg-white border-b flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800">{{ detail?.title || detail?.slug }}</div>
        <button class="text-xs text-gray-500 hover:text-gray-700" @click="close">Close</button>
      </div>
      <div class="flex-1 flex overflow-hidden min-h-0">
        <aside class="w-32 bg-white border-r">
          <nav class="p-2">
            <button class="w-full text-left px-2 py-1.5 text-xs rounded mb-1 transition-colors" :class="editTab==='details' ? 'bg-blue-50 text-blue-700' : 'text-gray-600 hover:bg-gray-50'" @click="editTab='details'">Details</button>
            <button class="w-full text-left px-2 py-1.5 text-xs rounded transition-colors" :class="editTab==='code' ? 'bg-blue-50 text-blue-700' : 'text-gray-600 hover:bg-gray-50'" @click="editTab='code'">Code</button>
          </nav>
        </aside>
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div v-if="editTab==='details'" class="flex-1 p-4 overflow-auto">
            <div class="bg-white rounded-lg p-4">
              <EntityForm v-model="form" />
            </div>
            <div class="mt-3 flex items-center justify-end gap-2">
              <button class="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50" @click="close">Cancel</button>
              <button class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded-lg disabled:opacity-50" :disabled="saving" @click="saveEdit">
                <span v-if="saving">Saving...</span>
                <span v-else>Save</span>
              </button>
            </div>
          </div>
          <div v-else class="h-full flex flex-col">
            <div class="h-1/2 p-3 flex flex-col border-b bg-white">
              <ClientOnly>
                <div class="flex-1 min-h-0 rounded overflow-hidden border border-gray-200">
                  <MonacoEditor
                    v-model="form.code"
                    :lang="editorLang"
                    :options="{ theme: 'vs-dark', automaticLayout: false, minimap: { enabled: false }, wordWrap: 'on', fontSize: 13 }"
                    style="height: 100%"
                  />
                </div>
              </ClientOnly>
              <div v-if="codeErrorMsg" class="mt-2 text-xs text-red-600 px-1">{{ codeErrorMsg }}</div>
              <div class="mt-2 flex items-center justify-end gap-2">
                <button class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded-lg disabled:opacity-50" :disabled="running" @click="runAndSave">
                  <span v-if="running && runMode === 'save'">Saving...</span>
                  <span v-else>Save</span>
                </button>
                <button class="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50 flex items-center gap-1.5" :disabled="running" @click="previewRun">
                  <Icon v-if="running && runMode === 'preview'" name="heroicons-arrow-path" class="w-3 h-3 animate-spin" />
                  <Icon v-else name="heroicons-play" class="w-3 h-3" />
                  <span v-if="running && runMode === 'preview'">Running...</span>
                  <span v-else>Run</span>
                </button>
              </div>
            </div>
            <div class="h-1/2 p-3 flex flex-col min-h-0">
              <div class="flex-1 overflow-auto min-h-0 bg-white rounded-lg border border-gray-200">
                <div v-if="codePreview?.info" class="px-3 py-2 text-xs text-gray-600 border-b bg-gray-50 flex items-center justify-between">
                  <span>Results</span>
                  <span>{{ codePreview.info.total_rows?.toLocaleString?.() || codePreview.info.total_rows }} rows</span>
                </div>
                <div class="overflow-auto" style="max-height: calc(100% - 36px);">
                  <div v-if="codePreview && codePreview.columns && codePreview.rows">
                    <table class="min-w-full text-xs">
                      <thead class="bg-gray-50 sticky top-0 border-b">
                        <tr>
                          <th v-for="col in codePreview.columns" :key="col.field" class="px-3 py-2 text-left text-xs font-medium text-gray-700">
                            {{ col.headerName || col.field }}
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="(row, rIdx) in codePreview.rows" :key="rIdx" class="border-b hover:bg-gray-50">
                          <td v-for="col in codePreview.columns" :key="col.field" class="px-3 py-2 text-gray-800">
                            {{ row[col.field] }}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <div v-else class="flex items-center justify-center h-full text-xs" :class="codeErrorMsg ? 'text-red-600' : 'text-gray-400'">
                    {{ codeErrorMsg || 'Click Run to preview results' }}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import EntityForm from '~/components/entity/EntityForm.vue'
import { useMyFetch } from '~/composables/useMyFetch'

type MinimalDS = { id: string; name?: string; type?: string }
type EntityDetail = {
  id: string
  type: string
  title: string
  slug: string
  description?: string | null
  data?: any
  data_model?: any
  view?: any
  last_refreshed_at?: string | null
  updated_at?: string | null
  tags?: string[]
  status?: string
  data_sources?: MinimalDS[]
  code?: string
  private_status?: string | null
  global_status?: string | null
  owner_id?: string
  reviewed_by?: any
}

const props = defineProps<{
  modelValue: boolean
  detail: EntityDetail | null
  entityId: string
  editorLang?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved'): void
}>()

const localOpen = ref<boolean>(props.modelValue)
watch(() => props.modelValue, v => { localOpen.value = v })
watch(localOpen, v => emit('update:modelValue', v))

const editorLang = ref<string>(props.editorLang || 'python')

const form = ref<{
  type: string
  title: string
  description: string | null
  code: string
  status: string
  data_source_ids?: string[]
  global_status?: string | null
}>({
  type: 'model',
  title: '',
  description: null,
  code: '',
  status: 'draft',
  data_source_ids: [],
  global_status: null
})

const editTab = ref<'details' | 'code'>('details')
const saving = ref(false)
const running = ref(false)
const runMode = ref<'preview' | 'save' | null>(null)
const codePreview = ref<any | null>(null)
const codeErrorMsg = ref('')

watch(() => props.modelValue, (v) => {
  if (v && props.detail) {
    form.value = {
      type: props.detail.type,
      title: props.detail.title,
      description: (props.detail.description || null) as any,
      code: props.detail.code || '',
      status: props.detail.status || 'draft',
      data_source_ids: props.detail.data_sources?.map(ds => ds.id) || [],
      global_status: props.detail.global_status || null
    }
    editTab.value = 'details'
    codePreview.value = props.detail.data || null
    codeErrorMsg.value = ''
  }
})

function close() {
  localOpen.value = false
}

async function previewRun() {
  running.value = true
  runMode.value = 'preview'
  codeErrorMsg.value = ''
  try {
    const { data, error } = await useMyFetch(`/api/entities/${props.entityId}/preview`, {
      method: 'POST',
      body: { code: form.value.code }
    })
    if (error.value) throw error.value
    const payload: any = data.value
    if (payload?.error) {
      codeErrorMsg.value = payload.error
      codePreview.value = null
      return
    }
    codePreview.value = payload?.data || null
  } catch (e: any) {
    codeErrorMsg.value = e?.data?.detail || e?.message || 'Failed to run preview'
  } finally {
    running.value = false
    runMode.value = null
  }
}

async function runAndSave() {
  running.value = true
  runMode.value = 'save'
  codeErrorMsg.value = ''
  try {
    const payload: any = {
      type: form.value.type,
      title: form.value.title || '',
      description: form.value.description || null,
      code: form.value.code || '',
      status: form.value.status || 'draft',
    }
    const { data, error } = await useMyFetch(`/api/entities/${props.entityId}/run`, { method: 'POST', body: payload })
    if (error.value) throw error.value
    const result: any = data.value
    if (result?.error) {
      codeErrorMsg.value = result.error
      codePreview.value = null
      return
    }
    codePreview.value = result?.data || null
    emit('saved')
    close()
  } catch (e: any) {
    codeErrorMsg.value = e?.data?.detail || e?.message || 'Failed to save and run'
  } finally {
    running.value = false
    runMode.value = null
  }
}

async function saveEdit() {
  saving.value = true
  try {
    const payload: any = {
      type: form.value.type,
      title: form.value.title || '',
      description: form.value.description || null,
      status: form.value.status || 'draft',
      data_source_ids: form.value.data_source_ids || []
    }
    const { error } = await useMyFetch(`/api/entities/${props.entityId}`, { method: 'PUT', body: payload })
    if (error.value) throw error.value
    emit('saved')
    close()
  } catch (e: any) {
    // Surface error in codeErrorMsg area for simplicity
    codeErrorMsg.value = e?.data?.detail || e?.message || 'Failed to save'
  } finally {
    saving.value = false
  }
}
</script>


