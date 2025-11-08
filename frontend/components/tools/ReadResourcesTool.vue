<template>
  <div class="mt-1">
    <!-- Status header -->
    <Transition name="fade" appear>
    <div class="mb-2 flex items-center text-xs text-gray-500 cursor-pointer hover:text-gray-700">
      <span v-if="status === 'running'" class="tool-shimmer flex items-center">
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 mr-1 text-gray-400" />
        Searching {{ queryLabel }}…
      </span>
      <span v-else class="text-gray-700 flex items-center">
        <Icon name="heroicons-magnifying-glass" class="w-3 h-3 mr-1 text-gray-400" />
        <span class="align-middle">Searched {{ queryLabel }}</span>
      </span>
    </div>
    </Transition>
    <!-- Preview of top results (click to toggle details) -->
    <Transition name="fade" appear>
    <div v-if="topResults && topResults.length" class="text-xs text-gray-600">
      <ul class="ml-1 space-y-1 leading-snug">
        <li v-for="(item, idx) in topResults.slice(0, 10)" :key="idx">
          <!-- Header row -->
          <div
            class="flex items-center py-1 px-1 rounded cursor-pointer hover:bg-gray-50"
            @click="toggleItem(idx)"
            :aria-expanded="isExpanded(idx)"
          >
            <Icon :name="isExpanded(idx) ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 mr-1" />
            <DataSourceIcon :type="inferIconTypeFromItem(item)" class="h-3 mr-2" />
            <div class="font-medium text-gray-700 truncate">
              {{ item.name || item.path || 'resource' }}
            </div>
          </div>
          <!-- Detail row -->
          <Transition name="fade">
            <div v-if="isExpanded(idx)" class="pl-6 pr-1 pb-1">
              <!-- DBT (verbose) -->
              <template v-if="isDbt(item)">
                <div class="text-gray-600 mb-1 flex items-center flex-wrap">
                  <span class="inline-block text-[10px] uppercase tracking-wide bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded mr-2">
                    {{ simplifyDbtType(item.resource_type) }}
                  </span>
                  <span v-if="item.database || item.schema" class="text-gray-400">
                    {{ [item.database, item.schema].filter(Boolean).join('.') }}
                  </span>
                </div>
                <!-- Metric-focused details -->
                <div v-if="isDbtMetric(item)" class="text-gray-600 space-y-1">
                  <div class="text-gray-500" v-if="dbtMetricSummary(item)">
                    {{ dbtMetricSummary(item) }}
                  </div>
                  <div class="text-gray-500" v-if="dbtMetricDimensions(item).length">
                    <span class="text-gray-400 mr-1">Dimensions:</span>
                    {{ dbtMetricDimensions(item).slice(0,4).join(', ') }}<span v-if="dbtMetricDimensions(item).length > 4">, …</span>
                  </div>
                  <div class="text-gray-500" v-if="dbtMetricGrains(item).length">
                    <span class="text-gray-400 mr-1">Time grains:</span>
                    {{ dbtMetricGrains(item).join(', ') }}
                  </div>
                  <!-- Fallback when extractor details are not present in output -->
                  <div class="text-gray-500" v-if="!dbtMetricSummary(item) && !dbtMetricDimensions(item).length && !dbtMetricGrains(item).length">
                    <span v-if="item.description">{{ truncate(item.description, 240) }}</span>
                    <span v-else-if="item.path" class="text-gray-400">{{ item.path }}</span>
                  </div>
                </div>
                <!-- Generic DBT fallback -->
                <div v-else class="text-gray-500">
                  <span v-if="item.description">{{ truncate(item.description, 240) }}</span>
                  <span v-else-if="item.path" class="text-gray-400">{{ item.path }}</span>
                </div>
              </template>
              <!-- Non-DBT concise details -->
              <template v-else>
                <div class="text-gray-500">
                  <span v-if="item.description">{{ truncate(item.description, 240) }}</span>
                  <span v-else-if="item.path" class="text-gray-400">{{ item.path }}</span>
                </div>
              </template>
            </div>
          </Transition>
        </li>
      </ul>
    </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface ToolExecution {
  id: string
  tool_name: string
  tool_action?: string
  status: string
  result_summary?: string
  result_json?: any
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

const status = computed<string>(() => props.toolExecution?.status || '')

const queryLabel = computed<string>(() => {
  const rj = props.toolExecution?.result_json || {}
  // Prefer explicit search_query from result
  let q: any = rj.search_query
  // Fallback to original arguments sent to tool
  if (q == null) q = (props.toolExecution as any)?.arguments_json?.query
  if (Array.isArray(q)) return q.join(', ')
  if (typeof q === 'string') return q
  if (q && typeof q === 'object') return JSON.stringify(q)
  // Fallback to summary parsing if present
  const sum = props.toolExecution?.result_summary || ''
  const m = sum.match(/^Searching\s+(.+?)…?$/)
  return m ? m[1] : 'resources'
})

// Extract top results from backend (lightweight preview)
const topResults = computed<any[]>(() => {
  const rj: any = props.toolExecution?.result_json || {}
  const tr = Array.isArray(rj.top_results) ? rj.top_results : []
  return tr
})

const expandedItems = ref<Set<number>>(new Set())
function toggleItem(index: number) {
  if (expandedItems.value.has(index)) {
    expandedItems.value.delete(index)
  } else {
    expandedItems.value.add(index)
  }
}
function isExpanded(index: number): boolean {
  return expandedItems.value.has(index)
}

function truncate(text: string, max: number): string {
  try {
    const t = String(text || '')
    return t.length > max ? t.slice(0, max) + '…' : t
  } catch {
    return ''
  }
}

// Determine icon type per item (dbt, lookml, markdown, resource)
function inferIconTypeFromItem(item: any): string {
  try {
    const rt = String(item?.resource_type || '').toLowerCase()
    const path = String(item?.path || '').toLowerCase()
    if (rt.includes('dbt_')) return 'dbt'
    if (rt.includes('lookml')) return 'lookml'
    if (rt.includes('markdown')) return 'markdown'
    if (path.endsWith('.lkml')) return 'lookml'
    if (path.endsWith('.md') || path.endsWith('.markdown')) return 'markdown'
    return 'resource'
  } catch {
    return 'resource'
  }
}

// ---------- DBT helpers ----------
function isDbt(item: any): boolean {
  try {
    const rt = String(item?.resource_type || '').toLowerCase()
    const p = String(item?.path || '').toLowerCase()
    return rt.startsWith('dbt_') || p.includes('/models/') || p.includes('/metrics/') || p.includes('/seeds/') || p.includes('/macros/') || p.includes('/tests/') || p.includes('/exposures/') || p.includes('/sources/')
  } catch {
    return false
  }
}

function isDbtMetric(item: any): boolean {
  try {
    const rt = String(item?.resource_type || '').toLowerCase()
    const p = String(item?.path || '').toLowerCase()
    return rt === 'dbt_metric' || p.includes('/metrics/')
  } catch {
    return false
  }
}

function simplifyDbtType(rt: string | null | undefined): string {
  const s = String(rt || '').toLowerCase()
  return s.replace(/^dbt_/, '') || 'dbt'
}

function dbtMetricRaw(item: any): any {
  const raw = item?.raw_data
  if (raw && typeof raw === 'object') return raw
  try {
    return JSON.parse(String(raw || '{}'))
  } catch {
    return {}
  }
}

function dbtMetricSummary(item: any): string | null {
  try {
    const raw = dbtMetricRaw(item)
    const method = raw.calculation_method || raw.method || raw.type
    const model = raw.model || raw.ref || raw.source
    const expr = raw.expression || raw.sql
    if (method && model) {
      return `${method} of ${model}${expr ? ` (${String(expr).slice(0, 80)}${String(expr).length > 80 ? '…' : ''})` : ''}`
    }
    if (expr) return String(expr).slice(0, 120) + (String(expr).length > 120 ? '…' : '')
    return null
  } catch {
    return null
  }
}

function dbtMetricDimensions(item: any): string[] {
  try {
    const raw = dbtMetricRaw(item)
    const dims = raw.dimensions || raw.group_by || []
    return Array.isArray(dims) ? dims.map((d: any) => String(d)).filter(Boolean) : []
  } catch {
    return []
  }
}

function dbtMetricGrains(item: any): string[] {
  try {
    const raw = dbtMetricRaw(item)
    const grains = raw.time_grains || raw.grains || []
    return Array.isArray(grains) ? grains.map((g: any) => String(g)).filter(Boolean) : []
  } catch {
    return []
  }
}
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}

@keyframes shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: 100% 0; }
}

/* Fade transition for initial appear and toggles */
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.25s ease, transform 0.25s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
  transform: translateY(2px);
}
</style>


