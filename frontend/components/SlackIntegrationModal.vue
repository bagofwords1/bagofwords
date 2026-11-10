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
              <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.slack.connectionModeLabel') }}</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.connection_mode === 'socket_mode' ? $t('settings.integrations.channels.slack.modeSocket') : $t('settings.integrations.channels.slack.modeEventsApi') }}</span>
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

        <!-- New conversation announcement -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{{ $t('settings.integrations.channels.common.announceNewReportTitle') }}</h3>
          <label class="flex items-start gap-2 cursor-pointer">
            <input
              type="checkbox"
              v-model="announceNewReport"
              :disabled="savingAnnounce"
              @change="saveAnnounceNewReport"
              class="mt-0.5"
              data-testid="announce-new-report-toggle"
            />
            <span class="text-sm">
              <span class="font-medium">{{ $t('settings.integrations.channels.common.announceNewReportLabel') }}</span>
              <span class="block text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.integrations.channels.common.announceNewReportDesc') }}</span>
            </span>
          </label>
        </div>

        <!-- Channel -> agent routing -->
        <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.integrations.channels.slack.channelRoutingTitle') }}</h3>
          <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">{{ $t('settings.integrations.channels.slack.channelRoutingDesc') }}</p>

          <div v-if="agentsLoading || channelsLoading" class="text-xs text-ink-muted">{{ $t('settings.integrations.channels.common.loading') }}</div>
          <template v-else>
            <!-- Connection default -->
            <div class="flex items-center gap-2 py-2 border-b border-gray-200 dark:border-gray-700">
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium">{{ $t('settings.integrations.channels.slack.connectionDefaultLabel') }}</div>
                <div class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.slack.connectionDefaultDesc') }}</div>
              </div>
              <USelectMenu
                v-model="defaultSel"
                :options="allAgents"
                multiple
                searchable
                option-attribute="name"
                value-attribute="id"
                :placeholder="$t('settings.integrations.channels.slack.channelPickerPlaceholder')"
                class="w-44 shrink-0"
              />
              <UButton size="xs" color="gray" :loading="savingKey==='__default__'" @click="saveDefault">{{ $t('settings.integrations.channels.slack.channelScopeSave') }}</UButton>
            </div>

            <!-- Discovered channels -->
            <div v-if="!channels.length" class="text-xs text-ink-muted py-3">{{ $t('settings.integrations.channels.slack.noChannelsYet') }}</div>
            <div
              v-for="ch in channels"
              :key="ch.external_channel_id"
              class="flex items-center gap-2 py-2 border-b border-gray-100 dark:border-gray-800"
            >
              <div class="flex-1 min-w-0">
                <div class="text-sm truncate">{{ ch.name || ch.external_channel_id }}</div>
                <div class="text-xs font-mono text-gray-400 truncate">{{ ch.external_channel_id }}</div>
              </div>
              <USelectMenu
                v-model="channelSel[ch.external_channel_id]"
                :options="allAgents"
                multiple
                searchable
                option-attribute="name"
                value-attribute="id"
                :placeholder="$t('settings.integrations.channels.slack.channelPickerPlaceholder')"
                class="w-44 shrink-0"
              />
              <UButton size="xs" color="gray" :loading="savingKey===ch.external_channel_id" @click="saveChannel(ch)">{{ $t('settings.integrations.channels.slack.channelScopeSave') }}</UButton>
            </div>

            <!-- Manual add -->
            <div class="mt-3">
              <div class="text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">{{ $t('settings.integrations.channels.slack.addChannelTitle') }}</div>
              <div class="flex items-center gap-2">
                <input v-model="newChannelId" :placeholder="$t('settings.integrations.channels.slack.addChannelIdPlaceholder')" class="flex-1 min-w-0 border rounded px-2 py-1 text-sm dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" />
                <input v-model="newChannelName" :placeholder="$t('settings.integrations.channels.slack.addChannelNamePlaceholder')" class="flex-1 min-w-0 border rounded px-2 py-1 text-sm dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" />
                <UButton size="xs" color="gray" :disabled="!newChannelId.trim()" @click="addChannel">{{ $t('settings.integrations.channels.slack.addChannelButton') }}</UButton>
              </div>
            </div>
          </template>
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
        <!-- Setup steps (Socket Mode) -->
        <div v-if="connectionMode === 'socket_mode'" class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mb-4">
          <p class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">{{ $t('settings.integrations.channels.slack.socketSetupIntro') }}</p>
          <ol class="text-xs text-gray-600 dark:text-gray-400 space-y-1 list-decimal list-inside">
            <li>{{ $t('settings.integrations.channels.slack.socketStep1') }}</li>
            <li>{{ $t('settings.integrations.channels.slack.socketStep2') }}</li>
            <li>{{ $t('settings.integrations.channels.slack.socketStep3') }}</li>
            <li>{{ $t('settings.integrations.channels.slack.socketStep4') }}</li>
            <li>{{ $t('settings.integrations.channels.slack.socketStep5') }}</li>
            <li>{{ $t('settings.integrations.channels.slack.socketStep6') }}</li>
          </ol>
        </div>

        <form @submit.prevent="connect">
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.slack.connectionModeLabel') }}</label>
            <div class="flex gap-4 text-sm">
              <label class="flex items-center gap-1.5 cursor-pointer">
                <input type="radio" value="socket_mode" v-model="connectionMode" />
                <span>{{ $t('settings.integrations.channels.slack.modeSocketRecommended') }}</span>
              </label>
              <label class="flex items-center gap-1.5 cursor-pointer">
                <input type="radio" value="events_api" v-model="connectionMode" />
                <span>{{ $t('settings.integrations.channels.slack.modeEventsApi') }}</span>
              </label>
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">
              {{ connectionMode === 'socket_mode' ? $t('settings.integrations.channels.slack.modeSocketHint') : $t('settings.integrations.channels.slack.modeEventsApiHint') }}
            </p>
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.slack.botToken') }}</label>
            <input v-model="botToken" type="text" placeholder="xoxb-…" class="w-full border rounded px-2 py-1 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" required />
          </div>
          <div v-if="connectionMode === 'socket_mode'" class="mb-4">
            <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.slack.appToken') }}</label>
            <input v-model="appToken" type="text" placeholder="xapp-…" class="w-full border rounded px-2 py-1 dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" required />
          </div>
          <div v-else class="mb-4">
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
          <button type="submit" class="bg-blue-600 text-white text-sm px-3 py-1.5 rounded-md">{{ $t('settings.integrations.channels.common.connect') }}</button>
        </form>
      </div>
      <button class="absolute top-2 end-2 text-ink-muted hover:text-gray-600 dark:hover:text-gray-300" :title="$t('settings.integrations.channels.common.close')" @click="$emit('close')">✕</button>
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

  const botToken = ref('')
  const signingSecret = ref('')
  const appToken = ref('')
  // Socket Mode is the recommended transport for new setups: no public URL,
  // no signing secret, and it enables the Agent experience events.
  const connectionMode = ref<'socket_mode' | 'events_api'>('socket_mode')
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

  // "New conversation report" announcement (org setting, per platform).
  // Default mirrors the backend schema default (on).
  const announceNewReport = ref<boolean>(true)
  const savingAnnounce = ref(false)

  async function loadAnnounceNewReport() {
    const res = await useMyFetch('/api/organization/settings')
    if (res.status.value === 'success') {
      const v = (res.data.value as any)?.config?.slack_announce_new_report
      if (typeof v === 'boolean') announceNewReport.value = v
    }
  }

  async function saveAnnounceNewReport() {
    savingAnnounce.value = true
    const res = await useMyFetch('/api/organization/settings', {
      method: 'PUT',
      body: { config: { slack_announce_new_report: announceNewReport.value } },
    })
    savingAnnounce.value = false
    if (res.status.value === 'success') {
      toast.add({
        title: announceNewReport.value ? t('settings.integrations.channels.common.announceNewReportEnabled') : t('settings.integrations.channels.common.announceNewReportDisabled'),
        color: 'green',
      })
    } else {
      announceNewReport.value = !announceNewReport.value
      toast.add({
        title: t('settings.integrations.channels.common.failedToUpdateSetting'),
        description: (res.error.value as any)?.data?.detail || (res.error.value as any)?.message,
        color: 'red',
      })
    }
  }

  // --- Channel -> agent routing ---
  // One Slack workspace is a single connection, so routing is per-channel: an
  // admin maps a channel to a set of agents; a channel with none uses the
  // connection default; routing only narrows what a user can already reach.
  const allAgents = ref<any[]>([])
  const agentsLoading = ref(false)
  const channelsLoading = ref(false)
  const channels = ref<any[]>([])
  const defaultSel = ref<string[]>([])
  const channelSel = ref<Record<string, string[]>>({})
  const savingKey = ref<string | null>(null)
  const newChannelId = ref('')
  const newChannelName = ref('')

  async function loadAgents() {
    agentsLoading.value = true
    const res = await useMyFetch('/api/data_sources', { query: { show_all: true } })
    agentsLoading.value = false
    if (res.status.value === 'success') {
      allAgents.value = (((res.data.value as any) || []) as any[]).map((d) => ({ id: d.id, name: d.name }))
    }
  }

  async function loadChannels() {
    const id = props.integrationData?.id
    if (!id) return
    channelsLoading.value = true
    const res = await useMyFetch(`/api/settings/integrations/${id}/channels`)
    channelsLoading.value = false
    if (res.status.value === 'success') {
      const data = res.data.value as any
      defaultSel.value = (data?.default_agent_ids || []) as string[]
      channels.value = (data?.channels || []) as any[]
      const map: Record<string, string[]> = {}
      for (const ch of channels.value) map[ch.external_channel_id] = (ch.agent_ids || []) as string[]
      channelSel.value = map
    }
  }

  async function loadRouting() {
    await Promise.all([loadAgents(), loadChannels()])
  }

  async function putScope(channel: string, agentIds: string[], channelName?: string) {
    const id = props.integrationData?.id
    if (!id) return false
    const q = channel ? `?channel=${encodeURIComponent(channel)}` : ''
    const body: any = { agent_ids: agentIds }
    if (channelName) body.channel_name = channelName
    const res = await useMyFetch(`/api/settings/integrations/${id}/agents${q}`, { method: 'PUT', body })
    return res.status.value === 'success'
  }

  function _routingToast(ok: boolean) {
    toast.add(ok
      ? { title: t('settings.integrations.channels.slack.channelScopeSaved'), color: 'green' }
      : { title: t('settings.integrations.channels.common.failedToUpdateSetting'), color: 'red' })
  }

  async function saveDefault() {
    savingKey.value = '__default__'
    const ok = await putScope('', defaultSel.value)
    savingKey.value = null
    _routingToast(ok)
  }

  async function saveChannel(ch: any) {
    savingKey.value = ch.external_channel_id
    const ok = await putScope(ch.external_channel_id, channelSel.value[ch.external_channel_id] || [])
    savingKey.value = null
    _routingToast(ok)
  }

  async function addChannel() {
    const cid = newChannelId.value.trim()
    if (!cid) return
    const ok = await putScope(cid, [], newChannelName.value.trim() || undefined)
    if (ok) {
      newChannelId.value = ''
      newChannelName.value = ''
      await loadChannels()
    } else {
      toast.add({ title: t('settings.integrations.channels.common.failedToUpdateSetting'), color: 'red' })
    }
  }

  onMounted(() => { if (props.integrated) { loadAnnounceNewReport(); loadRouting() } })
  watch(() => props.integrated, (v) => { if (v) { loadAnnounceNewReport(); loadRouting() } })

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
          connection_mode: connectionMode.value,
          app_token: connectionMode.value === 'socket_mode' ? appToken.value : null,
          signing_secret: connectionMode.value === 'events_api' ? signingSecret.value : null,
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