<template>
  <div class="py-6">
    <div class="max-w-3xl mx-auto px-4">
      <div class="mb-5">
        <h1 class="text-lg font-semibold text-gray-900">Catalog</h1>
        
        <!-- Filter tabs -->
        <div class="mt-3 flex items-center gap-2 border-b border-gray-200">
          <button 
            @click="filterType = 'published'"
            :class="[
              'px-3 py-2 text-xs font-medium border-b-2 transition-colors',
              filterType === 'published' 
                ? 'border-blue-500 text-blue-600' 
                : 'border-transparent text-gray-500 hover:text-gray-700'
            ]"
          >
            Published
          </button>
          <button 
            @click="filterType = 'suggested'"
            :class="[
              'px-3 py-2 text-xs font-medium border-b-2 transition-colors',
              filterType === 'suggested' 
                ? 'border-amber-500 text-amber-600' 
                : 'border-transparent text-gray-500 hover:text-gray-700'
            ]"
          >
            {{ isAdmin ? 'Draft / Suggested' : 'My Drafts' }}
            <span v-if="suggestedCount > 0" class="ml-1.5 px-1.5 py-0.5 rounded-full text-[10px] bg-amber-100 text-amber-700">{{ suggestedCount }}</span>
          </button>
        </div>
        
        <div class="mt-3 flex items-center gap-2">
          <input v-model="q" type="text" placeholder="Search entities…" class="w-full text-sm border rounded px-3 py-2" @keyup.enter="reload()" />
          <button class="text-xs px-3 py-2 rounded border border-gray-200 hover:bg-gray-50" @click="reload()">Search</button>
        </div>
      </div>

      <div v-if="loading" class="text-xs text-gray-500 inline-flex items-center">
        <Spinner class="mr-1" /> Loading...
      </div>
      <div v-else-if="filteredItems.length === 0" class="flex flex-col items-center justify-center py-16 px-4">
        <div class="w-16 h-16 rounded-full bg-gray-50 flex items-center justify-center mb-4">
          <Icon 
            :name="filterType === 'suggested' ? 'heroicons:light-bulb' : 'heroicons:cube'" 
            class="w-8 h-8 text-gray-400" 
          />
        </div>
        <h3 class="text-sm font-medium text-gray-900 mb-1">
          {{ filterType === 'suggested' ? 'No drafts or suggestions yet' : 'No published entities' }}
        </h3>
        <p class="text-xs text-gray-500 text-center max-w-sm">
          {{ filterType === 'suggested' 
            ? 'Draft and suggested entities will appear here as you create them.' 
            : 'Published entities from your connected data sources will appear here.' 
          }}
        </p>
      </div>

      <div class="space-y-3">
        <div
          v-for="item in filteredItems"
          :key="item.id"
          class="border border-gray-100 bg-white rounded-lg p-4 hover:shadow-md hover:border-gray-200 transition-all cursor-pointer"
          @click="navigateToEntity(item.id)"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 mb-1">
                <span
                  class="text-[10px] px-1.5 py-0.5 rounded border"
                  :class="item.type === 'metric' ? 'text-emerald-700 border-emerald-200 bg-emerald-50' : 'text-blue-700 border-blue-200 bg-blue-50'"
                >{{ (item.type || '').toUpperCase() }}</span>
                
                <!-- Green check badge for approved/published entities -->
                <Icon 
                  v-if="getEntityType(item) === 'global'" 
                  name="heroicons:check-badge" 
                  class="w-4 h-4 text-green-600" 
                  title="Approved" 
                />
                
                <!-- Entity workflow status badge -->
                <span 
                  v-if="getEntityType(item) === 'archived'"
                  class="text-[10px] px-1.5 py-0.5 rounded border text-red-700 border-red-200 bg-red-50"
                >ARCHIVED</span>
                <span 
                  v-else-if="getEntityType(item) === 'draft'"
                  class="text-[10px] px-1.5 py-0.5 rounded border text-gray-700 border-gray-200 bg-gray-50"
                >DRAFT</span>
                <span 
                  v-else-if="getEntityType(item) === 'private'"
                  class="text-[10px] px-1.5 py-0.5 rounded border text-gray-700 border-gray-200 bg-gray-50"
                >DRAFT</span>
                <span 
                  v-else-if="getEntityType(item) === 'suggested'"
                  class="text-[10px] px-1.5 py-0.5 rounded border text-amber-700 border-amber-200 bg-amber-50"
                >SUGGESTED</span>
                
                <span class="text-[11px] text-gray-400">{{ timeAgo(item.updated_at) }}</span>
              </div>
              <div class="text-sm font-medium text-gray-900 mb-1">{{ item.title || item.slug }}</div>
              <div class="text-[12px] text-gray-500 line-clamp-2">{{ item.description || 'No description' }}</div>

              <!-- Metadata icons -->
              <div class="flex items-center gap-3 mt-3">
                <div v-if="item.data_sources && item.data_sources.length > 0" class="flex items-center gap-1.5">
                  <img
                    v-for="ds in item.data_sources.slice(0, 3)"
                    :key="ds.id"
                    :src="dataSourceIcon(ds.type)"
                    :alt="ds.type"
                    :title="ds.name || ds.type"
                    class="w-4 h-4 rounded border border-gray-100 bg-white object-contain p-0.5"
                    @error="(e: any) => e.target && (e.target.style.visibility = 'hidden')"
                  />
                  <span v-if="item.data_sources.length > 3" class="text-[11px] text-gray-400">+{{ item.data_sources.length - 3 }}</span>
                </div>
                
                <!-- Data stats -->
                <div v-if="hasStats(item)" class="flex items-center gap-3 text-[11px] text-gray-500">
                  <div v-if="item.data?.info?.total_rows !== undefined" class="flex items-center gap-1" title="Rows">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"></path>
                    </svg>
                    <span>{{ getRowCount(item) }}</span>
                  </div>
                  <div v-if="item.data?.info?.total_columns !== undefined" class="flex items-center gap-1" title="Columns">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 4v16M15 4v16"></path>
                    </svg>
                    <span>{{ getColumnCount(item) }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Results summary -->
      <div v-if="!loading && filteredItems.length > 0" class="mt-6 text-center text-[11px] text-gray-500">
        Showing {{ filteredItems.length }} {{ filterType === 'suggested' ? 'draft/suggested' : 'published' }} {{ filteredItems.length === 1 ? 'entity' : 'entities' }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCan } from '~/composables/usePermissions'
import { useAuth } from '#imports'
import Spinner from '~/components/Spinner.vue'

type MinimalDS = { id: string; name?: string; type?: string }
type EntityList = { 
  id: string
  type: string
  title: string
  slug: string
  description?: string | null
  updated_at: string
  data_sources?: MinimalDS[]
  data?: {
    info?: {
      total_rows?: number
      total_columns?: number
    }
  }
  status?: string
  private_status?: string | null
  global_status?: string | null
  owner_id?: string
}

const router = useRouter()
const { data: authData } = useAuth()
const items = ref<EntityList[]>([])
const allItems = ref<EntityList[]>([])
const loading = ref(true)
const page = ref(1)
const limit = 20
const q = ref('')
const filterType = ref<'published' | 'suggested'>('published')
const isAdmin = computed(() => useCan('update_entities'))

const currentUserId = computed(() => (authData.value as any)?.user?.id)

const suggestedCount = computed(() => {
  return allItems.value.filter(item => {
    const type = getEntityType(item)
    return (type === 'private' || type === 'suggested' || type === 'draft') && !isArchived(item)
  }).length
})

// Filter items based on current filter type and user permissions
const filteredItems = computed(() => {
  let filtered = allItems.value

  // Always exclude archived entities
  filtered = filtered.filter(item => !isArchived(item))

  if (filterType.value === 'published') {
    // Show only global/published entities (approved AND published)
    filtered = filtered.filter(item => getEntityType(item) === 'global')
  } else if (filterType.value === 'suggested') {
    // Show draft and suggested entities
    filtered = filtered.filter(item => {
      const type = getEntityType(item)
      return type === 'private' || type === 'suggested' || type === 'draft'
    })
    
    // If not admin, show only user's own drafts/suggestions
    if (!isAdmin.value) {
      filtered = filtered.filter(item => item.owner_id === currentUserId.value)
    }
  }

  // Apply search filter
  if (q.value) {
    const search = q.value.toLowerCase()
    filtered = filtered.filter(item => 
      item.title?.toLowerCase().includes(search) || 
      item.slug?.toLowerCase().includes(search) ||
      item.description?.toLowerCase().includes(search)
    )
  }

  return filtered
})

watch(filterType, () => {
  page.value = 1
})

onMounted(async () => { await loadEntities() })

async function loadEntities() {
  loading.value = true
  try {
    // Fetch all entities - backend will filter by data source access
    const { data, error } = await useMyFetch('/api/entities', { method: 'GET' })
    if (error.value) throw error.value
    allItems.value = data.value as any
    loading.value = false
  } catch {
    allItems.value = []
    loading.value = false
  }
}

function isArchived(entity: EntityList): boolean {
  return entity.status === 'archived' || entity.private_status === 'archived'
}

function getEntityType(entity: EntityList): string {
  if (isArchived(entity)) return 'archived'
  if (entity.private_status && !entity.global_status) return 'private'
  if (entity.private_status && entity.global_status === 'suggested') return 'suggested'
  // Global approved entities - check if they're published or draft
  if (!entity.private_status && entity.global_status === 'approved') {
    if (entity.status === 'published') return 'global'
    if (entity.status === 'draft') return 'draft'  // Admin draft
  }
  return 'unknown'
}

function reload() {
  loadEntities()
}

function navigateToEntity(id: string) {
  router.push(`/catalog/${id}`)
}

function timeAgo(iso: string | Date | null | undefined) {
  if (!iso) return '—'
  const d = typeof iso === 'string' ? new Date(iso) : iso
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

function formatCount(num?: number): string {
  if (num === undefined || num === null) return '—'
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`
  return String(num)
}

function getRowCount(item: EntityList): string {
  return formatCount(item.data?.info?.total_rows)
}

function getColumnCount(item: EntityList): string {
  return formatCount(item.data?.info?.total_columns)
}

function hasStats(item: EntityList): boolean {
  return !!(item.data?.info?.total_rows !== undefined || item.data?.info?.total_columns !== undefined)
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
</style>


