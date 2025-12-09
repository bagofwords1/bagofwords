<template>
    <div class="flex flex-col min-h-screen">
        <!-- Full page loading spinner -->
        <div v-if="loading" class="flex flex-col items-center justify-center flex-grow py-20">
            <Spinner class="h-4 w-4 text-gray-400" />
            <p class="text-sm text-gray-500 mt-2">Loading...</p>
        </div>

        <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10" v-else>
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
                    <div class="mt-2 mb-3">
                        <input 
                            v-model="searchQuery" 
                            type="text" 
                            placeholder="Search data sources..." 
                            class="w-full px-3 py-1.5 text-sm border border-gray-200 rounded-md focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
                        />
                    </div>

                    <!-- Sample databases chips -->
                    <div v-if="uninstalledDemos.length > 0" class="flex flex-wrap gap-2 mb-4">
                        <button 
                            v-for="demo in uninstalledDemos" 
                            :key="`chip-${demo.id}`"
                            @click="installDemo(demo.id)"
                            :disabled="installingDemo === demo.id"
                            class="inline-flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 rounded-full border border-gray-200 bg-white hover:bg-gray-50 hover:border-gray-300 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                            <Spinner v-if="installingDemo === demo.id" class="h-3" />
                            <DataSourceIcon v-else class="h-4" :type="demo.type" />
                            {{ demo.name }}
                            <span class="text-[9px] font-medium uppercase tracking-wide text-purple-600 bg-purple-100 px-1.5 py-0.5 rounded">sample</span>
                        </button>
                    </div>

                    <!-- Regular data sources -->
                    <NuxtLink
                        :to="`/integrations/new?type=${ds.type}`"
                        class="border-b border-gray-100 w-full pt-2.5 pb-2.5 rounded-lg flex hover:bg-gray-50"
                        v-for="ds in filteredAvailableDs" :key="ds.type">
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
    </div>
</template>

<script lang="ts" setup>

import GoBackChevron from '@/components/excel/GoBackChevron.vue';
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import Spinner from '~/components/Spinner.vue'

const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()

const { organization } = useOrganization()
const { isExcel } = useExcel()

definePageMeta({ auth: true })
const available_ds = ref([]);
const connected_ds = ref([]);
const demo_ds = ref<any[]>([]);
const loadingConnected = ref(true);
const loadingAvailable = ref(true);
const loadingDemos = ref(true);
const installingDemo = ref<string | null>(null);
const searchQuery = ref('');
const loading = computed(() => loadingConnected.value || loadingAvailable.value || loadingDemos.value);
const connectedIntegrations = computed(() => (connected_ds.value || []).filter((ds: any) => ds.user_status?.has_user_credentials || ds.auth_policy === 'system_only' || ds.user_status?.effective_auth === 'system'))
const availableConnections = computed(() => (connected_ds.value || []).filter((ds: any) => ds.auth_policy === 'user_required' && !ds.user_status?.has_user_credentials && ds.user_status?.effective_auth !== 'system'))
const uninstalledDemos = computed(() => (demo_ds.value || []).filter((demo: any) => !demo.installed))
const filteredDemos = computed(() => {
    if (!searchQuery.value.trim()) return uninstalledDemos.value;
    const query = searchQuery.value.toLowerCase();
    return uninstalledDemos.value.filter((demo: any) => 
        demo.name?.toLowerCase().includes(query) || 
        demo.description?.toLowerCase().includes(query) ||
        demo.type?.toLowerCase().includes(query)
    );
})
const filteredAvailableDs = computed(() => {
    if (!searchQuery.value.trim()) return available_ds.value;
    const query = searchQuery.value.toLowerCase();
    return available_ds.value.filter((ds: any) => 
        ds.title?.toLowerCase().includes(query) || 
        ds.description?.toLowerCase().includes(query) ||
        ds.type?.toLowerCase().includes(query)
    );
})

async function getConnectedDataSources() {
    loadingConnected.value = true;
    try {
        const response = await useMyFetch('/data_sources', {
            method: 'GET',
        });

        if (!response.code === 200) {
            throw new Error('Could not fetch reports');
        }

        connected_ds.value = await response.data.value;
    } finally {
        loadingConnected.value = false;
    }
}

async function getAvailableDataSources() {
    loadingAvailable.value = true;
    try {
        const response = await useMyFetch('/available_data_sources', {
            method: 'GET',
        });

        if (!response.code === 200) {
            throw new Error('Could not fetch reports');
        }
        available_ds.value = await response.data.value;
    } finally {
        loadingAvailable.value = false;
    }
}

async function getDemoDataSources() {
    loadingDemos.value = true;
    try {
        const response = await useMyFetch('/data_sources/demos', {
            method: 'GET',
        });

        if (response.data.value) {
            demo_ds.value = response.data.value;
        }
    } finally {
        loadingDemos.value = false;
    }
}

async function installDemo(demoId: string) {
    installingDemo.value = demoId;
    try {
        const response = await useMyFetch(`/data_sources/demos/${demoId}`, {
            method: 'POST',
        });

        const result = response.data.value as any;
        if (result?.success) {
            // Refresh both lists
            await Promise.all([
                getConnectedDataSources(),
                getDemoDataSources(),
            ]);
            
            // Navigate to the new data source
            if (result.data_source_id) {
                navigateTo(`/integrations/${result.data_source_id}`);
            }
        }
    } finally {
        installingDemo.value = null;
    }
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
         getDemoDataSources()
    })
});



</script>



