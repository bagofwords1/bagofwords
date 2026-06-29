<template>
    <div class="p-4">
      <div class="flex items-center gap-2 mb-2">
        <img src="/icons/teams.png" alt="Teams" class="w-5 h-5" />
        <h1 class="text-lg font-semibold">{{ $t('settings.integrations.channels.teams.title') }}</h1>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.teams.subtitle') }}</p>
      <hr class="my-4" />

      <div v-if="integrated" class="mb-4">
        <p class="text-green-600 mb-4">{{ $t('settings.integrations.channels.teams.connectedNotice') }}</p>

        <!-- Usage Notes -->
        <div class="bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-blue-800 mb-2">{{ $t('settings.integrations.channels.common.usageNotes') }}</h3>
          <ul class="text-sm text-blue-700 space-y-1 list-disc list-inside">
            <li>{{ $t('settings.integrations.channels.teams.noteRegistered') }}</li>
            <li>{{ $t('settings.integrations.channels.teams.noteChannels') }}</li>
            <li>{{ $t('settings.integrations.channels.teams.noteChats') }}</li>
          </ul>
        </div>

        <!-- Integration Details -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{{ $t('settings.integrations.channels.common.integrationDetails') }}</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.teams.tenantId') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.tenant_id || $t('settings.integrations.channels.common.na') }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.teams.appId') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.app_id || $t('settings.integrations.channels.common.na') }}</span>
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
              <span class="font-medium">{{ $t('settings.integrations.channels.teams.autoLinkTitle') }}</span>
              <span class="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                {{ $t('settings.integrations.channels.teams.autoLinkDescConnected') }}
              </span>
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
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.teams.appIdLabel') }}</label>
            <input v-model="appId" type="text" class="w-full border rounded px-2 py-1" :placeholder="$t('settings.integrations.channels.teams.appIdPlaceholder')" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.teams.clientSecret') }}</label>
            <input v-model="clientSecret" type="password" class="w-full border rounded px-2 py-1" :placeholder="$t('settings.integrations.channels.teams.clientSecretPlaceholder')" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.teams.tenantIdLabel') }}</label>
            <input v-model="tenantId" type="text" class="w-full border rounded px-2 py-1" :placeholder="$t('settings.integrations.channels.teams.tenantIdPlaceholder')" required />
          </div>
          <div class="mb-4">
            <label class="flex items-start gap-2 cursor-pointer">
              <input type="checkbox" v-model="autoLinkByEmail" class="mt-0.5" />
              <span class="text-sm">
                <span class="font-medium">{{ $t('settings.integrations.channels.teams.autoLinkTitle') }}</span>
                <span class="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  {{ $t('settings.integrations.channels.teams.autoLinkDescSetup') }}
                </span>
              </span>
            </label>
          </div>
          <button type="submit" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md">{{ $t('settings.integrations.channels.common.connect') }}</button>
        </form>
      </div>
      <button class="absolute top-2 end-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-400" :title="$t('settings.integrations.channels.common.close')" @click="$emit('close')">&#x2715;</button>
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

  const appId = ref('')
  const clientSecret = ref('')
  const tenantId = ref('')
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
      const res = await useMyFetch('/api/settings/integrations/teams', {
        method: 'POST',
        body: {
          app_id: appId.value,
          client_secret: clientSecret.value,
          tenant_id: tenantId.value,
          auto_link_by_email: autoLinkByEmail.value,
        }
      })
      if (res.status.value === 'success') {
        toast.add({
          title: t('settings.integrations.channels.teams.connectedToast'),
          description: t('settings.integrations.channels.teams.connectedToastDesc'),
          color: 'green'
        })
        emit('updated')
        emit('close')
      } else {
        toast.add({
        title: t('settings.integrations.channels.teams.failedConnect'),
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
        title: t('settings.integrations.channels.teams.disconnectedToast'),
        description: t('settings.integrations.channels.teams.disconnectedToastDesc'),
        color: 'green'
      })
      emit('updated')
      emit('close')
    } else {
      toast.add({
        title: t('settings.integrations.channels.teams.failedDisconnect'),
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }
  </script>
