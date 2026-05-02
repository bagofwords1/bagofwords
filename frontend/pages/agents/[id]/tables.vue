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

            <!-- Files digest (auto-attached to new reports for this agent) -->
            <div class="flex items-center gap-3 mb-3 flex-wrap">
                <div
                    v-for="file in files.slice(0, 3)"
                    :key="file.id"
                    class="inline-flex items-center gap-1.5 text-xs text-gray-600 group"
                >
                    <UIcon name="i-heroicons-paper-clip" class="w-3 h-3 flex-shrink-0 text-gray-400" />
                    <UTooltip :text="file.filename">
                        <span class="truncate max-w-[160px]">{{ file.filename }}</span>
                    </UTooltip>
                    <button
                        v-if="canUpdateDataSource"
                        type="button"
                        class="text-gray-300 hover:text-gray-600 transition-colors opacity-0 group-hover:opacity-100"
                        :title="t('agentPage.tables.removeFile')"
                        @click="removeFile(file)"
                    >
                        <UIcon name="i-heroicons-x-mark" class="w-3 h-3" />
                    </button>
                </div>
                <span v-if="files.length > 3" class="text-xs text-gray-400">
                    +{{ files.length - 3 }}
                </span>
                <input
                    ref="fileInput"
                    type="file"
                    class="hidden"
                    multiple
                    @change="onFileInput"
                />
                <button
                    v-if="canUpdateDataSource"
                    class="text-xs text-gray-400 hover:text-gray-600 transition-colors disabled:opacity-50"
                    :disabled="uploading"
                    @click="triggerUpload"
                >
                    {{ uploading ? t('agentPage.tables.filesUploading') : (files.length === 0 ? t('agentPage.tables.addFiles') : t('agentPage.tables.manageFiles')) }}
                </button>
            </div>

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

// Files attached to this agent. Auto-snapshotted into reports created
// against this data source by the backend (see ReportService.create_report).
type AgentFile = { id: string; filename: string; content_type?: string }
const files = ref<AgentFile[]>([])
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

async function loadFiles() {
    if (!id.value) return
    try {
        const { data } = await useMyFetch(`/data_sources/${id.value}/files`, { method: 'GET' })
        files.value = (data.value as AgentFile[]) || []
    } catch (e) {
        console.error('Failed to load agent files', e)
    }
}

function triggerUpload() {
    fileInput.value?.click()
}

async function onFileInput(e: Event) {
    const input = e.target as HTMLInputElement
    const list = input.files
    if (!list || list.length === 0) return
    uploading.value = true
    try {
        for (const file of Array.from(list)) {
            const formData = new FormData()
            formData.append('file', file)
            const { data, error } = await useMyFetch(`/data_sources/${id.value}/files`, {
                method: 'POST',
                body: formData,
            })
            if (error.value || !data.value) {
                toast.add({ title: t('agentPage.tables.fileUploadFailed'), description: file.name, color: 'red' })
                continue
            }
            files.value.push(data.value as AgentFile)
        }
    } finally {
        uploading.value = false
        if (input) input.value = ''
    }
}

async function removeFile(file: AgentFile) {
    try {
        await useMyFetch(`/data_sources/${id.value}/files/${file.id}`, { method: 'DELETE' })
        files.value = files.value.filter(f => f.id !== file.id)
    } catch (e) {
        console.error('Failed to remove file', e)
        toast.add({ title: t('agentPage.tables.fileRemoveFailed'), color: 'red' })
    }
}

watch(id, () => { loadFiles() }, { immediate: true })

// Set schema mode based on permissions - wait for permissions to load
watch([injectedIntegration, permissionsLoaded], ([ds, loaded]) => {
    if (ds && loaded) {
        schemaMode.value = canUpdateDataSource.value ? 'full' : 'user'
    }
}, { immediate: true })

function onSaved() { toast.add({ title: 'Saved', description: 'Schema updated', color: 'green' }) }
</script>


