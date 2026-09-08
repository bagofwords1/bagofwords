<template>
    <div class="relative flex-grow min-w-0">
        <!-- Mirror technique: a transparent input over a token-coloured layer.
             The input owns caret, selection and IME; the layer is paint only. -->
        <div
            class="relative flex items-center h-8 rounded-md bg-white dark:bg-gray-800 ring-1 ring-inset text-[13px] font-mono"
            :class="shownError ? 'ring-red-300 dark:ring-red-800' : (focused ? 'ring-2 ring-blue-500' : 'ring-gray-300 dark:ring-gray-700')"
        >
            <UIcon name="i-heroicons-magnifying-glass" class="w-4 h-4 ms-2.5 text-gray-400 flex-shrink-0" />
            <div class="relative flex-grow h-full min-w-0">
                <div
                    ref="mirror"
                    aria-hidden="true"
                    class="absolute inset-0 flex items-center px-2 whitespace-pre overflow-hidden pointer-events-none"
                ><span v-if="!modelValue" class="text-gray-400 font-sans">{{ $t('monitoring.diagnosis.placeholder') }}</span><template v-else><span v-for="(seg, i) in segments" :key="i" :class="seg.cls">{{ seg.text }}</span></template></div>
                <input
                    ref="input"
                    :value="modelValue"
                    type="text"
                    spellcheck="false"
                    autocomplete="off"
                    autocapitalize="off"
                    class="relative w-full h-full px-2 bg-transparent outline-none text-transparent caret-gray-900 dark:caret-white"
                    :aria-label="$t('monitoring.diagnosis.queryAria')"
                    @input="onInput"
                    @keydown="onKeydown"
                    @focus="focused = true; refreshSuggestions()"
                    @blur="onBlur"
                    @click="refreshSuggestions"
                    @scroll="syncScroll"
                />
            </div>
            <div class="flex items-center gap-2 pe-2.5 flex-shrink-0 text-[11px] text-gray-400 font-sans">
                <span v-if="loading" class="animate-spin rounded-full h-3.5 w-3.5 border-b-2 border-blue-600"></span>
                <template v-else-if="!focused && !modelValue">
                    <kbd class="px-1 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900">/</kbd>
                    <span>{{ $t('monitoring.diagnosis.toFocus') }}</span>
                </template>
                <template v-else-if="focused && dirty">
                    <UIcon name="i-heroicons-arrow-uturn-left" class="w-3 h-3" />
                    <span>{{ $t('monitoring.diagnosis.toRun') }}</span>
                </template>
            </div>
        </div>

        <!-- Inline error, positioned from the parser -->
        <div v-if="shownError" class="flex items-center gap-1.5 mt-1.5 text-xs text-red-700 dark:text-red-400" data-testid="query-error">
            <UIcon name="i-heroicons-exclamation-circle" class="w-3.5 h-3.5 flex-shrink-0" />
            <span>{{ shownError.message }}<template v-if="shownError.suggestions.length">. {{ $t('monitoring.diagnosis.didYouMean') }} <button v-for="s in shownError.suggestions" :key="s" class="font-mono underline me-1" @mousedown.prevent="applySuggestionText(s)">{{ s }}</button></template></span>
        </div>

        <!-- Suggestions -->
        <div
            v-if="open && suggestions.length"
            class="absolute start-0 top-full mt-1 z-20 w-full max-w-[520px] p-1.5 rounded-lg bg-white dark:bg-gray-900 ring-1 ring-gray-200 dark:ring-gray-700 shadow-lg"
        >
            <div class="px-2.5 pt-1.5 pb-1 text-[11px] uppercase tracking-wider text-gray-400">
                {{ ctx.kind === 'field' ? $t('monitoring.diagnosis.suggestFields') : $t('monitoring.diagnosis.suggestValues', { field: ctx.kind === 'value' ? ctx.field.name : '' }) }}
            </div>
            <button
                v-for="(s, i) in suggestions"
                :key="s.insert"
                type="button"
                class="w-full flex items-center gap-2.5 h-8 px-2.5 rounded-md text-start"
                :class="i === active ? 'bg-gray-100 dark:bg-gray-800' : ''"
                @mousedown.prevent="accept(s)"
                @mousemove="active = i"
            >
                <span class="font-mono text-xs" :class="ctx.kind === 'field' ? 'text-blue-700 dark:text-blue-400' : 'text-gray-900 dark:text-white'">{{ s.label }}</span>
                <span v-if="s.type" class="text-[11px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500">{{ s.type }}</span>
                <span v-if="s.help" class="text-xs text-gray-500 truncate flex-grow">{{ s.help }}</span>
                <span v-if="s.count != null" class="text-xs text-gray-400 tabular-nums ms-auto">{{ s.count.toLocaleString() }}</span>
                <span v-if="i === active" class="text-[11px] text-gray-400 whitespace-nowrap">Tab · ↵</span>
            </button>
            <div v-if="ctx.kind === 'field'" class="px-2.5 pt-1.5 pb-1 border-t border-gray-100 dark:border-gray-800 mt-1 text-[11px] text-gray-400">
                {{ $t('monitoring.diagnosis.suggestFooter') }}
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { FIELDS, cursorContext, resolveField, type CursorContext, type QueryError, type Token } from '~/utils/diagnosisQuery'
import type { Facet } from '~/composables/useDiagnosisQuery'

interface Suggestion { label: string; insert: string; type?: string; help?: string; count?: number | null }

const props = defineProps<{
    modelValue: string
    tokens: Token[]
    error: QueryError | null
    loading: boolean
    dirty: boolean
    facets: (field: string, prefix: string, qWithoutTerm: string) => Promise<Facet[]>
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void; (e: 'run'): void }>()

const input = ref<HTMLInputElement | null>(null)
const mirror = ref<HTMLElement | null>(null)
const focused = ref(false)
const open = ref(false)
const active = ref(0)
const suggestions = ref<Suggestion[]>([])
const ctx = ref<CursorContext>({ kind: 'none' })
// An error at the very end of the text usually means "still typing" (status:|).
// Show it only once the user tries to run, or when it sits inside the text.
const attempted = ref(false)
const shownError = computed(() => {
    const e = props.error
    if (!e) return null
    if (attempted.value || e.position < props.modelValue.trimEnd().length) return e
    return null
})

const TOKEN_CLASS: Record<string, string> = {
    field: 'text-blue-700 dark:text-blue-400',
    colon: 'text-gray-400',
    op: 'text-gray-500',
    value: 'text-gray-900 dark:text-white',
    keyword: 'text-gray-500 font-semibold',
    paren: 'text-gray-400',
    text: 'text-gray-900 dark:text-white',
    phrase: 'text-emerald-700 dark:text-emerald-400',
}

// Segments tile the whole string: tokens get colours, gaps stay plain. When
// the query fails to parse, tokens are empty and the text renders plain.
const segments = computed(() => {
    const q = props.modelValue
    const out: { text: string; cls: string }[] = []
    let i = 0
    for (const t of props.tokens) {
        if (t.start > i) out.push({ text: q.slice(i, t.start), cls: '' })
        out.push({ text: q.slice(t.start, t.end), cls: TOKEN_CLASS[t.type] || '' })
        i = t.end
    }
    if (i < q.length) out.push({ text: q.slice(i), cls: shownError.value ? 'text-red-700 dark:text-red-400' : '' })
    return out
})

const syncScroll = () => {
    if (mirror.value && input.value) mirror.value.scrollLeft = input.value.scrollLeft
}

const onInput = (e: Event) => {
    attempted.value = false
    emit('update:modelValue', (e.target as HTMLInputElement).value)
    nextTick(() => { syncScroll(); refreshSuggestions() })
}

let facetTimer: ReturnType<typeof setTimeout> | null = null
let facetSeq = 0

const refreshSuggestions = () => {
    const el = input.value
    if (!el) return
    const caret = el.selectionStart ?? props.modelValue.length
    const c = cursorContext(props.modelValue, caret)
    ctx.value = c
    active.value = 0
    if (c.kind === 'field') {
        const p = c.prefix.toLowerCase()
        const list = FIELDS
            .filter(f => f.name.startsWith(p) || f.aliases.some(a => a.startsWith(p)) || (p.length >= 2 && f.name.includes(p)))
            .slice(0, 8)
            .map(f => ({ label: f.name, insert: f.name + ':', type: f.type, help: f.help }))
        suggestions.value = list
        open.value = list.length > 0 && (p.length > 0)
        return
    }
    if (c.kind === 'value') {
        const spec = c.field
        if (spec.type === 'enum') {
            const list = spec.values.filter(v => v.startsWith(c.prefix.toLowerCase())).map(v => ({ label: v, insert: v }))
            suggestions.value = list
            open.value = list.length > 0
            return
        }
        if (spec.facetable) {
            const mine = ++facetSeq
            if (facetTimer) clearTimeout(facetTimer)
            facetTimer = setTimeout(async () => {
                // The query minus the term being typed narrows the counts to the rest of the query.
                const without = (props.modelValue.slice(0, c.start - spec.name.length - 1) + props.modelValue.slice(caret)).trim()
                const rows = await props.facets(spec.name, c.prefix, without).catch(() => [])
                if (mine !== facetSeq) return
                suggestions.value = rows.map(r => ({ label: r.label, insert: /[\s()"]/.test(r.value) ? `"${r.value}"` : r.value, count: r.count }))
                open.value = suggestions.value.length > 0
            }, 150)
            return
        }
        if (spec.type === 'boolean') {
            suggestions.value = ['true', 'false'].filter(v => v.startsWith(c.prefix)).map(v => ({ label: v, insert: v }))
            open.value = suggestions.value.length > 0
            return
        }
        // number / duration / money / date: hint with examples, no network
        const hints: Record<string, string[]> = {
            number: ['>3', '<3', '1..5'],
            duration: ['>30s', '<5s', '1m..5m'],
            money: ['>$0.50', '<$0.01'],
            date: ['today', 'yesterday', '-7d', '2025-09-01'],
        }
        const ex = (hints[spec.type] || []).filter(v => v.startsWith(c.prefix))
        suggestions.value = c.prefix ? [] : ex.map(v => ({ label: v, insert: v, help: spec.help }))
        open.value = suggestions.value.length > 0
        return
    }
    suggestions.value = []
    open.value = false
}

const replaceRange = (start: number, end: number, insert: string, trailing = ' ') => {
    const q = props.modelValue
    const next = q.slice(0, start) + insert + trailing + q.slice(end)
    emit('update:modelValue', next)
    nextTick(() => {
        const el = input.value
        if (!el) return
        const pos = start + insert.length + trailing.length
        el.focus()
        el.setSelectionRange(pos, pos)
        syncScroll()
        refreshSuggestions()
    })
}

const accept = (s: Suggestion) => {
    const el = input.value
    const caret = el?.selectionStart ?? props.modelValue.length
    const c = ctx.value
    if (c.kind === 'field') replaceRange(c.start, caret, s.insert, '')
    else if (c.kind === 'value') replaceRange(c.start, caret, s.insert, c.op === 'any' ? '' : ' ')
    open.value = false
}

const applySuggestionText = (name: string) => {
    if (!props.error) return
    // Replace the unknown field at the error position with the suggestion.
    const q = props.modelValue
    const m = /^[A-Za-z_][A-Za-z0-9_.]*/.exec(q.slice(props.error.position))
    if (!m) return
    replaceRange(props.error.position, props.error.position + m[0].length, name, '')
}

const onKeydown = (e: KeyboardEvent) => {
    if (open.value && suggestions.value.length) {
        if (e.key === 'ArrowDown') { e.preventDefault(); active.value = (active.value + 1) % suggestions.value.length; return }
        if (e.key === 'ArrowUp') { e.preventDefault(); active.value = (active.value - 1 + suggestions.value.length) % suggestions.value.length; return }
        // Tab and Enter both take the highlighted suggestion (field or value);
        // Enter runs the query only once nothing is being suggested.
        if (e.key === 'Tab' || e.key === 'Enter') { e.preventDefault(); accept(suggestions.value[active.value]); return }
        if (e.key === 'Escape') { e.preventDefault(); open.value = false; return }
    }
    if (e.key === 'Enter') {
        e.preventDefault()
        open.value = false
        attempted.value = true
        emit('run')
        return
    }
    if (e.key === 'Escape') {
        (e.target as HTMLInputElement).blur()
    }
}

const onBlur = () => {
    focused.value = false
    open.value = false
}

// "/" focuses the bar from anywhere on the page (unless typing elsewhere).
const onGlobalKey = (e: KeyboardEvent) => {
    if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return
    const t = e.target as HTMLElement | null
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
    e.preventDefault()
    input.value?.focus()
}
onMounted(() => window.addEventListener('keydown', onGlobalKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKey))

defineExpose({ focus: () => input.value?.focus() })
</script>
