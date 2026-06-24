<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="px-6 md:px-8 pt-6 pb-4 border-b border-gray-100 flex items-start justify-between gap-4 flex-wrap">
      <div>
        <h1 class="text-xl font-semibold text-gray-900">Integrations</h1>
        <p class="mt-1 text-sm text-gray-500">Connect your tools and use workspace data — query them directly or from any report.</p>
      </div>
      <UButton color="primary" icon="heroicons-plus" @click="refresh">Add integration</UButton>
    </div>

    <div class="flex flex-1 min-h-0 flex-col md:flex-row">
      <!-- Master: list -->
      <div class="w-full md:w-80 lg:w-96 shrink-0 border-r border-gray-100 flex flex-col">
        <div class="p-3 border-b border-gray-100">
          <div class="relative">
            <UIcon name="heroicons-magnifying-glass" class="w-4 h-4 text-gray-400 absolute left-3 top-1/2 -translate-y-1/2" />
            <input v-model="search" type="text" placeholder="Search…"
              class="pl-9 pr-3 py-2 w-full text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-100 focus:border-indigo-300" />
          </div>
        </div>

        <div v-if="loading" class="flex items-center justify-center py-16">
          <Spinner class="h-5 w-5 text-gray-400" />
        </div>

        <div v-else class="flex-1 overflow-y-auto py-2">
          <IntegrationGroup title="Your connections" :items="personalItems" :selected="selected" @select="select" />
          <IntegrationGroup title="Workspace data" :items="orgItems" :selected="selected" @select="select" />
          <IntegrationGroup title="Available" :items="availableItems" :selected="selected" @select="select" />
          <p v-if="!filtered.length" class="text-xs text-gray-400 text-center py-8">No matches.</p>
        </div>
      </div>

      <!-- Detail -->
      <div class="flex-1 min-w-0 overflow-y-auto">
        <div v-if="!selected" class="h-full flex flex-col items-center justify-center text-center px-6 py-16">
          <div class="w-12 h-12 rounded-xl bg-gray-50 flex items-center justify-center">
            <UIcon name="heroicons-puzzle-piece" class="w-6 h-6 text-gray-300" />
          </div>
          <p class="mt-3 text-sm text-gray-500">Select an integration to view its tools and connect.</p>
        </div>

        <div v-else class="px-6 md:px-8 py-6 max-w-3xl">
          <div class="flex items-start justify-between gap-4 flex-wrap">
            <div class="flex items-center gap-3">
              <DataSourceIcon :type="selected.type" class="h-9" />
              <div>
                <div class="flex items-center gap-2">
                  <span class="text-lg font-semibold text-gray-900">{{ selected.title }}</span>
                  <span :class="badgeClass(selected.kind)">{{ badgeLabel(selected.kind) }}</span>
                </div>
                <div class="text-sm text-gray-500">{{ selected.description }}</div>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <UButton v-if="selected.connected" size="xs" color="gray" variant="solid" icon="heroicons-plus" @click="newReport(selected)">New report</UButton>
              <UButton v-if="selected.kind === 'personal' && !selected.connected" size="xs" color="primary" @click="connect(selected)">Connect</UButton>
              <UButton v-else-if="selected.kind === 'available'" size="xs" color="primary" @click="connect(selected)">Connect</UButton>
              <UButton v-else-if="selected.kind === 'org'" size="xs" color="gray" variant="ghost" icon="heroicons-cog-6-tooth" @click="configure(selected)">Configure</UButton>
            </div>
          </div>

          <!-- Tools (personal/connected tool integrations) -->
          <div v-if="selected.kind !== 'org' && selected.tools?.length" class="mt-6">
            <div class="flex items-center justify-between">
              <h3 class="text-sm font-medium text-gray-800">Tools</h3>
              <span class="text-xs text-gray-400">{{ selected.tools.length }} available</span>
            </div>
            <div v-if="selected.connected" class="mt-3 space-y-2">
              <div v-for="t in selected.tools" :key="t.id"
                class="flex items-start justify-between gap-3 border border-gray-100 rounded-lg p-3 hover:border-gray-200">
                <div class="min-w-0">
                  <div class="text-sm font-medium text-gray-900">{{ prettyName(t.name) }}</div>
                  <div class="text-xs text-gray-500 line-clamp-2">{{ t.description }}</div>
                </div>
                <UToggle :model-value="t.is_accessible" @update:model-value="(v) => toggleTool(selected, t, v)" />
              </div>
            </div>
            <div v-else class="mt-3 rounded-lg border border-dashed border-gray-200 p-6 text-center">
              <p class="text-sm text-gray-600">Connect your account to use {{ selected.title }} tools.</p>
              <UButton class="mt-3" color="primary" @click="connect(selected)">Connect {{ selected.title }}</UButton>
            </div>
          </div>

          <!-- Org data source: read-only note -->
          <div v-if="selected.kind === 'org'" class="mt-6 rounded-lg border border-gray-100 bg-gray-50/60 p-4">
            <div class="flex items-center gap-2 text-sm text-gray-700">
              <UIcon name="heroicons-building-office-2" class="w-4 h-4 text-gray-400" />
              Shared workspace data source. Query it from a report with <code class="px-1 text-indigo-600">@{{ selected.name }}</code> or open it in an agent.
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'

definePageMeta({ auth: true, layout: 'default' })

interface Tool { id: string; name: string; description?: string; is_enabled: boolean; is_accessible: boolean }
interface Item {
  key: string
  type: string
  title: string
  description: string
  kind: 'personal' | 'org' | 'available'
  connected: boolean
  needs_user_auth: boolean
  connection_id?: string
  tool_count: number
  tools: Tool[]
  name: string
}

const loading = ref(true)
const catalog = ref<any[]>([])
const connectedList = ref<any[]>([])
const search = ref('')
const selected = ref<Item | null>(null)

const items = computed<Item[]>(() => {
  const out: Item[] = []
  const seenTypes = new Set<string>()
  // Connected + org items from the live list.
  for (const c of connectedList.value) {
    seenTypes.add(c.type)
    out.push({
      key: c.connection_id, type: c.type, title: c.title, description: c.description,
      kind: c.kind, connected: c.connected, needs_user_auth: c.needs_user_auth,
      connection_id: c.connection_id, tool_count: c.tool_count, tools: c.tools || [], name: c.name,
    })
  }
  // Available catalog (self-serve types not already connected).
  for (const e of catalog.value) {
    if (seenTypes.has(e.type)) continue
    out.push({
      key: e.type, type: e.type, title: e.title, description: e.description,
      kind: 'available', connected: false, needs_user_auth: false,
      tool_count: 0, tools: [], name: e.title,
    })
  }
  return out
})

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return items.value
  return items.value.filter(i => `${i.title} ${i.description}`.toLowerCase().includes(q))
})
const personalItems = computed(() => filtered.value.filter(i => i.kind === 'personal'))
const orgItems = computed(() => filtered.value.filter(i => i.kind === 'org'))
const availableItems = computed(() => filtered.value.filter(i => i.kind === 'available'))

function badgeLabel(kind: string) {
  return kind === 'personal' ? 'Connected as you' : kind === 'org' ? 'Org' : 'Available'
}
function badgeClass(kind: string) {
  const base = 'text-[10px] px-1.5 py-0.5 rounded-full border '
  if (kind === 'personal') return base + 'text-green-700 bg-green-50 border-green-100'
  if (kind === 'org') return base + 'text-indigo-700 bg-indigo-50 border-indigo-100'
  return base + 'text-gray-500 bg-gray-50 border-gray-200'
}
function prettyName(n: string) { return n.replace(/_/g, ' ').replace(/\b\w/g, m => m.toUpperCase()) }

function select(i: Item) { selected.value = i }

async function connect(i: Item) {
  if (i.connection_id) navigateTo(`/integrations/${i.connection_id}/connection`)
  else navigateTo(`/agents/new?type=${i.type}`)
}
function configure(i: Item) { if (i.connection_id) navigateTo(`/integrations/${i.connection_id}/connection`) }
function newReport(i: Item) { navigateTo(`/?integration=${i.connection_id || i.type}`) }

async function toggleTool(item: Item, tool: Tool, value: boolean) {
  tool.is_accessible = value
  if (!item.connection_id) return
  await useMyFetch(`/integrations/${item.connection_id}/tools/${tool.name}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
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
    if (!selected.value && items.value.length) selected.value = items.value[0]
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>
