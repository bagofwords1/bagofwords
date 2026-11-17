<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
    <div class="p-6 space-y-4" data-testid="slack-channel-picker">
      <div>
        <h2 class="text-lg font-semibold">{{ $t('slackChannels.title') }}</h2>
        <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ $t('slackChannels.subtitle') }}</p>
      </div>

      <div v-if="loading" class="text-xs text-ink-muted py-6 text-center">{{ $t('common.loading') }}</div>

      <div v-else-if="loadFailed" class="text-xs text-red-600 py-6 text-center" data-testid="slack-scope-load-failed">
        {{ $t('slackChannels.loadFailed') }}
      </div>

      <template v-else>
        <div class="space-y-2">
          <label class="flex items-start gap-2 text-sm cursor-pointer">
            <input v-model="mode" type="radio" value="all" class="mt-0.5" data-testid="slack-scope-all" />
            <span>
              <span class="font-medium">{{ $t('slackChannels.allChannels') }}</span>
              <span class="block text-xs text-gray-500 dark:text-gray-400">{{ $t('slackChannels.allChannelsHint') }}</span>
            </span>
          </label>
          <label class="flex items-start gap-2 text-sm cursor-pointer">
            <input v-model="mode" type="radio" value="selected" class="mt-0.5" data-testid="slack-scope-selected" />
            <span class="font-medium">{{ $t('slackChannels.selectedChannels') }}</span>
          </label>
        </div>

        <div v-if="mode === 'selected'" class="border border-gray-200 dark:border-gray-700 rounded-md">
          <div v-if="connectRequired" class="p-4 text-xs text-gray-600 dark:text-gray-300">
            {{ $t('slackChannels.connectRequired') }}
          </div>
          <template v-else>
            <div class="p-2 border-b border-gray-100 dark:border-gray-800">
              <input v-model="query" type="text" :placeholder="$t('slackChannels.searchPlaceholder')"
                     data-testid="slack-channel-search"
                     class="w-full border border-gray-300 dark:border-gray-600 rounded-md px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-500" />
            </div>
            <ul class="max-h-64 overflow-y-auto divide-y divide-gray-50 dark:divide-gray-800">
              <li v-for="ch in visibleChannels" :key="ch.id">
                <label class="flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800">
                  <input v-model="selectedIds" type="checkbox" :value="ch.id" />
                  <UIcon :name="ch.is_private ? 'i-heroicons-lock-closed' : 'i-heroicons-hashtag'" class="w-3.5 h-3.5 text-ink-muted shrink-0" />
                  <span class="truncate">{{ ch.name }}</span>
                  <span v-if="ch.notMine" class="ms-auto text-2xs text-ink-muted shrink-0">{{ $t('slackChannels.notYourChannel') }}</span>
                </label>
              </li>
              <li v-if="!visibleChannels.length" class="px-3 py-4 text-xs text-ink-muted text-center">{{ $t('slackChannels.noMatch') }}</li>
            </ul>
            <div class="px-3 py-1.5 text-2xs text-ink-muted border-t border-gray-100 dark:border-gray-800">
              {{ $t('slackChannels.selectedCount', { n: selectedIds.length }) }}
            </div>
          </template>
        </div>

        <label class="flex items-start gap-2 text-sm cursor-pointer">
          <input v-model="includeDms" type="checkbox" class="mt-0.5" data-testid="slack-include-dms" />
          <span>
            <span class="font-medium">{{ $t('slackChannels.includeDms') }}</span>
            <span class="block text-xs text-gray-500 dark:text-gray-400">{{ $t('slackChannels.includeDmsHint') }}</span>
          </span>
        </label>

        <p class="text-2xs text-ink-muted">{{ $t('slackChannels.membershipNote') }}</p>

        <div class="flex justify-end gap-2 pt-1">
          <UButton color="gray" variant="ghost" size="sm" @click="isOpen = false">{{ $t('data.cancel') }}</UButton>
          <UButton color="blue" size="sm" :loading="saving" :disabled="mode === 'selected' && !selectedIds.length"
                   data-testid="slack-channel-save" @click="save">
            {{ $t('data.save') }}
          </UButton>
        </div>
      </template>
    </div>
  </UModal>
</template>

<script setup lang="ts">
// Per-agent Slack scope: which channels this agent reads through a shared
// Slack connection, and whether it also reads the user's DMs. Stored by Slack
// channel id, so a renamed channel stays selected. It only narrows — each
// person still reads only the channels they belong to.
interface Channel { id: string; name: string; is_private?: boolean; notMine?: boolean }

const isOpen = defineModel<boolean>({ default: false })
const props = defineProps<{ dsId: string; connectionId: string }>()
const emit = defineEmits<{ (e: 'saved', scope: any): void }>()
const { t } = useI18n()
const toast = useToast()

const loading = ref(true)
// The current choice couldn't be read: never fall back to "all channels",
// or saving would silently erase the agent's choice.
const loadFailed = ref(false)
const saving = ref(false)
const connectRequired = ref(false)
const channels = ref<Channel[]>([])
const selectedIds = ref<string[]>([])
const includeDms = ref(true)
const mode = ref<'all' | 'selected'>('all')
const query = ref('')

const base = computed(() => `/data_sources/${props.dsId}/connections/${props.connectionId}`)

const visibleChannels = computed(() => {
  const q = query.value.trim().toLowerCase().replace(/^#/, '')
  const list = q ? channels.value.filter(c => c.name.toLowerCase().includes(q)) : channels.value
  // Selected first, then by name — the current choice is what you review.
  const sel = new Set(selectedIds.value)
  return [...list].sort((a, b) => Number(sel.has(b.id)) - Number(sel.has(a.id)) || a.name.localeCompare(b.name))
})

async function load() {
  loading.value = true
  query.value = ''
  try {
    const [scopeRes, convRes] = await Promise.all([
      useMyFetch(`${base.value}/channel-scope`, { method: 'GET' }),
      useMyFetch(`${base.value}/conversations`, { method: 'GET' }),
    ])
    loadFailed.value = !!scopeRes.error.value || !scopeRes.data.value
    if (loadFailed.value) return
    const scope = scopeRes.data.value as any
    const conv = (convRes.data.value as any) || { channels: [], connect_required: true }
    connectRequired.value = !!conv.connect_required
    const mine: Channel[] = conv.channels || []
    const mineIds = new Set(mine.map(c => c.id))
    // Keep channels another manager chose that this person isn't in, so a
    // save here doesn't silently drop them.
    const others: Channel[] = (scope.allowed_channels || [])
      .filter((c: any) => !mineIds.has(c.id))
      .map((c: any) => ({ id: c.id, name: c.name || c.id, notMine: true }))
    channels.value = [...mine, ...others]
    selectedIds.value = (scope.allowed_channels || []).map((c: any) => c.id)
    includeDms.value = scope.include_direct_messages !== false
    mode.value = selectedIds.value.length ? 'selected' : 'all'
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const byId = new Map(channels.value.map(c => [c.id, c]))
    const allowed = mode.value === 'all' ? [] : selectedIds.value.map(id => ({ id, name: byId.get(id)?.name || '' }))
    const { data, error } = await useMyFetch(`${base.value}/channel-scope`, {
      method: 'PUT',
      body: { allowed_channels: allowed, include_direct_messages: includeDms.value },
    })
    if (error.value) {
      toast.add({ title: t('slackChannels.saveFailed'), description: (error.value as any)?.data?.detail, color: 'red' })
      return
    }
    toast.add({ title: t('slackChannels.saved'), color: 'green' })
    emit('saved', data.value)
    isOpen.value = false
  } finally {
    saving.value = false
  }
}

watch(isOpen, (open) => { if (open) load() }, { immediate: true })
</script>
