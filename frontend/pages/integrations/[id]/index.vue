<template>
    <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-2/3 md:pt-10">
        <div class="w-full px-4 pl-0 py-4">
            <div v-if="integration">
                <h1 class="text-lg font-semibold text-center">
                    <GoBackChevron v-if="isExcel" />
                    <DataSourceIcon :type="integration.type" class="w-5 inline" />
                    {{ integrationName }}
                </h1>
                <p class="text-sm text-gray-500">
                    <div class="mt-4 text-center">
                        <span class="text-green-500 font-semibold" v-if="testConnectionStatus?.success">
                            <UIcon name="heroicons-check-circle"/>
                        </span>
                        <span class="text-red-500 font-semibold" v-else>
                            <UIcon name="heroicons-x-circle"/>
                        </span>
                        {{ testConnectionStatus.success ? 'Connected' : 'Failed to connect' }}
                    </div>
                </p>

                <div class="flex border-b border-gray-200 mt-7">
                    <button 
                        @click="activeTab = 'main'"
                        class="px-4 py-1 text-sm"
                        :class="{ 
                            'border-b-2 border-blue-500 text-blue-600': activeTab === 'main', 
                            'text-gray-500': activeTab !== 'main',
                            'opacity-50 cursor-not-allowed': !testConnectionStatus?.success 
                        }"
                        :disabled="!testConnectionStatus?.success"
                    >
                        Main
                    </button>
                    <button 
                        @click="activeTab = 'schema'"
                        class="px-4 py-1 text-sm"
                        :class="{ 
                            'border-b-2 border-blue-500 text-blue-600': activeTab === 'schema', 
                            'text-gray-500': activeTab !== 'schema',
                            'opacity-50 cursor-not-allowed': !testConnectionStatus?.success 
                        }"
                        :disabled="!testConnectionStatus?.success"
                    >
                        Schema
                    </button>
                    <button 
                        @click="activeTab = 'context'"
                        class="px-4 py-1 text-sm"
                        :class="{ 
                            'border-b-2 border-blue-500 text-blue-600': activeTab === 'context', 
                            'text-gray-500': activeTab !== 'context',
                            'opacity-50 cursor-not-allowed': !testConnectionStatus?.success 
                        }"
                        :disabled="!testConnectionStatus?.success"
                    >
                        AI Context
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
                    <div class="text-sm text-black">
                        <span class="font-medium">Description</span>
                        <button @click="generateItem('description')" class=" float-right mb-2 text-blue-500 bg-white hover:bg-gray-100 border border-gray-300 rounded-md px-2 py-1 text-xs hover:text-blue-600">
                            <UIcon v-if="generatingItem === 'description'" name="heroicons-arrow-path" class="w-4 h-4 animate-spin inline-block" />
                            <span v-else>✨ Generate</span>
                        </button>
                        <p class="mt-2 mb-4">
                            <UTextarea 
                                v-model="integration.description" 
                                rows="8" 
                                class="w-full outline-none whitespace-pre-wrap" 
                            />
                        </p>
                    </div>
                    <div class="mt-4 text-sm">
                        <div class="flex justify-between">
                        <span class="font-medium">Description</span>
                        <button @click="generateItem('conversation_starters')" class=" float-right text-blue-500 bg-white hover:bg-gray-100 border border-gray-300 rounded-md px-2 py-1 text-xs hover:text-blue-600">
                            <UIcon v-if="generatingItem === 'conversation_starters'" name="heroicons-arrow-path" class="w-4 h-4 animate-spin inline-block" />
                            <span v-else>✨ Generate</span>
                        </button>
                    </div>
                        <p class="mt-2 mb-4">
                            <ul>
                                <li v-for="(starter, index) in integration.conversation_starters" :key="index" class="mb-2 flex items-center gap-2">
                                    <UTextarea 
                                        v-model="integration.conversation_starters[index]"
                                        rows="4"
                                        size="sm"
                                        class="w-full leading-4" 
                                        placeholder="Show me list of customers
Create a table of all customers, show customer name, email, phone, address and country. Add aggregated payments and orders data"
                                    />
                                    <button @click="deleteConversationStarter(index)" class="text-red-500 w-7 hover:text-red-600">
                                        <UIcon name="heroicons-trash" class="w-4 h-4" />
                                    </button>
                                </li>
                                <li>
                                    <button @click="addConversationStarter" class="flex items-center text-blue-500 hover:text-blue-600">
                                        <UIcon name="heroicons-plus" class="w-4 h-4 inline mr-1" />
                                        Add conversation starter
                                    </button>
                                </li>
                            </ul>
                        </p>
                    </div>
                    <div>
                        <button @click="updateIntegrationMain()" class="bg-blue-500 text-white px-4 py-2 text-sm mt-2 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50">
                            Save
                        </button>
                    </div>
                </div>

                <div v-if="activeTab === 'schema'" class="mt-4">
                    <div class="font-semibold mb-2">Schema</div>
                    
                    <div v-if="schema" class="mt-2">
                        <ul class="py-2 list-none list-inside">
                            <li class="py-1" v-for="table in schema" :key="table.name">
                                <div @click="toggleTable(table)" class="font-semibold text-gray-500 cursor-pointer">
                                    <UIcon :name="expandedTables[table.name] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="p-1" />
                                    {{ table.name }}
                                </div>
                                <ul v-if="expandedTables[table.name]" class="ml-4 mt-1 text-sm">
                                    <li v-for="column in table.columns" :key="column.name" class="flex py-0.5">
                                        <span class="text-gray-500 mr-2">{{ column.name }}</span>
                                        <span class="text-gray-400">{{ column.dtype }}</span>
                                    </li>
                                </ul>
                            </li>
                        </ul>
                    </div>
                    <div v-else-if="testConnectionStatus?.success" class="flex justify-center items-center p-4">
                        <UIcon name="heroicons-arrow-path" class="animate-spin" />
                    </div>
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
                                What is AI Context?
                            </h1>
                            <p class="text-sm text-gray-500 leading-6 mt-4">
                                The AI context for this data source is a place to add any information that you want to share with the AI agents that are using this data source.
                                
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
                        <div class="font-semibold mb-2">Data Source Settings</div>
                        
                        <form @submit.prevent="handleConfigUpdate" class="mb-8">
                            <div class="mb-4">
                                <input 
                                    v-model="integration.name"
                                    type="text" 
                                    placeholder="Name" 
                                    class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" 
                                />
                            </div>

                            <div v-if="configFields.length > 0" class="mt-4 bg-gray-50 p-5 rounded border">
                                <h3 class="text-sm font-semibold mb-4">Configuration</h3>
                                <div v-for="field in configFields" :key="field.field_name" class="mb-4">
                                    <label :for="field.field_name" class="block text-sm font-medium text-gray-700">
                                        {{ field.title || field.field_name }}
                                    </label>
                                    <input
                                        v-if="field.type === 'string'"
                                        type="text"
                                        v-model="configFormData[field.field_name]"
                                        :id="field.field_name"
                                        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                                        :placeholder="field.title || field.field_name"
                                    />
                                    <input
                                        v-else-if="field.type === 'integer'"
                                        type="number"
                                        v-model="configFormData[field.field_name]"
                                        :id="field.field_name"
                                        :min="field.minimum"
                                        :max="field.maximum"
                                        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                                        :placeholder="field.title || field.field_name"
                                    />
                                    <input
                                        v-else-if="field.type === 'boolean'"
                                        type="checkbox"
                                        v-model="configFormData[field.field_name]"
                                        :id="field.field_name"
                                        class="mt-1 h-4 w-4 text-blue-600 border-gray-300 rounded focus:ring-blue-500"
                                    />
                                </div>
                            </div>

                            <div v-if="credentialFields.length > 0" class="mt-4 bg-gray-50 p-5 rounded border">
                                <h3 class="text-sm font-semibold mb-4">Credentials
                                    <button 
                                        type="button"
                                        @click="editingCredentials = !editingCredentials" 
                                        class="text-blue-500 hover:text-blue-600 float-right"
                                    >
                                        <UIcon v-if="editingCredentials" name="heroicons-x-mark" class="w-4 h-4" />
                                        <UIcon v-else name="heroicons-pencil" class="w-4 h-4" />
                                    </button>
                                </h3>
                                <div v-for="field in credentialFields" :key="field.field_name" class="mb-4">
                                    <label :for="field.field_name" class="block text-sm font-medium text-gray-700">
                                        {{ field.title || field.field_name }}
                                    </label>
                                    <input
                                        v-if="field.type === 'string'"
                                        :type="isPasswordField(field.field_name) ? 'password' : 'text'"
                                        v-model="credentialsFormData[field.field_name]"
                                        :id="field.field_name"
                                        :disabled="!editingCredentials"
                                        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                                        :placeholder="field.title || field.field_name"
                                    />
                                </div>
                            </div>

                            <button 
                                type="submit"
                                class="bg-blue-500 mt-4 text-white px-4 py-2 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50 mr-4"
                            >
                                Save Configuration
                            </button>
                        </form>
                        <div class="mt-4 p-4 bg-gray-50 rounded-lg border border-red-200">
                            <div class="font-semibold text-red-500">Danger Zone</div>
                            <button @click="handleDelete()" class="mt-4 bg-red-500 text-xs text-white px-4 py-2 rounded-md hover:bg-red-600 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-opacity-50">
                                Delete Data Source
                            </button>
                        </div>
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

const toast = useToast();

const route = useRoute();
const ds_id = route.params.id;

const integration = ref(null);

const integrationName = ref('');

const testConnectionStatus = ref(null);
const editingCredentials = ref(false);

const activeTab = ref('main');

const schema = ref(null);

watch(testConnectionStatus, (newStatus) => {
    if (!newStatus?.success) {
        activeTab.value = 'configuration';
    }
}, { immediate: true });

const editingStarterIndex = ref(-1);

const expandedTables = ref<Record<string, boolean>>({});

const configFields = ref([]);
const credentialFields = ref([]);
const configFormData = reactive({});
const credentialsFormData = reactive({});

const generatingItem = ref<string | null>(null);

async function testConnection() {
    const response = await useMyFetch(`/data_sources/${ds_id}/test_connection`, {
        method: 'GET',
    });
    testConnectionStatus.value = response.data.value;
    if (!testConnectionStatus.value?.success) {
        activeTab.value = 'configuration';
        toast.add({
            title: 'Failed to connect',
            description: testConnectionStatus.value.message,
            color: 'red'
        });
    }
    else {
        activeTab.value = 'main';
    }
}


async function fetchSchema() {
    const response = await useMyFetch(`/data_sources/${ds_id}/schema`, {
        method: 'GET',
    });
    schema.value = response.data.value;
}

async function generateItem(item: string) {
    generatingItem.value = item;
    try {
        const response = await useMyFetch(`/data_sources/${ds_id}/generate_items`, {
            method: 'GET',
            params: { item }
        });
        if (response.status.value == "success") {
            integration.value[item] = response.data.value[item];
        } else {
            console.error('Failed to generate item');
        }
    } finally {
        generatingItem.value = null;
    }
}

async function fetchIntegration() {
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'GET',
    });

    if (response.status.value == "success") {
        integration.value = response.data.value;
        integrationName.value = integration.value.name;
    } else {
        console.error('Failed to fetch integration');
    }
}

async function updateIntegrationMain() {
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'PUT',
        body: {
            description: integration.value.description,
            conversation_starters: integration.value.conversation_starters
        }
    });
    if (response.status.value == "success") {
        integration.value = response.data.value;
        toast.add({
            title: 'Integration updated',
            description: 'Integration updated successfully',
            color: 'green'
        });
    } else {
        console.error('Failed to update integration context');
        toast.add({
            title: 'Failed to update integration',
            description: 'Failed to update integration',
            color: 'red'
        });
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
        toast.add({
            title: 'AI Context updated',
            description: 'AI Context updated successfully',
            color: 'green'
        });
    } else {
        console.error('Failed to update integration context');
        toast.add({
            title: 'Failed to update AI Context',
            description: 'Failed to update AI Context',
            color: 'red'
        });
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

function addConversationStarter() {
    if (!integration.value.conversation_starters) {
        integration.value.conversation_starters = [];
    }
    integration.value.conversation_starters.push('');
}

function editConversationStarter(index: number) {
    editingStarterIndex.value = index;
}

function saveConversationStarter(index: number) {
    editingStarterIndex.value = -1;
}

function deleteConversationStarter(index: number) {
    if (confirm('Are you sure you want to delete this conversation starter?')) {
        integration.value.conversation_starters.splice(index, 1);
    }
}

function toggleTable(table: any) {
    expandedTables.value[table.name] = !expandedTables.value[table.name];
}

function isPasswordField(fieldName: string) {
    return fieldName.toLowerCase().includes('password') || 
           fieldName.toLowerCase().includes('secret') || 
           fieldName.toLowerCase().includes('token');
}

async function getConfigFields() {
    const response = await useMyFetch(`/data_sources/${integration.value.type}/fields`, {
        method: 'GET',
    });
    
    const schema = response.data.value;
    
    if (schema.config?.properties) {
        configFields.value = Object.entries(schema.config.properties).map(([field_name, schema]) => ({
            field_name,
            ...schema
        }));
    }

    if (schema.credentials?.properties) {
        credentialFields.value = Object.entries(schema.credentials.properties).map(([field_name, schema]) => ({
            field_name,
            ...schema
        }));
    }

    if (integration.value?.config) {
        const config = typeof integration.value.config === 'string' 
            ? JSON.parse(integration.value.config) 
            : integration.value.config;
            
        Object.keys(config).forEach(key => {
            configFormData[key] = config[key];
        });
    }

    if (integration.value?.credentials) {
        const credentials = typeof integration.value.credentials === 'string'
            ? JSON.parse(integration.value.credentials)
            : integration.value.credentials;
            
        Object.keys(credentials).forEach(key => {
            credentialsFormData[key] = credentials[key];
        });
    }
}

async function handleConfigUpdate() {
    const response = await useMyFetch(`/data_sources/${ds_id}`, {
        method: 'PUT',
        body: {
            name: integration.value.name,
            config: configFormData,
            credentials: credentialsFormData
        }
    });

    if (response.status.value === "success") {
        integration.value = response.data.value;
        toast.add({
            title: `${integration.value.name} configuration updated`,
            description: 'Configuration updated successfully',
            color: 'green'
        })

        await testConnection();
        
    } else {
        toast.add({
            title: 'Failed to update integration',
            description: 'Failed to update integration',
            color: 'red'
        });
    }
}

onMounted(async () => {
    nextTick(async () => {
        await fetchIntegration();
        await testConnection();
        await fetchSchema();
        await getConfigFields();
    });
});
</script>