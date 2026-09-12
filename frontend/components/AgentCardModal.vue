<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-xl' }">
    <div class="flex max-h-[calc(100dvh-3rem)] flex-col" data-testid="agent-card">
      <header class="shrink-0 px-6 pt-6 pb-5">
        <div class="flex items-start gap-3">
          <DataSourceIcon :type="agent?.type || connections[0]?.type" :icon="agent?.icon" class="mt-0.5 h-8 w-8 shrink-0" />
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
              <h2 class="text-lg font-semibold leading-7 text-gray-900 dark:text-white break-words">{{ agent?.name }}</h2>
              <span class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium" :class="stageStyle.badge">
                <span class="h-1.5 w-1.5 rounded-full" :class="stageStyle.dot" />
                {{ $t(`agentsPage.stage.${stage}`) }}
              </span>
            </div>
            <p v-if="agent?.description" class="mt-2 text-sm leading-5 text-gray-500 dark:text-gray-400 whitespace-pre-wrap break-words">{{ agent.description }}</p>
          </div>
          <button type="button" :aria-label="$t('common.close')" class="-me-1 -mt-1 rounded-md p-1.5 text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800" @click="open = false"><UIcon name="heroicons-x-mark" class="h-4 w-4" /></button>
        </div>
        <div class="mt-5 flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
          <UIcon name="heroicons-document-text" class="h-4 w-4 text-gray-400" />
          <Spinner v-if="instructionCount == null && instructionsLoading" class="h-3 w-3" />
          <span v-else-if="instructionCount == null">— {{ $t('agentsPage.instructions') }}</span>
          <span v-else>{{ $t('agentsPage.countInstructions', { n: formatNumber(instructionCount) }, instructionCount) }}</span>
        </div>
      </header>

      <section class="min-h-0 overflow-y-auto px-6 pb-6" :aria-label="$t('data.connectionsTitle')">
        <h3 class="mb-2 text-xs font-medium text-gray-400 dark:text-gray-500">{{ $t('data.connectionsTitle') }}</h3>
        <p v-if="!connections.length" class="py-4 text-sm text-gray-500">{{ $t('data.noLinkedConnections') }}</p>
        <div v-for="row in rows" :key="row.connection.id" class="connection-row py-3.5" :data-connection-id="row.connection.id">
          <div class="connection-name flex min-w-0 items-center gap-2.5">
            <DataSourceIcon :type="row.connection.type" :connector-key="row.connection.connector_key" class="h-5 w-5 shrink-0" />
            <span class="min-w-0 text-sm font-medium text-gray-800 dark:text-gray-200 break-words">{{ row.connection.name }}</span>
          </div>
          <div class="connection-count space-y-1 text-xs text-gray-500 dark:text-gray-400 tabular-nums" :title="row.configured ? $t('agentCard.configuredCountsHint') : undefined">
            <span v-for="count in row.counts" :key="count.key" class="block">{{ $t(count.key, { n: formatNumber(count.value) }, count.value) }}</span>
            <span v-if="!row.counts.length" aria-hidden="true">—</span>
          </div>
          <div class="connection-access text-end">
            <button v-if="row.needsSignIn" type="button" :disabled="!!signingInId" :aria-busy="signingInId === row.connection.id" class="inline-flex h-7 items-center justify-center gap-1.5 rounded-md bg-blue-500 px-2.5 text-xs font-medium text-white hover:bg-blue-600 disabled:opacity-50" @click="signInConnection(row.connection)">
              <Spinner v-if="signingInId === row.connection.id" class="h-3 w-3" />
              {{ $t('data.signIn') }}
            </button>
            <template v-else>
              <span class="inline-flex items-center justify-end gap-1.5 text-xs" :class="row.status === 'error' ? 'text-red-600' : 'text-gray-600 dark:text-gray-400'">
                <UIcon v-if="row.signedIn || row.status === 'success'" name="heroicons-check" class="h-3.5 w-3.5 text-green-600" />
                <Spinner v-else-if="row.status === 'indexing'" class="h-3 w-3" />
                {{ row.signedIn ? $t('agentCard.signedIn') : $t(statusLabelKey(row.status)) }}
              </span>
              <time v-if="row.signedIn && row.signedInDate" :datetime="row.connection.user_status.signed_in_at" class="mt-1 block text-[11px] text-gray-400">{{ row.signedInDate }}</time>
            </template>
          </div>
        </div>
        <p v-if="rows.some(row => row.configured)" class="mt-3 text-[11px] leading-4 text-gray-400 dark:text-gray-500">{{ $t('agentCard.configuredCountsHint') }}</p>
      </section>
    </div>
  </UModal>
  <UserDataSourceCredentialsModal v-model="showCredentials" :data-source="credentialSource" @saved="onCredentialsSaved" />
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import { deriveStage, stageMeta } from '~/composables/useDataSourcePublishStatus'
import { getEffectiveStatus, needsConnectionSignIn, statusLabelKey } from '~/composables/useConnectionStatus'
import { useConnectionSignIn } from '~/composables/useConnectionSignIn'

const props = defineProps<{ modelValue: boolean; agent: any; connections: any[]; instructionCount?: number | null; instructionsLoading?: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void; (e: 'changed'): void }>()
const open = computed({ get: () => props.modelValue, set: value => emit('update:modelValue', value) })
const { t, locale } = useI18n()
const route = useRoute()
const toast = useToast()
const stage = computed(() => deriveStage(props.agent?.publish_status, props.agent?.reliability_status))
const stageStyle = computed(() => stageMeta(stage.value))
const formatNumber = (value: number) => new Intl.NumberFormat(locale.value).format(value)
function signedInDate(conn: any) {
  // A last-used/test timestamp does not establish when authentication happened.
  const value = conn.user_status?.signed_in_at
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? null : new Intl.DateTimeFormat(locale.value, { month: 'short', day: 'numeric', year: 'numeric' }).format(date)
}
const rows = computed(() => props.connections.map(connection => {
  const needsSignIn = needsConnectionSignIn(connection)
  const status = getEffectiveStatus(connection)
  const signedIn = !needsSignIn && connection.user_status?.effective_auth === 'user' && connection.user_status?.has_user_credentials === true && !['error', 'indexing_failed'].includes(status)
  // Unsigned viewers may have an already-disclosed shared discovery summary.
  // Keep it clearly labeled and never fetch a privileged catalog for this card.
  const stats = needsSignIn && connection.indexing?.status === 'completed' ? connection.indexing.stats : null
  const configured = !!stats && (stats.table_count != null || stats.tool_count != null)
  const catalogCount = needsSignIn ? stats?.table_count : connection.table_count
  const toolCount = needsSignIn ? stats?.tool_count : connection.tool_count
  const fileCount = needsSignIn ? undefined : connection.file_count
  const counts = [
    { key: 'agentsPage.countTools', value: toolCount },
    { key: 'agentsPage.countFiles', value: fileCount ?? (connection.data_shape === 'files' ? catalogCount : undefined) },
    { key: connection.type === 'monday' ? 'agentCard.countBoards' : 'agentsPage.countTables', value: connection.data_shape === 'files' || connection.data_shape === 'tools' ? undefined : catalogCount },
  ].filter(c => typeof c.value === 'number' && Number.isFinite(c.value) && c.value > 0)
  return { connection, needsSignIn, status, signedIn, configured: configured && counts.length > 0, counts, signedInDate: signedInDate(connection) }
}))
const signIn = useConnectionSignIn()
const signingInId = ref<string | null>(null)
const showCredentials = ref(false)
const credentialSource = ref<any>(null)
async function signInConnection(connection: any) {
  if (signingInId.value) return
  signingInId.value = connection.id
  let redirecting = false
  try {
    const result = await signIn.triggerUserSignIn(connection, { returnTo: route.fullPath })
    redirecting = result.redirecting
    if (redirecting) return
    if (result.error) toast.add({ title: t('agentsPage.toastSignInFailed'), description: result.error, color: 'red' })
    credentialSource.value = { id: props.agent.id, type: connection.type, connection, connections: [connection] }
    showCredentials.value = true
  } catch (error: any) {
    toast.add({ title: t('agentsPage.toastSignInFailed'), description: error?.message || String(error), color: 'red' })
  } finally {
    if (!redirecting) signingInId.value = null
  }
}
function onCredentialsSaved() { showCredentials.value = false; emit('changed') }
</script>

<style scoped>
.connection-row { display: grid; grid-template-columns: minmax(0, 1fr) 104px 112px; grid-template-areas: "name count access"; align-items: center; gap: 12px; }
.connection-name { grid-area: name; }
.connection-count { grid-area: count; }
.connection-access { grid-area: access; }
@media (max-width: 639px) {
  .connection-row { grid-template-columns: minmax(0, 1fr) 104px; grid-template-areas: "name access" "count access"; row-gap: 5px; }
  .connection-count { padding-inline-start: 30px; }
}
</style>
