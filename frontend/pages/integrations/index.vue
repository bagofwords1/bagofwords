<template>

    <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/3 md:pt-10">
        <div class="w-full px-4 pl-0 py-4">
            <div>
                <h1 class="text-lg font-semibold text-center">
                    <GoBackChevron v-if="isExcel" />
                    Integrations</h1>
                <p class="mt-4 text-gray-500">Connect and manage your data sources</p>
            </div>

            <div class="mt-6 mb-4" v-if="connected_ds.length > 0">
                <div class="font-medium">
                    Connected Integrations
                </div>
                <NuxtLink :to="`/integrations/${ds.id}`" class="border-b border-gray-100 w-full pt-2.5 pb-2.5 rounded-lg flex hover:bg-gray-50"
                    v-for="ds in connected_ds" :key="ds.type">

                    <div class=" p-2 w-18">
                        <DataSourceIcon class="w-8" :type="ds.type" />
                    </div>
                    <div class="w-full pl-3">
                        <span v-if="ds.is_active" class="float-right">
                            <UTooltip text="Data source is active">
                                <Icon name="heroicons:check-circle" class="w-4 h-4 text-green-500" />
                            </UTooltip>
                            </span>

                            <span v-else class="float-right">
                                <UTooltip text="Data source is inactive">
                                    <Icon name="heroicons:x-circle" class="w-4 h-4 text-red-500" />
                                </UTooltip>
                            </span>
                        {{ ds.name }}
    
                        <p class="text-gray-500 text-xs">

                            <span class="text-gray-500">
                                Created at {{ new Date(ds.created_at).toLocaleString() }}
                            </span>
                        </p>
                    </div>
                </NuxtLink>
            </div>

            <div class="mt-6 mb-4">
                <div class="font-medium">
                    Available Integrations
                </div>
            <NuxtLink
                :to="`/integrations/new?type=${ds.type}`"
                class="border-b border-gray-100 w-full pt-2.5 pb-2.5 rounded-lg flex hover:bg-gray-50"
                v-for="ds in available_ds" :key="ds.type">
                    <div class="p-2 w-18">
                        <DataSourceIcon class="w-8" :type="ds.type" />
                    </div>
                    <div class="w-9/10 pl-3">
                        {{ ds.title }}
                        <p class="text-gray-500 text-xs">{{ ds.description }}</p>
                    </div>
                </NuxtLink>
            </div>

        </div>
    </div>
</template>

<script lang="ts" setup>

import GoBackChevron from '@/components/excel/GoBackChevron.vue';

const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()

const { organization } = useOrganization()
const { isExcel } = useExcel()

definePageMeta({ auth: true })
const available_ds = ref([]);
const connected_ds = ref([]);

async function getConnectedDataSources() {
    const response = await useMyFetch('/data_sources', {
        method: 'GET',
    });

    if (!response.code === 200) {
        throw new Error('Could not fetch reports');
    }

    connected_ds.value = await response.data.value;


}

async function getAvailableDataSources() {
    const response = await useMyFetch('/available_data_sources', {
        method: 'GET',
    });

    if (!response.code === 200) {
        throw new Error('Could not fetch reports');
    }
    available_ds.value = await response.data.value;
}


onMounted(async () => {
    nextTick(async () => {
         getConnectedDataSources()
         getAvailableDataSources()
    })
});



</script>
