<template>
    <div class="h-full border-x border-gray-200 bg-gradient-to-b from-gray-50 to-white flex flex-col">
        <!-- Panel header -->
        <div class="px-4 py-3 border-b border-gray-100 flex items-center justify-between shrink-0">
            <h3 class="text-sm font-semibold text-gray-800">{{ $t('instructionModal.analysis') }}</h3>
            <UButton size="xs" variant="ghost" color="blue" @click="$emit('refresh')">
                <Icon name="heroicons:arrow-path" class="w-3.5 h-3.5 me-1" />
                {{ $t('instructionModal.refresh') }}
            </UButton>
        </div>
        <div class="flex-1 overflow-y-auto p-4 space-y-4">
            <!-- Related Instructions -->
            <div class="rounded-lg border border-gray-200 bg-white shadow-sm">
                <div class="flex items-center justify-between p-3 cursor-pointer hover:bg-gray-50 transition-colors" @click="showRelated = !showRelated">
                    <div class="flex items-center gap-2">
                        <h3 class="text-sm font-medium text-gray-900">{{ $t('instructionModal.related') }}</h3>
                        <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600">{{ related.length }}</span>
                    </div>
                    <Icon :name="showRelated ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" class="w-4 h-4 text-gray-400 transition-transform" />
                </div>
                <div v-show="showRelated" class="border-t border-gray-100 p-3 overflow-y-auto" :style="{ maxHeight: sectionMaxHeight }">
                    <div v-if="isLoadingRelated" class="py-6 flex items-center justify-center text-gray-500">
                        <Spinner class="w-4 h-4 me-2" /> <span class="text-xs">{{ $t('instructionModal.loading') }}</span>
                    </div>
                    <div v-else-if="related.length === 0" class="text-xs text-gray-500 py-2">{{ $t('instructionModal.noRelated') }}</div>
                    <ul v-else class="divide-y divide-gray-100">
                        <li v-for="inst in related" :key="inst.id" class="py-2">
                            <div class="flex-1">
                                <!-- Collapsed view with highlighted snippet -->
                                <div v-if="expandedInstructionId !== inst.id">
                                    <p class="text-xs text-gray-900 related-text" v-html="inst.highlightedHtml"></p>
                                    <div class="mt-1 flex items-center gap-2">
                                        <span class="inline-flex px-1.5 py-0.5 rounded-full text-[10px]"
                                              :class="inst.status === 'published' ? 'bg-green-100 text-green-800' : inst.status === 'draft' ? 'bg-yellow-100 text-yellow-800' : 'bg-gray-100 text-gray-800'">
                                            {{ inst.status }}
                                        </span>
                                        <span class="text-[10px] text-gray-500">{{ $t('instructionModal.by', { name: inst.createdByName }) }}</span>
                                        <button
                                            @click="expandedInstructionId = inst.id"
                                            class="text-[10px] text-blue-600 hover:text-blue-800 hover:underline"
                                        >
                                            {{ $t('instructionModal.readMore') }}
                                        </button>
                                    </div>
                                </div>
                                <!-- Expanded view with full MDC content -->
                                <div v-else class="space-y-2">
                                    <div class="bg-gray-50 rounded-lg p-3 border border-gray-100">
                                        <MDC :value="inst.text" class="text-xs text-gray-900 prose prose-xs max-w-none" />
                                    </div>
                                    <div class="flex items-center gap-2">
                                        <span class="inline-flex px-1.5 py-0.5 rounded-full text-[10px]"
                                              :class="inst.status === 'published' ? 'bg-green-100 text-green-800' : inst.status === 'draft' ? 'bg-yellow-100 text-yellow-800' : 'bg-gray-100 text-gray-800'">
                                            {{ inst.status }}
                                        </span>
                                        <span class="text-[10px] text-gray-500">{{ $t('instructionModal.by', { name: inst.createdByName }) }}</span>
                                        <button
                                            @click="expandedInstructionId = null"
                                            class="text-[10px] text-blue-600 hover:text-blue-800 hover:underline"
                                        >
                                            {{ $t('instructionModal.showLess') }}
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </li>
                    </ul>
                </div>
            </div>

            <!-- Impact Estimation -->
            <div class="rounded-lg border border-gray-200 bg-white shadow-sm">
                <div class="flex items-center justify-between p-3 cursor-pointer hover:bg-gray-50 transition-colors" @click="showImpact = !showImpact">
                    <div class="flex items-center gap-2">
                        <h3 class="text-sm font-medium text-gray-900">{{ $t('instructionModal.impact') }}</h3>
                        <UTooltip :text="impactTotalCount ? $t('instructionModal.impactTooltip', { matched: impactMatchedCount, total: impactTotalCount }) : $t('instructionModal.impactTooltipEmpty')">
                            <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
                                {{ Math.round(impactScore * 100) }}%
                            </span>
                        </UTooltip>
                    </div>
                    <Icon :name="showImpact ? 'heroicons:chevron-down' : 'heroicons:chevron-right'" class="w-4 h-4 text-gray-400 transition-transform" />
                </div>
                <div v-show="showImpact" class="border-t border-gray-100 p-3 overflow-y-auto" :style="{ maxHeight: sectionMaxHeight }">
                    <p class="text-xs text-gray-500 mb-2">{{ $t('instructionModal.sampleImpacted') }}</p>
                    <div v-if="isLoadingImpact" class="py-6 flex items-center justify-center text-gray-500">
                        <Spinner class="w-4 h-4 me-2" /> <span class="text-xs">{{ $t('instructionModal.loading') }}</span>
                    </div>
                    <div v-else-if="impactedPrompts.length === 0" class="text-xs text-gray-500 py-2">{{ $t('instructionModal.noRelevantPrompts') }}</div>
                    <ul v-else class="divide-y divide-gray-100">
                        <li v-for="(prompt, idx) in impactedPrompts" :key="idx" class="py-2">
                            <div class="flex items-start justify-between gap-3">
                                <p class="text-xs text-gray-900 flex-1">{{ prompt.content }}</p>
                                <span v-if="prompt.created_at" class="text-[10px] text-gray-500 whitespace-nowrap">{{ formatDate(prompt.created_at) }}</span>
                            </div>
                        </li>
                    </ul>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

interface RelatedInstruction {
    id: string
    text: string
    status: 'draft' | 'published' | 'archived'
    createdByName: string
    highlightedHtml: string
}
interface PromptSample {
    content: string
    created_at?: string | null
}

withDefaults(defineProps<{
    related?: RelatedInstruction[]
    isLoadingRelated?: boolean
    impactedPrompts?: PromptSample[]
    isLoadingImpact?: boolean
    impactScore?: number
    impactMatchedCount?: number
    impactTotalCount?: number
    sectionMaxHeight?: string
}>(), {
    related: () => [],
    isLoadingRelated: false,
    impactedPrompts: () => [],
    isLoadingImpact: false,
    impactScore: 0,
    impactMatchedCount: 0,
    impactTotalCount: 0,
    sectionMaxHeight: 'calc((min(85vh, 800px) - 120px) / 2)',
})

defineEmits<{ (e: 'refresh'): void }>()

const showRelated = ref(true)
const showImpact = ref(true)
const expandedInstructionId = ref<string | null>(null)

const _df = useFormatDate()
const formatDate = (d: string | Date | null | undefined) => {
    if (!d) return ''
    return _df.format(d, { year: 'numeric', month: 'short', day: 'numeric' })
}
</script>

<style scoped>
.related-text :deep(mark) {
    background-color: #fef08a;
    color: inherit;
    padding: 0 1px;
    border-radius: 2px;
}
</style>
