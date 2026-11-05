<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm">
        <div class="w-full max-w-7xl px-4 ps-0 py-2">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
                    {{ $t('dashboards.title') }}
                </h1>
                <p class="mt-2 text-ink-muted">{{ $t('dashboards.subtitle') }}</p>
            </div>

            <div class="mt-6">
                <!-- Toolbar: search, the funnel, the sort, grid or list -->
                <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
                    <div class="flex-1 max-w-md w-full">
                        <UInput
                            v-model="searchInput"
                            icon="i-heroicons-magnifying-glass"
                            :placeholder="$t('dashboards.searchPlaceholder')"
                            :aria-label="$t('dashboards.searchPlaceholder')"
                            maxlength="200"
                            data-testid="dashboard-search"
                            @blur="endSearchBurst"
                        />
                    </div>
                    <div class="flex items-center gap-2">
                        <DashboardFilterPopover
                            :state="state"
                            :facets="facets"
                            :tab="state.tab"
                            :names="names"
                            @update="patch => go({ ...state, ...patch, page: 1 })"
                        />
                        <div data-testid="dashboard-sort">
                            <USelectMenu
                                :model-value="state.sort"
                                :options="sortOptions"
                                value-attribute="value"
                                option-attribute="label"
                                icon="i-heroicons-arrows-up-down"
                                size="sm"
                                class="w-52"
                                :aria-label="$t('dashboards.sort.label')"
                                @update:model-value="setSort"
                            >
                                <template #label>
                                    <span class="text-xs whitespace-nowrap truncate">{{ sortLabel }}</span>
                                </template>
                            </USelectMenu>
                        </div>
                        <div class="inline-flex rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden" role="group">
                            <UButton
                                v-for="v in VIEWS"
                                :key="v.value"
                                color="gray"
                                variant="ghost"
                                square
                                :icon="v.icon"
                                class="rounded-none w-8 h-8 justify-center"
                                :class="[
                                    v.value === 'list' ? 'border-s border-gray-200 dark:border-gray-700' : '',
                                    view === v.value ? 'bg-gray-100 dark:bg-gray-800 text-ink' : 'text-ink-muted',
                                ]"
                                :aria-label="$t(v.label)"
                                :title="$t(v.label)"
                                :aria-pressed="view === v.value ? 'true' : 'false'"
                                :data-testid="`dashboard-view-${v.value}`"
                                @click="setView(v.value)"
                            />
                        </div>
                    </div>
                </div>

                <!-- Main tabs (My / Shared), with counts, and the type chips -->
                <div class="border-b border-gray-200 dark:border-gray-700 mb-5">
                    <nav class="-mb-px flex flex-wrap gap-6">
                        <button
                            v-for="tab in TABS"
                            :key="tab.value"
                            type="button"
                            class="whitespace-nowrap border-b-2 py-2 px-1 text-sm flex items-center"
                            :class="state.tab === tab.value
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-ink-muted hover:border-gray-300 dark:hover:border-gray-600 hover:text-ink-2'"
                            :aria-current="state.tab === tab.value ? 'page' : undefined"
                            :data-testid="`dashboard-tab-${tab.value}`"
                            @click="setTab(tab.value)"
                        >
                            <span>{{ $t(tab.label) }}</span>
                            <span v-if="facets" class="ms-1.5 text-2xs text-ink-muted nums" data-testid="dashboard-tab-count">{{ facets.tabs[tab.value] }}</span>
                            <span
                                v-if="tab.value === 'shared' && newCount > 0"
                                class="ms-1.5 px-1.5 rounded-full bg-violet-600 text-white text-2xs font-semibold leading-4 nums"
                                data-testid="dashboard-tab-new"
                            >{{ $t('dashboards.tabNew', { n: newCount }, newCount) }}</span>
                        </button>

                        <!-- Type filter (All / Dashboards / Docs) -->
                        <div class="ms-auto flex items-center gap-1 py-1.5">
                            <button
                                v-for="tf in typeFilters"
                                :key="tf.value"
                                type="button"
                                class="px-2.5 py-1 text-xs rounded-full border transition-colors"
                                :class="state.type === tf.value ? CHIP_ON : CHIP_OFF"
                                :aria-pressed="state.type === tf.value ? 'true' : 'false'"
                                @click="go({ ...state, type: tf.value, page: 1 })"
                            >
                                {{ tf.label }}
                            </button>
                        </div>
                    </nav>
                </div>

                <!-- Shared: how it reached me, with counts -->
                <div v-if="state.tab === 'shared'" class="flex flex-wrap items-center gap-1 -mt-2 mb-5">
                    <button
                        v-for="chip in ACCESS_CHIPS"
                        :key="chip"
                        type="button"
                        class="px-2.5 py-1 text-xs rounded-full border transition-colors"
                        :class="state.access === chip ? CHIP_ON : CHIP_OFF"
                        :aria-pressed="state.access === chip ? 'true' : 'false'"
                        :data-testid="`dashboard-access-chip-${chip}`"
                        @click="go({ ...state, access: chip, page: 1 })"
                    >
                        {{ $t(`dashboards.access.${chip}`) }}
                        <span v-if="facets?.access" class="ms-1 text-2xs text-ink-muted nums">{{ facets.access[chip] }}</span>
                    </button>
                </div>

                <!-- The org admin's line, under the Organization chip -->
                <div
                    v-if="attention"
                    class="-mt-2 mb-5 rounded-lg border border-gray-200 dark:border-gray-700 px-4 py-3 text-xs text-ink-muted flex flex-wrap items-center gap-2"
                    data-testid="dashboard-attention"
                >
                    <span class="text-sm font-medium text-ink">{{ $t('dashboards.attention.title') }}</span>
                    <span aria-hidden="true">·</span>
                    <span class="nums">{{ $t('dashboards.attention.failing', { n: attention.failing }, attention.failing) }}</span>
                    <span aria-hidden="true">·</span>
                    <span class="nums">{{ $t('dashboards.attention.stale', { n: attention.stale }, attention.stale) }}</span>
                    <UButton
                        v-if="attention.total > 0 && !state.attention"
                        color="primary"
                        variant="link"
                        size="xs"
                        :padded="false"
                        class="font-medium"
                        data-testid="dashboard-attention-show"
                        @click="go({ ...state, attention: true, page: 1 })"
                    >{{ $t('dashboards.attention.show') }}</UButton>
                </div>

                <!-- Active filters: "{n} of {total}", one chip each, Clear all -->
                <div
                    v-if="chips.length"
                    class="flex flex-wrap items-center gap-1.5 -mt-2 mb-5 text-xs"
                    data-testid="dashboard-active-filters"
                >
                    <span class="text-ink-muted me-1 nums" data-testid="dashboard-result-count">
                        <template v-if="facets && !isLoading">{{ $t('dashboards.filters.resultCount', { n: pagination.total, total: facets.tabs[state.tab] }) }}</template>
                        <template v-else-if="!isLoading">{{ $t('dashboards.count', { n: pagination.total }, pagination.total) }}</template>
                    </span>
                    <span
                        v-for="chip in chips"
                        :key="chip.id"
                        class="inline-flex items-center gap-1 ps-2.5 pe-1 py-1 rounded-full border border-blue-200 bg-blue-50 text-blue-700 dark:bg-blue-950 dark:border-blue-900 dark:text-blue-300"
                    >
                        <DataSourceIcon
                            v-if="chip.kind === 'agent'"
                            :type="names.agents[chip.value!]?.type"
                            :icon-token="names.agents[chip.value!]?.icon_token"
                            class="h-3"
                        />
                        <span class="truncate max-w-[16rem]">{{ chipLabel(chip) }}</span>
                        <UButton
                            color="primary"
                            variant="link"
                            size="xs"
                            square
                            :padded="false"
                            icon="i-heroicons-x-mark"
                            :aria-label="$t('dashboards.filters.remove', { name: chipLabel(chip) })"
                            @click="go(removeFilterChip(state, chip.id))"
                        />
                    </span>
                    <UButton
                        color="gray"
                        variant="link"
                        size="xs"
                        :padded="false"
                        class="ms-1 font-medium text-ink-muted"
                        data-testid="dashboard-clear-all"
                        @click="clearAll"
                    >{{ $t('dashboards.filters.clearAll') }}</UButton>
                </div>

                <!-- Loading state -->
                <div v-if="isLoading" class="mt-4">
                    <div v-if="view === 'grid'" class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-6">
                        <div
                            v-for="i in 10"
                            :key="i"
                            class="bg-gray-100 dark:bg-gray-800 rounded-xl overflow-hidden"
                        >
                            <div class="aspect-[4/3] bg-gray-200 dark:bg-gray-700 animate-pulse"></div>
                            <div class="p-3 space-y-2">
                                <div class="h-4 bg-gray-200 dark:bg-gray-700 rounded animate-pulse w-3/4"></div>
                                <div class="h-3 bg-gray-200 dark:bg-gray-700 rounded animate-pulse w-1/2"></div>
                            </div>
                        </div>
                    </div>
                    <div v-else class="rounded-xl border border-gray-200 dark:border-gray-800 divide-y divide-gray-100 dark:divide-gray-800">
                        <div v-for="i in 8" :key="i" class="flex items-center gap-3 px-4 py-3">
                            <div class="w-9 h-9 rounded-lg bg-gray-200 dark:bg-gray-700 animate-pulse"></div>
                            <div class="flex-1 space-y-2">
                                <div class="h-3.5 bg-gray-200 dark:bg-gray-700 rounded animate-pulse w-1/3"></div>
                                <div class="h-3 bg-gray-200 dark:bg-gray-700 rounded animate-pulse w-1/4"></div>
                            </div>
                        </div>
                    </div>
                </div>

                <div v-else-if="reports.length > 0">
                    <!-- Grid: one card per dashboard; it opens the dashboard's
                         /d page on both tabs. Whoever may edit it (its owner,
                         its editors -- collab01) does so through the pencil:
                         their own conversation on it, or a new one. -->
                    <div v-if="view === 'grid'" class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-6">
                        <DashboardCard
                            v-for="dashboard in reports"
                            :key="dashboard.id"
                            :dashboard="dashboard"
                            :tab="state.tab"
                            :editing="editingId === dashboard.id"
                            :busy="!!editingId"
                            :star-pending="pendingStars.has(dashboard.id)"
                            @edit="editDashboard(dashboard)"
                            @toggle-star="toggleStar(dashboard)"
                        />
                    </div>

                    <!-- List: the Conversations directory rows -->
                    <div v-else class="rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden">
                        <div class="flex items-center gap-2 px-4 py-2 bg-gray-50 dark:bg-gray-800/40 text-xs text-ink-muted">
                            <span class="nums">{{ $t('dashboards.count', { n: pagination.total }, pagination.total) }}</span>
                            <span aria-hidden="true">·</span>
                            <span>{{ sortLabel }}</span>
                            <template v-if="state.tab === 'shared' && state.access === 'all'">
                                <span aria-hidden="true">·</span>
                                <span>{{ $t('dashboards.list.directFirst') }}</span>
                            </template>
                            <span class="ms-auto hidden sm:block">{{ $t('dashboards.list.dateColumns') }}</span>
                        </div>
                        <ul class="divide-y divide-gray-100 dark:divide-gray-800">
                            <DashboardRow
                                v-for="dashboard in reports"
                                :key="dashboard.id"
                                :dashboard="dashboard"
                                :tab="state.tab"
                                :editing="editingId === dashboard.id"
                                :busy="!!editingId"
                                :star-pending="pendingStars.has(dashboard.id)"
                                @edit="editDashboard(dashboard)"
                                @toggle-star="toggleStar(dashboard)"
                            />
                        </ul>
                    </div>

                    <!-- Pagination -->
                    <div
                        v-if="pagination.total_pages > 1"
                        class="mt-6 flex flex-col md:flex-row gap-3 md:items-center justify-between"
                    >
                        <div class="text-xs text-ink-muted">
                            {{ $t('dashboards.showingRange', {
                                start: ((currentPage - 1) * pagination.limit) + 1,
                                end: Math.min(currentPage * pagination.limit, pagination.total),
                                total: pagination.total,
                            }) }}
                        </div>
                        <div class="flex items-center gap-2" data-testid="dashboard-pagination">
                            <button
                                type="button"
                                @click="changePage(currentPage - 1)"
                                :disabled="currentPage === 1"
                                :aria-label="$t('dashboards.pagePrevious')"
                                :class="[
                                    'px-3 py-1.5 text-xs font-medium rounded-md border transition-colors',
                                    currentPage === 1
                                        ? 'bg-gray-100 dark:bg-gray-800 text-ink-muted cursor-not-allowed border-gray-200 dark:border-gray-700'
                                        : 'bg-white dark:bg-gray-900 text-ink-2 hover:bg-gray-50 dark:hover:bg-gray-800 border-gray-300 dark:border-gray-600'
                                ]"
                            >
                                <Icon name="heroicons:chevron-left" class="w-4 h-4" />
                            </button>
                            <button
                                v-for="page in visiblePages"
                                :key="page"
                                type="button"
                                @click="changePage(page)"
                                :aria-current="page === currentPage ? 'page' : undefined"
                                :class="[
                                    'px-3 py-1.5 text-xs font-medium rounded-md border transition-colors min-w-[36px]',
                                    page === currentPage
                                        ? 'bg-blue-600 text-white border-blue-500'
                                        : 'bg-white dark:bg-gray-900 text-ink-2 hover:bg-gray-50 dark:hover:bg-gray-800 border-gray-300 dark:border-gray-600'
                                ]"
                            >
                                {{ page }}
                            </button>
                            <button
                                type="button"
                                @click="changePage(currentPage + 1)"
                                :disabled="currentPage === pagination.total_pages"
                                :aria-label="$t('dashboards.pageNext')"
                                :class="[
                                    'px-3 py-1.5 text-xs font-medium rounded-md border transition-colors',
                                    currentPage === pagination.total_pages
                                        ? 'bg-gray-100 dark:bg-gray-800 text-ink-muted cursor-not-allowed border-gray-200 dark:border-gray-700'
                                        : 'bg-white dark:bg-gray-900 text-ink-2 hover:bg-gray-50 dark:hover:bg-gray-800 border-gray-300 dark:border-gray-600'
                                ]"
                            >
                                <Icon name="heroicons:chevron-right" class="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Empty state (also when the list failed to load) -->
                <div v-else class="mt-12 flex flex-col items-center text-center" data-testid="dashboard-empty">
                    <Icon
                        name="heroicons:chart-bar-square"
                        class="mx-auto h-12 w-12 text-ink-muted"
                    />
                    <h3 class="mt-2 text-sm font-medium text-ink">
                        {{ $t('dashboards.empty') }}
                    </h3>
                    <p class="mt-1 text-sm text-ink-muted">
                        <i18n-t v-if="hasFilters" keypath="dashboards.emptyFiltered" tag="span">
                            <template #clear>
                                <UButton
                                    color="primary"
                                    variant="link"
                                    :padded="false"
                                    class="text-sm"
                                    data-testid="dashboard-empty-clear"
                                    @click="clearAll"
                                >{{ $t('dashboards.clearTheFilters') }}</UButton>
                            </template>
                        </i18n-t>
                        <template v-else>{{ $t('dashboards.emptyDescription') }}</template>
                    </p>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import DashboardCard from '~/components/dashboards/DashboardCard.vue'
import DashboardFilterPopover from '~/components/dashboards/DashboardFilterPopover.vue'
import DashboardRow from '~/components/dashboards/DashboardRow.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import {
    ACCESS_CHIPS,
    SORTS,
    activeFilterChips,
    clampPage,
    clearAllFilters,
    isNormalQuery,
    mergeStarState,
    parseListQuery,
    removeFilterChip,
    searchHistoryMethod,
    switchTab,
    toApiParams,
    toFacetParams,
    toRouteQuery,
    usesZonedDates,
    withListQuery,
    type DashboardFacets,
    type DashboardListState,
    type DashboardSort,
    type DashboardTab,
    type DashboardType,
    type FacetNames,
    type FilterChip,
    isolate,
} from '~/utils/dashboardList'

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()
const route = useRoute()
const router = useRouter()
const { timeZone } = useFormatDate()
const { isExcel } = useExcel()

definePageMeta({ auth: true })

// --- the state lives in the URL -----------------------------------------------------

const state = computed(() => parseListQuery(route.query))
const currentPage = computed(() => state.value.page)
const pagePath = route.path
// After unmount, a late failure shows no toast on the page the reader went to.
let unmounted = false

// --- search: 300 ms debounce; one history entry per typing burst ---------------------

const searchInput = ref(state.value.q)
// The time of the burst's last change; null after a blur or any other change of the URL.
let searchBurstAt: number | null = null
const endSearchBurst = () => { searchBurstAt = null }
// The q this page just wrote: the URL watcher leaves the field alone for it.
let ownSearch: string | null = null
let searchTimer: ReturnType<typeof setTimeout> | null = null
const cancelSearch = () => {
    if (searchTimer) clearTimeout(searchTimer)
    searchTimer = null
}

// A user's change is a new history entry; `replace` only inside a typing
// burst of the search (Durable decisions, "History"). Nothing once the
// router has left this page (a late debounce must not drag the reader back).
// A search still waiting for its debounce goes along with another control's
// change, in the same entry, unless that change sets the search itself.
const go = (next: DashboardListState, method: 'push' | 'replace' = 'push') => {
    if (router.currentRoute.value.path !== pagePath) return
    if (searchTimer && next.q === state.value.q) {
        cancelSearch()
        const q = searchInput.value.trim()
        if (q !== next.q) {
            next = { ...next, q, page: 1 }
            ownSearch = q
        }
    }
    router[method]({ query: withListQuery(route.query, next) as Record<string, string> })
}

watch(searchInput, (value) => {
    cancelSearch()
    searchTimer = setTimeout(() => {
        searchTimer = null
        const q = (value ?? '').trim()
        if (q === state.value.q) return
        const now = Date.now()
        const method = searchHistoryMethod(searchBurstAt, now)
        searchBurstAt = now
        ownSearch = q
        go({ ...state.value, q, page: 1 }, method)
    }, 300)
})
onBeforeRouteLeave(() => { cancelSearch() })

const TABS: Array<{ value: DashboardTab; label: string }> = [
    { value: 'my', label: 'dashboards.myDashboards' },
    { value: 'shared', label: 'dashboards.sharedWithMe' },
]
const setTab = (tab: DashboardTab) => {
    if (state.value.tab !== tab) go(switchTab(state.value, tab))
}

const typeFilters = computed(() => [
    { value: 'all' as DashboardType, label: t('dashboards.typeAll') },
    { value: 'page' as DashboardType, label: t('dashboards.typeDashboards') },
    { value: 'doc' as DashboardType, label: t('dashboards.typeDocs') },
])
const CHIP_ON = 'border-blue-200 bg-blue-50 text-blue-700 dark:bg-blue-950 dark:border-blue-900 dark:text-blue-300'
const CHIP_OFF = 'border-gray-200 dark:border-gray-700 text-ink-muted hover:bg-gray-50 dark:hover:bg-gray-800'

// "Recently published" is a Shared sort.
const SORT_LABEL: Record<DashboardSort, string> = {
    edited: 'dashboards.sort.edited',
    created: 'dashboards.sort.newest',
    '-created': 'dashboards.sort.oldest',
    title: 'dashboards.sort.title',
    published: 'dashboards.sort.published',
}
const sortOptions = computed(() => SORTS
    .filter(s => s !== 'published' || state.value.tab === 'shared')
    .map(value => ({ value, label: t(SORT_LABEL[value]) })))
const sortLabel = computed(() => t(SORT_LABEL[state.value.sort]))
const setSort = (sort: DashboardSort) => {
    if (sort !== state.value.sort) go({ ...state.value, sort, page: 1 })
}

const changePage = (page: number) => {
    if (page === currentPage.value || page < 1 || page > pagination.value.total_pages) return
    go({ ...state.value, page })
}

// "Clear all" clears the search too, including one still being typed.
const clearAll = () => {
    cancelSearch()
    searchInput.value = ''
    go(clearAllFilters(state.value))
}

// --- grid or list, remembered on this browser -----------------------------------------

const VIEW_KEY = 'bow.dashboards.view'
const VIEWS = [
    { value: 'grid' as const, icon: 'i-heroicons-squares-2x2', label: 'dashboards.view.grid' },
    { value: 'list' as const, icon: 'i-heroicons-list-bullet', label: 'dashboards.view.list' },
]
const readView = (): 'grid' | 'list' => {
    try {
        return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'grid'
    } catch {
        return 'grid' // storage blocked: the default
    }
}
const view = ref<'grid' | 'list'>(readView())
const setView = (next: 'grid' | 'list') => {
    if (view.value === next) return
    view.value = next
    try {
        localStorage.setItem(VIEW_KEY, next)
    } catch {
        // storage blocked: the choice lasts for this page only
    }
    // The page size differs (15 vs 50): back to the first page.
    if (state.value.page !== 1) go({ ...state.value, page: 1 })
    else load()
}

// --- the list ---------------------------------------------------------------------------

const reports = ref<any[]>([])
const isLoading = ref(true)
const pagination = ref({
    total: 0,
    page: 1,
    limit: 15,
    total_pages: 0,
    has_next: false,
    has_prev: false,
})

const visiblePages = computed(() => {
    const total = pagination.value.total_pages
    const current = currentPage.value
    const siblingCount = 1

    if (total <= 5) {
        return Array.from({ length: total }, (_, i) => i + 1)
    }

    const leftSibling = Math.max(current - siblingCount, 1)
    const rightSibling = Math.min(current + siblingCount, total)

    const shouldShowLeftDots = leftSibling > 2
    const shouldShowRightDots = rightSibling < total - 1

    if (!shouldShowLeftDots && shouldShowRightDots) {
        return Array.from({ length: 5 }, (_, i) => i + 1)
    }

    if (shouldShowLeftDots && !shouldShowRightDots) {
        return Array.from({ length: 5 }, (_, i) => total - 4 + i)
    }

    if (shouldShowLeftDots && shouldShowRightDots) {
        return Array.from({ length: rightSibling - leftSibling + 1 }, (_, i) => leftSibling + i)
    }

    return Array.from({ length: total }, (_, i) => i + 1)
})

// --- "New": the Shared tab's watermark, moved once per page instance ---------------------

// previous_seen_at of this visit: New stays on the cards it marked while the
// reader pages and filters.
const newSince = ref<string | null>(null)
let seen: Promise<void> | null = null
const markSharedSeen = () => {
    seen ??= (async () => {
        try {
            const { data, error } = await useMyFetch('/artifacts/shared/seen', { method: 'POST' })
            if (!error.value) newSince.value = (data.value as any)?.previous_seen_at ?? null
        } catch {
            // Silent: New is measured from the stored watermark instead.
        }
    })()
    return seen
}

// --- facets: counts, or none when they fail ------------------------------------------------

const facets = ref<DashboardFacets | null>(null)
const names = reactive<FacetNames>({ agents: {}, owners: {} })
let facetSeq = 0
// The facet query last asked for: paging, the sort and the view leave it as
// it is, so they do not refetch the counts. Taken at a fixed instant, so a
// rolling preset ("Last 7 days") makes the same key each time; the zone is
// in it for "This year" and custom dates. Cleared on a failure: the next
// load asks again.
let facetKey: string | null = null
const facetQueryKey = () => JSON.stringify([
    toFacetParams(state.value, new Date(0), { newSince: newSince.value, timeZone: timeZone.value }),
    timeZone.value ?? null,
])
const fetchFacets = async () => {
    const seq = ++facetSeq
    try {
        const { data, error } = await useMyFetch('/artifacts/facets', {
            method: 'GET',
            query: toFacetParams(state.value, new Date(), { newSince: newSince.value, timeZone: timeZone.value }),
        })
        if (seq !== facetSeq) return
        if (error.value) {
            console.warn('Could not fetch dashboard facets:', error.value)
            facetKey = null
        }
        const body = error.value ? null : (data.value as DashboardFacets | null)
        facets.value = body
        for (const agent of body?.agents ?? []) names.agents[agent.id] = agent
        for (const owner of body?.owners ?? []) if (owner.name) names.owners[owner.id] = owner.name
    } catch (error) {
        if (seq !== facetSeq) return
        console.warn('Could not fetch dashboard facets:', error)
        facets.value = null
        facetKey = null
    }
}
const newCount = computed(() => facets.value?.new_count ?? 0)
const attention = computed(() => (state.value.tab === 'shared' && state.value.access === 'org' ? facets.value?.attention ?? null : null))

// Dashboards whose star request is in flight: a second click on one waits.
const pendingStars = reactive(new Set<string>())

// Only the newest fetch writes the list and clears the skeleton: an older
// response landing late (a page or tab the reader has left) is dropped.
let fetchSeq = 0
// A quiet refetch must not join a GET already in flight (useMyFetch shares
// identical in-flight GETs): that one may have left before the star landed.
let freshSeq = 0

// quiet: refresh the cards in place, without the loading skeleton or a toast (after a star).
const fetchDashboards = async (quiet = false) => {
    const seq = ++fetchSeq
    if (!quiet) isLoading.value = true
    // Past the last page: the URL is replaced, and the skeleton stays until that load.
    let clamping = false
    try {
        const response = await useMyFetch('/artifacts', {
            method: 'GET',
            query: {
                ...toApiParams(state.value, new Date(), { view: view.value, newSince: newSince.value, timeZone: timeZone.value }),
                // Ignored by the API; only makes this GET its own call.
                _fresh: quiet ? `${Date.now()}-${++freshSeq}` : undefined,
            },
        })
        if (seq !== fetchSeq) return

        if (response.status.value === 'success' && response.data.value) {
            const body = response.data.value as any
            const last = clampPage(state.value, body.meta, body.dashboards.length)
            if (last !== null && router.currentRoute.value.path === pagePath) {
                clamping = true
                go({ ...state.value, page: last }, 'replace')
                return
            }
            // A star still in flight keeps its local state: this response may predate it.
            reports.value = mergeStarState(body.dashboards, pendingStars, reports.value)
            pagination.value = body.meta
        } else {
            throw response.error.value ?? new Error('Could not fetch dashboards')
        }
    } catch (error) {
        if (seq !== fetchSeq) return
        console.error('Error fetching dashboards:', error)
        if (!quiet) {
            // The empty state, with "clear the filters" when any is set.
            reports.value = []
            pagination.value = { ...pagination.value, total: 0, total_pages: 0 }
            if (!unmounted) toast.add({
                title: t('dashboards.fetchFailed'),
                description: getErrorMessage(error, t('dashboards.fetchFailed')),
                color: 'red',
            })
        }
    } finally {
        if (seq === fetchSeq && !clamping) isLoading.value = false
    }
}

// The list and its facets for the URL's state. On Shared, the watermark
// moves first (once), so this visit's New is measured from the previous one.
let loadSeq = 0
const load = async () => {
    const seq = ++loadSeq
    isLoading.value = true
    if (state.value.tab === 'shared') await markSharedSeen()
    if (seq !== loadSeq) return
    const key = facetQueryKey()
    if (key !== facetKey) {
        facetKey = key
        fetchFacets()
    }
    await fetchDashboards()
}

// Back/Forward (and every change the page pushes) lands here; not a
// navigation away from the page, whose query is not this page's state.
// The search field follows the URL only when the URL's q changed: a word
// still being typed survives a change of another filter.
let urlQ = state.value.q
watch(() => JSON.stringify(toRouteQuery(state.value)), () => {
    if (route.path !== pagePath) return
    const qChanged = state.value.q !== urlQ
    urlQ = state.value.q
    if (ownSearch !== null && ownSearch === state.value.q) {
        ownSearch = null
    } else {
        ownSearch = null
        searchBurstAt = null
        if (qChanged) {
            cancelSearch()
            if (searchInput.value.trim() !== state.value.q) searchInput.value = state.value.q
        }
    }
    load()
})

// The org time zone resolves after the first fetch on a cold load (a reload,
// a bookmark): "This year" and custom dates were sent in the browser's zone,
// so they are fetched again in the org's. Rolling presets do not depend on it.
watch(timeZone, () => {
    if (route.path === pagePath && usesZonedDates(state.value)) load()
})

// --- active filters and the empty state ----------------------------------------------------

const chips = computed(() => activeFilterChips(state.value))
const hasFilters = computed(() => chips.value.length > 0
    || state.value.type !== 'all'
    || (state.value.tab === 'shared' && state.value.access !== 'all'))

const PRESET_LABEL: Record<string, string> = {
    '7': 'dashboards.presets.last7',
    '30': 'dashboards.presets.last30',
    '90': 'dashboards.presets.last90',
    year: 'dashboards.presets.year',
    older90: 'dashboards.presets.older90',
}
const rangeLabel = (key: 'created' | 'edited') => {
    const s = state.value
    if (s[key] !== 'custom') return t(PRESET_LABEL[s[key]] ?? 'dashboards.presets.any')
    return t('dashboards.filters.customRange', { from: s[`${key}From`] || '…', to: s[`${key}To`] || '…' })
}
const chipLabel = (chip: FilterChip): string => {
    switch (chip.kind) {
        // User text inside a translated chip is bidi-isolated (see isolate()).
        case 'q': return t('dashboards.chips.search', { q: isolate(chip.value) })
        case 'agent': return isolate(names.agents[chip.value!]?.name) || t('dashboards.filters.unknownAgent')
        case 'owner': return t('dashboards.chips.owner', { name: isolate(names.owners[chip.value!]) || t('dashboards.filters.unknownOwner') })
        case 'created': return t('dashboards.chips.created', { range: rangeLabel('created') })
        case 'edited': return t('dashboards.chips.edited', { range: rangeLabel('edited') })
        case 'status': return t(`dashboards.status.${chip.value}`)
        case 'starred': return t('dashboards.chips.starred')
        case 'attention': return t('dashboards.attention.title')
    }
    return ''
}

// --- stars and the pencil ---------------------------------------------------------------------

// Every card on the page with this id (looked up again: a refetch may have
// replaced the object that was clicked).
const setStar = (id: string, starred: boolean) => {
    for (const card of reports.value) if (card.id === id) card.is_starred = starred
}

// The star: shown at once, then -- once no star is in flight -- the page is
// fetched again (starred cards come first, so the order may change). On
// failure the star goes back.
let starsChanged = false
const toggleStar = async (d: any) => {
    const id: string = d.id
    if (pendingStars.has(id)) return
    pendingStars.add(id)
    const next = !d.is_starred
    setStar(id, next)
    try {
        const { error } = await useMyFetch(`/artifacts/${encodeURIComponent(id)}/star`, {
            method: next ? 'POST' : 'DELETE',
        })
        if (error.value) throw error.value
        starsChanged = true
    } catch (error) {
        setStar(id, !next)
        toast.add({ title: t('dashboards.starFailed'), description: getErrorMessage(error), color: 'red' })
    } finally {
        pendingStars.delete(id)
    }
    if (pendingStars.size || !starsChanged) return
    starsChanged = false
    // A load in flight already reads the stars from the server (and
    // mergeStarState keeps any still pending); a quiet refetch now would
    // supersede it and drop its response.
    if (isLoading.value) return
    await fetchDashboards(true)
}

// The pencil: the reader's own conversation on the dashboard when they have
// one; else Edit makes one (a copy of the newest draft, run again with their
// credentials -- or, for an owner whose conversation was deleted, a new one
// from the published version, as on /d).
const editingId = ref<string | null>(null)
const editDashboard = async (d: any) => {
    if (editingId.value) return
    if (d.report_id) return navigateTo(`/reports/${d.report_id}`)
    editingId.value = d.id
    try {
        const { data, error } = await useMyFetch(`/artifacts/${encodeURIComponent(d.id)}/edit`, { method: 'POST' })
        const reportId = (data.value as any)?.report_id
        if (error.value || !reportId) {
            toast.add({ title: t('dashboardCollab.list.editFailed'), description: getErrorMessage(error.value), color: 'red' })
            return
        }
        await navigateTo(`/reports/${reportId}`)
    } catch (error) {
        toast.add({ title: t('dashboardCollab.list.editFailed'), description: getErrorMessage(error), color: 'red' })
    } finally {
        editingId.value = null
    }
}

onMounted(async () => {
    await nextTick()
    // A URL the page did not write (an unknown or invalid value): rewritten
    // once, in place, so Back never returns to it. Its state is the same.
    if (!isNormalQuery(route.query)) go(state.value, 'replace')
    load()
})

onBeforeUnmount(() => {
    unmounted = true
    cancelSearch()
})
</script>
