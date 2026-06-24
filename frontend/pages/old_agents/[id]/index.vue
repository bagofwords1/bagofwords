<template>
    <div class="py-6 relative">
        <!-- Hide content when there's a fetch error (layout shows error state) -->
        <div v-if="fetchError" />
        <div v-else>
            <!-- Indexing banner: shown while any linked connection is discovering schema -->
            <div
                v-if="indexingConnections.length > 0"
                class="mb-4 flex items-start gap-3 bg-blue-50 dark:bg-blue-950 border border-blue-200 text-blue-800 rounded-lg px-4 py-3"
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
                <NuxtLink :to="`/old_agents/${route.params.id}/connection`" class="text-xs font-medium underline">
                    View progress
                </NuxtLink>
            </div>

        <div>
            <div v-if="loading" class="text-xs text-gray-500 dark:text-gray-400 text-center">{{ $t('common.loading') }}</div>
            <div v-else class="md:w-2/3 space-y-6">

                <!-- Primary Instruction -->
                <div>
                    <!-- Inline create form -->
                    <div
                        v-if="creatingInstruction"
                        class="primary-instruction-editor flex flex-col border border-gray-200 dark:border-gray-700 rounded-xl overflow-hidden bg-white dark:bg-gray-900"
                        style="height: min(600px, 70vh)"
                    >
                        <InstructionGlobalCreateComponent
                            default-status="published"
                            :agent-id="(route.params.id as string)"
                            :initial-title="primaryInstructionDefaultTitle"
                            :uppercase-title="false"
                            @instruction-saved="onPrimaryInstructionCreated"
                            @cancel="creatingInstruction = false"
                        />
                    </div>

                    <!-- Inline edit form -->
                    <div
                        v-else-if="editingInstruction && dataSource?.primary_instruction"
                        class="primary-instruction-editor flex flex-col border border-gray-200 dark:border-gray-700 rounded-xl overflow-hidden bg-white dark:bg-gray-900"
                        style="height: min(600px, 70vh)"
                    >
                        <InstructionGlobalCreateComponent
                            :key="dataSource.primary_instruction.id"
                            :instruction="dataSource.primary_instruction"
                            :uppercase-title="false"
                            :start-in-edit-mode="true"
                            @instruction-saved="onPrimaryInstructionSaved"
                            @cancel="editingInstruction = false"
                        />
                    </div>

                    <!-- Existing instruction: simple read-only view -->
                    <template v-else-if="dataSource?.primary_instruction">
                        <div class="flex items-center justify-between gap-2 mb-2">
                            <span class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ dataSource.primary_instruction.title || 'Primary instruction' }}</span>
                            <PrimaryInstructionMenu
                                v-if="useCan('update_data_source')"
                                :agent-id="(route.params.id as string)"
                                :current-instruction-id="dataSource.primary_instruction.id"
                                :can-train="canStartTraining"
                                @edit="editingInstruction = true"
                                @select="onSelectExistingInstruction"
                                @start-training="startTrainingSession"
                            />
                        </div>
                        <InstructionText
                            :text="dataSource.primary_instruction.text"
                            :references="dataSource.primary_instruction.references || []"
                            :prose="true"
                            :markdown="true"
                        />
                    </template>

                    <!-- Empty state -->
                    <div v-else class="border border-dashed border-gray-200 rounded-xl px-6 py-10 text-center bg-gray-50/40">
                        <div class="mx-auto w-10 h-10 rounded-full bg-blue-50 flex items-center justify-center mb-3">
                            <UIcon name="heroicons-document-text" class="w-5 h-5 text-blue-500" />
                        </div>
                        <div class="text-sm font-medium text-gray-800">No primary instruction</div>
                        <div class="text-xs text-gray-500 mt-1 max-w-md mx-auto">
                            Give this agent a guiding instruction it applies to every report — context about the data, conventions to follow, or rules to enforce.
                        </div>
                        <div v-if="useCan('update_data_source')" class="mt-4 flex items-center justify-center gap-3">
                            <button
                                @click="creatingInstruction = true"
                                class="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
                            >
                                <UIcon name="heroicons-plus" class="w-3.5 h-3.5" />
                                Add Primary Instruction
                            </button>
                            <span class="text-xs text-gray-400">or</span>
                            <PrimaryInstructionPicker
                                :agent-id="(route.params.id as string)"
                                label="select existing"
                                @select="onSelectExistingInstruction"
                            />
                        </div>
                        <div v-if="useCan('update_data_source') && canStartTraining" class="mt-3">
                            <button @click="startTrainingSession" class="text-xs text-sky-600 hover:underline inline-flex items-center gap-1">
                                <UIcon name="heroicons-academic-cap" class="w-3.5 h-3.5" />
                                Start a training session
                            </button>
                        </div>
                    </div>
                </div>

                <!-- Conversation Starters -->
                <div>
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

    </div>
</template>

<script setup lang="ts">
import { ref, computed, inject, watch } from 'vue'
import { useCan } from '~/composables/usePermissions'
import { isIndexingActive, indexingSummary } from '~/composables/useConnectionStatus'
import InstructionGlobalCreateComponent from '~/components/InstructionGlobalCreateComponent.vue'
import InstructionText from '~/components/instructions/InstructionText.vue'
import PrimaryInstructionPicker from '~/components/instructions/PrimaryInstructionPicker.vue'
import PrimaryInstructionMenu from '~/components/instructions/PrimaryInstructionMenu.vue'
import { useOrgSettings } from '~/composables/useOrgSettings'
import type { Ref } from 'vue'

definePageMeta({ auth: true, layout: 'data' })

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const toast = useToast?.()

// Training mode is gated by org setting + permission (mirrors the prompt box).
const { isTrainingModeEnabled } = useOrgSettings()
const canStartTraining = computed(() => useCan('train_mode') && isTrainingModeEnabled.value)

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchIntegration = inject<() => Promise<void>>('fetchIntegration', async () => {})
const injectedLoading = inject<Ref<boolean>>('isLoading', ref(true))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))

const dataSource = injectedIntegration
const loading = injectedLoading
const fetchError = injectedFetchError

const availableMeta = ref<any | null>(null)
const showEditModal = ref(false)
const editStarters = ref<{ title: string; prompt: string }[]>([])
const savingStarters = ref(false)

// Primary instruction: read-only display by default; create + edit use InstructionGlobalCreateComponent inline.
const creatingInstruction = ref(false)
const editingInstruction = ref(false)
const primaryInstructionDefaultTitle = computed(() => {
    const name = (dataSource.value?.name || '').trim()
    return name ? `${name} - Main` : 'Main'
})

async function onPrimaryInstructionCreated(saved: any) {
    const id = route.params.id as string
    const newId = saved?.id
    try {
        if (newId) {
            const { error } = await useMyFetch(`/data_sources/${id}`, {
                method: 'PUT',
                body: { primary_instruction_id: newId },
            })
            if (error?.value) throw new Error(String(error.value))
        }
        creatingInstruction.value = false
        await injectedFetchIntegration()
        toast?.add?.({ title: 'Saved', description: 'Primary instruction created.' })
    } catch (e: any) {
        toast?.add?.({ title: 'Error', description: String(e?.message || e), color: 'red' })
    }
}

async function onPrimaryInstructionSaved(_saved: any) {
    editingInstruction.value = false
    await injectedFetchIntegration()
}

async function startTrainingSession() {
    const agentId = route.params.id as string
    // Partial prompt the admin completes — pre-filled, not auto-submitted.
    const prompt = 'I need to update the instruction for this agent with '
    try {
        // 1. New report scoped to this agent.
        const { data, error } = await useMyFetch<any>('/reports', {
            method: 'POST',
            body: { title: 'Training session', data_sources: [agentId] },
        })
        const reportId = (data?.value as any)?.id
        if (error?.value || !reportId) throw new Error(error?.value ? String(error.value) : 'Failed to create report')
        // 2. Put it in training mode.
        const { error: modeErr } = await useMyFetch(`/reports/${reportId}`, {
            method: 'PUT',
            body: { mode: 'training' },
        })
        if (modeErr?.value) throw new Error(String(modeErr.value))
        // 3. Land on the report with the prompt pre-filled (non-submitting).
        router.push({ path: `/reports/${reportId}`, query: { prompt } })
    } catch (e: any) {
        toast?.add?.({ title: 'Error', description: String(e?.message || e), color: 'red' })
    }
}

async function onSelectExistingInstruction(instruction: any) {
    const id = route.params.id as string
    const newId = instruction?.id
    if (!newId) return
    try {
        const { error } = await useMyFetch(`/data_sources/${id}`, {
            method: 'PUT',
            body: { primary_instruction_id: newId },
        })
        if (error?.value) throw new Error(String(error.value))
        editingInstruction.value = false
        creatingInstruction.value = false
        await injectedFetchIntegration()
        toast?.add?.({ title: 'Saved', description: 'Primary instruction updated.' })
    } catch (e: any) {
        toast?.add?.({ title: 'Error', description: String(e?.message || e), color: 'red' })
    }
}

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
    try {
        const { data: avail, error: availErr } = await useMyFetch('/available_data_sources', { method: 'GET' })
        if (!availErr?.value && Array.isArray(avail.value)) {
            const byType = (avail.value as any[]).find((x: any) => x.type === dataSource.value?.type)
            availableMeta.value = byType || null
        }
    } catch {}
}

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
</script>

<style scoped>
.primary-instruction-editor :deep(.wysiwyg-content .tiptap-prose),
.primary-instruction-editor :deep(.raw-textarea) {
    font-size: 13px;
}

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
	li > p:only-child,
	li > p:last-child { margin-bottom: 0; }

	pre { @apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto; }
	code { @apply bg-gray-50 px-1 py-0.5 rounded text-sm font-mono; }
	a { @apply text-blue-600 hover:text-blue-800 underline; }
	blockquote { @apply border-l-4 border-gray-200 pl-4 italic my-4; }
	table { @apply w-full border-collapse mb-4; }
	table th, table td { @apply border border-gray-200 p-2 text-xs bg-white; }
}
</style>
