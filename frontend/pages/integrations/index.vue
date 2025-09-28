<template>

    <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10">
        <div class="w-full px-4 pl-0 py-4">
            <div>
                <h1 class="text-lg font-semibold text-center">
                    <GoBackChevron v-if="isExcel" />
                    Integrations</h1>
                <p class="mt-4 text-gray-500">Connect and manage your data sources</p>
            </div>

            <div class="mt-6 mb-4" v-if="connectedIntegrations.length > 0">
                <div class="font-medium">Connected Integrations</div>
                <NuxtLink :to="`/integrations/${ds.id}`" class="border-b border-gray-100 w-full pt-2.5 pb-2.5 rounded-lg flex hover:bg-gray-50"
                    v-for="ds in connectedIntegrations" :key="ds.id">
                    <div class=" p-2 w-18">
                        <DataSourceIcon class="w-8" :type="ds.type" />
                    </div>
                    <div class="w-full pl-3">
                        <span class="float-right">
                            <UTooltip :text="ds.status === 'active' ? `Connected via ${getAuthTooltipSource(ds)}` : `Disconnected (${getAuthTooltipSource(ds)})`">
                                <span class="text-green-800 bg-green-100 px-1 py-0 rounded-md text-[10px] flex items-center" v-if="ds.status === 'active'">
                                    <UIcon name="heroicons-check" class="mr-1" /> Connected
                                </span>
                                <span class="text-red-800 bg-red-100 px-1 py-0 rounded-md text-[10px] flex items-center" v-else>
                                    <UIcon name="heroicons-x-circle" /> Disconnected
                                </span>
                            </UTooltip>
                        </span>
                        {{ ds.name }}
                        <p class="text-gray-500 text-xs">
                            <span class="text-gray-500">Created at {{ new Date(ds.created_at).toLocaleString() }}</span>
                        </p>
                    </div>
                </NuxtLink>
            </div>

            <div class="mt-6 mb-4" v-if="availableConnections.length > 0">
                <div class="font-medium">Available Connections</div>
                <div class="border-b border-gray-100 w-full pt-2.5 pb-2.5 rounded-lg flex hover:bg-gray-50"
                    v-for="ds in availableConnections" :key="ds.id">
                    <div class=" p-2 w-18">
                        <DataSourceIcon class="w-8" :type="ds.type" />
                    </div>
                    <div class="w-full pl-3">
                        <UButton size="xs" color="blue" variant="solid" class="float-right" @click="openCreds(ds)">Connect</UButton>
                        {{ ds.name }}
                        <p class="text-gray-500 text-xs">
                            <span class="text-gray-500">Created at {{ new Date(ds.created_at).toLocaleString() }}</span>
                        </p>
                    </div>
                </div>
            </div>

            <div class="mt-6 mb-4" v-if="canCreateDataSource()">
                <div class="font-medium">
                    Integrate a data source:
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

    <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="selectedDs" @saved="getConnectedDataSources" />
</template>

<script lang="ts" setup>

import GoBackChevron from '@/components/excel/GoBackChevron.vue';
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'

const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()

const { organization } = useOrganization()
const { isExcel } = useExcel()

definePageMeta({ auth: true })
const available_ds = ref([]);
const connected_ds = ref([]);
const connectedIntegrations = computed(() => (connected_ds.value || []).filter((ds: any) => ds.user_status?.has_user_credentials || ds.auth_policy === 'system_only' || ds.user_status?.effective_auth === 'system'))
const availableConnections = computed(() => (connected_ds.value || []).filter((ds: any) => ds.auth_policy === 'user_required' && !ds.user_status?.has_user_credentials && ds.user_status?.effective_auth !== 'system'))

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

const showCredsModal = ref(false)
const selectedDs = ref<any>(null)
function openCreds(ds: any) {
  selectedDs.value = ds
  showCredsModal.value = true
}

function getAuthTooltipSource(ds: any) {
  if (ds?.user_status?.effective_auth === 'user') return 'user credentials'
  if (ds?.user_status?.effective_auth === 'system' || ds?.auth_policy === 'system_only') return 'system credentials'
  return 'no credentials'
}

function canCreateDataSource() {
  try {
    // prefer composable if available
    return typeof useCan === 'function' ? useCan('create_data_source') : false
  } catch (e) {
    return false
  }
}
onMounted(async () => {
    nextTick(async () => {
         getConnectedDataSources()
         getAvailableDataSources()
    })
});



</script>



