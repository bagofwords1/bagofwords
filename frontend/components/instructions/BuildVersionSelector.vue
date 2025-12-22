<template>
    <UTooltip :text="tooltip" :popper="{ placement: 'top' }">
        <USelectMenu
            :model-value="selectedBuild ?? undefined"
            @update:model-value="(v: any) => selectedBuild = v ?? null"
            :options="buildOptions"
            value-attribute="value"
            size="xs"
            :ui="{
                wrapper: 'relative',
                trigger: 'inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs bg-white border border-gray-200 rounded-md shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-1 focus:ring-gray-300',
                option: { size: 'xs', padding: 'px-2 py-1' }
            }"
            :ui-menu="{ width: 'w-56' }"
        >
            <template #leading>
                <UIcon name="i-heroicons-cube" class="w-4 h-4 text-gray-500" />
            </template>
            <template #label>
                <div class="flex items-center gap-1.5">
                    <span class="text-gray-700">{{ selectedLabel }}</span>
                    <!-- Info icon inside the selector - clickable to open diff modal -->
                    <UIcon 
                        v-if="modelValue && parentBuildId"
                        name="i-heroicons-information-circle" 
                        class="w-3.5 h-3.5 text-gray-400 hover:text-gray-600 cursor-pointer"
                        @click.stop="openDiffModal"
                    />
                </div>
            </template>
            <template #option="{ option }">
                <!-- Version Explorer option -->
                <div 
                    v-if="option.value === '__explorer__'"
                    class="flex items-center gap-1.5 text-xs w-full border-t border-gray-100 pt-1 -mt-1 text-blue-600"
                    @click.stop="openExplorer"
                >
                    <UIcon name="i-heroicons-folder-open" class="w-3.5 h-3.5 shrink-0" />
                    <span class="flex-1 font-medium">{{ option.label }}</span>
                </div>
                <!-- Regular build option -->
                <div v-else class="flex items-center gap-1.5 text-xs w-full">
                    <UIcon :name="getSourceIcon(option.source)" class="w-3.5 h-3.5 text-gray-500 shrink-0" />
                    <span class="flex-1">{{ option.label }}</span>
                    <!-- Info icon for builds with parent (not Latest) -->
                    <UIcon 
                        v-if="option.value && getParentBuildIdFor(option.value)"
                        name="i-heroicons-information-circle" 
                        class="w-3.5 h-3.5 text-gray-400 hover:text-blue-500 shrink-0"
                        @click.stop="openDiffModalFor(option.value)"
                    />
                </div>
            </template>
        </USelectMenu>
    </UTooltip>
    
    <!-- Build Explorer Modal -->
    <BuildExplorerModal
        v-model="isDiffModalOpen"
        :build-id="diffBuildId || ''"
        :compare-to-build-id="diffParentBuildId || ''"
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
}>(), {
    modelValue: null,
    builds: () => [],
    loading: false,
    maxBuilds: 30
})

const emit = defineEmits<{
    'update:modelValue': [value: string | null]
    'rollback': [newBuildId: string]
}>()

// Diff modal state
const isDiffModalOpen = ref(false)

const selectedBuild = computed({
    get: () => props.modelValue,
    set: (value) => {
        // Don't actually select the explorer option - it just opens the modal
        if (value === '__explorer__') {
            openExplorer()
            return
        }
        emit('update:modelValue', value)
    }
})

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

const buildOptions = computed(() => {
    const options: BuildOption[] = [
        { value: null, label: 'Latest', buildNumber: 0, source: 'user' }
    ]
    
    // Add available builds (limited)
    for (const build of limitedBuilds.value) {
        options.push({
            value: build.value,
            label: String(build.buildNumber),
            buildNumber: build.buildNumber,
            status: build.status,
            createdAt: build.createdAt,
            source: build.source,
            gitProvider: build.gitProvider
        })
    }
    
    // Add Version Explorer option at the end
    options.push({
        value: '__explorer__',
        label: 'Version Explorer',
        source: 'explorer'
    })
    
    return options
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
