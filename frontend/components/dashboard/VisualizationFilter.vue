<template>
  <UPopover v-model="isOpen" mode="click" :popper="{ placement: 'bottom-end', strategy: 'fixed', modifiers: [{ name: 'preventOverflow', options: { boundary: 'viewport' } }] }">
    <!-- Trigger: Clean funnel icon with badge -->
    <UTooltip text="Filter">
      <UChip v-if="activeFilterCount > 0" :text="activeFilterCount" size="2xl" color="blue">
        <button
          type="button"
          class="relative p-1 hover:bg-gray-100 rounded transition-colors text-blue-500"
        >
          <Icon name="heroicons:funnel" class="w-3.5 h-3.5" />
        </button>
      </UChip>
      <button
        v-else
        type="button"
        class="relative p-1 hover:bg-gray-100 rounded transition-colors text-gray-400 hover:text-gray-600"
      >
        <Icon name="heroicons:funnel" class="w-3.5 h-3.5" />
      </button>
    </UTooltip>

    <!-- Popover Panel -->
    <template #panel>
      <div class="w-[380px] max-w-[95vw]">
        <!-- Header -->
        <div class="flex items-center justify-between px-3 py-2 border-b border-gray-200">
          <span class="font-medium text-sm text-gray-700">Filters</span>
          <UButton
            v-if="hasActiveFilters"
            color="gray"
            variant="ghost"
            size="xs"
            @click="clearFilters"
          >
            Clear
          </UButton>
        </div>

        <!-- Content -->
        <div class="p-3 max-h-[320px] overflow-y-auto">
          <!-- No columns available -->
          <div v-if="discoveredColumns.length === 0" class="text-center py-6">
            <p class="text-xs text-gray-500">No data available to filter</p>
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
                      class="w-[120px]"
                      searchable
                      searchable-placeholder="Search..."
                      :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                      :ui-menu="{ height: 'max-h-48', option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                      @update:model-value="onColumnChange(condition)"
                    />

                    <!-- Operator Select -->
                    <USelectMenu
                      v-model="condition.operator"
                      :options="getOperatorsForColumn(condition.column)"
                      size="xs"
                      value-attribute="value"
                      option-attribute="label"
                      class="w-[90px]"
                      :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                      :ui-menu="{ option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                    />

                    <!-- Value Input -->
                    <template v-if="!noValueOperators.includes(condition.operator)">
                      <!-- Between: two inputs -->
                      <template v-if="condition.operator === 'between'">
                        <UInput
                          v-model="condition.value"
                          :type="getColumnType(condition.column) === 'date' ? 'date' : 'number'"
                          placeholder="From"
                          size="xs"
                          class="w-[70px]"
                        />
                        <span class="text-[10px] text-gray-400">-</span>
                        <UInput
                          v-model="condition.value2"
                          :type="getColumnType(condition.column) === 'date' ? 'date' : 'number'"
                          placeholder="To"
                          size="xs"
                          class="w-[70px]"
                        />
                      </template>
                      <!-- Regular inputs -->
                      <template v-else>
                        <USelectMenu
                          v-if="shouldShowSelect(condition)"
                          v-model="condition.value"
                          :options="getValueOptions(condition.column)"
                          placeholder="Value"
                          size="xs"
                          value-attribute="value"
                          option-attribute="label"
                          class="flex-1 min-w-[80px]"
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
                          class="flex-1 min-w-[80px]"
                        />
                        <UInput
                          v-else-if="getColumnType(condition.column) === 'date'"
                          v-model="condition.value"
                          type="date"
                          size="xs"
                          class="flex-1 min-w-[80px]"
                        />
                        <UInput
                          v-else
                          v-model="condition.value"
                          type="text"
                          placeholder="Value"
                          size="xs"
                          class="flex-1 min-w-[80px]"
                        />
                      </template>
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

        <!-- Footer with row count and apply -->
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
import {
  parseColumnKey,
  formatColumnLabel,
  inferColumnType,
  generateFilterId,
  evaluateFilters as sharedEvaluateFilters,
  stringOperators,
  numberOperators,
  dateOperators,
  booleanOperators,
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
const filterGroups = ref<FilterGroup[]>([])  // Working copy for editing
const filterInstanceId = `vizfilter-${props.visualizationId}-${Date.now()}`

// Operators that don't need a value
const noValueOperators = ['is_empty', 'is_not_empty', 'is_true', 'is_false']

// Local state
const isOpen = ref(false)

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
  // Update working copy to reflect external changes (only for this viz's conditions)
  syncFilterGroupsFromShared()
}

// Sync local filterGroups from shared filters (extract this viz's conditions)
function syncFilterGroupsFromShared() {
  const myGroups: FilterGroup[] = []
  for (const group of filters.value) {
    const myConditions = group.conditions.filter(c => {
      const { vizId } = parseColumnKey(c.column)
      return vizId === props.visualizationId
    })
    if (myConditions.length > 0) {
      myGroups.push({
        id: group.id,
        conditions: JSON.parse(JSON.stringify(myConditions))
      })
    }
  }
  filterGroups.value = myGroups
}

onMounted(() => {
  window.addEventListener('filter:updated', handleFilterUpdate)
})

onUnmounted(() => {
  window.removeEventListener('filter:updated', handleFilterUpdate)
})

// Computed: filter count for this visualization
const activeFilterCount = computed(() => {
  let count = 0
  for (const group of filters.value) {
    for (const cond of group.conditions) {
      const { vizId } = parseColumnKey(cond.column)
      if (vizId === props.visualizationId) count++
    }
  }
  return count
})

const hasActiveFilters = computed(() => activeFilterCount.value > 0)

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

// Get column info
function getColumn(key: string) {
  return discoveredColumns.value.find(c => c.key === key)
}

function getColumnType(key: string): string {
  return getColumn(key)?.type || 'string'
}

// Get operators for column type
function getOperatorsForColumn(columnKey: string) {
  const type = getColumnType(columnKey)
  switch (type) {
    case 'number': return numberOperators
    case 'date': return dateOperators
    case 'boolean': return booleanOperators
    default: return stringOperators
  }
}

// Value options for select dropdown
function getValueOptions(columnKey: string) {
  const col = getColumn(columnKey)
  if (!col) return []
  
  return col.uniqueValues.map(v => ({
    label: String(v),
    value: v
  }))
}

// Should show select dropdown for value
function shouldShowSelect(condition: FilterCondition): boolean {
  if (!['equals', 'not_equals'].includes(condition.operator)) {
    return false
  }
  
  const col = getColumn(condition.column)
  if (!col) return false
  
  // Never show dropdown for number or date columns
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
  condition.value2 = undefined
}

// Row counts
const totalRowCount = computed(() => props.rows?.length || 0)

const filteredRowCount = computed(() => {
  if (!filterGroups.value.length) return totalRowCount.value
  
  // Create temporary filter groups with proper column format for evaluation
  const evalGroups: FilterGroup[] = filterGroups.value.map(group => ({
    id: group.id,
    conditions: group.conditions.map(c => ({
      ...c,
      column: `${props.visualizationId}:${c.column}`
    }))
  }))
  
  const rows = props.rows || []
  return rows.filter((row: any) => 
    sharedEvaluateFilters(row, evalGroups, props.visualizationId)
  ).length
})

// Filter group management
function addGroup() {
  const defaultColumn = columnOptions.value[0]?.value || ''
  filterGroups.value.push({
    id: generateFilterId(),
    conditions: [{
      id: generateFilterId(),
      column: defaultColumn,
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
  const defaultColumn = columnOptions.value[0]?.value || ''
  group.conditions.push({
    id: generateFilterId(),
    column: defaultColumn,
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

// Apply filters
function applyFilters() {
  // Build new shared filter state
  // First, remove all conditions for this visualization from existing filters
  let newFilters: FilterGroup[] = filters.value
    .map(group => ({
      ...group,
      conditions: group.conditions.filter(c => {
        const { vizId } = parseColumnKey(c.column)
        return vizId !== props.visualizationId
      })
    }))
    .filter(g => g.conditions.length > 0)
  
  // Then add the new conditions with proper column format (vizId:columnName)
  for (const group of filterGroups.value) {
    const newConditions = group.conditions.map(c => ({
      ...c,
      column: `${props.visualizationId}:${c.column}`
    }))
    
    // Find existing group to merge into, or create new
    const existingGroup = newFilters.find(g => g.id === group.id)
    if (existingGroup) {
      existingGroup.conditions.push(...newConditions)
    } else {
      newFilters.push({
        id: group.id,
        conditions: newConditions
      })
    }
  }
  
  setFilters(newFilters)
  isOpen.value = false
}

// Clear all filters for this visualization
function clearFilters() {
  filterGroups.value = []
  
  // Also clear from shared state
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

// Set default column when opening if no groups exist
watch(isOpen, (open) => {
  if (open) {
    // Sync from shared state when opening
    syncFilterGroupsFromShared()
  }
})
</script>
