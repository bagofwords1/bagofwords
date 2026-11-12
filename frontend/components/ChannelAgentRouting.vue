<template>
  <div class="rounded-xl border border-gray-200 dark:border-gray-700 p-4 mb-4">
    <div class="flex items-center gap-2 mb-0.5">
      <UIcon name="i-heroicons-arrows-right-left" class="w-4 h-4 text-ink-muted" />
      <h3 class="text-sm font-semibold text-gray-800 dark:text-gray-200">{{ $t('settings.integrations.channels.routing.title') }}</h3>
    </div>
    <p class="text-xs text-gray-500 dark:text-gray-400 mb-4 leading-relaxed">{{ $t('settings.integrations.channels.routing.desc') }}</p>

    <div v-if="agentsLoading || channelsLoading" class="flex items-center gap-2 text-xs text-ink-muted py-3">
      <UIcon name="i-heroicons-arrow-path" class="w-3.5 h-3.5 animate-spin" />
      {{ $t('settings.integrations.channels.common.loading') }}
    </div>
    <template v-else>
      <!-- Connection default -->
      <div class="rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 p-3 flex items-center gap-3">
        <div class="flex-1 min-w-0">
          <div class="text-sm font-medium flex items-center gap-1.5">
            <UIcon name="i-heroicons-globe-alt" class="w-3.5 h-3.5 text-ink-muted shrink-0" />
            {{ $t('settings.integrations.channels.routing.connectionDefaultLabel') }}
          </div>
          <div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.integrations.channels.routing.connectionDefaultDesc') }}</div>
        </div>
        <UIcon v-if="savedKey==='__default__'" name="i-heroicons-check-circle-20-solid" class="w-4 h-4 text-green-500 shrink-0" />
        <USelectMenu
          v-model="defaultSel"
          :options="allAgents"
          multiple
          searchable
          option-attribute="name"
          value-attribute="id"
          :placeholder="$t('settings.integrations.channels.routing.pickerPlaceholder')"
          class="w-48 shrink-0"
          @change="saveDefault"
        />
      </div>

      <!-- Discovered channels -->
      <div class="mt-4">
        <div class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1.5">
          {{ $t('settings.integrations.channels.routing.channelsHeading') }}<span v-if="channels.length"> ({{ channels.length }})</span>
        </div>
        <div v-if="!channels.length" class="rounded-lg border border-dashed border-gray-200 dark:border-gray-700 text-xs text-ink-muted px-3 py-4 text-center">
          {{ $t('settings.integrations.channels.routing.noChannelsYet') }}
        </div>
        <div v-else class="rounded-lg border border-gray-200 dark:border-gray-700 divide-y divide-gray-100 dark:divide-gray-800">
          <div
            v-for="ch in channels"
            :key="ch.external_channel_id"
            class="flex items-center gap-3 p-3 bg-white dark:bg-gray-800/50"
          >
            <UIcon name="i-heroicons-hashtag" class="w-3.5 h-3.5 text-ink-muted shrink-0" />
            <div class="flex-1 min-w-0">
              <div class="text-sm truncate">{{ ch.name || ch.external_channel_id }}</div>
              <div class="text-[11px] font-mono text-gray-400 truncate">{{ ch.external_channel_id }}</div>
            </div>
            <UIcon v-if="savedKey===ch.external_channel_id" name="i-heroicons-check-circle-20-solid" class="w-4 h-4 text-green-500 shrink-0" />
            <USelectMenu
              v-model="channelSel[ch.external_channel_id]"
              :options="allAgents"
              multiple
              searchable
              option-attribute="name"
              value-attribute="id"
              :placeholder="$t('settings.integrations.channels.routing.pickerPlaceholder')"
              class="w-48 shrink-0"
              @change="() => saveChannel(ch)"
            />
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
  import { ref, watch, onMounted } from 'vue'

  // Shared "Channel -> agent routing" panel for any external connection that
  // spans multiple channels (Slack, Teams). One connection can carry many
  // channels, so routing is per-channel: an admin maps a channel to a set of
  // agents; a channel with none mapped uses the connection default; routing
  // only ever narrows what a user can already reach. Platform-agnostic — the
  // backend keys scope on (platform_id, channel_id) the same way for every
  // platform, and channels are discovered on traffic.
  const props = defineProps<{
    integrationId?: string
    integrated: boolean
  }>()
  const toast = useToast()
  const { t } = useI18n()

  const allAgents = ref<any[]>([])
  const agentsLoading = ref(false)
  const channelsLoading = ref(false)
  const channels = ref<any[]>([])
  const defaultSel = ref<string[]>([])
  const channelSel = ref<Record<string, string[]>>({})
  const savedKey = ref<string | null>(null)
  let _savedTimer: any = null
  function _flagSaved(key: string) {
    savedKey.value = key
    clearTimeout(_savedTimer)
    _savedTimer = setTimeout(() => { if (savedKey.value === key) savedKey.value = null }, 1800)
  }

  async function loadAgents() {
    agentsLoading.value = true
    const res = await useMyFetch('/api/data_sources', { query: { show_all: true } })
    agentsLoading.value = false
    if (res.status.value === 'success') {
      allAgents.value = (((res.data.value as any) || []) as any[]).map((d) => ({ id: d.id, name: d.name }))
    }
  }

  async function loadChannels() {
    const id = props.integrationId
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

  async function putScope(channel: string, agentIds: string[]) {
    const id = props.integrationId
    if (!id) return false
    const q = channel ? `?channel=${encodeURIComponent(channel)}` : ''
    const res = await useMyFetch(`/api/settings/integrations/${id}/agents${q}`, { method: 'PUT', body: { agent_ids: agentIds } })
    return res.status.value === 'success'
  }

  // Autosave on selection change (USelectMenu emits `change` only on user
  // action, not on the programmatic v-model set during load). A brief inline
  // check confirms the save; only failures raise a toast.
  async function saveDefault() {
    const ok = await putScope('', defaultSel.value)
    if (ok) _flagSaved('__default__')
    else toast.add({ title: t('settings.integrations.channels.common.failedToUpdateSetting'), color: 'red' })
  }

  async function saveChannel(ch: any) {
    const ok = await putScope(ch.external_channel_id, channelSel.value[ch.external_channel_id] || [])
    if (ok) _flagSaved(ch.external_channel_id)
    else toast.add({ title: t('settings.integrations.channels.common.failedToUpdateSetting'), color: 'red' })
  }

  onMounted(() => { if (props.integrated) loadRouting() })
  watch(() => props.integrated, (v) => { if (v) loadRouting() })
  watch(() => props.integrationId, (v) => { if (v && props.integrated) loadRouting() })
</script>
