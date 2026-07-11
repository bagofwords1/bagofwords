<template>
    <div class="flex items-center gap-2">
        <!-- Live preview of the current icon (custom emoji or default type icon) -->
        <div class="w-9 h-9 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 flex items-center justify-center overflow-hidden">
            <DataSourceIcon :type="props.type" :connector-key="props.connectorKey" :icon="modelToken" class="w-5 h-5" />
        </div>

        <UPopover v-if="!props.disabled" :popper="{ placement: 'bottom-start' }">
            <button
                type="button"
                class="h-8 px-3 rounded-lg border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50"
            >
                {{ hasCustom ? 'Change icon' : 'Set custom icon' }}
            </button>

            <template #panel="{ close }">
                <div class="p-3 w-64">
                    <div class="text-[11px] text-gray-400 dark:text-gray-500 mb-2">Pick an emoji</div>
                    <div class="grid grid-cols-8 gap-1 mb-3">
                        <button
                            v-for="e in PRESET_EMOJIS"
                            :key="e"
                            type="button"
                            class="w-7 h-7 rounded-md flex items-center justify-center text-lg leading-none hover:bg-gray-100 dark:hover:bg-gray-800"
                            :class="rawEmoji === e ? 'bg-gray-100 dark:bg-gray-800 ring-1 ring-gray-300 dark:ring-gray-600' : ''"
                            @click="pick(e); close()"
                        >{{ e }}</button>
                    </div>

                    <div class="text-[11px] text-gray-400 dark:text-gray-500 mb-1">Or paste any emoji</div>
                    <div class="flex items-center gap-2">
                        <input
                            v-model="freeInput"
                            type="text"
                            maxlength="8"
                            placeholder="🙂"
                            class="w-16 h-8 px-2 text-center text-lg bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 dark:bg-gray-800 dark:border-gray-700"
                            @keydown.enter.prevent="applyFree(); close()"
                        />
                        <button
                            type="button"
                            :disabled="!firstGrapheme(freeInput)"
                            class="h-8 px-3 rounded-lg bg-gray-900 text-white text-xs font-medium hover:bg-black disabled:opacity-40"
                            @click="applyFree(); close()"
                        >Use</button>
                    </div>

                    <div v-if="hasCustom" class="mt-3 pt-3 border-t border-gray-100 dark:border-gray-800">
                        <button
                            type="button"
                            class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 inline-flex items-center gap-1"
                            @click="clear(); close()"
                        >
                            <UIcon name="i-heroicons-arrow-uturn-left" class="w-3 h-3" />
                            Reset to default icon
                        </button>
                    </div>
                </div>
            </template>
        </UPopover>
    </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const props = defineProps<{
    // The stored icon token ("emoji:<grapheme>" | "preset:<key>" | null).
    modelValue: string | null | undefined
    // Fallback type/connector for the default-icon preview.
    type?: string | null
    connectorKey?: string | null
    disabled?: boolean
}>()

// modelValue in → token out. We also emit 'change' so parents can persist.
const emit = defineEmits<{
    (e: 'update:modelValue', value: string | null): void
    (e: 'change', value: string | null): void
}>()

// A small, task-oriented starter set — the free-input covers everything else.
const PRESET_EMOJIS = [
    '📊', '📈', '📉', '🗄️', '🧮', '💰', '🧾', '🏦',
    '🛒', '📦', '🚚', '🧑‍💻', '🤖', '🧠', '🔍', '📡',
    '⚙️', '🔧', '🗂️', '📁', '🗃️', '📇', '🧩', '🔗',
    '🌐', '☁️', '⚡', '🔥', '⭐', '🎯', '🧪', '🩺',
]

const modelToken = computed(() => props.modelValue ?? null)
const parsed = computed(() => parseAgentIcon(modelToken.value))
const hasCustom = computed(() => parsed.value.kind === 'emoji')
const rawEmoji = computed(() => (parsed.value.kind === 'emoji' ? parsed.value.value : ''))

const freeInput = ref('')
watch(modelToken, () => { freeInput.value = '' })

// Grab the first user-perceived character (handles multi-codepoint emoji).
function firstGrapheme(s: string | null | undefined): string {
    const str = (s || '').trim()
    if (!str) return ''
    try {
        // @ts-ignore - Intl.Segmenter is widely available in modern browsers
        if (typeof Intl !== 'undefined' && (Intl as any).Segmenter) {
            const seg = new (Intl as any).Segmenter(undefined, { granularity: 'grapheme' })
            for (const g of seg.segment(str)) return g.segment
        }
    } catch { /* fall through */ }
    return Array.from(str)[0] || ''
}

function commit(emoji: string | null) {
    const token = emojiToIconToken(emoji)
    emit('update:modelValue', token)
    emit('change', token)
}

function pick(e: string) {
    commit(e)
}

function applyFree() {
    const g = firstGrapheme(freeInput.value)
    if (g) commit(g)
}

function clear() {
    commit(null)
}
</script>
