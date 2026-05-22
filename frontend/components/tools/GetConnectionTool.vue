<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span>Loading connection <span class="font-medium">{{ requestedName }}</span>…</span>
        </span>
        <span v-else-if="!connection" class="text-gray-600">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1 text-gray-400 inline" />
          {{ errorMessage || `Connection '${requestedName}' not found` }}
        </span>
        <span v-else class="text-gray-700 flex items-center">
          <DataSourceIcon :type="connection.type" class="h-2 me-1" />
          <span class="font-medium">{{ connection.name }}</span>
          <span class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 ms-2 uppercase tracking-wide">{{ connection.type }}</span>
          <span
            v-if="connection.indexing_status && connection.indexing_status !== 'completed'"
            class="text-[9px] px-1 py-0.5 rounded bg-yellow-50 text-yellow-700 ms-1"
          >{{ connection.indexing_status }}</span>
        </span>
      </div>
    </Transition>

    <div v-if="connection" class="text-xs text-gray-600 ms-1 space-y-2">
      <!-- Summary bar -->
      <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-400 ms-1">
        <span class="inline-flex items-center gap-1">
          <Icon name="heroicons-shield-check" class="w-3 h-3" />
          {{ connection.auth_policy === 'user_required' ? 'user creds' : 'system creds' }}
        </span>
        <span class="inline-flex items-center gap-1">
          <Icon name="heroicons-table-cells" class="w-3 h-3" />
          {{ connection.tables_total }} tables
        </span>
        <span v-if="isToolProvider" class="inline-flex items-center gap-1">
          <Icon name="heroicons-wrench-screwdriver" class="w-3 h-3" />
          {{ connection.tools_total }} tools
        </span>
        <span v-if="connection.last_synced_at" class="inline-flex items-center gap-1">
          <Icon name="heroicons-clock" class="w-3 h-3" />
          {{ shortDate(connection.last_synced_at) }}
        </span>
      </div>

      <!-- Config preview -->
      <section v-if="hasConfig">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('config')"
        >
          <Icon :name="isOpen('config') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-cog-6-tooth" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Config</span>
        </button>
        <table v-if="isOpen('config')" class="ps-5 mt-1 text-[11px]">
          <tbody>
            <tr v-for="(v, k) in connection.config_preview" :key="k">
              <td class="pe-3 text-gray-500 align-top">{{ k }}</td>
              <td class="text-gray-700 break-all">{{ renderValue(v) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <!-- Tables -->
      <section v-if="connection.tables && connection.tables.length">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('tables')"
        >
          <Icon :name="isOpen('tables') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-table-cells" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Tables</span>
          <span class="text-gray-400">
            {{ connection.tables.length }}<template v-if="connection.tables_truncated"> of {{ connection.tables_total }}</template>
          </span>
        </button>
        <ul v-if="isOpen('tables')" class="ps-5 mt-1 space-y-0.5">
          <li v-for="(t, i) in connection.tables" :key="i">
            <div
              class="flex items-center gap-2 py-0.5 cursor-pointer hover:bg-gray-50 rounded"
              @click="toggleTable(i)"
            >
              <Icon
                v-if="hasColumns(t)"
                :name="isTableOpen(i) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
                class="w-3 h-3 text-gray-400 rtl-flip"
              />
              <span v-else class="w-3 h-3" />
              <span v-if="t.schema" class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500">{{ t.schema }}</span>
              <code class="text-[11px] text-gray-700 truncate">{{ t.name }}</code>
              <span class="text-[10px] text-gray-400 ms-auto">{{ t.column_count }} cols<span v-if="t.no_rows != null"> · {{ t.no_rows.toLocaleString() }} rows</span></span>
            </div>
            <div v-if="hasColumns(t) && isTableOpen(i)" class="ps-5">
              <table class="text-[11px]">
                <thead class="text-gray-400">
                  <tr><th class="text-start pe-4 font-normal">Column</th><th class="text-start font-normal">Type</th></tr>
                </thead>
                <tbody>
                  <tr v-for="(c, ci) in t.columns_preview || []" :key="ci">
                    <td class="pe-4 text-gray-600">{{ c.name }}</td>
                    <td class="text-gray-400">{{ c.dtype || 'any' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </li>
          <li v-if="connection.tables_truncated" class="text-[10px] text-gray-400">…</li>
        </ul>
      </section>

      <!-- Tools (mcp/custom_api only) -->
      <section v-if="connection.tools && connection.tools.length">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('tools')"
        >
          <Icon :name="isOpen('tools') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-wrench-screwdriver" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Tools</span>
          <span class="text-gray-400">{{ connection.tools.length }}<template v-if="connection.tools.length < connection.tools_total"> of {{ connection.tools_total }}</template></span>
        </button>
        <ul v-if="isOpen('tools')" class="ps-5 mt-1 space-y-0.5">
          <li v-for="(t, i) in connection.tools" :key="i" class="flex items-center gap-2">
            <code class="text-[11px] text-gray-700">{{ t.name }}</code>
            <span
              class="text-[9px] px-1 py-0.5 rounded"
              :class="policyClass(t.policy, t.is_enabled)"
            >{{ t.is_enabled ? t.policy : 'disabled' }}</span>
            <span v-if="t.description" class="text-[10px] text-gray-400 truncate">— {{ t.description }}</span>
          </li>
        </ul>
      </section>

      <!-- Agents using -->
      <section v-if="connection.agents && connection.agents.length">
        <div class="flex items-center gap-1 text-gray-700">
          <Icon name="heroicons-cube" class="w-3 h-3 text-gray-400" />
          <span class="font-medium">Used by</span>
        </div>
        <div class="ps-5 mt-1">
          <span
            v-for="a in connection.agents"
            :key="a.id"
            class="inline-block text-[10px] px-1 py-0.5 rounded bg-blue-50 text-blue-700 me-1 mb-0.5"
          >{{ a.name }}</span>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface ToolExecution {
  status: string
  result_json?: any
  arguments_json?: any
}
const props = defineProps<{ toolExecution: ToolExecution }>()

const TOOL_PROVIDER_TYPES = new Set(['mcp', 'custom_api'])

const status = computed<string>(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const connection = computed<any>(() => result.value?.connection || null)
const errorMessage = computed<string>(() => result.value?.error_message || '')
const requestedName = computed<string>(() => args.value?.name || connection.value?.name || 'connection')
const isToolProvider = computed<boolean>(() => TOOL_PROVIDER_TYPES.has(connection.value?.type))
const hasConfig = computed<boolean>(() => {
  const cfg = connection.value?.config_preview
  return cfg && typeof cfg === 'object' && Object.keys(cfg).length > 0
})

const openSections = ref<Set<string>>(new Set(['tables', 'tools']))
function toggleSection(key: string) {
  if (openSections.value.has(key)) openSections.value.delete(key)
  else openSections.value.add(key)
}
function isOpen(key: string): boolean { return openSections.value.has(key) }

const openTables = ref<Set<number>>(new Set())
function toggleTable(i: number) {
  if (openTables.value.has(i)) openTables.value.delete(i)
  else openTables.value.add(i)
}
function isTableOpen(i: number) { return openTables.value.has(i) }
function hasColumns(t: any): boolean {
  return Array.isArray(t?.columns_preview) && t.columns_preview.length > 0
}

function renderValue(v: any): string {
  if (v == null) return ''
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function policyClass(policy: string, isEnabled: boolean): string {
  if (!isEnabled) return 'bg-gray-100 text-gray-400'
  if (policy === 'deny') return 'bg-red-50 text-red-600'
  if (policy === 'confirm') return 'bg-amber-50 text-amber-700'
  return 'bg-green-50 text-green-700'
}

function shortDate(iso: string): string {
  try {
    const d = new Date(iso)
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  } catch { return '' }
}
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
