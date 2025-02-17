<template>
    <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/3 md:pt-10">
        <div class="w-full px-4 pl-0 py-4">
            <div>
                <h1 class="text-lg font-semibold text-center">
                    <GoBackChevron v-if="isExcel" />
            Connect Data
          </h1>
          <p class="mt-2 text-gray-500"></p>
        </div>
  
        <div class="mt-6">
          <form @submit.prevent="handleSubmit">
            <div>
              <USelectMenu 
                v-model="selected_ds"
                :options="available_ds"
                option-attribute="title"
                @change="changeSelect"
                class="w-full "
              >


              <template #option="{ option }">
                <DataSourceIcon :type="option?.type" class="w-4" />
                <span>{{ option.title }}</span>
                </template>

                </USelectMenu>
            </div>
            <div class="mt-4">
              <input 
                v-model="name"
                type="text" 
                placeholder="Name" 
                class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500" 
              />
            </div>
  
            <!-- Dynamic Form Fields -->
            <div v-if="fields.config || fields.credentials" class="mt-4">
              <!-- Configuration Fields -->
              <div v-if="fields.config" class="mb-6 bg-gray-50 p-5 rounded border">
                <div class="flex justify-between items-center mb-4">
                  <h3 class="text-sm font-semibold">Configuration</h3>
                  <button @click="showNatGateway = true" class="text-xs hidden text-blue-500 hover:text-blue-600">Network Settings</button>
                </div>
                <UModal v-model="showNatGateway">
                  <div class="p-4 relative">
                    <button @click="showNatGateway = false"
                      class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                      <UIcon name="heroicons:x-mark" class="w-5 h-5" />
                    </button>
                    <h1 class="text-lg font-semibold">Network Settings</h1>
                    <p class="text-sm text-gray-500">Configure network access for your integration</p>
                    <hr class="my-4" />
                    
                    <div>
                      <div class="mt-4">
                        <label class="block text-sm font-medium text-gray-700 mb-2">Incoming connections</label>
                        <p class="text-sm text-gray-600">Make sure to allow incoming connections from the following IP address:</p>
                        <div class="mt-2 p-3 bg-gray-50 rounded-md">
                          <span class="font-mono">{{ nat_gateway_ip }}</span>
                        </div>
                      </div>
                    </div>

                    <div class="border-t border-gray-200 pt-4 mt-8">
                      <div class="flex justify-end">
                        <button 
                          @click="showNatGateway = false"
                          class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
                        >
                          Close
                        </button>
                      </div>
                    </div>
                  </div>
                </UModal>
                <div v-for="field in configFields" :key="field.field_name" class="mb-4">
                  <label :for="field.field_name" class="block text-sm font-medium text-gray-700">
                    {{ field.title || field.field_name }}
                  </label>
                  <input
                    v-if="field.type === 'string'"
                    type="text"
                    v-model="formData.config[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                  <input
                    v-else-if="field.type === 'integer'"
                    type="number"
                    v-model="formData.config[field.field_name]"
                    :id="field.field_name"
                    :min="field.minimum"
                    :max="field.maximum"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                </div>
              </div>

              <!-- Credentials Fields -->
              <div v-if="fields.credentials" class="mb-6 bg-gray-50 p-5 rounded border">
                <h3 class="text-sm font-semibold mb-4">Credentials</h3>
                <div v-for="field in credentialFields" :key="field.field_name" class="mb-4">
                  <label :for="field.field_name" class="block text-sm font-medium text-gray-700">
                    {{ field.title || field.field_name }}
                  </label>
                  <input
                    v-if="field.type === 'string'"
                    :type="isPasswordField(field.field_name) ? 'password' : 'text'"
                    v-model="formData.credentials[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                </div>
              </div>
            </div>
            <div class="mt-4 flex flex-col gap-2 bg-gray-50 p-5 rounded border">
              <h1 class="text-sm font-semibold">LLM settings</h1>
              <UCheckbox v-model="generate_summary" label="Generate data source summary" />
              <UCheckbox v-model="generate_conversation_starters" label="Generate conversation starters" />
              <UCheckbox v-model="generate_ai_rules" label="Generate AI rules" />
            </div>
  
            <div class="mt-4">
              <button
                :disabled="isSubmitting"
                class="bg-blue-500 text-white px-4 py-2 border-2 border-blue-500 rounded-md hover:bg-blue-600 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2">
                <UIcon v-if="isSubmitting" name="heroicons-spinner-20-solid" class="w-4 h-4 animate-spin" />
                {{ isSubmitting ? 'Integrating...' : 'Integrate' }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  </template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue';

const available_ds = ref([]);
const selected_ds = ref('');
const route = useRoute();
const fields = ref({ config: null, credentials: null });
const name = ref('');
const formData = reactive({
  config: {},
  credentials: {}
});
const generate_summary = ref(true);
const generate_conversation_starters = ref(true);
const generate_ai_rules = ref(true);
const isSubmitting = ref(false);
const showNatGateway = ref(false);

const nat_gateway_ip = ref('51.21.11.139');

const toast = useToast();

const { isExcel } = useExcel()

// Computed properties for fields
const configFields = computed(() => {
  if (!fields.value.config?.properties) return [];
  return Object.entries(fields.value.config.properties).map(([field_name, schema]) => ({
    field_name,
    ...schema
  }));
});

const credentialFields = computed(() => {
  if (!fields.value.credentials?.properties) return [];
  return Object.entries(fields.value.credentials.properties).map(([field_name, schema]) => ({
    field_name,
    ...schema
  }));
});

// Helper function to determine if a field should be a password input
function isPasswordField(fieldName: string) {
  return fieldName.toLowerCase().includes('password') || 
         fieldName.toLowerCase().includes('secret') || 
         fieldName.toLowerCase().includes('token');
}

async function getAvailableDataSources() {
    const response = await useMyFetch('/available_data_sources', {
        method: 'GET',
    });

    if (!response.code === 200) {
        throw new Error('Could not fetch reports');
    }
    available_ds.value = await response.data.value;
    setSelectedDataSource();
}

function setSelectedDataSource() {
    const type = route.query.type; // Extract the type from the query string
    selected_ds.value = available_ds.value.find(ds => ds.type === type);

    getFields();
}

async function testConnection(dataSourceId: string) {
  const response = await useMyFetch(`/data_sources/${dataSourceId}/test_connection`, {
    method: 'GET',
  });
  console.log(response);
}

async function getFields() {
  const type = route.query.type;
  try {
    const response = await useMyFetch(`/data_sources/${type}/fields`, {
      method: 'GET',
    });
    
    fields.value = response.data.value;
    initFormData();
  } catch (error) {
    console.error('Error fetching fields:', error);
  }
}

function initFormData() {
  // Initialize config fields
  if (fields.value.config?.properties) {
    Object.keys(fields.value.config.properties).forEach(field_name => {
      const schema = fields.value.config.properties[field_name];
      formData.config[field_name] = schema.default || '';
    });
  }

  // Initialize credentials fields
  if (fields.value.credentials?.properties) {
    Object.keys(fields.value.credentials.properties).forEach(field_name => {
      const schema = fields.value.credentials.properties[field_name];
      formData.credentials[field_name] = schema.default || '';
    });
  }
}

async function handleSubmit() {
  if (isSubmitting.value) return;
  isSubmitting.value = true;

  try {
    // Check required fields
    const missingConfigFields = configFields.value.filter(field => 
      field.required && !formData.config[field.field_name]
    );
    const missingCredentialFields = credentialFields.value.filter(field => 
      field.required && !formData.credentials[field.field_name]
    );

    if (missingConfigFields.length > 0 || missingCredentialFields.length > 0) {
      console.error('Missing required fields:', { config: missingConfigFields, credentials: missingCredentialFields });
      return;
    }

    // Prepare the payload
    const payload = {
      name: name.value,
      type: selected_ds.value.type,
      config: formData.config,
      credentials: formData.credentials,
      generate_summary: generate_summary.value,
      generate_conversation_starters: generate_conversation_starters.value,
      generate_ai_rules: generate_ai_rules.value
    };

    const response = await useMyFetch('/data_sources', {
      method: 'POST',
      body: JSON.stringify(payload),
      headers: {
        'Content-Type': 'application/json'
      }
    });

    if (response.status.value === 'success') {
      navigateTo(`/integrations/${response.data.value.id}`);
    } else {
      toast.add({
        title: 'Failed to create data source',
        description: response.error.value.data.detail,
        icon: 'i-heroicons-x-circle',
        color: 'red'
      });
    }
  } catch (error) {
    toast.add({
      title: 'Error submitting form',
      description: error.message,
      icon: 'i-heroicons-x-circle',
      color: 'red'
    });
    console.error('Error submitting form:', error);
  } finally {
    isSubmitting.value = false;
  }
}

function changeSelect() {
  // Reset fields and formData when a new selection is made
  fields.value = { config: null, credentials: null };
  formData.value = { config: {}, credentials: {} }; // Clear the previous form data

  // Navigate to the new route and fetch the fields after navigation
  navigateTo(`/integrations/new?type=${selected_ds.value.type}`).then(() => {
    getFields(); // Fetch new fields after the route has changed
  });
}

onMounted(async () => {
  getAvailableDataSources();
    nextTick(async () => {
        getAvailableDataSources();
    });
});
</script>