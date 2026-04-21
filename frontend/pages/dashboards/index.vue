<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm">
        <div class="w-full max-w-7xl px-4 ps-0 py-2">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
                    Dashboards
                </h1>
                <p class="mt-2 text-gray-500">Browse and manage your dashboards and slides</p>
            </div>

            <div class="mt-6">
                <!-- Header with search -->
                <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
                    <div class="flex-1 max-w-md w-full">
                        <div class="relative">
                            <input
                                v-model="searchTerm"
                                type="text"
                                placeholder="Search dashboards..."
                                class="w-full ps-10 pe-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                            />
                            <UIcon
                                name="i-heroicons-magnifying-glass"
                                class="absolute start-3 top-2.5 h-4 w-4 text-gray-400"
                            />
                        </div>
                    </div>
                </div>

                <!-- Main tabs (My / Shared) -->
                <div class="border-b border-gray-200 mb-5">
                    <nav class="-mb-px flex space-x-6">
                        <button
                            class="whitespace-nowrap border-b-2 py-2 px-1 text-sm flex items-center"
                            :class="activeFilter === 'my'
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700'"
                            @click="setActiveFilter('my')"
                        >
                            <span>My dashboards</span>
                        </button>
                        <button
                            class="whitespace-nowrap border-b-2 py-2 px-1 text-sm flex items-center"
                            :class="activeFilter === 'shared'
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700'"
                            @click="setActiveFilter('shared')"
                        >
                            <span>Shared with me</span>
                        </button>
                    </nav>
                </div>

                <!-- Loading state -->
                <div v-if="isLoading" class="mt-4">
                    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-6">
                        <div
                            v-for="i in 10"
                            :key="i"
                            class="bg-gray-100 rounded-xl overflow-hidden"
                        >
                            <div class="aspect-[4/3] bg-gray-200 animate-pulse"></div>
                            <div class="p-3 space-y-2">
                                <div class="h-4 bg-gray-200 rounded animate-pulse w-3/4"></div>
                                <div class="h-3 bg-gray-200 rounded animate-pulse w-1/2"></div>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Cards grid -->
                <div v-else-if="reports.length > 0">
                    <div class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-6">
                        <RecentReportCard
                            v-for="report in reports"
                            :key="report.id"
                            :report="report"
                            :view-mode="activeFilter === 'shared' ? 'org' : 'my'"
                            :is-owner="report.user?.id === (currentUser as any)?.id"
                        />
                    </div>

                    <!-- Pagination -->
                    <div
                        v-if="pagination.total_pages > 1"
                        class="mt-6 flex flex-col md:flex-row gap-3 md:items-center justify-between"
                    >
                        <div class="text-xs text-gray-500">
                            Showing
                            {{ ((currentPage - 1) * pagination.limit) + 1 }}
                            to
                            {{ Math.min(currentPage * pagination.limit, pagination.total) }}
                            of
                            {{ pagination.total }}
                            dashboards
                        </div>
                        <div class="flex items-center gap-2">
                            <button
                                @click="changePage(currentPage - 1)"
                                :disabled="currentPage === 1"
                                :class="[
                                    'px-3 py-1.5 text-xs font-medium rounded-md border transition-colors',
                                    currentPage === 1
                                        ? 'bg-gray-100 text-gray-400 cursor-not-allowed border-gray-200'
                                        : 'bg-white text-gray-700 hover:bg-gray-50 border-gray-300'
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
                                        : 'bg-white text-gray-700 hover:bg-gray-50 border-gray-300'
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
                                        ? 'bg-gray-100 text-gray-400 cursor-not-allowed border-gray-200'
                                        : 'bg-white text-gray-700 hover:bg-gray-50 border-gray-300'
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
                    <h3 class="mt-2 text-sm font-medium text-gray-900">
                        No dashboards found
                    </h3>
                    <p class="mt-1 text-sm text-gray-500">
                        Try adjusting your search term, or create a report with a dashboard.
                    </p>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import RecentReportCard from '~/components/home/RecentReportCard.vue'

const { data: currentUser } = useAuth()
const toast = useToast()

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

const fetchDashboards = async (page: number = 1, filter: 'my' | 'shared' = 'my', search: string = '') => {
    isLoading.value = true
    try {
        const response = await useMyFetch('/reports', {
            method: 'GET',
            query: {
                page,
                limit: pagination.value.limit,
                filter,
                search: search?.trim() || undefined,
                has_artifacts: 'yes',
            },
        })

        if (response.status.value === 'success' && response.data.value) {
            reports.value = response.data.value.reports
            pagination.value = response.data.value.meta
        } else {
            throw new Error('Could not fetch dashboards')
        }
    } catch (error) {
        console.error('Error fetching dashboards:', error)
        toast.add({
            title: 'Error',
            description: 'Failed to fetch dashboards',
            color: 'red',
        })
    } finally {
        isLoading.value = false
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
