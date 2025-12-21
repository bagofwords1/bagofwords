<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-3xl' }">
        <UCard>
            <!-- Header -->
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-base font-semibold text-gray-900">
                        Build #{{ diffData?.build_b_number || '...' }} Changes
                    </h3>
                    <UButton
                        color="gray"
                        variant="ghost"
                        icon="i-heroicons-x-mark-20-solid"
                        size="xs"
                        @click="close"
                    />
                </div>
                <div class="text-xs text-gray-500 mt-0.5">
                    Compared to Build #{{ diffData?.build_a_number || '...' }}
                </div>
                <!-- Summary badges -->
                <div v-if="diffData" class="flex gap-2 mt-2">
                    <span v-if="diffData.added_count" class="text-[10px] px-1.5 py-0.5 bg-green-50 text-green-700 rounded border border-green-200">
                        +{{ diffData.added_count }} added
                    </span>
                    <span v-if="diffData.modified_count" class="text-[10px] px-1.5 py-0.5 bg-amber-50 text-amber-700 rounded border border-amber-200">
                        ~{{ diffData.modified_count }} modified
                    </span>
                    <span v-if="diffData.removed_count" class="text-[10px] px-1.5 py-0.5 bg-red-50 text-red-700 rounded border border-red-200">
                        −{{ diffData.removed_count }} removed
                    </span>
                    <span v-if="!diffData.added_count && !diffData.modified_count && !diffData.removed_count" class="text-[10px] text-gray-400">
                        No changes
                    </span>
                </div>
            </template>

            <!-- Content -->
            <div class="min-h-[200px] max-h-[500px] overflow-y-auto">
                <!-- Loading State -->
                <div v-if="loading" class="flex items-center justify-center py-12">
                    <div class="text-center">
                        <UIcon name="i-heroicons-arrow-path" class="w-6 h-6 mx-auto mb-2 text-gray-400 animate-spin" />
                        <p class="text-xs text-gray-500">Loading changes...</p>
                    </div>
                </div>

                <!-- Error State -->
                <div v-else-if="error" class="flex items-center justify-center py-12">
                    <div class="text-center">
                        <UIcon name="i-heroicons-exclamation-triangle" class="w-6 h-6 mx-auto mb-2 text-red-400" />
                        <p class="text-xs text-red-600">{{ error }}</p>
                    </div>
                </div>

                <!-- Empty State -->
                <div v-else-if="diffData && !diffData.items?.length" class="flex items-center justify-center py-12">
                    <div class="text-center">
                        <UIcon name="i-heroicons-check-circle" class="w-8 h-8 mx-auto mb-2 text-green-400" />
                        <p class="text-sm text-gray-600">No changes in this build</p>
                    </div>
                </div>

                <!-- Diff Content -->
                <div v-else-if="diffData" class="space-y-3">
                    <!-- Added Section -->
                    <div v-if="addedItems.length">
                        <button 
                            @click="toggleSection('added')"
                            class="flex items-center gap-2 w-full text-left text-xs font-medium text-green-700 hover:text-green-800 py-1"
                        >
                            <UIcon 
                                :name="expandedSections.added ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" 
                                class="w-3 h-3"
                            />
                            <span>Added ({{ addedItems.length }})</span>
                        </button>
                        <div v-if="expandedSections.added" class="ml-4 space-y-1">
                            <DiffItem 
                                v-for="item in addedItems" 
                                :key="item.instruction_id"
                                :item="item"
                                :expanded="expandedItems.has(item.instruction_id)"
                                @toggle="toggleItem(item.instruction_id)"
                            />
                        </div>
                    </div>

                    <!-- Modified Section -->
                    <div v-if="modifiedItems.length">
                        <button 
                            @click="toggleSection('modified')"
                            class="flex items-center gap-2 w-full text-left text-xs font-medium text-amber-700 hover:text-amber-800 py-1"
                        >
                            <UIcon 
                                :name="expandedSections.modified ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" 
                                class="w-3 h-3"
                            />
                            <span>Modified ({{ modifiedItems.length }})</span>
                        </button>
                        <div v-if="expandedSections.modified" class="ml-4 space-y-1">
                            <DiffItem 
                                v-for="item in modifiedItems" 
                                :key="item.instruction_id"
                                :item="item"
                                :expanded="expandedItems.has(item.instruction_id)"
                                @toggle="toggleItem(item.instruction_id)"
                            />
                        </div>
                    </div>

                    <!-- Removed Section -->
                    <div v-if="removedItems.length">
                        <button 
                            @click="toggleSection('removed')"
                            class="flex items-center gap-2 w-full text-left text-xs font-medium text-red-700 hover:text-red-800 py-1"
                        >
                            <UIcon 
                                :name="expandedSections.removed ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" 
                                class="w-3 h-3"
                            />
                            <span>Removed ({{ removedItems.length }})</span>
                        </button>
                        <div v-if="expandedSections.removed" class="ml-4 space-y-1">
                            <DiffItem 
                                v-for="item in removedItems" 
                                :key="item.instruction_id"
                                :item="item"
                                :expanded="expandedItems.has(item.instruction_id)"
                                @toggle="toggleItem(item.instruction_id)"
                            />
                        </div>
                    </div>
                </div>
            </div>
        </UCard>
    </UModal>
</template>

<script setup lang="ts">
interface DiffInstructionItem {
    instruction_id: string
    change_type: 'added' | 'removed' | 'modified'
    title?: string
    text: string
    category?: string
    source_type?: string
    status?: string
    load_mode?: string
    previous_text?: string
    previous_title?: string
    previous_status?: string
    previous_load_mode?: string
    previous_category?: string
    changed_fields?: string[]
    from_version_number?: number
    to_version_number?: number
}

interface BuildDiffDetailedResponse {
    build_a_id: string
    build_b_id: string
    build_a_number: number
    build_b_number: number
    items: DiffInstructionItem[]
    added_count: number
    modified_count: number
    removed_count: number
}

interface Props {
    modelValue: boolean
    buildId: string
    compareToBuildId: string
}

const props = defineProps<Props>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
}>()

// State
const loading = ref(false)
const error = ref<string | null>(null)
const diffData = ref<BuildDiffDetailedResponse | null>(null)
const expandedSections = reactive({
    added: true,
    modified: true,
    removed: true
})
const expandedItems = ref<Set<string>>(new Set())

const isOpen = computed({
    get: () => props.modelValue,
    set: (value) => emit('update:modelValue', value)
})

// Computed - filter items by type
const addedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'added') || []
)
const modifiedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'modified') || []
)
const removedItems = computed(() => 
    diffData.value?.items.filter(i => i.change_type === 'removed') || []
)

// Methods
const fetchDiff = async () => {
    if (!props.buildId || !props.compareToBuildId) {
        error.value = 'Missing build IDs'
        return
    }
    
    loading.value = true
    error.value = null
    
    try {
        const response = await useMyFetch<BuildDiffDetailedResponse>(
            `/api/builds/${props.buildId}/diff/details?compare_to=${props.compareToBuildId}`
        )
        
        if (response.error.value) {
            error.value = 'Failed to load diff'
            console.error('Error fetching diff:', response.error.value)
        } else if (response.data.value) {
            diffData.value = response.data.value
        }
    } catch (e) {
        error.value = 'Failed to load diff'
        console.error('Failed to fetch diff:', e)
    } finally {
        loading.value = false
    }
}

const close = () => {
    emit('update:modelValue', false)
}

const toggleSection = (section: 'added' | 'modified' | 'removed') => {
    expandedSections[section] = !expandedSections[section]
}

const toggleItem = (id: string) => {
    if (expandedItems.value.has(id)) {
        expandedItems.value.delete(id)
    } else {
        expandedItems.value.add(id)
    }
    expandedItems.value = new Set(expandedItems.value)
}

// Watch for modal opening
watch(() => props.modelValue, (newValue) => {
    if (newValue) {
        fetchDiff()
    } else {
        // Reset state on close
        diffData.value = null
        expandedItems.value = new Set()
    }
})

// Child component for each diff item
const DiffItem = defineComponent({
    props: {
        item: {
            type: Object as PropType<DiffInstructionItem>,
            required: true
        },
        expanded: {
            type: Boolean,
            default: false
        }
    },
    emits: ['toggle'],
    setup(props, { emit }) {
        const displayTitle = computed(() => {
            if (props.item.title) return props.item.title
            const text = props.item.text || ''
            return text.length > 60 ? text.slice(0, 60) + '...' : text
        })
        
        const versionLabel = computed(() => {
            if (props.item.change_type === 'modified') {
                return `v${props.item.from_version_number || '?'} → v${props.item.to_version_number || '?'}`
            }
            if (props.item.change_type === 'added' && props.item.to_version_number) {
                return `v${props.item.to_version_number}`
            }
            if (props.item.change_type === 'removed' && props.item.from_version_number) {
                return `v${props.item.from_version_number}`
            }
            return null
        })
        
        const changeTypeClass = computed(() => {
            switch (props.item.change_type) {
                case 'added': return 'border-l-green-400 bg-green-50/50'
                case 'modified': return 'border-l-amber-400 bg-amber-50/50'
                case 'removed': return 'border-l-red-400 bg-red-50/50'
                default: return 'border-l-gray-300'
            }
        })
        
        const changeTypeIcon = computed(() => {
            switch (props.item.change_type) {
                case 'added': return 'i-heroicons-plus-circle'
                case 'modified': return 'i-heroicons-pencil-square'
                case 'removed': return 'i-heroicons-minus-circle'
                default: return 'i-heroicons-document'
            }
        })
        
        const iconColorClass = computed(() => {
            switch (props.item.change_type) {
                case 'added': return 'text-green-500'
                case 'modified': return 'text-amber-500'
                case 'removed': return 'text-red-500'
                default: return 'text-gray-400'
            }
        })
        
        // Source type icon and label
        const sourceIcon = computed(() => {
            switch (props.item.source_type) {
                case 'ai': return 'i-heroicons-sparkles'
                case 'git': return 'i-heroicons-code-bracket'
                case 'user': return 'i-heroicons-user'
                default: return 'i-heroicons-document'
            }
        })
        
        const sourceLabel = computed(() => {
            switch (props.item.source_type) {
                case 'ai': return 'AI'
                case 'git': return 'Git'
                case 'user': return 'User'
                default: return null
            }
        })
        
        const sourceColorClass = computed(() => {
            switch (props.item.source_type) {
                case 'ai': return 'text-purple-500'
                case 'git': return 'text-blue-500'
                case 'user': return 'text-gray-500'
                default: return 'text-gray-400'
            }
        })
        
        // Check if we have text changes to show diff
        const hasTextChange = computed(() => 
            props.item.change_type === 'modified' && 
            props.item.changed_fields?.includes('text') && 
            props.item.previous_text
        )
        
        // Check for non-text field changes
        const hasFieldChanges = computed(() => {
            if (props.item.change_type !== 'modified') return false
            const nonTextFields = (props.item.changed_fields || []).filter(f => f !== 'text')
            return nonTextFields.length > 0
        })
        
        // Render field change badge
        const renderFieldChange = (field: string, oldVal: string | null | undefined, newVal: string | null | undefined) => {
            const fieldLabels: Record<string, string> = {
                'status': 'Status',
                'load_mode': 'Load Mode',
                'category': 'Category',
                'title': 'Title'
            }
            return h('div', { class: 'flex items-center gap-2 text-[10px]' }, [
                h('span', { class: 'text-gray-500 w-16' }, fieldLabels[field] || field),
                h('span', { class: 'text-red-500 line-through' }, oldVal || '—'),
                h('span', { class: 'text-gray-400' }, '→'),
                h('span', { class: 'text-green-600 font-medium' }, newVal || '—')
            ])
        }
        
        return () => h('div', { class: ['border-l-2 rounded-r', changeTypeClass.value] }, [
            // Header row (clickable)
            h('button', {
                class: 'flex items-center gap-2 w-full text-left px-2 py-1.5 hover:bg-white/50 transition-colors',
                onClick: () => emit('toggle')
            }, [
                h(resolveComponent('UIcon'), {
                    name: props.expanded ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right',
                    class: 'w-3 h-3 text-gray-400 shrink-0'
                }),
                h(resolveComponent('UIcon'), {
                    name: changeTypeIcon.value,
                    class: ['w-3.5 h-3.5 shrink-0', iconColorClass.value]
                }),
                // Source type icon (AI/User/Git)
                props.item.source_type && h(resolveComponent('UTooltip'), { 
                    text: sourceLabel.value || props.item.source_type,
                    popper: { placement: 'top' }
                }, () => h(resolveComponent('UIcon'), {
                    name: sourceIcon.value,
                    class: ['w-3 h-3 shrink-0', sourceColorClass.value]
                })),
                h('span', { class: 'text-[11px] text-gray-800 truncate flex-1' }, displayTitle.value),
                // Show changed fields badges for modified items
                props.item.change_type === 'modified' && props.item.changed_fields?.length && h('div', { class: 'flex gap-1 shrink-0' }, 
                    props.item.changed_fields.slice(0, 3).map(field => 
                        h('span', { 
                            class: 'text-[8px] px-1 py-0.5 bg-amber-100 text-amber-700 rounded',
                            key: field 
                        }, field)
                    )
                ),
                versionLabel.value && h('span', { class: 'text-[9px] text-gray-400 shrink-0 ml-1' }, versionLabel.value)
            ]),
            // Expanded content
            props.expanded && h('div', { class: 'px-3 pb-2 pt-1 space-y-2' }, [
                // Field changes (status, load_mode, category, title) for modified items
                hasFieldChanges.value && h('div', { class: 'bg-amber-50/50 border border-amber-100 rounded px-2 py-1.5 space-y-1' }, [
                    h('div', { class: 'text-[9px] uppercase tracking-wide text-amber-600 mb-1' }, 'Field Changes'),
                    // Status change
                    props.item.changed_fields?.includes('status') && renderFieldChange(
                        'status', 
                        props.item.previous_status, 
                        props.item.status
                    ),
                    // Load mode change
                    props.item.changed_fields?.includes('load_mode') && renderFieldChange(
                        'load_mode', 
                        props.item.previous_load_mode, 
                        props.item.load_mode
                    ),
                    // Category change
                    props.item.changed_fields?.includes('category') && renderFieldChange(
                        'category', 
                        props.item.previous_category, 
                        props.item.category
                    ),
                    // Title change
                    props.item.changed_fields?.includes('title') && renderFieldChange(
                        'title', 
                        props.item.previous_title, 
                        props.item.title
                    ),
                ]),
                // Text diff for modified items
                hasTextChange.value && h('div', { class: 'space-y-1.5' }, [
                    h('div', { class: 'text-[9px] uppercase tracking-wide text-gray-400' }, 'Text Changes'),
                    h('div', { class: 'bg-red-50 border border-red-100 rounded px-2 py-1.5' }, [
                        h('div', { class: 'flex items-start gap-1.5' }, [
                            h('span', { class: 'text-red-400 font-mono text-[10px] shrink-0' }, '−'),
                            h('pre', { class: 'text-[10px] text-red-700 whitespace-pre-wrap font-sans leading-relaxed flex-1' }, props.item.previous_text)
                        ])
                    ]),
                    h('div', { class: 'bg-green-50 border border-green-100 rounded px-2 py-1.5' }, [
                        h('div', { class: 'flex items-start gap-1.5' }, [
                            h('span', { class: 'text-green-400 font-mono text-[10px] shrink-0' }, '+'),
                            h('pre', { class: 'text-[10px] text-green-700 whitespace-pre-wrap font-sans leading-relaxed flex-1' }, props.item.text)
                        ])
                    ])
                ]),
                // For modified without text change, or added/removed - show content
                (props.item.change_type !== 'modified' || !hasTextChange.value) && h('div', { class: 'bg-gray-50 rounded px-2 py-1.5 border border-gray-100' }, [
                    h('pre', { class: 'text-[10px] text-gray-700 whitespace-pre-wrap font-sans leading-relaxed' }, props.item.text)
                ]),
                // Status, Load Mode, Source badges for added/removed
                (props.item.change_type === 'added' || props.item.change_type === 'removed') && 
                (props.item.status || props.item.load_mode || props.item.category || props.item.source_type) && 
                h('div', { class: 'flex flex-wrap gap-2 mt-1' }, [
                    props.item.source_type && h('div', { class: 'flex items-center gap-1' }, [
                        h('span', { class: 'text-[9px] text-gray-400' }, 'Source:'),
                        h('span', { 
                            class: [
                                'text-[9px] px-1.5 py-0.5 rounded flex items-center gap-1',
                                props.item.source_type === 'ai' ? 'bg-purple-50 text-purple-600' :
                                props.item.source_type === 'git' ? 'bg-blue-50 text-blue-600' :
                                'bg-gray-100 text-gray-600'
                            ]
                        }, [
                            h(resolveComponent('UIcon'), {
                                name: sourceIcon.value,
                                class: 'w-2.5 h-2.5'
                            }),
                            sourceLabel.value || props.item.source_type
                        ])
                    ]),
                    props.item.status && h('div', { class: 'flex items-center gap-1' }, [
                        h('span', { class: 'text-[9px] text-gray-400' }, 'Status:'),
                        h('span', { class: 'text-[9px] px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded' }, props.item.status)
                    ]),
                    props.item.load_mode && h('div', { class: 'flex items-center gap-1' }, [
                        h('span', { class: 'text-[9px] text-gray-400' }, 'Load:'),
                        h('span', { class: 'text-[9px] px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded' }, props.item.load_mode)
                    ]),
                    props.item.category && h('div', { class: 'flex items-center gap-1' }, [
                        h('span', { class: 'text-[9px] text-gray-400' }, 'Category:'),
                        h('span', { class: 'text-[9px] px-1.5 py-0.5 bg-gray-100 text-gray-600 rounded' }, props.item.category)
                    ])
                ])
            ])
        ])
    }
})
</script>
