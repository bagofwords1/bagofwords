<template>
    <!-- The dashboard's notes (collab01): its README, handed to every
         editor's agent. Editable with the settings capability. -->
    <div class="space-y-3" data-testid="history-notes">
        <p class="text-xs text-gray-500 dark:text-gray-400 flex items-start gap-1.5">
            <Icon name="heroicons:sparkles" class="w-3.5 h-3.5 shrink-0 mt-px" />
            <span>{{ $t('dashboardCollab.history.notesHelp') }}</span>
        </p>

        <div v-if="loading" class="flex items-center gap-2 py-6 text-sm text-gray-400">
            <Spinner class="w-4 h-4" /> {{ $t('common.loading') }}
        </div>
        <div v-else-if="loadError" class="py-6 flex flex-col items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
            <span>{{ loadError }}</span>
            <UButton size="xs" color="gray" variant="soft" @click="load">{{ $t('common.retry') }}</UButton>
        </div>

        <template v-else-if="canEdit">
            <UTextarea v-model="draft" :rows="8" autoresize :maxrows="16" :maxlength="MAX"
                :placeholder="$t('dashboardCollab.history.notesPlaceholder')" :disabled="saving"
                data-testid="history-notes-input" />
            <div class="flex flex-wrap items-center justify-between gap-2">
                <span class="text-[11px] text-gray-400 tabular-nums">{{ $t('dashboardCollab.history.notesLength', { n: draft.length, max: MAX }) }}</span>
                <div class="flex gap-2 ms-auto">
                    <UButton v-if="dirty" size="xs" color="gray" variant="ghost" :disabled="saving" @click="draft = saved">
                        {{ $t('common.cancel') }}
                    </UButton>
                    <UButton size="xs" color="primary" :loading="saving" :disabled="!dirty" data-testid="history-notes-save" @click="save">
                        {{ $t('common.save') }}
                    </UButton>
                </div>
            </div>
        </template>

        <template v-else>
            <div v-if="saved" class="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap break-words rounded-lg bg-gray-50 dark:bg-gray-800 p-3"
                dir="auto" data-testid="history-notes-readonly">{{ saved }}</div>
            <HistoryEmpty v-else icon="heroicons:document-text" :text="$t('dashboardCollab.history.notesEmpty')" />
            <p class="text-[11px] text-gray-400 flex items-start gap-1.5">
                <Icon name="heroicons:lock-closed" class="w-3.5 h-3.5 shrink-0 mt-px" />
                <span>{{ $t('dashboardCollab.history.notesReadOnly') }}</span>
            </p>
        </template>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import HistoryEmpty from './HistoryEmpty.vue'

const props = defineProps<{ dashboardId: string; canEdit: boolean }>()

const MAX = 20000 // DashboardSettingsUpdate.notes max_length
const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const loading = ref(true)
const loadError = ref<string | null>(null)
const saving = ref(false)
const saved = ref('')
const draft = ref('')
const dirty = computed(() => draft.value !== saved.value)

async function load() {
    loading.value = true
    loadError.value = null
    try {
        const res = await useMyFetch(`/dashboards/${props.dashboardId}/settings`)
        if (res.error.value) throw res.error.value
        saved.value = (res.data.value as any)?.notes || ''
        draft.value = saved.value
    } catch (e) {
        loadError.value = getErrorMessage(e, t('dashboardCollab.history.notesLoadFailed'))
    } finally {
        loading.value = false
    }
}

async function save() {
    saving.value = true
    try {
        // "" clears the notes.
        const res = await useMyFetch(`/dashboards/${props.dashboardId}/settings`, { method: 'PUT', body: { notes: draft.value } })
        if (res.error.value) throw res.error.value
        saved.value = (res.data.value as any)?.notes || ''
        draft.value = saved.value
        toast.add({ title: t('dashboardCollab.history.notesSaved'), color: 'green' })
    } catch (e) {
        toast.add({ title: t('dashboardCollab.history.notesSaveFailed'), description: getErrorMessage(e), color: 'red' })
    } finally {
        saving.value = false
    }
}

watch(() => props.dashboardId, (id) => { if (id) load() }, { immediate: true })
</script>
