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
                                class="absolute start-3 top-2.5 h-4 w-4 text-gray-400"
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
                        <!-- One card per dashboard (phase 8): it opens the
                             dashboard's /d page on both tabs. Whoever may edit
                             it (its owner, its editors -- collab01) does so
                             through the pencil: their own conversation on it,
                             or a new one. Shared cards say "Can edit" /
                             "Can view"; on My dashboards every card is theirs. -->
                        <div
                            v-for="dashboard in reports"
                            :key="dashboard.id"
                            class="group relative"
                        >
                            <RecentReportCard
                                :report="asCard(dashboard)"
                                view-mode="org"
                                :to="`/d/${dashboard.slug || dashboard.id}`"
                                :is-owner="false"
                            />
                            <span
                                v-if="roleLabel(dashboard)"
                                class="absolute top-2 start-2 px-2 py-0.5 text-[11px] font-medium rounded-full bg-white/90 dark:bg-gray-900/90 text-gray-700 dark:text-gray-200 shadow-sm pointer-events-none"
                                data-testid="dashboard-card-role"
                            >{{ roleLabel(dashboard) }}</span>
                            <button
                                v-if="canEdit(dashboard)"
                                type="button"
                                class="absolute top-2 end-2 p-1.5 bg-white/90 rounded-full hover:bg-white shadow-sm transition-opacity focus:opacity-100 disabled:cursor-wait"
                                :class="editingId === dashboard.id ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'"
                                :disabled="!!editingId"
                                :aria-label="$t('dashboardCollab.list.edit')"
                                :title="$t('dashboardCollab.list.edit')"
                                data-testid="dashboard-card-edit"
                                @click.prevent.stop="editDashboard(dashboard)"
                            >
                                <Spinner v-if="editingId === dashboard.id" class="w-4 h-4 text-gray-600" />
                                <Icon v-else name="heroicons:pencil-square" class="w-4 h-4 text-gray-600" />
                            </button>
                        </div>
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
                                        ? 'bg-gray-100 dark:bg-gray-800 text-gray-400 cursor-not-allowed border-gray-200 dark:border-gray-700'
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
                                        ? 'bg-blue-500 text-white border-blue-500'
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
                                        ? 'bg-gray-100 dark:bg-gray-800 text-gray-400 cursor-not-allowed border-gray-200 dark:border-gray-700'
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
                        class="mx-auto h-12 w-12 text-gray-400"
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
import RecentReportCard from '~/components/home/RecentReportCard.vue'
import Spinner from '~/components/Spinner.vue'

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

const fetchDashboards = async (page: number = 1, filter: 'my' | 'shared' = 'my', search: string = '') => {
    isLoading.value = true
    try {
        const response = await useMyFetch('/dashboards', {
            method: 'GET',
            query: {
                page,
                limit: pagination.value.limit,
                filter,
                search: search?.trim() || undefined,
                mode: typeFilter.value === 'all' ? undefined : typeFilter.value,
            },
        })

        if (response.status.value === 'success' && response.data.value) {
            reports.value = (response.data.value as any).dashboards
            pagination.value = response.data.value.meta
        } else {
            throw new Error('Could not fetch dashboards')
        }
    } catch (error) {
        console.error('Error fetching dashboards:', error)
        toast.add({
            title: t('common.error'),
            description: t('dashboards.fetchFailed'),
            color: 'red',
        })
    } finally {
        isLoading.value = false
    }
}

// The card reads a conversation's fields: give it the dashboard's. Its id is
// the editing conversation's when there is one, for the activity dot, as
// the conversation's card had.
const asCard = (d: any) => ({
    id: d.report_id || d.id,
    title: d.title,
    slug: d.slug || d.id,
    status: d.published ? 'published' : 'draft',
    user: d.owner || {},
    artifact_modes: [d.mode],
    conversation_share_enabled: false,
    thumbnail_url: d.thumbnail_url,
})

// What this reader may do with it (collab01). Older answers without `role`
// fall back to ownership.
const roleOf = (d: any): 'owner' | 'edit' | 'view' => d.role || (d.is_owner ? 'owner' : 'view')
const canEdit = (d: any) => roleOf(d) === 'owner' || roleOf(d) === 'edit'
// Ownership stays implicit (My dashboards lists only one's own).
const roleLabel = (d: any) => {
    const role = roleOf(d)
    if (role === 'edit') return t('dashboardCollab.list.canEdit')
    if (role === 'view') return t('dashboardCollab.list.canView')
    return ''
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
        const { data, error } = await useMyFetch(`/dashboards/${encodeURIComponent(d.id)}/edit`, { method: 'POST' })
        const reportId = (data.value as any)?.report_id
        if (error.value || !reportId) {
            toast.add({ title: t('dashboardCollab.list.editFailed'), description: getErrorMessage(error.value), color: 'red' })
            return
        }
        await navigateTo(`/reports/${reportId}`)
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
