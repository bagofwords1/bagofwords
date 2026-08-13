<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-5 relative">
      <button @click="emit('update:modelValue', false)" class="absolute top-2 end-2 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 outline-none">
        <Icon name="heroicons:x-mark" class="w-5 h-5" />
      </button>

      <div class="mb-4">
        <h1 class="text-base font-semibold flex items-center">
            <DataSourceIcon :type="connectionType" class="h-4 me-2" />
            {{ $t('data.connectNamed', { name: ds?.name }) }}</h1>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ $t('data.provideCredentials') }}</p>
      </div>

      <div v-if="authOptions.length > 1" class="mb-3">
        <label class="text-xs text-gray-600 dark:text-gray-400">{{ $t('data.authMethod') }}</label>
        <USelectMenu v-model="authMode" :options="authOptions" option-attribute="label" value-attribute="value" />
      </div>

      <!-- OAuth mode: standard sign-in button -->
      <div v-if="isOAuthMode" class="mt-4">
        <UButton
          size="sm"
          color="blue"
          variant="solid"
          block
          :loading="oauthLoading"
          @click="onOAuthSignIn"
        >
          {{ currentAuthTitle || $t('data.signIn') }}
        </UButton>
      </div>

      <!-- Standard credential form -->
      <template v-else>
        <div class="space-y-3">
          <div v-for="field in credentialFields" :key="field.key" class="flex flex-col">
            <!-- Key/value secret map (e.g. browser secret parameters) -->
            <template v-if="field.uiType === 'keyvalue'">
              <label class="text-xs text-gray-600 dark:text-gray-400 mb-1">{{ field.title }}</label>
              <p class="text-[11px] text-gray-400 dark:text-gray-500 mb-2">{{ $t('data.secretsEditor.hint') }}</p>

              <!-- Already-saved keys: names only, values are write-only -->
              <div v-if="visibleSavedKeys.length" class="flex flex-wrap gap-1.5 mb-2">
                <span v-for="k in visibleSavedKeys" :key="k"
                  class="inline-flex items-center gap-1 ps-2 pe-1 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800 text-[11px] font-mono text-gray-700 dark:text-gray-300">
                  <Icon name="heroicons:key" class="w-3 h-3 text-gray-400" />
                  {{ k }}
                  <span class="text-gray-400">••••</span>
                  <button type="button" @click="removeSavedKey(k)" class="text-gray-400 hover:text-red-500 p-0.5" :title="$t('data.secretsEditor.remove')">
                    <Icon name="heroicons:x-mark" class="w-3 h-3" />
                  </button>
                </span>
              </div>

              <div v-for="(row, i) in secretRows" :key="i" class="flex items-center gap-2 mb-1.5">
                <input v-model="row.key" type="text" dir="ltr" :placeholder="$t('data.secretsEditor.key')" :readonly="row.declared"
                  :class="['w-2/5 text-xs font-mono border rounded px-2 py-1.5', row.declared ? 'bg-gray-50 dark:bg-gray-800 text-gray-500' : '']" />
                <input v-model="row.value" type="password" dir="ltr" autocomplete="new-password"
                  :placeholder="savedKeySet.has(row.key) ? $t('data.secretsEditor.replaceValue') : $t('data.secretsEditor.value')"
                  class="flex-1 text-xs font-mono border rounded px-2 py-1.5" />
                <button v-if="!row.declared" type="button" @click="secretRows.splice(i, 1)" class="text-gray-400 hover:text-red-500">
                  <Icon name="heroicons:x-mark" class="w-4 h-4" />
                </button>
                <span v-else class="w-4"></span>
              </div>
              <button type="button" @click="secretRows.push({ key: '', value: '', declared: false })" class="text-xs text-blue-600 hover:text-blue-800 flex items-center gap-1">
                <Icon name="heroicons:plus" class="w-3.5 h-3.5" />
                {{ $t('data.secretsEditor.add') }}
              </button>
            </template>

            <template v-else>
              <label class="text-xs text-gray-600 dark:text-gray-400 mb-1">{{ field.title }}</label>
              <input v-if="field.type === 'string'" :type="field.format === 'password' || field.uiType === 'password' ? 'password' : 'text'" v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
              <input v-else-if="field.type === 'integer'" type="number" v-model.number="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
              <UCheckbox v-else-if="field.type === 'boolean'" v-model="form.credentials[field.key]">{{ field.title }}</UCheckbox>
              <textarea v-else-if="field.type === 'text' || field.type === 'textarea'" v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm"></textarea>
              <input v-else v-model="form.credentials[field.key]" class="border rounded px-2 py-1 text-sm" />
            </template>
          </div>
        </div>

        <div class="flex justify-between mt-5">
          <UButton size="xs" color="gray" variant="soft" :loading="testing" @click="onTest">{{ $t('data.testConnection') }}</UButton>
          <div class="space-x-2">
            <UButton size="xs" color="gray" variant="soft" @click="emit('update:modelValue', false)">{{ $t('data.cancel') }}</UButton>
            <UButton size="xs" color="blue" variant="solid" :loading="saving" @click="onSave">{{ $t('data.save') }}</UButton>
          </div>
        </div>
      </template>

      <div v-if="testResult" class="mt-3 text-xs">
        <span :class="testResult.success ? 'text-green-600' : 'text-red-600'">
          {{ testResult.success ? $t('data.connectedSuccess') : $t('data.connectionFailed') }}
        </span>
        <span v-if="testResult.message" class="text-gray-500 dark:text-gray-400"> - {{ testResult.message }}</span>
      </div>
    </div>
  </UModal>

</template>

<script lang="ts" setup>
import { computed, watch, ref } from 'vue'

const props = defineProps<{ modelValue: boolean, dataSource: any }>()
const emit = defineEmits(['update:modelValue', 'saved'])

const { t } = useI18n()

const open = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v)
})

const ds = computed(() => props.dataSource)
// Use nested connection type (Option A architecture)
const connectionType = computed(() => ds.value?.connection?.type || ds.value?.type)
const connectionId = computed(() => ds.value?.connection?.id || ds.value?.connection_id || ds.value?.connections?.[0]?.id)
const authMode = ref<string>('')
const form = ref<{ auth_mode: string, credentials: Record<string, any>, is_primary?: boolean }>({ auth_mode: '', credentials: {}, is_primary: true })
const authOptions = ref<{ label: string, value: string }[]>([])
const fieldsByAuth = ref<Record<string, any>>({})
const credentialFields = computed(() => {
  const schema = fieldsByAuth.value[authMode.value]
  if (!schema) return []
  const props = (schema.properties || {})
  const req = schema.required || []
  return Object.keys(props)
    .map((k) => {
      const f = props[k] || {}
      return {
        key: k,
        title: f.title || k,
        type: f.type || 'string',
        format: f.format,
        uiType: f['ui:type'],
        uiScope: f['ui:scope'],
        required: req.includes(k)
      }
    })
    // Fields the admin fills on the connection (e.g. proxy auth) are not part
    // of the member connect flow.
    .filter((f) => f.uiScope !== 'system')
})

// --- key/value secret editor state ---------------------------------------
const secretRows = ref<{ key: string; value: string; declared: boolean }[]>([])
const savedKeys = ref<string[]>([])
const removedKeys = ref<string[]>([])
const savedKeySet = computed(() => new Set(savedKeys.value))
const visibleSavedKeys = computed(() => savedKeys.value.filter((k) => !removedKeys.value.includes(k)))
const keyValueField = computed(() => credentialFields.value.find((f) => f.uiType === 'keyvalue'))

function removeSavedKey(k: string) {
  if (!removedKeys.value.includes(k)) removedKeys.value.push(k)
}

let _secretEditorRun = 0
async function initSecretEditor() {
  // loadFields() and the authMode watcher can both trigger this; the run token
  // makes the last invocation win instead of both appending prefill rows.
  const run = ++_secretEditorRun
  secretRows.value = []
  removedKeys.value = []
  savedKeys.value = []
  if (!keyValueField.value) return
  // Names of already-saved secrets (values are write-only and never returned).
  let saved: string[] = []
  try {
    const { data } = await useMyFetch(`/data_sources/${ds.value.id}/my-credentials`, { method: 'GET' })
    const existing = data.value as any
    if (existing?.metadata_json?.secret_keys) saved = existing.metadata_json.secret_keys
  } catch (e) { /* no saved credentials yet */ }
  if (run !== _secretEditorRun) return
  savedKeys.value = saved
  // Keys the admin declared each member should provide → prefilled rows.
  const declared: string[] = ds.value?.connection?.config?.user_secret_keys
    || ds.value?.connections?.[0]?.config?.user_secret_keys || []
  const rows: { key: string; value: string; declared: boolean }[] = []
  for (const k of declared) {
    if (!saved.includes(k)) rows.push({ key: k, value: '', declared: true })
  }
  if (!rows.length && !saved.length) rows.push({ key: '', value: '', declared: false })
  secretRows.value = rows
}

function buildCredentialsPayload(): Record<string, any> {
  const creds: Record<string, any> = { ...form.value.credentials }
  const kv = keyValueField.value
  if (kv) {
    const map: Record<string, string | null> = {}
    for (const row of secretRows.value) {
      const k = row.key.trim()
      if (k && row.value) map[k] = row.value
    }
    for (const k of removedKeys.value) map[k] = null
    creds[kv.key] = map
  }
  return creds
}

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
  catalogOwnership.value = payload?.catalog_ownership || 'shared'
  // build options
  const names = Object.keys((payload?.auth?.by_auth) || {})
  authOptions.value = names.map((n) => ({ label: payload.auth.by_auth[n]?.title || n, value: n }))
  // The OAuth-only direct-redirect path is handled by `useConnectionSignIn`
  // before this modal ever opens. By the time we're here, the user actually
  // has a choice to make — render the standard form with the registry's
  // default auth selected.
  const defaultAuth = payload?.auth?.default
  authMode.value = (defaultAuth && names.includes(defaultAuth)) ? defaultAuth : names[0] || ''
  form.value.auth_mode = authMode.value
  form.value.credentials = {}
  // The authMode watcher re-inits the secret editor; only init here when the
  // watcher didn't fire (mode unchanged), to avoid duplicated prefill rows.
  await initSecretEditor()
}

const catalogOwnership = ref<string>('shared')

watch(authMode, async (v) => {
  form.value.auth_mode = v || ''
  form.value.credentials = {}
  await initSecretEditor()
})

const isOAuthMode = computed(() => authMode.value === 'oauth')
const currentAuthTitle = computed(() => {
  const opt = authOptions.value.find(o => o.value === authMode.value)
  return opt?.label || t('data.signIn')
})
const oauthLoading = ref(false)

async function onOAuthSignIn() {
  if (!connectionId.value) {
    testResult.value = { success: false, message: t('data.noConnectionForSource') }
    return
  }
  try {
    oauthLoading.value = true
    const { data, error } = await useMyFetch(`/connections/${connectionId.value}/oauth/authorize`, { method: 'GET' })
    if (error.value) throw error.value
    const result = data.value as any
    if (result?.authorization_url) {
      window.location.href = result.authorization_url
    }
  } catch (e: any) {
    testResult.value = { success: false, message: e?.message || t('data.oauthStartFailed') }
  } finally {
    oauthLoading.value = false
  }
}

const saving = ref(false)
const testing = ref(false)
const testResult = ref<{ success: boolean, message?: string } | null>(null)

async function onSave() {
  try {
    saving.value = true
    const body = { ...form.value, credentials: buildCredentialsPayload() }
    const { error } = await useMyFetch(`/data_sources/${ds.value.id}/my-credentials`, { method: 'POST', body })
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
    const body = { ...form.value, credentials: buildCredentialsPayload() }
    const { data, error } = await useMyFetch(`/data_sources/${ds.value.id}/my-credentials/test`, { method: 'POST', body })
    if (error.value) throw error.value
    testResult.value = data.value as any
  } catch (e: any) {
    testResult.value = { success: false, message: e?.message || t('data.failed') }
  } finally {
    testing.value = false
  }
}

</script>


