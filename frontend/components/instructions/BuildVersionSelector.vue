<template>
    <UTooltip :text="tooltip" :popper="{ placement: 'top' }">
        <button
            @click="openExplorer"
            class="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs bg-white border border-gray-200 rounded-md shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-1 focus:ring-gray-300"
        >
            <UIcon name="i-heroicons-cube" class="w-4 h-4 text-gray-500" />
            <span class="text-gray-700">{{ selectedLabel }}</span>
        </button>
    </UTooltip>
    
    <!-- Build Explorer Modal -->
    <BuildExplorerModal
        v-model="isExplorerOpen"
        :git-repo-id="gitRepoId"
        @rollback="(id: string) => emit('rollback', id)"
    />
</template>

<script setup lang="ts">
import BuildExplorerModal from './BuildExplorerModal.vue'

const props = withDefaults(defineProps<{
    /** Currently selected build ID (null = main/latest) */
    modelValue: string | null
    /** Loading state */
    loading?: boolean
    /** Git repository ID for push operations */
    gitRepoId?: string
}>(), {
    modelValue: null,
    loading: false,
    gitRepoId: ''
})

const emit = defineEmits<{
    'update:modelValue': [value: string | null]
    'rollback': [newBuildId: string]
}>()

// Modal state
const isExplorerOpen = ref(false)

const selectedLabel = computed(() => {
    return 'Latest'
})

const tooltip = computed(() => {
    if (props.loading) return 'Loading builds...'
    return 'Open Version Explorer'
})

const openExplorer = () => {
    isExplorerOpen.value = true
}
</script>
