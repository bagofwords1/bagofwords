<template>
    <div class="flex pl-2 md:pl-4 text-sm">
        <div class="w-full md:w-3/4 px-4 pl-0 py-4">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
                    Reports
                </h1>
                <p class="mt-2 text-gray-500">Browse your reports</p>
            </div>

            <!-- Filter buttons -->
            <div class="mt-6 mb-4">
                <div class="flex space-x-1 bg-gray-100 p-1 rounded-lg w-fit">
                    <button 
                        @click="setActiveFilter('my')" 
                        :class="[
                            activeFilter === 'my' 
                                ? 'bg-white text-gray-900 shadow-sm' 
                                : 'text-gray-500 hover:text-gray-900',
                            'px-3 py-2 text-xs font-medium rounded-md transition-all duration-200'
                        ]"
                    >
                        My Reports
                    </button>
                    <button 
                        @click="setActiveFilter('published')" 
                        :class="[
                            activeFilter === 'published' 
                                ? 'bg-white text-gray-900 shadow-sm' 
                                : 'text-gray-500 hover:text-gray-900',
                            'px-3 py-2 text-xs font-medium rounded-md transition-all duration-200'
                        ]"
                    >
                        All Organization Reports
                    </button>
                </div>
            </div>

            <div class="mt-6">
                <!-- Styled table container with shadow, border, and rounded corners -->
                <div class="bg-white shadow-sm border border-gray-200 rounded-lg overflow-hidden">
                    <div class="overflow-x-auto">
                        <table class="min-w-full divide-y divide-gray-200">
                            <thead class="bg-gray-50">
                                <tr>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Title</th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Data Sources</th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Created</th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">User</th>
                                    <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                                </tr>
                            </thead>
                            <tbody class="bg-white divide-y divide-gray-200">
                                <tr v-for="report in reports" :key="report.id" class="hover:bg-gray-50">
                                    <td class="px-6 py-4 whitespace-nowrap">
                                        <NuxtLink :to="`/reports/${report.id}`" class="text-blue-500 hover:underline">
                                            {{ report.title }}
                                        </NuxtLink>

                                        <div v-if="report.external_platform && report.external_platform.platform_type == 'slack'" class="ml-2 h-3 inline mr-2">
                                            <img src="/icons/slack.png" class="h-3 inline mr-2" />
                                        </div>
                                        <div v-if="report.cron_schedule" class="ml-2 h-3 inline mr-2">
                                            <UTooltip text="Running on a schedule">
                                                <Icon name="heroicons:clock" />
                                            </UTooltip>
                                        </div>
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                        <UTooltip :text="data_source.name" v-for="data_source in report.data_sources">
                                            <DataSourceIcon :type="data_source.type" class="h-3 inline mr-2" />
                                        </UTooltip>
                                    </td>
                                    <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                                        <div class="flex items-center">
                                            <span
                                                :class="[
                                                    report.status === 'published' ? 'bg-green-100 text-green-800' : 
                                                    report.status === 'draft' ? 'bg-gray-100 text-gray-800' : 
                                                    'bg-gray-100 text-gray-800',
                                                    'px-2 py-1 text-xs font-medium rounded-full capitalize'
                                                ]"
                                            >
                                                {{ report.status }}
                                            </span>
                                            <a target="_blank" :href="`/r/${report.id}`" v-if="report.status === 'published'" class="text-green-800">
                                                <Icon name="heroicons:arrow-top-right-on-square" class="inline-block w-4 h-4 ml-1" />
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
                                            @click="confirmDelete(report.id)" 
                                            v-if="canDeleteReport(report)"
                                            class="text-red-600 hover:text-red-900 font-medium transition-colors duration-150"
                                        >
                                            <Icon name="heroicons:archive-box" class="inline-block w-4 h-4 mr-1" /> Archive
                                        </button>
                                    </td>
                                </tr>
                            </tbody>
                        </table>
                    </div>

                    <!-- Empty state (if needed) -->
                    <div v-if="reports.length === 0" class="text-center py-12">
                        <Icon name="heroicons:document-text" class="mx-auto h-12 w-12 text-gray-400" />
                        <h3 class="mt-2 text-sm font-medium text-gray-900">No reports found</h3>
                        <p class="mt-1 text-sm text-gray-500">
                            Get started by creating your first report.
                        </p>
                    </div>
                </div>

                <!-- Custom Pagination -->
                <div v-if="pagination.total_pages > 1" class="mt-6 flex justify-center">
                    <nav class="flex items-center gap-1">

                        <!-- Previous Page -->
                        <button 
                            @click="changePage(currentPage - 1)"
                            :disabled="currentPage === 1"
                            :class="[
                                'px-3 py-2 text-sm font-medium rounded-md border transition-colors',
                                currentPage === 1 
                                    ? 'bg-gray-100 text-gray-400 cursor-not-allowed border-gray-200' 
                                    : 'bg-white text-gray-700 hover:bg-gray-50 border-gray-300'
                            ]"
                        >
                            <Icon name="heroicons:chevron-left" class="w-4 h-4" />
                        </button>

                        <!-- Page Numbers -->
                        <button 
                            v-for="page in visiblePages" 
                            :key="page"
                            @click="changePage(page)"
                            :class="[
                                'px-3 py-2 text-sm font-medium rounded-md border transition-colors min-w-[40px]',
                                page === currentPage 
                                    ? 'bg-blue-500 text-white border-blue-500' 
                                    : 'bg-white text-gray-700 hover:bg-gray-50 border-gray-300'
                            ]"
                        >
                            {{ page }}
                        </button>

                        <!-- Next Page -->
                        <button 
                            @click="changePage(currentPage + 1)"
                            :disabled="currentPage === pagination.total_pages"
                            :class="[
                                'px-3 py-2 text-sm font-medium rounded-md border transition-colors',
                                currentPage === pagination.total_pages 
                                    ? 'bg-gray-100 text-gray-400 cursor-not-allowed border-gray-200' 
                                    : 'bg-white text-gray-700 hover:bg-gray-50 border-gray-300'
                            ]"
                        >
                            <Icon name="heroicons:chevron-right" class="w-4 h-4" />
                        </button>
                    </nav>
                </div>

                <!-- Pagination Info -->
                <div v-if="pagination.total > 0" class="mt-4 text-center text-sm text-gray-500">
                    Showing {{ ((currentPage - 1) * pagination.limit) + 1 }} to {{ Math.min(currentPage * pagination.limit, pagination.total) }} of {{ pagination.total }} reports
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue';

const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
const { organization } = useOrganization()
const toast = useToast()

definePageMeta({ auth: true })

// Reactive data
const reports = ref([]);
const activeFilter = ref('my'); // 'my' or 'published'
const currentPage = ref(1);
const pagination = ref({
    total: 0,
    page: 1,
    limit: 10,
    total_pages: 0,
    has_next: false,
    has_prev: false
});
const { isExcel } = useExcel()

// Computed property for visible page numbers
const visiblePages = computed(() => {
    const total = pagination.value.total_pages;
    const current = currentPage.value;
    const siblingCount = 1;
    
    if (total <= 5) {
        // Show all pages if 5 or fewer
        return Array.from({ length: total }, (_, i) => i + 1);
    }
    
    const leftSibling = Math.max(current - siblingCount, 1);
    const rightSibling = Math.min(current + siblingCount, total);
    
    const shouldShowLeftDots = leftSibling > 2;
    const shouldShowRightDots = rightSibling < total - 1;
    
    if (!shouldShowLeftDots && shouldShowRightDots) {
        // Show: 1, 2, 3, 4, 5, ...
        const leftRange = Array.from({ length: 5 }, (_, i) => i + 1);
        return leftRange;
    }
    
    if (shouldShowLeftDots && !shouldShowRightDots) {
        // Show: ..., 6, 7, 8, 9, 10
        const rightRange = Array.from({ length: 5 }, (_, i) => total - 4 + i);
        return rightRange;
    }
    
    if (shouldShowLeftDots && shouldShowRightDots) {
        // Show: ..., 4, 5, 6, ...
        const middleRange = Array.from(
            { length: rightSibling - leftSibling + 1 },
            (_, i) => leftSibling + i
        );
        return middleRange;
    }
    
    return Array.from({ length: total }, (_, i) => i + 1);
});

// Check if current user can delete a report (only owner can delete)
const canDeleteReport = (report) => {
    return currentUser.value && (report.user.id === currentUser.value.id || report.user.email === currentUser.value.email);
};

// Handle page change
const changePage = async (page: number) => {
    if (page === currentPage.value || page < 1 || page > pagination.value.total_pages) {
        return;
    }
    
    console.log('Page change to:', page);
    currentPage.value = page;
    await fetchReports(page, activeFilter.value);
};

// Method to set active filter and reset pagination
const setActiveFilter = async (filter: string) => {
    console.log('Filter changed to:', filter);
    activeFilter.value = filter;
    currentPage.value = 1;
    await fetchReports(1, filter);
};

// Fetch reports with pagination
const fetchReports = async (page: number = 1, filter: string = 'my') => {
    try {
        console.log('Fetching reports with page:', page, 'filter:', filter);
        
        const response = await useMyFetch('/reports', {
            method: 'GET',
            query: {
                page: page,
                limit: 10,
                filter: filter
            }
        });

        if (response.status.value === 'success' && response.data.value) {
            reports.value = response.data.value.reports;
            pagination.value = response.data.value.meta;
            console.log('Fetched reports:', reports.value.length, 'Pagination:', pagination.value);
        } else {
            throw new Error('Could not fetch reports');
        }
    } catch (error) {
        console.error('Error fetching reports:', error);
        toast.add({
            title: 'Error',
            description: 'Failed to fetch reports',
            color: 'red'
        });
    }
};

async function confirmDelete(reportId: string) {
    if (confirm('Are you sure you want to delete this report?')) {
        await deleteReport(reportId);
        // Refresh the current page
        await fetchReports(currentPage.value, activeFilter.value);
    }
}

async function deleteReport(reportId: string) {
    try {
        const response = await useMyFetch(`/reports/${reportId}`, {
            method: 'DELETE',
        });
        
        if (response.status.value === 'success') {
            toast.add({
                title: 'Report archived successfully',
                description: 'Report archived successfully',
                color: 'green'
            });
        } else {
            throw new Error('Failed to delete report');
        }
    } catch (error) {
        toast.add({
            title: 'Failed to archive report',
            description: 'Failed to archive report',
            color: 'red'
        });
    }
}

// Initial load
onMounted(async () => {
    await nextTick();
    await fetchReports(1, 'my');
});
</script>
