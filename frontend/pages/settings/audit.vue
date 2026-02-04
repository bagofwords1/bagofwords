<template>
  <div class="mt-4">
    <div class="mb-4">
      <h2 class="text-sm font-medium text-gray-900">Audit Logs</h2>
      <p class="text-xs text-gray-500 mt-0.5">Track user activity across your workspace</p>
    </div>

    <!-- Enterprise Gate -->
    <template v-if="!hasFeature('audit_logs')">
      <div class="rounded border border-gray-200 p-4 bg-gray-50">
        <p class="text-xs text-gray-600 mb-2">
          Activity logging requires an enterprise license. Track all user actions, changes, and access events for compliance and security monitoring.
        </p>
        <a
          href="https://docs.bagofwords.com/enterprise"
          target="_blank"
          rel="noopener noreferrer"
          class="text-xs text-blue-600 hover:text-blue-700"
        >
          Learn more →
        </a>
      </div>
    </template>

    <!-- Audit Logs Content -->
    <template v-else>
      <!-- Search & Filters -->
      <div class="mb-3 flex items-center gap-2">
        <div class="relative flex-1 max-w-[200px]">
          <input
            v-model="searchQuery"
            type="text"
            placeholder="Search..."
            class="w-full pl-7 pr-2 py-1 text-xs border border-gray-200 rounded focus:outline-none focus:border-gray-400 bg-white"
            @input="debouncedSearch"
          />
          <svg class="absolute left-2 top-1.5 w-3.5 h-3.5 text-gray-400" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
          </svg>
        </div>

        <!-- Multiselect Action Filter -->
        <div class="relative" ref="dropdownRef">
          <button
            type="button"
            class="flex items-center gap-1.5 px-2 py-1 text-xs border border-gray-200 rounded hover:border-gray-300 bg-white"
            @click="showActionDropdown = !showActionDropdown"
          >
            <span class="text-gray-600">
              {{ selectedActions.length === 0 ? 'All actions' : `${selectedActions.length} selected` }}
            </span>
            <svg class="w-3 h-3 text-gray-400" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
            </svg>
          </button>
          <div
            v-if="showActionDropdown"
            class="absolute top-full left-0 mt-1 w-44 bg-white border border-gray-200 rounded shadow-sm z-10"
          >
            <div class="py-1">
              <label
                v-for="action in actionOptions"
                :key="action.value"
                class="flex items-center gap-2 px-2 py-1 hover:bg-gray-50 cursor-pointer"
              >
                <input
                  type="checkbox"
                  :value="action.value"
                  v-model="selectedActions"
                  class="w-3 h-3 rounded border-gray-300 text-gray-900 focus:ring-0"
                  @change="applyFilters"
                />
                <span class="text-xs text-gray-700">{{ action.label }}</span>
              </label>
            </div>
          </div>
        </div>

        <button
          v-if="hasActiveFilters"
          class="text-xs text-gray-400 hover:text-gray-600"
          @click="clearFilters"
        >
          Clear
        </button>
      </div>

      <!-- Loading State -->
      <div v-if="loading" class="py-8 text-center">
        <div class="inline-block w-4 h-4 border-2 border-gray-200 border-t-gray-500 rounded-full animate-spin"></div>
      </div>

      <!-- Error State -->
      <div v-else-if="error" class="py-6 text-center text-xs text-red-500">
        {{ error }}
      </div>

      <!-- Logs List -->
      <div v-else class="border border-gray-200 rounded overflow-hidden">
        <template v-if="logs.length > 0">
          <div
            v-for="(log, idx) in logs"
            :key="log.id"
            class="flex items-center px-3 py-2 text-xs hover:bg-gray-50"
            :class="{ 'border-t border-gray-100': idx > 0 }"
          >
            <!-- Timestamp -->
            <span class="w-20 flex-shrink-0 text-gray-400 font-mono text-[11px]">
              {{ formatTimestamp(log.created_at) }}
            </span>

            <!-- User -->
            <span class="w-36 flex-shrink-0 text-gray-700 truncate" :title="log.user_email || undefined">
              {{ log.user_email || 'System' }}
            </span>

            <!-- Action -->
            <span class="w-24 flex-shrink-0">
              <span class="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium" :class="getActionClass(log.action)">
                {{ formatAction(log.action) }}
              </span>
            </span>

            <!-- Resource -->
            <span class="flex-1 text-gray-500 truncate" :title="log.details?.title">
              <template v-if="log.resource_type">
                {{ log.resource_type }}
                <template v-if="log.details?.title"> · {{ log.details.title }}</template>
              </template>
            </span>

            <!-- IP -->
            <span class="w-28 flex-shrink-0 text-gray-400 font-mono text-[11px] text-right">
              {{ log.ip_address || '' }}
            </span>
          </div>
        </template>

        <!-- Empty State -->
        <div v-else class="py-8 text-center">
          <p class="text-xs text-gray-400">No activity found</p>
        </div>
      </div>

      <!-- Pagination -->
      <div v-if="totalPages > 1" class="mt-2 flex items-center justify-between">
        <span class="text-[11px] text-gray-400">
          {{ (page - 1) * pageSize + 1 }}–{{ Math.min(page * pageSize, total) }} of {{ total }}
        </span>
        <div class="flex items-center gap-0.5">
          <button
            :disabled="page <= 1"
            class="px-1.5 py-0.5 text-[11px] text-gray-500 hover:text-gray-700 disabled:text-gray-300 disabled:cursor-not-allowed"
            @click="prevPage(buildFilters())"
          >
            ‹ Prev
          </button>
          <span class="px-1.5 text-[11px] text-gray-400">{{ page }}/{{ totalPages }}</span>
          <button
            :disabled="page >= totalPages"
            class="px-1.5 py-0.5 text-[11px] text-gray-500 hover:text-gray-700 disabled:text-gray-300 disabled:cursor-not-allowed"
            @click="nextPage(buildFilters())"
          >
            Next ›
          </button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { useAuditLogs, type AuditLogFilters } from '~/ee/composables/useAuditLogs'

definePageMeta({
  auth: true,
  permissions: ['view_audit_logs'],
  layout: 'settings'
})

const { hasFeature, license } = useEnterprise()
const { logs, loading, error, total, page, pageSize, totalPages, fetchLogs, nextPage, prevPage, fetchActionTypes } = useAuditLogs()

const searchQuery = ref('')
const selectedActions = ref<string[]>([])
const showActionDropdown = ref(false)
const dropdownRef = ref<HTMLElement | null>(null)
const hasFetched = ref(false)

const actionOptions = ref<{ value: string; label: string }[]>([])

// Fetch action types from backend
const loadActionTypes = async () => {
  const actionTypes = await fetchActionTypes()
  
  // Transform action types into options with nice labels
  actionOptions.value = actionTypes.map(action => ({
    value: action,
    label: formatActionLabel(action)
  }))
}

// Helper function to format action types into readable labels
const formatActionLabel = (action: string): string => {
  // Split by dot and format each part
  const parts = action.split('.')
  if (parts.length === 2) {
    const [resource, actionType] = parts
    const formattedResource = resource.charAt(0).toUpperCase() + resource.slice(1).replace(/_/g, ' ')
    const formattedAction = actionType.charAt(0).toUpperCase() + actionType.slice(1).replace(/_/g, ' ')
    return `${formattedResource} ${formattedAction}`
  }
  // Fallback: just capitalize and replace underscores
  return action.split('.').map(part => 
    part.charAt(0).toUpperCase() + part.slice(1).replace(/_/g, ' ')
  ).join(' ')
}

const hasActiveFilters = computed(() => {
  return searchQuery.value || selectedActions.value.length > 0
})

// Close dropdown on click outside
const handleClickOutside = (e: MouseEvent) => {
  if (dropdownRef.value && !dropdownRef.value.contains(e.target as Node)) {
    showActionDropdown.value = false
  }
}

onMounted(async () => {
  document.addEventListener('click', handleClickOutside)
  await loadActionTypes()
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
})

let searchTimeout: ReturnType<typeof setTimeout> | null = null
const debouncedSearch = () => {
  if (searchTimeout) clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => {
    applyFilters()
  }, 300)
}

const buildFilters = (): AuditLogFilters => {
  return {
    action: selectedActions.value.length > 0 ? selectedActions.value.join(',') : undefined,
    search: searchQuery.value || undefined,
  }
}

const applyFilters = () => {
  page.value = 1
  fetchLogs(buildFilters())
}

const clearFilters = () => {
  searchQuery.value = ''
  selectedActions.value = []
  page.value = 1
  fetchLogs()
}

const formatTimestamp = (timestamp: string) => {
  // Ensure UTC parsing if no timezone specified
  const isoTimestamp = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z'
  const date = new Date(isoTimestamp)
  const now = new Date()
  const diff = now.getTime() - date.getTime()
  const minutes = Math.floor(diff / 60000)
  const hours = Math.floor(diff / 3600000)
  const days = Math.floor(diff / 86400000)

  if (minutes < 1) return 'now'
  if (minutes < 60) return `${minutes}m`
  if (hours < 24) return `${hours}h`
  if (days < 7) return `${days}d`

  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

const formatAction = (action: string) => {
  const parts = action.split('.')
  if (parts.length === 2) {
    return parts[1].replace(/_/g, ' ')
  }
  return action.replace(/_/g, ' ')
}

const getActionClass = (action: string) => {
  const type = action.split('.')[1]
  switch (type) {
    case 'created':
      return 'bg-green-50 text-green-700'
    case 'deleted':
    case 'removed':
      return 'bg-red-50 text-red-700'
    case 'published':
      return 'bg-blue-50 text-blue-700'
    case 'invited':
      return 'bg-purple-50 text-purple-700'
    default:
      return 'bg-gray-100 text-gray-600'
  }
}

// Watch for license to load, then fetch logs
watch(
  () => license.value,
  (newLicense) => {
    if (newLicense && hasFeature('audit_logs') && !hasFetched.value) {
      hasFetched.value = true
      fetchLogs()
    }
  },
  { immediate: true }
)
</script>
