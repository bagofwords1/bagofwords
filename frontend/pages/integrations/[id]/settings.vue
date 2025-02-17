<template>
    <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10">
        <div class="w-full px-4 pl-0 py-4">
            <div v-if="integration">
                <h1 class="text-lg font-semibold text-center">
                    <GoBackChevron v-if="isExcel" />
                    <DataSourceIcon :type="integration.type" class="w-5 inline" />
                    {{ integration.name }}
                </h1>
                <p class="text-sm text-gray-500">
                    <div class="mt-4 text-center">
                        <span class="text-green-500 font-semibold" v-if="testConnectionStatus?.success">
                            <UIcon name="heroicons-check-circle"/>
                        </span>
                        {{ testConnectionStatus.success ? 'Connected' : 'Failed to connect' }}
                    </div>
                </p>

                <div class="flex border-b border-gray-200 mt-7">
                    <button 
                        @click="activeTab = 'main'"
                        class="px-4 py-1 text-sm"
                        :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'main', 'text-gray-500': activeTab !== 'main' }"
                    >
                        Main
                    </button>
                    <button 
                        @click="activeTab = 'schema'"
                        class="px-4 py-1 text-sm"
                        :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'schema', 'text-gray-500': activeTab !== 'schema' }"
                    >
                        Schema
                    </button>
                    <button 
                        @click="activeTab = 'context'"
                        class="px-4 py-1 text-sm"
                        :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'context', 'text-gray-500': activeTab !== 'context' }"
                    >
                        AI Rules
                    </button>
                    <button 
                        @click="activeTab = 'configuration'"
                        class="px-4 py-1 text-sm"
                        :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'configuration', 'text-gray-500': activeTab !== 'configuration' }"
                    >
                        Configuration
                    </button>
                </div>

                <div v-if="activeTab === 'main'" class="mt-4 w-full">
                    <div class="text-sm text-gray-500">
                        <strong>Description</strong>
                        <p class="mt-2 mb-4">
                            <UTextarea v-model="integration.description" rows="14" class="w-full outline-none" />
                        </p>
                    </div>
                    <div class="mt-4 text-gray-500 text-sm">
                        <strong>Conversation Starters</strong>
                    </div>
                    <div>
                        <button @click="updateIntegrationDescription()" class="bg-blue-500 text-white px-4 py-2 text-sm mt-2 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50">
                            Save
                        </button>
                    </div>
                </div>

                <div v-if="activeTab === 'schema'" class="mt-4">
                    <!-- Add your schema tab content here -->
                </div>

                <div v-if="activeTab === 'context'" class="mt-4">

                    <div class="mt-4 flex">
                        <div class="w-2/3 pr-4">
                            <UTextarea v-model="integration.context" rows="23" class="w-full"
                            placeholder="Add context for AI agents about this data source:

** Key tables and fields **
- table_a - represents users
- table_b - represents customers

** Example queries and code **
- SELECT * FROM table_a WHERE date > '2024-01-01'
- SELECT * FROM table_b order by date desc limit 10

** Data formatting requirements **
- Date is in EST time.
- Don't allow more than 5 columns in a query.

** Rules for using the data **
- Prioritize tables A and B.
- Use table C only if table A and B don't have the data you need.
- Always use left joins with table A.
- Always order by date desc.

"
                            />
                            <button @click="updateIntegrationContext()" class="bg-blue-500 text-white px-4 py-2 text-sm mt-2 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50">
                                Save
                            </button>
                        </div>
                        <div class="w-1/3 bg-gray-100 p-4 rounded-lg">
                            <h1 class="text-sm font-semibold">
                                What is AI Rules?
                            </h1>
                            <p class="text-sm text-gray-500 leading-6 mt-4">
                                The AI rules for this data source is a place to add any information that you want to share with the AI agents that are using this data source.
                                
                                <br />
                                <br />
                                <b>Useful information to include:</b>
                                <ul class="list-disc list-inside mt-4">
                                    <li class="mb-3">
                                        Rules for how to use the data: <br />
                                        • Prioritize tables A and B. <br />
                                        • Use table C only if table A and B don't have the data you need. <br />
                                        • Date is EST time. <br />
                                        • Always use left joins with table A. <br />
                                        • Always order by date desc.
                                    </li>
                                    <li class="mb-3">Sample code queries
                                        Users: SELECT * FROM table_a WHERE date > '2024-01-01' <br />
                                        Customers: SELECT * FROM table_b order by date desc limit 10
                                    </li>
                                    <li class="mb-3">Any other information that you want to share with the AI agents</li>
                                </ul>
                            </p>
                        </div>
                    </div>
                </div>

                <div v-if="activeTab === 'configuration'" class="mt-4">
                    <div class="mt-4">
                        <button @click="handleDelete()" class="bg-red-500 text-xs text-white px-4 py-2 rounded-md hover:bg-red-600 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-opacity-50">
                            Delete Data Source
                        </button>
                    </div>
                </div>
            </div>
            <div v-else>
                <p>Loading...</p>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue';
const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth();
const { organization } = useOrganization();
const { isExcel } = useExcel()
definePageMeta({ auth: true });

const route = useRoute();
const ds_id = route.params.id;

const integration = ref(null);

const testConnectionStatus = ref(null);

const activeTab = ref('main');

async function testConnection() {
    const response = await useMyFetch(`/data_sources/${ds_id}/test_connection`, {
        method: 'GET',
    });
    testConnectionStatus.value = response.data.value;
}

async function fetchIntegration() {
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'GET',
    });

    if (response.status.value == "success") {
        integration.value = response.data.value;
    } else {
        console.error('Failed to fetch integration');
    }
}

async function updateIntegrationContext() {
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'PUT',
        body: {
            context: integration.value.context
        }
    });
    if (response.status.value == "success") {
        integration.value = response.data.value;
    } else {
        console.error('Failed to update integration context');
    }
}

function handleDelete() {
    if (confirm('Are you sure you want to delete this integration?')) {
        useMyFetch(`/data_sources/${ds_id}`, {
            method: 'DELETE',
        }).then(() => {
            navigateTo('/integrations');
        });
    }
}

onMounted(() => {
    nextTick(() => {
        fetchIntegration();
        testConnection();
    });
});
</script>