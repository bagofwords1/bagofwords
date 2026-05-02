<template>
    <div class="py-6">
        <!-- Hide content when there's a fetch error (layout shows error state) -->
        <div v-if="injectedFetchError" />
        <div v-else>

            <!-- Connection digest -->
            <div v-if="connections.length > 0" class="flex items-center gap-3 mb-3 flex-wrap">
                <div
                    v-for="conn in connections.slice(0, 3)"
                    :key="conn.id"
                    class="inline-flex items-center gap-1.5 text-xs text-gray-600"
                >
                    <span :class="['w-1.5 h-1.5 rounded-full flex-shrink-0', statusDotClass(getEffectiveStatus(conn))]" />
                    <DataSourceIcon :type="conn.type" class="h-3.5" />
                    <span>{{ conn.name }}</span>
                </div>
                <span v-if="connections.length > 3" class="text-xs text-gray-400">
                    +{{ connections.length - 3 }}
                </span>
                <button
                    class="text-xs text-gray-400 hover:text-gray-600 transition-colors"
                    @click="showManageModal = true"
                >
                    {{ t('agentPage.tables.manageConnections') }}
                </button>
            </div>

            <AgentConnectionsModal v-model="showManageModal" />

            <!-- Schema indexing in progress -->
            <div
                v-if="anyIndexing"
                class="mb-3 flex items-center gap-2 rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs text-blue-800"
            >
                <UIcon name="heroicons-arrow-path" class="w-4 h-4 animate-spin" />
                <span>{{ t('agentPage.tables.schemaRefreshing') }}</span>
            </div>

            <div class="border border-gray-200 rounded-lg p-6">
                <TablesSelector :ds-id="id" :schema="schemaMode" :can-update="canUpdateDataSource" :show-refresh="true" :show-save="canUpdateDataSource" :show-header="true" :header-title="t('agentPage.tables.selectTables')" :header-subtitle="t('agentPage.tables.selectTablesSubtitle')" :save-label="t('agentPage.tables.save')" :show-stats="true" @saved="onSaved" />
            </div>
        </div>
    </div>

</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'data' })
import TablesSelector from '@/components/datasources/TablesSelector.vue'
const { t } = useI18n()
import AgentConnectionsModal from '~/components/AgentConnectionsModal.vue'
import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'
import { hasAnyActiveIndexing, getEffectiveStatus, statusDotClass } from '~/composables/useConnectionStatus'
import type { Ref } from 'vue'

const toast = useToast()
const route = useRoute()
const id = computed(() => String(route.params.id || ''))

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))

const showManageModal = ref(false)

const loading = ref(false)
const schemaMode = ref<'full' | 'user'>('full')

const connections = computed(() => injectedIntegration.value?.connections || [])
const anyIndexing = computed(() => hasAnyActiveIndexing(injectedIntegration.value?.connections))

const permissionsLoaded = usePermissionsLoaded()
const canUpdateDataSource = computed(() => useCan('update_data_source'))

// Tables state is managed by TablesSelector component

// Set schema mode based on permissions - wait for permissions to load
watch([injectedIntegration, permissionsLoaded], ([ds, loaded]) => {
    if (ds && loaded) {
        schemaMode.value = canUpdateDataSource.value ? 'full' : 'user'
    }
}, { immediate: true })

function onSaved() { toast.add({ title: 'Saved', description: 'Schema updated', color: 'green' }) }
</script>


