<template>
    <div v-if="queries.length > 0" class="mb-4 space-y-2">
        <div v-for="query in queries" :key="query.id">
            <ToolWidgetPreview
                v-if="query.toolExecution"
                :tool-execution="query.toolExecution"
                :readonly="true"
                :initial-collapsed="true"
            />
            <!-- Fallback if no step data -->
            <div v-else class="border border-gray-100 rounded-lg bg-gray-50/50 px-3 py-2">
                <div class="flex items-center justify-between text-xs text-gray-500">
                    <span class="font-medium text-gray-700">{{ query.title || 'Untitled Query' }}</span>
                    <span class="text-[10px] text-gray-300 font-mono">{{ query.id.slice(0, 8) }}</span>
                </div>
                <div v-if="query.description" class="mt-1 text-xs text-gray-400">{{ query.description }}</div>
            </div>
        </div>

        <!-- Artifact preview -->
        <div
            v-if="artifactRef"
            class="border border-gray-100 rounded-lg bg-gray-50/50 px-3 py-2"
        >
            <div class="flex items-center gap-1.5 text-xs text-gray-500">
                <Icon name="heroicons:document-chart-bar" class="w-3.5 h-3.5" />
                <span class="font-medium text-gray-700">{{ artifactRef.title || 'Artifact' }}</span>
                <span class="text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-400">{{ artifactRef.mode }}</span>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import ToolWidgetPreview from '~/components/tools/ToolWidgetPreview.vue'

interface QueryItem {
    id: string
    title: string
    description?: string
    toolExecution?: any
}

interface ArtifactRef {
    id: string
    title: string
    mode: string
}

defineProps<{
    queries: QueryItem[]
    artifactRef?: ArtifactRef | null
}>()
</script>
