<template>
    <div class="py-6">
        <div v-if="injectedFetchError" />
        <div v-else>

            <!-- Connection digest (structured/table connections) -->
            <div v-if="tableConnections.length > 0" class="flex items-center gap-3 mb-3 flex-wrap">
                <div
                    v-for="conn in tableConnections.slice(0, 3)"
                    :key="conn.id"
                    class="inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400"
                >
                    <span :class="['w-1.5 h-1.5 rounded-full flex-shrink-0', statusDotClass(getEffectiveStatus(conn))]" />
                    <DataSourceIcon :type="conn.type" class="h-3.5" />
                    <span>{{ conn.name }}</span>
                </div>
                <span v-if="tableConnections.length > 3" class="text-xs text-gray-400">+{{ tableConnections.length - 3 }}</span>
                <button class="text-xs text-gray-400 hover:text-gray-600" @click="showManageModal = true">
                    {{ t('agentPage.tables.manageConnections') }}
                </button>
            </div>

            <AgentConnectionsModal v-model="showManageModal" />

            <div v-if="anyIndexing" class="mb-3 flex items-center gap-2 rounded-md border border-blue-200 bg-blue-50 dark:bg-blue-950 px-3 py-2 text-xs text-blue-800">
                <UIcon name="heroicons-arrow-path" class="w-4 h-4 animate-spin" />
                <span>{{ t('agentPage.tables.schemaRefreshing') }}</span>
            </div>

            <div v-if="needsSignIn" class="border border-gray-200 dark:border-gray-700 rounded-lg p-10 text-center bg-gray-50 dark:bg-gray-900">
                <DataSourceIcon :type="pendingSignInConn?.type" class="h-10 mx-auto mb-3" />
                <h3 class="text-base font-semibold text-gray-900 dark:text-white">Sign in to access your tables</h3>
                <p class="text-sm text-gray-600 dark:text-gray-400 mt-1">
                    {{ pendingSignInConn?.name || 'This connection' }} uses per-user authentication — your tables will load after you sign in with {{ signInProviderName }}.
                </p>
                <UButton size="sm" color="blue" class="mt-4" @click="startSignIn">Sign in with {{ signInProviderName }}</UButton>
            </div>

            <div v-else-if="tableConnections.length === 0" class="border border-dashed border-gray-200 dark:border-gray-700 rounded-lg p-10 text-center text-sm text-gray-500 dark:text-gray-400">
                No database connections on this agent. File sources live in the <NuxtLink :to="`/old_agents/${id}/files`" class="text-blue-600 hover:underline">Files</NuxtLink> tab.
            </div>

            <div v-else-if="!registryLoaded" class="py-10 text-center text-gray-400 text-sm">Loading…</div>

            <div v-else class="border border-gray-200 dark:border-gray-700 rounded-lg p-6">
                <TablesSelector :ds-id="id" :schema="schemaMode" :connection-filter="tableConnectionIds"
                    :can-update="canUpdateDataSource" :show-refresh="true" :show-save="canUpdateDataSource"
                    :show-header="true" :header-title="'Select tables'" :header-subtitle="'Choose which tables to enable'"
                    :save-label="t('agentPage.tables.save')" :show-stats="true" @saved="onSaved" />
            </div>

            <UserDataSourceCredentialsModal v-model="showCredsModal" :data-source="selectedConnForSignIn" />
        </div>
    </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'data' })
import TablesSelector from '@/components/datasources/TablesSelector.vue'
const { t } = useI18n()
import AgentConnectionsModal from '~/components/AgentConnectionsModal.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'
import { hasAnyActiveIndexing, getEffectiveStatus, statusDotClass } from '~/composables/useConnectionStatus'
import type { Ref } from 'vue'

const toast = useToast()
const route = useRoute()
const id = computed(() => String(route.params.id || ''))

const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))

const showManageModal = ref(false)
const schemaMode = ref<'full' | 'user'>('full')

const connections = computed(() => injectedIntegration.value?.connections || [])
const anyIndexing = computed(() => hasAnyActiveIndexing(injectedIntegration.value?.connections))

// Registry → data_shape, so we can restrict this tab to structured (table /
// object) connections. File connections live in the Files tab.
const registryByType = ref<Record<string, any>>({})
const registryLoaded = ref(false)
onMounted(async () => {
  try {
    const { data } = await useMyFetch('/available_data_sources', { method: 'GET' })
    for (const entry of (data.value as any[]) || []) registryByType.value[entry.type] = entry
  } catch {} finally { registryLoaded.value = true }
})
// Structured connections only (SQL/object). Excludes file + tool shapes so the
// grid never shows file rows. Gated on registryLoaded so shapes are known.
const tableConnections = computed(() => (connections.value as any[]).filter((c: any) => {
  const shape = registryByType.value[c.type]?.data_shape
  return shape === 'tables' || shape === 'objects' || (registryLoaded.value && !shape)
}))
const tableConnectionIds = computed(() => tableConnections.value.map((c: any) => String(c.id)).join(','))

const pendingSignInConn = computed(() => {
  for (const conn of tableConnections.value as any[]) {
    if (conn.auth_policy !== 'user_required') continue
    if (conn.user_status?.has_user_credentials) continue
    if (conn.user_status?.effective_auth === 'system') continue
    return conn
  }
  return null
})
const needsSignIn = computed(() => !!pendingSignInConn.value)
const signInProviderName = computed(() => {
  const ty = pendingSignInConn.value?.type
  if (ty === 'onedrive' || ty === 'sharepoint') return 'Microsoft'
  if (ty === 'google_drive' || ty === 'bigquery') return 'Google'
  if (ty === 'powerbi') return 'Power BI'
  if (ty === 'ms_fabric') return 'Microsoft Fabric'
  return 'the provider'
})

const showCredsModal = ref(false)
const selectedConnForSignIn = ref<any>(null)
const signIn = useConnectionSignIn()
async function startSignIn() {
  if (!pendingSignInConn.value) return
  const result = await signIn.triggerUserSignIn(pendingSignInConn.value)
  if (result.redirecting) return
  if (result.error) toast.add({ title: 'Sign-in failed to start', description: result.error, color: 'red' })
  selectedConnForSignIn.value = { name: pendingSignInConn.value.name, type: pendingSignInConn.value.type, connection: pendingSignInConn.value }
  showCredsModal.value = true
}

const permissionsLoaded = usePermissionsLoaded()
const canUpdateDataSource = computed(() => useCan('manage', { type: 'data_source', id: id.value }))

watch([injectedIntegration, permissionsLoaded], ([ds, loaded]) => {
  if (ds && loaded) schemaMode.value = canUpdateDataSource.value ? 'full' : 'user'
}, { immediate: true })

function onSaved() { toast.add({ title: 'Saved', description: 'Schema updated', color: 'green' }) }
</script>
