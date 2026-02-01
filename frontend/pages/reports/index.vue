<template>
    <div class="flex justify-center pl-2 md:pl-4 text-sm">
        <div class="w-full max-w-7xl px-4 pl-0 py-2">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
                    Reports
                </h1>
                <p class="mt-2 text-gray-500">Browse and manage your reports</p>
            </div>

            <div class="mt-6">
                <!-- Header with search (like Instructions) -->
                <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
                    <div class="flex-1 max-w-md w-full">
                        <div class="relative">
                            <input
                                v-model="searchTerm"
                                type="text"
                                placeholder="Search reports..."
                                class="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                            />
                            <UIcon
                                name="i-heroicons-magnifying-glass"
                                class="absolute left-3 top-2.5 h-4 w-4 text-gray-400"
                            />
                        </div>
                    </div>

                    <div class="flex items-center justify-end gap-2 w-full md:w-auto">
                        <UButton
                            color="blue"
                            variant="solid"
                            size="xs"
                            icon="i-heroicons-plus"
                            class="w-full md:w-auto"
                            @click="createNewReport"
                        >
                            New report
                        </UButton>
                    </div>
                </div>

                <!-- Main tabs (My / Organization) -->
                <div class="border-b border-gray-200 mb-3">
                    <nav class="-mb-px flex space-x-6">
                        <button
                            class="whitespace-nowrap border-b-2 py-2 px-1 text-sm flex items-center"
                            :class="activeFilter === 'my'
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700'"
                            @click="setActiveFilter('my')"
                        >
                            <span>My reports</span>
                        </button>
                        <button
                            class="whitespace-nowrap border-b-2 py-2 px-1 text-sm flex items-center"
                            :class="activeFilter === 'published'
                                ? 'border-blue-500 text-blue-600'
                                : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-700'"
                            @click="setActiveFilter('published')"
                        >
                            <span>Organization Reports</span>
                        </button>
                    </nav>
                </div>

                <!-- Sub-filters row (status) -->
                <div v-if="activeFilter === 'my'" class="flex flex-wrap items-center justify-between gap-3 mb-5 text-xs">
                    <div class="flex items-center space-x-2">
                        <span class="text-gray-500">Status</span>
                        <div class="flex items-center space-x-1">
                            <UButton
                                size="xs"
                                :variant="statusFilter === 'all' ? 'soft' : 'ghost'"
                                color="gray"
                                @click="statusFilter = 'all'"
                            >
                                All
                            </UButton>
                            <UButton
                                size="xs"
                                :variant="statusFilter === 'draft' ? 'soft' : 'ghost'"
                                color="gray"
                                @click="statusFilter = 'draft'"
                            >
                                Draft
                            </UButton>
                            <UButton
                                size="xs"
                                :variant="statusFilter === 'published' ? 'soft' : 'ghost'"
                                color="gray"
                                @click="statusFilter = 'published'"
                            >
                                Published
                            </UButton>
                        </div>
                    </div>

                    <!-- Bulk actions dropdown (My reports only) -->
                    <div v-if="activeFilter === 'my'" class="ml-auto">
                        <UDropdown :items="actionsDropdownItems" :popper="{ placement: 'bottom-end' }">
                            <UButton
                                color="white"
                                variant="ghost"
                                size="xs"
                                class="border border-gray-200 text-gray-700"
                                trailing-icon="i-heroicons-chevron-down-20-solid"
                            >
                                Actions
                            </UButton>
                        </UDropdown>
                    </div>
                </div>

                <!-- Table card -->
                <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="min-w-full divide-y divide-gray-200">
                            <thead class="bg-gray-50">
                                <tr>
                                    <th class="px-4 py-3 w-10 text-center">
                                        <input
                                            type="checkbox"
                                            :checked="allVisibleSelected"
                                            @change="toggleAllVisible"
                                        />
                                    </th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Title
                                    </th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Data Sources
                                    </th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Status
                                    </th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Created
                                    </th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        User
                                    </th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                                        Actions
                                    </th>
                                </tr>
                            </thead>
                            <tbody class="bg-white divide-y divide-gray-200">
                                <!-- Loading state -->
                                <tr v-if="isLoading">
                                    <td colspan="7" class="px-6 py-12 text-center">
                                        <div class="flex items-center justify-center text-gray-500">
                                            <Spinner class="w-4 h-4 mr-2" />
                                            <span class="text-sm">Loading...</span>
                                        </div>
                                    </td>
                                </tr>
                                <!-- Data rows -->
                                <template v-else>
                                    <tr
                                        v-for="report in visibleReports"
                                        :key="report.id"
                                        class="hover:bg-gray-50"
                                    >
                                        <td class="px-4 py-4 w-10 text-center">
                                            <input
                                                type="checkbox"
                                                :checked="selectedIds.has(report.id)"
                                                @change="toggleOne(report.id)"
                                            />
                                        </td>
                                        <td class="px-6 py-4 whitespace-nowrap">
                                            <NuxtLink
                                                :to="`/reports/${report.id}`"
                                                class="text-blue-500 hover:underline"
                                            >
                                                {{ report.title }}
                                            </NuxtLink>
                                            <div
                                                v-if="report.external_platform && report.external_platform.platform_type == 'slack'"
                                                class="ml-2 h-3 inline mr-2"
                                            >
                                                <img src="/icons/slack.png" class="h-3 inline mr-2" />
                                            </div>
                                            <div
                                                v-if="report.external_platform && report.external_platform.platform_type == 'teams'"
                                                class="ml-2 h-3 inline mr-2"
                                            >
                                                <img src="/icons/teams.png" class="h-3 inline mr-2" />
                                            </div>
                                            <div
                                                v-if="report.external_platform && report.external_platform.platform_type == 'mcp'"
                                                class="ml-2 h-3 inline mr-2"
                                            >
                                                <UTooltip text="Created via MCP">
                                                    <img src="/icons/mcp.png" class="h-3 inline" />
                                                </UTooltip>
                                            </div>
                                            <div
                                                v-if="report.cron_schedule"
                                                class="ml-2 h-3 inline mr-2"
                                            >
                                                <UTooltip text="Running on a schedule">
                                                    <Icon name="heroicons:clock" />
                                                </UTooltip>
                                            </div>
                                        </td>
                                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                            <UTooltip
                                                v-for="data_source in report.data_sources"
                                                :key="data_source.id || data_source.name"
                                                :text="data_source.name"
                                            >
                                                <DataSourceIcon
                                                    :type="data_source.type"
                                                    class="h-3 inline mr-2"
                                                />
                                            </UTooltip>
                                        </td>
                                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                            <div class="flex items-center">
                                                <span
                                                    :class="[
                                                        report.status === 'published'
                                                            ? 'bg-green-100 text-green-800'
                                                            : report.status === 'draft'
                                                            ? 'bg-gray-100 text-gray-800'
                                                            : 'bg-gray-100 text-gray-800',
                                                        'px-2 py-1 text-xs font-medium rounded-full capitalize'
                                                    ]"
                                                >
                                                    {{ report.status }}
                                                </span>
                                                <a
                                                    v-if="report.status === 'published'"
                                                    :href="`/r/${report.id}`"
                                                    target="_blank"
                                                    class="text-green-800"
                                                >
                                                    <Icon
                                                        name="heroicons:arrow-top-right-on-square"
                                                        class="inline-block w-4 h-4 ml-1"
                                                    />
                                                </a>
                                            </div>
                                        </td>
                                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                            {{ report.created_at.split('T')[0].split('-').reverse().join('/') }}
                                        </td>
                                        <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                            {{ report.user.name }}
                                        </td>
                                        <td class="px-6 py-4 whitespace-nowrap">
                                            <button
                                                v-if="canDeleteReport(report)"
                                                @click="confirmDelete(report.id)"
                                                class="text-red-600 hover:text-red-900 font-medium transition-colors duration-150"
                                            >
                                                <Icon
                                                    name="heroicons:archive-box"
                                                    class="inline-block w-4 h-4 mr-1"
                                                />
                                                Archive
                                            </button>
                                        </td>
                                    </tr>
                                    <tr v-if="visibleReports.length === 0">
                                        <td
                                            colspan="7"
                                            class="px-6 py-12 text-center text-gray-500 text-sm"
                                        >
                                            <div class="flex flex-col items-center">
                                                <Icon
                                                    name="heroicons:document-text"
                                                    class="mx-auto h-12 w-12 text-gray-400"
                                                />
                                                <h3 class="mt-2 text-sm font-medium text-gray-900">
                                                    No reports found
                                                </h3>
                                                <p class="mt-1 text-sm text-gray-500">
                                                    Try adjusting your filters or search term.
                                                </p>
                                            </div>
                                        </td>
                                    </tr>
                                </template>
                            </tbody>
                        </table>
                    </div>

                    <!-- Pagination -->
                    <div
                        v-if="!isLoading && pagination.total_pages > 1"
                        class="px-6 py-3 border-t border-gray-200 flex flex-col md:flex-row gap-3 md:items-center justify-between"
                    >
                        <div class="text-xs text-gray-500">
                            Showing
                            {{ ((currentPage - 1) * pagination.limit) + 1 }}
                            to
                            {{ Math.min(currentPage * pagination.limit, pagination.total) }}
                            of
                            {{ pagination.total }}
                            reports
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
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import Spinner from '@/components/Spinner.vue'

const { data: currentUser } = useAuth()
const toast = useToast()
const router = useRouter()

definePageMeta({ auth: true })

const reports = ref<any[]>([])
const activeFilter = ref<'my' | 'published'>('my')
const currentPage = ref(1)
const isLoading = ref(true)
const pagination = ref({
    total: 0,
    page: 1,
    limit: 10,
    total_pages: 0,
    has_next: false,
    has_prev: false,
})
const searchTerm = ref('')
const selectedIds = ref<Set<string>>(new Set())
const statusFilter = ref<'all' | 'draft' | 'published'>('all')
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

const visibleReports = computed(() => {
    if (statusFilter.value === 'all') return reports.value
    return reports.value.filter(r => r.status === statusFilter.value)
})

const allVisibleSelected = computed(() => {
    return visibleReports.value.length > 0 && visibleReports.value.every(r => selectedIds.value.has(r.id))
})

const canDeleteReport = (report: any) => {
    return currentUser.value && (report.user.id === currentUser.value.id || report.user.email === currentUser.value.email)
}

const changePage = async (page: number) => {
    if (page === currentPage.value || page < 1 || page > pagination.value.total_pages) {
        return
    }
    currentPage.value = page
    await fetchReports(page, activeFilter.value, searchTerm.value)
}

const setActiveFilter = async (filter: 'my' | 'published') => {
    if (activeFilter.value === filter) return
    activeFilter.value = filter
    // Sync status filter with selected tab
    if (filter === 'published') {
        // Organization tab: always published
        statusFilter.value = 'published'
    } else {
        // My reports tab: show all by default
        statusFilter.value = 'all'
    }
    currentPage.value = 1
    await fetchReports(1, filter, searchTerm.value)
}

const fetchReports = async (page: number = 1, filter: 'my' | 'published' = 'my', search: string = '') => {
    isLoading.value = true
    try {
        const response = await useMyFetch('/reports', {
            method: 'GET',
            query: {
                page,
                limit: pagination.value.limit,
                filter,
                search: search?.trim() || undefined,
            },
        })

        if (response.status.value === 'success' && response.data.value) {
            reports.value = response.data.value.reports
            pagination.value = response.data.value.meta
            selectedIds.value = new Set()
        } else {
            throw new Error('Could not fetch reports')
        }
    } catch (error) {
        console.error('Error fetching reports:', error)
        toast.add({
            title: 'Error',
            description: 'Failed to fetch reports',
            color: 'red',
        })
    } finally {
        isLoading.value = false
    }
}

const toggleOne = (id: string) => {
    const s = new Set(selectedIds.value)
    if (s.has(id)) s.delete(id)
    else s.add(id)
    selectedIds.value = s
}

const toggleAllVisible = () => {
    const s = new Set(selectedIds.value)
    const allSelected = visibleReports.value.length > 0 && visibleReports.value.every(r => s.has(r.id))
    if (allSelected) {
        for (const r of visibleReports.value) s.delete(r.id)
    } else {
        for (const r of visibleReports.value) s.add(r.id)
    }
    selectedIds.value = s
}

async function confirmDelete(reportId: string) {
    if (confirm('Are you sure you want to archive this report?')) {
        await deleteReport(reportId)
        await fetchReports(currentPage.value, activeFilter.value, searchTerm.value)
    }
}

async function archiveSelected() {
    if (selectedIds.value.size === 0) return
    const ok = window.confirm(`Archive ${selectedIds.value.size} selected report(s)?`)
    if (!ok) return
    try {
        const response: any = await useMyFetch('/reports/bulk/archive', {
            method: 'POST',
            body: Array.from(selectedIds.value),
        })
        if (response?.error?.value) {
            throw response.error.value
        }
        const archived = (response?.data?.value as any)?.archived ?? selectedIds.value.size
        toast.add({
            title: 'Reports archived',
            description: `Archived ${archived} report(s)`,
            color: 'green',
        })
        await fetchReports(currentPage.value, activeFilter.value, searchTerm.value)
    } catch (error: any) {
        console.error('Error bulk archiving reports', error)
        const message =
            error?.data?.detail ||
            error?.data?.message ||
            error?.message ||
            'Failed to archive selected reports'
        toast.add({
            title: 'Failed to archive selected reports',
            description: String(message),
            color: 'red',
        })
    }
}

async function deleteReport(reportId: string) {
    try {
        const response = await useMyFetch(`/reports/${reportId}`, {
            method: 'DELETE',
        })

        if (response.status.value === 'success') {
            toast.add({
                title: 'Report archived',
                description: 'Report archived successfully',
                color: 'green',
            })
        } else {
            throw new Error('Failed to archive report')
        }
    } catch (error: any) {
        console.error('Error archiving report', error)
        const message =
            error?.data?.detail ||
            error?.data?.message ||
            error?.message ||
            'Failed to archive report'
        toast.add({
            title: 'Failed to archive report',
            description: String(message),
            color: 'red',
        })
    }
}

const actionsDropdownItems = computed(() => {
    return [
        [
            {
                label: 'Archive selected',
                icon: 'i-heroicons-archive-box',
                disabled: selectedIds.value.size === 0,
                click: () => archiveSelected(),
            },
        ],
    ]
})

const createNewReport = async () => {
    try {
        // Match default layout behavior: pre-fetch data sources and attach them
        const dsResponse: any = await useMyFetch('/data_sources', {
            method: 'GET',
        })
        if (dsResponse?.error?.value) {
            throw new Error('Could not fetch data sources')
        }
        const list = ((dsResponse?.data?.value || []) as Array<{ id: string | number }>)

        const response: any = await useMyFetch('/reports', {
            method: 'POST',
            body: JSON.stringify({
                title: 'untitled report',
                files: [],
                data_sources: list.map((ds) => ds.id),
            }),
        })

        if (response?.error?.value) {
            throw new Error('Report creation failed')
        }

        const data: any = response?.data?.value
        if (data?.id) {
            router.push({ path: `/reports/${data.id}` })
        }
    } catch (e: any) {
        console.error('Failed to create report', e)
        const message =
            e?.data?.detail ||
            e?.data?.message ||
            e?.message ||
            'Failed to create report'
        toast.add({
            title: 'Failed to create report',
            description: String(message),
            color: 'red',
        })
    }
}

let _searchTimer: any = null
watch(searchTerm, () => {
    if (_searchTimer) clearTimeout(_searchTimer)
    _searchTimer = setTimeout(() => {
        currentPage.value = 1
        fetchReports(1, activeFilter.value, searchTerm.value)
    }, 300)
})

onMounted(async () => {
    await nextTick()
    await fetchReports(1, 'my', '')
})
</script>
