<template>
  <div class="px-6 md:px-10 py-8 max-w-7xl mx-auto w-full">
    <!-- Header -->
    <div class="flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h1 class="text-2xl font-semibold text-gray-900">Integrations</h1>
        <p class="mt-1 text-sm text-gray-500">Connect external tools to retrieve data, take actions, and more.</p>
      </div>
      <UButton color="primary" icon="heroicons-plus" @click="refresh">
        Add integration
      </UButton>
    </div>

    <!-- Filters + search -->
    <div class="mt-6 flex items-center justify-between gap-4 flex-wrap">
      <div class="flex items-center gap-2">
        <button
          v-for="f in filters"
          :key="f.key"
          @click="activeFilter = f.key"
          :class="[
            'px-3 py-1.5 rounded-full text-sm border transition',
            activeFilter === f.key
              ? 'bg-gray-900 text-white border-gray-900'
              : 'bg-white text-gray-600 border-gray-200 hover:border-gray-300'
          ]"
        >{{ f.label }}</button>
      </div>
      <div class="relative">
        <UIcon name="heroicons-magnifying-glass" class="w-4 h-4 text-gray-400 absolute left-3 top-1/2 -translate-y-1/2" />
        <input
          v-model="search"
          type="text"
          placeholder="Search integrations..."
          class="pl-9 pr-3 py-2 w-64 max-w-full text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-100 focus:border-indigo-300"
        />
      </div>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="flex flex-col items-center justify-center py-24">
      <Spinner class="h-5 w-5 text-gray-400" />
      <p class="text-sm text-gray-500 mt-3">Loading integrations...</p>
    </div>

    <template v-else>
      <!-- Connected / popular -->
      <section v-if="connectedCards.length && activeFilter !== 'needs'" class="mt-8">
        <h2 class="flex items-center gap-1.5 text-sm font-medium text-gray-700">
          <UIcon name="heroicons-fire" class="w-4 h-4 text-orange-500" /> Popular in workspace
        </h2>
        <div class="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          <IntegrationCard
            v-for="c in connectedCards" :key="c.type"
            :card="c" @open="openDetail(c)" @connect="connect(c)"
          />
        </div>
      </section>

      <!-- All integrations -->
      <section class="mt-10">
        <h2 class="text-sm font-medium text-gray-700">All integrations</h2>
        <div class="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          <IntegrationCard
            v-for="c in otherCards" :key="c.type"
            :card="c" @open="openDetail(c)" @connect="connect(c)"
          />
        </div>
        <p v-if="!filteredCards.length" class="text-sm text-gray-400 py-12 text-center">
          No integrations match “{{ search }}”.
        </p>
      </section>
    </template>

    <!-- Detail modal -->
    <UModal v-model="detailOpen" :ui="{ width: 'sm:max-w-2xl' }">
      <div v-if="detail" class="p-6">
        <div class="flex items-center gap-3">
          <DataSourceIcon :type="detail.type" class="h-9" />
          <div>
            <div class="flex items-center gap-1.5">
              <span class="text-lg font-semibold text-gray-900">{{ detail.title }}</span>
              <UIcon v-if="detail.connected" name="heroicons-check-badge" class="w-4 h-4 text-indigo-500" />
            </div>
            <div class="text-sm text-gray-500">{{ detail.description }}</div>
          </div>
        </div>

        <div class="mt-5 flex items-center justify-between">
          <h3 class="text-sm font-medium text-gray-800">Actions</h3>
          <span class="text-xs text-gray-400">{{ detail.tools?.length || 0 }} available</span>
        </div>

        <div v-if="detail.connected" class="mt-3 space-y-2 max-h-80 overflow-y-auto pr-1">
          <div
            v-for="t in detail.tools" :key="t.id"
            class="flex items-start justify-between gap-3 border border-gray-100 rounded-lg p-3 hover:border-gray-200"
          >
            <div class="min-w-0">
              <div class="text-sm font-medium text-gray-900">{{ prettyName(t.name) }}</div>
              <div class="text-xs text-gray-500 line-clamp-2">{{ t.description }}</div>
            </div>
            <UToggle
              :model-value="t.is_accessible"
              @update:model-value="(v) => toggleTool(detail, t, v)"
            />
          </div>
        </div>

        <div v-else class="mt-4 rounded-lg border border-dashed border-gray-200 p-6 text-center">
          <p class="text-sm text-gray-600">Connect your account to use {{ detail.title }} actions.</p>
          <UButton class="mt-3" color="primary" @click="connect(detail)">
            Connect {{ detail.title }}
          </UButton>
        </div>
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'

definePageMeta({ auth: true, layout: 'default' })

interface Tool { id: string; name: string; description?: string; is_enabled: boolean; is_accessible: boolean }
interface Card {
  type: string
  title: string
  description: string
  ui_form: string
  connected: boolean
  needs_user_auth: boolean
  connection_id?: string
  tool_count: number
  tools: Tool[]
}

const loading = ref(true)
const catalog = ref<any[]>([])
const connectedList = ref<any[]>([])
const search = ref('')
const activeFilter = ref<'all' | 'connected' | 'needs'>('all')
const filters = [
  { key: 'all', label: 'All' },
  { key: 'connected', label: 'Connected' },
  { key: 'needs', label: 'Needs configuration' },
] as const

const detailOpen = ref(false)
const detail = ref<Card | null>(null)

const cards = computed<Card[]>(() => {
  const byType: Record<string, any> = {}
  for (const c of connectedList.value) byType[c.type] = c
  return catalog.value.map((e) => {
    const conn = byType[e.type]
    return {
      type: e.type,
      title: e.title,
      description: e.description,
      ui_form: e.ui_form,
      connected: !!conn?.connected,
      needs_user_auth: !!conn?.needs_user_auth,
      connection_id: conn?.connection_id,
      tool_count: conn?.tool_count || 0,
      tools: conn?.tools || [],
    } as Card
  })
})

const filteredCards = computed(() => {
  const q = search.value.trim().toLowerCase()
  return cards.value.filter((c) => {
    if (activeFilter.value === 'connected' && !c.connected) return false
    if (activeFilter.value === 'needs' && !(c.needs_user_auth || (!c.connected && !c.connection_id))) return false
    if (q && !(`${c.title} ${c.description}`.toLowerCase().includes(q))) return false
    return true
  })
})

const connectedCards = computed(() => filteredCards.value.filter((c) => c.connected))
const otherCards = computed(() => filteredCards.value.filter((c) => !c.connected))

function prettyName(n: string) {
  return n.replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())
}

function openDetail(c: Card) {
  detail.value = c
  detailOpen.value = true
}

async function connect(c: Card) {
  // Deep-link to the connection's per-user OAuth / setup page.
  if (c.connection_id) {
    navigateTo(`/integrations/${c.connection_id}/connection`)
  } else {
    navigateTo(`/agents/new?type=${c.type}`)
  }
}

async function toggleTool(card: Card, tool: Tool, value: boolean) {
  tool.is_accessible = value
  if (!card.connection_id) return
  await useMyFetch(`/integrations/${card.connection_id}/tools/${tool.name}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tool_name: tool.name, is_accessible: value }),
  })
}

async function refresh() {
  loading.value = true
  try {
    const [cat, list] = await Promise.all([
      useMyFetch('/integrations/catalog', { method: 'GET' }),
      useMyFetch('/integrations', { method: 'GET' }),
    ])
    catalog.value = (cat.data.value as any[]) || []
    connectedList.value = (list.data.value as any[]) || []
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>
