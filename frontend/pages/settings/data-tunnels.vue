<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900 dark:text-white">
      {{ $t('settings.dataTunnels.title') }}
      <p class="text-sm text-gray-500 dark:text-gray-400 font-normal mb-6">
        {{ $t('settings.dataTunnels.subtitle') }}
      </p>
    </h2>
  </div>

  <!-- Two-pane layout (mirrors settings/integrations): sub-nav on the left,
       the selected section on the right. -->
  <div class="mt-2 flex gap-8 min-h-[26rem]">
    <!-- Left: section sub-nav -->
    <nav class="w-64 shrink-0 space-y-0.5">
      <button
        v-for="item in sections"
        :key="item.key"
        type="button"
        class="group w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-start transition-colors"
        :class="selectedKey === item.key ? 'bg-gray-100 dark:bg-gray-800' : 'hover:bg-gray-50 dark:hover:bg-gray-800'"
        @click="selectedKey = item.key"
      >
        <span class="w-6 h-6 shrink-0 flex items-center justify-center">
          <UIcon :name="item.icon" class="w-5 h-5 text-gray-500 dark:text-gray-400" />
        </span>
        <span
          class="flex-1 min-w-0 truncate text-sm"
          :class="selectedKey === item.key ? 'font-medium text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-400'"
        >
          {{ item.name }}
        </span>
      </button>
    </nav>

    <!-- Right: selected section -->
    <div class="flex-1 min-w-0 border-l border-gray-100 dark:border-gray-800 pl-8">

      <!-- ── Section 1: Connect a new edge agent ─────────────────────────── -->
      <div v-if="selectedKey === 'connect'" class="max-w-2xl">
        <h3 class="text-[15px] font-medium text-gray-900 dark:text-white">
          {{ $t('settings.dataTunnels.connect.title') }}
        </h3>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {{ $t('settings.dataTunnels.connect.subtitle') }}
        </p>

        <!-- Step 1: identity -->
        <div class="mt-6">
          <div class="text-xs font-semibold text-gray-700 dark:text-gray-300 mb-3">
            {{ $t('settings.dataTunnels.connect.step1') }}
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <UFormGroup :label="$t('settings.dataTunnels.connect.agentId')" :error="idError">
              <UInput v-model="agentId" placeholder="nyc-01" autocomplete="off" />
              <template #hint />
            </UFormGroup>
            <UFormGroup :label="$t('settings.dataTunnels.connect.agentName')">
              <UInput v-model="agentName" placeholder="NYC Office" autocomplete="off" />
            </UFormGroup>
          </div>
          <p class="mt-2 text-[11px] text-gray-400">{{ $t('settings.dataTunnels.connect.agentIdHint') }}</p>
        </div>

        <!-- Step 2: config.yaml -->
        <div class="mt-6">
          <div class="flex items-center justify-between mb-2">
            <div class="text-xs font-semibold text-gray-700 dark:text-gray-300">
              {{ $t('settings.dataTunnels.connect.step2') }}
            </div>
            <UButton
              size="2xs" color="gray" variant="ghost"
              icon="i-heroicons-clipboard-document"
              @click="copy(configYaml)"
            >{{ $t('settings.dataTunnels.connect.copy') }}</UButton>
          </div>
          <p class="text-[11px] text-gray-400 mb-2">{{ $t('settings.dataTunnels.connect.configHint') }}</p>
          <pre class="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-3 text-[12px] leading-relaxed font-mono text-gray-800 dark:text-gray-200 overflow-x-auto whitespace-pre">{{ configYaml }}</pre>
        </div>

        <!-- Step 3: docker run -->
        <div class="mt-6">
          <div class="flex items-center justify-between mb-2">
            <div class="text-xs font-semibold text-gray-700 dark:text-gray-300">
              {{ $t('settings.dataTunnels.connect.step3') }}
            </div>
            <UButton
              size="2xs" color="gray" variant="ghost"
              icon="i-heroicons-clipboard-document"
              @click="copy(dockerCmd)"
            >{{ $t('settings.dataTunnels.connect.copy') }}</UButton>
          </div>
          <p class="text-[11px] text-gray-400 mb-2">{{ $t('settings.dataTunnels.connect.dockerHint') }}</p>
          <pre class="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-900 dark:bg-black p-3 text-[12px] leading-relaxed font-mono text-gray-100 overflow-x-auto whitespace-pre">{{ dockerCmd }}</pre>
          <p class="mt-2 text-[11px] text-gray-400">{{ $t('settings.dataTunnels.connect.dockerNote') }}</p>
        </div>

        <div class="mt-6 flex items-start gap-2 text-[11px] text-gray-400">
          <UIcon name="i-heroicons-information-circle" class="w-3.5 h-3.5 mt-0.5 shrink-0" />
          <span>{{ $t('settings.dataTunnels.connect.waiting') }}</span>
        </div>
      </div>

      <!-- ── Section 2: Registered edge agents (the original view) ────────── -->
      <div v-else-if="selectedKey === 'agents'">
        <div class="mb-4">
          <h3 class="text-[15px] font-medium text-gray-900 dark:text-white">{{ $t('settings.dataTunnels.agentsTitle') }}</h3>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.dataTunnels.agentsSubtitle') }}</p>
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
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'

definePageMeta({
  auth: true,
  permissions: ['manage_settings'],
  layout: 'settings'
})

const { t } = useI18n()
const toast = useToast()
const { organization, ensureOrganization } = useOrganization()

const sections = computed(() => [
  { key: 'connect', name: t('settings.dataTunnels.sectionConnect'), icon: 'i-heroicons-plus-circle' },
  { key: 'agents', name: t('settings.dataTunnels.sectionAgents'), icon: 'i-heroicons-server-stack' },
])
const selectedKey = ref<'connect' | 'agents'>('connect')

// ── Connect: config generator ────────────────────────────────────────────
const agentId = ref('')
const agentName = ref('')
const orgId = ref('')

// Edge agent id becomes a NATS subject token — no dots, spaces, '*' or '>'.
const ID_RE = /^[A-Za-z0-9_-]+$/
const idError = computed(() =>
  agentId.value && !ID_RE.test(agentId.value)
    ? t('settings.dataTunnels.connect.agentIdError')
    : ''
)

const configYaml = computed(() => {
  const id = agentId.value || '<edge-agent-id>'
  const name = agentName.value || '<edge-agent-name>'
  const org = orgId.value || '<organization-id>'
  return [
    '# Bow Data Edge Agent — config.yaml',
    '# org_id is your unique and system generated identifier, do not change it.',
    `org_id: ${org}`,
    `edge_agent_id: ${id}`,
    // quote the name: it can contain spaces
    `edge_agent_name: ${JSON.stringify(name)}`,
    '',
    '# Tunnel endpoint URL',
    'tunnel_endpoint_url: wss://tunnel.example.com',
  ].join('\n')
})

const dockerCmd = computed(() => [
  'docker run -d --name bow-data-edge-agent \\',
  '  --restart unless-stopped \\',
  '  -v "$(pwd)/config.yaml:/etc/bow/edge-agent.yaml:ro" \\',
  '  -v bow-edge-data:/data \\',
  '  -e BOW_EDGE_AGENT_TUNNEL_TOKEN=<your-tunnel-token> \\',
  '  -p 127.0.0.1:9191:9191 \\',
  '  bow/data-edge-agent:latest',
].join('\n'))

async function copy(text: string) {
  if (!text) return
  let ok = false
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
      ok = true
    }
  } catch { /* fall through */ }
  if (!ok) {
    // Fallback for plain-http origins.
    try {
      const el = document.createElement('textarea')
      el.value = text
      el.style.position = 'fixed'
      el.style.opacity = '0'
      document.body.appendChild(el)
      el.select()
      ok = document.execCommand('copy')
      document.body.removeChild(el)
    } catch { ok = false }
  }
  toast.add(ok
    ? { title: t('settings.dataTunnels.connect.copied'), icon: 'i-heroicons-check-circle', color: 'green' }
    : { title: t('settings.dataTunnels.connect.copyManual'), color: 'orange' })
}

// ── Registered agents ────────────────────────────────────────────────────
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

onMounted(async () => {
  await ensureOrganization()
  orgId.value = organization.value?.id || ''
  load()
})
</script>
