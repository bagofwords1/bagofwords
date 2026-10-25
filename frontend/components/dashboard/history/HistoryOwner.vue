<template>
    <!-- Hand the dashboard to one of its editors (collab01): the owner or an
         org admin. The previous owner stays on as an editor. -->
    <div class="space-y-4" data-testid="history-owner">
        <div v-if="loading" class="flex items-center gap-2 py-6 text-sm text-gray-400">
            <Spinner class="w-4 h-4" /> {{ $t('common.loading') }}
        </div>
        <div v-else-if="loadError" class="py-6 flex flex-col items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
            <span>{{ loadError }}</span>
            <UButton size="xs" color="gray" variant="soft" @click="load">{{ $t('common.retry') }}</UButton>
        </div>
        <template v-else>
            <div>
                <label class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1.5 block">{{ $t('dashboardCollab.history.ownerCurrent') }}</label>
                <div class="flex items-center gap-2.5" data-testid="history-owner-current">
                    <div class="w-7 h-7 shrink-0 bg-gray-100 dark:bg-gray-800 rounded-full flex items-center justify-center text-xs font-medium text-gray-600 dark:text-gray-400">
                        {{ initial(owner) }}
                    </div>
                    <div class="flex flex-col min-w-0">
                        <span class="text-sm text-gray-800 dark:text-gray-200 truncate">{{ owner?.name || owner?.email || $t('dashboardCollab.share.unknownPerson') }}</span>
                        <span v-if="owner?.name && owner?.email" class="text-xs text-gray-400 truncate">{{ owner.email }}</span>
                    </div>
                </div>
            </div>

            <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('dashboardCollab.history.ownerHelp') }}</p>

            <HistoryEmpty v-if="!candidates.length" icon="heroicons:user-plus" :text="$t('dashboardCollab.history.ownerNoEditors')" />
            <template v-else>
                <div>
                    <label class="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-1.5 block">{{ $t('dashboardCollab.history.ownerPick') }}</label>
                    <USelectMenu v-model="picked" :options="candidates" value-attribute="id" option-attribute="label"
                        searchable :searchable-placeholder="$t('common.search')" :placeholder="$t('dashboardCollab.history.ownerPickPlaceholder')"
                        :disabled="busy" size="sm" data-testid="history-owner-pick" @update:model-value="confirming = false">
                        <template #option="{ option }">
                            <div class="flex flex-col min-w-0">
                                <span class="text-sm truncate">{{ option.label }}</span>
                                <span v-if="option.name && option.email" class="text-xs text-gray-400 truncate">{{ option.email }}</span>
                            </div>
                        </template>
                    </USelectMenu>
                </div>

                <div v-if="confirming && pickedPerson" class="p-3 rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/40 space-y-2"
                    data-testid="history-transfer-confirm">
                    <p class="text-sm text-gray-800 dark:text-gray-200">{{ $t('dashboardCollab.history.transferConfirm', { name: pickedPerson.label }) }}</p>
                    <div class="flex flex-wrap justify-end gap-2">
                        <UButton size="xs" color="gray" variant="ghost" :disabled="busy" @click="confirming = false">{{ $t('common.cancel') }}</UButton>
                        <UButton size="xs" color="amber" icon="i-heroicons-arrows-right-left" :loading="busy" data-testid="history-transfer-go" @click="transfer">
                            {{ $t('dashboardCollab.history.transfer') }}
                        </UButton>
                    </div>
                </div>
                <div v-else class="flex justify-end">
                    <UButton size="xs" color="gray" variant="soft" icon="i-heroicons-arrows-right-left" :disabled="!picked || busy"
                        data-testid="history-transfer" @click="confirming = true">
                        {{ $t('dashboardCollab.history.transfer') }}
                    </UButton>
                </div>
            </template>
        </template>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import HistoryEmpty from './HistoryEmpty.vue'

const props = defineProps<{ dashboardId: string }>()
const emit = defineEmits<{ (e: 'transferred'): void }>()

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

type Member = { id: string; name: string; email: string }
const loading = ref(true)
const loadError = ref<string | null>(null)
const busy = ref(false)
const confirming = ref(false)
const picked = ref<string | null>(null)
const members = ref<Member[]>([])
const ownerId = ref<string | null>(null)
const editorIds = ref<string[]>([])

const member = (id: string | null) => members.value.find(m => m.id === id) || null
const owner = computed(() => member(ownerId.value))
const initial = (m: Member | null) => (m?.name || m?.email || '?').charAt(0).toUpperCase()
// Ownership goes only to a person granted "Can edit" (not a group).
const candidates = computed(() => editorIds.value
    .filter(id => id !== ownerId.value)
    .map((id) => {
        const m = member(id)
        return { id, name: m?.name || '', email: m?.email || '', label: m?.name || m?.email || t('dashboardCollab.share.unknownPerson') }
    }))
const pickedPerson = computed(() => candidates.value.find(c => c.id === picked.value) || null)

function applySharing(sh: any) {
    ownerId.value = sh?.owner_id || null
    editorIds.value = sh?.editor_user_ids || []
    if (picked.value && !editorIds.value.includes(picked.value)) picked.value = null
}

async function load() {
    loading.value = true
    loadError.value = null
    try {
        const [sharing, people] = await Promise.all([
            useMyFetch(`/artifacts/${props.dashboardId}/sharing`),
            useMyFetch('/organization/members'),
        ])
        if (sharing.error.value) throw sharing.error.value
        members.value = ((people.data.value as any[]) || []).map((u: any) => ({ id: u.id, name: u.name || '', email: u.email || '' }))
        applySharing(sharing.data.value)
    } catch (e) {
        loadError.value = getErrorMessage(e, t('dashboardCollab.history.ownerLoadFailed'))
    } finally {
        loading.value = false
    }
}

async function transfer() {
    const target = pickedPerson.value
    if (!target) return
    busy.value = true
    try {
        const res = await useMyFetch(`/artifacts/${props.dashboardId}/transfer`, { method: 'POST', body: { user_id: target.id } })
        if (res.error.value) throw res.error.value
        applySharing(res.data.value)
        picked.value = null
        confirming.value = false
        toast.add({ title: t('dashboardCollab.history.transferDone', { name: target.label }), color: 'green' })
        emit('transferred')
    } catch (e) {
        toast.add({ title: t('dashboardCollab.history.transferFailed'), description: getErrorMessage(e), color: 'red' })
    } finally {
        busy.value = false
    }
}

watch(() => props.dashboardId, (id) => { if (id) load() }, { immediate: true })
</script>
