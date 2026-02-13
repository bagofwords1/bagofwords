<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-5 relative">
      <button @click="emit('update:modelValue', false)" class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
        <Icon name="heroicons:x-mark" class="w-5 h-5" />
      </button>

      <div class="mb-4">
        <h1 class="text-base font-semibold flex items-center">
            <DataSourceIcon :type="connectionType" class="h-4 mr-2" />
            Connect {{ ds?.name }}</h1>
        <p class="mt-1 text-xs text-gray-500">Provide your credentials to enable access for you.</p>
      </div>

      <div class="mb-3">
        <label class="text-xs text-gray-600">Authentication Method</label>
        <USelectMenu v-model="authMode" :options="authOptions" option-attribute="label" value-attribute="value" />
      </div>

      <div class="space-y-3">
        <div v-for="field in credentialFields" :key="field.key" class="flex flex-col">
          <label class="text-xs text-gray-600 mb-1">{{ field.title }}</label>
          <input v-if="field.type === 'string'" :type="field.format === 'password' ? 'password' : 'text'" v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
          <input v-else-if="field.type === 'integer'" type="number" v-model.number="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
          <UCheckbox v-else-if="field.type === 'boolean'" v-model="form.credentials[field.key]">{{ field.title }}</UCheckbox>
          <textarea v-else-if="field.type === 'text' || field.type === 'textarea'" v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm"></textarea>
          <input v-else v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
        </div>
      </div>

      <div class="flex justify-between mt-5">
        <UButton size="xs" color="gray" variant="soft" :loading="testing" @click="onTest">Test connection</UButton>
        <div class="space-x-2">
          <UButton size="xs" color="gray" variant="soft" @click="emit('update:modelValue', false)">Cancel</UButton>
          <UButton size="xs" color="blue" variant="solid" :loading="saving" @click="onSave">Save</UButton>
        </div>
      </div>

      <div v-if="testResult" class="mt-3 text-xs">
        <span :class="testResult.success ? 'text-green-600' : 'text-red-600'">
          {{ testResult.success ? 'Connection successful' : 'Connection failed' }}
        </span>
        <span v-if="testResult.message" class="text-gray-500"> - {{ testResult.message }}</span>
      </div>
    </div>
  </UModal>
  
</template>

<script lang="ts" setup>
import { computed, watch, ref } from 'vue'

const props = defineProps<{ modelValue: boolean, dataSource: any }>()
const emit = defineEmits(['update:modelValue', 'saved'])

const open = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v)
})

const ds = computed(() => props.dataSource)
// Use nested connection type (Option A architecture)
const connectionType = computed(() => ds.value?.connection?.type || ds.value?.type)
const connectionId = computed(() => ds.value?.connection?.id)
const authMode = ref<string>('')
const form = ref<{ auth_mode: string, credentials: Record<string, any>, is_primary?: boolean }>({ auth_mode: '', credentials: {}, is_primary: true })
const authOptions = ref<{ label: string, value: string }[]>([])
const fieldsByAuth = ref<Record<string, any>>({})
const credentialFields = computed(() => {
  const schema = fieldsByAuth.value[authMode.value]
  if (!schema) return []
  const props = (schema.properties || {})
  const req = schema.required || []
  return Object.keys(props).map((k) => {
    const f = props[k] || {}
    return {
      key: k,
      title: f.title || k,
      type: f.type || 'string',
      format: f.format,
      required: req.includes(k)
    }
  })
})

watch(open, async (val) => {
  if (val && connectionType.value) {
    await loadFields()
  }
})

async function loadFields() {
  const { data } = await useMyFetch(`/data_sources/${connectionType.value}/fields`, { method: 'GET', query: { auth_policy: 'user_required' } })
  const payload = data.value as any
  const byAuth = (payload?.credentials_by_auth) || {}
  fieldsByAuth.value = byAuth
  // build options
  const names = Object.keys((payload?.auth?.by_auth) || {})
  authOptions.value = names.map((n) => ({ label: payload.auth.by_auth[n]?.title || n, value: n }))
  authMode.value = payload?.auth?.default || names[0] || ''
  form.value.auth_mode = authMode.value
  form.value.credentials = {}
}

watch(authMode, (v) => {
  form.value.auth_mode = v || ''
  form.value.credentials = {}
})

const saving = ref(false)
const testing = ref(false)
const testResult = ref<{ success: boolean, message?: string } | null>(null)

async function onSave() {
  try {
    saving.value = true
    const { error } = await useMyFetch(`/data_sources/${ds.value.id}/my-credentials`, { method: 'POST', body: form.value })
    if (error.value) throw error.value
    emit('saved')
    emit('update:modelValue', false)
  } catch (e) {
    // optionally toast
  } finally {
    saving.value = false
  }
}

async function onTest() {
  try {
    testing.value = true
    const { data, error } = await useMyFetch(`/data_sources/${ds.value.id}/my-credentials/test`, { method: 'POST', body: form.value })
    if (error.value) throw error.value
    testResult.value = data.value as any
  } catch (e: any) {
    testResult.value = { success: false, message: e?.message || 'Failed' }
  } finally {
    testing.value = false
  }
}

</script>


