<template>
    <div class="relative inline-block" ref="rootRef">
        <button
            type="button"
            @click.stop="toggle"
            class="text-[10px] text-blue-600 hover:underline"
        >
            {{ label }}
        </button>

        <!-- Dropdown panel -->
        <div
            v-if="open"
            class="absolute z-30 mt-1 start-0 w-80 bg-white border border-gray-200 rounded-lg shadow-lg overflow-hidden"
        >
            <!-- Search -->
            <div class="p-2 border-b border-gray-100">
                <div class="relative">
                    <UIcon name="heroicons-magnifying-glass" class="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
                    <input
                        ref="searchRef"
                        v-model="search"
                        type="text"
                        placeholder="Search instructions…"
                        class="w-full h-8 ps-7 pe-2 text-xs border border-gray-200 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-200"
                        @input="onSearchInput"
                        @click.stop
                    />
                </div>
            </div>

            <!-- List -->
            <div class="max-h-72 overflow-y-auto py-1">
                <div v-if="loading" class="px-3 py-4 text-center text-xs text-gray-400">
                    Loading…
                </div>
                <div v-else-if="items.length === 0" class="px-3 py-4 text-center text-xs text-gray-400">
                    No instructions found
                </div>
                <button
                    v-for="inst in items"
                    :key="inst.id"
                    type="button"
                    :disabled="inst.id === currentInstructionId"
                    @click.stop="select(inst)"
                    class="w-full text-start px-3 py-2 hover:bg-gray-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    :class="inst.id === currentInstructionId ? 'bg-blue-50/50' : ''"
                >
                    <div class="flex items-center gap-1.5">
                        <span class="text-xs font-medium text-gray-800 truncate">
                            {{ inst.title || 'Untitled instruction' }}
                        </span>
                        <span v-if="inst.id === currentInstructionId" class="text-[9px] px-1 py-0.5 bg-blue-100 text-blue-700 rounded shrink-0">
                            Current
                        </span>
                    </div>
                    <div class="text-[11px] text-gray-500 line-clamp-2 mt-0.5">
                        {{ inst.text }}
                    </div>
                </button>
            </div>

            <!-- Footer hint -->
            <div class="px-3 py-2 border-t border-gray-100 text-[10px] text-gray-400">
                The previous primary stays in your library — it's only unlinked.
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick } from 'vue'

const props = withDefaults(defineProps<{
    agentId: string
    currentInstructionId?: string | null
    label?: string
}>(), {
    label: 'Replace',
    currentInstructionId: null,
})

const emit = defineEmits<{
    (e: 'select', instruction: any): void
}>()

const rootRef = ref<HTMLElement | null>(null)
const searchRef = ref<HTMLInputElement | null>(null)
const open = ref(false)
const loading = ref(false)
const items = ref<any[]>([])
const search = ref('')

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

async function toggle() {
    open.value = !open.value
    if (open.value) {
        await fetchInstructions()
        await nextTick()
        searchRef.value?.focus()
    }
}

function select(inst: any) {
    if (inst.id === props.currentInstructionId) return
    open.value = false
    emit('select', inst)
}

function onOutsideClick(e: MouseEvent) {
    if (rootRef.value && !rootRef.value.contains(e.target as Node)) {
        open.value = false
    }
}
onMounted(() => document.addEventListener('click', onOutsideClick))
onUnmounted(() => document.removeEventListener('click', onOutsideClick))
</script>
