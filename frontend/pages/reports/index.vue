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

            <div class="mt-6">
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
                                <button @click="confirmDelete(report.id)" class="text-red-600 hover:text-red-900 font-medium transition-colors duration-150">
                                    <Icon name="heroicons:archive-box" class="inline-block w-4 h-4 mr-1" /> Archive
                                </button>
                            </td>
                        </tr>
                    </tbody>
                </table>
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
 const reports = ref([]);
 const { isExcel } = useExcel()

async function confirmDelete(reportId: string) {
    if (confirm('Are you sure you want to delete this report?')) {
        await deleteReport(reportId);
        // Refresh the reports list after deletion
        reports.value = reports.value.filter(report => report.id !== reportId);
    }
}

async function deleteReport(reportId: string) {
    const response = await useMyFetch(`/reports/${reportId}`, {
        method: 'DELETE',
    }).then(response => {
        if (response.status.value === 'success') {
            toast.add({
                title: 'Report deleted successfully',
                description: 'Report deleted successfully',
                color: 'green'
            });
        } else {
            toast.add({
                title: 'Failed to delete report',
                description: 'Failed to delete report',
                color: 'red'
            });
        }
    });
}


 onMounted(async () => {
    nextTick(async () => {
     const response = await useMyFetch('/reports', {
         method: 'GET',
     });

     if (!response.code === 200) {
         throw new Error('Could not fetch reports');
     }

     reports.value = await response.data.value;
    })
 });
</script>
