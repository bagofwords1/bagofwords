<template>
    <!-- Provider managers (Enterprise, org LLM admins only): members who run
         this provider — its credentials, turning its models on or off, model
         settings and routing hints. Org-wide LLM settings stay with admins. -->
    <div data-testid="provider-managers">
        <div class="flex items-center gap-1.5 mb-1">
            <span class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.llms.providerManagers.title') }}</span>
            <UPopover mode="hover" :popper="{ placement: 'top' }">
                <UIcon name="i-heroicons-question-mark-circle" class="w-4 h-4 text-ink-muted hover:text-ink-2 cursor-help" />
                <template #panel>
                    <div class="p-3 max-w-xs text-xs text-ink-2 space-y-1.5">
                        <p>{{ $t('settings.llms.providerManagers.can') }}</p>
                        <p>{{ $t('settings.llms.providerManagers.cannot') }}</p>
                    </div>
                </template>
            </UPopover>
        </div>
        <p class="text-xs text-ink-muted mb-2">{{ $t('settings.llms.providerManagers.hint') }}</p>

        <div class="flex gap-2 mb-2">
            <USelectMenu
                v-model="toAdd"
                :options="addableOptions"
                option-attribute="label"
                value-attribute="value"
                searchable
                :placeholder="$t('settings.llms.providerManagers.addPlaceholder')"
                class="flex-1"
                size="sm"
                data-testid="provider-manager-select"
            />
            <UButton color="blue" size="sm" :disabled="!toAdd || busy" :loading="busy" data-testid="provider-manager-add" @click="add">
                {{ $t('settings.llms.providerManagers.add') }}
            </UButton>
        </div>

        <div class="border border-line rounded-lg divide-y divide-line">
            <div v-if="loading" class="px-3 py-3 text-sm text-ink-muted">
                <Spinner class="w-4 h-4 me-2 inline" />{{ $t('settings.llms.providerManagers.loading') }}
            </div>
            <div v-else-if="managers.length === 0" class="px-3 py-3 text-sm text-ink-muted" data-testid="provider-managers-empty">
                {{ $t('settings.llms.providerManagers.none') }}
            </div>
            <div
                v-for="m in managers"
                v-else
                :key="m.user_id"
                class="px-3 py-2 flex items-center justify-between gap-2"
                data-testid="provider-manager-row"
            >
                <div class="min-w-0">
                    <div class="text-sm text-ink truncate">{{ m.name || m.email }}</div>
                    <div v-if="m.name && m.email" class="text-xs text-ink-muted truncate">{{ m.email }}</div>
                </div>
                <UButton
                    size="xs" color="gray" variant="ghost" icon="i-heroicons-x-mark"
                    :aria-label="$t('settings.llms.providerManagers.remove')"
                    data-testid="provider-manager-remove"
                    @click="remove(m.user_id)"
                />
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import Spinner from './Spinner.vue'
import { useErrorMessage } from '~/composables/useErrorMessage'

const props = defineProps<{ providerId: string }>()

type Manager = { user_id: string; name?: string | null; email?: string | null }
type OrgMember = { id: string; user_id?: string; user?: { id: string; name?: string; email: string }; email?: string }

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()
const { organization } = useOrganization()

const managers = ref<Manager[]>([])
const members = ref<OrgMember[]>([])
const toAdd = ref<string | null>(null)
const loading = ref(false)
const busy = ref(false)

// Only registered members can manage a provider (a pending invite has no user).
const addableOptions = computed(() => {
    const taken = new Set(managers.value.map(m => m.user_id))
    return members.value
        .map(m => ({ id: m.user_id || m.user?.id, m }))
        .filter(x => x.id && !taken.has(x.id))
        .map(({ id, m }) => ({
            value: id as string,
            label: m.user?.name ? `${m.user.name} (${m.user.email})` : (m.user?.email || m.email || id),
        }))
})

async function load() {
    if (!props.providerId) return
    loading.value = true
    try {
        const { data } = await useMyFetch(`/llm/providers/${props.providerId}/managers`)
        managers.value = (data.value as Manager[]) || []
    } finally {
        loading.value = false
    }
}

async function loadMembers() {
    const { data } = await useMyFetch(`/organizations/${organization.value.id}/members`)
    members.value = (data.value as OrgMember[]) || []
}

async function add() {
    if (!toAdd.value) return
    busy.value = true
    try {
        const { error } = await useMyFetch(`/llm/providers/${props.providerId}/managers/${toAdd.value}`, { method: 'PUT' })
        if (error.value) {
            toast.add({ title: getErrorMessage(error.value, t('settings.llms.providerManagers.failed')), color: 'red' })
            return
        }
        toAdd.value = null
        await load()
        toast.add({ title: t('settings.llms.providerManagers.added'), color: 'green' })
    } finally {
        busy.value = false
    }
}

async function remove(userId: string) {
    const { error } = await useMyFetch(`/llm/providers/${props.providerId}/managers/${userId}`, { method: 'DELETE' })
    if (error.value) {
        toast.add({ title: getErrorMessage(error.value, t('settings.llms.providerManagers.failed')), color: 'red' })
        return
    }
    await load()
    toast.add({ title: t('settings.llms.providerManagers.removed'), color: 'green' })
}

watch(() => props.providerId, load)
onMounted(async () => {
    await Promise.all([load(), loadMembers()])
})
</script>
