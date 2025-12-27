<template>
    <UTooltip :text="tooltip">
        <button
            @click="$emit('click')"
            class="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs bg-white border border-gray-200 rounded-md shadow-sm hover:bg-gray-50 focus:outline-none focus:ring-1 focus:ring-gray-300"
        >
            <span class="relative">
                <!-- GitBranchIcon when not connected -->
                <GitBranchIcon v-if="!hasConnection" class="w-4 h-4 text-gray-500" />
                <!-- Provider icon when connected -->
                <UIcon v-else :name="providerIcon" class="w-4 h-4" />
                <!-- Green dot indicator when connected -->
                <span 
                    v-if="hasConnection" 
                    class="absolute -top-0.5 -right-0.5 w-1.5 h-1.5 rounded-full bg-green-500"
                ></span>
            </span>
            <span class="text-gray-700">{{ label }}</span>
        </button>
    </UTooltip>
</template>

<script setup lang="ts">
import GitBranchIcon from '~/components/icons/GitBranchIcon.vue'

interface GitRepo {
    provider: string
    repoName: string
}

const props = withDefaults(defineProps<{
    /** Whether there's at least one git connection */
    hasConnection: boolean
    /** Connected repositories info */
    connectedRepos?: GitRepo[]
    /** Custom label override */
    customLabel?: string
    /** Custom tooltip override */
    customTooltip?: string
    /** Last indexed timestamp for tooltip */
    lastIndexedAt?: string | null
}>(), {
    hasConnection: false,
    connectedRepos: () => [],
    lastIndexedAt: null
})

defineEmits<{
    click: []
}>()

// Git provider icons mapping
const gitProviderIcons: Record<string, string> = {
    github: 'logos:github-icon',
    gitlab: 'logos:gitlab',
    bitbucket: 'logos:bitbucket',
    custom: 'i-heroicons-server',
}

const providerIcon = computed(() => {
    if (props.connectedRepos.length === 0) return 'i-heroicons-code-bracket'
    const provider = props.connectedRepos[0].provider || 'custom'
    return gitProviderIcons[provider] || 'i-heroicons-code-bracket'
})

const label = computed(() => {
    if (props.customLabel) return props.customLabel
    if (!props.hasConnection || props.connectedRepos.length === 0) return 'Connect Git'
    if (props.connectedRepos.length === 1) return props.connectedRepos[0].repoName
    return `${props.connectedRepos.length} Repos`
})

const tooltip = computed(() => {
    if (props.customTooltip) return props.customTooltip
    if (!props.hasConnection) return 'Connect a Git repository'
    
    let tooltipText = props.connectedRepos.length === 1 
        ? 'Connected' 
        : `${props.connectedRepos.length} repos connected`
    
    if (props.lastIndexedAt) {
        const date = new Date(props.lastIndexedAt)
        const now = new Date()
        const diffMs = now.getTime() - date.getTime()
        const diffMins = Math.floor(diffMs / 60000)
        const diffHours = Math.floor(diffMs / 3600000)
        const diffDays = Math.floor(diffMs / 86400000)
        
        let timeAgo = ''
        if (diffMins < 1) timeAgo = 'just now'
        else if (diffMins < 60) timeAgo = `${diffMins}m ago`
        else if (diffHours < 24) timeAgo = `${diffHours}h ago`
        else timeAgo = `${diffDays}d ago`
        
        tooltipText += ` • Last indexed ${timeAgo}`
    }
    
    return tooltipText
})
</script>





