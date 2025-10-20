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
              
              <!-- Green check badge for approved/published entities -->
              <Icon 
                v-if="entityType === 'global'" 
                name="heroicons:check-badge" 
                class="w-4 h-4 text-green-600" 
                title="Approved" 
              />
              
              <!-- Entity workflow status badge -->
              <span 
                v-if="entityType === 'draft'"
                class="text-[10px] px-1.5 py-0.5 rounded border text-gray-700 border-gray-200 bg-gray-50"
              >DRAFT</span>
              <span 
                v-else-if="entityType === 'private'"
                class="text-[10px] px-1.5 py-0.5 rounded border text-gray-700 border-gray-200 bg-gray-50"
              >DRAFT</span>
              <span 
                v-else-if="entityType === 'suggested'"
                class="text-[10px] px-1.5 py-0.5 rounded border text-amber-700 border-amber-200 bg-amber-50"
              >SUGGESTED</span>
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

        <div class="mt-3 flex items-center gap-2 flex-wrap">
          <div v-if="viewType" class="text-[11px] text-gray-500 px-1.5 py-0.5 bg-gray-50 border border-gray-100 rounded">{{ viewType }}</div>
          <div v-if="detail?.data?.info?.total_rows !== undefined" class="text-[11px] text-gray-400">{{ formatCount(detail?.data?.info?.total_rows) }} rows</div>
          <div v-if="detail?.data?.info?.total_columns !== undefined" class="text-[11px] text-gray-400">{{ formatCount(detail?.data?.info?.total_columns) }} columns</div>
          <div v-if="detail?.last_refreshed_at" class="text-[11px] text-gray-400">Refreshed {{ timeAgo(detail?.last_refreshed_at as any) }}</div>
          
          <!-- Workflow actions -->
          <button v-if="canSuggest" class="ml-auto text-[11px] px-2 py-0.5 rounded border border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100" @click="suggestEntity" :disabled="suggesting">
            <span v-if="suggesting">Suggesting…</span>
            <span v-else>Suggest for Approval</span>
          </button>
          
          <button v-if="canWithdraw" class="ml-auto text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-gray-50" @click="withdrawSuggestion" :disabled="withdrawing">
            <span v-if="withdrawing">Withdrawing…</span>
            <span v-else>Withdraw Suggestion</span>
          </button>
          
          <button v-if="canApprove" class="ml-auto text-[11px] px-2 py-0.5 rounded border border-green-300 bg-green-50 text-green-700 hover:bg-green-100" @click="approveSuggestion" :disabled="approving">
            <span v-if="approving">Approving…</span>
            <span v-else>Approve</span>
          </button>
          
          <button v-if="canApprove" class="text-[11px] px-2 py-0.5 rounded border border-red-300 bg-red-50 text-red-700 hover:bg-red-100" @click="rejectSuggestion" :disabled="rejecting">
            <span v-if="rejecting">Rejecting…</span>
            <span v-else>Reject</span>
          </button>
          
          <button v-if="canCreateEntities || isOwner" class="text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-gray-50" :class="{ 'ml-auto': !canSuggest && !canWithdraw && !canApprove }" @click="openEdit = true">
            Edit
          </button>
          <button class="text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-gray-50" :class="{ 'ml-auto': !canCreateEntities && !isOwner && !canSuggest && !canWithdraw && !canApprove }" @click="refreshEntity" :disabled="refreshing">
            <span v-if="refreshing">Refreshing…</span>
            <span v-else>Refresh</span>
          </button>
        </div>

        <div class="mt-4">
          <div class="border border-gray-100 rounded bg-white">
            <!-- Tab Navigation -->
            <div class="flex border-b border-gray-100">
              <button 
                v-if="showVisual"
                @click="activeTab = 'visual'"
                :class="[
                  'px-4 py-2 text-xs font-medium border-b-2 transition-colors',
                  activeTab === 'visual' 
                    ? 'border-blue-500 text-blue-600' 
                    : 'border-transparent text-gray-500 hover:text-gray-700'
                ]"
              >
                Visual
              </button>
              <button 
                @click="activeTab = 'data'"
                :class="[
                  'px-4 py-2 text-xs font-medium border-b-2 transition-colors',
                  activeTab === 'data' 
                    ? 'border-blue-500 text-blue-600' 
                    : 'border-transparent text-gray-500 hover:text-gray-700'
                ]"
              >
                <span>Data</span>
                <span v-if="rowCount" class="ml-1.5 text-[11px] text-gray-400">({{ rowCount }})</span>
              </button>
              <button 
                @click="activeTab = 'code'"
                :class="[
                  'px-4 py-2 text-xs font-medium border-b-2 transition-colors',
                  activeTab === 'code' 
                    ? 'border-blue-500 text-blue-600' 
                    : 'border-transparent text-gray-500 hover:text-gray-700'
                ]"
              >
                Code
              </button>
            </div>

            <div class="p-3">
              <!-- Visual Content -->
              <Transition name="fade" mode="out-in">
                <div v-if="activeTab === 'visual'" class="bg-gray-50 rounded-sm p-2">
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

              <!-- Data/Table Content -->
              <Transition name="fade" mode="out-in">
                <div v-if="activeTab === 'data'" class="h-[400px]">
                  <RenderTable :widget="effectiveWidget" :step="effectiveStep" />
                </div>
              </Transition>

              <!-- Code Content -->
              <Transition name="fade" mode="out-in">
                <div v-if="activeTab === 'code'" class="bg-gray-50 rounded p-3 overflow-auto" style="max-height: 400px;">
                  <div class="flex items-center justify-between mb-2">
                    <span class="text-[11px] text-gray-500">&nbsp;</span>
                    <button class="text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-white" @click="copyCode">Copy</button>
                  </div>
                  <pre class="text-[11px] text-gray-800"><code>{{ detail?.code || '// No code available' }}</code></pre>
                </div>
              </Transition>
            </div>
          </div>
        </div>
      </div>
    </div>

    <UModal v-model="openEdit" :ui="{ width: 'sm:max-w-6xl', height: 'sm:h-[90vh]' }">
      <div class="h-full flex flex-col">
        <div class="px-4 py-3 bg-white border-b flex items-center justify-between flex-shrink-0">
          <div class="text-sm font-medium text-gray-800">{{ detail?.title || detail?.slug }}</div>
          <button class="text-xs text-gray-500 hover:text-gray-700" @click="openEdit = false">Close</button>
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
                <button class="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50" @click="openEdit = false">Cancel</button>
                <button class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded-lg disabled:opacity-50" :disabled="saving" @click="saveEdit">
                  <span v-if="saving">Saving...</span>
                  <span v-else>Save</span>
                </button>
              </div>
            </div>
            <div v-else class="h-full flex flex-col">
              <!-- Editor section - exactly half height -->
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
              <!-- Results section - exactly half height -->
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
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, defineAsyncComponent } from 'vue'
import { useRoute } from 'vue-router'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCan } from '~/composables/usePermissions'
import { useAuth } from '#imports'
import RenderVisual from '~/components/RenderVisual.vue'
import RenderTable from '~/components/RenderTable.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'
import EntityForm from '~/components/tools/EntityForm.vue'

const toast = useToast()

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

const route = useRoute()
const { data: authData } = useAuth()
const id = computed(() => String(route.params.id || ''))
const detail = ref<EntityDetail | null>(null)
const loading = ref(true)
const canCreateEntities = computed(() => useCan('create_entities'))
const canUpdateEntities = computed(() => useCan('update_entities'))
const canSuggestEntities = computed(() => useCan('suggest_entities'))
const canApproveEntities = computed(() => useCan('approve_entities'))

// Entity workflow status
const entityType = computed(() => {
  const e = detail.value
  if (!e) return null
  if (e.status === 'archived' || e.private_status === 'archived') return 'archived'
  if (e.private_status && !e.global_status) return 'private'
  if (e.private_status && e.global_status === 'suggested') return 'suggested'
  // Global approved entities - check if they're published or draft
  if (!e.private_status && e.global_status === 'approved') {
    if (e.status === 'published') return 'global'
    if (e.status === 'draft') return 'draft'  // Admin draft
  }
  return 'unknown'
})

const isOwner = computed(() => {
  const currentUserId = (authData.value as any)?.user?.id
  return currentUserId && detail.value?.owner_id === currentUserId
})

const canSuggest = computed(() => {
  return isOwner.value && entityType.value === 'private' && canSuggestEntities.value
})

const canWithdraw = computed(() => {
  return isOwner.value && entityType.value === 'suggested'
})

const canApprove = computed(() => {
  return canApproveEntities.value && entityType.value === 'suggested'
})

const viewType = computed(() => String(detail.value?.view?.type || ''))
const shape = computed(() => {
  const d = detail.value?.data
  if (!d) return null
  const rows = Array.isArray(d?.rows) ? d.rows.length : (typeof d?.info?.total_rows === 'number' ? d.info.total_rows : null)
  const cols = Array.isArray(d?.columns) ? d.columns.length : (Array.isArray(d?.rows) && d.rows[0] ? Object.keys(d.rows[0]).length : null)
  return { rows, cols }
})

// Tab state and visualization logic
const activeTab = ref<'visual' | 'data' | 'code'>('visual')

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
const editTab = ref<'details'|'code'>('details')
const errorMsg = ref('')
const saving = ref(false)
const running = ref(false)
const runMode = ref<'preview' | 'save' | null>(null)
const codePreview = ref<any | null>(null)
const codeErrorMsg = ref('')

// Watch for data changes to update active tab
watch([showVisual, hasData], () => {
  if (showVisual.value) {
    activeTab.value = 'visual'
  } else if (hasData.value) {
    activeTab.value = 'data'
  } else {
    activeTab.value = 'code'
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
    const { data, error } = await useMyFetch(`/api/entities/${id.value}/run`, { method: 'POST', body: {} })
    if (error.value) throw error.value
    detail.value = data.value as any
  } catch {}
  refreshing.value = false
}

// Suggestion workflow actions
const suggesting = ref(false)
async function suggestEntity() {
  if (suggesting.value) return
  suggesting.value = true
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id.value}/suggest`, { method: 'POST' })
    if (error.value) throw error.value
    detail.value = data.value as any
    toast.add({
      title: 'Success',
      description: 'Entity suggested for approval',
      color: 'green'
    })
  } catch (e: any) {
    console.error('Failed to suggest entity:', e)
    toast.add({
      title: 'Error',
      description: 'Failed to suggest entity',
      color: 'red'
    })
  }
  suggesting.value = false
}

const withdrawing = ref(false)
async function withdrawSuggestion() {
  if (withdrawing.value) return
  withdrawing.value = true
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id.value}/withdraw`, { method: 'POST' })
    if (error.value) throw error.value
    detail.value = data.value as any
    toast.add({
      title: 'Success',
      description: 'Suggestion withdrawn',
      color: 'green'
    })
  } catch (e: any) {
    console.error('Failed to withdraw suggestion:', e)
    toast.add({
      title: 'Error',
      description: 'Failed to withdraw suggestion',
      color: 'red'
    })
  }
  withdrawing.value = false
}

const approving = ref(false)
async function approveSuggestion() {
  if (approving.value) return
  approving.value = true
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id.value}/approve`, { method: 'POST' })
    if (error.value) throw error.value
    detail.value = data.value as any
    toast.add({
      title: 'Success',
      description: 'Entity approved and published',
      color: 'green'
    })
  } catch (e: any) {
    console.error('Failed to approve suggestion:', e)
    toast.add({
      title: 'Error',
      description: 'Failed to approve entity',
      color: 'red'
    })
  }
  approving.value = false
}

const rejecting = ref(false)
async function rejectSuggestion() {
  if (rejecting.value) return
  rejecting.value = true
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id.value}/reject`, { method: 'POST' })
    if (error.value) throw error.value
    detail.value = data.value as any
    toast.add({
      title: 'Success',
      description: 'Entity rejected',
      color: 'green'
    })
  } catch (e: any) {
    console.error('Failed to reject suggestion:', e)
    toast.add({
      title: 'Error',
      description: 'Failed to reject entity',
      color: 'red'
    })
  }
  rejecting.value = false
}

watch(openEdit, (v) => {
  if (v && detail.value) {
    form.value = {
      type: detail.value.type,
      title: detail.value.title,
      description: (detail.value.description || null) as any,
      code: detail.value.code || '',
      status: detail.value.status || 'draft',
      data_source_ids: detail.value.data_sources?.map(ds => ds.id) || [],
      global_status: detail.value.global_status || null
    }
    editTab.value = 'details'
    // Prepopulate preview with existing data
    codePreview.value = detail.value.data || null
    codeErrorMsg.value = ''
    errorMsg.value = ''
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

function formatCount(num?: number): string {
  if (num === undefined || num === null) return '—'
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`
  return String(num)
}

async function previewRun() {
  running.value = true
  runMode.value = 'preview'
  codeErrorMsg.value = ''
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id.value}/preview`, {
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
    const { data, error } = await useMyFetch(`/api/entities/${id.value}/run`, { method: 'POST', body: payload })
    if (error.value) throw error.value
    const result: any = data.value
    if (result?.error) {
      codeErrorMsg.value = result.error
      codePreview.value = null
      return
    }
    codePreview.value = result?.data || null
    detail.value = result as any
    openEdit.value = false
    await load()
  } catch (e: any) {
    codeErrorMsg.value = e?.data?.detail || e?.message || 'Failed to save and run'
  } finally {
    running.value = false
    runMode.value = null
  }
}

async function saveEdit() {
  saving.value = true
  errorMsg.value = ''
  try {
    const payload: any = {
      type: form.value.type,
      title: form.value.title || '',
      description: form.value.description || null,
      status: form.value.status || 'draft',
      data_source_ids: form.value.data_source_ids || []
    }
    const { error } = await useMyFetch(`/api/entities/${id.value}`, { method: 'PUT', body: payload })
    if (error.value) throw error.value
    openEdit.value = false
    await load()
    toast.add({
      title: 'Success',
      description: 'Entity saved successfully',
      color: 'green'
    })
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to save'
    toast.add({
      title: 'Error',
      description: e?.data?.detail || e?.message || 'Failed to save entity',
      color: 'red'
    })
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
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


