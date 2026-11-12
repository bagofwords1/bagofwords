<template>
  <!-- Only shown when the user can reach at least one connector (signed in or not). -->
  <div v-if="connectors.length" class="inline-block relative" data-testid="connectors-menu">
    <UPopover v-model:open="menuOpen" :popper="{ strategy: 'absolute', placement: 'bottom-start', offset: [0, 8] }">
      <UTooltip :text="$t('connectors.title')" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
        <button
          type="button"
          class="relative inline-flex items-center text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800 rounded-md p-2 text-xs"
          data-testid="connectors-trigger"
          :aria-label="$t('connectors.title')"
        >
          <PlugIcon class="h-4 w-4" />
          <span v-if="connectedCount" class="ms-1 text-xs tabular-nums" data-testid="connectors-count">{{ connectedCount }}</span>
          <span
            v-if="needsSignInCount"
            class="absolute top-1 end-1 w-1.5 h-1.5 rounded-full bg-red-500"
            :title="$t('connectors.needsSignIn', { n: needsSignInCount }, needsSignInCount)"
          />
        </button>
      </UTooltip>
      <template #panel="{ close }">
        <div class="text-xs text-start w-[280px] rounded-xl py-1" data-testid="connectors-panel">
          <div class="px-3 pt-2 pb-1 text-2xs font-medium uppercase tracking-wide text-ink-muted">{{ $t('connectors.title') }}</div>
          <ul class="max-h-72 overflow-y-auto">
            <li
              v-for="ds in connectors"
              :key="ds.id"
              class="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-800/70"
              :data-testid="`connector-row-${ds.id}`"
            >
              <DataSourceIcon :type="ds.type" :connector-key="ds.connector_key" :icon-token="ds.icon_token" :icon="ds.icon" class="h-4 w-4 shrink-0" />
              <span class="flex-1 min-w-0 truncate text-gray-800 dark:text-gray-200"><bdi>{{ ds.name }}</bdi></span>
              <button
                v-if="statusOf(ds) === 'sign_in'"
                type="button"
                class="shrink-0 inline-flex items-center gap-1 h-6 px-2 rounded-md bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-400 text-2xs font-medium hover:bg-blue-100 disabled:opacity-50"
                :disabled="connectingId === ds.id"
                :data-testid="`connector-signin-${ds.id}`"
                @click="connect(ds)"
              >
                <Spinner v-if="connectingId === ds.id" class="w-3 h-3 animate-spin" />
                {{ $t('connectors.signIn') }}
              </button>
              <span v-else class="shrink-0 inline-flex items-center gap-1.5 text-2xs text-ink-muted" :title="statusLabel(ds)">
                <span class="w-2 h-2 rounded-full" :class="statusOf(ds) === 'offline' ? 'bg-red-500' : 'bg-green-500'" />
                {{ statusLabel(ds) }}
              </span>
            </li>
          </ul>
          <div class="mt-1 border-t border-gray-100 dark:border-gray-800 pt-1">
            <button
              type="button"
              class="w-full flex items-center gap-2 px-3 py-1.5 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/70"
              data-testid="connectors-browse"
              @click="close(); menuOpen = false; showBrowse = true"
            >
              <Icon name="heroicons-squares-2x2" class="w-4 h-4 text-ink-muted" />
              {{ $t('connectors.browse') }}
            </button>
          </div>
        </div>
      </template>
    </UPopover>

    <BrowseConnectorsModal v-model="showBrowse" @changed="load({ force: true })" />
    <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="credsDs" @saved="onCredsSaved" />
  </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import PlugIcon from '~/components/icons/PlugIcon.vue'
import BrowseConnectorsModal from '~/components/connectors/BrowseConnectorsModal.vue'

const { t } = useI18n()
const { connectors, loading, load, connectorStatus } = useConnectors()
const { connectingId, startConnect, asCredentialsModalSource } = useDataSourceConnect()

const showBrowse = ref(false)
const menuOpen = ref(false)
// Refresh on open so a sign-in/out elsewhere (or in another tab) shows here.
watch(menuOpen, (v) => { if (v) load() })
const showCredsModal = ref(false)
const credsDs = ref<any>(null)

const statusOf = (ds: any) => connectorStatus(ds)
const connectedCount = computed(() => connectors.value.filter((ds) => ['connected', 'shared', 'service_account'].includes(statusOf(ds))).length)
const needsSignInCount = computed(() => connectors.value.filter((ds) => ['sign_in', 'offline'].includes(statusOf(ds))).length)

function statusLabel(ds: any): string {
  switch (statusOf(ds)) {
    case 'connected': return t('connectors.statusConnected')
    case 'shared': return t('connectors.statusShared')
    case 'service_account': return t('connectors.statusServiceAccount')
    case 'offline': return t('connectors.statusOffline')
    default: return t('connectors.signIn')
  }
}

async function connect(ds: any) {
  const openModal = await startConnect(ds)
  if (!openModal) return // OAuth redirect in progress
  credsDs.value = asCredentialsModalSource(ds)
  showCredsModal.value = true
}

async function onCredsSaved() {
  showCredsModal.value = false
  await load({ force: true })
}

onMounted(() => load())
</script>
