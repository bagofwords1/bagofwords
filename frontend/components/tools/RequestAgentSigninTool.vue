<template>
  <div class="min-w-0" data-testid="request-agent-signin">
    <!-- Running: the ids are being checked; nothing to act on yet. -->
    <ToolCallHeader
      v-if="status === 'running' || !agents.length"
      :tool-execution="toolExecution"
      icon="heroicons-key"
      :expandable="false"
    >
      <template #label>{{ llmTitle || t('tools.requestAgentSignin.checking') }}</template>
    </ToolCallHeader>

    <!-- One row per agent the answer needs: icon, name, why, Connect. -->
    <div
      v-else
      class="mt-1 max-w-xl rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 divide-y divide-gray-100 dark:divide-gray-800"
    >
      <div
        v-for="a in agents"
        :key="a.id"
        class="flex items-center gap-3 px-3 py-2.5"
        :data-testid="`signin-agent-${a.id}`"
      >
        <DataSourceIcon
          :type="a.type"
          :connector-key="a.connector_key"
          :icon="a.icon"
          class="w-5 h-5 flex-shrink-0"
        />
        <div class="min-w-0 flex-1">
          <div class="text-chat-2 font-medium text-gray-800 dark:text-gray-100 truncate">{{ a.name }}</div>
          <div v-if="a.description" class="text-chat-3 text-ink-muted truncate">{{ a.description }}</div>
        </div>
        <span
          v-if="connected[a.id]"
          class="inline-flex items-center gap-1 text-chat-2 font-medium text-green-700 dark:text-green-400 flex-shrink-0"
          data-testid="signin-connected"
        >
          <Icon name="heroicons-check-circle" class="w-4 h-4" />
          {{ t('tools.requestAgentSignin.connected') }}
        </span>
        <button
          v-else
          type="button"
          :disabled="connectingId === a.id"
          class="inline-flex items-center gap-1.5 px-3 py-1 text-chat-2 font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:opacity-60 rounded-md transition-colors flex-shrink-0"
          data-testid="signin-connect"
          @click="connect(a)"
        >
          <Spinner v-if="connectingId === a.id" class="w-3 h-3 animate-spin" />
          <Icon v-else name="heroicons-key" class="w-3.5 h-3.5" />
          {{ t('tools.requestAgentSignin.connect') }}
        </button>
      </div>
      <!-- Once every agent is connected the same question can run again. -->
      <div v-if="allConnected" class="flex items-center justify-between gap-3 px-3 py-2 bg-gray-50 dark:bg-gray-800/50 rounded-b-lg">
        <span class="text-chat-3 text-ink-muted">{{ t('tools.requestAgentSignin.readyHint') }}</span>
        <button
          type="button"
          class="inline-flex items-center gap-1.5 px-3 py-1 text-chat-2 font-medium text-blue-700 dark:text-blue-300 bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 hover:bg-blue-100 dark:hover:bg-blue-900/50 rounded-md transition-colors flex-shrink-0"
          data-testid="signin-ask-again"
          @click="askAgain"
        >
          <Icon name="heroicons-arrow-path" class="w-3.5 h-3.5" />
          {{ t('tools.requestAgentSignin.askAgain') }}
        </button>
      </div>
    </div>

    <UserDataSourceCredentialsModal
      v-model="showCredsModal"
      :data-source="selectedConnectDs"
      @saved="onCredsSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import Spinner from '~/components/Spinner.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import { useResponseI18n } from '~/composables/useResponseI18n'
import { useDataSourceConnect } from '~/composables/useDataSourceConnect'

const { t } = useResponseI18n()

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution, systemCompletionId?: string }>()

const status = computed<string>(() => props.toolExecution?.status || '')
const llmTitle = computed<string>(() => {
  const v = props.toolExecution?.arguments_json?.title
  return typeof v === 'string' ? v.trim() : ''
})
const agents = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.agents) ? rj.agents : []
})

// Live sign-in state per agent. The tool result is a snapshot from when the
// agent asked; the user may have connected since (another tab, an OAuth
// round-trip that reloaded this page), so it is re-read from the server.
const connected = ref<Record<string, boolean>>({})
const allConnected = computed(() => agents.value.length > 0 && agents.value.every(a => connected.value[a.id]))

const { connectingId, needsUserConnection, startConnect, asCredentialsModalSource } = useDataSourceConnect()
const showCredsModal = ref(false)
const selectedConnectDs = ref<any>(null)

async function loadAgent(id: string): Promise<any | null> {
  try {
    const { data } = await useMyFetch(`/data_sources/${id}`, { method: 'GET' })
    return (data as any)?.value || null
  } catch {
    return null
  }
}

async function refreshStatus() {
  await Promise.all(agents.value.map(async (a) => {
    const ds = await loadAgent(a.id)
    if (ds) connected.value = { ...connected.value, [a.id]: !needsUserConnection(ds) }
  }))
}

async function connect(a: any) {
  if (connectingId.value) return
  const ds = await loadAgent(a.id)
  if (!ds) return
  if (!needsUserConnection(ds)) {
    connected.value = { ...connected.value, [a.id]: true }
    return
  }
  // OAuth-only connections redirect to the provider (returns false); the
  // rest fall back to the credentials modal.
  const openModal = await startConnect(ds)
  if (!openModal) return
  selectedConnectDs.value = asCredentialsModalSource(ds)
  showCredsModal.value = true
}

async function onCredsSaved() {
  showCredsModal.value = false
  await refreshStatus()
}

// Re-send the question that led here, now that the agent can be used. The
// report page owns the prompt box and the rerun gates; it resolves which user
// message this completion answered.
function askAgain() {
  window.dispatchEvent(new CustomEvent('prompt:rerun-for-completion', {
    detail: { completionId: props.systemCompletionId },
  }))
}

onMounted(() => { if (agents.value.length) refreshStatus() })
watch(() => agents.value.map(a => a.id).join(','), (ids, prev) => { if (ids && ids !== prev) refreshStatus() })
</script>
