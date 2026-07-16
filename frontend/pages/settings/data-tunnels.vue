<template>
  <div class="mt-4">
    <div class="mb-4">
      <h2 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.dataTunnels.title') }}</h2>
      <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.dataTunnels.subtitle') }}</p>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 py-6">
      <svg class="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
      </svg>
      {{ $t('settings.dataTunnels.loading') }}
    </div>

    <!-- Error -->
    <div v-else-if="error" class="rounded border border-red-200 dark:border-red-800 p-3 bg-red-50 dark:bg-red-900/20 text-xs text-red-700 dark:text-red-300">
      {{ error }}
    </div>

    <!-- Empty -->
    <div v-else-if="agents.length === 0" class="rounded border border-gray-200 dark:border-gray-700 p-6 text-center">
      <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.dataTunnels.empty') }}</p>
    </div>

    <!-- Agents + their advertised data sources -->
    <div v-else class="space-y-4">
      <div
        v-for="agent in agents"
        :key="agent.id"
        class="border border-gray-200 dark:border-gray-700 rounded overflow-hidden"
      >
        <!-- Agent header -->
        <div class="flex items-center justify-between px-3 py-2 bg-gray-50 dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700">
          <div class="flex items-center gap-2 min-w-0">
            <UIcon name="i-heroicons-server-stack" class="w-4 h-4 text-gray-400 shrink-0" />
            <span class="text-xs font-medium text-gray-900 dark:text-white truncate">
              {{ agent.label || agent.edge_agent_id }}
            </span>
            <span class="text-[11px] text-gray-400">{{ agent.edge_agent_id }}</span>
          </div>
          <div class="flex items-center gap-3 shrink-0">
            <span
              class="inline-flex items-center gap-1 text-[11px] px-1.5 py-0.5 rounded"
              :class="statusClass(agent.status)"
            >
              <span class="w-1.5 h-1.5 rounded-full" :class="statusDot(agent.status)" />
              {{ agent.status || 'unknown' }}
            </span>
            <span v-if="agent.last_advertised_at" class="text-[11px] text-gray-400">
              {{ $t('settings.dataTunnels.lastSeen') }} {{ formatTime(agent.last_advertised_at) }}
            </span>
          </div>
        </div>

        <!-- Advertised connections -->
        <div v-if="agent.connections.length === 0" class="px-3 py-2 text-xs text-gray-400">
          {{ $t('settings.dataTunnels.noConnections') }}
        </div>
        <div v-else>
          <div
            v-for="conn in agent.connections"
            :key="conn.name"
            class="flex items-center justify-between px-3 py-2 text-xs border-b border-gray-100 dark:border-gray-800 last:border-b-0"
          >
            <div class="flex items-center gap-2 min-w-0">
              <!-- Data source type logo (postgresql, snowflake, …); falls back to
                   document.png for unknown types. -->
              <DataSourceIcon :type="conn.type" class="h-4 w-4 shrink-0" />
              <span class="font-medium text-gray-900 dark:text-white truncate">{{ conn.label || conn.name }}</span>
              <span class="text-gray-400">{{ conn.name }}</span>
              <span class="text-[11px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300">
                {{ conn.type }}
              </span>
            </div>
            <div class="shrink-0">
              <span
                v-if="conn.status === 'conflict'"
                class="text-[11px] text-amber-700 dark:text-amber-400"
                :title="conn.reason || ''"
              >
                {{ $t('settings.dataTunnels.conflict') }}
              </span>
              <span v-else class="text-[11px] text-green-700 dark:text-green-400">
                {{ $t('settings.dataTunnels.registered') }}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({
  auth: true,
  permissions: ['manage_settings'],
  layout: 'settings'
})

interface AdvertisedConnection {
  name: string
  type: string
  label?: string | null
  status?: string | null
  reason?: string | null
}
interface DataEdgeAgent {
  id: string
  edge_agent_id: string
  label?: string | null
  status?: string | null
  client_version?: string | null
  last_advertised_at?: string | null
  connections: AdvertisedConnection[]
}

const agents = ref<DataEdgeAgent[]>([])
const loading = ref(true)
const error = ref<string | null>(null)

const load = async () => {
  loading.value = true
  error.value = null
  try {
    const res = await useMyFetch('/api/data-tunnels/agents')
    if (res.status.value !== 'success') {
      const detail = (res.error?.value as any)?.data?.detail
      throw new Error(detail || 'Failed to load data tunnels')
    }
    agents.value = (res.data.value as DataEdgeAgent[]) || []
  } catch (e: any) {
    error.value = e?.message || 'Failed to load data tunnels'
  } finally {
    loading.value = false
  }
}

const statusClass = (status?: string | null) => {
  if (status === 'online') return 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-400'
  if (status === 'stale') return 'bg-amber-50 dark:bg-amber-900/20 text-amber-700 dark:text-amber-400'
  return 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400'
}
const statusDot = (status?: string | null) => {
  if (status === 'online') return 'bg-green-500'
  if (status === 'stale') return 'bg-amber-500'
  return 'bg-gray-400'
}
const formatTime = (iso: string) => {
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

onMounted(load)
</script>
