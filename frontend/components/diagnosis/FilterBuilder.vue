<template>
    <div class="relative flex-shrink-0">
        <button
            type="button"
            class="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-md text-[13px] font-medium text-gray-700 dark:text-gray-300 bg-white dark:bg-gray-800 ring-1 ring-inset hover:bg-gray-50 dark:hover:bg-gray-700"
            :class="open ? 'ring-2 ring-blue-500' : 'ring-gray-300 dark:ring-gray-700'"
            @click="toggle"
        >
            <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5 text-gray-500" />
            <span>{{ $t('monitoring.diagnosis.filterButton') }}</span>
        </button>

        <div
            v-if="open"
            ref="popover"
            class="absolute end-0 top-full mt-1 z-30 w-80 p-1.5 rounded-lg bg-white dark:bg-gray-900 ring-1 ring-gray-200 dark:ring-gray-700 shadow-lg"
        >
            <!-- Step 1: pick a field -->
            <template v-if="!field">
                <div class="flex items-center gap-2 h-8 px-2.5 mb-1 rounded-md ring-1 ring-gray-200 dark:ring-gray-700 text-[13px]">
                    <UIcon name="i-heroicons-magnifying-glass" class="w-3.5 h-3.5 text-gray-400" />
                    <input
                        ref="fieldSearch"
                        v-model="fieldQuery"
                        type="text"
                        class="flex-grow bg-transparent outline-none placeholder-gray-400 dark:text-white"
                        :placeholder="$t('monitoring.diagnosis.filterBy')"
                        @keydown.escape="close"
                    />
                </div>
                <div class="max-h-80 overflow-y-auto">
                    <template v-for="group in groups" :key="group.entity">
                        <div v-if="group.fields.length" class="px-2.5 pt-2 pb-1 text-[11px] uppercase tracking-wider text-gray-400">
                            {{ group.entity === 'run' ? $t('monitoring.diagnosis.groupRun') : $t('monitoring.diagnosis.groupTool') }}
                        </div>
                        <button
                            v-for="f in group.fields"
                            :key="f.name"
                            type="button"
                            class="w-full flex items-center gap-2.5 h-8 px-2.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800 text-start"
                            @click="pickField(f)"
                        >
                            <span class="text-[13px] text-gray-900 dark:text-white flex-grow">{{ f.name }}</span>
                            <span class="text-[11px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500">{{ f.type }}</span>
                            <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 text-gray-400" />
                        </button>
                    </template>
                </div>
            </template>

            <!-- Step 2: pick values -->
            <template v-else>
                <div class="flex items-center gap-1.5 px-2.5 pt-1.5 pb-2 text-xs text-gray-500">
                    <button type="button" class="inline-flex" @click="field = null">
                        <UIcon name="i-heroicons-chevron-left" class="w-3 h-3" />
                    </button>
                    <span class="font-medium text-gray-700 dark:text-gray-300">{{ field.name }}</span>
                    <span class="text-gray-300">·</span>
                    <span class="truncate">{{ field.help }}</span>
                </div>

                <!-- Values with counts (enum / facetable) -->
                <template v-if="field.facetable || field.type === 'enum'">
                    <div v-if="field.facetable && !field.values.length" class="flex items-center gap-2 h-8 px-2.5 mb-1 rounded-md ring-1 ring-gray-200 dark:ring-gray-700 text-[13px]">
                        <UIcon name="i-heroicons-magnifying-glass" class="w-3.5 h-3.5 text-gray-400" />
                        <input v-model="valueQuery" type="text" class="flex-grow bg-transparent outline-none dark:text-white" :placeholder="$t('monitoring.diagnosis.searchValues')" @input="loadValues" />
                    </div>
                    <div class="max-h-64 overflow-y-auto">
                        <div v-if="loadingValues" class="px-2.5 py-2 text-xs text-gray-400">{{ $t('monitoring.diagnosis.loadingValues') }}</div>
                        <div v-else-if="!values.length" class="px-2.5 py-2 text-xs text-gray-400">{{ $t('monitoring.diagnosis.noValues') }}</div>
                        <label
                            v-for="v in values"
                            :key="v.value"
                            class="flex items-center gap-2.5 h-8 px-2.5 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800 cursor-pointer"
                        >
                            <UCheckbox :model-value="picked.has(v.value)" @update:model-value="togglePick(v.value)" />
                            <span class="font-mono text-xs text-gray-900 dark:text-white flex-grow truncate">{{ v.label }}</span>
                            <span v-if="v.count != null" class="text-xs text-gray-400 tabular-nums">{{ v.count.toLocaleString() }}</span>
                        </label>
                    </div>
                </template>

                <!-- Typed values (number / duration / money / date / text without facets) -->
                <template v-else>
                    <div class="flex items-center gap-1.5 px-2.5 pb-1">
                        <USelectMenu v-if="opsFor(field).length > 1" v-model="op" :options="opsFor(field)" size="xs" class="w-20" />
                        <UInput v-model="typed" size="xs" class="flex-grow font-mono" :placeholder="placeholderFor(field)" @keydown.enter.prevent="add" />
                    </div>
                    <div class="px-2.5 pb-1 text-[11px] text-gray-400">{{ hintFor(field) }}</div>
                </template>

                <div class="flex items-center justify-between gap-2 px-2.5 pt-2 mt-1 border-t border-gray-100 dark:border-gray-800">
                    <span class="font-mono text-[11px] text-gray-500 truncate">{{ preview || '…' }}</span>
                    <UButton size="xs" color="blue" :disabled="!preview" @click="add">{{ $t('monitoring.diagnosis.addFilter') }}</UButton>
                </div>
            </template>
        </div>
    </div>
</template>

<script setup lang="ts">
import { FIELDS, type FieldSpec } from '~/utils/diagnosisQuery'
import type { Facet } from '~/composables/useDiagnosisQuery'

const props = defineProps<{
    facets: (field: string, prefix: string, qWithoutTerm: string) => Promise<Facet[]>
    query: string
}>()
const emit = defineEmits<{ (e: 'add', terms: string): void }>()
const { t } = useI18n()

const open = ref(false)
const popover = ref<HTMLElement | null>(null)
const fieldSearch = ref<HTMLInputElement | null>(null)
const fieldQuery = ref('')
const field = ref<FieldSpec | null>(null)
const values = ref<Facet[]>([])
const loadingValues = ref(false)
const valueQuery = ref('')
const picked = ref<Set<string>>(new Set())
const typed = ref('')
const op = ref('=')

const groups = computed(() => {
    const q = fieldQuery.value.trim().toLowerCase()
    const match = (f: FieldSpec) => f.builder && (!q || f.name.includes(q) || f.help.toLowerCase().includes(q))
    return [
        { entity: 'run', fields: FIELDS.filter(f => f.entity === 'run' && match(f)) },
        { entity: 'tool', fields: FIELDS.filter(f => f.entity === 'tool' && match(f)) },
    ]
})

const opsFor = (f: FieldSpec) => (['number', 'duration', 'money', 'date'].includes(f.type) ? ['=', '>', '>=', '<', '<='] : ['='])
const placeholderFor = (f: FieldSpec) => ({ number: '3', duration: '30s', money: '0.50', date: '2025-09-01', text: 'value', id: 'id', boolean: 'true', enum: '' } as Record<string, string>)[f.type]
const hintFor = (f: FieldSpec) => ({
    number: t('monitoring.diagnosis.hintNumber'),
    duration: t('monitoring.diagnosis.hintDuration'),
    money: t('monitoring.diagnosis.hintMoney'),
    date: t('monitoring.diagnosis.hintDate'),
    text: t('monitoring.diagnosis.hintText'),
} as Record<string, string>)[f.type] || ''

const quote = (v: string) => (/[\s()"]/.test(v) ? `"${v}"` : v)

const preview = computed(() => {
    const f = field.value
    if (!f) return ''
    if (f.facetable || f.type === 'enum') {
        const vals = Array.from(picked.value)
        if (!vals.length) return ''
        return vals.length === 1 ? `${f.name}:${quote(vals[0])}` : `${f.name}:(${vals.map(quote).join(' OR ')})`
    }
    const v = typed.value.trim()
    if (!v) return ''
    const prefix = op.value === '=' ? '' : op.value
    return `${f.name}:${prefix}${quote(v)}`
})

let valueSeq = 0
const loadValues = async () => {
    const f = field.value
    if (!f) return
    if (f.type === 'enum' && !f.facetable) {
        values.value = f.values.map(v => ({ value: v, label: v, count: null as any }))
        return
    }
    const mine = ++valueSeq
    loadingValues.value = true
    try {
        const rows = await props.facets(f.name, valueQuery.value.trim(), props.query).catch(() => [])
        if (mine !== valueSeq) return
        values.value = rows
        // Enum fields without data still list their values, at zero.
        if (f.type === 'enum') {
            const seen = new Set(rows.map(r => r.value))
            for (const v of f.values) if (!seen.has(v)) values.value.push({ value: v, label: v, count: 0 })
        }
    } finally {
        if (mine === valueSeq) loadingValues.value = false
    }
}

const pickField = (f: FieldSpec) => {
    field.value = f
    picked.value = new Set()
    typed.value = ''
    op.value = '='
    valueQuery.value = ''
    values.value = []
    loadValues()
}

const togglePick = (v: string) => {
    const next = new Set(picked.value)
    if (next.has(v)) next.delete(v)
    else next.add(v)
    picked.value = next
}

const add = () => {
    if (!preview.value) return
    emit('add', preview.value)
    close()
}

const close = () => {
    open.value = false
    field.value = null
    fieldQuery.value = ''
}

const toggle = () => {
    if (open.value) return close()
    open.value = true
    nextTick(() => fieldSearch.value?.focus())
}

const onDocClick = (e: MouseEvent) => {
    if (!open.value) return
    const root = popover.value?.parentElement
    if (root && !root.contains(e.target as Node)) close()
}
onMounted(() => document.addEventListener('mousedown', onDocClick))
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>
