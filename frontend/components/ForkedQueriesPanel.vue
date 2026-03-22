<template>
    <div v-if="queries.length > 0" class="border border-gray-100 rounded-lg bg-gray-50/50 mb-4">
        <!-- Header -->
        <button
            @click="isCollapsed = !isCollapsed"
            class="w-full flex items-center justify-between px-3 py-2 text-xs text-gray-500 hover:text-gray-700 transition-colors"
        >
            <span class="flex items-center gap-1.5">
                <Icon name="heroicons:chart-bar" class="w-3.5 h-3.5" />
                <span class="font-medium">Inherited Queries ({{ queries.length }})</span>
            </span>
            <Icon
                :name="isCollapsed ? 'heroicons:chevron-right' : 'heroicons:chevron-down'"
                class="w-3.5 h-3.5"
            />
        </button>

        <!-- Query cards -->
        <Transition name="fade">
            <div v-if="!isCollapsed" class="px-3 pb-3 space-y-2">
                <div
                    v-for="query in queries"
                    :key="query.id"
                    class="border border-gray-200 rounded-md bg-white overflow-hidden"
                >
                    <!-- Query header -->
                    <button
                        @click="toggleQuery(query.id)"
                        class="w-full flex items-center justify-between px-3 py-2 text-xs hover:bg-gray-50 transition-colors"
                    >
                        <span class="font-medium text-gray-700 truncate">
                            {{ query.title || 'Untitled Query' }}
                        </span>
                        <div class="flex items-center gap-2 flex-shrink-0">
                            <span class="text-[10px] text-gray-300 font-mono">{{ query.id.slice(0, 8) }}</span>
                            <Icon
                                :name="isQueryCollapsed(query.id) ? 'heroicons:chevron-right' : 'heroicons:chevron-down'"
                                class="w-3 h-3 text-gray-400"
                            />
                        </div>
                    </button>

                    <!-- Query content (reuses ToolWidgetPreview rendering) -->
                    <div v-if="!isQueryCollapsed(query.id)">
                        <ToolWidgetPreview
                            v-if="query.toolExecution"
                            :tool-execution="query.toolExecution"
                            :readonly="true"
                            :initial-collapsed="false"
                        />
                        <!-- Description fallback if no step data -->
                        <div v-else-if="query.description" class="px-3 pb-2 text-xs text-gray-500">
                            {{ query.description }}
                        </div>
                    </div>
                </div>

                <!-- Artifact preview -->
                <div
                    v-if="artifactRef"
                    class="border border-gray-200 rounded-md bg-white px-3 py-2"
                >
                    <div class="flex items-center gap-1.5 text-xs text-gray-500">
                        <Icon name="heroicons:document-chart-bar" class="w-3.5 h-3.5" />
                        <span class="font-medium text-gray-700">{{ artifactRef.title || 'Artifact' }}</span>
                        <span class="text-[10px] px-1 py-0.5 rounded bg-gray-100 text-gray-400">{{ artifactRef.mode }}</span>
                    </div>
                </div>
            </div>
        </Transition>
    </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
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

const props = defineProps<{
    queries: QueryItem[]
    artifactRef?: ArtifactRef | null
}>()

const isCollapsed = ref(false)
const collapsedQueries = ref<Set<string>>(new Set())

function toggleQuery(id: string) {
    if (collapsedQueries.value.has(id)) {
        collapsedQueries.value.delete(id)
    } else {
        collapsedQueries.value.add(id)
    }
}

function isQueryCollapsed(id: string): boolean {
    return collapsedQueries.value.has(id)
}
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
    transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
    opacity: 0;
}
</style>
