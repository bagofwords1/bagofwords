<template>
    <!-- On top of a conversation that joined a dashboard (collab01): whose
         work it continues, and what changed before -- built in code from the
         dashboard's structure and history, never from anyone's rows. Same
         place and shape as the fork banner. Only the conversation's own user
         gets rows; for anyone else this renders nothing. -->
    <div
        v-for="card in cards"
        :key="card.artifact_id"
        class="px-4 py-2 bg-violet-50 dark:bg-violet-950/40 border-b border-violet-100 dark:border-violet-900/60 text-xs text-violet-800 dark:text-violet-200"
        data-testid="continued-from-card"
    >
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5 flex-shrink-0" />
            <span>{{ headline(card) }}</span>
            <NuxtLink
                v-if="card.can_open_previous && card.continued_from_report_id"
                :to="`/reports/${card.continued_from_report_id}`"
                class="underline hover:text-violet-950 dark:hover:text-white transition-colors"
            >{{ $t('dashboardCollab.conversation.openTheirConversation') }}</NuxtLink>
            <span class="text-violet-300 dark:text-violet-700">·</span>
            <NuxtLink
                :to="`/d/${card.slug || card.artifact_id}`"
                class="inline-flex items-center gap-1 font-medium underline hover:text-violet-950 dark:hover:text-white transition-colors min-w-0"
            >
                <Icon name="heroicons:squares-2x2" class="w-3 h-3 flex-shrink-0" />
                <span class="truncate max-w-[16rem]">{{ card.title || $t('dashboardCollab.conversation.untitled') }}</span>
            </NuxtLink>
            <button
                v-if="hasDetails(card)"
                type="button"
                class="ms-auto inline-flex items-center gap-0.5 hover:text-violet-950 dark:hover:text-white transition-colors"
                :aria-expanded="isOpen(card)"
                data-testid="continued-from-toggle"
                @click="toggle(card)"
            >
                {{ $t('dashboardCollab.conversation.whatChangedBefore') }}
                <Icon :name="isOpen(card) ? 'heroicons:chevron-up' : 'heroicons:chevron-down'" class="w-3 h-3" />
            </button>
        </div>

        <!-- The copies of someone else's charts run again as this user. -->
        <div
            v-if="card.rerunning"
            class="mt-1.5 flex items-center gap-1.5 text-violet-700 dark:text-violet-300"
            data-testid="continued-from-rerunning"
            role="status"
        >
            <Spinner class="w-3 h-3" />
            <span>{{ $t('dashboardCollab.conversation.rerunning') }}</span>
        </div>

        <div v-if="isOpen(card)" class="mt-2 ps-5 space-y-2 text-violet-900/90 dark:text-violet-100/90" data-testid="continued-from-details">
            <div v-if="history(card).length">
                <div class="font-medium mb-0.5">{{ $t('dashboardCollab.conversation.historyHeading') }}</div>
                <ul class="space-y-0.5">
                    <li v-for="h in history(card)" :key="`v${h.version}`">
                        {{ $t('dashboardCollab.conversation.historyEntry', { version: h.version, name: h.by || $t('dashboardCollab.conversation.someone'), changes: changesText(h) }) }}
                    </li>
                </ul>
            </div>
            <div v-if="charts(card).length">
                <div class="font-medium mb-0.5">{{ $t('dashboardCollab.conversation.chartsHeading') }}</div>
                <ul class="space-y-0.5">
                    <li v-for="(c, i) in charts(card)" :key="`c${i}`">
                        {{ c.title || $t('dashboardCollab.conversation.untitled') }}<span
                            v-if="c.filters?.length"
                            class="text-violet-600/80 dark:text-violet-300/80"
                        > ({{ $t('dashboardCollab.conversation.chartFilters', { filters: list(c.filters) }) }})</span>
                    </li>
                </ul>
            </div>
            <div v-if="sources(card).length">
                <span class="font-medium">{{ $t('dashboardCollab.conversation.sourcesHeading') }}</span>
                {{ list(sources(card)) }}
            </div>
            <div v-if="card.summary?.notes">
                <div class="font-medium mb-0.5">{{ $t('dashboardCollab.conversation.notesHeading') }}</div>
                <p class="whitespace-pre-line" dir="auto">{{ card.summary.notes }}</p>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

interface HistoryEntry {
    version: number
    by?: string | null
    at?: string | null
    added?: string[]
    changed?: string[]
    removed?: string[]
}

interface ConversationCard {
    artifact_id: string
    title?: string | null
    slug?: string | null
    continued_from?: { id: string; name?: string | null } | null
    continued_from_report_id?: string | null
    can_open_previous?: boolean
    started_at?: string | null
    base_version_number?: number | null
    summary?: {
        charts?: Array<{ title?: string; query?: string; filters?: string[] }>
        sources?: string[]
        history?: HistoryEntry[]
        notes?: string | null
    } | null
    rerunning?: boolean
}

const props = defineProps<{ reportId: string }>()
// `rerun-done`: the charts finished running again as this user -- the page
// remounts its dashboard so it shows their rows.
const emit = defineEmits<{ (e: 'rerun-done'): void }>()

const { t, locale } = useI18n()
const { data: sessionUser } = useAuth()

const cards = ref<ConversationCard[]>([])
const opened = ref<Set<string>>(new Set())

const POLL_MS = 3000
// Re-running is a few seconds of work; the server gives a stuck step up
// after 5 minutes. Stop asking a little after that.
const MAX_POLLS = 110
let timer: ReturnType<typeof setTimeout> | null = null
let polls = 0
let alive = true
let loadedFor: string | null = null

function stop() {
    if (timer) clearTimeout(timer)
    timer = null
}

async function load() {
    stop()
    const id = props.reportId
    if (!alive || !id) return
    const wasRerunning = cards.value.some((c) => c.rerunning)
    let next: ConversationCard[] | null = null
    try {
        const { data, error } = await useMyFetch(`/dashboards/conversations/${encodeURIComponent(id)}`)
        if (!error.value && Array.isArray(data.value)) next = data.value as ConversationCard[]
    } catch { /* keep what is shown; the next tick asks again */ }
    // Left the page, or moved to another conversation, while this was in flight.
    if (!alive || id !== props.reportId) return
    if (next !== null) {
        if (loadedFor !== id) opened.value = new Set()
        loadedFor = id
        cards.value = next
    }
    const rerunning = cards.value.some((c) => c.rerunning)
    if (wasRerunning && !rerunning && next !== null) emit('rerun-done')
    if (rerunning && polls < MAX_POLLS) {
        polls += 1
        timer = setTimeout(load, POLL_MS)
    }
}

watch(() => props.reportId, () => {
    polls = 0
    cards.value = []
    load()
}, { immediate: true })

onBeforeUnmount(() => {
    alive = false
    stop()
})

const myId = computed(() => {
    const u = sessionUser.value as any
    return String(u?.user?.id || u?.id || '')
})

function headline(card: ConversationCard) {
    const version = card.base_version_number
    const who = card.continued_from
    if (who && myId.value && String(who.id) === myId.value) {
        return version
            ? t('dashboardCollab.conversation.continuedFromYoursVersion', { version })
            : t('dashboardCollab.conversation.continuedFromYours')
    }
    if (who?.name) {
        return version
            ? t('dashboardCollab.conversation.continuedFromVersion', { name: who.name, version })
            : t('dashboardCollab.conversation.continuedFrom', { name: who.name })
    }
    return version
        ? t('dashboardCollab.conversation.continuedFromPublishedVersion', { version })
        : t('dashboardCollab.conversation.continuedFromPublished')
}

const history = (card: ConversationCard) => card.summary?.history || []
const charts = (card: ConversationCard) => card.summary?.charts || []
const sources = (card: ConversationCard) => card.summary?.sources || []
const hasDetails = (card: ConversationCard) =>
    history(card).length > 0 || charts(card).length > 0 || sources(card).length > 0 || !!card.summary?.notes

const isOpen = (card: ConversationCard) => opened.value.has(card.artifact_id)
function toggle(card: ConversationCard) {
    const next = new Set(opened.value)
    if (next.has(card.artifact_id)) next.delete(card.artifact_id)
    else next.add(card.artifact_id)
    opened.value = next
}

// "A, B and C" in the reader's language.
function list(items: Array<string | null | undefined>) {
    const named = items.map((s) => s || t('dashboardCollab.conversation.untitled'))
    try {
        return new Intl.ListFormat(locale.value, { style: 'long', type: 'conjunction' }).format(named)
    } catch {
        return named.join(', ')
    }
}

function changesText(h: HistoryEntry) {
    const parts: string[] = []
    if (h.added?.length) parts.push(t('dashboardCollab.conversation.historyAdded', { items: list(h.added) }))
    if (h.changed?.length) parts.push(t('dashboardCollab.conversation.historyChanged', { items: list(h.changed) }))
    if (h.removed?.length) parts.push(t('dashboardCollab.conversation.historyRemoved', { items: list(h.removed) }))
    return parts.length ? parts.join(t('dashboardCollab.conversation.historySeparator')) : t('dashboardCollab.conversation.historyFirst')
}
</script>
