<template>
  <UPopover v-model="isOpen" :popper="{ placement: 'bottom-end' }">
    <!-- Trigger Button (default slot) - matches Toolbar button style -->

        <UTooltip text="Add Filters">
    <UChip v-if="activeFilterCount > 0" :text="activeFilterCount" size="2xl" color="blue">
      <button
        type="button"
        class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded"
      >
        <Icon name="heroicons:funnel" class="w-4 h-4" />
      </button>
    </UChip>
    <button
      v-else
      type="button"
      class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded"
    >
      <Icon name="heroicons:funnel" class="w-4 h-4" />
    </button>

    </UTooltip>
    <!-- Popover Panel -->
    <template #panel>
      <div class="w-[460px] max-w-[95vw]">
        <!-- Header -->
        <div class="flex items-center justify-between px-3 py-2 border-b border-gray-200">
          <span class="font-medium text-sm text-gray-700">Filters</span>
          <UButton
            v-if="hasActiveFilters"
            color="gray"
            variant="ghost"
            size="xs"
            @click="clearAllFilters"
          >
            Clear
          </UButton>
        </div>

        <!-- Content -->
        <div class="p-3 max-h-[320px] overflow-y-auto">
          <!-- Loading state -->
          <div v-if="props.isLoading" class="text-center py-6">
            <Icon name="heroicons:arrow-path" class="w-5 h-5 text-gray-300 mx-auto mb-2 animate-spin" />
            <p class="text-xs text-gray-500">Loading...</p>
          </div>
          
          <!-- No columns available -->
          <div v-else-if="discoveredColumns.length === 0" class="text-center py-6">
            <p class="text-xs text-gray-500 mb-2">No data available to filter</p>
            <UButton size="xs" color="gray" variant="ghost" @click="refreshColumns">
              Refresh
            </UButton>
          </div>
          
          <!-- Empty State - has columns but no filters -->
          <div v-else-if="filterGroups.length === 0" class="text-center py-6">
            <p class="text-xs text-gray-500 mb-3">No filters applied</p>
            <UButton size="xs" color="blue" @click="addGroup">
              Add filter
            </UButton>
          </div>

          <!-- Filter Groups -->
          <div v-else class="space-y-2">
            <div
              v-for="(group, groupIndex) in filterGroups"
              :key="group.id"
            >
              <!-- OR Divider -->
              <div v-if="groupIndex > 0" class="flex items-center gap-2 py-1">
                <div class="flex-1 h-px bg-gray-300"></div>
                <span class="text-[10px] font-semibold text-orange-500">OR</span>
                <div class="flex-1 h-px bg-gray-300"></div>
              </div>

              <!-- Group Card -->
              <div class="bg-gray-50 rounded p-2">
                <!-- Conditions -->
                <div
                  v-for="(condition, condIndex) in group.conditions"
                  :key="condition.id"
                  class="mb-2 last:mb-0"
                >
                  <div v-if="condIndex > 0" class="text-[10px] font-semibold text-blue-500 mb-1">AND</div>

                  <div class="flex items-center gap-1.5">
                    <!-- Column Select -->
                    <USelectMenu
                      v-model="condition.column"
                      :options="columnOptions"
                      placeholder="Column"
                      size="xs"
                      value-attribute="value"
                      option-attribute="label"
                      class="w-[160px]"
                      searchable
                      searchable-placeholder="Search..."
                      :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                      :ui-menu="{ height: 'max-h-48', option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                      @update:model-value="onColumnChange(condition)"
                    >
                      <template #option="{ option }">
                        <div 
                          v-if="option.header" 
                          class="text-[10px] font-semibold text-gray-500 uppercase tracking-wider py-0.5 bg-gray-50 -mx-2 px-2 cursor-default"
                        >
                          {{ option.label }}
                        </div>
                        <div v-else class="text-xs pl-1">
                          {{ option.label }}
                        </div>
                      </template>
                    </USelectMenu>

                    <!-- Operator Select -->
                    <USelectMenu
                      v-model="condition.operator"
                      :options="getOperatorsForColumn(condition.column)"
                      size="xs"
                      value-attribute="value"
                      option-attribute="label"
                      class="w-[100px]"
                      :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                      :ui-menu="{ option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                    />

                    <!-- Value Input -->
                    <template v-if="!['is_empty', 'is_not_empty', 'is_true', 'is_false'].includes(condition.operator)">
                      <USelectMenu
                        v-if="shouldShowSelect(condition)"
                        v-model="condition.value"
                        :options="getValueOptions(condition.column)"
                        placeholder="Value"
                        size="xs"
                        value-attribute="value"
                        option-attribute="label"
                        class="flex-1 min-w-[100px]"
                        searchable
                        searchable-placeholder="Search..."
                        :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                        :ui-menu="{ height: 'max-h-48', option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                      />
                      <UInput
                        v-else-if="getColumnType(condition.column) === 'number'"
                        v-model="condition.value"
                        type="number"
                        placeholder="Value"
                        size="xs"
                        class="flex-1 min-w-[100px]"
                      />
                      <UInput
                        v-else-if="getColumnType(condition.column) === 'date'"
                        v-model="condition.value"
                        type="date"
                        size="xs"
                        class="flex-1 min-w-[100px]"
                      />
                      <UInput
                        v-else
                        v-model="condition.value"
                        type="text"
                        placeholder="Value"
                        size="xs"
                        class="flex-1 min-w-[100px]"
                      />
                    </template>

                    <!-- Remove Button -->
                    <UButton
                      color="gray"
                      variant="ghost"
                      size="xs"
                      icon="i-heroicons-x-mark"
                      @click="removeCondition(group, condition)"
                    />
                  </div>
                </div>

                <!-- Actions Row -->
                <div class="flex items-center gap-2 pt-2 mt-2 border-t border-gray-200">
                  <UButton
                    color="gray"
                    variant="ghost"
                    size="xs"
                    icon="i-heroicons-plus"
                    @click="addCondition(group)"
                  >
                    AND
                  </UButton>
                  <UButton
                    v-if="groupIndex === filterGroups.length - 1"
                    color="orange"
                    variant="ghost"
                    size="xs"
                    @click="addGroup"
                  >
                    OR
                  </UButton>
                  <div class="flex-1"></div>
                  <UButton
                    color="red"
                    variant="ghost"
                    size="xs"
                    icon="i-heroicons-trash"
                    @click="removeGroup(group)"
                  />
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Footer -->
        <div v-if="filterGroups.length > 0" class="px-3 py-2 border-t border-gray-200 flex items-center justify-between">
          <span class="text-xs text-gray-500">
            {{ filteredRowCount }} of {{ totalRowCount }} rows
          </span>
          <UButton size="xs" color="blue" @click="applyFilters">
            Apply
          </UButton>
        </div>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'

// Types
interface FilterCondition {
  id: string
  column: string  // Format: "widgetId:columnName"
  operator: string
  value: any
  value2?: any
}

interface FilterGroup {
  id: string
  conditions: FilterCondition[]
}

interface DiscoveredColumn {
  key: string  // Format: "widgetId:columnName" for uniqueness
  name: string
  label: string
  widgetId: string
  widgetTitle: string
  type: 'string' | 'number' | 'date' | 'boolean'
  uniqueValues: any[]
  sampleValues: any[]
}

// Props
interface VisualizationData {
  id: string
  title: string
  queryId: string
  rows: any[]
  columns: any[]
}

const props = defineProps<{
  visualizations: VisualizationData[]
  isLoading?: boolean
  reportId?: string  // For shared filter sync
}>()

// Emits
const emit = defineEmits<{
  (e: 'update:filters', filters: FilterGroup[]): void
}>()

// State
const isOpen = ref(false)
const filterGroups = ref<FilterGroup[]>([])
const appliedFilters = ref<FilterGroup[]>([])  // Filters that have been applied
const columnsVersion = ref(0)  // Force reactivity trigger
const filterInstanceId = `filterbuilder-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

// Generate unique IDs
const generateId = () => `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`

// Listen for shared filter updates from per-visualization filters
function handleSharedFilterUpdate(ev: Event) {
  const detail = (ev as CustomEvent).detail
  if (!detail || detail.source === filterInstanceId) return
  if (props.reportId && detail.reportId !== props.reportId) return
  
  // Update local state to match shared state
  const newFilters = JSON.parse(JSON.stringify(detail.filters || []))
  appliedFilters.value = newFilters
  filterGroups.value = JSON.parse(JSON.stringify(newFilters))
}

onMounted(() => {
  window.addEventListener('filter:updated', handleSharedFilterUpdate as any)
})

onUnmounted(() => {
  window.removeEventListener('filter:updated', handleSharedFilterUpdate as any)
})

// Watch for changes in visualizations to update columns
watch(() => props.visualizations, () => {
  columnsVersion.value++
}, { deep: true, immediate: true })

// Watch for panel open to refresh columns
watch(isOpen, (newVal) => {
  if (newVal) {
    columnsVersion.value++
  }
})

// Manual refresh
function refreshColumns() {
  columnsVersion.value++
}

// Apply filters - emit to parent
function applyFilters() {
  // Deep copy current filters to applied
  appliedFilters.value = JSON.parse(JSON.stringify(filterGroups.value))
  emit('update:filters', appliedFilters.value)
  isOpen.value = false
}

// Discover columns from all visualizations
const discoveredColumns = computed<DiscoveredColumn[]>(() => {
  // Use columnsVersion to force reactivity on deep changes
  const _version = columnsVersion.value
  const columns: DiscoveredColumn[] = []
  
  for (const viz of props.visualizations || []) {
    const rows = viz.rows || []
    if (!rows.length) continue
    
    const vizId = viz.id
    const vizTitle = viz.title || `Visualization ${vizId.slice(0, 6)}`
    const sampleRow = rows[0]
    const keys = Object.keys(sampleRow || {})
    
    for (const key of keys) {
      const columnKey = `${vizId}:${key}`
      
      // Sample values for type inference and options
      const values = rows.slice(0, 100).map(r => r[key]).filter(v => v != null)
      const uniqueVals = [...new Set(values)]
      
      columns.push({
        key: columnKey,
        name: key,
        label: formatColumnLabel(key),
        widgetId: vizId,
        widgetTitle: vizTitle,
        type: inferColumnType(values),
        uniqueValues: uniqueVals.slice(0, 50),
        sampleValues: values.slice(0, 10)
      })
    }
  }
  
  // Sort by visualization title, then by column label
  return columns.sort((a, b) => {
    const vizCompare = a.widgetTitle.localeCompare(b.widgetTitle)
    if (vizCompare !== 0) return vizCompare
    return a.label.localeCompare(b.label)
  })
})

// Visualization options for the first dropdown
const visualizationOptions = computed(() => {
  const vizs = props.visualizations || []
  if (vizs.length === 0) return []
  
  // If only one visualization, return just that one
  if (vizs.length === 1) {
    return [{
      label: vizs[0].title || 'Visualization',
      value: vizs[0].id
    }]
  }
  
  return vizs.map(viz => ({
    label: viz.title || `Visualization ${viz.id.slice(0, 6)}`,
    value: viz.id
  }))
})

// Get columns for a specific visualization
function getColumnsForVisualization(vizId: string) {
  return discoveredColumns.value.filter(col => col.widgetId === vizId)
}

// Column options for a specific visualization
function getColumnOptionsForVisualization(vizId: string) {
  const cols = getColumnsForVisualization(vizId)
  return cols.map(col => ({
    label: col.label,
    value: col.key
  }))
}

// Column options for select dropdown - grouped by visualization
const columnOptions = computed(() => {
  const vizs = props.visualizations || []
  
  // If only one visualization, just show column names (no grouping needed)
  if (vizs.length <= 1) {
    return discoveredColumns.value.map(col => ({
      label: col.label,
      value: col.key
    }))
  }
  
  // Multiple visualizations - group columns under visualization headers
  const options: Array<{ label: string; value: string; disabled?: boolean; header?: boolean }> = []
  
  // Group columns by visualization
  const grouped = new Map<string, typeof discoveredColumns.value>()
  for (const col of discoveredColumns.value) {
    if (!grouped.has(col.widgetId)) {
      grouped.set(col.widgetId, [])
    }
    grouped.get(col.widgetId)!.push(col)
  }
  
  // Build options with headers
  for (const [vizId, cols] of grouped) {
    const vizTitle = cols[0]?.widgetTitle || `Visualization ${vizId.slice(0, 6)}`
    
    // Add visualization header (unselectable)
    options.push({
      label: vizTitle,
      value: `header-${vizId}`,
      disabled: true,
      header: true
    })
    
    // Add columns under this visualization
    for (const col of cols) {
      options.push({
        label: col.label,
        value: col.key
      })
    }
  }
  
  return options
})

// Get rows from a visualization
function getVisualizationRows(vizId: string): any[] {
  const viz = props.visualizations?.find(v => v.id === vizId)
  return viz?.rows || []
}

// Format column name for display
function formatColumnLabel(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .split(' ')
    .map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(' ')
}

// Infer column type from sample values
function inferColumnType(values: any[]): 'string' | 'number' | 'date' | 'boolean' {
  if (!values.length) return 'string'
  
  const sample = values.filter(v => v != null).slice(0, 20)
  if (!sample.length) return 'string'
  
  // Check for boolean
  if (sample.every(v => typeof v === 'boolean' || v === 'true' || v === 'false')) {
    return 'boolean'
  }
  
  // Check for number
  if (sample.every(v => typeof v === 'number' || (!isNaN(parseFloat(v)) && isFinite(v)))) {
    return 'number'
  }
  
  // Check for date
  const datePatterns = [
    /^\d{4}-\d{2}-\d{2}/, // YYYY-MM-DD
    /^\d{2}\/\d{2}\/\d{4}/, // MM/DD/YYYY
    /^\d{4}\/\d{2}\/\d{2}/, // YYYY/MM/DD
  ]
  if (sample.every(v => {
    if (v instanceof Date) return true
    if (typeof v === 'string') {
      return datePatterns.some(p => p.test(v)) || !isNaN(Date.parse(v))
    }
    return false
  })) {
    return 'date'
  }
  
  return 'string'
}

// Get column info by key (widgetId:columnName)
function getColumn(key: string): DiscoveredColumn | undefined {
  return discoveredColumns.value.find(c => c.key === key)
}

function getColumnType(key: string): string {
  return getColumn(key)?.type || 'string'
}

// Parse column key to get widget ID and column name
function parseColumnKey(key: string): { widgetId: string, columnName: string } {
  const [widgetId, ...rest] = (key || '').split(':')
  return { widgetId, columnName: rest.join(':') }
}

// Get column label for display
function getColumnLabel(key: string): string {
  const col = getColumn(key)
  if (!col) {
    const { columnName } = parseColumnKey(key)
    return formatColumnLabel(columnName) || key
  }
  return col.label
}

// Get display name for a column (widget name > column label)
function getColumnDisplayName(key: string): string {
  const col = getColumn(key)
  if (!col) {
    const { columnName } = parseColumnKey(key)
    return formatColumnLabel(columnName) || 'Select column'
  }
  return col.label
}

// Operators based on column type
const stringOperators = [
  { label: 'equals', value: 'equals' },
  { label: 'not equals', value: 'not_equals' },
  { label: 'contains', value: 'contains' },
  { label: 'not contains', value: 'not_contains' },
  { label: 'starts with', value: 'starts_with' },
  { label: 'ends with', value: 'ends_with' },
  { label: 'is empty', value: 'is_empty' },
  { label: 'is not empty', value: 'is_not_empty' },
  { label: 'in', value: 'in' },
  { label: 'not in', value: 'not_in' },
]

const numberOperators = [
  { label: 'equals', value: 'equals' },
  { label: 'not equals', value: 'not_equals' },
  { label: 'greater than', value: 'greater_than' },
  { label: 'less than', value: 'less_than' },
  { label: 'greater or equal', value: 'gte' },
  { label: 'less or equal', value: 'lte' },
  { label: 'between', value: 'between' },
  { label: 'is empty', value: 'is_empty' },
  { label: 'is not empty', value: 'is_not_empty' },
]

const dateOperators = [
  { label: 'equals', value: 'equals' },
  { label: 'before', value: 'before' },
  { label: 'after', value: 'after' },
  { label: 'between', value: 'between' },
  { label: 'is empty', value: 'is_empty' },
  { label: 'is not empty', value: 'is_not_empty' },
]

const booleanOperators = [
  { label: 'is true', value: 'is_true' },
  { label: 'is false', value: 'is_false' },
]

function getOperatorsForColumn(columnName: string) {
  const type = getColumnType(columnName)
  switch (type) {
    case 'number': return numberOperators
    case 'date': return dateOperators
    case 'boolean': return booleanOperators
    default: return stringOperators
  }
}

// Value options for select inputs
function getValueOptions(columnKey: string) {
  const col = getColumn(columnKey)
  if (!col) return []
  
  return col.uniqueValues.map(v => ({
    label: String(v),
    value: v
  }))
}

// Should show select dropdown for value - only for string columns with equals/not_equals and low cardinality
function shouldShowSelect(condition: FilterCondition): boolean {
  // Only show dropdown for equals/not_equals operators
  if (!['equals', 'not_equals'].includes(condition.operator)) {
    return false
  }
  
  const col = getColumn(condition.column)
  if (!col) return false
  
  // Never show dropdown for number or date columns - use input instead
  if (col.type === 'number' || col.type === 'date') {
    return false
  }
  
  // Show select for low-cardinality string/boolean columns (≤50 unique values)
  return col.uniqueValues.length > 0 && col.uniqueValues.length <= 50
}

// Handle column change - reset operator and value
function onColumnChange(condition: FilterCondition) {
  const type = getColumnType(condition.column)
  const operators = getOperatorsForColumn(condition.column)
  condition.operator = operators[0]?.value || 'equals'
  condition.value = type === 'boolean' ? true : ''
}

// Filter group management
function addGroup() {
  filterGroups.value.push({
    id: generateId(),
    conditions: [{
      id: generateId(),
      column: columnOptions.value[0]?.value || '',
      operator: 'equals',
      value: ''
    }]
  })
}

function removeGroup(group: FilterGroup) {
  const idx = filterGroups.value.findIndex(g => g.id === group.id)
  if (idx !== -1) {
    filterGroups.value.splice(idx, 1)
  }
}

function addCondition(group: FilterGroup) {
  group.conditions.push({
    id: generateId(),
    column: columnOptions.value[0]?.value || '',
    operator: 'equals',
    value: ''
  })
}

function removeCondition(group: FilterGroup, condition: FilterCondition) {
  const idx = group.conditions.findIndex(c => c.id === condition.id)
  if (idx !== -1) {
    group.conditions.splice(idx, 1)
  }
  // Remove group if empty
  if (group.conditions.length === 0) {
    removeGroup(group)
  }
}

function clearAllFilters() {
  filterGroups.value = []
  appliedFilters.value = []
  emit('update:filters', [])
}

// Computed stats - based on APPLIED filters (not pending)
const hasActiveFilters = computed(() => appliedFilters.value.length > 0)

const activeFilterCount = computed(() => 
  appliedFilters.value.reduce((sum, g) => sum + g.conditions.length, 0)
)

// Check if there are pending (unapplied) changes
const hasPendingChanges = computed(() => 
  JSON.stringify(filterGroups.value) !== JSON.stringify(appliedFilters.value)
)

const totalRowCount = computed(() => {
  let count = 0
  for (const viz of props.visualizations || []) {
    count += viz.rows?.length || 0
  }
  return count
})

const filteredRowCount = computed(() => {
  if (!hasActiveFilters.value) return totalRowCount.value
  
  let count = 0
  for (const viz of props.visualizations || []) {
    const vizId = viz.id
    const rows = viz.rows || []
    count += rows.filter(row => evaluateFilters(row, filterGroups.value, vizId)).length
  }
  return count
})

// Filter evaluation
function evaluateFilters(row: any, groups: FilterGroup[], widgetId?: string): boolean {
  if (!groups.length) return true
  
  // Check if any condition in any group applies to this widget
  const hasRelevantConditions = groups.some(group => 
    group.conditions.some(cond => {
      const { widgetId: condWidgetId } = parseColumnKey(cond.column)
      return condWidgetId === widgetId
    })
  )
  
  // If no conditions apply to this widget, don't filter it
  if (widgetId && !hasRelevantConditions) {
    return true
  }
  
  // OR across groups
  return groups.some(group => {
    // AND within group (only conditions for this widget)
    return group.conditions.every(cond => evaluateCondition(row, cond, widgetId))
  })
}

function evaluateCondition(row: any, condition: FilterCondition, widgetId?: string): boolean {
  // Parse the column key to get widget and column name
  const { widgetId: conditionWidgetId, columnName } = parseColumnKey(condition.column)
  
  // If filtering a specific widget, skip conditions for other widgets
  if (widgetId && conditionWidgetId !== widgetId) {
    return true // Don't filter out rows for non-matching widget conditions
  }
  
  // Case-insensitive column lookup
  const columnKey = Object.keys(row).find(
    k => k.toLowerCase() === columnName.toLowerCase()
  )
  const value = columnKey ? row[columnKey] : undefined
  const target = condition.value
  
  switch (condition.operator) {
    case 'equals':
      return String(value).toLowerCase() === String(target).toLowerCase()
    case 'not_equals':
      return String(value).toLowerCase() !== String(target).toLowerCase()
    case 'contains':
      return String(value).toLowerCase().includes(String(target).toLowerCase())
    case 'not_contains':
      return !String(value).toLowerCase().includes(String(target).toLowerCase())
    case 'starts_with':
      return String(value).toLowerCase().startsWith(String(target).toLowerCase())
    case 'ends_with':
      return String(value).toLowerCase().endsWith(String(target).toLowerCase())
    case 'greater_than':
      return Number(value) > Number(target)
    case 'less_than':
      return Number(value) < Number(target)
    case 'gte':
      return Number(value) >= Number(target)
    case 'lte':
      return Number(value) <= Number(target)
    case 'between':
      return Number(value) >= Number(target) && Number(value) <= Number(condition.value2)
    case 'before':
      return new Date(value) < new Date(target)
    case 'after':
      return new Date(value) > new Date(target)
    case 'in':
      return Array.isArray(target) && target.some(t => 
        String(value).toLowerCase() === String(t).toLowerCase()
      )
    case 'not_in':
      return !Array.isArray(target) || !target.some(t => 
        String(value).toLowerCase() === String(t).toLowerCase()
      )
    case 'is_empty':
      return value == null || value === ''
    case 'is_not_empty':
      return value != null && value !== ''
    case 'is_true':
      return value === true || value === 'true' || value === 1
    case 'is_false':
      return value === false || value === 'false' || value === 0
    default:
      return true
  }
}

// Expose filter evaluation function for parent component
defineExpose({
  evaluateFilters,
  appliedFilters
})
</script>

<style scoped>
/* No custom styles needed - using Nuxt UI defaults */
</style>

 