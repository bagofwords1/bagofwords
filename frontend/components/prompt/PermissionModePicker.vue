<template>
    <!-- Permission mode for tool calls in this report: Ask / Auto / Allow all.
         grey = Ask, blue = Auto, amber = Allow all. -->
    <UPopover :popper="popper">
        <UTooltip :text="tooltip" :popper="{ strategy: 'fixed', placement: 'top' }">
            <button
                type="button"
                data-testid="permission-mode-button"
                :data-mode="chipMode"
                class="rounded-md px-2 py-1 text-xs flex items-center"
                :class="chipClass"
                :aria-label="tooltip"
            >
                <Icon :name="chipIcon" class="w-4 h-4 flex-shrink-0" />
                <span v-if="!compact" class="ms-1">{{ chipLabel }}</span>
            </button>
        </UTooltip>
        <template #panel="{ close }">
            <div class="p-2 text-xs w-[260px]" data-testid="permission-mode-panel">
                <div class="px-2 pb-1 text-gray-500 dark:text-gray-400">{{ $t('prompt.permissionMode.title') }}</div>
                <!-- The org settings did not load, or the report's stored mode
                     could not be read: the mode in force is unknown. A mode
                     can still be picked; it is stored for the report. -->
                <div v-if="unknown || storedUnknown" class="px-2 py-1.5 flex flex-col items-start gap-2">
                    <span class="text-gray-600 dark:text-gray-300 leading-snug">{{ $t('prompt.permissionMode.unknown.hint') }}</span>
                    <button
                        type="button"
                        data-testid="permission-mode-retry"
                        class="px-2 py-1 rounded border border-gray-200 dark:border-gray-700 hover:bg-gray-100 dark:hover:bg-gray-800/70 text-gray-800 dark:text-gray-100"
                        @click="() => { emit('retry'); close(); }"
                    >
                        {{ $t('prompt.permissionMode.retry') }}
                    </button>
                </div>
                <button
                    v-for="m in shown"
                    :key="m"
                    type="button"
                    :data-testid="`permission-mode-${m}`"
                    class="w-full text-start px-2 py-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 flex items-start justify-between gap-2"
                    @click="() => { pick(m); close(); }"
                >
                    <span class="flex items-start gap-2 min-w-0">
                        <Icon :name="m === 'allow_all' ? 'heroicons-shield-exclamation' : 'heroicons-shield-check'" class="w-4 h-4 mt-0.5 flex-shrink-0" :class="iconClass(m)" />
                        <span class="min-w-0">
                            <span class="block text-gray-800 dark:text-gray-100">
                                {{ $t(`prompt.permissionMode.modes.${m}.label`) }}
                                <span v-if="!unknown && m === orgDefault" class="text-ink-muted">· {{ $t('prompt.permissionMode.default') }}</span>
                            </span>
                            <span class="block text-gray-500 dark:text-gray-400 leading-snug">{{ $t(`prompt.permissionMode.modes.${m}.hint`) }}</span>
                        </span>
                    </span>
                    <Icon v-if="!modeUnknown && m === effective" name="heroicons-check" class="w-4 h-4 text-blue-500 flex-shrink-0 mt-0.5" />
                </button>
            </div>
        </template>
    </UPopover>
</template>

<script setup lang="ts">
import { computed } from 'vue'

type Mode = 'ask' | 'auto' | 'allow_all'

const props = defineProps<{
    // The report's own choice; null = follow the org default.
    modelValue: Mode | null
    orgDefault: Mode
    allowAllEnabled: boolean
    // The strictest cap among the report's agents (ask | auto), else null.
    agentCap?: 'ask' | 'auto' | null
    // The org settings failed to load: the default and the switch are unknown.
    unknown?: boolean
    // The report's stored mode is unknown: a save got no answer and the mode
    // could not be read back. No label is right until it is read or saved.
    storedUnknown?: boolean
    compact?: boolean
}>()
// null = follow the org default again; retry = load what is unknown again.
const emit = defineEmits<{ (e: 'update:modelValue', v: Mode | null): void, (e: 'retry'): void }>()
const { t } = useI18n()

const popper = { strategy: 'absolute' as const, placement: 'top-start' as const, offset: [0, 8] }

const options = computed<Mode[]>(() => (props.allowAllEnabled ? ['ask', 'auto', 'allow_all'] : ['ask', 'auto']))
// While the org settings are unknown only Ask is offered: it is safe whatever
// the default and the switch are.
const shown = computed<Mode[]>(() => (props.unknown ? ['ask'] : options.value))

// What the next run will use. Allow all without the switch acts as Ask, and
// an agent's cap lowers the mode to it: the same rules the server applies.
const RANK: Record<Mode, number> = { ask: 0, auto: 1, allow_all: 2 }
const effective = computed<Mode>(() => {
    const picked = props.modelValue || props.orgDefault || 'ask'
    const m: Mode = picked === 'allow_all' && !props.allowAllEnabled ? 'ask' : picked
    return props.agentCap && RANK[props.agentCap] < RANK[m] ? props.agentCap : m
})

// Never the Ask label for a mode nobody knows: the server may run Allow all.
// The report's own Ask is known whatever the org settings say.
const modeUnknown = computed(() => !!props.storedUnknown || (!!props.unknown && props.modelValue !== 'ask'))
const chipMode = computed(() => (modeUnknown.value ? 'unknown' : effective.value))
const chipLabel = computed(() => (modeUnknown.value
    ? t('prompt.permissionMode.unknown.label')
    : t(`prompt.permissionMode.modes.${effective.value}.label`)))
const chipIcon = computed(() => (modeUnknown.value
    ? 'heroicons-question-mark-circle'
    : effective.value === 'allow_all' ? 'heroicons-shield-exclamation' : 'heroicons-shield-check'))

const tooltip = computed(() => t('prompt.permissionMode.tooltip', { mode: chipLabel.value }))

const chipClass = computed(() => {
    if (modeUnknown.value) {
        return 'text-red-600 border border-red-200 hover:bg-red-50 dark:text-red-300 dark:border-red-800/60 dark:hover:bg-red-900/30'
    }
    if (effective.value === 'allow_all') {
        return 'text-amber-700 bg-amber-50 border border-amber-200 hover:bg-amber-100 dark:text-amber-300 dark:bg-amber-900/30 dark:border-amber-700/60'
    }
    if (effective.value === 'auto') {
        return 'text-blue-600 hover:bg-blue-50 dark:text-blue-300 dark:hover:bg-blue-900/30'
    }
    return 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800/50'
})

function iconClass(m: Mode) {
    return m === 'allow_all' ? 'text-amber-500' : m === 'auto' ? 'text-blue-500' : 'text-ink-muted'
}

// Picking the org's default mode clears the report's own choice, so the
// report keeps following the default if an admin changes it later. While the
// default is unknown the choice is always stored for the report.
function pick(m: Mode) {
    const next = !props.unknown && m === props.orgDefault ? null : m
    // While the stored mode is unknown, a pick is always saved, even the one shown.
    if (next !== props.modelValue || props.storedUnknown) emit('update:modelValue', next)
}
</script>
