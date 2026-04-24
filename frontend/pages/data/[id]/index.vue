<template>
    <div class="py-6 relative">
        <!-- Hide content when there's a fetch error (layout shows error state) -->
        <div v-if="fetchError" />
        <div v-else>
            <!-- Indexing banner: shown while any linked connection is discovering schema -->
            <div
                v-if="indexingConnections.length > 0"
                class="mb-4 flex items-start gap-3 bg-blue-50 border border-blue-200 text-blue-800 rounded-lg px-4 py-3"
            >
                <UIcon name="heroicons-arrow-path" class="w-5 h-5 mt-0.5 animate-spin flex-none" />
                <div class="flex-1 text-sm">
                    <div class="font-medium">
                        Discovering schema for
                        {{ indexingConnections.length }}
                        {{ indexingConnections.length === 1 ? 'connection' : 'connections' }}…
                    </div>
                    <div class="mt-1 text-xs text-blue-700 space-y-0.5">
                        <div v-for="conn in indexingConnections" :key="conn.id">
                            <span class="font-medium">{{ conn.name }}</span>
                            <span class="ms-1 text-blue-600">· {{ connIndexingSummary(conn) }}</span>
                        </div>
                    </div>
                </div>
                <NuxtLink :to="`/data/${route.params.id}/connection`" class="text-xs font-medium underline">
                    View progress
                </NuxtLink>
            </div>

        <div class="bg-white border border-gray-200 rounded-lg p-8 md:p-10">
            <div v-if="loading" class="text-xs text-gray-500 text-center">{{ $t('common.loading') }}</div>
            <div v-else class="md:w-2/3 ">
                <div class="flex items-center gap-2">
                    <div class="text-xs uppercase tracking-wide text-gray-400">{{ $t('dataSource.description') }}</div>
                    <button v-if="useCan('update_data_source')" @click="openEditDescription" class="text-[10px] text-blue-600 hover:underline">{{ $t('dataSource.edit') }}</button>
                </div>
                <div class="mt-3 markdown-wrapper text-sm leading-relaxed text-start text-gray-600" v-if="computedDescription">
                    <MDC :value="computedDescription" class="markdown-content" />
                </div>
                <div class="mt-8">
                    <div class="flex items-center gap-2">
                        <div class="text-xs uppercase tracking-wide text-gray-400">{{ $t('dataSource.conversationStarters') }}</div>
                        <button v-if="useCan('update_data_source')" @click="openEditStarters" class="text-[10px] text-blue-600 hover:underline">{{ $t('dataSource.edit') }}</button>
                    </div>
                    <div class="mt-3 flex flex-wrap gap-2">
                        <div v-for="starter in displayDataSource?.conversation_starters" :key="starter"
                        class="bg-gray-100 rounded-lg px-3 py-2 text-xs"
                        >
                            <span>{{ starter.split('\n')[0] }}</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        </div>

        <UModal v-model="showEditModal" :ui="{ width: 'sm:max-w-2xl' }">
            <div class="p-5">
                <div class="text-sm font-medium text-gray-900">{{ $t('dataSource.editStartersTitle') }}</div>
                <div class="text-xs text-gray-600 mt-1">{{ $t('dataSource.editStartersHint') }}</div>

                <div class="mt-4 space-y-2 max-h-[60vh] overflow-auto pe-1">
                    <div v-for="(item, idx) in editStarters" :key="idx" class="rounded-md border border-gray-100 p-2">
                        <div class="flex items-center justify-between mb-1">
                            <span class="text-[10px] uppercase tracking-wide text-gray-400">{{ $t('dataSource.starterN', { n: idx + 1 }) }}</span>
                            <button @click="removeStarter(idx)" class="text-[11px] text-gray-500 hover:text-red-600">{{ $t('dataSource.remove') }}</button>
                        </div>
                        <div class="space-y-1">
                            <div>
                                <label class="block text-[11px] text-gray-500 mb-0.5">{{ $t('dataSource.starterTitle') }}</label>
                                <input v-model="item.title" type="text" class="w-full h-8 text-sm border border-gray-200 rounded-md px-2 focus:outline-none focus:ring-2 focus:ring-blue-200" :placeholder="$t('dataSource.starterTitlePlaceholder')" />
                            </div>
                            <div>
                                <label class="block text-[11px] text-gray-500 mb-0.5">{{ $t('dataSource.starterPrompt') }}</label>
                                <textarea v-model="item.prompt" rows="2" class="w-full text-sm border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-200" :placeholder="$t('dataSource.starterPromptPlaceholder')"></textarea>
                            </div>
                        </div>
                    </div>
                    <div>
                        <button @click="addStarter" class="text-xs border border-gray-300 text-gray-700 rounded-lg px-2 py-1 hover:bg-gray-50">{{ $t('dataSource.addStarter') }}</button>
                    </div>
                </div>

                <div class="flex justify-end gap-2 mt-4">
                    <button @click="onCancelEdit" class="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg">{{ $t('dataSource.cancel') }}</button>
                    <button @click="onSaveStarters" :disabled="savingStarters" class="px-3 py-1.5 text-xs border border-blue-300 text-blue-700 rounded-lg hover:bg-blue-50">{{ savingStarters ? $t('dataSource.saving') : $t('dataSource.save') }}</button>
                </div>
            </div>
        </UModal>

        <UModal v-model="showDescModal" :ui="{ width: 'sm:max-w-xl' }">
            <div class="p-5">
                <div class="text-sm font-medium text-gray-900">{{ $t('dataSource.editDescTitle') }}</div>
                <div class="text-xs text-gray-600 mt-1">{{ $t('dataSource.editDescHint') }}</div>

                <div class="mt-3">
                    <label class="block text-[11px] text-gray-500 mb-0.5">{{ $t('dataSource.description') }}</label>
                    <textarea v-model="descForm" rows="6" class="w-full text-sm border border-gray-200 rounded-md px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-200" :placeholder="$t('dataSource.descPlaceholder')"></textarea>
                </div>

                <div class="flex justify-end gap-2 mt-4">
                    <button @click="onCancelDesc" class="px-3 py-1.5 text-xs border border-gray-300 text-gray-700 rounded-lg">{{ $t('dataSource.cancel') }}</button>
                    <button @click="onSaveDesc" :disabled="savingDesc" class="px-3 py-1.5 text-xs border border-blue-300 text-blue-700 rounded-lg hover:bg-blue-50">{{ savingDesc ? $t('dataSource.saving') : $t('dataSource.save') }}</button>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, inject, watch } from 'vue'
import { useCan } from '~/composables/usePermissions'
import DataSourceQuestionsHome from '~/components/DataSourceQuestionsHome.vue'
import { isIndexingActive, indexingSummary } from '~/composables/useConnectionStatus'
import type { Ref } from 'vue'

definePageMeta({ auth: true, layout: 'data' })

const { t } = useI18n()
const route = useRoute()
const toast = useToast?.()

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})
const injectedLoading = inject<Ref<boolean>>('isLoading', ref(true))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))

// Use injected data as main data source
const dataSource = injectedIntegration
const loading = injectedLoading
const fetchError = injectedFetchError

const availableMeta = ref<any | null>(null)
const showEditModal = ref(false)
const editStarters = ref<{ title: string; prompt: string }[]>([])
const savingStarters = ref(false)
const showDescModal = ref(false)
const descForm = ref('')
const savingDesc = ref(false)

const computedDescription = computed(() => {
    return dataSource.value?.description || availableMeta.value?.description || ''
})

const indexingConnections = computed(() =>
    (dataSource.value?.connections || []).filter((c: any) => isIndexingActive(c?.indexing))
)

function connIndexingSummary(conn: any) {
    return indexingSummary(conn?.indexing)
}

const displayDataSource = computed(() => {
    if (!dataSource.value) return null
    const starters = (dataSource.value?.conversation_starters && dataSource.value.conversation_starters.length > 0)
        ? dataSource.value.conversation_starters
        : (availableMeta.value?.conversation_starters || [])
    return {
        ...dataSource.value,
        conversation_starters: starters,
    }
})

async function loadAvailableMeta() {
    // Fallback to catalog description by type, if needed
    try {
        const { data: avail, error: availErr } = await useMyFetch('/available_data_sources', { method: 'GET' })
        if (!availErr?.value && Array.isArray(avail.value)) {
            const byType = (avail.value as any[]).find((x: any) => x.type === dataSource.value?.type)
            availableMeta.value = byType || null
        }
    } catch {}
}

// Load available meta when dataSource is ready
watch(() => dataSource.value?.type, (type) => {
    if (type) loadAvailableMeta()
}, { immediate: true })

function openEditStarters() {
    const starters = (dataSource.value?.conversation_starters && dataSource.value.conversation_starters.length > 0)
        ? dataSource.value.conversation_starters
        : (availableMeta.value?.conversation_starters || [])
    editStarters.value = (starters || []).map((s: string) => {
        const parts = String(s).split('\n')
        const title = (parts[0] || '').trim()
        const prompt = parts.slice(1).join('\n').trim()
        return { title, prompt }
    })
    if (editStarters.value.length === 0) editStarters.value = [{ title: '', prompt: '' }]
    showEditModal.value = true
}

function addStarter() {
    editStarters.value.push({ title: '', prompt: '' })
}

function removeStarter(index: number) {
    editStarters.value.splice(index, 1)
}

async function onSaveStarters() {
    if (savingStarters.value) return
    savingStarters.value = true
    const id = route.params.id as string
    const conversation_starters = editStarters.value
        .map(s => `${(s.title || '').trim()}${s.prompt?.trim() ? `\n${s.prompt.trim()}` : ''}`)
        .filter(s => s.trim().length > 0)
    const { error } = await useMyFetch(`/data_sources/${id}`, {
        method: 'PUT',
        body: { conversation_starters },
    })
    savingStarters.value = false
    if (!error?.value) {
        // Refresh from layout
        await injectedFetchIntegration()
        showEditModal.value = false
        toast?.add?.({ title: t('dataSource.savedTitle'), description: t('dataSource.startersUpdated') })
    } else {
        toast?.add?.({ title: t('dataSource.errorTitle'), description: String(error.value), color: 'red' })
    }
}

function onCancelEdit() {
    showEditModal.value = false
}

function openEditDescription() {
    descForm.value = dataSource.value?.description || ''
    showDescModal.value = true
}

async function onSaveDesc() {
    if (savingDesc.value) return
    savingDesc.value = true
    const id = route.params.id as string
    const payload = { description: (descForm.value || '').trim() }
    const { error } = await useMyFetch(`/data_sources/${id}`, { method: 'PUT', body: payload })
    savingDesc.value = false
    if (!error?.value) {
        // Refresh from layout
        await injectedFetchIntegration()
        showDescModal.value = false
        toast?.add?.({ title: t('dataSource.savedTitle'), description: t('dataSource.descUpdated') })
    } else {
        toast?.add?.({ title: t('dataSource.errorTitle'), description: String(error.value), color: 'red' })
    }
}

function onCancelDesc() {
    showDescModal.value = false
}
</script>

<style scoped>
.markdown-wrapper :deep(.markdown-content) {
	@apply leading-relaxed;
	font-size: 14px;

	:where(h1, h2, h3, h4, h5, h6) {
		@apply font-bold mb-4 mt-6;
	}

	h1 { @apply text-3xl; }
	h2 { @apply text-2xl; }
	h3 { @apply text-xl; }

	ul, ol { @apply ps-6 mb-4; }
	ul { @apply list-disc; }
	ol { @apply list-decimal; }
	li { @apply mb-1.5; }

	pre { @apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto; }
	code { @apply bg-gray-50 px-1 py-0.5 rounded text-sm font-mono; }
	a { @apply text-blue-600 hover:text-blue-800 underline; }
	blockquote { @apply border-l-4 border-gray-200 pl-4 italic my-4; }
	table { @apply w-full border-collapse mb-4; }
	table th, table td { @apply border border-gray-200 p-2 text-xs bg-white; }
}
</style>


