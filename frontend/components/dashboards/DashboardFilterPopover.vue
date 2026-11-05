<template>
    <!-- The funnel: the same button and popover as the Conversations list
         (pages/reports/index.vue). Each change is emitted at once as a
         partial list state; the page puts it in the URL. -->
    <div ref="rootRef" class="relative shrink-0">
        <UTooltip :text="t('dashboards.filters.button')">
            <button
                ref="buttonRef"
                type="button"
                class="relative inline-flex items-center justify-center w-8 h-8 rounded-lg border transition-colors"
                :class="open || count > 0
                    ? 'border-blue-200 dark:border-blue-900 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300'
                    : 'border-gray-200 dark:border-gray-700 text-ink-muted hover:bg-gray-50 dark:hover:bg-gray-800'"
                :aria-label="t('dashboards.filters.button')"
                :aria-expanded="open ? 'true' : 'false'"
                :aria-controls="open ? panelId : undefined"
                data-testid="dashboard-filters-button"
                @click="open = !open"
            >
                <UIcon name="i-heroicons-funnel" class="h-4 w-4" aria-hidden="true" />
                <span
                    v-if="count > 0"
                    class="absolute -top-1 -end-1 inline-flex items-center justify-center min-w-[15px] h-[15px] px-1 text-2xs font-semibold rounded-full bg-blue-600 text-white nums"
                    data-testid="dashboard-filters-count"
                >{{ count }}</span>
            </button>
        </UTooltip>

        <div
            v-if="open"
            :id="panelId"
            ref="panelRef"
            class="absolute end-0 z-dropdown mt-2 w-[380px] max-w-[92vw] bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 rounded-xl shadow-lg p-4"
            data-testid="dashboard-filters-popover"
        >
            <div class="space-y-3">
                <!-- Agent (multi) -->
                <div class="flex items-center justify-between gap-3">
                    <span class="text-xs text-ink-muted">{{ t('dashboards.filters.agent') }}</span>
                    <USelectMenu
                        :model-value="state.agents"
                        :options="agentOptions"
                        multiple
                        value-attribute="id"
                        option-attribute="name"
                        class="w-48"
                        data-testid="dashboard-filter-agent"
                        @update:model-value="(ids: string[]) => emit('update', { agents: ids })"
                    >
                        <template #label>
                            <span class="flex items-center gap-1.5 min-w-0 text-xs">
                                <DataSourceIcon
                                    v-for="agent in selectedAgents"
                                    :key="agent.id"
                                    :type="agent.type"
                                    :icon-token="agent.icon_token"
                                    class="h-3.5 shrink-0"
                                />
                                <span v-text-dir class="truncate">{{ agentLabel }}</span>
                            </span>
                        </template>
                        <template #option="{ option }">
                            <span class="flex items-center gap-1.5 w-full min-w-0 text-xs">
                                <DataSourceIcon :type="option.type" :icon-token="option.icon_token" class="h-3.5 shrink-0" />
                                <span v-text-dir class="truncate">{{ option.name }}</span>
                                <span v-if="option.count !== undefined" class="ms-auto text-ink-muted nums">{{ option.count }}</span>
                            </span>
                        </template>
                    </USelectMenu>
                </div>

                <!-- Created / Edited: presets, or a custom range -->
                <template v-for="key in DATE_KEYS" :key="key">
                    <div class="flex items-center justify-between gap-3">
                        <span class="text-xs text-ink-muted">{{ t(`dashboards.filters.${key}`) }}</span>
                        <USelectMenu
                            :model-value="shownPreset(key)"
                            :options="presetOptions"
                            value-attribute="value"
                            option-attribute="label"
                            class="w-48"
                            :data-testid="`dashboard-filter-${key}`"
                            @update:model-value="(preset: DatePreset) => setPreset(key, preset)"
                        >
                            <template #label>
                                <span class="text-xs whitespace-nowrap truncate">{{ presetLabel(key) }}</span>
                            </template>
                        </USelectMenu>
                    </div>
                    <div v-if="shownPreset(key) === 'custom'" class="flex items-center justify-end gap-2">
                        <UInput
                            type="date"
                            size="xs"
                            class="w-32"
                            :model-value="custom[key].from"
                            :max="custom[key].to || undefined"
                            :color="rangeInvalid(key) ? 'red' : undefined"
                            :aria-invalid="rangeInvalid(key) ? 'true' : undefined"
                            :aria-describedby="rangeInvalid(key) ? `${panelId}-${key}-invalid` : undefined"
                            :aria-label="t('dashboards.filters.from')"
                            :data-testid="`dashboard-filter-${key}-from`"
                            @update:model-value="(v: string) => setCustom(key, 'from', v)"
                        />
                        <span class="text-xs text-ink-muted">{{ t('dashboards.filters.to') }}</span>
                        <UInput
                            type="date"
                            size="xs"
                            class="w-32"
                            :model-value="custom[key].to"
                            :min="custom[key].from || undefined"
                            :color="rangeInvalid(key) ? 'red' : undefined"
                            :aria-invalid="rangeInvalid(key) ? 'true' : undefined"
                            :aria-describedby="rangeInvalid(key) ? `${panelId}-${key}-invalid` : undefined"
                            :aria-label="t('dashboards.filters.toLabel')"
                            :data-testid="`dashboard-filter-${key}-to`"
                            @update:model-value="(v: string) => setCustom(key, 'to', v)"
                        />
                    </div>
                    <p
                        v-if="shownPreset(key) === 'custom' && rangeInvalid(key)"
                        :id="`${panelId}-${key}-invalid`"
                        class="text-xs text-end text-red-600 dark:text-red-400"
                        role="alert"
                        :data-testid="`dashboard-filter-${key}-invalid`"
                    >{{ t('dashboards.filters.rangeInvalid') }}</p>
                </template>

                <!-- Owner (Shared, multi) -->
                <div v-if="tab === 'shared'" class="flex items-center justify-between gap-3">
                    <span class="text-xs text-ink-muted">{{ t('dashboards.filters.owner') }}</span>
                    <USelectMenu
                        :model-value="state.owners"
                        :options="ownerOptions"
                        multiple
                        value-attribute="id"
                        option-attribute="name"
                        class="w-48"
                        data-testid="dashboard-filter-owner"
                        @update:model-value="(ids: string[]) => emit('update', { owners: ids })"
                    >
                        <template #label>
                            <span v-text-dir class="text-xs whitespace-nowrap truncate">{{ ownerLabel }}</span>
                        </template>
                        <template #option="{ option }">
                            <span class="flex items-center gap-1.5 w-full min-w-0 text-xs">
                                <span v-text-dir class="truncate">{{ option.name }}</span>
                                <span v-if="option.count !== undefined" class="ms-auto text-ink-muted nums">{{ option.count }}</span>
                            </span>
                        </template>
                    </USelectMenu>
                </div>

                <!-- Status (My) -->
                <div v-if="tab === 'my'" class="flex items-center justify-between gap-3">
                    <span class="text-xs text-ink-muted">{{ t('dashboards.filters.status') }}</span>
                    <USelectMenu
                        :model-value="state.status"
                        :options="statusOptions"
                        value-attribute="value"
                        option-attribute="label"
                        class="w-48"
                        data-testid="dashboard-filter-status"
                        @update:model-value="(status: DashboardStatus) => emit('update', { status })"
                    >
                        <template #label>
                            <span class="text-xs whitespace-nowrap">{{ t(STATUS_LABEL[state.status]) }}</span>
                        </template>
                        <template #option="{ option }">
                            <span class="flex items-center gap-1.5 w-full text-xs">
                                <span class="truncate">{{ option.label }}</span>
                                <span v-if="option.count !== undefined" class="ms-auto text-ink-muted nums">{{ option.count }}</span>
                            </span>
                        </template>
                    </USelectMenu>
                </div>

                <!-- Starred only -->
                <div class="flex items-center justify-between gap-3">
                    <span class="text-xs text-ink-muted">{{ t('dashboards.filters.starredOnly') }}</span>
                    <UButton
                        color="gray"
                        variant="ghost"
                        size="xs"
                        square
                        :aria-label="t('dashboards.filters.starredOnly')"
                        :aria-pressed="state.starred ? 'true' : 'false'"
                        data-testid="dashboard-filter-starred"
                        @click="emit('update', { starred: !state.starred })"
                    >
                        <UIcon
                            :name="state.starred ? 'i-heroicons-star-solid' : 'i-heroicons-star'"
                            class="h-4 w-4"
                            :class="state.starred ? 'text-amber-400' : 'text-ink-subtle'"
                            aria-hidden="true"
                        />
                    </UButton>
                </div>
            </div>

            <div v-if="count > 0" class="mt-4 pt-3 border-t border-gray-100 dark:border-gray-800 flex justify-end">
                <UButton
                    color="gray"
                    variant="link"
                    size="xs"
                    :padded="false"
                    class="font-medium text-ink-muted"
                    data-testid="dashboard-filters-clear"
                    @click="clearFunnel"
                >{{ t('dashboards.filters.clearAll') }}</UButton>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import {
    PRESETS,
    STATUSES,
    activeFilterCount,
    type DashboardFacets,
    type DashboardListState,
    type DashboardStatus,
    type DatePreset,
    type FacetNames,
} from '~/utils/dashboardList'

const props = defineProps<{
    state: DashboardListState
    // Null while loading or when GET /facets failed: no counts then.
    facets: DashboardFacets | null
    tab: 'my' | 'shared'
    // Names of agents and owners seen in earlier facets, for a selected one
    // the current facets leave out (its count is 0).
    names?: FacetNames
}>()
const emit = defineEmits<{ (e: 'update', patch: Partial<DashboardListState>): void }>()

const { t } = useI18n()

const DATE_KEYS = ['created', 'edited'] as const
type DateKey = typeof DATE_KEYS[number]

const PRESET_LABEL: Record<DatePreset, string> = {
    any: 'dashboards.presets.any',
    '7': 'dashboards.presets.last7',
    '30': 'dashboards.presets.last30',
    '90': 'dashboards.presets.last90',
    year: 'dashboards.presets.year',
    older90: 'dashboards.presets.older90',
    custom: 'dashboards.presets.custom',
}
const STATUS_LABEL: Record<DashboardStatus, string> = {
    any: 'dashboards.status.any',
    published: 'dashboards.status.published',
    changes: 'dashboards.status.changes',
    draft: 'dashboards.status.draft',
}

const open = ref(false)
const rootRef = ref<HTMLElement | null>(null)
const buttonRef = ref<HTMLButtonElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)
const panelId = useId()
const count = computed(() => activeFilterCount(props.state))

// Agents: those the facets list (with counts), then any selected one they
// leave out, so it can still be cleared.
const agentOptions = computed(() => {
    const listed = (props.facets?.agents ?? []).map(a => ({ ...a, name: a.name || t('dashboards.filters.unknownAgent') }))
    const ids = new Set(listed.map(a => a.id))
    const missing = props.state.agents.filter(id => !ids.has(id)).map(id => {
        const known = props.names?.agents[id]
        return { id, name: known?.name || t('dashboards.filters.unknownAgent'), type: known?.type ?? null, icon_token: known?.icon_token ?? null, count: undefined as number | undefined }
    })
    return [...listed, ...missing]
})
const selectedAgents = computed(() => agentOptions.value.filter(a => props.state.agents.includes(a.id)))
const agentLabel = computed(() => {
    const n = props.state.agents.length
    if (!n) return t('dashboards.filters.allAgents')
    return n === 1 ? (selectedAgents.value[0]?.name ?? '') : t('dashboards.filters.agentsCount', { n }, n)
})

const ownerOptions = computed(() => {
    const listed = (props.facets?.owners ?? []).map(o => ({ id: o.id, name: o.name || t('dashboards.filters.unknownOwner'), count: o.count as number | undefined }))
    const ids = new Set(listed.map(o => o.id))
    const missing = props.state.owners.filter(id => !ids.has(id))
        .map(id => ({ id, name: props.names?.owners[id] || t('dashboards.filters.unknownOwner'), count: undefined }))
    return [...listed, ...missing]
})
const ownerLabel = computed(() => {
    const n = props.state.owners.length
    if (!n) return t('dashboards.filters.anyone')
    return n === 1
        ? (ownerOptions.value.find(o => o.id === props.state.owners[0])?.name ?? '')
        : t('dashboards.filters.ownersCount', { n }, n)
})

const statusOptions = computed(() => STATUSES.map(value => ({
    value,
    label: t(STATUS_LABEL[value]),
    count: props.facets?.status
        ? (value === 'any'
            ? props.facets.status.published + props.facets.status.changes + props.facets.status.draft
            : props.facets.status[value])
        : undefined,
})))

const presetOptions = computed(() => PRESETS.map(value => ({ value, label: t(PRESET_LABEL[value]) })))

// A custom range is kept here until it has a date: the URL holds only a
// range with at least one end, and only a from that is not after the to.
const pickingCustom = reactive<Record<DateKey, boolean>>({ created: false, edited: false })
const custom = reactive<Record<DateKey, { from: string; to: string }>>({
    created: { from: props.state.createdFrom, to: props.state.createdTo },
    edited: { from: props.state.editedFrom, to: props.state.editedTo },
})
for (const key of DATE_KEYS) {
    watch(() => [props.state[key], props.state[`${key}From`], props.state[`${key}To`]] as const, ([preset, from, to]) => {
        if (preset === 'custom') {
            custom[key] = { from, to }
            pickingCustom[key] = false
        } else if (preset !== 'any' || !pickingCustom[key]) {
            custom[key] = { from: '', to: '' }
            pickingCustom[key] = false
        }
    })
}

function shownPreset(key: DateKey): DatePreset {
    return pickingCustom[key] ? 'custom' : props.state[key]
}

// The applied range (the URL's), never dates typed but not applied yet.
function presetLabel(key: DateKey): string {
    const preset = shownPreset(key)
    if (preset !== 'custom') return t(PRESET_LABEL[preset])
    const from = props.state[`${key}From`]
    const to = props.state[`${key}To`]
    return props.state[key] === 'custom' && (from || to)
        ? t('dashboards.filters.customRange', { from: from || '…', to: to || '…' })
        : t(PRESET_LABEL.custom)
}

// Both ends typed, the start after the end: not applied, and said so.
function rangeInvalid(key: DateKey): boolean {
    const { from, to } = custom[key]
    return !!(from && to && from > to)
}

function setPreset(key: DateKey, preset: DatePreset) {
    if (preset === 'custom') {
        if (props.state[key] !== 'custom') {
            pickingCustom[key] = true
            custom[key] = { from: '', to: '' }
            // Leaving a preset for a range not picked yet: no filter meanwhile.
            if (props.state[key] !== 'any') emit('update', { [key]: 'any' } as Partial<DashboardListState>)
        }
        return
    }
    pickingCustom[key] = false
    emit('update', { [key]: preset, [`${key}From`]: '', [`${key}To`]: '' } as Partial<DashboardListState>)
}

function setCustom(key: DateKey, end: 'from' | 'to', value: string) {
    custom[key] = { ...custom[key], [end]: value || '' }
    const { from, to } = custom[key]
    if (from && to && from > to) return // not a range yet
    if (!from && !to) {
        if (props.state[key] === 'custom') {
            pickingCustom[key] = true
            emit('update', { [key]: 'any', [`${key}From`]: '', [`${key}To`]: '' } as Partial<DashboardListState>)
        }
        return
    }
    emit('update', { [key]: 'custom', [`${key}From`]: from, [`${key}To`]: to } as Partial<DashboardListState>)
}

// "Clear all" here clears what the funnel's badge counts.
function clearFunnel() {
    pickingCustom.created = false
    pickingCustom.edited = false
    emit('update', {
        agents: [], owners: [], status: 'any', starred: false,
        created: 'any', createdFrom: '', createdTo: '',
        edited: 'any', editedFrom: '', editedTo: '',
    })
}

// Close on a click outside (the event's path, taken when it was dispatched:
// a picked option may be gone from the DOM by the time this runs) and on Escape.
function onDocumentClick(e: MouseEvent) {
    if (open.value && rootRef.value && !e.composedPath().includes(rootRef.value)) open.value = false
}
// Escape from inside the panel hands focus back to the funnel button, so it
// is not lost with the removed panel.
function onKeydown(e: KeyboardEvent) {
    if (e.key !== 'Escape' || !open.value || e.defaultPrevented) return
    const focusInside = !!panelRef.value?.contains(document.activeElement)
    open.value = false
    if (focusInside) buttonRef.value?.focus()
}
onMounted(() => {
    document.addEventListener('click', onDocumentClick)
    document.addEventListener('keydown', onKeydown)
})
onBeforeUnmount(() => {
    document.removeEventListener('click', onDocumentClick)
    document.removeEventListener('keydown', onKeydown)
})
</script>
