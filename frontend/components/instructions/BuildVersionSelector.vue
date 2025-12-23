<template>
    <UTooltip :text="tooltip" :popper="{ placement: 'top' }">
        <UPopover :popper="{ placement: 'bottom-start' }">
            <UButton
                size="xs"
                color="white"
                variant="solid"
                class="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs bg-white border border-gray-200 rounded-md shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-1 focus:ring-gray-300"
            >
                <UIcon name="i-heroicons-cube" class="w-4 h-4 text-gray-500" />
                <span class="text-gray-700">{{ selectedLabel }}</span>
                <UIcon 
                    v-if="modelValue && parentBuildId"
                    name="i-heroicons-information-circle" 
                    class="w-3.5 h-3.5 text-gray-400 hover:text-gray-600 cursor-pointer"
                    @click.stop="openDiffModal"
                />
                <UIcon name="i-heroicons-chevron-down" class="w-3.5 h-3.5 text-gray-400" />
            </UButton>
            
            <template #panel="{ close }">
                <div class="w-56 bg-white rounded-md shadow-lg ring-1 ring-black ring-opacity-5 overflow-hidden">
                    <!-- Scrollable options area -->
                    <div class="max-h-48 overflow-y-auto">
                        <!-- Latest option -->
                        <div 
                            class="flex items-center gap-1.5 text-xs w-full px-2 py-1.5 cursor-pointer hover:bg-gray-100"
                            :class="{ 'bg-gray-50': !modelValue }"
                            @click="selectBuild(null); close()"
                        >
                            <UIcon name="i-heroicons-user" class="w-3.5 h-3.5 text-gray-500 shrink-0" />
                            <span class="flex-1">Latest</span>
                            <UIcon v-if="!modelValue" name="i-heroicons-check" class="w-3.5 h-3.5 text-primary-500 shrink-0" />
                        </div>
                        
                        <!-- Build options -->
                        <div 
                            v-for="build in limitedBuilds" 
                            :key="build.value"
                            class="flex items-center gap-1.5 text-xs w-full px-2 py-1.5 cursor-pointer hover:bg-gray-100"
                            :class="{ 'bg-gray-50': modelValue === build.value }"
                            @click="selectBuild(build.value); close()"
                        >
                            <UIcon :name="getSourceIcon(build.source)" class="w-3.5 h-3.5 text-gray-500 shrink-0" />
                            <span class="flex-1">{{ build.buildNumber }}</span>
                            <UIcon 
                                v-if="build.value && getParentBuildIdFor(build.value)"
                                name="i-heroicons-information-circle" 
                                class="w-3.5 h-3.5 text-gray-400 hover:text-blue-500 shrink-0"
                                @click.stop="openDiffModalFor(build.value)"
                            />
                            <UIcon v-if="modelValue === build.value" name="i-heroicons-check" class="w-3.5 h-3.5 text-primary-500 shrink-0" />
                        </div>
                    </div>
                    
                    <!-- Frozen footer - always visible -->
                    <div 
                        class="flex items-center gap-1.5 text-xs w-full px-2 py-2 cursor-pointer bg-gray-50 border-t border-gray-200 text-blue-600 hover:bg-gray-100"
                        @click="openExplorer(); close()"
                    >
                        <UIcon name="i-heroicons-folder-open" class="w-3.5 h-3.5 shrink-0" />
                        <span class="flex-1 font-medium">Version Explorer</span>
                    </div>
                </div>
            </template>
        </UPopover>
    </UTooltip>
    
    <!-- Build Explorer Modal -->
    <BuildExplorerModal
        v-model="isDiffModalOpen"
        :build-id="diffBuildId || ''"
        :compare-to-build-id="diffParentBuildId || ''"
        :git-repo-id="gitRepoId"
        @rollback="(id: string) => emit('rollback', id)"
    />
</template>

<script setup lang="ts">
import BuildExplorerModal from './BuildExplorerModal.vue'

interface BuildOption {
    value: string | null
    label: string
    buildNumber?: number
    status?: string
    createdAt?: string
    source?: string
    gitProvider?: string
}

const props = withDefaults(defineProps<{
    /** Currently selected build ID (null = main/latest) */
    modelValue: string | null
    /** Available builds */
    builds?: BuildOption[]
    /** Loading state */
    loading?: boolean
    /** Max builds to show (default 30) */
    maxBuilds?: number
    /** Git repository ID for push operations */
    gitRepoId?: string
}>(), {
    modelValue: null,
    builds: () => [],
    loading: false,
    maxBuilds: 30,
    gitRepoId: ''
})

const emit = defineEmits<{
    'update:modelValue': [value: string | null]
    'rollback': [newBuildId: string]
}>()

// Diff modal state
const isDiffModalOpen = ref(false)

const selectBuild = (value: string | null) => {
    emit('update:modelValue', value)
}

// Get icon based on source type
const getSourceIcon = (source?: string, gitProvider?: string): string => {
    if (source === 'git') {
        // Use git provider specific icon if available
        switch (gitProvider?.toLowerCase()) {
            case 'github': return 'i-simple-icons-github'
            case 'gitlab': return 'i-simple-icons-gitlab'
            case 'bitbucket': return 'i-simple-icons-bitbucket'
            default: return 'i-heroicons-code-bracket'
        }
    }
    if (source === 'ai') return 'i-heroicons-sparkles'
    return 'i-heroicons-user' // default for 'user' source
}

// Limit builds to maxBuilds (default 30)
const limitedBuilds = computed(() => {
    return props.builds.slice(0, props.maxBuilds)
})


const selectedLabel = computed(() => {
    if (!props.modelValue) return 'Latest'
    const build = props.builds.find(b => b.value === props.modelValue)
    return build ? String(build.buildNumber) : 'Latest'
})

// Find parent build ID for any given build
const getParentBuildIdFor = (buildId: string | null): string | null => {
    if (!buildId) return null
    
    const currentBuild = props.builds.find(b => b.value === buildId)
    if (!currentBuild?.buildNumber) return null
    
    // Find the previous build by number
    const parentBuild = props.builds.find(b => 
        b.buildNumber === (currentBuild.buildNumber! - 1)
    )
    
    return parentBuild?.value || null
}

// Find parent build (previous build by number) for currently selected
const parentBuildId = computed(() => getParentBuildIdFor(props.modelValue))

// State for which build to show in modal
const diffBuildId = ref<string | null>(null)
const diffParentBuildId = ref<string | null>(null)

const tooltip = computed(() => {
    if (props.loading) return 'Loading builds...'
    if (!props.modelValue) return 'Viewing latest (main) build'
    
    const build = props.builds.find(b => b.value === props.modelValue)
    if (!build) return 'Select build version'
    
    let text = `Build ${build.buildNumber}`
    if (build.source) text += ` • ${build.source}`
    if (build.createdAt) {
        const date = new Date(build.createdAt)
        text += ` • ${date.toLocaleDateString()}`
    }
    text += ' • Click ⓘ for changes'
    return text
})

const openDiffModal = () => {
    if (props.modelValue && parentBuildId.value) {
        diffBuildId.value = props.modelValue
        diffParentBuildId.value = parentBuildId.value
        isDiffModalOpen.value = true
    }
}

const openDiffModalFor = (buildId: string) => {
    const parentId = getParentBuildIdFor(buildId)
    if (parentId) {
        diffBuildId.value = buildId
        diffParentBuildId.value = parentId
        isDiffModalOpen.value = true
    }
}

const openExplorer = () => {
    // Open the modal without specific build - it will show all builds
    diffBuildId.value = null
    diffParentBuildId.value = null
    isDiffModalOpen.value = true
}
</script>
