<template>
  <div class="py-6">
    <div class="max-w-3xl mx-auto px-4">
      <div class="mb-5">
        <h1 class="text-lg font-semibold text-gray-900">Catalog</h1>
        <div class="mt-3 flex items-center gap-2">
          <input v-model="q" type="text" placeholder="Search entities…" class="w-full text-sm border rounded px-3 py-2" @keyup.enter="reload()" />
          <button class="text-xs px-3 py-2 rounded border border-gray-200 hover:bg-gray-50" @click="reload()">Search</button>
        </div>
      </div>

      <div v-if="loading" class="text-xs text-gray-500 inline-flex items-center">
        <Spinner class="mr-1" /> Loading...
      </div>
      <div v-else-if="items.length === 0" class="text-xs text-gray-400">No entities found.</div>

      <div class="space-y-3">
        <div
          v-for="item in items"
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
                
                <!-- Placeholder stats -->
                <div class="flex items-center gap-3 text-[11px] text-gray-500">
                  <div class="flex items-center gap-1" title="Rows">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16"></path>
                    </svg>
                    <span>1.2K</span>
                  </div>
                  <div class="flex items-center gap-1" title="Columns">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 4v16M15 4v16"></path>
                    </svg>
                    <span>12</span>
                  </div>
                </div>
              </div>
            </div>
            <div class="flex-shrink-0">
              <Icon name="heroicons:check-badge" class="w-5 h-5 text-green-500" title="Validated" />
            </div>
          </div>
        </div>
      </div>

      <!-- Pagination -->
      <div class="mt-6 flex items-center justify-center gap-2">
        <button class="text-xs px-3 py-1.5 rounded border border-gray-200 hover:bg-gray-50" :disabled="page===1 || loading" @click="prevPage">Prev</button>
        <div class="text-[11px] text-gray-500">Page {{ page }}</div>
        <button class="text-xs px-3 py-1.5 rounded border border-gray-200 hover:bg-gray-50" :disabled="loading || items.length < limit" @click="nextPage">Next</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCan } from '~/composables/usePermissions'

type MinimalDS = { id: string; name?: string; type?: string }
type EntityList = { id: string; type: string; title: string; slug: string; description?: string | null; updated_at: string; data_sources?: MinimalDS[] }

const router = useRouter()
const items = ref<EntityList[]>([])
const loading = ref(true)
const page = ref(1)
const limit = 20
const q = ref('')
const canCreateEntities = computed(() => useCan('create_entities'))

onMounted(async () => { await loadEntities() })

async function loadEntities() {
  loading.value = true
  try {
    const { data, error } = await useMyFetch('/api/entities', { method: 'GET' })
    if (error.value) throw error.value
    items.value = data.value as any
    loading.value = false
  } catch {
    items.value = []
    loading.value = false
  }
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

function nextPage() {
  if (loading.value) return
  page.value += 1
  reload()
}

function prevPage() {
  if (loading.value || page.value === 1) return
  page.value -= 1
  reload()
}
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;  
  overflow: hidden;
}
</style>


