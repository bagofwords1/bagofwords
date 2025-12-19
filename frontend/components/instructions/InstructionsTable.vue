<template>
    <div class="flex flex-col h-full">
        <!-- Loading state -->
        <div v-if="loading" class="flex items-center justify-center py-12 flex-1">
            <Spinner />
        </div>

        <!-- Empty state -->
        <div v-else-if="instructions.length === 0" class="flex items-center justify-center py-12 flex-1">
            <div class="flex flex-col items-center justify-center gap-2 text-center">
                <Icon name="heroicons:document-text" class="mx-auto h-10 w-10 text-gray-300" />
                <h3 class="mt-2 text-sm font-medium text-gray-900">{{ emptyTitle }}</h3>
                <p class="mt-1 text-xs text-gray-500">{{ emptyMessage }}</p>
            </div>
        </div>

        <!-- Table -->
        <div v-else class="flex-1 flex flex-col min-h-0">
            <div class="bg-white border border-gray-200 rounded-lg overflow-hidden flex flex-col flex-1 min-h-0">
                <div class="overflow-x-auto overflow-y-auto flex-1">
                    <table class="min-w-full divide-y divide-gray-200">
                        <thead class="bg-gray-50 sticky top-0 z-10">
                            <tr>
                                <!-- Checkbox header -->
                                <th v-if="selectable" :class="compact ? 'px-2 py-1.5 w-8' : 'px-3 py-2 w-10'">
                                    <input 
                                        type="checkbox"
                                        :checked="isAllPageSelected"
                                        :indeterminate="isSomeSelected && !isAllPageSelected"
                                        @change="$emit('toggle-page')"
                                        :class="compact ? 'h-3.5 w-3.5' : 'h-4 w-4'"
                                        class="rounded border-gray-300 text-gray-800 focus:ring-gray-500"
                                    />
                                </th>
                                <th :class="[compact ? 'px-2 py-1.5 text-[10px]' : 'px-3 py-2 text-xs', 'text-left font-medium text-gray-500 uppercase tracking-wider']">
                                    Instruction
                                </th>
                                <th v-if="showSource" :class="[compact ? 'px-2 py-1.5 text-[10px] w-10' : 'px-3 py-2 text-xs w-12', 'text-center font-medium text-gray-500 uppercase tracking-wider']">
                                    Type
                                </th>
                                <th v-if="showCategory" :class="[compact ? 'px-2 py-1.5 text-[10px] w-14' : 'px-3 py-2 text-xs w-16', 'text-center font-medium text-gray-500 uppercase tracking-wider']">
                                    Category
                                </th>
                                <th v-if="showDataSource" :class="[compact ? 'px-2 py-1.5 text-[10px] w-24' : 'px-3 py-2 text-xs w-28', 'text-left font-medium text-gray-500 uppercase tracking-wider']">
                                    Data Source
                                </th>
                                <th v-if="showLoadMode" :class="[compact ? 'px-2 py-1.5 text-[10px] w-14' : 'px-3 py-2 text-xs w-16', 'text-center font-medium text-gray-500 uppercase tracking-wider']">
                                    Load
                                </th>
                                <th v-if="showLabels" :class="[compact ? 'px-2 py-1.5 text-[10px] w-28' : 'px-3 py-2 text-xs w-32', 'text-left font-medium text-gray-500 uppercase tracking-wider']">
                                    Labels
                                </th>
                                <th v-if="showStatus" :class="[compact ? 'px-2 py-1.5 text-[10px] w-16' : 'px-3 py-2 text-xs w-20', 'text-center font-medium text-gray-500 uppercase tracking-wider']">
                                    Status
                                </th>
                            </tr>
                        </thead>
                        <tbody class="bg-white divide-y divide-gray-200">
                            <tr 
                                v-for="instruction in instructions" 
                                :key="instruction.id"
                                class="hover:bg-gray-50 transition-colors cursor-pointer"
                                :class="{ 'bg-blue-50': selectable && selectedIds?.has(instruction.id) }"
                                @click="handleRowClick(instruction, $event)"
                            >
                                <!-- Checkbox -->
                                <td v-if="selectable" :class="compact ? 'px-2 py-1.5 w-8' : 'px-3 py-2.5 w-10'">
                                    <input 
                                        type="checkbox"
                                        :checked="selectedIds?.has(instruction.id)"
                                        @change.stop="$emit('toggle-select', instruction.id)"
                                        @click.stop
                                        :class="compact ? 'h-3.5 w-3.5' : 'h-4 w-4'"
                                        class="rounded border-gray-300 text-gray-800 focus:ring-gray-500"
                                    />
                                </td>

                                <!-- Instruction text -->
                                <td :class="compact ? 'px-2 py-1.5' : 'px-3 py-2.5'">
                                    <div :class="compact ? 'max-w-md' : 'max-w-lg'">
                                        <!-- Git source path (small) -->
                                        <p 
                                            v-if="instruction.source_type === 'git' && instruction.title" 
                                            :class="compact ? 'text-[9px]' : 'text-[10px]'"
                                            class="text-gray-400 font-mono truncate mb-0.5"
                                            :title="instruction.title"
                                        >
                                            {{ instruction.title }}
                                        </p>
                                        <!-- Instruction text -->
                                        <p 
                                            :class="[compact ? 'text-[11px]' : 'text-xs', { 'line-clamp-2': !expandedRows.has(instruction.id) }]"
                                            class="text-gray-900 leading-snug"
                                            :title="instruction.text"
                                        >
                                            {{ instruction.text }}
                                        </p>
                                        <button 
                                            v-if="instruction.text && instruction.text.length > 150"
                                            @click.stop="toggleExpand(instruction.id)"
                                            :class="compact ? 'text-[9px]' : 'text-[10px]'"
                                            class="text-blue-500 hover:text-blue-700 mt-0.5 font-medium"
                                        >
                                            {{ expandedRows.has(instruction.id) ? 'less' : 'more' }}
                                        </button>
                                    </div>
                                </td>

                                <!-- Source type / Resource type -->
                                <td v-if="showSource" :class="compact ? 'px-2 py-1.5' : 'px-3 py-2.5'">
                                    <div class="flex items-center justify-center gap-1.5">
                                        <template v-if="helpers.getSourceType(instruction) === 'git'">
                                            <UTooltip :text="helpers.getResourceTypeTooltip(instruction)">
                                                <img 
                                                    v-if="helpers.getResourceTypeIcon(instruction)"
                                                    :src="helpers.getResourceTypeIcon(instruction)" 
                                                    :alt="helpers.getResourceTypeTooltip(instruction)"
                                                    class="w-6 object-contain"
                                                />
                                                <UIcon 
                                                    v-else
                                                    :name="helpers.getResourceTypeFallbackIcon(instruction)" 
                                                    class="w-5 h-5 text-gray-500"
                                                />
                                            </UTooltip>
                                        </template>
                                        <template v-else>
                                            <UTooltip :text="helpers.getSourceTooltip(instruction)">
                                                <UIcon 
                                                    :name="helpers.getSourceIcon(instruction)" 
                                                    class="w-5 h-5"
                                                    :class="{
                                                        'text-amber-500': helpers.getSourceType(instruction) === 'ai',
                                                        'text-blue-500': helpers.getSourceType(instruction) === 'user'
                                                    }"
                                                />
                                            </UTooltip>
                                        </template>
                                    </div>
                                </td>

                                <!-- Category -->
                                <td v-if="showCategory" :class="[compact ? 'px-2 py-1.5' : 'px-3 py-2.5', 'text-center']">
                                    <span 
                                        :class="[compact ? 'text-[9px] px-1 py-0.5' : 'text-[10px] px-1.5 py-0.5', 'rounded font-medium', helpers.getCategoryClass(instruction.category)]"
                                    >
                                        {{ helpers.formatCategory(instruction.category) }}
                                    </span>
                                </td>

                                <!-- Data Source -->
                                <td v-if="showDataSource" :class="compact ? 'px-2 py-1.5' : 'px-3 py-2.5'">
                                    <div v-if="instruction.data_sources?.length" class="flex items-center gap-1.5">
                                        <UTooltip :text="instruction.data_sources.map((ds: any) => ds.name).join(', ')">
                                            <div class="flex items-center gap-1">
                                                <DataSourceIcon 
                                                    :type="instruction.data_sources[0]?.type" 
                                                    :class="compact ? 'h-3 w-3' : 'h-3.5 w-3.5'"
                                                    class="flex-shrink-0"
                                                />
                                                <span :class="[compact ? 'text-[9px] max-w-[60px]' : 'text-[10px] max-w-[70px]', 'text-gray-600 truncate']">
                                                    {{ instruction.data_sources[0]?.name || '—' }}
                                                </span>
                                            </div>
                                        </UTooltip>
                                        <span v-if="instruction.data_sources.length > 1" :class="compact ? 'text-[8px]' : 'text-[9px]'" class="text-gray-400">
                                            +{{ instruction.data_sources.length - 1 }}
                                        </span>
                                    </div>
                                    <span v-else :class="compact ? 'text-[9px]' : 'text-[10px]'" class="text-gray-300">—</span>
                                </td>

                                <!-- Load mode -->
                                <td v-if="showLoadMode" :class="[compact ? 'px-2 py-1.5' : 'px-3 py-2.5', 'text-center']">
                                    <span 
                                        :class="[compact ? 'text-[9px] px-1 py-0.5' : 'text-[10px] px-1.5 py-0.5', 'rounded font-medium', helpers.getLoadModeClass(instruction.load_mode)]"
                                    >
                                        {{ helpers.getLoadModeLabel(instruction.load_mode) }}
                                    </span>
                                </td>

                                <!-- Labels -->
                                <td v-if="showLabels" :class="compact ? 'px-2 py-1.5' : 'px-3 py-2.5'">
                                    <div class="flex flex-wrap items-center gap-1">
                                        <template v-if="instruction.labels?.length">
                                            <UTooltip
                                                v-for="label in instruction.labels.slice(0, 2)"
                                                :key="label.id"
                                                :text="label.description || label.name"
                                            >
                                                <span
                                                    :class="compact ? 'text-[8px] px-1 py-0.5' : 'text-[9px] px-1.5 py-0.5'"
                                                    class="inline-flex items-center gap-1 rounded-full border font-medium"
                                                    :style="{
                                                        borderColor: label.color || '#CBD5F5',
                                                        backgroundColor: label.color ? `${label.color}15` : '#F9FAFB',
                                                        color: '#374151'
                                                    }"
                                                >
                                                    <span :class="compact ? 'w-1 h-1' : 'w-1.5 h-1.5'" class="rounded-full" :style="{ backgroundColor: label.color || '#94A3B8' }"></span>
                                                    {{ label.name }}
                                                </span>
                                            </UTooltip>
                                            <span v-if="instruction.labels.length > 2" :class="compact ? 'text-[8px]' : 'text-[9px]'" class="text-gray-400">
                                                +{{ instruction.labels.length - 2 }}
                                            </span>
                                        </template>
                                        <span v-else :class="compact ? 'text-[9px]' : 'text-[10px]'" class="text-gray-300">—</span>
                                    </div>
                                </td>

                                <!-- Status -->
                                <td v-if="showStatus" :class="[compact ? 'px-2 py-1.5' : 'px-3 py-2.5', 'text-center']">
                                    <UTooltip :text="helpers.getStatusTooltip(instruction)">
                                        <span 
                                            :class="[compact ? 'text-[9px] px-1 py-0.5' : 'text-[10px] px-1.5 py-0.5', 'rounded font-medium', helpers.getStatusClass(instruction)]"
                                        >
                                            {{ helpers.formatStatus(instruction.status) }}
                                        </span>
                                    </UTooltip>
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>

                <!-- Pagination -->
                <div v-if="showPagination" :class="[compact ? 'px-2 py-1.5' : 'px-3 py-2', 'border-t border-gray-200 flex items-center justify-between flex-shrink-0 bg-gray-50']">
                    <div :class="compact ? 'text-[10px]' : 'text-xs'" class="text-gray-500">
                        {{ (currentPage - 1) * pageSize + 1 }}–{{ Math.min(currentPage * pageSize, totalItems) }} of {{ totalItems }}
                    </div>
                    <div class="flex items-center gap-1">
                        <UButton
                            icon="i-heroicons-chevron-left"
                            color="gray"
                            variant="ghost"
                            :size="compact ? '2xs' : 'xs'"
                            :disabled="currentPage === 1"
                            @click="$emit('page-change', currentPage - 1)"
                        />
                        <UButton
                            v-for="page in visiblePages"
                            :key="page"
                            :color="page === currentPage ? 'gray' : 'gray'"
                            :variant="page === currentPage ? 'solid' : 'ghost'"
                            :size="compact ? '2xs' : 'xs'"
                            :class="compact ? 'min-w-[20px]' : 'min-w-[24px]'"
                            @click="$emit('page-change', page)"
                        >
                            {{ page }}
                        </UButton>
                        <UButton
                            icon="i-heroicons-chevron-right"
                            color="gray"
                            variant="ghost"
                            :size="compact ? '2xs' : 'xs'"
                            :disabled="currentPage === totalPages"
                            @click="$emit('page-change', currentPage + 1)"
                        />
                    </div>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import type { Instruction } from '~/composables/useInstructionHelpers'
import { useInstructionHelpers } from '~/composables/useInstructionHelpers'
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const props = withDefaults(defineProps<{
    instructions: Instruction[]
    loading?: boolean
    compact?: boolean
    
    // Selection
    selectable?: boolean
    selectedIds?: Set<string>
    isAllPageSelected?: boolean
    isSomeSelected?: boolean
    
    // Column visibility
    showSource?: boolean
    showCategory?: boolean
    showDataSource?: boolean
    showLoadMode?: boolean
    showLabels?: boolean
    showStatus?: boolean
    
    // Pagination
    showPagination?: boolean
    currentPage?: number
    pageSize?: number
    totalItems?: number
    totalPages?: number
    visiblePages?: number[]
    
    // Empty state
    emptyTitle?: string
    emptyMessage?: string
}>(), {
    loading: false,
    compact: false,
    selectable: false,
    isAllPageSelected: false,
    isSomeSelected: false,
    showSource: true,
    showCategory: true,
    showDataSource: true,
    showLoadMode: true,
    showLabels: true,
    showStatus: true,
    showPagination: true,
    currentPage: 1,
    pageSize: 25,
    totalItems: 0,
    totalPages: 1,
    visiblePages: () => [1],
    emptyTitle: 'No instructions',
    emptyMessage: 'No instructions found matching your criteria.'
})

const emit = defineEmits<{
    click: [instruction: Instruction]
    'page-change': [page: number]
    'toggle-select': [id: string]
    'toggle-page': []
}>()

const helpers = useInstructionHelpers()
const expandedRows = ref<Set<string>>(new Set())

const toggleExpand = (id: string) => {
    if (expandedRows.value.has(id)) {
        expandedRows.value.delete(id)
    } else {
        expandedRows.value.add(id)
    }
    expandedRows.value = new Set(expandedRows.value)
}

const handleRowClick = (instruction: Instruction, event: MouseEvent) => {
    // Always emit click to open modal - checkbox handles selection separately
    emit('click', instruction)
}
</script>
