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

const form = reactive({ name: 'Browser', allow_downloads: true })
const urlText = ref('')
const testing = ref(false)
const submitting = ref(false)
const testResult = ref<{ success: boolean; message: string } | null>(null)
const submitError = ref<string | null>(null)

const patterns = computed(() =>
  urlText.value.split('\n').map((s) => s.trim()).filter(Boolean)
)

function buildConfig() {
  return { url_patterns: patterns.value, allow_downloads: form.allow_downloads }
}

async function testConnection() {
  testing.value = true
  testResult.value = null
  try {
    const response = await useMyFetch('/connections/test-params', {
      method: 'POST',
      body: { name: form.name || 'browser', type: 'browser', config: buildConfig(), credentials: {} },
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
        credentials: {},
        auth_policy: 'system_only',
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
