<template>
  <div class="space-y-4">
    <div>
      <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.browserForm.name') }}</label>
      <input
        v-model="form.name"
        type="text"
        :placeholder="$t('data.browserForm.namePlaceholder')"
        class="w-full text-sm rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
    </div>

    <div>
      <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('data.browserForm.urls') }}</label>
      <textarea
        v-model="urlText"
        rows="5"
        dir="ltr"
        placeholder="https://portal.vendor.com/**&#10;https://*.wikipedia.org/**"
        class="w-full text-xs font-mono rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 focus:outline-none focus:ring-1 focus:ring-blue-500"
      ></textarea>
      <p class="mt-1 text-[11px] text-gray-500 dark:text-gray-400">{{ $t('data.browserForm.urlsHelp') }}</p>
    </div>

    <label class="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300">
      <input v-model="form.allow_downloads" type="checkbox" class="rounded border-gray-300" />
      {{ $t('data.browserForm.allowDownloads') }}
    </label>

    <!-- Shared secret parameters -->
    <div class="rounded-md border border-gray-200 dark:border-gray-800 p-3 space-y-2">
      <div class="flex items-center justify-between">
        <div>
          <p class="text-xs font-medium text-gray-700 dark:text-gray-300 flex items-center gap-1">
            <Icon name="heroicons:key" class="w-3.5 h-3.5" />
            {{ $t('data.browserForm.secrets') }}
          </p>
          <p class="text-[11px] text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('data.browserForm.secretsHelp') }}</p>
        </div>
      </div>
      <div v-for="(row, i) in secretRows" :key="i" class="flex items-center gap-2">
        <input
          v-model="row.key"
          type="text"
          dir="ltr"
          :placeholder="$t('data.browserForm.keyPlaceholder')"
          class="w-2/5 text-xs font-mono rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
        <input
          v-model="row.value"
          type="password"
          dir="ltr"
          autocomplete="new-password"
          :placeholder="$t('data.browserForm.valuePlaceholder')"
          class="flex-1 text-xs font-mono rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
        <button type="button" @click="secretRows.splice(i, 1)" class="text-gray-400 hover:text-red-500">
          <Icon name="heroicons:x-mark" class="w-4 h-4" />
        </button>
      </div>
      <button type="button" @click="secretRows.push({ key: '', value: '' })" class="text-xs text-blue-600 hover:text-blue-800 flex items-center gap-1">
        <Icon name="heroicons:plus" class="w-3.5 h-3.5" />
        {{ $t('data.browserForm.addSecret') }}
      </button>
    </div>

    <!-- Per-user secrets -->
    <div class="rounded-md border border-gray-200 dark:border-gray-800 p-3 space-y-2">
      <label class="flex items-start gap-2 text-xs text-gray-700 dark:text-gray-300">
        <input v-model="perUser" type="checkbox" class="rounded border-gray-300 mt-0.5" />
        <span>
          <span class="font-medium flex items-center gap-1">
            <Icon name="heroicons:user-circle" class="w-3.5 h-3.5" />
            {{ $t('data.browserForm.perUser') }}
          </span>
          <span class="block text-[11px] text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('data.browserForm.perUserHelp') }}</span>
        </span>
      </label>
      <div v-if="perUser" class="ps-6">
        <label class="block text-[11px] font-medium text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.browserForm.userKeys') }}</label>
        <input
          v-model="userKeysText"
          type="text"
          dir="ltr"
          placeholder="API_TOKEN, FIRST_NAME"
          class="w-full text-xs font-mono rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
        <p class="mt-1 text-[11px] text-gray-500 dark:text-gray-400">{{ $t('data.browserForm.userKeysHelp') }}</p>
      </div>
    </div>

    <!-- Proxy (advanced) -->
    <div class="rounded-md border border-gray-200 dark:border-gray-800 p-3 space-y-2">
      <button type="button" @click="showProxy = !showProxy" class="w-full flex items-center justify-between text-xs font-medium text-gray-700 dark:text-gray-300">
        <span class="flex items-center gap-1">
          <Icon name="heroicons:globe-alt" class="w-3.5 h-3.5" />
          {{ $t('data.browserForm.proxy') }}
          <span v-if="form.proxy_server" class="ms-1 px-1.5 py-0.5 rounded bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-300 text-[10px]">{{ $t('data.browserForm.proxyConfigured') }}</span>
        </span>
        <Icon :name="showProxy ? 'heroicons:chevron-up' : 'heroicons:chevron-down'" class="w-4 h-4 text-gray-400" />
      </button>
      <div v-if="showProxy" class="space-y-2">
        <p class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('data.browserForm.proxyHelp') }}</p>
        <div class="grid grid-cols-2 gap-2">
          <div>
            <label class="block text-[11px] text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.browserForm.proxyServer') }}</label>
            <input v-model="form.proxy_server" type="text" dir="ltr" placeholder="http://proxy.internal:3128"
              class="w-full text-xs font-mono rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-[11px] text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.browserForm.proxyBypass') }}</label>
            <input v-model="form.proxy_bypass" type="text" dir="ltr" placeholder="localhost,127.0.0.1,.internal"
              class="w-full text-xs font-mono rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-[11px] text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.browserForm.proxyUsername') }}</label>
            <input v-model="proxyUsername" type="text" dir="ltr" autocomplete="off"
              class="w-full text-xs rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
          <div>
            <label class="block text-[11px] text-gray-600 dark:text-gray-400 mb-1">{{ $t('data.browserForm.proxyPassword') }}</label>
            <input v-model="proxyPassword" type="password" dir="ltr" autocomplete="new-password"
              class="w-full text-xs rounded-md border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-500" />
          </div>
        </div>
      </div>
    </div>

    <div v-if="testResult" :class="['text-xs px-3 py-2 rounded', testResult.success ? 'bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300' : 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300']">
      {{ testResult.message }}
    </div>
    <div v-if="submitError" class="text-xs px-3 py-2 rounded bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300">
      {{ submitError }}
    </div>

    <div class="flex items-center justify-between pt-2">
      <button type="button" @click="testConnection" :disabled="testing || !patterns.length" class="text-xs text-blue-600 hover:text-blue-800 disabled:opacity-50">
        <Spinner v-if="testing" class="w-3 h-3 inline me-1" />
        {{ $t('data.browserForm.test') }}
      </button>
      <div class="flex items-center gap-2">
        <button type="button" @click="$emit('cancel')" class="text-xs px-3 py-1.5 rounded-md border border-gray-300 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800">
          {{ $t('data.browserForm.cancel') }}
        </button>
        <button type="button" @click="save" :disabled="submitting || !patterns.length || !form.name.trim()" class="text-xs px-3 py-1.5 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50">
          <Spinner v-if="submitting" class="w-3 h-3 inline me-1" />
          {{ $t('data.browserForm.save') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'

const emit = defineEmits<{ (e: 'saved', ds: any): void; (e: 'cancel'): void }>()

const form = reactive({ name: 'Browser', allow_downloads: true, proxy_server: '', proxy_bypass: '' })
const urlText = ref('')
const secretRows = ref<{ key: string; value: string }[]>([])
const perUser = ref(false)
const userKeysText = ref('')
const showProxy = ref(false)
const proxyUsername = ref('')
const proxyPassword = ref('')
const testing = ref(false)
const submitting = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)
const submitError = ref<string | null>(null)

const patterns = computed(() =>
  urlText.value.split('\n').map((s) => s.trim()).filter(Boolean)
)

const userSecretKeys = computed(() =>
  userKeysText.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean)
)

function buildConfig() {
  const cfg: Record<string, any> = { url_patterns: patterns.value, allow_downloads: form.allow_downloads }
  if (form.proxy_server.trim()) cfg.proxy_server = form.proxy_server.trim()
  if (form.proxy_bypass.trim()) cfg.proxy_bypass = form.proxy_bypass.trim()
  if (perUser.value && userSecretKeys.value.length) cfg.user_secret_keys = userSecretKeys.value
  // The backend validates `credentials` against the auth variant named in
  // config.auth_type; without it, secrets would validate as "no auth".
  if (Object.keys(buildCredentials()).length > 0 || perUser.value) cfg.auth_type = 'secrets'
  return cfg
}

function buildCredentials() {
  const secrets: Record<string, string> = {}
  for (const row of secretRows.value) {
    const k = row.key.trim()
    if (k && row.value) secrets[k] = row.value
  }
  const creds: Record<string, any> = {}
  const hasSecrets = Object.keys(secrets).length > 0
  if (hasSecrets) creds.secrets = secrets
  if (proxyUsername.value.trim()) creds.proxy_username = proxyUsername.value.trim()
  if (proxyPassword.value) creds.proxy_password = proxyPassword.value
  return creds
}

function authFields() {
  return {
    credentials: buildCredentials(),
    auth_policy: perUser.value ? 'user_required' : 'system_only',
    ...(perUser.value ? { allowed_user_auth_modes: ['secrets'] } : {}),
  }
}

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const response = await useMyFetch('/connections/test-params', {
      method: 'POST',
      body: { name: form.name || 'browser', type: 'browser', config: buildConfig(), credentials: buildCredentials() },
    })
    if (response.error?.value) {
      const err: any = response.error.value
      testResult.value = { success: false, message: err?.data?.detail || err?.message || 'Test failed' }
    } else {
      testResult.value = (response.data.value as any) || { success: false, message: 'No response' }
    }
  } catch (e: any) {
    testResult.value = { success: false, message: e?.data?.detail || 'Test failed' }
  } finally {
    testing.value = false
  }
}

async function save() {
  submitting.value = true
  submitError.value = null
  try {
    const response = await useMyFetch('/data_sources', {
      method: 'POST',
      body: {
        name: form.name.trim(),
        type: 'browser',
        config: buildConfig(),
        ...authFields(),
      },
    })
    if (response.error?.value) {
      submitError.value = response.error.value?.data?.detail || 'Failed to create browser connection'
      return
    }
    if (response.data.value) emit('saved', response.data.value)
  } catch (e: any) {
    submitError.value = e?.data?.detail || 'Failed to create browser connection'
  } finally {
    submitting.value = false
  }
}
</script>
