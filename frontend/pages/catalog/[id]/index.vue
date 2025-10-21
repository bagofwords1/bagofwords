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
          <div class="flex-shrink-0 ml-auto flex items-center gap-2">
            <button v-if="canDeleteEntities" class="text-[11px] px-2 py-0.5 rounded border border-red-300 bg-red-50 text-red-700 hover:bg-red-100" @click="deleteEntity" :disabled="deleting">
              <span v-if="deleting">Deleting…</span>
              <span v-else>Delete</span>
            </button>
            <button v-if="canUpdateEntities" class="text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-gray-50" @click="openEdit = true">
              Edit
            </button>
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
          
          
          <button class="text-[11px] px-2 py-0.5 rounded border border-gray-200 hover:bg-gray-50" :class="{ 'ml-auto': !canCreateEntities && !isOwner && !canSuggest && !canWithdraw && !canApprove }" @click="refreshEntity" :disabled="refreshing">
            <span v-if="refreshing">Refreshing…</span>
            <span v-else>Refresh Data</span>
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

    <EntityEditModal
      v-model="openEdit"
      :detail="detail"
      :entity-id="id"
      :editor-lang="editorLang"
      @saved="onModalSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, defineAsyncComponent } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCan } from '~/composables/usePermissions'
import { useAuth } from '#imports'
import RenderVisual from '~/components/RenderVisual.vue'
import RenderTable from '~/components/RenderTable.vue'
import { resolveEntryByType } from '@/components/dashboard/registry'
import EntityForm from '~/components/entity/EntityForm.vue'
import EntityEditModal from '~/components/entity/EntityEditModal.vue'

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
const router = useRouter()
const { data: authData } = useAuth()
const id = computed(() => String(route.params.id || ''))
const detail = ref<EntityDetail | null>(null)
const loading = ref(true)
const canCreateEntities = computed(() => useCan('create_entities'))
const canUpdateEntities = computed(() => useCan('update_entities'))
const canSuggestEntities = computed(() => useCan('suggest_entities'))
const canApproveEntities = computed(() => useCan('approve_entities'))
const canDeleteEntities = computed(() => useCan('delete_entities'))

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

const deleting = ref(false)
async function deleteEntity() {
  if (deleting.value) return
  const confirmed = window.confirm('Are you sure you want to delete this entity? This action cannot be undone.')
  if (!confirmed) return
  deleting.value = true
  try {
    const { error } = await useMyFetch(`/api/entities/${id.value}`, { method: 'DELETE' })
    if (error.value) throw error.value
    toast.add({ title: 'Deleted', description: 'Entity deleted successfully', color: 'green' })
    router.push('/catalog')
  } catch (e: any) {
    toast.add({ title: 'Error', description: e?.data?.detail || e?.message || 'Failed to delete entity', color: 'red' })
  } finally {
    deleting.value = false
  }
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

function onModalSaved() {
  load()
  toast.add({ title: 'Success', description: 'Entity saved successfully', color: 'green' })
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


