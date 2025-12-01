<template>
  <UPopover v-model="isOpen" mode="click" :popper="{ placement: 'bottom-end', strategy: 'fixed', modifiers: [{ name: 'preventOverflow', options: { boundary: 'viewport' } }] }">
    <!-- Trigger: Clean funnel icon -->
    <button
      type="button"
      class="relative p-0.5 hover:bg-gray-100 rounded transition-colors"
      :class="{ 'text-blue-500': filterCount > 0, 'text-gray-400 hover:text-gray-600': filterCount === 0 }"
    >
      <Icon name="heroicons:funnel" class="w-3.5 h-3.5" />
      <!-- Badge for filter count -->
      <span
        v-if="filterCount > 0"
        class="absolute -top-1 -right-1 w-3.5 h-3.5 bg-blue-500 text-white text-[9px] font-medium rounded-full flex items-center justify-center"
      >
        {{ filterCount > 9 ? '9+' : filterCount }}
      </span>
    </button>

    <!-- Popover Panel -->
    <template #panel>
      <div class="w-[320px] max-w-[90vw]">
        <!-- Header -->
        <div class="flex items-center justify-between px-3 py-2 border-b border-gray-100">
          <span class="text-xs font-medium text-gray-700">Filter</span>
          <button
            v-if="filterCount > 0"
            type="button"
            class="text-[10px] text-gray-400 hover:text-red-500"
            @click="clearFilters"
          >
            Clear all
          </button>
        </div>

        <!-- Content -->
        <div class="p-2 max-h-[280px] overflow-y-auto">
          <!-- No columns available -->
          <div v-if="discoveredColumns.length === 0" class="text-center py-4">
            <p class="text-xs text-gray-400">No data available</p>
          </div>

          <!-- Active conditions -->
          <div v-else>
            <!-- Existing conditions -->
            <div v-for="condition in myConditions" :key="condition.id" class="mb-2">
              <div class="flex items-center gap-1 bg-gray-50 rounded p-1.5">
                <span class="text-[10px] text-gray-500 truncate max-w-[70px]">
                  {{ getColumnLabel(condition.column) }}
                </span>
                <span class="text-[10px] text-gray-400">
                  {{ getOperatorLabel(condition.operator) }}
                </span>
                <span class="text-[10px] text-gray-700 font-medium truncate max-w-[80px]">
                  {{ formatValue(condition) }}
                </span>
                <button
                  type="button"
                  class="ml-auto p-0.5 hover:bg-gray-200 rounded text-gray-400 hover:text-red-500"
                  @click="removeCondition(condition.id)"
                >
                  <Icon name="heroicons:x-mark" class="w-3 h-3" />
                </button>
              </div>
            </div>

            <!-- Add new condition -->
            <div class="border border-dashed border-gray-200 rounded p-2 mt-2">
              <!-- Column select -->
              <USelectMenu
                v-model="newCondition.column"
                :options="columnOptions"
                placeholder="Column"
                size="xs"
                value-attribute="value"
                option-attribute="label"
                class="mb-1.5"
                :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                :ui-menu="{ height: 'max-h-40', option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                @update:model-value="onColumnChange"
              />

              <!-- Operator select -->
              <USelectMenu
                v-model="newCondition.operator"
                :options="operatorOptions"
                placeholder="Operator"
                size="xs"
                value-attribute="value"
                option-attribute="label"
                class="mb-1.5"
                :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                :ui-menu="{ option: { size: 'text-xs', padding: 'py-1 px-2' } }"
              />

              <!-- Value input -->
              <template v-if="!noValueOperators.includes(newCondition.operator)">
                <USelectMenu
                  v-if="shouldShowValueSelect"
                  v-model="newCondition.value"
                  :options="valueOptions"
                  placeholder="Value"
                  size="xs"
                  value-attribute="value"
                  option-attribute="label"
                  class="mb-1.5"
                  searchable
                  searchable-placeholder="Search..."
                  :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                  :ui-menu="{ height: 'max-h-40', option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                />
                <UInput
                  v-else-if="selectedColumnType === 'number'"
                  v-model="newCondition.value"
                  type="number"
                  placeholder="Value"
                  size="xs"
                  class="mb-1.5"
                />
                <UInput
                  v-else-if="selectedColumnType === 'date'"
                  v-model="newCondition.value"
                  type="date"
                  size="xs"
                  class="mb-1.5"
                />
                <UInput
                  v-else
                  v-model="newCondition.value"
                  type="text"
                  placeholder="Value"
                  size="xs"
                  class="mb-1.5"
                />
              </template>

              <!-- Add button -->
              <UButton
                size="xs"
                color="blue"
                variant="soft"
                class="w-full"
                :disabled="!canAdd"
                @click="addCondition"
              >
                Add Filter
              </UButton>
            </div>
          </div>
        </div>

        <!-- Footer with row count -->
        <div v-if="filterCount > 0" class="px-3 py-1.5 border-t border-gray-100 text-[10px] text-gray-400">
          {{ filteredRowCount }} of {{ totalRowCount }} rows
        </div>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import {
  parseColumnKey,
  formatColumnLabel,
  inferColumnType,
  getOperatorsForType,
  generateFilterId,
  type FilterCondition,
  type FilterGroup
} from '~/composables/useSharedFilters'

const props = defineProps<{
  reportId: string
  visualizationId: string
  rows: any[]
  columns?: Array<{ field: string; [key: string]: any }>
}>()

// Local filter state - synced via events
const filters = ref<FilterGroup[]>([])
const filterInstanceId = `vizfilter-${props.visualizationId}-${Date.now()}`

// Broadcast filter changes
function setFilters(newFilters: FilterGroup[]) {
  filters.value = JSON.parse(JSON.stringify(newFilters))
  if (props.reportId) {
    window.dispatchEvent(new CustomEvent('filter:updated', {
      detail: {
        reportId: props.reportId,
        filters: filters.value,
        source: filterInstanceId
      }
    }))
  }
}

// Listen for external filter changes
function handleFilterUpdate(ev: Event) {
  const detail = (ev as CustomEvent).detail
  if (!detail || detail.source === filterInstanceId) return
  if (props.reportId && detail.reportId !== props.reportId) return
  filters.value = JSON.parse(JSON.stringify(detail.filters || []))
}

onMounted(() => {
  window.addEventListener('filter:updated', handleFilterUpdate)
})

onUnmounted(() => {
  window.removeEventListener('filter:updated', handleFilterUpdate)
})

// Local state
const isOpen = ref(false)
const newCondition = ref({
  column: '',
  operator: 'equals',
  value: ''
})

// Operators that don't need a value
const noValueOperators = ['is_empty', 'is_not_empty', 'is_true', 'is_false']

// Computed: filter count for this visualization
const filterCount = computed(() => {
  let count = 0
  for (const group of filters.value) {
    for (const cond of group.conditions) {
      const { vizId } = parseColumnKey(cond.column)
      if (vizId === props.visualizationId) count++
    }
  }
  return count
})

// Computed: conditions for this visualization
const myConditions = computed(() => {
  const result: FilterCondition[] = []
  for (const group of filters.value) {
    for (const cond of group.conditions) {
      const { vizId } = parseColumnKey(cond.column)
      if (vizId === props.visualizationId) {
        result.push(cond)
      }
    }
  }
  return result
})

// Discover columns from rows
const discoveredColumns = computed(() => {
  const cols: Array<{
    key: string
    name: string
    label: string
    type: 'string' | 'number' | 'date' | 'boolean'
    uniqueValues: any[]
  }> = []

  if (!props.rows?.length) return cols

  const sampleRow = props.rows[0]
  const keys = Object.keys(sampleRow || {})

  for (const key of keys) {
    const values = props.rows.slice(0, 100).map(r => r[key]).filter(v => v != null)
    const uniqueVals = [...new Set(values)].slice(0, 50)

    cols.push({
      key,
      name: key,
      label: formatColumnLabel(key),
      type: inferColumnType(values),
      uniqueValues: uniqueVals
    })
  }

  return cols.sort((a, b) => a.label.localeCompare(b.label))
})

// Column options for select
const columnOptions = computed(() =>
  discoveredColumns.value.map(col => ({
    label: col.label,
    value: col.key
  }))
)

// Get selected column info
const selectedColumn = computed(() =>
  discoveredColumns.value.find(c => c.key === newCondition.value.column)
)

const selectedColumnType = computed(() => selectedColumn.value?.type || 'string')

// Operator options based on column type
const operatorOptions = computed(() => getOperatorsForType(selectedColumnType.value))

// Value options for select (low cardinality string columns)
const valueOptions = computed(() => {
  if (!selectedColumn.value) return []
  return selectedColumn.value.uniqueValues.map(v => ({
    label: String(v),
    value: v
  }))
})

// Should show value select dropdown
const shouldShowValueSelect = computed(() => {
  if (!['equals', 'not_equals'].includes(newCondition.value.operator)) return false
  if (!selectedColumn.value) return false
  if (selectedColumn.value.type === 'number' || selectedColumn.value.type === 'date') return false
  return selectedColumn.value.uniqueValues.length > 0 && selectedColumn.value.uniqueValues.length <= 50
})

// Can add new condition
const canAdd = computed(() => {
  if (!newCondition.value.column) return false
  if (!newCondition.value.operator) return false
  if (!noValueOperators.includes(newCondition.value.operator) && !newCondition.value.value && newCondition.value.value !== 0) {
    return false
  }
  return true
})

// Row counts
const totalRowCount = computed(() => props.rows?.length || 0)
const filteredRowCount = computed(() => {
  if (!filters.value.length) return totalRowCount.value
  const rows = props.rows || []
  return rows.filter((row: any) => {
    // Simple inline evaluation for this viz
    return filters.value.some(group =>
      group.conditions.every(cond => {
        const { vizId, columnName } = parseColumnKey(cond.column)
        if (vizId !== props.visualizationId) return true
        const value = row[columnName]
        const target = cond.value
        switch (cond.operator) {
          case 'equals': return String(value).toLowerCase() === String(target).toLowerCase()
          case 'not_equals': return String(value).toLowerCase() !== String(target).toLowerCase()
          case 'contains': return String(value).toLowerCase().includes(String(target).toLowerCase())
          case 'greater_than': return Number(value) > Number(target)
          case 'less_than': return Number(value) < Number(target)
          case 'is_empty': return value == null || value === ''
          case 'is_not_empty': return value != null && value !== ''
          default: return true
        }
      })
    )
  }).length
})

// Reset operator and value when column changes
function onColumnChange() {
  const type = selectedColumnType.value
  const operators = getOperatorsForType(type)
  newCondition.value.operator = operators[0]?.value || 'equals'
  newCondition.value.value = ''
}

// Get column label from full key (vizId:columnName)
function getColumnLabel(fullKey: string): string {
  const { columnName } = parseColumnKey(fullKey)
  const col = discoveredColumns.value.find(c => c.key === columnName)
  return col?.label || formatColumnLabel(columnName)
}

// Get operator display label
function getOperatorLabel(op: string): string {
  const allOps = [
    { value: 'equals', label: '=' },
    { value: 'not_equals', label: '≠' },
    { value: 'contains', label: '∋' },
    { value: 'not_contains', label: '∌' },
    { value: 'starts_with', label: 'starts' },
    { value: 'ends_with', label: 'ends' },
    { value: 'greater_than', label: '>' },
    { value: 'less_than', label: '<' },
    { value: 'gte', label: '≥' },
    { value: 'lte', label: '≤' },
    { value: 'before', label: '<' },
    { value: 'after', label: '>' },
    { value: 'is_empty', label: 'empty' },
    { value: 'is_not_empty', label: 'not empty' },
    { value: 'is_true', label: 'true' },
    { value: 'is_false', label: 'false' },
  ]
  return allOps.find(o => o.value === op)?.label || op
}

// Format condition value for display
function formatValue(condition: FilterCondition): string {
  if (noValueOperators.includes(condition.operator)) return ''
  const val = condition.value
  if (val === null || val === undefined) return ''
  const str = String(val)
  return str.length > 15 ? str.slice(0, 15) + '…' : str
}

// Add new condition
function addCondition() {
  if (!canAdd.value) return

  const condition: FilterCondition = {
    id: generateFilterId(),
    column: `${props.visualizationId}:${newCondition.value.column}`,
    operator: newCondition.value.operator,
    value: newCondition.value.value
  }

  // Find existing group for this viz or create new
  const newFilters = JSON.parse(JSON.stringify(filters.value))
  const existingGroupIdx = newFilters.findIndex((g: any) =>
    g.conditions.some((c: any) => parseColumnKey(c.column).vizId === props.visualizationId)
  )

  if (existingGroupIdx >= 0) {
    newFilters[existingGroupIdx].conditions.push(condition)
  } else {
    newFilters.push({
      id: generateFilterId(),
      conditions: [condition]
    })
  }

  setFilters(newFilters)

  // Reset form
  newCondition.value.value = ''
}

// Remove a condition
function removeCondition(conditionId: string) {
  const newFilters = filters.value
    .map(group => ({
      ...group,
      conditions: group.conditions.filter(c => c.id !== conditionId)
    }))
    .filter(g => g.conditions.length > 0)

  setFilters(newFilters)
}

// Clear all filters for this visualization
function clearFilters() {
  const newFilters = filters.value
    .map(group => ({
      ...group,
      conditions: group.conditions.filter(c => {
        const { vizId } = parseColumnKey(c.column)
        return vizId !== props.visualizationId
      })
    }))
    .filter(g => g.conditions.length > 0)

  setFilters(newFilters)
}

// Set default column when opening if not set
watch(isOpen, (open) => {
  if (open && !newCondition.value.column && columnOptions.value.length > 0) {
    newCondition.value.column = columnOptions.value[0].value
    onColumnChange()
  }
})
</script>

