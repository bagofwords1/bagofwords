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

      <!-- ── Section 1: Connect a new edge agent (wizard) ─────────────────── -->
      <div v-if="selectedKey === 'connect'" class="max-w-2xl">
        <h3 class="text-[15px] font-medium text-gray-900 dark:text-white">
          {{ $t('settings.dataTunnels.connect.title') }}
        </h3>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
          {{ $t('settings.dataTunnels.connect.subtitle') }}
        </p>

        <!-- Provisioning unavailable notice -->
        <div
          v-if="caps && !caps.enabled"
          class="mt-6 rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-3 text-xs text-amber-800 dark:text-amber-300 flex items-start gap-2"
        >
          <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 mt-0.5 shrink-0" />
          <span>{{ $t('settings.dataTunnels.wizard.provisioningDisabled') }}</span>
        </div>

        <template v-else>
          <!-- Stepper header -->
          <ol class="mt-6 flex items-center gap-2">
            <li v-for="(s, i) in stepDefs" :key="s.key" class="flex items-center gap-2">
              <span class="flex items-center gap-2">
                <span
                  class="w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-medium border"
                  :class="stepCircleClass(s.key)"
                >
                  <UIcon v-if="isStepDone(s.key)" name="i-heroicons-check" class="w-3.5 h-3.5" />
                  <template v-else>{{ i + 1 }}</template>
                </span>
                <span
                  class="text-xs"
                  :class="step === s.key ? 'font-medium text-gray-900 dark:text-white' : 'text-gray-500 dark:text-gray-400'"
                >{{ s.label }}</span>
              </span>
              <span v-if="i < stepDefs.length - 1" class="w-8 h-px bg-gray-200 dark:bg-gray-700" />
            </li>
          </ol>

          <!-- Step 1: identity -->
          <div v-if="step === 'identity'" class="mt-6">
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <UFormGroup :label="$t('settings.dataTunnels.connect.agentId')" :error="idError">
                <UInput v-model="agentId" placeholder="nyc-01" autocomplete="off" :disabled="creating" />
              </UFormGroup>
              <UFormGroup :label="$t('settings.dataTunnels.connect.agentName')">
                <UInput v-model="agentName" placeholder="NYC Office" autocomplete="off" :disabled="creating" />
              </UFormGroup>
            </div>
            <p class="mt-2 text-[11px] text-gray-400">{{ $t('settings.dataTunnels.connect.agentIdHint') }}</p>
            <div v-if="createError" class="mt-3 rounded border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-2 text-xs text-red-700 dark:text-red-300">
              {{ createError }}
            </div>
            <div class="mt-5">
              <UButton color="blue" size="sm" :loading="creating" :disabled="!canCreate" @click="createAgent">
                {{ $t('settings.dataTunnels.wizard.createBtn') }}
              </UButton>
            </div>
          </div>

          <!-- Step 2: certificate -->
          <div v-else-if="step === 'certificate'" class="mt-6">
            <div class="rounded-lg border border-gray-200 dark:border-gray-700 p-4">
              <div v-if="!certReady" class="flex items-center gap-3">
                <svg class="animate-spin w-5 h-5 text-blue-500" viewBox="0 0 24 24" fill="none">
                  <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                  <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                <div>
                  <div class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.dataTunnels.wizard.certGenerating') }}</div>
                  <div class="text-[11px] text-gray-400">{{ $t('settings.dataTunnels.wizard.certWaitNote') }}</div>
                </div>
              </div>
              <div v-else class="flex items-center gap-3">
                <span class="w-8 h-8 rounded-full bg-green-100 dark:bg-green-900/30 flex items-center justify-center">
                  <UIcon name="i-heroicons-check" class="w-5 h-5 text-green-600 dark:text-green-400" />
                </span>
                <div>
                  <div class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.dataTunnels.wizard.certReady') }}</div>
                  <div class="text-[11px] text-gray-400">{{ certReason }}</div>
                </div>
              </div>
            </div>
            <div v-if="certError" class="mt-3 rounded border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-2 text-xs text-red-700 dark:text-red-300">
              {{ certError }}
            </div>
            <div class="mt-5">
              <UButton color="blue" size="sm" :disabled="!certReady" @click="step = 'configure'">
                {{ $t('settings.dataTunnels.wizard.nextBtn') }}
              </UButton>
            </div>
          </div>

          <!-- Step 3: configure -->
          <div v-else-if="step === 'configure'" class="mt-6">
            <!-- Download -->
            <div class="rounded-lg border border-gray-200 dark:border-gray-700 p-4">
              <div class="flex items-center justify-between">
                <div>
                  <div class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.dataTunnels.wizard.downloadTitle') }}</div>
                  <div class="text-[11px] text-gray-400 mt-0.5">{{ $t('settings.dataTunnels.wizard.downloadNote') }}</div>
                </div>
                <UButton color="blue" size="sm" icon="i-heroicons-arrow-down-tray" :loading="downloading" @click="downloadBundle">
                  {{ $t('settings.dataTunnels.wizard.downloadBtn') }}
                </UButton>
              </div>
              <div v-if="downloadError" class="mt-3 rounded border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-2 text-xs text-red-700 dark:text-red-300">
                {{ downloadError }}
              </div>
            </div>

            <!-- Placement instructions -->
            <div class="mt-5">
              <div class="text-xs font-semibold text-gray-700 dark:text-gray-300 mb-2">{{ $t('settings.dataTunnels.wizard.placeTitle') }}</div>
              <ul class="text-xs text-gray-600 dark:text-gray-400 space-y-1 list-disc pl-5">
                <li>{{ $t('settings.dataTunnels.wizard.placeStep1') }}</li>
                <li>{{ $t('settings.dataTunnels.wizard.placeStep2') }}</li>
                <li>{{ $t('settings.dataTunnels.wizard.placeStep3') }}</li>
              </ul>
            </div>

            <!-- Config preview -->
            <div class="mt-5">
              <div class="flex items-center justify-between mb-2">
                <div class="text-xs font-semibold text-gray-700 dark:text-gray-300">{{ $t('settings.dataTunnels.wizard.configTitle') }}</div>
                <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-clipboard-document" @click="copy(configYaml)">
                  {{ $t('settings.dataTunnels.connect.copy') }}
                </UButton>
              </div>
              <pre class="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-3 text-[12px] leading-relaxed font-mono text-gray-800 dark:text-gray-200 overflow-x-auto whitespace-pre">{{ configYaml }}</pre>
            </div>

            <!-- Run command -->
            <div class="mt-5">
              <div class="flex items-center justify-between mb-2">
                <div class="text-xs font-semibold text-gray-700 dark:text-gray-300">{{ $t('settings.dataTunnels.wizard.runTitle') }}</div>
                <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-clipboard-document" @click="copy(runCmd)">
                  {{ $t('settings.dataTunnels.connect.copy') }}
                </UButton>
              </div>
              <pre class="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-900 dark:bg-black p-3 text-[12px] leading-relaxed font-mono text-gray-100 overflow-x-auto whitespace-pre">{{ runCmd }}</pre>
            </div>

            <div class="mt-6 flex items-center gap-2">
              <UButton color="blue" size="sm" @click="finishWizard">{{ $t('settings.dataTunnels.wizard.finishBtn') }}</UButton>
              <span class="text-[11px] text-gray-400">{{ $t('settings.dataTunnels.connect.waiting') }}</span>
            </div>
          </div>
        </template>
      </div>

      <!-- ── Section 2: Registered edge agents ────────────────────────────── -->
      <div v-else-if="selectedKey === 'agents'">
        <div class="mb-4">
          <h3 class="text-[15px] font-medium text-gray-900 dark:text-white">{{ $t('settings.dataTunnels.agentsTitle') }}</h3>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.dataTunnels.agentsSubtitle') }}</p>
        </div>

        <div v-if="loading" class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 py-6">
          <svg class="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
          {{ $t('settings.dataTunnels.loading') }}
        </div>

        <div v-else-if="error" class="rounded border border-red-200 dark:border-red-800 p-3 bg-red-50 dark:bg-red-900/20 text-xs text-red-700 dark:text-red-300">
          {{ error }}
        </div>

        <div v-else-if="agents.length === 0" class="rounded border border-gray-200 dark:border-gray-700 p-6 text-center">
          <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.dataTunnels.empty') }}</p>
        </div>

        <div v-else class="space-y-4">
          <div
            v-for="agent in agents"
            :key="agent.id"
            class="border border-gray-200 dark:border-gray-700 rounded overflow-hidden"
          >
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
                  :class="isOnline(agent.status) ? 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-400' : 'bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400'"
                >
                  <span class="w-1.5 h-1.5 rounded-full" :class="isOnline(agent.status) ? 'bg-green-500' : 'bg-gray-400'" />
                  {{ isOnline(agent.status) ? $t('settings.dataTunnels.online') : $t('settings.dataTunnels.offline') }}
                </span>
                <span v-if="agent.last_advertised_at" class="text-[11px] text-gray-400">
                  {{ $t('settings.dataTunnels.lastSeen') }} {{ formatTime(agent.last_advertised_at) }}
                </span>
                <UButton
                  size="2xs" color="red" variant="ghost" icon="i-heroicons-trash"
                  :loading="removingId === agent.id"
                  @click="askRemove(agent)"
                >{{ $t('settings.dataTunnels.wizard.removeBtn') }}</UButton>
              </div>
            </div>

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

  <!-- Remove confirmation -->
  <UModal v-model="removeModalOpen" :ui="{ width: 'sm:max-w-md' }">
    <div class="p-5">
      <div class="flex items-start gap-3">
        <span class="w-9 h-9 rounded-full bg-red-100 dark:bg-red-900/30 flex items-center justify-center shrink-0">
          <UIcon name="i-heroicons-trash" class="w-5 h-5 text-red-600 dark:text-red-400" />
        </span>
        <div class="min-w-0">
          <h3 class="text-sm font-medium text-gray-900 dark:text-white">
            {{ $t('settings.dataTunnels.wizard.removeTitle') }}
          </h3>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {{ $t('settings.dataTunnels.wizard.removeConfirm', { id: agentToRemove?.edge_agent_id }) }}
          </p>
        </div>
      </div>
      <div class="mt-5 flex justify-end gap-2">
        <UButton color="gray" variant="ghost" size="sm" @click="agentToRemove = null">
          {{ $t('settings.dataTunnels.wizard.cancelBtn') }}
        </UButton>
        <UButton color="red" size="sm" :loading="removingId === agentToRemove?.id" @click="confirmRemove">
          {{ $t('settings.dataTunnels.wizard.removeBtn') }}
        </UButton>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'

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

// ── Provisioning capabilities ────────────────────────────────────────────
interface Caps { enabled: boolean; agent_url: string | null }
const caps = ref<Caps | null>(null)

const loadCaps = async () => {
  try {
    const res = await useMyFetch('/api/data-tunnels/provisioning')
    if (res.status.value === 'success') caps.value = res.data.value as Caps
    else caps.value = { enabled: false, agent_url: null }
  } catch { caps.value = { enabled: false, agent_url: null } }
}

// ── Wizard ────────────────────────────────────────────────────────────────
type Step = 'identity' | 'certificate' | 'configure'
const step = ref<Step>('identity')
const stepDefs = computed(() => [
  { key: 'identity' as Step, label: t('settings.dataTunnels.wizard.step1Title') },
  { key: 'certificate' as Step, label: t('settings.dataTunnels.wizard.step2Title') },
  { key: 'configure' as Step, label: t('settings.dataTunnels.wizard.step3Title') },
])
const stepOrder: Step[] = ['identity', 'certificate', 'configure']
const isStepDone = (k: Step) => stepOrder.indexOf(k) < stepOrder.indexOf(step.value)
const stepCircleClass = (k: Step) => {
  if (isStepDone(k)) return 'bg-blue-500 border-blue-500 text-white'
  if (step.value === k) return 'border-blue-500 text-blue-600 dark:text-blue-400'
  return 'border-gray-300 dark:border-gray-600 text-gray-400'
}

const agentId = ref('')
const agentName = ref('')
const orgId = ref('')

// Edge agent id: DNS-1123-ish label (matches backend validation).
const ID_RE = /^[a-z0-9]([a-z0-9-]{0,25}[a-z0-9])?$/
const idError = computed(() =>
  agentId.value && !ID_RE.test(agentId.value)
    ? t('settings.dataTunnels.connect.agentIdError')
    : ''
)
const canCreate = computed(() => !!agentId.value && !idError.value && !creating.value)

const creating = ref(false)
const createError = ref('')
const createdAgent = ref<DataEdgeAgent | null>(null)

const createAgent = async () => {
  createError.value = ''
  creating.value = true
  try {
    const res = await useMyFetch('/api/data-tunnels/agents', {
      method: 'POST',
      body: { edge_agent_id: agentId.value, label: agentName.value || null },
    })
    if (res.status.value !== 'success') {
      const detail = (res.error?.value as any)?.data?.detail
      throw new Error(detail || t('settings.dataTunnels.wizard.createError'))
    }
    createdAgent.value = res.data.value as DataEdgeAgent
    step.value = 'certificate'
    startCertPolling()
  } catch (e: any) {
    createError.value = e?.message || t('settings.dataTunnels.wizard.createError')
  } finally {
    creating.value = false
  }
}

// ── Certificate polling ──────────────────────────────────────────────────
const certReady = ref(false)
const certReason = ref('')
const certError = ref('')
let certTimer: any = null

const stopCertPolling = () => { if (certTimer) { clearTimeout(certTimer); certTimer = null } }

const pollCert = async () => {
  if (!createdAgent.value) return
  try {
    const res = await useMyFetch(`/api/data-tunnels/agents/${createdAgent.value.id}/certificate`)
    if (res.status.value === 'success') {
      const d = res.data.value as { ready: boolean; reason?: string }
      certReady.value = !!d.ready
      certReason.value = d.reason || ''
      if (certReady.value) { stopCertPolling(); return }
    }
  } catch (e: any) {
    certError.value = e?.message || ''
  }
  certTimer = setTimeout(pollCert, 2000)
}
const startCertPolling = () => {
  certReady.value = false; certReason.value = ''; certError.value = ''
  stopCertPolling()
  pollCert()
}

// ── Config preview (mirrors the backend-generated config.yaml) ────────────
const configYaml = computed(() => {
  const id = createdAgent.value?.edge_agent_id || agentId.value || '<edge-agent-id>'
  const name = createdAgent.value?.label || agentName.value || id
  const org = orgId.value || '<organization-id>'
  const url = caps.value?.agent_url || 'wss://<your-bow-host>:443'
  return [
    `# Bow Data Edge Agent — generated for '${id}'.`,
    '# Keep this file and the certs/ directory together; run the agent from here.',
    `org_id: ${org}`,
    `edge_agent_id: ${id}`,
    `edge_agent_name: ${JSON.stringify(name)}`,
    `tunnel_endpoint_url: ${url}`,
    '',
    '# mTLS to the broker (paths relative to this file).',
    'tls_ca: certs/ca.pem',
    'tls_cert: certs/client.pem',
    'tls_key: certs/client-key.pem',
    'tls_verify: true',
    '',
    '# Add the on-prem data sources this agent serves.',
    'connections: []',
  ].join('\n')
})

const runCmd = computed(() => [
  '# Extract the downloaded bundle, then from that directory:',
  'docker run -d --name bow-data-edge-agent --restart unless-stopped \\',
  '  -v "$(pwd)/config.yaml:/etc/bow/config.yaml:ro" \\',
  '  -v "$(pwd)/certs:/etc/bow/certs:ro" \\',
  '  -w /etc/bow \\',
  '  -e BOW_EDGE_AGENT_CONFIG=/etc/bow/config.yaml \\',
  '  -v bow-edge-data:/data \\',
  '  -p 127.0.0.1:9191:9191 \\',
  '  bow/data-edge-agent:latest',
].join('\n'))

// ── Bundle download ───────────────────────────────────────────────────────
const downloading = ref(false)
const downloadError = ref('')

const downloadBundle = async () => {
  if (!createdAgent.value) return
  downloadError.value = ''
  downloading.value = true
  try {
    const { data, error } = await useMyFetch<Blob>(
      `/api/data-tunnels/agents/${createdAgent.value.id}/bundle`,
      { method: 'GET', responseType: 'blob' as any }
    )
    if (error?.value) {
      let detail = t('settings.dataTunnels.wizard.downloadError')
      try { detail = JSON.parse(await (error.value as any).data.text()).detail || detail } catch { /* ignore */ }
      throw new Error(detail)
    }
    const url = URL.createObjectURL(data.value as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `bow-edge-agent-${createdAgent.value.edge_agent_id}.zip`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  } catch (e: any) {
    downloadError.value = e?.message || t('settings.dataTunnels.wizard.downloadError')
  } finally {
    downloading.value = false
  }
}

const resetWizard = () => {
  stopCertPolling()
  step.value = 'identity'
  agentId.value = ''
  agentName.value = ''
  createdAgent.value = null
  createError.value = ''
  certReady.value = false
  downloadError.value = ''
}

const finishWizard = () => {
  resetWizard()
  selectedKey.value = 'agents'
  load()
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
const removingId = ref<string | null>(null)

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

const agentToRemove = ref<DataEdgeAgent | null>(null)
const removeModalOpen = computed({
  get: () => agentToRemove.value !== null,
  set: (v: boolean) => { if (!v) agentToRemove.value = null },
})
const askRemove = (agent: DataEdgeAgent) => { agentToRemove.value = agent }

const confirmRemove = async () => {
  const agent = agentToRemove.value
  if (!agent) return
  removingId.value = agent.id
  try {
    const res = await useMyFetch(`/api/data-tunnels/agents/${agent.id}`, { method: 'DELETE' })
    if (res.status.value !== 'success') {
      const detail = (res.error?.value as any)?.data?.detail
      throw new Error(detail || t('settings.dataTunnels.wizard.removeError'))
    }
    agents.value = agents.value.filter(a => a.id !== agent.id)
    toast.add({ title: t('settings.dataTunnels.wizard.removed'), icon: 'i-heroicons-check-circle', color: 'green' })
    agentToRemove.value = null
  } catch (e: any) {
    toast.add({ title: e?.message || t('settings.dataTunnels.wizard.removeError'), color: 'red' })
  } finally {
    removingId.value = null
  }
}

const isOnline = (status?: string | null) => status === 'online'
const formatTime = (iso: string) => {
  try { return new Date(iso).toLocaleString() } catch { return iso }
}

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

onMounted(async () => {
  await ensureOrganization()
  orgId.value = organization.value?.id || ''
  loadCaps()
  load()
})
onUnmounted(stopCertPolling)
</script>
