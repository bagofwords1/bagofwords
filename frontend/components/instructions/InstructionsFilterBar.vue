<template>
    <div class="flex flex-wrap items-center gap-3">
        <!-- Search -->
        <div class="relative flex-1 min-w-[200px] max-w-sm">
            <input
                :value="search"
                @input="$emit('update:search', ($event.target as HTMLInputElement).value)"
                type="text"
                placeholder="Search instructions..."
                class="w-full pl-9 pr-8 py-1.5 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-1 focus:ring-gray-300 focus:border-gray-300 bg-white"
            />
            <UIcon name="i-heroicons-magnifying-glass" class="absolute left-3 top-2 h-4 w-4 text-gray-400" />
            <button 
                v-if="search" 
                @click="$emit('update:search', '')"
                class="absolute right-3 top-2 text-gray-400 hover:text-gray-600"
            >
                <UIcon name="i-heroicons-x-mark" class="h-4 w-4" />
            </button>
        </div>

        <!-- Source type dropdown (multi-select) - only show if there are available types -->
        <USelectMenu
            v-if="sourceOptions.length > 0"
            :model-value="sourceTypes"
            @update:model-value="$emit('update:sourceTypes', $event)"
            :options="sourceOptions"
            value-attribute="value"
            option-attribute="label"
            size="xs"
            class="w-32"
            multiple
            :close-on-select="false"
            :ui="{ trigger: 'py-1.5' }"
        >
            <template #label>
                <span class="flex items-center gap-1.5 text-xs">
                    <UIcon name="i-heroicons-squares-2x2" class="h-3 w-3" />
                    {{ sourceTypes.length ? `${sourceTypes.length} type${sourceTypes.length !== 1 ? 's' : ''}` : 'All Types' }}
                </span>
            </template>
            <template #option="{ option }">
                <span class="flex items-center gap-1.5 text-xs">
                    <img v-if="option.icon" :src="option.icon" class="h-3.5" />
                    <UIcon v-else-if="option.heroicon" :name="option.heroicon" class="h-3 w-3" />
                    {{ option.label }}
                </span>
            </template>
        </USelectMenu>

        <!-- Status dropdown -->
        <USelectMenu
            v-if="!compact"
            :model-value="status"
            @update:model-value="$emit('update:status', $event)"
            :options="statusOptions"
            value-attribute="value"
            option-attribute="label"
            size="xs"
            class="w-28"
            :ui="{ trigger: 'py-1.5' }"
        >
            <template #label>
                <span class="text-xs">{{ selectedStatus?.label || 'Status' }}</span>
            </template>
            <template #option="{ option }">
                <span class="text-xs">{{ option.label }}</span>
            </template>
        </USelectMenu>

        <!-- Labels dropdown with create option -->
        <UPopover v-if="!compact" :popper="{ placement: 'bottom-start' }" :ui="{ width: 'w-auto' }">
            <button
                class="inline-flex items-center justify-between gap-1 w-28 px-2.5 py-1.5 text-xs bg-white border border-gray-200 rounded-md shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-1 focus:ring-gray-300"
            >
                <span class="truncate text-gray-700">
                    {{ labelIds.length ? `${labelIds.length} label${labelIds.length !== 1 ? 's' : ''}` : 'Labels' }}
                </span>
                <UIcon name="i-heroicons-chevron-down-20-solid" class="h-4 w-4 text-gray-400 flex-shrink-0" />
            </button>

            <template #panel>
                <div class="w-48">
                    <!-- Label options -->
                    <div v-if="labelOptions.length > 0" class="max-h-48 overflow-y-auto py-1">
                        <label
                            v-for="option in labelOptions"
                            :key="option.value"
                            class="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50 cursor-pointer"
                        >
                            <input
                                type="checkbox"
                                :checked="labelIds.includes(option.value)"
                                @change="toggleLabel(option.value)"
                                class="h-3.5 w-3.5 rounded border-gray-300 text-gray-800 focus:ring-gray-500"
                            />
                            <span class="w-2 h-2 rounded-full flex-shrink-0" :style="{ backgroundColor: option.color || '#94A3B8' }"></span>
                            <span class="text-xs text-gray-700 truncate">{{ option.label }}</span>
                        </label>
                    </div>
                    <div v-else class="px-3 py-2 text-xs text-gray-500">
                        No labels yet
                    </div>
                    
                    <!-- Create new label -->
                    <div class="border-t border-gray-100">
                        <button
                            @click="openCreateLabelModal"
                            class="w-full flex items-center gap-2 px-3 py-2 text-xs text-gray-600 hover:bg-gray-50 hover:text-gray-900"
                        >
                            <UIcon name="i-heroicons-plus" class="h-3.5 w-3.5" />
                            Create new label
                        </button>
                    </div>
                </div>
            </template>
        </UPopover>

        <!-- Label Form Modal -->
        <InstructionLabelFormModal
            v-model="showLabelFormModal"
            :label="null"
            @saved="handleLabelCreated"
        />

        <!-- More Filters Popover (Category + Load Mode + Data Source) -->
        <UPopover v-if="!compact" :popper="{ placement: 'bottom-start' }" :ui="{ width: 'w-auto' }">
            <UButton
                size="xs"
                color="gray"
                :variant="hasAdvancedFilters ? 'soft' : 'ghost'"
                trailing-icon="i-heroicons-chevron-down-20-solid"
            >
                <span class="flex items-center gap-1">
                    <UIcon name="i-heroicons-funnel" class="w-3 h-3" />
                    More
                    <span v-if="advancedFilterCount > 0" class="ml-0.5 px-1 py-0.5 text-[9px] bg-gray-200 rounded-full">
                        {{ advancedFilterCount }}
                    </span>
                </span>
            </UButton>

            <template #panel="{ close }">
                <div class="p-5 w-80 space-y-4">
                    <!-- Category filter -->
                    <div>
                        <label class="block text-xs font-medium text-gray-700 mb-2">Category</label>
                        <USelectMenu
                            :model-value="categories"
                            @update:model-value="$emit('update:categories', $event)"
                            :options="categorySelectOptions"
                            value-attribute="value"
                            option-attribute="label"
                            size="xs"
                            class="w-full"
                            multiple
                            :close-on-select="false"
                        >
                            <template #label>
                                <span class="text-xs">
                                    {{ categories.length ? `${categories.length} selected` : 'All Categories' }}
                                </span>
                            </template>
                            <template #option="{ option }">
                                <span class="text-xs">{{ option.label }}</span>
                            </template>
                        </USelectMenu>
                    </div>

                    <!-- Load mode filter -->
                    <div>
                        <label class="block text-xs font-medium text-gray-700 mb-2">Load Rule</label>
                        <USelectMenu
                            :model-value="loadModes"
                            @update:model-value="$emit('update:loadModes', $event)"
                            :options="loadModeSelectOptions"
                            value-attribute="value"
                            option-attribute="label"
                            size="xs"
                            class="w-full"
                            multiple
                            :close-on-select="false"
                        >
                            <template #label>
                                <span class="text-xs">
                                    {{ loadModes.length ? `${loadModes.length} selected` : 'All Load Rules' }}
                                </span>
                            </template>
                            <template #option="{ option }">
                                <span class="text-xs">{{ option.label }}</span>
                            </template>
                        </USelectMenu>
                    </div>

                    <!-- Data Source filter (hidden when domain filter is used globally) -->
                    <div v-if="dataSources.length > 0 && !hideDomainFilter">
                        <label class="block text-xs font-medium text-gray-700 mb-2">Data Source</label>
                        <USelectMenu
                            :model-value="dataSourceId"
                            @update:model-value="$emit('update:dataSourceId', $event)"
                            :options="dataSourceOptions"
                            value-attribute="value"
                            option-attribute="label"
                            size="xs"
                            class="w-full"
                            searchable
                            searchable-placeholder="Search..."
                        >
                            <template #label>
                                <span class="flex items-center gap-2 text-xs">
                                    <DataSourceIcon v-if="selectedDataSource?.type" :type="selectedDataSource.type" class="h-4 w-4" />
                                    {{ selectedDataSource?.label || 'All Data Sources' }}
                                </span>
                            </template>
                            <template #option="{ option }">
                                <span class="flex items-center gap-2 text-xs">
                                    <DataSourceIcon v-if="option.type" :type="option.type" class="h-4 w-4" />
                                    {{ option.label }}
                                </span>
                            </template>
                        </USelectMenu>
                    </div>

                    <!-- Clear advanced filters -->
                    <button
                        v-if="hasAdvancedFilters"
                        @click="clearAdvancedFilters"
                        class="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1 pt-2 border-t border-gray-100"
                    >
                        <UIcon name="i-heroicons-x-mark" class="h-3 w-3" />
                        Clear advanced filters
                    </button>
                </div>
            </template>
        </UPopover>

        <slot name="actions" />

        <!-- Clear all filters -->
        <button 
            v-if="hasActiveFilters"
            @click="$emit('reset')"
            class="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
        >
            <UIcon name="i-heroicons-x-mark" class="h-3 w-3" />
            Clear
        </button>
    </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import InstructionLabelFormModal from '~/components/InstructionLabelFormModal.vue'

interface FilterOption {
    value: string | null
    label: string
    icon?: string
    heroicon?: string
    color?: string
    type?: string
}

interface Label {
    id: string
    name: string
    color?: string | null
}

interface DataSource {
    id: string
    name: string
    type: string
}

interface SourceTypeOption {
    value: string
    label: string
    icon?: string
    heroicon?: string
}

const props = withDefaults(defineProps<{
    search: string
    sourceTypes: string[]
    availableSourceTypes?: SourceTypeOption[]
    status: string | null
    loadModes: string[]
    categories: string[]
    dataSourceId?: string | null
    dataSourceIds?: string[]
    labelIds: string[]
    labels?: Label[]
    dataSources?: DataSource[]
    compact?: boolean
    hideDomainFilter?: boolean
}>(), {
    compact: false,
    hideDomainFilter: false,
    labels: () => [],
    dataSources: () => [],
    labelIds: () => [],
    sourceTypes: () => [],
    availableSourceTypes: () => [],
    loadModes: () => [],
    categories: () => [],
    dataSourceIds: () => []
})

const emit = defineEmits<{
    'update:search': [value: string]
    'update:sourceTypes': [value: string[]]
    'update:status': [value: string | null]
    'update:loadModes': [value: string[]]
    'update:categories': [value: string[]]
    'update:dataSourceId': [value: string | null]
    'update:dataSourceIds': [value: string[]]
    'update:labelIds': [value: string[]]
    'reset': []
    'labelCreated': []
}>()

// Label creation modal state
const showLabelFormModal = ref(false)

const openCreateLabelModal = () => {
    showLabelFormModal.value = true
}

const handleLabelCreated = () => {
    showLabelFormModal.value = false
    emit('labelCreated')
}

const toggleLabel = (labelId: string) => {
    const newLabelIds = props.labelIds.includes(labelId)
        ? props.labelIds.filter(id => id !== labelId)
        : [...props.labelIds, labelId]
    emit('update:labelIds', newLabelIds)
}

// Use dynamic source options if provided, otherwise empty (filter won't show if empty)
const sourceOptions = computed(() => {
    if (props.availableSourceTypes.length > 0) {
        return props.availableSourceTypes.map(st => ({
            value: st.value,
            label: st.label,
            icon: st.icon,
            heroicon: st.heroicon
        }))
    }
    return []
})

const statusOptions: FilterOption[] = [
    { value: null, label: 'All Status' },
    { value: 'published', label: 'Published' },
    { value: 'draft', label: 'Draft' },
    { value: 'archived', label: 'Archived' },
]

const loadModeOptions: FilterOption[] = [
    { value: null, label: 'All Load Rules' },
    { value: 'always', label: 'Always' },
    { value: 'intelligent', label: 'Smart' },
    { value: 'disabled', label: 'Disabled' },
]

// Multi-select options (without the "All" option)
const loadModeSelectOptions = [
    { value: 'always', label: 'Always' },
    { value: 'intelligent', label: 'Smart' },
    { value: 'disabled', label: 'Disabled' },
]

const categoryOptions: FilterOption[] = [
    { value: null, label: 'All Categories' },
    { value: 'general', label: 'General' },
    { value: 'code', label: 'Code' },
]

// Multi-select options (without the "All" option)
const categorySelectOptions = [
    { value: 'general', label: 'General' },
    { value: 'code', label: 'Code' },
]

const labelOptions = computed(() => {
    return props.labels.map(l => ({
        value: l.id,
        label: l.name,
        color: l.color || '#94A3B8'
    }))
})

const dataSourceOptions = computed(() => {
    return [
        { value: null, label: 'All Data Sources', type: null },
        ...props.dataSources.map(ds => ({
            value: ds.id,
            label: ds.name,
            type: ds.type
        }))
    ]
})

const selectedStatus = computed(() => statusOptions.find(o => o.value === props.status) || statusOptions[0])
const selectedDataSource = computed(() => dataSourceOptions.value.find(o => o.value === props.dataSourceId) || dataSourceOptions.value[0])

const hasAdvancedFilters = computed(() => {
    const hasDataSourceFilter = !props.hideDomainFilter && (props.dataSourceId || props.dataSourceIds?.length > 0)
    return props.categories.length > 0 || props.loadModes.length > 0 || hasDataSourceFilter
})

const advancedFilterCount = computed(() => {
    let count = 0
    if (props.categories.length > 0) count++
    if (props.loadModes.length > 0) count++
    if (!props.hideDomainFilter && (props.dataSourceId || props.dataSourceIds?.length > 0)) count++
    return count
})

const hasActiveFilters = computed(() => {
    return props.search || props.sourceTypes.length > 0 || props.status || props.labelIds.length > 0 || hasAdvancedFilters.value
})

const clearAdvancedFilters = () => {
    emit('update:categories', [])
    emit('update:loadModes', [])
    if (!props.hideDomainFilter) {
        emit('update:dataSourceId', null)
        emit('update:dataSourceIds', [])
    }
}
</script>
