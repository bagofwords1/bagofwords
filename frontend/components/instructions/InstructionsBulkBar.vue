<template>
    <div class="flex items-center gap-2">
        <!-- Selection info (only when selected) -->
        <template v-if="selectedCount > 0">
            <span class="text-xs font-medium text-gray-600">
                {{ selectedCount }} selected
            </span>
            <button 
                v-if="selectAllMode === 'page' && total > selectedCount"
                @click="$emit('select-all')"
                class="text-xs text-blue-600 hover:text-blue-700"
            >
                All {{ total }}
            </button>
            <button 
                @click="$emit('clear')"
                class="text-xs text-gray-400 hover:text-gray-600"
            >
                <UIcon name="i-heroicons-x-mark" class="w-3 h-3" />
            </button>
        </template>

        <!-- Update dropdown with organized sections -->
        <UDropdown 
            :items="menuItems" 
            :popper="{ placement: 'bottom-end' }"
            :disabled="selectedCount === 0"
            :ui="{ 
                item: { padding: 'py-1.5 px-3' },
                width: 'w-48'
            }"
        >
            <button
                :disabled="selectedCount === 0"
                class="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs bg-white border border-gray-200 rounded-md shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-1 focus:ring-gray-300 disabled:opacity-50 disabled:cursor-not-allowed"
            >
                <UIcon name="i-heroicons-pencil-square" class="w-4 h-4 text-gray-500" />
                <span class="text-gray-700">Update</span>
                <UIcon name="i-heroicons-chevron-down-20-solid" class="w-4 h-4 text-gray-400" />
            </button>
            <template #item="{ item }">
                <div 
                    class="flex items-center gap-2 text-xs w-full"
                    :class="[
                        { 
                            'opacity-70 cursor-default': item.disabled,
                            'font-medium text-gray-800 uppercase text-[10px] tracking-wide': item.header
                        },
                        item.class
                    ]"
                >
                    <UIcon v-if="item.icon && !item.header" :name="item.icon" class="w-3.5 h-3.5 shrink-0" />
                    <UIcon v-if="item.isLabel" name="i-heroicons-tag" class="w-3 h-3 shrink-0" />
                    <span 
                        v-if="item.color" 
                        class="w-2 h-2 rounded-full shrink-0" 
                        :style="{ backgroundColor: item.color }"
                    />
                    <span>{{ item.label }}</span>
                </div>
            </template>
        </UDropdown>
    </div>
</template>

<script setup lang="ts">
interface Label {
    id: string
    name: string
    color?: string | null
}

const props = withDefaults(defineProps<{
    selectedCount: number
    selectAllMode: 'none' | 'page' | 'all'
    total: number
    labels?: Label[]
}>(), {
    labels: () => []
})

const emit = defineEmits<{
    'select-all': []
    'clear': []
    'publish': []
    'archive': []
    'make-draft': []
    'load-always': []
    'load-intelligent': []
    'load-disabled': []
    'add-label': [labelId: string]
    'remove-label': [labelId: string]
    'open-scope-modal': []
    'delete': []
}>()

const menuItems = computed(() => {
    const items: any[][] = []
    
    // Status section
    items.push([
        { label: 'Status', header: true, disabled: true },
        { label: 'Publish', icon: 'i-heroicons-check', click: () => emit('publish') },
        { label: 'Draft', icon: 'i-heroicons-pencil', click: () => emit('make-draft') },
        { label: 'Archive', icon: 'i-heroicons-archive-box', click: () => emit('archive') },
        { label: 'Delete', icon: 'i-heroicons-trash', click: () => emit('delete'), class: 'text-red-600' },
    ])
    
    // Load mode section
    items.push([
        { label: 'Load Mode', header: true, disabled: true },
        { label: 'Always', icon: 'i-heroicons-arrow-path', click: () => emit('load-always') },
        { label: 'Smart', icon: 'i-heroicons-light-bulb', click: () => emit('load-intelligent') },
        { label: 'Disabled', icon: 'i-heroicons-x-circle', click: () => emit('load-disabled') },
    ])
    
    // Scope section
    items.push([
        { label: 'Scope', header: true, disabled: true },
        { label: 'Set Source...', icon: 'i-heroicons-server-stack', click: () => emit('open-scope-modal') },
    ])
    
    // Labels section (if labels exist)
    if (props.labels && props.labels.length > 0) {
        const addLabelActions: any[] = [
            { label: 'Add Label', header: true, disabled: true }
        ]
        
        props.labels.forEach(label => {
            addLabelActions.push({
                label: label.name,
                icon: 'i-heroicons-plus-circle',
                isLabel: true,
                color: label.color || '#94A3B8',
                click: () => emit('add-label', label.id)
            })
        })
        
        items.push(addLabelActions)
        
        const removeLabelActions: any[] = [
            { label: 'Remove Label', header: true, disabled: true }
        ]
        
        props.labels.forEach(label => {
            removeLabelActions.push({
                label: label.name,
                icon: 'i-heroicons-minus-circle',
                isLabel: true,
                color: label.color || '#94A3B8',
                click: () => emit('remove-label', label.id)
            })
        })
        
        items.push(removeLabelActions)
    }
    
    return items
})
</script>
