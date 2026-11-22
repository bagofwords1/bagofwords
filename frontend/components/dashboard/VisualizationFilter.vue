<template>
  <!-- UPopover's model prop is `open`, not `modelValue` — a bare v-model binds nothing,
       leaving isOpen permanently false (no sync on open, no close on apply). -->
  <!-- Popper's "-end" is the PHYSICAL right edge, in RTL too: there the
       funnel sits at the panel's left, so "bottom-end" grew the 380px popup
       further left, past the panel, where it was cut off. Open it toward the
       inside instead — "bottom-start" (left-aligned) in RTL. -->
  <UPopover v-model:open="isOpen" mode="click" :popper="popperOptions">
    <!-- Trigger: Clean funnel icon with badge -->
    <UTooltip :text="t('vizFilter.filter')">
      <UChip v-if="activeFilterCount > 0" :text="activeFilterCount" size="2xl" color="blue">
        <button
          type="button"
          class="relative p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors text-blue-500"
        >
          <Icon name="heroicons:funnel" class="w-3.5 h-3.5" />
        </button>
      </UChip>
      <button
        v-else
        type="button"
        class="relative p-1 hover:bg-gray-100 dark:hover:bg-gray-700 rounded transition-colors text-ink-muted hover:text-ink-2"
      >
        <Icon name="heroicons:funnel" class="w-3.5 h-3.5" />
      </button>
    </UTooltip>

    <!-- Popover Panel -->
    <template #panel>
      <div class="w-[380px] max-w-[95vw]">
        <!-- Header -->
        <div class="flex items-center justify-between px-3 py-2 border-b border-gray-200 dark:border-gray-700">
          <span class="font-medium text-sm text-gray-700 dark:text-gray-300">{{ t('vizFilter.title') }}</span>
          <UButton
            v-if="hasActiveFilters"
            color="gray"
            variant="ghost"
            size="xs"
            @click="clearFilters"
          >
            {{ t('vizFilter.clear') }}
          </UButton>
        </div>

        <!-- Content -->
        <div class="p-3 max-h-[320px] overflow-y-auto">
          <!-- No columns available -->
          <div v-if="discoveredColumns.length === 0" class="text-center py-6">
            <p class="text-xs text-gray-500 dark:text-gray-400">{{ t('vizFilter.noData') }}</p>
          </div>

          <!-- Empty State - has columns but no filters -->
          <div v-else-if="filterGroups.length === 0" class="text-center py-6">
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">{{ t('vizFilter.noFilters') }}</p>
            <UButton size="xs" color="blue" @click="addGroup">
              {{ t('vizFilter.addFilter') }}
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
                <div class="flex-1 h-px bg-gray-300 dark:bg-gray-600"></div>
                <span class="text-2xs font-semibold text-orange-500">{{ t('vizFilter.or') }}</span>
                <div class="flex-1 h-px bg-gray-300 dark:bg-gray-600"></div>
              </div>

              <!-- Group Card -->
              <div class="bg-gray-50 dark:bg-gray-900 rounded p-2">
                <!-- Conditions -->
                <div
                  v-for="(condition, condIndex) in group.conditions"
                  :key="condition.id"
                  class="mb-2 last:mb-0"
                >
                  <div v-if="condIndex > 0" class="text-2xs font-semibold text-blue-500 mb-1">{{ t('vizFilter.and') }}</div>

                  <div class="flex items-center gap-1.5">
                    <!-- Column Select -->
                    <USelectMenu
                      v-model="condition.column"
                      :options="columnOptions"
                      :placeholder="t('vizFilter.column')"
                      size="xs"
                      value-attribute="value"
                      option-attribute="label"
                      class="w-[110px]"
                      searchable
                      :searchable-placeholder="t('dataGrid.search')"
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
                      class="w-[104px]"
                      :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                      :ui-menu="{ option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                      @update:model-value="onOperatorChange(condition)"
                    />

                    <!-- Value Input -->
                    <template v-if="!noValueOperators.includes(condition.operator)">
                      <!-- Between: two inputs -->
                      <template v-if="condition.operator === 'between'">
                        <UInput
                          v-model="condition.value"
                          :type="getColumnType(condition.column) === 'date' ? 'date' : 'number'"
                          :placeholder="t('dataGrid.inRangeStart')"
                          size="xs"
                          class="w-[70px]"
                        />
                        <span class="text-2xs text-ink-muted">-</span>
                        <UInput
                          v-model="condition.value2"
                          :type="getColumnType(condition.column) === 'date' ? 'date' : 'number'"
                          :placeholder="t('dataGrid.inRangeEnd')"
                          size="xs"
                          class="w-[70px]"
                        />
                      </template>
                      <!-- One of / not one of: what a table header's value list
                           writes; any number of values. -->
                      <USelectMenu
                        v-else-if="multiValueOperators.includes(condition.operator)"
                        v-model="condition.value"
                        :options="getAllValueOptions(condition)"
                        multiple
                        :placeholder="t('vizFilter.value')"
                        size="xs"
                        value-attribute="value"
                        option-attribute="label"
                        class="flex-1 min-w-[80px]"
                        searchable
                        :searchable-placeholder="t('dataGrid.search')"
                        :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                        :ui-menu="{ height: 'max-h-48', option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                      >
                        <template #label>
                          <span class="truncate" dir="auto">{{ multiValueLabel(condition) }}</span>
                        </template>
                      </USelectMenu>
                      <template v-else>
                        <USelectMenu
                          v-if="shouldShowSelect(condition)"
                          v-model="condition.value"
                          :options="getValueOptions(condition.column)"
                          :placeholder="t('vizFilter.value')"
                          size="xs"
                          value-attribute="value"
                          option-attribute="label"
                          class="flex-1 min-w-[80px]"
                          searchable
                          :searchable-placeholder="t('dataGrid.search')"
                          :popper="{ strategy: 'fixed', placement: 'bottom-start' }"
                          :ui-menu="{ height: 'max-h-48', option: { size: 'text-xs', padding: 'py-1 px-2' } }"
                        />
                        <UInput
                          v-else-if="getColumnType(condition.column) === 'number'"
                          v-model="condition.value"
                          type="number"
                          :placeholder="t('vizFilter.value')"
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
                          :placeholder="t('vizFilter.value')"
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
                <div class="flex items-center gap-2 pt-2 mt-2 border-t border-gray-200 dark:border-gray-700">
                  <UButton
                    color="gray"
                    variant="ghost"
                    size="xs"
                    icon="i-heroicons-plus"
                    @click="addCondition(group)"
                  >
                    {{ t('vizFilter.and') }}
                  </UButton>
                  <UButton
                    v-if="groupIndex === filterGroups.length - 1"
                    color="orange"
                    variant="ghost"
                    size="xs"
                    @click="addGroup"
                  >
                    {{ t('vizFilter.or') }}
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

        <!-- Footer with row count and apply. Also shown when the working copy is empty
             but filters are still applied, so removing the last condition can be committed. -->
        <div v-if="filterGroups.length > 0 || hasActiveFilters" class="px-3 py-2 border-t border-gray-200 dark:border-gray-700 flex items-center justify-between">
          <span class="text-xs text-gray-500 dark:text-gray-400" dir="auto">
            {{ t('dataGrid.filteredRows', { shown: filteredRowCount, total: totalRowCount }) }}
          </span>
          <UButton size="xs" color="blue" @click="applyFilters">
            {{ t('vizFilter.apply') }}
          </UButton>
        </div>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { localeTextDir } from '~/utils/textDirection'
import { BLANK_KEY, COLUMN_ORIGIN, collectValues, valueKey } from '~/utils/valueFilter'
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

const { t, locale: uiLocale } = useI18n()
const popperOptions = computed(() => ({
  placement: localeTextDir(String(uiLocale.value)) === 'rtl' ? 'bottom-start' : 'bottom-end',
  strategy: 'fixed',
  modifiers: [{ name: 'preventOverflow', options: { boundary: 'viewport' } }],
}))

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
// Operators that take a list of values (what a table header's value list writes)
const multiValueOperators = ['in', 'not_in']
// What a header condition can be; edited into anything else here, it stops
// being the header's own condition.
const COLUMN_OPERATORS = ['in', 'not_in', 'between', 'gte', 'lte']

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

// Sync local filterGroups from shared filters (extract this viz's conditions).
// Shared state stores columns as "vizId:columnName"; the working copy uses the bare
// column name (that's what columnOptions holds), so strip the prefix on the way in —
// the mirror of what applyFilters adds on the way out. Without this the round-trip
// leaves the column select empty and the condition matches nothing.
function syncFilterGroupsFromShared() {
  const myGroups: FilterGroup[] = []
  for (const group of filters.value) {
    const myConditions = group.conditions
      .filter(c => parseColumnKey(c.column).vizId === props.visualizationId)
      .map(c => ({ ...JSON.parse(JSON.stringify(c)), column: parseColumnKey(c.column).columnName }))
    if (myConditions.length > 0) {
      myGroups.push({
        id: group.id,
        conditions: myConditions
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
    // Type inference is fine on a small sample
    const sampleValues = props.rows.slice(0, 100).map(r => r[key]).filter(v => v != null)

    // Unique values: scan all rows so the dropdown isn't blind to values past row 100.
    // Early-exit at 51 — beyond shouldShowSelect's 50-cap the UI falls back to a text input,
    // so we don't need to keep accumulating.
    const valueSet = new Set<any>()
    for (const row of props.rows) {
      const v = row[key]
      if (v == null) continue
      valueSet.add(v)
      if (valueSet.size > 50) break
    }

    cols.push({
      key,
      name: key,
      label: formatColumnLabel(key),
      type: inferColumnType(sampleValues),
      uniqueValues: [...valueSet]
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

// Get operators for column type — labels in the viewer's language (symbols
// such as > stay as they are), plus "is one of" / "is not one of".
const SYMBOL_OPERATORS = ['greater_than', 'less_than', 'gte', 'lte']
const opLabel = (op: { label: string; value: string }) =>
  SYMBOL_OPERATORS.includes(op.value) ? op.label : t(`vizFilter.op.${op.value}`)
function getOperatorsForColumn(columnKey: string) {
  const type = getColumnType(columnKey)
  const base = type === 'number' ? numberOperators
    : type === 'date' ? dateOperators
    : type === 'boolean' ? booleanOperators
    : stringOperators
  const extra = type === 'boolean' ? [] : [
    ...(type === 'number' ? [{ label: 'between', value: 'between' }] : []),
    { label: 'in', value: 'in' },
    { label: 'not in', value: 'not_in' },
  ]
  const list = [...base, ...extra]
  return list.map(op => ({ value: op.value, label: opLabel(op) }))
}

// Every distinct value of a column (not capped at 50 like the single-value
// select): a header may have picked any of them. Values already chosen stay
// selectable even if a re-run dropped them.
function getAllValueOptions(condition: FilterCondition) {
  const values = collectValues((props.rows || []).map((r: any) => r?.[condition.column])).slice(0, 1000)
  const opts = values.map(o => ({
    value: o.key === BLANK_KEY ? '' : o.raw,
    label: o.key === BLANK_KEY ? t('dataGrid.blankValue') : String(o.raw),
  }))
  const have = new Set(values.map(o => o.key))
  for (const v of Array.isArray(condition.value) ? condition.value : []) {
    if (!have.has(valueKey(v))) opts.push({ value: v, label: String(v) })
  }
  return opts
}

function multiValueLabel(condition: FilterCondition) {
  const values = Array.isArray(condition.value) ? condition.value : []
  if (!values.length) return t('vizFilter.value')
  const first = values[0] === '' ? t('dataGrid.blankValue') : String(values[0])
  return values.length > 1 ? t('vizFilter.valuesMore', { first, n: values.length - 1 }) : first
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
  delete (condition as any).origin
}

// A list operator holds an array of values, every other one a single value.
function onOperatorChange(condition: FilterCondition) {
  const multi = multiValueOperators.includes(condition.operator)
  if (multi && !Array.isArray(condition.value)) {
    condition.value = condition.value === '' || condition.value == null ? [] : [condition.value]
  } else if (!multi && Array.isArray(condition.value)) {
    condition.value = condition.value[0] ?? ''
  }
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
    const newConditions = group.conditions.map(c => {
      const next: any = { ...c, column: `${props.visualizationId}:${c.column}` }
      if (next.origin === COLUMN_ORIGIN && !COLUMN_OPERATORS.includes(next.operator)) delete next.origin
      return next
    })

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
