<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-2xl' }">
        <div class="p-6 relative" data-testid="sandbox-environment-modal">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h3 class="text-lg font-semibold text-ink pe-8">
                {{ environment ? $t('sandbox.environments.form.editTitle', { name: environment.name }) : $t('sandbox.environments.form.addTitle') }}
            </h3>
            <p class="text-sm text-ink-muted">{{ $t('sandbox.environments.form.hint') }}</p>
            <hr class="my-4 border-line" />

            <form class="space-y-5" @submit.prevent="save">
                <!-- What the API said about this environment as saved. -->
                <div
                    v-if="environment?.warnings?.length"
                    class="rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 flex items-start gap-3 text-sm text-amber-800 dark:text-amber-200"
                    data-testid="sandbox-environment-warnings"
                >
                    <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 shrink-0 mt-0.5" />
                    <ul class="min-w-0 space-y-1">
                        <li v-for="warning in environment.warnings" :key="warning" dir="auto">{{ warning }}</li>
                    </ul>
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div class="flex flex-col">
                        <label for="sandbox-env-name" class="text-sm font-medium text-ink-2 mb-1.5">
                            {{ $t('sandbox.environments.form.name') }} <span class="text-red-600" aria-hidden="true">*</span>
                        </label>
                        <UInput id="sandbox-env-name" v-model="form.name" required maxlength="120" data-testid="sandbox-env-name" />
                    </div>
                    <div class="flex flex-col">
                        <label for="sandbox-env-image" class="text-sm font-medium text-ink-2 mb-1.5">
                            {{ $t('sandbox.environments.form.image') }} <span class="text-red-600" aria-hidden="true">*</span>
                        </label>
                        <UInput
                            id="sandbox-env-image"
                            v-model="form.image"
                            required
                            dir="ltr"
                            spellcheck="false"
                            placeholder="registry.example.com/team/image:tag"
                            :ui="{ base: 'font-mono' }"
                            data-testid="sandbox-env-image"
                        />
                    </div>
                    <div class="flex flex-col sm:col-span-2">
                        <label for="sandbox-env-description" class="text-sm font-medium text-ink-2 mb-1.5">
                            {{ $t('sandbox.environments.form.description') }}
                            <span class="font-normal text-ink-muted">{{ $t('common.optional') }}</span>
                        </label>
                        <UTextarea
                            id="sandbox-env-description"
                            v-model="form.description"
                            :rows="2"
                            autoresize
                            maxlength="2000"
                            :placeholder="$t('sandbox.environments.form.descriptionPlaceholder')"
                            data-testid="sandbox-env-description"
                        />
                    </div>
                </div>

                <!-- Resources -->
                <div>
                    <div class="text-sm font-semibold text-ink mb-2">{{ $t('sandbox.environments.form.resources') }}</div>
                    <div class="grid grid-cols-2 sm:grid-cols-4 gap-4">
                        <div class="flex flex-col">
                            <label for="sandbox-env-cpus" class="text-sm font-medium text-ink-2 mb-1.5">{{ $t('sandbox.environments.form.cpus') }}</label>
                            <UInput id="sandbox-env-cpus" v-model.number="form.cpus" type="number" step="0.25" min="0.25" max="64" dir="ltr" data-testid="sandbox-env-cpus" />
                        </div>
                        <div class="flex flex-col">
                            <label for="sandbox-env-memory" class="text-sm font-medium text-ink-2 mb-1.5">{{ $t('sandbox.environments.form.memory') }}</label>
                            <UInput id="sandbox-env-memory" v-model.number="form.memory_mb" type="number" step="1" min="128" max="262144" dir="ltr" data-testid="sandbox-env-memory" />
                        </div>
                        <div class="flex flex-col">
                            <label for="sandbox-env-disk" class="text-sm font-medium text-ink-2 mb-1.5">{{ $t('sandbox.environments.form.disk') }}</label>
                            <UInput id="sandbox-env-disk" v-model.number="form.disk_mb" type="number" step="1" min="256" max="1048576" dir="ltr" data-testid="sandbox-env-disk" />
                        </div>
                        <div class="flex flex-col">
                            <label for="sandbox-env-timeout" class="text-sm font-medium text-ink-2 mb-1.5">{{ $t('sandbox.environments.form.runTimeout') }}</label>
                            <UInput id="sandbox-env-timeout" v-model.number="form.run_timeout_seconds" type="number" step="1" min="10" max="3600" dir="ltr" data-testid="sandbox-env-timeout" />
                        </div>
                    </div>
                    <p class="mt-1.5 text-xs text-ink-muted">{{ $t('sandbox.environments.form.resourcesHint') }}</p>
                </div>

                <!-- Network -->
                <div class="flex flex-col">
                    <label for="sandbox-env-network" class="text-sm font-medium text-ink-2 mb-1.5">{{ $t('sandbox.environments.form.network') }}</label>
                    <USelect
                        id="sandbox-env-network"
                        v-model="form.network_policy"
                        :options="networkOptions"
                        option-attribute="label"
                        value-attribute="value"
                        class="sm:max-w-xs"
                        data-testid="sandbox-env-network"
                    />
                    <p class="mt-1.5 text-xs text-ink-muted leading-relaxed">{{ $t(`sandbox.network.${form.network_policy}Hint`) }}</p>
                    <!-- Allowlists are edited elsewhere (not yet in this form): shown as stored. -->
                    <div v-if="form.network_policy === 'allowlist' && environment?.allowed_domains?.length" class="mt-2 flex flex-wrap gap-1" data-testid="sandbox-env-allowed-domains">
                        <UBadge v-for="domain in environment.allowed_domains" :key="domain" size="xs" color="gray"><span dir="ltr">{{ domain }}</span></UBadge>
                    </div>
                </div>

                <!-- Switches -->
                <div class="space-y-3">
                    <div class="flex items-start justify-between gap-6">
                        <div class="min-w-0">
                            <div class="text-sm font-medium text-ink-2">{{ $t('sandbox.environments.form.strongIsolation') }}</div>
                            <p class="mt-0.5 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.environments.form.strongIsolationHint') }}</p>
                        </div>
                        <UToggle v-model="form.require_strong_isolation" class="shrink-0 mt-0.5" :aria-label="$t('sandbox.environments.form.strongIsolation')" data-testid="sandbox-env-strong-isolation" />
                    </div>
                    <div class="flex items-start justify-between gap-6">
                        <div class="min-w-0">
                            <div class="text-sm font-medium text-ink-2">{{ $t('sandbox.environments.form.isDefault') }}</div>
                            <p class="mt-0.5 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.environments.form.isDefaultHint') }}</p>
                        </div>
                        <UToggle v-model="form.is_default" class="shrink-0 mt-0.5" :aria-label="$t('sandbox.environments.form.isDefault')" data-testid="sandbox-env-default" />
                    </div>
                    <div class="flex items-start justify-between gap-6">
                        <div class="min-w-0">
                            <div class="text-sm font-medium text-ink-2">{{ $t('sandbox.environments.form.enabled') }}</div>
                            <p class="mt-0.5 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.environments.form.enabledHint') }}</p>
                        </div>
                        <UToggle v-model="form.enabled" class="shrink-0 mt-0.5" :aria-label="$t('sandbox.environments.form.enabled')" data-testid="sandbox-env-enabled" />
                    </div>
                </div>

                <!-- Who may use it -->
                <div data-testid="sandbox-env-access">
                    <div class="text-sm font-semibold text-ink">{{ $t('sandbox.environments.form.access') }}</div>
                    <p class="mt-0.5 mb-3 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.environments.form.accessHint') }}</p>
                    <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
                        <div v-for="picker in pickers" :key="picker.type" class="flex flex-col min-w-0">
                            <label class="text-sm font-medium text-ink-2 mb-1.5">{{ picker.label }}</label>
                            <USelectMenu
                                v-model="access[picker.type]"
                                :options="picker.options"
                                multiple
                                searchable
                                option-attribute="label"
                                value-attribute="id"
                                :disabled="!picker.options.length"
                                :searchable-placeholder="$t('common.search')"
                                :popper="{ strategy: 'fixed' }"
                                :data-testid="`sandbox-env-access-${picker.type}`"
                            >
                                <template #label>
                                    <span v-if="access[picker.type].length" class="truncate">{{ picker.summary }}</span>
                                    <span v-else class="text-ink-subtle truncate">{{ picker.options.length ? picker.placeholder : picker.empty }}</span>
                                </template>
                                <template #option="{ option }">
                                    <span class="truncate" :data-testid="`sandbox-env-access-option-${picker.type}`">{{ option.label }}</span>
                                    <span v-if="option.hint" class="truncate text-xs text-ink-muted">{{ option.hint }}</span>
                                </template>
                            </USelectMenu>
                        </div>
                    </div>
                    <div v-if="selectedChips.length" class="mt-3 flex flex-wrap gap-1.5" data-testid="sandbox-env-access-chips">
                        <UBadge v-for="chip in selectedChips" :key="`${chip.type}:${chip.id}`" size="xs" color="gray" class="gap-1">
                            <UIcon :name="chip.icon" class="w-3 h-3" />
                            {{ chip.label }}
                            <button type="button" class="hover:text-ink" :aria-label="$t('common.remove')" @click="removeGrant(chip.type, chip.id)">
                                <UIcon name="i-heroicons-x-mark" class="w-3 h-3" />
                            </button>
                        </UBadge>
                    </div>
                    <p v-else class="mt-3 text-xs text-ink-muted" data-testid="sandbox-env-access-none">{{ $t('sandbox.environments.form.accessNone') }}</p>
                </div>

                <p v-if="errorMessage" class="text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-environment-error">
                    {{ errorMessage }}
                </p>

                <div class="flex justify-end gap-2 pt-1">
                    <UButton variant="ghost" color="gray" type="button" @click="open = false">{{ $t('common.cancel') }}</UButton>
                    <UButton type="submit" color="blue" :loading="saving" data-testid="sandbox-environment-save">
                        {{ environment ? $t('common.saveChanges') : $t('sandbox.environments.form.create') }}
                    </UButton>
                </div>
            </form>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import type { SandboxAccessGrant, SandboxEnvironment, SandboxPrincipalOption } from '~/composables/useSandboxApi'

type PrincipalType = 'role' | 'group' | 'user'

const props = defineProps<{
    modelValue: boolean
    environment: SandboxEnvironment | null
    roles: SandboxPrincipalOption[]
    groups: SandboxPrincipalOption[]
    users: SandboxPrincipalOption[]
}>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    saved: [environment: SandboxEnvironment]
}>()

const { t } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage } = useErrorMessage()

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})

const DEFAULTS = {
    name: '', description: '', image: '',
    cpus: 1, memory_mb: 2048, disk_mb: 5120, run_timeout_seconds: 600,
    network_policy: 'none', require_strong_isolation: false, is_default: false, enabled: true,
}
const form = reactive({ ...DEFAULTS })
const access = reactive<Record<PrincipalType, string[]>>({ role: [], group: [], user: [] })
const saving = ref(false)
const errorMessage = ref('')

// "allowlist" exists in the API but is not offered for new environments; an
// environment that already has it keeps it.
const networkOptions = computed(() => {
    const options = [
        { value: 'none', label: t('sandbox.network.none') },
        { value: 'registries', label: t('sandbox.network.registries') },
        { value: 'full', label: t('sandbox.network.full') },
    ]
    if (props.environment?.network_policy === 'allowlist') {
        options.splice(2, 0, { value: 'allowlist', label: t('sandbox.network.allowlist') })
    }
    return options
})

const optionsOf = (type: PrincipalType) => (type === 'role' ? props.roles : type === 'group' ? props.groups : props.users)
const ICONS: Record<PrincipalType, string> = { role: 'i-heroicons-identification', group: 'i-heroicons-user-group', user: 'i-heroicons-user' }

const pickers = computed(() => (['role', 'group', 'user'] as PrincipalType[]).map(type => ({
    type,
    options: optionsOf(type),
    label: t(`sandbox.environments.form.${type}s`),
    placeholder: t(`sandbox.environments.form.${type}sPlaceholder`),
    empty: t(`sandbox.environments.form.${type}sEmpty`),
    summary: t('sandbox.environments.form.selectedCount', { n: access[type].length }),
})))

const selectedChips = computed(() => (['role', 'group', 'user'] as PrincipalType[]).flatMap(type =>
    access[type].map(id => ({
        type,
        id,
        icon: ICONS[type],
        // A grant whose principal is no longer listed is kept.
        label: optionsOf(type).find(option => option.id === id)?.label || t('sandbox.environments.form.unknownPrincipal'),
    }))
))

function removeGrant(type: PrincipalType, id: string) {
    access[type] = access[type].filter(item => item !== id)
}

const grantsPayload = (): SandboxAccessGrant[] =>
    (['role', 'group', 'user'] as PrincipalType[]).flatMap(type =>
        access[type].map(id => ({ principal_type: type, principal_id: id })))

const grantKey = (grants: SandboxAccessGrant[]) =>
    grants.map(grant => `${grant.principal_type}:${grant.principal_id}`).sort().join('|')

// Set once this dialog has created the environment. If a later step of the
// same save fails (the access list), a retry updates this environment instead
// of creating a second one with the same name.
const createdId = ref<string | null>(null)

watch(() => props.modelValue, (isOpen) => {
    if (!isOpen) return
    errorMessage.value = ''
    createdId.value = null
    const env = props.environment
    Object.assign(form, DEFAULTS, env ? {
        name: env.name, description: env.description || '', image: env.image,
        cpus: env.cpus, memory_mb: env.memory_mb, disk_mb: env.disk_mb,
        run_timeout_seconds: env.run_timeout_seconds, network_policy: env.network_policy,
        require_strong_isolation: env.require_strong_isolation, is_default: env.is_default, enabled: env.enabled,
    } : {})
    for (const type of ['role', 'group', 'user'] as PrincipalType[]) {
        access[type] = (env?.access || []).filter(grant => grant.principal_type === type).map(grant => grant.principal_id)
    }
})

async function save() {
    if (saving.value) return
    saving.value = true
    errorMessage.value = ''
    const body = {
        name: form.name.trim(),
        description: form.description.trim() || null,
        image: form.image.trim(),
        cpus: Number(form.cpus),
        memory_mb: Number(form.memory_mb),
        disk_mb: Number(form.disk_mb),
        run_timeout_seconds: Number(form.run_timeout_seconds),
        network_policy: form.network_policy,
        require_strong_isolation: form.require_strong_isolation,
        is_default: form.is_default,
        enabled: form.enabled,
    }
    let saved: SandboxEnvironment | null = null
    try {
        const existingId = props.environment?.id || createdId.value
        saved = existingId
            ? await call<SandboxEnvironment>(`/environments/${existingId}`, { method: 'PUT', body })
            : await call<SandboxEnvironment>('/environments', { method: 'POST', body })
        if (!props.environment) createdId.value = saved.id
        const grants = grantsPayload()
        if (grantKey(grants) !== grantKey(saved.access || [])) {
            saved = {
                ...saved,
                access: await call<SandboxAccessGrant[]>(`/environments/${saved.id}/access`, { method: 'PUT', body: { grants } }),
            }
        }
        emit('saved', saved)
        toast.add({
            title: props.environment ? t('sandbox.environments.toast.saved') : t('sandbox.environments.toast.created'),
            color: 'green',
        })
        // Not errors: things the admin should know about it as saved.
        for (const warning of saved.warnings || []) {
            toast.add({ title: t('sandbox.environments.toast.warning'), description: warning, color: 'amber', icon: 'i-heroicons-exclamation-triangle', timeout: 12000 })
        }
        open.value = false
    } catch (error) {
        errorMessage.value = getErrorMessage(error, t('sandbox.environments.toast.saveFailed'))
        // The environment itself was saved and only the access change failed:
        // keep the list in step with the server.
        if (saved) emit('saved', saved)
    } finally {
        saving.value = false
    }
}
</script>
