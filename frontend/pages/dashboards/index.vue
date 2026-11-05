<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm">
        <div class="w-full max-w-7xl px-4 ps-0 py-2">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
                    {{ $t('dashboards.title') }}
                </h1>
                <p class="mt-2 text-gray-500 dark:text-gray-400">{{ $t('dashboards.subtitle') }}</p>
            </div>

            <div class="mt-6">
                <!-- Header with search -->
                <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
                    <div class="flex-1 max-w-md w-full">
                        <div class="relative">
                            <input
                                v-model="searchTerm"
                                type="text"
                                :placeholder="$t('dashboards.searchPlaceholder')"
                                class="w-full ps-10 pe-4 py-2 border border-gray-300 dark:border-gray-600 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                            />
                            <UIcon
                                name="i-heroicons-magnifying-glass"
                                class="absolute start-3 top-2.5 h-4 w-4 text-ink-muted"
                            />
                        </div>
                    </div>
                </div>

                <!-- Main tabs (My / Shared) -->
                <div class="border-b border-gray-200 dark:border-gray-700 mb-5">
                    <nav class="-mb-px flex space-x-6">
                        <button
                            class="whitespace-nowrap border-b-2 py-2 px-1 text-sm flex items-center"
                            :class="activeFilter === 'my'
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300'"
                            @click="setActiveFilter('my')"
                        >
                            <span>{{ $t('dashboards.myDashboards') }}</span>
                        </button>
                        <button
                            class="whitespace-nowrap border-b-2 py-2 px-1 text-sm flex items-center"
                            :class="activeFilter === 'shared'
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 dark:text-gray-400 hover:border-gray-300 dark:hover:border-gray-600 hover:text-gray-700 dark:hover:text-gray-300'"
                            @click="setActiveFilter('shared')"
                        >
                            <span>{{ $t('dashboards.sharedWithMe') }}</span>
                        </button>

                        <!-- Type filter (All / Dashboards / Docs) -->
                        <div class="ms-auto flex items-center gap-1 py-1.5">
                            <button
                                v-for="tf in typeFilters"
                                :key="tf.value"
                                class="px-2.5 py-1 text-xs rounded-full border transition-colors"
                                :class="typeFilter === tf.value
                                    ? 'border-blue-200 bg-blue-50 text-blue-700 dark:bg-blue-950 dark:border-blue-900 dark:text-blue-300'
                                    : 'border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800'"
                                @click="setTypeFilter(tf.value)"
                            >
                                {{ tf.label }}
                            </button>
                        </div>
                    </nav>
                </div>

                <!-- Loading state -->
                <div v-if="isLoading" class="mt-4">
                    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-6">
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
                </div>

                <!-- Cards grid -->
                <div v-else-if="reports.length > 0">
                    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-6">
                        <!-- One card per dashboard: it opens the dashboard's
                             /d page on both tabs. Whoever may edit it (its
                             owner, its editors -- collab01) does so through
                             the pencil: their own conversation on it, or a
                             new one. Shared cards say how they reached the
                             reader. -->
                        <DashboardCard
                            v-for="dashboard in reports"
                            :key="dashboard.id"
                            :dashboard="dashboard"
                            :tab="activeFilter"
                            :editing="editingId === dashboard.id"
                            :busy="!!editingId"
                            :star-pending="pendingStars.has(dashboard.id)"
                            @edit="editDashboard(dashboard)"
                            @toggle-star="toggleStar(dashboard)"
                        />
                    </div>

                    <!-- Pagination -->
                    <div
                        v-if="pagination.total_pages > 1"
                        class="mt-6 flex flex-col md:flex-row gap-3 md:items-center justify-between"
                    >
                        <div class="text-xs text-gray-500 dark:text-gray-400">
                            {{ $t('dashboards.showingRange', {
                                start: ((currentPage - 1) * pagination.limit) + 1,
                                end: Math.min(currentPage * pagination.limit, pagination.total),
                                total: pagination.total,
                            }) }}
                        </div>
                        <div class="flex items-center gap-2">
                            <button
                                @click="changePage(currentPage - 1)"
                                :disabled="currentPage === 1"
                                :class="[
                                    'px-3 py-1.5 text-xs font-medium rounded-md border transition-colors',
                                    currentPage === 1
                                        ? 'bg-gray-100 dark:bg-gray-800 text-ink-muted cursor-not-allowed border-gray-200 dark:border-gray-700'
                                        : 'bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 border-gray-300 dark:border-gray-600'
                                ]"
                            >
                                <Icon name="heroicons:chevron-left" class="w-4 h-4" />
                            </button>
                            <button
                                v-for="page in visiblePages"
                                :key="page"
                                @click="changePage(page)"
                                :class="[
                                    'px-3 py-1.5 text-xs font-medium rounded-md border transition-colors min-w-[36px]',
                                    page === currentPage
                                        ? 'bg-blue-600 text-white border-blue-500'
                                        : 'bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 border-gray-300 dark:border-gray-600'
                                ]"
                            >
                                {{ page }}
                            </button>
                            <button
                                @click="changePage(currentPage + 1)"
                                :disabled="currentPage === pagination.total_pages"
                                :class="[
                                    'px-3 py-1.5 text-xs font-medium rounded-md border transition-colors',
                                    currentPage === pagination.total_pages
                                        ? 'bg-gray-100 dark:bg-gray-800 text-ink-muted cursor-not-allowed border-gray-200 dark:border-gray-700'
                                        : 'bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 border-gray-300 dark:border-gray-600'
                                ]"
                            >
                                <Icon name="heroicons:chevron-right" class="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Empty state -->
                <div v-else class="mt-12 flex flex-col items-center text-center">
                    <Icon
                        name="heroicons:chart-bar-square"
                        class="mx-auto h-12 w-12 text-ink-muted"
                    />
                    <h3 class="mt-2 text-sm font-medium text-gray-900 dark:text-white">
                        {{ $t('dashboards.empty') }}
                    </h3>
                    <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
                        {{ $t('dashboards.emptyDescription') }}
                    </p>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import DashboardCard from '~/components/dashboards/DashboardCard.vue'
import { mergeStarState } from '~/utils/dashboardList'

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

definePageMeta({ auth: true })

const reports = ref<any[]>([])
const activeFilter = ref<'my' | 'shared'>('my')
const currentPage = ref(1)
const isLoading = ref(true)
const pagination = ref({
    total: 0,
    page: 1,
    limit: 15,
    total_pages: 0,
    has_next: false,
    has_prev: false,
})
const searchTerm = ref('')
const { isExcel } = useExcel()

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

const changePage = async (page: number) => {
    if (page === currentPage.value || page < 1 || page > pagination.value.total_pages) {
        return
    }
    currentPage.value = page
    await fetchDashboards(page, activeFilter.value, searchTerm.value)
}

const setActiveFilter = async (filter: 'my' | 'shared') => {
    if (activeFilter.value === filter) return
    activeFilter.value = filter
    currentPage.value = 1
    await fetchDashboards(1, filter, searchTerm.value)
}

// Type filter: All / Dashboards / Docs (server-side via artifact_mode)
const typeFilter = ref<'all' | 'page' | 'doc'>('all')
const typeFilters = computed(() => [
    { value: 'all' as const, label: t('dashboards.typeAll') },
    { value: 'page' as const, label: t('dashboards.typeDashboards') },
    { value: 'doc' as const, label: t('dashboards.typeDocs') },
])
const setTypeFilter = async (tf: 'all' | 'page' | 'doc') => {
    if (typeFilter.value === tf) return
    typeFilter.value = tf
    currentPage.value = 1
    await fetchDashboards(1, activeFilter.value, searchTerm.value)
}

// Dashboards whose star request is in flight: a second click on one waits.
const pendingStars = reactive(new Set<string>())

// Only the newest fetch writes the list and clears the skeleton: an older
// response landing late (a page or tab the reader has left) is dropped.
let fetchSeq = 0
// A quiet refetch must not join a GET already in flight (useMyFetch shares
// identical in-flight GETs): that one may have left before the star landed.
let freshSeq = 0

// quiet: refresh the cards in place, without the loading skeleton or a toast (after a star).
const fetchDashboards = async (page: number = 1, filter: 'my' | 'shared' = 'my', search: string = '', quiet = false) => {
    const seq = ++fetchSeq
    if (!quiet) isLoading.value = true
    try {
        const response = await useMyFetch('/artifacts', {
            method: 'GET',
            query: {
                page,
                limit: pagination.value.limit,
                filter,
                search: search?.trim() || undefined,
                mode: typeFilter.value === 'all' ? undefined : typeFilter.value,
                // Ignored by the API; only makes this GET its own call.
                _fresh: quiet ? `${Date.now()}-${++freshSeq}` : undefined,
            },
        })
        if (seq !== fetchSeq) return

        if (response.status.value === 'success' && response.data.value) {
            const body = response.data.value as any
            // A star still in flight keeps its local state: this response may predate it.
            reports.value = mergeStarState(body.dashboards, pendingStars, reports.value)
            pagination.value = body.meta
        } else {
            throw new Error('Could not fetch dashboards')
        }
    } catch (error) {
        if (seq !== fetchSeq) return
        console.error('Error fetching dashboards:', error)
        if (!quiet) {
            toast.add({
                title: t('common.error'),
                description: t('dashboards.fetchFailed'),
                color: 'red',
            })
        }
    } finally {
        if (seq === fetchSeq) isLoading.value = false
    }
}

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
    await fetchDashboards(currentPage.value, activeFilter.value, searchTerm.value, true)
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

let _searchTimer: any = null
watch(searchTerm, () => {
    if (_searchTimer) clearTimeout(_searchTimer)
    _searchTimer = setTimeout(() => {
        currentPage.value = 1
        fetchDashboards(1, activeFilter.value, searchTerm.value)
    }, 300)
})

onMounted(async () => {
    await nextTick()
    await fetchDashboards(1, 'my', '')
})
</script>
