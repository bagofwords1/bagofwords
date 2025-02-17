<template>
    <div class="flex flex-col min-h-screen bg-gray-50">
        <!-- Top bar -->
        <div class="flex justify-between items-center p-3">
            <div class="logo">
                <!-- Replace with your actual logo -->
            </div>
            <div class="hamburger">
                <!-- Add hamburger icon (you can replace this with an icon component if you're using one) -->
                <UDropdown :items="menuItems" :popper="{ placement: 'bottom-start' }">
                    <UButton color="white" label="" trailing-icon="i-heroicons-bars-3" />
                </UDropdown>

            </div>
        </div>

        <!-- Existing content -->
        <div class="flex flex-col p-4 flex-grow">
            <h1 class="text-3xl mt-10 font-bold">Hi, {{ currentUser.name }} 👋</h1>
            <p class="text-sm mt-2 text-gray-500">Select or create a report to continue</p>
            <div @click="createNewReport" class="flex cursor-pointer flex-col text-sm w-full text-left mt-4 p-2 bg-white rounded-md border border-gray-200 hover:shadow-md hover:border-blue-300">
                <div class="flex">
                    <div class="w-5/6 pr-4">
                        <p class="text-sm text-gray-600 italic">
                            Create a new report to analyze your data
                        </p>
                    </div>
                    <div class="w-1/6 text-right">
                        <button class="">
                            <UIcon name="i-heroicons-arrow-right" />
                        </button>
                    </div>
                </div>
            </div>
            <div class="flex flex-col w-full text-left mt-4 p-2 bg-white rounded-md border border-gray-200">
                <div class="text-xs font-semibold text-blue-500 mb-2">
                    REPORTS
                </div>
                <div v-for="report in previous_reports.slice(0, 7)" :key="report.id"
                class="flex flex-row justify-between items-left w-full py-2 px-2 text-sm hover:bg-gray-50">
                    <NuxtLink :to="`/excel/reports/${report.id}`"> 
                        <li> {{ report.title }}</li>
                    </NuxtLink>
                </div>
            </div>


            <div @click="router.push('/integrations')" class="flex cursor-pointer flex-col text-sm w-full text-left mt-4 p-2 bg-white rounded-md border border-gray-200 hover:shadow-md hover:border-blue-300">
                <div class="flex">
                    <div class="w-4/5 pr-4">
                        <p class="text-sm text-black">
                            <DataSourceIcon type="netsuite" class="h-5 inline mr-2" />
                            <DataSourceIcon type="salesforce" class="h-5 inline mr-2" />
                            Manage integrations
                        </p>
                        <!-- Existing reports list can go here -->
                    </div>
                    <div class="w-1/5 text-right">
                        <button class="">
                            <UIcon name="i-heroicons-arrow-right" />
                        </button>
                    </div>
                </div>
            </div>


        </div>
    </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router';
import { useExcel } from '~/composables/useExcel';
import { onMounted, nextTick } from 'vue';

const router = useRouter()
const previous_reports = ref([])
const selectedDataSources = ref([])

const { signIn, signOut, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
const { organization, clearOrganization, ensureOrganization } = useOrganization()

definePageMeta({ layout: 'excel', auth: true })

const menuItems = ref([
    [{ label: 'Reports', icon: 'i-heroicons-document-chart-bar', to: '/reports' }],
    [{ label: 'Memory', icon: 'i-heroicons-cube', to: '/memory' }],
    [{ label: 'Integrations', icon: 'i-heroicons-circle-stack', to: '/integrations' }],
    [{ label: currentUser.value?.name, icon: 'i-heroicons-user'},
    { label: organization.value.name, icon: 'i-heroicons-building-office'  }
    ],
    [{ label: 'Logout', icon: 'i-heroicons-arrow-right-on-rectangle', click: 
    () => {
      signOff()
    } }],
])

const { isExcel } = useExcel()

onMounted(async () => {
  ensureOrganization()
  nextTick(async () => {
    await getDataSourceOptions()
    await getReports()
  })
})

const checkExcelStatus = () => {
  console.log('Manually checking Excel status:', isExcel.value)
  // You can add more logic here if needed
}

const getReports = async () => {
    const response = await useMyFetch('/reports', {
        method: 'GET',
    });

    if (!response.code === 200) {
        throw new Error('Could not fetch reports');
    }

    previous_reports.value = await response.data.value;
}

const getDataSourceOptions = async () => {
    const response = await useMyFetch('/data_sources', {
        method: 'GET',
    });

    if (!response.code === 200) {
        throw new Error('Could not fetch data sources');
    }

    selectedDataSources.value = await response.data.value;
}

const createNewReport = async () => {
    const response = await useMyFetch('/reports', {
        method: 'POST',
        body: JSON.stringify({title: 'untitled report',
         files: [],
         data_sources: selectedDataSources.value.map((ds: any) => ds.id)})
    });

    if (!response.code === 200) {
        throw new Error('Report creation failed');
    }

    const data = await response.data.value;
    router.push({
        path: `/excel/reports/${data.id}`
    })
}

async function signOff() {
  await signOut({ 
    callbackUrl: '/' 
  })
}
</script>
