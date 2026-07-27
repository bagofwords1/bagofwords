<template>
    <div class="p-4">
      <div class="flex items-center gap-2 mb-2">
        <img src="/icons/google_chat.png" alt="Google Chat" class="w-5 h-5" />
        <h1 class="text-lg font-semibold">{{ $t('settings.integrations.channels.googleChat.title') }}</h1>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.googleChat.subtitle') }}</p>
      <hr class="my-4" />

      <div v-if="integrated" class="mb-4">
        <p class="text-green-600 mb-4">{{ $t('settings.integrations.channels.googleChat.connectedNotice') }}</p>

        <!-- Usage Notes -->
        <div class="bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-blue-800 mb-2">{{ $t('settings.integrations.channels.common.usageNotes') }}</h3>
          <ul class="text-sm text-blue-700 space-y-1 list-disc list-inside">
            <li>{{ $t('settings.integrations.channels.googleChat.noteRegistered') }}</li>
            <li>{{ $t('settings.integrations.channels.googleChat.noteChannels') }}</li>
            <li>{{ $t('settings.integrations.channels.googleChat.noteChats') }}</li>
            <li>{{ $t('settings.integrations.channels.googleChat.noteNoInbound') }}</li>
          </ul>
        </div>

        <!-- Integration Details -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{{ $t('settings.integrations.channels.common.integrationDetails') }}</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.googleChat.subscription') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.pubsub_subscription || $t('settings.integrations.channels.common.na') }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.googleChat.serviceAccount') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.client_email || $t('settings.integrations.channels.common.na') }}</span>
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

        <!-- Account Linking -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{{ $t('settings.integrations.channels.common.accountLinking') }}</h3>
          <label class="flex items-start gap-2 cursor-pointer">
            <input
              type="checkbox"
              v-model="autoLinkByEmail"
              :disabled="savingAutoLink"
              @change="saveAutoLinkByEmail"
              class="mt-0.5"
            />
            <span class="text-sm">
              <span class="font-medium">{{ $t('settings.integrations.channels.googleChat.autoLinkTitle') }}</span>
              <span class="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                {{ $t('settings.integrations.channels.googleChat.autoLinkDescConnected') }}
              </span>
            </span>
          </label>
        </div>

        <!-- Conversation session staleness -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.integrations.channels.common.sessionStalenessTitle') }}</h3>
          <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">{{ $t('settings.integrations.channels.googleChat.sessionStalenessDesc') }}</p>
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
        <!-- Setup instructions -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <p class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">{{ $t('settings.integrations.channels.googleChat.setupIntro') }}</p>
          <ol class="text-xs text-gray-600 dark:text-gray-400 space-y-1 list-decimal list-inside">
            <li>{{ $t('settings.integrations.channels.googleChat.setupStep1') }}</li>
            <li>{{ $t('settings.integrations.channels.googleChat.setupStep2') }}</li>
            <li>{{ $t('settings.integrations.channels.googleChat.setupStep3') }}</li>
            <li>{{ $t('settings.integrations.channels.googleChat.setupStep4') }}</li>
            <li>{{ $t('settings.integrations.channels.googleChat.setupStep5') }}</li>
          </ol>
        </div>

        <form @submit.prevent="connect">
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.googleChat.subscriptionLabel') }}</label>
            <input v-model="pubsubSubscription" type="text" class="w-full border rounded px-2 py-1 font-mono text-xs" :placeholder="$t('settings.integrations.channels.googleChat.subscriptionPlaceholder')" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.googleChat.serviceAccountJsonLabel') }}</label>
            <textarea v-model="serviceAccountJson" rows="6" class="w-full border rounded px-2 py-1 font-mono text-xs" :placeholder="$t('settings.integrations.channels.googleChat.serviceAccountJsonPlaceholder')" required></textarea>
          </div>
          <div class="mb-4">
            <label class="flex items-start gap-2 cursor-pointer">
              <input type="checkbox" v-model="autoLinkByEmail" class="mt-0.5" />
              <span class="text-sm">
                <span class="font-medium">{{ $t('settings.integrations.channels.googleChat.autoLinkTitle') }}</span>
                <span class="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  {{ $t('settings.integrations.channels.googleChat.autoLinkDescSetup') }}
                </span>
              </span>
            </label>
          </div>
          <button type="submit" :disabled="connecting" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md disabled:opacity-50">{{ $t('settings.integrations.channels.common.connect') }}</button>
        </form>
      </div>
      <button class="absolute top-2 end-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-400" :title="$t('settings.integrations.channels.common.close')" @click="$emit('close')">&#x2715;</button>
    </div>
  </template>

  <script setup lang="ts">
  import { ref, watch, onMounted } from 'vue'
  const props = defineProps<{
    integrated: boolean
    integrationData?: any
  }>()
  const emit = defineEmits(['close', 'updated'])
  const toast = useToast()
  const { t } = useI18n()

  // Conversation session staleness (org setting, hours). Default mirrors the
  // backend schema default (120h = 5 days).
  const sessionMaxAgeHours = ref<number>(120)
  const savingSessionMaxAge = ref(false)

  async function loadSessionMaxAge() {
    const res = await useMyFetch('/api/organization/settings')
    if (res.status.value === 'success') {
      const v = (res.data.value as any)?.config?.google_chat_session_max_age_hours
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
      body: { config: { google_chat_session_max_age_hours: v } },
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

  const pubsubSubscription = ref('')
  const serviceAccountJson = ref('')
  const connecting = ref(false)
  // Default ON for new connections; reflects stored config for existing ones.
  const autoLinkByEmail = ref<boolean>(
    props.integrationData?.platform_config?.auto_link_by_email ?? true
  )
  const savingAutoLink = ref(false)

  watch(() => props.integrationData?.platform_config?.auto_link_by_email, (v) => {
    if (v !== undefined) autoLinkByEmail.value = !!v
  })

  async function saveAutoLinkByEmail() {
    if (!props.integrationData?.id) return
    savingAutoLink.value = true
    const nextConfig = {
      ...(props.integrationData?.platform_config || {}),
      auto_link_by_email: autoLinkByEmail.value,
    }
    const res = await useMyFetch(`/api/settings/integrations/${props.integrationData.id}`, {
      method: 'PUT',
      body: { platform_config: nextConfig },
    })
    savingAutoLink.value = false
    if (res.status.value === 'success') {
      toast.add({
        title: autoLinkByEmail.value ? t('settings.integrations.channels.common.autoLinkEnabled') : t('settings.integrations.channels.common.autoLinkDisabled'),
        color: 'green',
      })
      emit('updated')
    } else {
      autoLinkByEmail.value = !autoLinkByEmail.value
      toast.add({
        title: t('settings.integrations.channels.common.failedToUpdateSetting'),
        description: (res.error.value as any)?.data?.detail || (res.error.value as any)?.message,
        color: 'red',
      })
    }
  }

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
      connecting.value = true
      const res = await useMyFetch('/api/settings/integrations/google_chat', {
        method: 'POST',
        body: {
          pubsub_subscription: pubsubSubscription.value.trim(),
          service_account_json: serviceAccountJson.value,
          auto_link_by_email: autoLinkByEmail.value,
        }
      })
      connecting.value = false
      if (res.status.value === 'success') {
        toast.add({
          title: t('settings.integrations.channels.googleChat.connectedToast'),
          description: t('settings.integrations.channels.googleChat.connectedToastDesc'),
          color: 'green'
        })
        emit('updated')
        emit('close')
      } else {
        toast.add({
        title: t('settings.integrations.channels.googleChat.failedConnect'),
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }

  async function disconnect() {
    const res = await useMyFetch(`/api/settings/integrations/${props.integrationData?.id}`, {
      method: 'DELETE'
    })
    if (res.status.value === 'success') {
      toast.add({
        title: t('settings.integrations.channels.googleChat.disconnectedToast'),
        description: t('settings.integrations.channels.googleChat.disconnectedToastDesc'),
        color: 'green'
      })
      emit('updated')
      emit('close')
    } else {
      toast.add({
        title: t('settings.integrations.channels.googleChat.failedDisconnect'),
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }
  </script>
