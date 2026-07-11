<template>
    <div :class="props.iconOnly ? 'inline-flex' : 'flex items-center gap-2'">
        <!-- Live preview of the current icon (full mode only) -->
        <div v-if="!props.iconOnly" class="w-9 h-9 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800 flex items-center justify-center overflow-hidden">
            <DataSourceIcon :type="props.type" :connector-key="props.connectorKey" :icon="modelToken" class="w-5 h-5" />
        </div>

        <UPopover v-if="!props.disabled" :popper="{ placement: 'bottom-start' }">
            <!-- Compact trigger: the icon itself is clickable (used in the agent header) -->
            <button
                v-if="props.iconOnly"
                type="button"
                :title="hasCustom ? 'Change icon' : 'Set custom icon'"
                class="group inline-flex items-center justify-center rounded-md p-0.5 -m-0.5 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors relative"
            >
                <DataSourceIcon :type="props.type" :connector-key="props.connectorKey" :icon="modelToken" :class="props.iconClass || 'w-4 h-4'" class="shrink-0" />
                <UIcon name="i-heroicons-pencil" class="w-2.5 h-2.5 text-gray-400 dark:text-gray-500 absolute -bottom-1 -end-1 opacity-0 group-hover:opacity-100 bg-white dark:bg-gray-900 rounded-full" />
            </button>
            <button
                v-else
                type="button"
                class="h-8 px-3 rounded-lg border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 text-xs font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50"
            >
                {{ hasCustom ? 'Change icon' : 'Set custom icon' }}
            </button>

            <template #panel="{ close }">
                <div class="p-3 w-64">
                    <!-- Use one of the agent's connection icons -->
                    <template v-if="connectionOptions.length">
                        <div class="text-[11px] text-gray-400 dark:text-gray-500 mb-2">Use a connection icon</div>
                        <div class="flex flex-wrap gap-1 mb-3">
                            <button
                                v-for="c in connectionOptions"
                                :key="c.token"
                                type="button"
                                :title="c.name"
                                class="w-7 h-7 rounded-md flex items-center justify-center hover:bg-gray-100 dark:hover:bg-gray-800"
                                :class="modelToken === c.token ? 'bg-gray-100 dark:bg-gray-800 ring-1 ring-gray-300 dark:ring-gray-600' : ''"
                                @click="commit(c.token, true); close()"
                            >
                                <DataSourceIcon :type="c.type" :connector-key="c.connectorKey" class="w-4 h-4" />
                            </button>
                        </div>
                    </template>

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
        <!-- Disabled + icon-only: render the plain icon (no trigger). -->
        <DataSourceIcon v-else-if="props.iconOnly" :type="props.type" :connector-key="props.connectorKey" :icon="modelToken" :class="props.iconClass || 'w-4 h-4'" class="shrink-0" />
    </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const props = defineProps<{
    // The stored icon token ("emoji:<grapheme>" | "type:<key>" | "preset:<key>" | null).
    modelValue: string | null | undefined
    // Fallback type/connector for the default-icon preview.
    type?: string | null
    connectorKey?: string | null
    // The agent's connections, so the picker can offer their type/brand icons.
    connections?: Array<{ type?: string | null; connector_key?: string | null; name?: string | null }>
    // Compact mode: the icon itself is the clickable trigger (no preview box or
    // text button). Used in the agent-view header.
    iconOnly?: boolean
    // Size class for the icon in iconOnly mode (default w-4 h-4).
    iconClass?: string
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
// "Reset to default" is offered whenever any override is set (emoji or a pinned
// connection type icon).
const hasCustom = computed(() => parsed.value.kind === 'emoji' || parsed.value.kind === 'type')
const rawEmoji = computed(() => (parsed.value.kind === 'emoji' ? parsed.value.value : ''))

// Distinct connection icons for this agent. Each pins a "type:<key>" token where
// the key is the brand connector (e.g. "notion") when present, else the plain
// connection type (e.g. "snowflake"). Deduped so repeated types show once.
const connectionOptions = computed(() => {
    const out: { type?: string | null; connectorKey?: string | null; name: string; token: string }[] = []
    const seen = new Set<string>()
    for (const c of (props.connections || [])) {
        const key = (c.connector_key || c.type || '').toString().trim()
        if (!key) continue
        const token = `type:${key}`
        if (seen.has(token)) continue
        seen.add(token)
        out.push({ type: c.type, connectorKey: c.connector_key, name: c.name || key, token })
    }
    return out
})

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

// commit an emoji grapheme (default) or a pre-built token (raw = true).
function commit(value: string | null, raw = false) {
    const token = raw ? value : emojiToIconToken(value)
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
    commit(null, true)
}
</script>
