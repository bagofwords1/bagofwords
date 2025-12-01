import { ref, computed, onMounted, onUnmounted } from 'vue'

// ==================== Types ====================

export interface FilterCondition {
  id: string
  column: string  // Format: "vizId:columnName"
  operator: string
  value: any
  value2?: any
}

export interface FilterGroup {
  id: string
  conditions: FilterCondition[]
}

// ==================== Utilities ====================

export function generateFilterId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
}

export function parseColumnKey(key: string): { vizId: string; columnName: string } {
  const [vizId, ...rest] = (key || '').split(':')
  return { vizId, columnName: rest.join(':') }
}

export function formatColumnLabel(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .split(' ')
    .map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(' ')
}

export function inferColumnType(values: any[]): 'string' | 'number' | 'date' | 'boolean' {
  if (!values.length) return 'string'
  
  const sample = values.filter(v => v != null).slice(0, 20)
  if (!sample.length) return 'string'
  
  if (sample.every(v => typeof v === 'boolean' || v === 'true' || v === 'false')) {
    return 'boolean'
  }
  
  if (sample.every(v => typeof v === 'number' || (!isNaN(parseFloat(v)) && isFinite(v)))) {
    return 'number'
  }
  
  const datePatterns = [/^\d{4}-\d{2}-\d{2}/, /^\d{2}\/\d{2}\/\d{4}/, /^\d{4}\/\d{2}\/\d{2}/]
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

// ==================== Operators ====================

export const stringOperators = [
  { label: 'equals', value: 'equals' },
  { label: 'not equals', value: 'not_equals' },
  { label: 'contains', value: 'contains' },
  { label: 'not contains', value: 'not_contains' },
  { label: 'starts with', value: 'starts_with' },
  { label: 'ends with', value: 'ends_with' },
  { label: 'is empty', value: 'is_empty' },
  { label: 'is not empty', value: 'is_not_empty' },
]

export const numberOperators = [
  { label: 'equals', value: 'equals' },
  { label: 'not equals', value: 'not_equals' },
  { label: '>', value: 'greater_than' },
  { label: '<', value: 'less_than' },
  { label: '≥', value: 'gte' },
  { label: '≤', value: 'lte' },
  { label: 'is empty', value: 'is_empty' },
  { label: 'is not empty', value: 'is_not_empty' },
]

export const dateOperators = [
  { label: 'equals', value: 'equals' },
  { label: 'before', value: 'before' },
  { label: 'after', value: 'after' },
  { label: 'is empty', value: 'is_empty' },
  { label: 'is not empty', value: 'is_not_empty' },
]

export const booleanOperators = [
  { label: 'is true', value: 'is_true' },
  { label: 'is false', value: 'is_false' },
]

export function getOperatorsForType(type: string) {
  switch (type) {
    case 'number': return numberOperators
    case 'date': return dateOperators
    case 'boolean': return booleanOperators
    default: return stringOperators
  }
}

// ==================== Filter Evaluation ====================

export function evaluateCondition(row: any, condition: FilterCondition, targetVizId?: string): boolean {
  const { vizId: condVizId, columnName } = parseColumnKey(condition.column)
  
  // Skip conditions for other visualizations
  if (targetVizId && condVizId !== targetVizId) {
    return true
  }
  
  // Case-insensitive column lookup
  const columnKey = Object.keys(row).find(k => k.toLowerCase() === columnName.toLowerCase())
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

export function evaluateFilters(row: any, groups: FilterGroup[], targetVizId?: string): boolean {
  if (!groups.length) return true
  
  // Check if any condition applies to this visualization
  const hasRelevantConditions = groups.some(group =>
    group.conditions.some(cond => {
      const { vizId } = parseColumnKey(cond.column)
      return vizId === targetVizId
    })
  )
  
  // If no conditions apply, don't filter
  if (targetVizId && !hasRelevantConditions) {
    return true
  }
  
  // OR across groups, AND within group
  return groups.some(group =>
    group.conditions.every(cond => evaluateCondition(row, cond, targetVizId))
  )
}

// ==================== Shared Filters Composable ====================

const EVENT_NAME = 'filter:updated'

export function useSharedFilters(reportId: string) {
  const filters = ref<FilterGroup[]>([])
  const instanceId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  
  // Set filters and broadcast to other components
  function setFilters(newFilters: FilterGroup[]) {
    filters.value = JSON.parse(JSON.stringify(newFilters))
    window.dispatchEvent(new CustomEvent(EVENT_NAME, {
      detail: { reportId, filters: filters.value, source: instanceId }
    }))
  }
  
  // Get filters for a specific visualization
  function getFiltersForViz(vizId: string): FilterGroup[] {
    return filters.value
      .map(group => ({
        ...group,
        conditions: group.conditions.filter(c => {
          const { vizId: condVizId } = parseColumnKey(c.column)
          return condVizId === vizId
        })
      }))
      .filter(g => g.conditions.length > 0)
  }
  
  // Count active filters for a visualization
  function getFilterCountForViz(vizId: string): number {
    return filters.value.reduce((count, group) => {
      return count + group.conditions.filter(c => {
        const { vizId: condVizId } = parseColumnKey(c.column)
        return condVizId === vizId
      }).length
    }, 0)
  }
  
  // Add a condition for a specific visualization
  function addConditionForViz(vizId: string, columnName: string, operator: string, value: any) {
    const condition: FilterCondition = {
      id: generateFilterId(),
      column: `${vizId}:${columnName}`,
      operator,
      value
    }
    
    // Find existing group with conditions for this viz, or create new group
    const existingGroup = filters.value.find(g =>
      g.conditions.some(c => parseColumnKey(c.column).vizId === vizId)
    )
    
    if (existingGroup) {
      existingGroup.conditions.push(condition)
      setFilters([...filters.value])
    } else {
      const newGroup: FilterGroup = {
        id: generateFilterId(),
        conditions: [condition]
      }
      setFilters([...filters.value, newGroup])
    }
  }
  
  // Remove a condition by ID
  function removeCondition(conditionId: string) {
    const newFilters = filters.value
      .map(group => ({
        ...group,
        conditions: group.conditions.filter(c => c.id !== conditionId)
      }))
      .filter(g => g.conditions.length > 0)
    
    setFilters(newFilters)
  }
  
  // Clear all filters for a visualization
  function clearFiltersForViz(vizId: string) {
    const newFilters = filters.value
      .map(group => ({
        ...group,
        conditions: group.conditions.filter(c => {
          const { vizId: condVizId } = parseColumnKey(c.column)
          return condVizId !== vizId
        })
      }))
      .filter(g => g.conditions.length > 0)
    
    setFilters(newFilters)
  }
  
  // Clear all filters
  function clearAllFilters() {
    setFilters([])
  }
  
  // Apply filters to rows
  function filterRows(rows: any[], vizId: string): any[] {
    if (!filters.value.length) return rows
    return rows.filter(row => evaluateFilters(row, filters.value, vizId))
  }
  
  // Listen for external filter updates
  function onFilterEvent(ev: Event) {
    const detail = (ev as CustomEvent).detail
    if (!detail) return
    if (detail.reportId === reportId && detail.source !== instanceId) {
      filters.value = JSON.parse(JSON.stringify(detail.filters || []))
    }
  }
  
  onMounted(() => {
    window.addEventListener(EVENT_NAME, onFilterEvent)
  })
  
  onUnmounted(() => {
    window.removeEventListener(EVENT_NAME, onFilterEvent)
  })
  
  // Computed helpers
  const hasFilters = computed(() => filters.value.length > 0)
  const totalFilterCount = computed(() =>
    filters.value.reduce((sum, g) => sum + g.conditions.length, 0)
  )
  
  return {
    filters,
    setFilters,
    getFiltersForViz,
    getFilterCountForViz,
    addConditionForViz,
    removeCondition,
    clearFiltersForViz,
    clearAllFilters,
    filterRows,
    hasFilters,
    totalFilterCount,
    evaluateFilters: (row: any, vizId?: string) => evaluateFilters(row, filters.value, vizId)
  }
}

