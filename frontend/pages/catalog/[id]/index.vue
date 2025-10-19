<template>
  <div class="py-6">
    <div class="max-w-3xl mx-auto px-4">
      <div class="mb-5">
        <div class="flex items-start gap-3">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-2 mb-1">
              <span
                class="text-[10px] px-1.5 py-0.5 rounded border"
                :class="detail?.type === 'metric' ? 'text-emerald-700 border-emerald-200 bg-emerald-50' : 'text-blue-700 border-blue-200 bg-blue-50'"
              >{{ (detail?.type || '').toUpperCase() }}</span>
            </div>
            <h1 class="text-lg font-semibold text-gray-900">{{ detail?.title || detail?.slug }}</h1>
            <div class="text-[12px] text-gray-600 mt-1">{{ detail?.description || '—' }}</div>
            <!-- Data source icons under description -->
            <div v-if="detail?.data_sources?.length" class="mt-2 flex items-center gap-1.5">
              <img
                v-for="ds in (detail?.data_sources || [])"
                :key="ds.id"
                :src="dataSourceIcon(ds.type)"
                :alt="ds.type"
                class="w-5 h-5 rounded border border-gray-100 bg-white object-contain p-0.5"
                @error="(e: any) => e.target && (e.target.style.visibility = 'hidden')"
              />
            </div>
          </div>
        </div>

        <div class="mt-3 flex items-center gap-2">
          <div v-if="viewType" class="text-[11px] text-gray-500 px-1.5 py-0.5 bg-gray-50 border border-gray-100 rounded">{{ viewType }}</div>
          <div v-if="detail?.last_refreshed_at" class="text-[11px] text-gray-400">Refreshed {{ timeAgo(detail?.last_refreshed_at as any) }}</div>
          <button class="ml-auto text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-gray-50" @click="refreshEntity" :disabled="refreshing">
            <span v-if="refreshing">Refreshing…</span>
            <span v-else>Refresh</span>
          </button>
        </div>

        <div class="mt-4">
          <div class="border border-gray-100 rounded bg-white">
            <div class="px-3 py-2 text-xs text-gray-500 border-b bg-gray-50 flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span>Preview</span>
                <span v-if="rowCount" class="text-[11px] text-gray-400">{{ rowCount }} rows</span>
              </div>
              <div class="flex items-center gap-2">
                <button 
                  v-if="hasData"
                  @click="activeTab = activeTab === 'chart' ? 'table' : 'chart'"
                  class="text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-gray-50"
                >
                  {{ activeTab === 'chart' ? 'Show Table' : 'Show Chart' }}
                </button>
              </div>
            </div>
            <div class="p-3">
              <!-- Chart/Visual Content -->
              <Transition name="fade" mode="out-in">
                <div v-if="activeTab === 'chart' && showVisual" class="bg-gray-50 rounded-sm p-2">
                  <div v-if="resolvedCompEl" :class="chartHeightClass">
                    <component
                      :is="resolvedCompEl"
                      :widget="effectiveWidget"
                      :data="detail?.data"
                      :data_model="detail?.data_model || { type: detail?.view?.type }"
                      :step="effectiveStep"
                      :view="detail?.view"
                    />
                  </div>
                  <div v-else-if="chartVisualTypes.has(detail?.view?.type)" class="h-[340px]">
                    <RenderVisual :widget="effectiveWidget" :data="detail?.data" :data_model="detail?.data_model || { type: detail?.view?.type }" />
                  </div>
                </div>
              </Transition>

              <!-- Table Content -->
              <Transition name="fade" mode="out-in">
                <div v-if="activeTab === 'table' || !showVisual" class="h-[400px]">
                  <RenderTable :widget="effectiveWidget" :step="effectiveStep" />
                </div>
              </Transition>
            </div>
          </div>
        </div>

        <div v-if="canCreateEntities" class="mt-4 flex justify-end">
          <button class="text-xs px-3 py-1.5 rounded border border-gray-200 hover:bg-gray-50" @click="openEdit = true">Edit</button>
        </div>
      </div>
    </div>

    <UModal v-model="openEdit" :ui="{ width: 'sm:max-w-4xl', height: 'sm:h-[90vh]' }">
      <div class="h-full flex flex-col">
        <div class="px-4 py-3 border-b flex items-center justify-between flex-shrink-0">
          <div class="text-sm font-medium text-gray-800">Edit Entity</div>
          <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50" @click="openEdit = false">Close</button>
        </div>
        <div class="flex-1 flex overflow-hidden min-h-0">
          <aside class="w-44 border-r">
            <nav class="p-2 text-sm">
              <button class="w-full text-left px-2 py-1.5 rounded mb-1 transition-colors" :class="tab==='details' ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'" @click="tab='details'">Details</button>
              <button class="w-full text-left px-2 py-1.5 rounded transition-colors" :class="tab==='code' ? 'bg-gray-100 text-gray-900' : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'" @click="tab='code'">Code</button>
            </nav>
          </aside>
          <section class="flex-1 flex flex-col overflow-hidden min-h-0">
            <div v-if="tab==='details'" class="flex-1 p-4 overflow-auto">
              <div class="grid grid-cols-2 gap-4">
                <div>
                  <label class="block text-xs text-gray-600 mb-1">Type</label>
                  <select v-model="form.type" class="w-full text-sm border rounded px-2 py-1.5">
                    <option value="model">model</option>
                    <option value="metric">metric</option>
                  </select>
                </div>
                <div>
                  <label class="block text-xs text-gray-600 mb-1">Status</label>
                  <select v-model="form.status" class="w-full text-sm border rounded px-2 py-1.5">
                    <option value="draft">draft</option>
                    <option value="published">published</option>
                  </select>
                </div>
                <div class="col-span-2">
                  <label class="block text-xs text-gray-600 mb-1">Title</label>
                  <input v-model="form.title" type="text" class="w-full text-sm border rounded px-2 py-1.5" />
                </div>
                <div class="col-span-2">
                  <label class="block text-xs text-gray-600 mb-1">Slug</label>
                  <input v-model="form.slug" type="text" class="w-full text-sm border rounded px-2 py-1.5" />
                </div>
                <div class="col-span-2">
                  <label class="block text-xs text-gray-600 mb-1">Description</label>
                  <textarea v-model="form.description" rows="2" class="w-full text-sm border rounded px-2 py-1.5" />
                </div>
                <div class="col-span-2">
                  <label class="block text-xs text-gray-600 mb-1">Tags (comma-separated)</label>
                  <input v-model="tagsInput" type="text" class="w-full text-sm border rounded px-2 py-1.5" />
                  <div class="mt-1 flex flex-wrap gap-1">
                    <span v-for="t in form.tags" :key="t" class="text-[11px] bg-gray-100 text-gray-700 px-2 py-0.5 rounded">{{ t }}</span>
                  </div>
                </div>
                <div class="col-span-2">
                  <label class="block text-xs text-gray-600 mb-1">View Type</label>
                  <select v-model="viewTypeEdit" class="w-full text-sm border rounded px-2 py-1.5">
                    <option value="">(none)</option>
                    <option v-for="opt in viewOptions" :key="opt" :value="opt">{{ opt }}</option>
                  </select>
                </div>
              </div>
            </div>
            <div v-else class="flex-1 p-4 flex flex-col min-h-0">
              <div class="flex-1 min-h-0">
                <ClientOnly>
                  <div class="h-full">
                    <MonacoEditor v-model="form.code" :lang="editorLang" :options="{ theme: 'vs-dark', minimap: { enabled: false }, wordWrap: 'on' }" style="height: 100%" />
                  </div>
                </ClientOnly>
              </div>
              <div v-if="errorMsg" class="mt-2 text-xs text-red-600">{{ errorMsg }}</div>
            </div>
            <div class="px-4 py-3 border-t flex items-center justify-end gap-2 flex-shrink-0">
              <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50" @click="openEdit = false">Cancel</button>
              <button class="px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700 disabled:opacity-60" :disabled="saving" @click="saveEdit">
                <span v-if="saving">Saving…</span>
                <span v-else>Save</span>
              </button>
            </div>
          </section>
        </div>
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, defineAsyncComponent } from 'vue'
import { useRoute } from 'vue-router'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCan } from '~/composables/usePermissions'
import RenderVisual from '~/components/RenderVisual.vue'
import RenderTable from '~/components/RenderTable.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'

type MinimalDS = { id: string; name?: string; type?: string }
type EntityDetail = { id: string; type: string; title: string; slug: string; description?: string | null; data?: any; data_model?: any; view?: any; last_refreshed_at?: string | null; updated_at?: string | null; tags?: string[]; status?: string; data_sources?: MinimalDS[]; code?: string }

const route = useRoute()
const id = computed(() => String(route.params.id || ''))
const detail = ref<EntityDetail | null>(null)
const loading = ref(true)
const canCreateEntities = computed(() => useCan('create_entities'))

const viewType = computed(() => String(detail.value?.view?.type || ''))
const shape = computed(() => {
  const d = detail.value?.data
  if (!d) return null
  const rows = Array.isArray(d?.rows) ? d.rows.length : (typeof d?.info?.total_rows === 'number' ? d.info.total_rows : null)
  const cols = Array.isArray(d?.columns) ? d.columns.length : (Array.isArray(d?.rows) && d.rows[0] ? Object.keys(d.rows[0]).length : null)
  return { rows, cols }
})

// Tab state and visualization logic
const activeTab = ref<'chart' | 'table'>('chart')

const chartVisualTypes = new Set<string>([
  'pie_chart',
  'line_chart',
  'bar_chart',
  'area_chart',
  'heatmap',
  'scatter_plot',
  'map',
  'candlestick',
  'treemap',
  'radar_chart'
])

const showVisual = computed(() => {
  const vType = detail.value?.view?.type
  const t = vType || detail.value?.data_model?.type
  if (!t) return false
  const entry = resolveEntryByType(String(t).toLowerCase())
  if (entry) {
    return entry.componentKey !== 'table.aggrid'
  }
  return chartVisualTypes.has(String(t)) || String(t) === 'count'
})

const hasData = computed(() => {
  const rows = detail.value?.data?.rows
  if (Array.isArray(rows)) return rows.length >= 0
  return !!detail.value
})

const rowCount = computed(() => {
  const rows = detail.value?.data?.rows
  if (Array.isArray(rows)) {
    return `${rows.length.toLocaleString()}`
  }
  return null
})

// Dashboard registry-driven dynamic component
const compCache = new Map<string, any>()
function getCompForType(type?: string | null) {
  const t = (type || '').toLowerCase()
  if (!t) return null
  if (compCache.has(t)) return compCache.get(t)
  const entry = resolveEntryByType(t)
  if (!entry) return null
  const comp = defineAsyncComponent(entry.load)
  compCache.set(t, comp)
  return comp
}

const resolvedCompEl = computed(() => {
  const vType = detail.value?.view?.type
  const dmType = detail.value?.data_model?.type
  return getCompForType(String(vType || dmType || ''))
})

const chartHeightClass = computed(() => {
  const t = String((detail.value?.view?.type || detail.value?.data_model?.type || '')).toLowerCase()
  return t === 'count' ? 'h-[120px] flex items-start' : 'h-[340px]'
})

const effectiveWidget = computed(() => {
  return { id: detail.value?.id || 'preview', title: detail.value?.title || detail.value?.slug } as any
})

const effectiveStep = computed(() => {
  return {
    id: detail.value?.id,
    data: detail.value?.data,
    data_model: detail.value?.data_model || { type: detail.value?.view?.type },
    code: detail.value?.code,
    status: 'success'
  } as any
})

const openEdit = ref(false)
const editorLang = ref('python')
const form = ref<{ type: string; title: string; slug: string; description: string | null; tags: string[]; code: string; view: Record<string, any> | null; status: string }>({ type: 'model', title: '', slug: '', description: null, tags: [], code: '', view: null, status: 'draft' })
const tagsInput = ref('')
const viewTypeEdit = ref<string>('')
const viewOptions = ['table','count','bar_chart','line_chart','area_chart','heatmap','scatter_plot','map','candlestick','treemap','radar_chart']
const tab = ref<'details'|'code'>('details')
const errorMsg = ref('')
const saving = ref(false)

// Watch for data changes to update active tab
watch([showVisual, hasData], () => {
  if (showVisual.value) {
    activeTab.value = 'chart'
  } else if (hasData.value) {
    activeTab.value = 'table'
  }
}, { immediate: true })

onMounted(load)

async function load() {
  loading.value = true
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id.value}`, { method: 'GET' })
    if (error.value) throw error.value
    detail.value = data.value as any
  } catch {
  } finally {
    loading.value = false
  }
}

const refreshing = ref(false)
async function refreshEntity() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id.value}/run`, { method: 'POST' })
    if (error.value) throw error.value
    detail.value = data.value as any
  } catch {}
  refreshing.value = false
}

watch(openEdit, (v) => {
  if (v && detail.value) {
    form.value = {
      type: detail.value.type,
      title: detail.value.title,
      slug: detail.value.slug,
      description: (detail.value.description || null) as any,
      tags: Array.isArray(detail.value.tags) ? [...detail.value.tags] : [],
      code: detail.value.code || '',
      view: detail.value.view ? JSON.parse(JSON.stringify(detail.value.view)) : null,
      status: detail.value.status || 'draft',
    }
    viewTypeEdit.value = String(detail.value.view?.type || '')
    tagsInput.value = ''
    tab.value = 'details'
  }
})

watch(tagsInput, (val) => {
  if (!val) return
  if (val.endsWith(',') || val.endsWith(' ')) {
    const parts = val.split(/[\s,]+/).map(p => p.trim()).filter(Boolean)
    const last = parts[parts.length - 1]
    if (last && !form.value.tags.includes(last)) form.value.tags.push(last)
    tagsInput.value = ''
  }
})

watch(viewTypeEdit, (t) => {
  if (!t) {
    form.value.view = null
  } else {
    const v = (form.value.view || {})
    form.value.view = { ...v, type: t }
  }
})

function parseAsUTCIfNaive(s: string): Date {
  // if string has no timezone, treat as UTC
  const hasTZ = /Z|[+-]\d{2}:?\d{2}$/.test(s)
  return new Date(hasTZ ? s : `${s}Z`)
}

function timeAgo(iso: string | Date | null | undefined) {
  if (!iso) return '—'
  const d = typeof iso === 'string' ? parseAsUTCIfNaive(iso) : iso
  const diff = Math.max(0, Date.now() - (d?.getTime?.() || 0))
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}

function dataSourceIcon(type?: string) {
  if (!type) return '/public/icons/database.png'
  const key = String(type).toLowerCase()
  return `/data_sources_icons/${key}.png`
}

function copyCode() {
  try {
    const code = detail.value?.code || ''
    navigator.clipboard.writeText(code)
  } catch {}
}

async function saveEdit() {
  saving.value = true
  errorMsg.value = ''
  try {
    const payload: any = {
      type: form.value.type,
      title: form.value.title || '',
      slug: form.value.slug || '',
      description: form.value.description || null,
      tags: form.value.tags || [],
      code: form.value.code || '',
      view: form.value.view || null,
      status: form.value.status || 'draft',
    }
    const { error } = await useMyFetch(`/api/entities/${id.value}`, { method: 'PUT', body: payload })
    if (error.value) throw error.value
    openEdit.value = false
    await load()
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to save'
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;  
  overflow: hidden;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>


