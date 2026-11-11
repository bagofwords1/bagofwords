<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-2xl' }">
    <div class="p-5 text-start" data-testid="browse-connectors-modal">
      <div class="flex items-start gap-3 mb-4">
        <div class="flex-1 min-w-0">
          <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('connectors.title') }}</h3>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('connectors.subtitle') }}</p>
        </div>
        <button type="button" :aria-label="$t('common.close')" class="text-ink-muted hover:text-gray-600 dark:hover:text-gray-300" @click="open = false">
          <UIcon name="heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>

      <div class="relative mb-3">
        <UIcon name="i-heroicons-magnifying-glass" class="absolute start-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-ink-muted pointer-events-none" />
        <input
          v-model="query"
          type="search"
          :placeholder="$t('connectors.search')"
          class="w-full h-9 ps-8 pe-3 text-sm rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
      </div>

      <div v-if="loading && !connectors.length" class="py-8 flex justify-center"><Spinner class="w-4 h-4 animate-spin" /></div>
      <div v-else-if="!filtered.length" class="py-8 text-center text-sm text-gray-500 dark:text-gray-400">
        {{ connectors.length ? $t('connectors.noResults', { q: query.trim() }) : $t('connectors.empty') }}
      </div>
      <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800 border border-gray-100 dark:border-gray-800 rounded-lg max-h-[60vh] overflow-y-auto">
        <li v-for="ds in filtered" :key="ds.id" class="flex items-center gap-3 px-3 py-2.5" :data-testid="`browse-row-${ds.id}`">
          <DataSourceIcon :type="ds.type" :connector-key="ds.connector_key" :icon-token="ds.icon_token" :icon="ds.icon" class="h-6 w-6 shrink-0" />
          <div class="flex-1 min-w-0">
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate"><bdi>{{ ds.name }}</bdi></div>
            <div class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ accessLine(ds) }}</div>
          </div>
          <span class="shrink-0 inline-flex items-center gap-1.5 text-xs" :class="statusClass(ds)" :data-testid="`browse-status-${ds.id}`">
            <span class="w-2 h-2 rounded-full" :class="dotClass(ds)" />
            {{ statusLabel(ds) }}
          </span>
          <div class="shrink-0 flex items-center gap-1.5">
            <UButton
              v-if="['sign_in', 'offline'].includes(statusOf(ds))"
              size="xs" color="blue" :loading="connectingId === ds.id"
              :data-testid="`browse-signin-${ds.id}`"
              @click="connect(ds)"
            >{{ $t('connectors.signIn') }}</UButton>
            <UButton
              v-if="statusOf(ds) === 'connected'"
              size="xs" color="gray" variant="soft" :loading="busyId === ds.id + ':out'"
              :data-testid="`browse-signout-${ds.id}`"
              @click="doSignOut(ds)"
            >{{ $t('connectors.signOut') }}</UButton>
            <UButton
              v-if="canRemove(ds)"
              size="xs" color="gray" variant="ghost" :loading="busyId === ds.id + ':agent'"
              :title="$t('connectors.useAsAgent')"
              :data-testid="`browse-as-agent-${ds.id}`"
              @click="doUseAsAgent(ds)"
            >{{ $t('connectors.useAsAgent') }}</UButton>
            <UButton
              v-if="canRemove(ds)"
              size="xs" color="red" variant="ghost" :loading="busyId === ds.id + ':rm'"
              :data-testid="`browse-remove-${ds.id}`"
              @click="doRemove(ds)"
            >{{ $t('connectors.remove') }}</UButton>
          </div>
        </li>
      </ul>

      <p class="mt-3 text-2xs text-ink-muted">{{ canAddConnectors ? $t('connectors.addHint') : $t('connectors.askAdmin') }}</p>
    </div>
    <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="credsDs" @saved="onCredsSaved" />
  </UModal>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits(['update:modelValue', 'changed'])
const open = computed({ get: () => props.modelValue, set: (v: boolean) => emit('update:modelValue', v) })

const { t } = useI18n()
const { connectors, loading, load, signOut, remove, connectorStatus } = useConnectors()
const { connectingId, startConnect, asCredentialsModalSource } = useDataSourceConnect()

const query = ref('')
const busyId = ref<string | null>(null)
const showCredsModal = ref(false)
const credsDs = ref<any>(null)

const statusOf = (ds: any) => connectorStatus(ds)
const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  const list = [...connectors.value].sort((a, b) => String(a.name).localeCompare(String(b.name)))
  return q ? list.filter((ds) => String(ds.name).toLowerCase().includes(q) || String(ds.type || '').toLowerCase().includes(q)) : list
})
const canAddConnectors = computed(() => useCan('create_data_source'))
const canRemove = (ds: any) => useCan('manage', { type: 'data_source', id: String(ds.id) })

function statusLabel(ds: any): string {
  switch (statusOf(ds)) {
    case 'connected': return t('connectors.statusConnected')
    case 'shared': return t('connectors.statusShared')
    case 'service_account': return t('connectors.statusServiceAccount')
    case 'offline': return t('connectors.statusOffline')
    default: return t('connectors.statusNotConnected')
  }
}
const dotClass = (ds: any) => ({ sign_in: 'bg-gray-300 dark:bg-gray-600', offline: 'bg-red-500' } as any)[statusOf(ds)] || 'bg-green-500'
const statusClass = (ds: any) => (statusOf(ds) === 'offline' ? 'text-red-600 dark:text-red-400' : 'text-gray-600 dark:text-gray-300')
const accessLine = (ds: any) => (statusOf(ds) === 'shared' ? t('connectors.accessShared') : t('connectors.accessPersonal'))

async function connect(ds: any) {
  const openModal = await startConnect(ds)
  if (!openModal) return // OAuth redirect in progress
  credsDs.value = asCredentialsModalSource(ds)
  showCredsModal.value = true
}
async function onCredsSaved() {
  showCredsModal.value = false
  await load({ force: true })
  emit('changed')
}
async function doSignOut(ds: any) {
  busyId.value = ds.id + ':out'
  try { if (await signOut(ds)) emit('changed') } finally { busyId.value = null }
}
// An admin curated this (e.g. an MCP wrapped with instructions) and wants it
// picked like any agent: store the explicit kind; it leaves this list.
async function doUseAsAgent(ds: any) {
  busyId.value = ds.id + ':agent'
  try {
    const { error } = await useMyFetch(`/data_sources/${ds.id}`, { method: 'PUT', body: { kind: 'agent' } })
    if (error.value) {
      useToast().add({ title: t('connectors.actionFailed'), description: (error.value as any)?.data?.detail || '', color: 'red' })
      return
    }
    useToast().add({ title: t('connectors.movedToAgents', { name: ds.name }), color: 'green' })
    await load({ force: true })
    emit('changed')
  } finally { busyId.value = null }
}
async function doRemove(ds: any) {
  if (!window.confirm(t('connectors.removeConfirm', { name: ds.name }))) return
  busyId.value = ds.id + ':rm'
  try { if (await remove(ds)) emit('changed') } finally { busyId.value = null }
}

watch(open, (v) => { if (v) { query.value = ''; load({ force: true }) } })
</script>
