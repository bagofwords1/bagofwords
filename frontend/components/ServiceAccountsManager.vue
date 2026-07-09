<template>
    <div class="mt-4">
        <!-- Header -->
        <div class="flex flex-col md:flex-row md:items-start md:justify-between gap-3 mb-6">
            <div>
                <h3 class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ $t('serviceAccounts.title') }}</h3>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-1 max-w-xl">{{ $t('serviceAccounts.subtitle') }}</p>
            </div>
            <UButton
                v-if="useCan('manage_service_accounts')"
                color="blue" variant="solid" size="xs" icon="i-heroicons-plus"
                @click="openCreateModal"
            >
                {{ $t('serviceAccounts.newAccount') }}
            </UButton>
        </div>

        <!-- Table -->
        <div class="bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
            <div class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-100 dark:divide-gray-800">
                    <thead class="bg-gray-50/60 dark:bg-gray-900">
                        <tr>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('serviceAccounts.colName') }}</th>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('serviceAccounts.colRole') }}</th>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('serviceAccounts.colKeys') }}</th>
                            <th class="px-4 py-2 text-start text-xs font-medium text-gray-500 dark:text-gray-400">{{ $t('serviceAccounts.colStatus') }}</th>
                            <th class="px-4 py-2 text-end text-xs font-medium text-gray-500 dark:text-gray-400"></th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-100 dark:divide-gray-800">
                        <tr v-for="sa in accounts" :key="sa.id" class="hover:bg-gray-50/70 dark:hover:bg-gray-800/50">
                            <td class="px-4 py-3">
                                <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ sa.name }}</div>
                                <div v-if="sa.description" class="text-xs text-gray-500 dark:text-gray-400">{{ sa.description }}</div>
                            </td>
                            <td class="px-4 py-3">
                                <UBadge v-for="r in sa.roles" :key="r.id" size="xs" color="gray" variant="subtle" class="me-1">{{ r.name }}</UBadge>
                                <span v-if="!sa.roles.length" class="text-xs text-gray-400 italic">{{ $t('serviceAccounts.noRole') }}</span>
                            </td>
                            <td class="px-4 py-3 text-sm text-gray-600 dark:text-gray-300">{{ sa.key_count }}</td>
                            <td class="px-4 py-3">
                                <UBadge v-if="sa.disabled" size="xs" color="red" variant="subtle">{{ $t('serviceAccounts.disabled') }}</UBadge>
                                <UBadge v-else size="xs" color="green" variant="subtle">{{ $t('serviceAccounts.active') }}</UBadge>
                            </td>
                            <td class="px-4 py-3 text-end">
                                <UDropdown :items="rowActions(sa)" v-if="useCan('manage_service_accounts')">
                                    <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-ellipsis-horizontal" />
                                </UDropdown>
                            </td>
                        </tr>
                        <tr v-if="!accounts.length">
                            <td colspan="5" class="px-4 py-10 text-center text-sm text-gray-400">
                                {{ $t('serviceAccounts.empty') }}
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Create modal -->
        <UModal v-model="showCreateModal">
            <div class="p-5">
                <h3 class="text-base font-semibold mb-4">{{ $t('serviceAccounts.newAccount') }}</h3>
                <form @submit.prevent="createAccount" class="space-y-4">
                    <div>
                        <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('serviceAccounts.colName') }}</label>
                        <UInput v-model="form.name" required placeholder="CI Pipeline" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('serviceAccounts.description') }}</label>
                        <UInput v-model="form.description" placeholder="Automated report generation" />
                    </div>
                    <div>
                        <label class="block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">{{ $t('serviceAccounts.colRole') }}</label>
                        <USelectMenu
                            v-model="form.role_id"
                            :options="roleOptions"
                            value-attribute="value"
                            option-attribute="label"
                            :placeholder="$t('serviceAccounts.selectRole')"
                        />
                        <p class="text-xs text-gray-400 mt-1">{{ $t('serviceAccounts.roleHint') }}</p>
                    </div>
                    <div class="flex justify-end gap-2 pt-2">
                        <UButton variant="ghost" type="button" @click="showCreateModal = false">{{ $t('common.cancel') }}</UButton>
                        <UButton type="submit" color="blue" :loading="saving">{{ $t('serviceAccounts.create') }}</UButton>
                    </div>
                </form>
            </div>
        </UModal>

        <!-- Keys / detail modal -->
        <UModal v-model="showKeysModal" :ui="{ width: 'sm:max-w-2xl' }">
            <div class="p-5" v-if="activeAccount">
                <div class="flex items-center justify-between mb-4">
                    <h3 class="text-base font-semibold">{{ activeAccount.name }} — {{ $t('serviceAccounts.apiKeys') }}</h3>
                    <UButton color="blue" variant="solid" size="xs" icon="i-heroicons-plus" @click="createKey">{{ $t('serviceAccounts.newKey') }}</UButton>
                </div>

                <!-- Newly-minted key (shown once) -->
                <div v-if="newKey" class="mb-4 p-3 rounded-md bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
                    <p class="text-xs text-amber-800 dark:text-amber-300 mb-2">{{ $t('serviceAccounts.keyOnce') }}</p>
                    <div class="flex items-center gap-2">
                        <input
                            ref="keyInput"
                            :value="newKey"
                            readonly
                            class="flex-1 text-xs font-mono bg-white dark:bg-gray-800 rounded px-2 py-1.5 border border-amber-200 dark:border-amber-800 select-all"
                            @focus="(e) => (e.target as HTMLInputElement).select()"
                        />
                        <UButton size="xs" color="blue" icon="i-heroicons-clipboard" @click="copy(newKey)">{{ $t('serviceAccounts.copy') }}</UButton>
                    </div>
                </div>

                <table class="min-w-full divide-y divide-gray-100 dark:divide-gray-800">
                    <thead>
                        <tr>
                            <th class="px-2 py-2 text-start text-xs font-medium text-gray-500">{{ $t('serviceAccounts.keyName') }}</th>
                            <th class="px-2 py-2 text-start text-xs font-medium text-gray-500">{{ $t('serviceAccounts.keyPrefix') }}</th>
                            <th class="px-2 py-2 text-start text-xs font-medium text-gray-500">{{ $t('serviceAccounts.keyLastUsed') }}</th>
                            <th class="px-2 py-2"></th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-gray-100 dark:divide-gray-800">
                        <tr v-for="k in activeAccount.keys" :key="k.id">
                            <td class="px-2 py-2 text-sm">{{ k.name }}</td>
                            <td class="px-2 py-2 text-xs font-mono text-gray-500">{{ k.key_prefix }}…</td>
                            <td class="px-2 py-2 text-xs text-gray-500">{{ k.last_used_at ? new Date(k.last_used_at).toLocaleString() : $t('serviceAccounts.never') }}</td>
                            <td class="px-2 py-2 text-end">
                                <UButton size="xs" color="red" variant="ghost" icon="i-heroicons-trash" @click="revokeKey(k.id)" />
                            </td>
                        </tr>
                        <tr v-if="!activeAccount.keys.length">
                            <td colspan="4" class="px-2 py-6 text-center text-xs text-gray-400">{{ $t('serviceAccounts.noKeys') }}</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { useCan } from '~/composables/usePermissions'

interface Role { id: string; name: string }
interface ApiKeyRow { id: string; name: string; key_prefix: string; last_used_at?: string | null }
interface ServiceAccount {
    id: string; name: string; description?: string; disabled: boolean;
    roles: Role[]; key_count: number; keys?: ApiKeyRow[]
}

const props = defineProps<{ organization: { id: string; name?: string } }>()
const organizationId = props.organization.id
const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const accounts = ref<ServiceAccount[]>([])
const roles = ref<Role[]>([])
const saving = ref(false)

const showCreateModal = ref(false)
const showKeysModal = ref(false)
const activeAccount = ref<(ServiceAccount & { keys: ApiKeyRow[] }) | null>(null)
const newKey = ref<string | null>(null)
const keyInput = ref<HTMLInputElement | null>(null)
const form = ref<{ name: string; description: string; role_id: string | undefined }>({ name: '', description: '', role_id: undefined })

const roleOptions = computed(() => roles.value.map(r => ({ label: r.name, value: r.id })))

async function loadAccounts() {
    const { data, error } = await useMyFetch('/service_accounts')
    if (!error.value && data.value) accounts.value = data.value as ServiceAccount[]
}

async function loadRoles() {
    const { data, error } = await useMyFetch(`/organizations/${organizationId}/roles`)
    if (!error.value && data.value) roles.value = (data.value as any[]).map(r => ({ id: r.id, name: r.name }))
}

function openCreateModal() {
    form.value = { name: '', description: '', role_id: undefined }
    showCreateModal.value = true
}

async function createAccount() {
    saving.value = true
    try {
        const { data, error } = await useMyFetch('/service_accounts', {
            method: 'POST',
            body: { name: form.value.name, description: form.value.description || null, role_id: form.value.role_id || null },
        })
        if (error.value) throw error.value
        toast.add({ title: t('serviceAccounts.created'), color: 'green' })
        showCreateModal.value = false
        await loadAccounts()
        // Open the detail modal so the admin can immediately mint a key.
        if (data.value) await openKeys(data.value as ServiceAccount)
    } catch (e: any) {
        toast.add({ title: t('serviceAccounts.createFailed'), description: e?.data?.detail || String(e), color: 'red' })
    } finally {
        saving.value = false
    }
}

async function openKeys(sa: ServiceAccount) {
    newKey.value = null
    const { data, error } = await useMyFetch(`/service_accounts/${sa.id}`)
    if (!error.value && data.value) {
        activeAccount.value = data.value as any
        showKeysModal.value = true
    }
}

async function createKey() {
    if (!activeAccount.value) return
    const said = activeAccount.value.id
    const { data, error } = await useMyFetch(`/service_accounts/${said}/keys`, {
        method: 'POST', body: { name: 'key' },
    })
    if (error.value) {
        toast.add({ title: t('serviceAccounts.keyFailed'), color: 'red' })
        return
    }
    const minted = (data.value as any).key
    // Refresh the keys list WITHOUT going through openKeys() — it resets
    // newKey, which would wipe the one-time secret before it can be shown.
    const { data: detail, error: derr } = await useMyFetch(`/service_accounts/${said}`)
    if (!derr.value && detail.value) activeAccount.value = detail.value as any
    // Set after the refresh so the full key stays visible (shown only once).
    newKey.value = minted
    showKeysModal.value = true
    await loadAccounts()
}

// Don't leak a previously-minted secret if the modal is reopened later.
watch(showKeysModal, (open) => { if (!open) newKey.value = null })

async function revokeKey(keyId: string) {
    if (!activeAccount.value) return
    try {
        await useMyFetchStrict(`/service_accounts/${activeAccount.value.id}/keys/${keyId}`, { method: 'DELETE' })
        await openKeys(activeAccount.value)
        await loadAccounts()
    } catch (e: any) {
        toast.add({ title: getErrorMessage(e, 'Could not revoke key'), color: 'red' })
    }
}

async function setDisabled(sa: ServiceAccount, disabled: boolean) {
    try {
        await useMyFetchStrict(`/service_accounts/${sa.id}`, { method: 'PATCH', body: { disabled } })
        await loadAccounts()
    } catch (e: any) {
        toast.add({ title: getErrorMessage(e, disabled ? 'Could not disable service account' : 'Could not enable service account'), color: 'red' })
    }
}

async function deleteAccount(sa: ServiceAccount) {
    try {
        await useMyFetchStrict(`/service_accounts/${sa.id}`, { method: 'DELETE' })
        toast.add({ title: t('serviceAccounts.deleted'), color: 'green' })
        await loadAccounts()
    } catch (e: any) {
        toast.add({ title: getErrorMessage(e, 'Could not delete service account'), color: 'red' })
    }
}

function rowActions(sa: ServiceAccount) {
    return [[
        { label: t('serviceAccounts.manageKeys'), icon: 'i-heroicons-key', click: () => openKeys(sa) },
        sa.disabled
            ? { label: t('serviceAccounts.enable'), icon: 'i-heroicons-play', click: () => setDisabled(sa, false) }
            : { label: t('serviceAccounts.disable'), icon: 'i-heroicons-pause', click: () => setDisabled(sa, true) },
        { label: t('serviceAccounts.delete'), icon: 'i-heroicons-trash', click: () => deleteAccount(sa) },
    ]]
}

async function copy(text: string | null) {
    if (!text) return
    let ok = false
    try {
        // Clipboard API only works in secure contexts (https / localhost).
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text)
            ok = true
        }
    } catch { /* fall through to legacy path */ }
    if (!ok) {
        // Fallback for plain-http origins: copy via a temporary selection.
        try {
            const el = keyInput.value || document.createElement('textarea')
            if (!keyInput.value) { el.value = text; document.body.appendChild(el) }
            ;(el as HTMLInputElement).select()
            ok = document.execCommand('copy')
            if (!keyInput.value) document.body.removeChild(el)
        } catch { ok = false }
    }
    toast.add(
        ok
            ? { title: t('serviceAccounts.copied'), color: 'green' }
            : { title: t('serviceAccounts.copyManual'), color: 'orange' }
    )
}

onMounted(async () => {
    await Promise.all([loadAccounts(), loadRoles()])
})
</script>
