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
        <span v-else class="text-gray-700 dark:text-gray-300 flex items-center">
          <Icon name="heroicons-exclamation-triangle" class="w-3 h-3 me-1 text-amber-500" />
          <span class="align-middle">{{ $t('tools.createAgent.failed') }}</span>
        </span>
      </div>
    </Transition>

    <!-- Failure / rejection detail -->
    <div v-if="status !== 'running' && !succeeded && message" class="text-xs text-gray-500 dark:text-gray-400 ms-1 mb-1">
      {{ message }}
    </div>

    <!-- Agent card -->
    <Transition name="fade" appear>
      <div
        v-if="succeeded"
        class="rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 max-w-xl"
      >
        <!-- Card header: name, status, badges -->
        <div class="px-3 pt-2.5 pb-2 border-b border-gray-100 dark:border-gray-800">
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

        <!-- Tabs -->
        <div class="px-3 pt-1.5">
          <div class="flex items-center gap-3 text-[11px] border-b border-gray-100 dark:border-gray-800">
            <button
              v-for="tab in visibleTabs" :key="tab"
              class="pb-1.5 -mb-px border-b-2 transition-colors"
              :class="activeTab === tab ? 'border-blue-500 text-blue-600 dark:text-blue-400 font-medium' : 'border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'"
              @click="activeTab = tab"
            >
              {{ $t(`tools.createAgent.tab.${tab}`) }}
              <span class="text-[9px] text-gray-400 ms-0.5">{{ tabCount(tab) }}</span>
            </button>
          </div>

          <!-- Tables tab -->
          <div v-if="activeTab === 'tables'" class="py-1.5 max-h-48 overflow-y-auto">
            <ul v-if="tableItems.length" class="space-y-0.5 leading-snug text-[11px]">
              <li v-for="tbl in tableItems" :key="tbl.id || tbl.name" class="flex items-center py-0.5">
                <span class="w-1.5 h-1.5 rounded-full me-1.5 flex-shrink-0" :class="tbl.is_active ? 'bg-emerald-500' : 'bg-gray-300 dark:bg-gray-700'"></span>
                <span class="text-gray-700 dark:text-gray-300 truncate">{{ tbl.name }}</span>
              </li>
            </ul>
            <div v-else class="text-[11px] text-gray-400 py-1">{{ $t('tools.createAgent.emptyTab') }}</div>
          </div>

          <!-- Tools tab -->
          <div v-if="activeTab === 'tools'" class="py-1.5 max-h-48 overflow-y-auto">
            <ul v-if="toolRows.length" class="space-y-0.5 leading-snug text-[11px]">
              <li v-for="tool in toolRows" :key="tool.id || tool.name" class="flex items-center py-0.5">
                <span class="w-1.5 h-1.5 rounded-full me-1.5 flex-shrink-0" :class="tool.is_enabled ? 'bg-emerald-500' : 'bg-gray-300 dark:bg-gray-700'"></span>
                <span class="text-gray-700 dark:text-gray-300 font-mono truncate">{{ tool.name }}</span>
                <span v-if="tool.policy && tool.policy !== 'allow'" class="ms-1.5 text-[9px] px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-950 text-amber-700 dark:text-amber-400 flex-shrink-0">{{ tool.policy }}</span>
              </li>
            </ul>
            <div v-else class="text-[11px] text-gray-400 py-1">{{ $t('tools.createAgent.emptyTab') }}</div>
          </div>

          <!-- Files tab -->
          <div v-if="activeTab === 'files'" class="py-1.5 max-h-48 overflow-y-auto">
            <ul v-if="fileNames.length" class="space-y-0.5 leading-snug text-[11px]">
              <li v-for="f in fileNames" :key="f" class="flex items-center py-0.5">
                <Icon name="heroicons-document" class="w-3 h-3 me-1 text-gray-400 flex-shrink-0" />
                <span class="text-gray-700 dark:text-gray-300 font-mono truncate">{{ f }}</span>
              </li>
            </ul>
            <div v-else class="text-[11px] text-gray-400 py-1">{{ $t('tools.createAgent.emptyTab') }}</div>
          </div>
        </div>

        <!-- Footer actions -->
        <div class="px-3 py-2 border-t border-gray-100 dark:border-gray-800 flex items-center gap-3">
          <NuxtLink
            :to="`/agents/${agentId}`"
            class="text-[11px] text-blue-600 hover:text-blue-800 dark:text-blue-400 inline-flex items-center gap-1"
          >
            <Icon name="heroicons:arrow-top-right-on-square" class="w-3 h-3" />
            {{ $t('tools.createAgent.open') }}
          </NuxtLink>
          <span class="text-[10px] text-gray-400">{{ $t('tools.createAgent.editHint') }}</span>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

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
const message = computed<string>(() => result.value?.message || props.toolExecution?.result_summary || '')

const agentId = computed<string>(() => String(result.value?.data_source_id || ''))
const agentName = computed<string>(() => result.value?.name || args.value?.name || '')
const description = computed<string>(() => result.value?.description || args.value?.description || '')
const isPublic = computed<boolean>(() => !!result.value?.is_public)
const connections = computed<string[]>(() => Array.isArray(result.value?.connections) ? result.value.connections : [])
const unresolved = computed<string[]>(() => Array.isArray(result.value?.unresolved) ? result.value.unresolved : [])
const requiresConnect = computed<boolean>(() => !!result.value?.requires_user_connect)

// Live agent state (status + tabs). Falls back to the tool result payload.
const agentActive = ref<boolean>(true)
const tableItems = ref<any[]>([])
const toolRows = ref<any[]>([])
const fileNames = ref<string[]>([])
const loaded = ref(false)

const visibleTabs = computed<string[]>(() => {
  const tabs: string[] = []
  if (tableItems.value.length || (result.value?.tables_total || 0) > 0) tabs.push('tables')
  if (toolRows.value.length || (result.value?.tools_total || 0) > 0) tabs.push('tools')
  if (fileNames.value.length) tabs.push('files')
  return tabs.length ? tabs : ['tables']
})
const activeTab = ref('tables')
watch(visibleTabs, (tabs) => { if (!tabs.includes(activeTab.value)) activeTab.value = tabs[0] }, { immediate: true })

function tabCount(tab: string): string {
  if (tab === 'tables') {
    const active = tableItems.value.length ? tableItems.value.filter((t) => t.is_active).length : (result.value?.tables_active || 0)
    const total = tableItems.value.length || result.value?.tables_total || 0
    return total ? `${active}/${total}` : ''
  }
  if (tab === 'tools') {
    const enabled = toolRows.value.length ? toolRows.value.filter((t) => t.is_enabled).length : (result.value?.tools_enabled || 0)
    const total = toolRows.value.length || result.value?.tools_total || 0
    return total ? `${enabled}/${total}` : ''
  }
  return fileNames.value.length ? String(fileNames.value.length) : ''
}

async function loadAgentState() {
  if (!agentId.value || loaded.value) return
  loaded.value = true
  // Seed from the tool result so the card renders even if fetches fail.
  tableItems.value = (result.value?.active_table_sample || []).map((n: string) => ({ name: n, is_active: true }))
  try {
    const [{ data: schemaData }, { data: toolsData }, { data: connsData }] = await Promise.all([
      useMyFetch(`/data_sources/${agentId.value}/full_schema?page=1&page_size=100&sort_by=is_active&sort_dir=desc`, { method: 'GET' }),
      useMyFetch(`/data_sources/${agentId.value}/tools`, { method: 'GET' }),
      useMyFetch(`/data_sources/${agentId.value}/connections`, { method: 'GET' }),
    ])
    const schema: any = schemaData?.value
    if (schema?.tables?.length) tableItems.value = schema.tables
    const tools: any = toolsData?.value
    if (Array.isArray(tools)) toolRows.value = tools
    const conns: any = connsData?.value
    const fileConns = Array.isArray(conns) ? conns.filter((c: any) => ['network_dir', 's3', 'sharepoint', 'onedrive', 'google_drive', 'outlook_mail'].includes(c.type)) : []
    if (fileConns.length) {
      const lists = await Promise.all(fileConns.map((c: any) =>
        useMyFetch(`/data_sources/${agentId.value}/connections/${c.id}/files?limit=30`, { method: 'GET' }).then((r: any) => r?.data?.value).catch(() => null)
      ))
      fileNames.value = lists.flatMap((l: any) => Array.isArray(l?.files) ? l.files.map((f: any) => f.name || f.path || String(f)) : (Array.isArray(l) ? l.map((f: any) => f.name || f.path || String(f)) : []))
    }
    // Agent health from the connections payload when present.
    const anyDown = Array.isArray(conns) && conns.some((c: any) => c.last_connection_status === 'error')
    agentActive.value = !anyDown
  } catch {
    /* card still renders from the tool result */
  }
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
</style>
