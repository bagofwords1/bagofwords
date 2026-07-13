<template>
    <div class="p-4">
      <div class="flex items-center gap-2 mb-2">
        <img src="/icons/whatsapp.png" alt="WhatsApp" class="w-5 h-5" />
        <h1 class="text-lg font-semibold">{{ $t('settings.integrations.channels.whatsapp.title') }}</h1>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.subtitle') }}</p>
      <hr class="my-4" />

      <div v-if="integrated" class="mb-4">
        <p class="text-green-600 mb-4">{{ $t('settings.integrations.channels.whatsapp.connectedNotice') }}</p>

        <!-- Usage Notes -->
        <div class="bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-blue-800 mb-2">{{ $t('settings.integrations.channels.common.usageNotes') }}</h3>
          <ul class="text-sm text-blue-700 space-y-1 list-disc list-inside">
            <li>{{ $t('settings.integrations.channels.whatsapp.noteRegistered') }}</li>
            <li>{{ $t('settings.integrations.channels.whatsapp.noteDm') }}</li>
            <li>{{ $t('settings.integrations.channels.whatsapp.noteWindow') }}</li>
          </ul>
        </div>

        <!-- Integration Details -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{{ $t('settings.integrations.channels.common.integrationDetails') }}</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.businessName') }}</span>
              <span class="font-medium">{{ integrationData?.platform_config?.verified_name || $t('settings.integrations.channels.common.na') }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.phoneNumber') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.display_phone_number || $t('settings.integrations.channels.common.na') }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.phoneNumberId') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.phone_number_id || $t('settings.integrations.channels.common.na') }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.wabaId') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.waba_id || $t('settings.integrations.channels.common.na') }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.common.connectedLabel') }}</span>
              <span class="font-medium">{{ formatDate(integrationData?.created_at) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.common.lastUpdatedLabel') }}</span>
              <span class="font-medium">{{ formatDate(integrationData?.updated_at) }}</span>
            </div>
          </div>
        </div>

        <!-- Webhook Setup Info -->
        <div class="bg-yellow-50 dark:bg-yellow-950 border border-yellow-200 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-yellow-800 mb-2">{{ $t('settings.integrations.channels.whatsapp.webhookSetup') }}</h3>
          <p class="text-xs text-yellow-700 mb-1">
            {{ $t('settings.integrations.channels.whatsapp.webhookConfigure') }}
          </p>
          <code class="block bg-white dark:bg-gray-900 border border-yellow-200 rounded px-2 py-1 text-xs break-all">
            {{ webhookUrl }}
          </code>
          <i18n-t keypath="settings.integrations.channels.whatsapp.webhookVerifyHint" tag="p" class="text-xs text-yellow-700 mt-2">
            <template #verifyToken><strong>{{ $t('settings.integrations.channels.whatsapp.verifyTokenName') }}</strong></template>
            <template #messages><code>messages</code></template>
          </i18n-t>
        </div>

        <!-- Conversation session staleness -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.integrations.channels.common.sessionStalenessTitle') }}</h3>
          <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">{{ $t('settings.integrations.channels.whatsapp.sessionStalenessDesc') }}</p>
          <div class="flex items-center gap-2">
            <input
              v-model.number="sessionMaxAgeHours"
              type="number"
              min="1"
              max="720"
              class="w-24 border rounded px-2 py-1 text-sm"
              :disabled="savingSessionMaxAge"
              @keyup.enter="saveSessionMaxAge"
            />
            <span class="text-sm text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.common.hoursSuffix') }}</span>
            <UButton size="xs" color="gray" :loading="savingSessionMaxAge" @click="saveSessionMaxAge">
              {{ $t('settings.integrations.channels.common.save') }}
            </UButton>
          </div>
        </div>

        <UButton
          color="red"
          variant="soft"
          @click="disconnect"
        >
          {{ $t('settings.integrations.channels.common.disconnect') }}
        </UButton>
      </div>
      <div v-else>
        <form @submit.prevent="connect">
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.accessToken') }}</label>
            <input v-model="accessToken" type="password" class="w-full border rounded px-2 py-1" required />
            <i18n-t keypath="settings.integrations.channels.whatsapp.accessTokenHint" tag="p" class="text-xs text-gray-500 dark:text-gray-400 mt-1">
              <template #scope1><code>whatsapp_business_messaging</code></template>
              <template #scope2><code>whatsapp_business_management</code></template>
            </i18n-t>
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.phoneNumberIdLabel') }}</label>
            <input v-model="phoneNumberId" type="text" class="w-full border rounded px-2 py-1" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.wabaIdLabel') }}</label>
            <input v-model="wabaId" type="text" class="w-full border rounded px-2 py-1" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.appSecret') }}</label>
            <input v-model="appSecret" type="password" class="w-full border rounded px-2 py-1" required />
            <i18n-t keypath="settings.integrations.channels.whatsapp.appSecretHint" tag="p" class="text-xs text-gray-500 dark:text-gray-400 mt-1">
              <template #header><code>X-Hub-Signature-256</code></template>
            </i18n-t>
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.verifyToken') }}</label>
            <input v-model="verifyToken" type="text" class="w-full border rounded px-2 py-1" required />
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ $t('settings.integrations.channels.whatsapp.verifyTokenHint') }}</p>
          </div>

          <div class="bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-3 mb-4">
            <p class="text-xs text-gray-600 dark:text-gray-400 mb-1">{{ $t('settings.integrations.channels.whatsapp.webhookAfterConnect') }}</p>
            <code class="block bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded px-2 py-1 text-xs break-all">
              {{ webhookUrl }}
            </code>
          </div>

          <button type="submit" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md">{{ $t('settings.integrations.channels.common.connect') }}</button>
        </form>
      </div>
      <button class="absolute top-2 end-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-400" :title="$t('settings.integrations.channels.common.close')" @click="$emit('close')">✕</button>
    </div>
  </template>

  <script setup lang="ts">
  import { ref, computed, onMounted, watch } from 'vue'
  const props = defineProps<{
    integrated: boolean
    integrationData?: any
  }>()
  const emit = defineEmits(['close', 'updated'])
  const toast = useToast()
  const { t } = useI18n()

  // Conversation session staleness (org setting, hours). Default mirrors the
  // backend schema default (24h).
  const sessionMaxAgeHours = ref<number>(24)
  const savingSessionMaxAge = ref(false)

  async function loadSessionMaxAge() {
    const res = await useMyFetch('/api/organization/settings')
    if (res.status.value === 'success') {
      const v = (res.data.value as any)?.config?.whatsapp_session_max_age_hours
      if (typeof v === 'number' && v > 0) sessionMaxAgeHours.value = v
    }
  }

  async function saveSessionMaxAge() {
    const v = sessionMaxAgeHours.value
    if (!Number.isInteger(v) || v < 1 || v > 720) {
      toast.add({ title: t('settings.integrations.channels.common.sessionStalenessInvalid'), color: 'amber' })
      return
    }
    savingSessionMaxAge.value = true
    const res = await useMyFetch('/api/organization/settings', {
      method: 'PUT',
      body: { config: { whatsapp_session_max_age_hours: v } },
    })
    savingSessionMaxAge.value = false
    if (res.status.value === 'success') {
      toast.add({ title: t('settings.integrations.channels.common.sessionStalenessSaved'), color: 'green' })
    } else {
      toast.add({
        title: t('settings.integrations.channels.common.failedToUpdateSetting'),
        description: (res.error.value as any)?.data?.detail || (res.error.value as any)?.message,
        color: 'red',
      })
    }
  }

  onMounted(() => {
    if (props.integrated) loadSessionMaxAge()
  })
  watch(() => props.integrated, (v) => { if (v) loadSessionMaxAge() })

  const accessToken = ref('')
  const phoneNumberId = ref('')
  const wabaId = ref('')
  const appSecret = ref('')
  const verifyToken = ref('')

  const webhookUrl = computed(() => {
    if (typeof window !== 'undefined') {
      return `${window.location.origin}/api/settings/integrations/whatsapp/webhook`
    }
    return '/api/settings/integrations/whatsapp/webhook'
  })

  const _df = useFormatDate()
  function formatDate(dateString: string | undefined) {
    if (!dateString) return t('settings.integrations.channels.common.na')
    return _df.format(dateString, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    })
  }

  async function connect() {
      const res = await useMyFetch('/api/settings/integrations/whatsapp', {
        method: 'POST',
        body: {
          access_token: accessToken.value,
          phone_number_id: phoneNumberId.value,
          waba_id: wabaId.value,
          app_secret: appSecret.value,
          verify_token: verifyToken.value,
        }
      })
      if (res.status.value === 'success') {
        toast.add({
          title: t('settings.integrations.channels.whatsapp.connectedToast'),
          description: t('settings.integrations.channels.whatsapp.connectedToastDesc'),
          color: 'green'
        })
        emit('updated')
        emit('close')
      } else {
        toast.add({
          title: t('settings.integrations.channels.whatsapp.failedConnect'),
          description: (res.error.value as any).data?.detail || (res.error.value as any).message,
          color: 'red'
        })
    }
  }

  async function disconnect() {
    if (!props.integrationData?.id) return
    const res = await useMyFetch(`/api/settings/integrations/${props.integrationData.id}`, {
      method: 'DELETE'
    })
    if (res.status.value === 'success') {
      toast.add({
        title: t('settings.integrations.channels.whatsapp.disconnectedToast'),
        description: t('settings.integrations.channels.whatsapp.disconnectedToastDesc'),
        color: 'green'
      })
      emit('updated')
      emit('close')
    } else {
      toast.add({
        title: t('settings.integrations.channels.whatsapp.failedDisconnect'),
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }
  </script>
