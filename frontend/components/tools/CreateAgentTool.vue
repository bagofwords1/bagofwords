<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500 dark:text-gray-400">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-sparkles" class="w-3 h-3 me-1 text-gray-400" />
          <span>{{ $t('tools.createAgent.creating', { name: args.name || '' }) }}</span>
        </span>
        <span v-else-if="succeeded" class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-sparkles" class="w-3 h-3 me-1 text-emerald-500" />
          <span class="align-middle">{{ $t('tools.createAgent.created') }}</span>
        </span>
        <span v-else-if="needsSelection" class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-chat-bubble-left-right" class="w-3 h-3 me-1 text-blue-400" />
          <span class="align-middle">{{ $t('tools.createAgent.needsSelection') }}</span>
        </span>
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-amber-500" />
          <span class="align-middle">{{ $t('tools.createAgent.failed') }}</span>
        </span>
      </div>
    </Transition>

    <!-- Selection menu (needs_selection): compact chips, the clarify question follows. -->
    <div v-if="needsSelection && selectionGroups.length" class="ms-1 mb-1 flex items-center gap-1 flex-wrap">
      <span
        v-for="g in selectionGroups" :key="g.label"
        class="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400"
      >{{ g.label }} · {{ g.count }}</span>
    </div>

    <!-- Failure / rejection detail -->
    <div v-else-if="status !== 'running' && !succeeded && message" class="text-xs text-gray-500 dark:text-gray-400 ms-1 mb-1">
      {{ message }}
    </div>

    <!-- Agent card -->
    <Transition name="fade" appear>
      <div
        v-if="succeeded"
        class="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 max-w-2xl"
      >
        <!-- Card header: name, status, badges -->
        <div class="px-3 pt-2.5 pb-2">
          <div class="flex items-center gap-2">
            <Icon name="heroicons-cpu-chip" class="w-4 h-4 text-blue-500 flex-shrink-0" />
            <span class="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{{ agentName }}</span>
            <span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded-full flex-shrink-0"
              :class="agentActive ? 'bg-emerald-50 dark:bg-emerald-950 text-emerald-700 dark:text-emerald-400' : 'bg-gray-100 dark:bg-gray-800 text-gray-500'">
              <span class="w-1.5 h-1.5 rounded-full" :class="agentActive ? 'bg-emerald-500' : 'bg-gray-400'"></span>
              {{ agentActive ? $t('tools.createAgent.statusActive') : $t('tools.createAgent.statusInactive') }}
            </span>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 flex-shrink-0">
              {{ isPublic ? $t('tools.createAgent.public') : $t('tools.createAgent.private') }}
            </span>
            <span class="flex-1"></span>
            <NuxtLink
              :to="`/agents/${agentId}`"
              class="text-[11px] text-blue-600 hover:text-blue-800 dark:text-blue-400 inline-flex items-center gap-1 flex-shrink-0"
            >
              <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
              {{ $t('tools.createAgent.open') }}
            </NuxtLink>
          </div>
          <div v-if="description" class="mt-1 text-[11px] text-gray-500 dark:text-gray-400 leading-snug">{{ description }}</div>
          <div v-if="connections.length" class="mt-1 text-[10px] text-gray-400">
            {{ $t('tools.createAgent.on') }} {{ connections.join(', ') }}
          </div>
          <div v-if="requiresConnect" class="mt-1 text-[10px] text-amber-600 dark:text-amber-400 flex items-center gap-1">
            <Icon name="heroicons-key" class="w-3 h-3" />
            {{ $t('tools.createAgent.requiresConnect') }}
          </div>
          <div v-if="unresolved.length" class="mt-1 text-[10px] text-amber-600 dark:text-amber-400">
            {{ $t('tools.createAgent.unresolved', { items: unresolved.join(', ') }) }}
          </div>
        </div>

        <!-- Collapsed section chips — click expands, click again closes. -->
        <div class="px-3 pb-2 flex items-center gap-1.5 flex-wrap border-t border-gray-100 dark:border-gray-800 pt-2">
          <button
            v-for="section in visibleSections" :key="section"
            class="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded-md border transition-colors"
            :class="openSection === section
              ? 'border-blue-300 dark:border-blue-800 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300'
              : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'"
            @click="toggleSection(section)"
          >
            <Icon :name="sectionIcon(section)" class="w-3 h-3" />
            {{ $t(`tools.createAgent.tab.${section}`) }}
            <span class="text-[9px] opacity-70">{{ sectionCount(section) }}</span>
            <Icon :name="openSection === section ? 'heroicons-chevron-up' : 'heroicons-chevron-down'" class="w-2.5 h-2.5 opacity-60" />
          </button>
        </div>

        <!-- Expanded section body -->
        <div v-if="openSection" class="px-3 pb-2 border-t border-gray-100 dark:border-gray-800 pt-2">
          <!-- Tables — the real selector: server-side search, schema filter,
               sort, pagination; holds up on connections with thousands of tables. -->
          <div v-if="openSection === 'tables'" class="create-agent-selector">
            <TablesSelector
              :key="agentId"
              :ds-id="agentId"
              schema="full"
              :can-update="true"
              :show-refresh="false"
              :show-save="true"
              :show-stats="false"
              :page-size="15"
              max-height="280px"
            />
          </div>

          <!-- Tools — the real per-agent overlay editor. -->
          <div v-else-if="openSection === 'tools'" class="create-agent-selector">
            <ToolsSelector
              v-if="toolConns.length"
              :key="agentId"
              :ds-id="agentId"
              :connections="toolConns"
              :can-update="true"
              :show-header="false"
            />
            <div v-else class="text-[11px] text-gray-400 py-1">{{ $t('tools.createAgent.emptyTab') }}</div>
          </div>

          <!-- Files -->
          <div v-else-if="openSection === 'files'" class="max-h-64 overflow-y-auto">
            <ul v-if="fileNames.length" class="space-y-0.5 leading-snug text-[11px]">
              <li v-for="f in fileNames" :key="f" class="flex items-center py-0.5">
                <Icon name="heroicons-document" class="w-3 h-3 me-1 text-gray-400 flex-shrink-0" />
                <span class="text-gray-700 dark:text-gray-300 font-mono truncate">{{ f }}</span>
              </li>
            </ul>
            <div v-else class="text-[11px] text-gray-400 py-1">{{ $t('tools.createAgent.emptyTab') }}</div>
          </div>

          <!-- Instructions — compact agent-scoped list. -->
          <div v-else-if="openSection === 'instructions'" class="max-h-64 overflow-y-auto">
            <ul v-if="instructions.length" class="space-y-1 leading-snug text-[11px]">
              <li v-for="inst in instructions" :key="inst.id" class="flex items-start py-0.5 gap-1.5">
                <Icon name="heroicons-document-text" class="w-3 h-3 mt-0.5 text-indigo-400 flex-shrink-0" />
                <span class="text-gray-700 dark:text-gray-300">{{ instructionLabel(inst) }}</span>
                <span v-if="inst.status && inst.status !== 'published'"
                      class="text-[9px] px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 flex-shrink-0">
                  {{ inst.status }}
                </span>
                <span v-if="inst.load_mode === 'always'"
                      class="text-[9px] px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 flex-shrink-0">
                  {{ inst.load_mode }}
                </span>
              </li>
            </ul>
            <div v-else class="text-[11px] text-gray-400 py-1">{{ $t('tools.createAgent.emptyTab') }}</div>
          </div>

          <!-- Evals — the real per-agent evals panel. -->
          <div v-else-if="openSection === 'evals'" class="create-agent-selector max-h-96 overflow-y-auto">
            <AgentEvalsPanel :key="'evals-' + agentId" :agent-id="agentId" />
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import TablesSelector from '~/components/datasources/TablesSelector.vue'
import ToolsSelector from '~/components/datasources/ToolsSelector.vue'
import AgentEvalsPanel from '~/components/AgentEvalsPanel.vue'
import { useCan } from '~/composables/usePermissions'

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
const result = computed(() => props.toolExecution?.result_json || {})
const args = computed(() => props.toolExecution?.arguments_json || {})

const succeeded = computed<boolean>(() => status.value === 'success' && result.value?.success === true && !!result.value?.data_source_id)
const needsSelection = computed<boolean>(() => result.value?.rejected_reason === 'needs_selection')
const selectionGroups = computed<any[]>(() => Array.isArray(result.value?.selection_groups) ? result.value.selection_groups : [])
const message = computed<string>(() => result.value?.message || props.toolExecution?.result_summary || '')

const agentId = computed<string>(() => String(result.value?.data_source_id || ''))
const agentName = computed<string>(() => result.value?.name || args.value?.name || '')
const description = computed<string>(() => result.value?.description || args.value?.description || '')
const isPublic = computed<boolean>(() => !!result.value?.is_public)
const connections = computed<string[]>(() => Array.isArray(result.value?.connections) ? result.value.connections : [])
const unresolved = computed<string[]>(() => Array.isArray(result.value?.unresolved) ? result.value.unresolved : [])
const requiresConnect = computed<boolean>(() => !!result.value?.requires_user_connect)

const FILE_TYPES = ['network_dir', 's3', 'sharepoint', 'onedrive', 'google_drive', 'outlook_mail']
const TOOL_TYPES = ['mcp', 'custom_api']

// Live agent state
const agentActive = ref<boolean>(true)
const connRows = ref<any[]>([])
const fileNames = ref<string[]>([])
const instructions = ref<any[]>([])
const instructionsAllowed = ref(false)
const loaded = ref(false)

const toolConns = computed(() => connRows.value.filter((c: any) =>
  TOOL_TYPES.includes(c.type) || c.data_shape === 'tools'))
const fileConns = computed(() => connRows.value.filter((c: any) => FILE_TYPES.includes(c.type)))

// Permission gating (server still enforces on every fetch; these decide what
// to surface). `manage` on the new agent implies the section permissions.
const canSchema = computed(() => useCan('view_schema', { type: 'data_source', id: agentId.value }))
const canEvals = computed(() => useCan('manage_evals') || useCan('manage_evals', { type: 'data_source', id: agentId.value }))

const visibleSections = computed<string[]>(() => {
  const out: string[] = []
  if (((result.value?.tables_total || 0) > 0 || (!toolConns.value.length && !fileConns.value.length)) && canSchema.value) out.push('tables')
  if ((toolConns.value.length || (result.value?.tools_total || 0) > 0) && canSchema.value) out.push('tools')
  if ((fileConns.value.length || fileNames.value.length) && canSchema.value) out.push('files')
  if (instructionsAllowed.value) out.push('instructions')
  if (canEvals.value) out.push('evals')
  return out
})

// Collapsed by default — click a chip to expand, click it again to close.
const openSection = ref<string | null>(null)
function toggleSection(section: string) {
  openSection.value = openSection.value === section ? null : section
}

function sectionIcon(section: string): string {
  return {
    tables: 'heroicons-table-cells',
    tools: 'heroicons-wrench-screwdriver',
    files: 'heroicons-folder',
    instructions: 'heroicons-document-text',
    evals: 'heroicons-check-badge',
  }[section] || 'heroicons-squares-2x2'
}

function sectionCount(section: string): string {
  if (section === 'tables') {
    const active = result.value?.tables_active || 0
    const total = result.value?.tables_total || 0
    return total ? `${active}/${total}` : ''
  }
  if (section === 'tools') {
    const enabled = result.value?.tools_enabled || 0
    const total = result.value?.tools_total || 0
    return total ? `${enabled}/${total}` : ''
  }
  if (section === 'files') return fileNames.value.length ? String(fileNames.value.length) : ''
  if (section === 'instructions') return String(instructions.value.length)
  return ''
}

function instructionLabel(inst: any): string {
  const t = inst?.title || String(inst?.text || '').split('\n')[0]
  return t.length > 90 ? t.slice(0, 87) + '…' : t
}

async function loadAgentState() {
  if (!agentId.value || loaded.value) return
  loaded.value = true
  try {
    const { data: connsData } = await useMyFetch(`/data_sources/${agentId.value}/connections`, { method: 'GET' })
    const conns: any = connsData?.value
    if (Array.isArray(conns)) connRows.value = conns
    if (fileConns.value.length) {
      const lists = await Promise.all(fileConns.value.map((c: any) =>
        useMyFetch(`/data_sources/${agentId.value}/connections/${c.id}/files?limit=30`, { method: 'GET' }).then((r: any) => r?.data?.value).catch(() => null)
      ))
      fileNames.value = lists.flatMap((l: any) => Array.isArray(l?.files) ? l.files.map((f: any) => f.name || f.path || String(f)) : (Array.isArray(l) ? l.map((f: any) => f.name || f.path || String(f)) : []))
    }
    const anyDown = connRows.value.some((c: any) => c.last_connection_status === 'error')
    agentActive.value = !anyDown
  } catch { /* header still renders from the tool result */ }

  // Instructions are access-scoped server-side: a 200 means the viewer may see
  // them (drafts included — training-created instructions await approval).
  try {
    const { data, error } = await useMyFetch('/instructions', {
      method: 'GET',
      query: { skip: 0, limit: 20, data_source_ids: agentId.value, include_global: false, include_drafts: true },
    })
    if (!error?.value) {
      instructions.value = (data?.value as any)?.items || []
      instructionsAllowed.value = true
    }
  } catch { instructionsAllowed.value = false }
}

onMounted(() => { if (succeeded.value) loadAgentState() })
watch(succeeded, (ok) => { if (ok) loadAgentState() })
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
/* Keep the embedded selectors compact inside the chat card. */
.create-agent-selector :deep(table) { font-size: 11px; }
.create-agent-selector :deep(h1) { font-size: 13px; }
</style>
