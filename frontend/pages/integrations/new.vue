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
                </div>
  
                <div v-for="field in configFields" :key="field.field_name" class="mb-4">
                  <label v-if="field.type !== 'boolean'" :for="field.field_name" class="block text-sm font-medium text-gray-700">
                    {{ field.title || field.field_name }}
                  </label>
                  <p v-if="field.description" class="text-xs text-gray-500 mt-0.5">{{ field.description }}</p>
                  <UCheckbox
                    v-if="field.uiType === 'boolean' || field.type === 'boolean'"
                    v-model="formData.config[field.field_name]"
                    :label="field.title || field.field_name"
                    color="blue"
                  />
                  <textarea
                    v-else-if="field.uiType === 'textarea'"
                    v-model="formData.config[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                    rows="4"
                  />
                  <input
                    v-else-if="field.uiType === 'number' || field.type === 'integer'"
                    type="number"
                    v-model="formData.config[field.field_name]"
                    :id="field.field_name"
                    :min="field.minimum"
                    :max="field.maximum"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                  <input
                    v-else-if="field.uiType === 'password' || field.type === 'password'"
                    type="password"
                    v-model="formData.config[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                  <input
                    v-else
                    type="text"
                    v-model="formData.config[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                </div>
              </div>

              <!-- Credentials Fields -->
              <div v-if="fields.credentials" class="mb-6 bg-gray-50 p-5 rounded border">
                <div class="flex items-center justify-between mb-4">
                  <h3 class="text-sm font-semibold">Credentials</h3>
                  <div v-if="authOptions.length" class="w-48">
                    <USelectMenu
                      v-model="selectedAuth"
                      :options="authOptions"
                      option-attribute="label"
                      value-attribute="value"
                      @change="handleAuthChange"
                      v-if="authOptions.length > 1"
                    >
                    </USelectMenu>
                  </div>
                </div>
                <div v-for="field in credentialFields" :key="field.field_name" class="mb-4">
                  <label :for="field.field_name" class="block text-sm font-medium text-gray-700">
                    {{ field.title || field.field_name }}
                  </label>
                  <p v-if="field.description" class="text-xs text-gray-500 mt-0.5">{{ field.description }}</p>
                  <UCheckbox
                    v-if="field.uiType === 'boolean' || field.type === 'boolean'"
                    v-model="formData.credentials[field.field_name]"
                    :label="field.title || field.field_name"
                    color="blue"
                  />
                  <textarea
                    v-else-if="field.uiType === 'textarea'"
                    v-model="formData.credentials[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                    rows="4"
                  />
                  <input
                    v-else-if="field.uiType === 'number'"
                    type="number"
                    v-model="formData.credentials[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                  <input
                    v-else-if="field.uiType === 'password' || isPasswordField(field.field_name)"
                    type="password"
                    v-model="formData.credentials[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                  <input
                    v-else
                    type="text"
                    v-model="formData.credentials[field.field_name]"
                    :id="field.field_name"
                    class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 sm:text-sm"
                    :placeholder="field.title || field.field_name"
                  />
                </div>
              </div>
            </div>
            <div class="mt-4 flex flex-col gap-2 bg-gray-50 p-5 rounded border">
              <h1 class="text-sm font-semibold">Access Settings</h1>
              <UCheckbox v-model="is_public" label="Make this data source public (accessible to all organization members)" />
              <UCheckbox v-model="require_user_auth" label="Require user auth (users must provide their own credentials)" />
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
const selectedAuth = ref<string | null>(null);
const name = ref('');
const formData = reactive({
  config: {},
  credentials: {}
});
const is_public = ref(true);
const require_user_auth = ref(false);
const generate_summary = ref(true);
const generate_conversation_starters = ref(true);
const generate_ai_rules = ref(true);
const isSubmitting = ref(false);

const toast = useToast();

const { isExcel } = useExcel()

// Computed properties for fields
const configFields = computed(() => {
  if (!fields.value.config?.properties) return [] as Array<any>;
  return Object.entries(fields.value.config.properties).map(([field_name, schema]: any) => ({
    field_name,
    title: schema?.title,
    description: schema?.description,
    type: schema?.type,
    minimum: schema?.minimum,
    maximum: schema?.maximum,
    uiType: schema?.['ui:type'] || null,
  }));
});

const credentialFields = computed(() => {
  // Prefer credentials_by_auth if present and an auth is selected
  const byAuth = (fields.value as any)?.credentials_by_auth;
  const active = byAuth && selectedAuth.value ? byAuth[selectedAuth.value] : null;
  const credsSchema = active || fields.value.credentials;
  if (!credsSchema?.properties) return [] as Array<any>;
  return Object.entries(credsSchema.properties).map(([field_name, schema]: any) => ({
    field_name,
    title: schema?.title,
    description: schema?.description,
    type: schema?.type,
    minimum: schema?.minimum,
    maximum: schema?.maximum,
    uiType: schema?.['ui:type'] || null,
  }));
});

// Map checkbox to backend policy string
const auth_policy = computed(() => (require_user_auth.value ? 'user_required' : 'system_only'));

// Auth options, if backend provides auth metadata
const authOptions = computed(() => {
  const authMeta = (fields.value as any)?.auth;
  if (!authMeta) return [] as Array<{ label: string; value: string }>;
  const options: Array<{ label: string; value: string }> = [];
  const byAuth = authMeta.by_auth || {};
  for (const key of Object.keys(byAuth)) {
    const label = (byAuth[key]?.title as string) || key;
    options.push({ label, value: key });
  }
  return options;
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
    const response = await useMyFetch(`/data_sources/${type}/fields?auth_policy=${auth_policy.value}` as any, {
      method: 'GET',
    });
    
    fields.value = response.data.value;
    // If backend returns auth metadata, set default selected
    const authMeta = (fields.value as any)?.auth;
    if (authMeta && !selectedAuth.value) {
      selectedAuth.value = authMeta.default || null;
    }
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

  // Initialize credentials fields (respect selected auth mode)
  const byAuth = (fields.value as any)?.credentials_by_auth;
  const active = byAuth && selectedAuth.value ? byAuth[selectedAuth.value] : null;
  const credsSchema = active || fields.value.credentials;
  if (credsSchema?.properties) {
    Object.keys(credsSchema.properties).forEach((field_name: string) => {
      const schema: any = credsSchema.properties[field_name];
      if (schema?.type === 'boolean') {
        formData.credentials[field_name] = typeof schema.default === 'boolean' ? schema.default : false;
      } else if (schema?.type === 'integer' || schema?.['ui:type'] === 'number') {
        formData.credentials[field_name] = typeof schema.default === 'number' ? schema.default : undefined;
      } else {
        formData.credentials[field_name] = schema?.default ?? '';
      }
    });
  }
}

function handleAuthChange() {
  // Reset credentials when auth mode changes and re-init fields locally
  formData.credentials = {} as any;
  initFormData();
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
      config: { ...formData.config, auth_type: selectedAuth.value || undefined },
      credentials: formData.credentials,
      is_public: is_public.value,
      auth_policy: auth_policy.value,
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