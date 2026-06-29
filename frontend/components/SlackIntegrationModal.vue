<template>
    <div class="p-4">
      <div class="flex items-center gap-2 mb-2">
        <img src="/icons/slack.png" alt="Slack" class="w-5 h-5" />
        <h1 class="text-lg font-semibold">{{ $t('settings.integrations.channels.slack.title') }}</h1>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.slack.subtitle') }}</p>
      <hr class="my-4" />

      <div v-if="integrated" class="mb-4">
        <p class="text-green-600 mb-4">{{ $t('settings.integrations.channels.slack.connectedNotice') }}</p>

        <!-- Usage Notes -->
        <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-blue-800 mb-2">{{ $t('settings.integrations.channels.common.usageNotes') }}</h3>
          <ul class="text-sm text-blue-700 space-y-1 list-disc list-inside">
            <li>{{ $t('settings.integrations.channels.slack.noteRegistered') }}</li>
            <li>{{ $t('settings.integrations.channels.slack.noteChannels') }}</li>
            <li>{{ $t('settings.integrations.channels.slack.noteDms') }}</li>
          </ul>
        </div>

        <!-- Integration Details -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{{ $t('settings.integrations.channels.common.integrationDetails') }}</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.slack.workspaceName') }}</span>
              <span class="font-medium">{{ integrationData?.platform_config?.team_name || $t('settings.integrations.channels.common.na') }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.slack.workspaceId') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.team_id || $t('settings.integrations.channels.common.na') }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.slack.baseUrl') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.base_url || $t('settings.integrations.channels.common.na') }}</span>
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
              <span class="font-medium">{{ $t('settings.integrations.channels.slack.autoLinkTitle') }}</span>
              <i18n-t keypath="settings.integrations.channels.slack.autoLinkDescConnected" tag="span" class="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                <template #scope><code>users:read.email</code></template>
              </i18n-t>
            </span>
          </label>
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
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.slack.botToken') }}</label>
            <input v-model="botToken" type="text" class="w-full border rounded px-2 py-1 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.slack.signingSecret') }}</label>
            <input v-model="signingSecret" type="text" class="w-full border rounded px-2 py-1 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" required />
          </div>
          <div class="mb-4">
            <label class="flex items-start gap-2 cursor-pointer">
              <input type="checkbox" v-model="autoLinkByEmail" class="mt-0.5" />
              <span class="text-sm">
                <span class="font-medium">{{ $t('settings.integrations.channels.slack.autoLinkTitle') }}</span>
                <i18n-t keypath="settings.integrations.channels.slack.autoLinkDescSetup" tag="span" class="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  <template #scope><code>users:read.email</code></template>
                </i18n-t>
              </span>
            </label>
          </div>
          <button type="submit" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md">{{ $t('settings.integrations.channels.common.connect') }}</button>
        </form>
      </div>
      <button class="absolute top-2 end-2 text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-300" :title="$t('settings.integrations.channels.common.close')" @click="$emit('close')">✕</button>
    </div>
  </template>
  
  <script setup lang="ts">
  import { ref, watch } from 'vue'
  const props = defineProps<{
    integrated: boolean
    integrationData?: any
  }>()
  const emit = defineEmits(['close', 'updated'])
  const toast = useToast()
  const { t } = useI18n()

  const botToken = ref('')
  const signingSecret = ref('')
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
      const res = await useMyFetch('/api/settings/integrations/slack', {
        method: 'POST',
        body: {
          bot_token: botToken.value,
          signing_secret: signingSecret.value,
          auto_link_by_email: autoLinkByEmail.value,
        }
      })
      if (res.status.value === 'success') {
        toast.add({
          title: t('settings.integrations.channels.slack.connectedToast'),
          description: t('settings.integrations.channels.slack.connectedToastDesc'),
          color: 'green'
        })
        emit('updated')
        emit('close')
      } else {
        toast.add({
        title: t('settings.integrations.channels.slack.failedConnect'),
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
        title: t('settings.integrations.channels.slack.disconnectedToast'),
        description: t('settings.integrations.channels.slack.disconnectedToastDesc'),
        color: 'green'
      })
      emit('updated')
      emit('close')
    } else {
      toast.add({
        title: t('settings.integrations.channels.slack.failedDisconnect'),
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }
  </script>