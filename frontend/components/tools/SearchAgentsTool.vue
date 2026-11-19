<template>
  <div class="min-w-0">
    <!-- Status header (click to expand/collapse results) -->
    <ToolCallHeader :tool-execution="toolExecution" icon="heroicons-magnifying-glass" :expanded="toolHeaderExpanded" @toggle="toolHeaderExpanded = !toolHeaderExpanded">
      <template #label>
      <template v-if="status === 'running'">{{ llmTitle || (queryLabel ? `Searching agents for ${queryLabel}` : 'Searching agents') }}</template>
      <template v-else>{{ llmTitle || (total === 1 ? 'Found 1 agent' : `Found ${total} agents`) }}</template></template>
    </ToolCallHeader>
    <div v-show="toolHeaderExpanded" class="tool-call-details w-full min-w-0 mt-2">
      <div class="mb-2 flex flex-wrap items-center gap-2 text-chat-3 text-ink-muted">
      <template v-if="status === 'running'"></template>
      <template v-else><span v-if="llmTitle" class="ms-1.5 text-chat-3 text-ink-muted flex-shrink-0">· {{ total === 1 ? '1 agent' : `${total} agents` }}</span><span v-if="agents.length" class="ms-2 flex items-center gap-0.5 min-w-0 overflow-hidden"><DataSourceIcon
              v-for="a in agents.slice(0, 8)"
              :key="a.id"
              :type="a.type"
              :connector-key="a.connector_key"
              :icon-token="a.icon_token" :icon="a.icon"
              class="w-3.5 h-3.5 flex-shrink-0"
            /><span v-if="agents.length > 8" class="ms-0.5 text-chat-3 text-ink-muted flex-shrink-0">+{{ agents.length - 8 }}</span></span></template></div>

    <!-- Results list (collapsed by default; click header to toggle) -->
    <Transition name="fade">
      <div v-if="toolHeaderExpanded && agents.length" class="text-chat-2 text-gray-600 dark:text-gray-400">
        <ul class="ms-1 space-y-0.5 leading-snug">
          <li
            v-for="(a, idx) in agents"
            :key="a.id || idx"
            class="flex items-center py-1 px-1 rounded"
          >
            <DataSourceIcon
              :type="a.type"
              :connector-key="a.connector_key"
              :icon-token="a.icon_token" :icon="a.icon"
              class="w-3.5 h-3.5 me-1.5 flex-shrink-0"
            />
            <span class="font-medium text-gray-700 dark:text-gray-300 truncate">{{ a.name }}</span>
            <span v-if="a.description" class="ms-1.5 text-gray-500 dark:text-gray-400 truncate hidden sm:inline">
              — {{ a.description }}
            </span>
            <span
              v-if="a.focused"
              class="ms-1.5 text-chat-3 px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-300 flex-shrink-0"
            >focused</span>
            <!-- Lifecycle stage (Development / Training / Disabled), same as
                 the agent selector — not the raw publish_status ("draft"). -->
            <span
              v-else-if="stageOf(a) !== 'production'"
              :class="['ms-1.5 text-chat-3 px-1 py-0.5 rounded border flex-shrink-0', stageMeta(stageOf(a)).badge]"
            >{{ t('agentsPage.stage.' + stageOf(a)) }}</span>
            <button
              v-if="a.needs_signin"
              @click.stop="signInAgent(a)"
              :disabled="signingInId === a.id"
              class="ms-2 inline-flex items-center gap-1 px-2 py-0.5 text-chat-2 font-medium text-blue-600 dark:text-blue-300 bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 rounded-md hover:bg-blue-100 transition-colors flex-shrink-0"
            >
              <Spinner v-if="signingInId === a.id" class="w-2.5 h-2.5 animate-spin" />
              <Icon v-else name="heroicons-key" class="w-2.5 h-2.5" />
              Connect
            </button>
          </li>
        </ul>
      </div>
    </Transition>

    <!-- Empty state -->
    <div v-if="status !== 'running' && !agents.length" class="text-chat-2 text-ink-muted ms-1">
      No agents matched.
    </div>

    <!-- Credentials modal fallback (OAuth-only connections redirect instead) -->
    <UserDataSourceCredentialsModal
      v-model="showCredsModal"
      :data-source="selectedConnectDs"
      @saved="showCredsModal = false"
    />
  
    </div>
  </div>
</template>

<script setup lang="ts">
import ToolCallHeader from '~/components/tools/ToolCallHeader.vue'
import { ref as activityRef } from 'vue'
const toolHeaderExpanded = activityRef(false)

const { t } = useResponseI18n()

import { computed, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import { deriveStage, stageMeta } from '~/composables/useDataSourcePublishStatus'

const isCollapsed = ref(true)
function toggleCollapsed() { isCollapsed.value = !isCollapsed.value }

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_summary?: string
  result_json?: any
  arguments_json?: any
}

const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')

const llmTitle = computed<string>(() => {
  const t = (props.toolExecution as any)?.arguments_json?.title
  return typeof t === 'string' ? t.trim() : ''
})

const queryLabel = computed<string>(() => {
  const q = (props.toolExecution as any)?.arguments_json?.query
  if (Array.isArray(q)) return q.filter(Boolean).map((s: string) => `"${s}"`).join(', ')
  if (typeof q === 'string' && q) return `"${q}"`
  return ''
})

const agents = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return Array.isArray(rj.agents) ? rj.agents : []
})

function stageOf(a: any) {
  return deriveStage(a.status, a.reliability_status)
}

const total = computed<number>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  return typeof rj.total === 'number' ? rj.total : agents.value.length
})

// Sign In for agents the user can access but hasn't connected (user_required
// auth, e.g. PowerBI OBO) — same flow as DataSourceSelector: OAuth-only
// connections redirect straight to the provider; otherwise the creds modal.
const signIn = useConnectionSignIn()
const signingInId = ref<string | null>(null)
const showCredsModal = ref(false)
const selectedConnectDs = ref<any>(null)

async function signInAgent(a: any) {
  if (signingInId.value) return
  signingInId.value = a.id
  try {
    const { data } = await useMyFetch(`/data_sources/${a.id}`, { method: 'GET' })
    const ds: any = (data as any)?.value
    if (!ds) return
    const pending = (ds.connections || []).find(
      (c: any) => c.auth_policy === 'user_required' && !c.user_status?.has_user_credentials
    )
    if (pending) {
      const result = await signIn.triggerUserSignIn(pending)
      if (result.redirecting) return // page is navigating to the provider
    }
    selectedConnectDs.value = ds
    showCredsModal.value = true
  } catch (e) {
    console.error('agent sign-in failed', e)
  } finally {
    signingInId.value = null
  }
}
</script>

<style scoped>
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.25s ease, transform 0.25s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
  transform: translateY(2px);
}
</style>
