<template>
  <div>
    <form @submit.prevent="handleSubmit" class="space-y-4">
      <div>
        <label class="block text-xs font-medium text-gray-700 mb-1">Connection Name</label>
        <input v-model="form.name" type="text" :placeholder="props.integrationTitle"
               class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
      </div>

      <div class="rounded-md border border-gray-200 bg-gray-50 p-3 text-xs text-gray-600 space-y-1">
        <div class="font-medium text-gray-700">Setup</div>
        <div>
          Register an OAuth app at the provider and paste the credentials below.
          Each user signs in individually to access their own data — no shared service account.
        </div>
      </div>

      <div v-if="loadingFields" class="text-xs text-gray-400">Loading…</div>
      <template v-else>
        <div v-for="field in credentialFields" :key="field.key" class="flex flex-col">
          <label class="block text-xs font-medium text-gray-700 mb-1">
            {{ field.title }}
            <span v-if="field.required" class="text-red-500">*</span>
          </label>
          <input
            v-if="field.type === 'string' && field.format !== 'textarea'"
            :type="field.format === 'password' ? 'password' : 'text'"
            v-model="form.credentials[field.key]"
            :placeholder="isEditMode && field.format === 'password' ? 'unchanged' : (field.description || '')"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <textarea
            v-else-if="field.type === 'string' && field.format === 'textarea'"
            v-model="form.credentials[field.key]"
            class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <UCheckbox v-else-if="field.type === 'boolean'" v-model="form.credentials[field.key]">{{ field.title }}</UCheckbox>
          <input v-else v-model="form.credentials[field.key]"
                 class="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
          <div v-if="field.description" class="text-[11px] text-gray-400 mt-0.5">{{ field.description }}</div>
        </div>
      </template>

      <div class="rounded-md border border-blue-100 bg-blue-50 p-3 text-xs text-blue-800 flex items-start gap-2">
        <UIcon name="heroicons-information-circle" class="w-4 h-4 mt-0.5" />
        <div>
          After saving, each user attaches this integration to an agent and signs in individually.
          The agent can then call file tools on their behalf.
        </div>
      </div>

      <div v-if="!isEditMode" class="flex items-start gap-2 pt-1">
        <UCheckbox v-model="createAgent" class="mt-0.5" />
        <div class="text-xs">
          <label class="font-medium text-gray-700 cursor-pointer" @click="createAgent = !createAgent">
            Create a public agent with this integration
          </label>
          <div class="text-gray-500">
            We'll create an agent named "{{ agentName || (props.integrationTitle || 'Integration') }}" everyone in your org can use.
            Each user signs in individually before using it.
          </div>
          <input
            v-if="createAgent"
            v-model="agentName"
            type="text"
            :placeholder="props.integrationTitle"
            class="mt-2 w-full border border-gray-300 rounded-md px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
        </div>
      </div>

      <div v-if="testResult" :class="['text-xs px-3 py-2 rounded', testResult.success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700']">
        {{ testResult.message }}
      </div>

      <div class="flex items-center justify-between pt-2">
        <UButton
          color="gray"
          variant="ghost"
          size="sm"
          :loading="testing"
          :disabled="hasMissingRequired"
          @click="onTest"
        >
          Test credentials
        </UButton>
        <div class="flex items-center gap-2">
          <UButton color="gray" variant="ghost" size="sm" @click="emit('cancel')">Cancel</UButton>
          <UButton type="submit" color="blue" size="sm" :loading="submitting" :disabled="!form.name || hasMissingRequired">
            {{ isEditMode ? 'Save' : 'Save Integration' }}
          </UButton>
        </div>
      </div>
    </form>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

const props = defineProps<{
  integrationType: string
  integrationTitle?: string
  editConnection?: any
}>()
const emit = defineEmits<{
  (e: 'saved', connection: any): void
  (e: 'cancel'): void
}>()

const toast = useToast()
const isEditMode = computed(() => !!props.editConnection)

interface FieldSpec {
  key: string
  title: string
  type: string
  format?: string
  description?: string
  required: boolean
}

// The integration's admin credential schema — pulled from the registry's
// `/data_sources/<type>/fields` endpoint. The form renders whatever the
// registry declares, so adding a new integration is a registry change + an
// icon, no code change here.
const fieldsByAuth = ref<Record<string, any>>({})
const defaultAuth = ref<string>('')
const loadingFields = ref(true)
const credentialFields = computed<FieldSpec[]>(() => {
  const schema = fieldsByAuth.value[defaultAuth.value]
  if (!schema) return []
  const props = (schema.properties || {})
  const req = schema.required || []
  return Object.keys(props).map((k) => {
    const f = props[k] || {}
    return {
      key: k,
      title: f.title || k,
      type: f.type || 'string',
      format: f['ui:type'] === 'password' ? 'password' : (f['ui:type'] === 'textarea' ? 'textarea' : f.format),
      description: f.description,
      required: req.includes(k),
    }
  })
})
const hasMissingRequired = computed(() =>
  credentialFields.value.some(f => f.required && !form.credentials[f.key] && !(isEditMode.value && f.format === 'password'))
)

const form = reactive<{ name: string, credentials: Record<string, any> }>({
  name: props.integrationTitle || '',
  credentials: {},
})

// "Auto-create a public agent" — sensible default for the common case where
// admin enables the integration so users in the org can use it immediately.
// Admin can uncheck if they want to enable the integration but stage agent
// creation separately (e.g., to set up access controls first).
const createAgent = ref(true)
const agentName = ref(props.integrationTitle || '')
watch(() => props.integrationTitle, (t) => {
  if (t && !agentName.value) agentName.value = t
})

const submitting = ref(false)
const testing = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)

async function onTest() {
  testing.value = true
  testResult.value = null
  try {
    const credentials: Record<string, any> = {}
    for (const [k, v] of Object.entries(form.credentials)) {
      if (v !== null && v !== undefined && v !== '') credentials[k] = v
    }
    // For per-user integrations, test-connection verifies that the admin
    // app credentials are well-formed (token can be acquired); it doesn't
    // touch any user-owned files.
    const response = await useMyFetch('/connections/test-params', {
      method: 'POST',
      body: {
        name: form.name || 'test',
        type: props.integrationType,
        config: {},
        credentials,
        auth_policy: 'user_required',
        allowed_user_auth_modes: ['oauth'],
      },
    })
    testResult.value = response.data.value as any
  } catch (e: any) {
    testResult.value = { success: false, message: e?.data?.detail || String(e) }
  } finally {
    testing.value = false
  }
}

async function loadFields() {
  loadingFields.value = true
  try {
    const { data } = await useMyFetch(`/data_sources/${props.integrationType}/fields`, {
      method: 'GET',
      // The admin form needs the "system credentials" variant (admin OAuth
      // app). The per-user OAuth variant (empty schema) is used by the
      // user-signin modal, not here.
    })
    const payload = data.value as any
    fieldsByAuth.value = payload?.credentials_by_auth || {}
    // Pick the auth variant that isn't the empty "oauth" delegated one. By
    // convention the admin variant is the registry's default.
    defaultAuth.value = payload?.auth?.default || Object.keys(fieldsByAuth.value)[0] || ''
  } catch (e: any) {
    toast.add({ title: 'Failed to load fields', description: e?.data?.detail || String(e), color: 'red' })
  } finally {
    loadingFields.value = false
  }
}

watch(() => props.integrationType, async (t) => {
  if (t) await loadFields()
}, { immediate: true })

watch(() => props.editConnection, async (conn) => {
  if (!conn) return
  // Load non-secret credential values via /connections/{id} so admins can
  // edit existing OAuth app config without re-entering everything.
  try {
    const { data } = await useMyFetch(`/connections/${conn.id}`, { method: 'GET' })
    const detail = data.value as any
    if (detail) {
      form.name = detail.name || ''
      const meta = detail.credentials_meta || {}
      // Copy in non-secret fields only — secrets stay blank with "unchanged"
      // placeholder. The backend treats blank as "keep existing".
      for (const f of credentialFields.value) {
        if (f.format !== 'password' && meta[f.key] !== undefined) {
          form.credentials[f.key] = meta[f.key]
        }
      }
    }
  } catch {}
}, { immediate: true })

async function handleSubmit() {
  submitting.value = true
  try {
    // Strip blanks so they don't overwrite secrets in edit mode.
    const credentials: Record<string, any> = {}
    for (const [k, v] of Object.entries(form.credentials)) {
      if (v !== null && v !== undefined && v !== '') credentials[k] = v
    }

    if (isEditMode.value && props.editConnection) {
      const response = await useMyFetch(`/connections/${props.editConnection.id}`, {
        method: 'PUT',
        body: { name: form.name, credentials },
      })
      if (response.data.value) emit('saved', response.data.value)
      return
    }

    // Create the connection first — it has to exist before we can link an
    // agent to it.
    const connResponse = await useMyFetch('/connections', {
      method: 'POST',
      body: {
        name: form.name,
        type: props.integrationType,
        config: {},
        credentials,
        // Integrations are user_required by definition — the admin enables
        // the org-level OAuth app; each user signs in individually.
        auth_policy: 'user_required',
        allowed_user_auth_modes: ['oauth'],
      },
    })
    const connection = connResponse.data.value as any
    if (!connection?.id) {
      emit('saved', connection)
      return
    }

    // Optionally auto-create a public agent linked to this connection. This
    // is best-effort: if it fails (e.g., duplicate name), we still surface
    // the successful integration save and toast the agent failure.
    if (createAgent.value) {
      try {
        await useMyFetch('/data_sources', {
          method: 'POST',
          body: {
            name: (agentName.value || props.integrationTitle || form.name).trim(),
            type: props.integrationType,
            connection_ids: [connection.id],
            is_public: false,
            auth_policy: 'user_required',
            allowed_user_auth_modes: ['oauth'],
          },
        })
        toast.add({
          title: 'Integration ready',
          description: `${form.name} is connected and a public agent was created. Users can sign in to start using it.`,
          color: 'green',
        })
      } catch (e: any) {
        toast.add({
          title: 'Integration saved, but agent creation failed',
          description: e?.data?.detail || String(e),
          color: 'yellow',
        })
      }
    }

    emit('saved', connection)
  } catch (e: any) {
    toast.add({
      title: isEditMode.value ? 'Failed to update integration' : 'Failed to create integration',
      description: e?.data?.detail || String(e),
      color: 'red',
    })
  } finally {
    submitting.value = false
  }
}
</script>
