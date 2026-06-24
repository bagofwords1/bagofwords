<template>
    <div class="relative inline-block" ref="rootRef">
        <!-- Trigger: three-dots (kebab) -->
        <button
            type="button"
            @click.stop="toggle"
            class="flex items-center justify-center w-6 h-6 rounded-md text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
            :class="{ 'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300': open }"
            aria-label="Primary instruction actions"
        >
            <UIcon name="heroicons-ellipsis-horizontal" class="w-4 h-4" />
        </button>

        <!-- Panel -->
        <div
            v-if="open"
            class="absolute z-30 mt-1 end-0 w-72 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg shadow-lg overflow-hidden"
        >
            <!-- Default action list -->
            <div v-if="view === 'menu'" class="py-1">
                <button type="button" @click.stop="onEdit" class="menu-item">
                    <UIcon name="heroicons-pencil-square" class="w-4 h-4 text-gray-400" />
                    <span>Edit instruction</span>
                </button>
                <button type="button" @click.stop="openReplace" class="menu-item">
                    <UIcon name="heroicons-arrow-path-rounded-square" class="w-4 h-4 text-gray-400" />
                    <span>Replace with existing…</span>
                </button>

                <template v-if="canTrain">
                    <div class="my-1 border-t border-gray-100 dark:border-gray-800"></div>
                    <button type="button" @click.stop="onStartTraining" class="menu-item">
                        <UIcon name="heroicons-academic-cap" class="w-4 h-4 text-sky-500" />
                        <span>Start a training session</span>
                    </button>
                    <div class="px-3 pb-1.5 pt-0.5 text-[10px] leading-tight text-gray-400 dark:text-gray-600">
                        Opens a new report in training mode to refine this agent's instructions.
                    </div>
                </template>
            </div>

            <!-- Replace view: searchable existing instructions -->
            <div v-else-if="view === 'replace'">
                <div class="flex items-center gap-1.5 p-2 border-b border-gray-100 dark:border-gray-800">
                    <button type="button" @click.stop="view = 'menu'" class="p-0.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
                        <UIcon name="heroicons-chevron-left" class="w-4 h-4" />
                    </button>
                    <div class="relative flex-1">
                        <UIcon name="heroicons-magnifying-glass" class="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
                        <input
                            ref="searchRef"
                            v-model="search"
                            type="text"
                            placeholder="Search instructions…"
                            class="w-full h-8 ps-7 pe-2 text-xs border border-gray-200 dark:border-gray-700 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-200"
                            @input="onSearchInput"
                            @click.stop
                        />
                    </div>
                </div>

                <div class="max-h-64 overflow-y-auto py-1">
                    <div v-if="loading" class="px-3 py-4 text-center text-xs text-gray-400 dark:text-gray-600">Loading…</div>
                    <div v-else-if="items.length === 0" class="px-3 py-4 text-center text-xs text-gray-400 dark:text-gray-600">No instructions found</div>
                    <button
                        v-for="inst in items"
                        :key="inst.id"
                        type="button"
                        :disabled="inst.id === currentInstructionId"
                        @click.stop="select(inst)"
                        class="w-full text-start px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        :class="inst.id === currentInstructionId ? 'bg-blue-50/50 dark:bg-blue-950' : ''"
                    >
                        <div class="flex items-center gap-1.5">
                            <span class="text-xs font-medium text-gray-800 dark:text-gray-200 truncate">{{ inst.title || 'Untitled instruction' }}</span>
                            <span v-if="inst.id === currentInstructionId" class="text-[9px] px-1 py-0.5 bg-blue-100 dark:bg-blue-900/50 text-blue-700 rounded shrink-0">Current</span>
                        </div>
                        <div class="text-[11px] text-gray-500 dark:text-gray-400 line-clamp-2 mt-0.5">{{ inst.text }}</div>
                    </button>
                </div>

                <div class="px-3 py-2 border-t border-gray-100 dark:border-gray-800 text-[10px] text-gray-400 dark:text-gray-600">
                    The previous primary stays in your library — it's only unlinked.
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick } from 'vue'

const props = withDefaults(defineProps<{
    agentId: string
    currentInstructionId?: string | null
    canTrain?: boolean
}>(), {
    currentInstructionId: null,
    canTrain: false,
})

const emit = defineEmits<{
    (e: 'edit'): void
    (e: 'select', instruction: any): void
    (e: 'start-training'): void
}>()

const rootRef = ref<HTMLElement | null>(null)
const searchRef = ref<HTMLInputElement | null>(null)
const open = ref(false)
const view = ref<'menu' | 'replace'>('menu')
const loading = ref(false)
const items = ref<any[]>([])
const search = ref('')

function toggle() {
    open.value = !open.value
    if (open.value) view.value = 'menu'
}

function close() {
    open.value = false
}

function onEdit() {
    close()
    emit('edit')
}

function onStartTraining() {
    close()
    emit('start-training')
}

async function openReplace() {
    view.value = 'replace'
    await fetchInstructions()
    await nextTick()
    searchRef.value?.focus()
}

async function fetchInstructions() {
    loading.value = true
    try {
        const query: Record<string, any> = {
            skip: 0,
            limit: 50,
            status: 'published',
            data_source_ids: props.agentId,
            include_global: true,
        }
        if (search.value.trim()) query.search = search.value.trim()
        const { data, error } = await useMyFetch<any>('/instructions', { method: 'GET', query })
        if (error?.value) throw new Error(String(error.value))
        items.value = (data.value as any)?.items || []
    } catch (e) {
        items.value = []
    } finally {
        loading.value = false
    }
}

let searchTimeout: ReturnType<typeof setTimeout> | null = null
function onSearchInput() {
    if (searchTimeout) clearTimeout(searchTimeout)
    searchTimeout = setTimeout(fetchInstructions, 250)
}

function select(inst: any) {
    if (inst.id === props.currentInstructionId) return
    close()
    emit('select', inst)
}

function onOutsideClick(e: MouseEvent) {
    if (rootRef.value && !rootRef.value.contains(e.target as Node)) close()
}
onMounted(() => document.addEventListener('click', onOutsideClick))
onUnmounted(() => document.removeEventListener('click', onOutsideClick))
</script>

<style scoped>
.menu-item {
    @apply w-full flex items-center gap-2.5 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-start;
}
</style>
