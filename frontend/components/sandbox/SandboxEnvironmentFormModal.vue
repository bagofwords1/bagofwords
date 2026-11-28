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
                <SandboxEnvironmentWarnings v-if="environment" :environment="environment" detailed data-testid="sandbox-environment-warnings" />

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

                <!-- Ask the person in the chat before a run -->
                <div class="flex flex-col">
                    <label for="sandbox-env-confirm" class="text-sm font-medium text-ink-2 mb-1.5">{{ $t('sandbox.environments.form.confirmRuns') }}</label>
                    <USelect
                        id="sandbox-env-confirm"
                        v-model="form.confirm_runs"
                        :options="confirmOptions"
                        option-attribute="label"
                        value-attribute="value"
                        class="sm:max-w-xs"
                        data-testid="sandbox-env-confirm-runs"
                    />
                    <p class="mt-1.5 text-xs text-ink-muted leading-relaxed" data-testid="sandbox-env-confirm-hint">{{ $t(`sandbox.environments.confirm.${confirmKey}Hint`) }}</p>
                </div>

                <!-- Hooks -->
                <div data-testid="sandbox-env-hooks">
                    <div class="text-sm font-semibold text-ink">
                        {{ $t('sandbox.environments.form.hooks') }}
                        <span class="font-normal text-ink-muted">{{ $t('common.optional') }}</span>
                    </div>
                    <p class="mt-0.5 mb-3 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.environments.form.hooksHint') }}</p>
                    <div class="space-y-4">
                        <div v-for="hook in HOOKS" :key="hook" class="flex flex-col">
                            <label :for="`sandbox-env-${hook}`" class="text-sm font-medium text-ink-2 mb-1.5">{{ $t(`sandbox.environments.form.${hook}`) }}</label>
                            <UTextarea
                                :id="`sandbox-env-${hook}`"
                                v-model="form[hook]"
                                :rows="hook === 'setup_script' ? 4 : 2"
                                autoresize
                                :maxrows="14"
                                dir="ltr"
                                spellcheck="false"
                                maxlength="20000"
                                :placeholder="HOOK_PLACEHOLDERS[hook]"
                                :ui="{ base: 'font-mono text-xs' }"
                                :data-testid="`sandbox-env-${hook}`"
                            />
                            <p class="mt-1.5 text-xs text-ink-muted leading-relaxed">{{ $t(`sandbox.environments.form.${hook}Hint`) }}</p>
                        </div>
                    </div>
                </div>

                <!-- Volumes of a compute, mounted in every sandbox -->
                <div data-testid="sandbox-env-mounts">
                    <div class="text-sm font-semibold text-ink">
                        {{ $t('sandbox.environments.mounts.title') }}
                        <span class="font-normal text-ink-muted">{{ $t('common.optional') }}</span>
                    </div>
                    <p class="mt-0.5 mb-3 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.environments.mounts.hint') }}</p>
                    <datalist id="sandbox-env-volume-names">
                        <option v-for="volume in volumes" :key="volume.name" :value="volume.name">{{ volume.computes.join(', ') }}</option>
                    </datalist>
                    <div v-if="form.mounts.length" class="space-y-3">
                        <div v-for="(mount, index) in form.mounts" :key="mount.key" class="rounded-lg border border-line p-3" data-testid="sandbox-env-mount-row">
                            <div class="grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-3 items-start">
                                <div class="flex flex-col min-w-0">
                                    <label :for="`sandbox-mount-volume-${mount.key}`" class="text-xs font-medium text-ink-2 mb-1">{{ $t('sandbox.environments.mounts.volume') }}</label>
                                    <UInput
                                        :id="`sandbox-mount-volume-${mount.key}`"
                                        v-model="mount.volume"
                                        list="sandbox-env-volume-names"
                                        dir="ltr"
                                        spellcheck="false"
                                        autocomplete="off"
                                        maxlength="40"
                                        :placeholder="volumes[0]?.name || 'datasets'"
                                        :ui="{ base: 'font-mono' }"
                                        data-testid="sandbox-mount-volume"
                                    />
                                </div>
                                <div class="flex flex-col min-w-0">
                                    <label :for="`sandbox-mount-path-${mount.key}`" class="text-xs font-medium text-ink-2 mb-1">{{ $t('sandbox.environments.mounts.path') }}</label>
                                    <UInput
                                        :id="`sandbox-mount-path-${mount.key}`"
                                        v-model="mount.path"
                                        dir="ltr"
                                        spellcheck="false"
                                        maxlength="300"
                                        placeholder="/data"
                                        :color="mountErrors[mount.key] ? 'red' : undefined"
                                        :ui="{ base: 'font-mono' }"
                                        data-testid="sandbox-mount-path"
                                    />
                                </div>
                                <UButton
                                    type="button"
                                    color="gray"
                                    variant="ghost"
                                    size="xs"
                                    icon="i-heroicons-trash"
                                    class="sm:mt-6"
                                    :aria-label="$t('sandbox.environments.mounts.remove')"
                                    data-testid="sandbox-mount-remove"
                                    @click="form.mounts.splice(index, 1)"
                                />
                            </div>
                            <!-- The server's own sentence about this row (a reserved path, a bad name). -->
                            <p v-if="mountErrors[mount.key]" class="mt-1.5 text-xs text-red-600 dark:text-red-400 break-words" role="alert" dir="auto" data-testid="sandbox-mount-error">
                                {{ mountErrors[mount.key] }}
                            </p>
                            <p v-else-if="mountNote(mount)" class="mt-1.5 text-xs text-ink-muted" data-testid="sandbox-mount-volume-note">{{ mountNote(mount) }}</p>
                            <div class="mt-3 flex items-start justify-between gap-6">
                                <div class="min-w-0">
                                    <div class="text-sm font-medium text-ink-2">{{ $t('sandbox.environments.mounts.readOnly') }}</div>
                                    <p class="mt-0.5 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.environments.mounts.readOnlyHint') }}</p>
                                </div>
                                <UToggle v-model="mount.read_only" class="shrink-0 mt-0.5" :aria-label="$t('sandbox.environments.mounts.readOnly')" data-testid="sandbox-mount-read-only" />
                            </div>
                            <!-- Writable: who sees what is written there. -->
                            <fieldset v-if="!mount.read_only" class="mt-3 space-y-2" data-testid="sandbox-mount-sharing">
                                <legend class="sr-only">{{ $t('sandbox.environments.mounts.sharing') }}</legend>
                                <URadio
                                    v-for="choice in SHARING"
                                    :key="choice.key"
                                    v-model="mount.sharing"
                                    :value="choice.key"
                                    :name="`sandbox-mount-sharing-${mount.key}`"
                                    :data-testid="`sandbox-mount-sharing-${choice.key}`"
                                >
                                    <template #label>
                                        <span class="text-sm text-ink-2">{{ $t(`sandbox.environments.mounts.${choice.key}`) }}</span>
                                    </template>
                                    <template #help>
                                        <span class="text-xs text-ink-muted leading-relaxed">{{ $t(`sandbox.environments.mounts.${choice.key}Hint`) }}</span>
                                    </template>
                                </URadio>
                                <p
                                    v-if="mount.sharing === 'shared'"
                                    class="rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-3 py-2 flex items-start gap-2 text-xs text-amber-800 dark:text-amber-200 leading-relaxed"
                                    data-testid="sandbox-mount-shared-warning"
                                >
                                    <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 shrink-0" />
                                    <span>{{ $t('sandbox.environments.mounts.sharedWarning') }}</span>
                                </p>
                            </fieldset>
                        </div>
                    </div>
                    <UButton
                        type="button"
                        color="gray"
                        variant="soft"
                        size="xs"
                        icon="i-heroicons-plus"
                        :class="form.mounts.length ? 'mt-3' : ''"
                        :disabled="form.mounts.length >= 10"
                        data-testid="sandbox-mount-add"
                        @click="addMount"
                    >
                        {{ $t('sandbox.environments.mounts.add') }}
                    </UButton>
                    <p v-if="!volumes.length" class="mt-2 text-xs text-ink-muted" data-testid="sandbox-env-no-volumes">{{ $t('sandbox.environments.mounts.noVolumes') }}</p>
                </div>

                <!-- Which computes may run it -->
                <div class="flex flex-col">
                    <label for="sandbox-env-compute-labels" class="text-sm font-medium text-ink-2 mb-1.5">
                        {{ $t('sandbox.environments.form.computeLabels') }}
                        <span class="font-normal text-ink-muted">{{ $t('common.optional') }}</span>
                    </label>
                    <UInput id="sandbox-env-compute-labels" v-model="form.compute_labels" dir="ltr" placeholder="gpu, eu-west" class="sm:max-w-xs" data-testid="sandbox-env-compute-labels" />
                    <p class="mt-1.5 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.environments.form.computeLabelsHint') }}</p>
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
import type { SandboxAccessGrant, SandboxEnvironment, SandboxMount, SandboxPrincipalOption } from '~/composables/useSandboxApi'
import SandboxEnvironmentWarnings from '~/components/sandbox/SandboxEnvironmentWarnings.vue'

type PrincipalType = 'role' | 'group' | 'user'

const props = defineProps<{
    modelValue: boolean
    environment: SandboxEnvironment | null
    roles: SandboxPrincipalOption[]
    groups: SandboxPrincipalOption[]
    users: SandboxPrincipalOption[]
    // The volume names that exist on any compute, and which computes have each.
    volumes: Array<{ name: string; computes: string[] }>
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
    confirm_runs: 'never', setup_script: '', start_script: '', stop_script: '', compute_labels: '',
}
const HOOKS = ['setup_script', 'start_script', 'stop_script'] as const
const HOOK_PLACEHOLDERS: Record<typeof HOOKS[number], string> = {
    setup_script: 'pip install -r /data/requirements.txt',
    start_script: 'nohup my-service > /tmp/service.log 2>&1 &',
    stop_script: 'my-service --flush',
}
const CONFIRM_RUNS = ['never', 'always', 'full_network', 'packages']
const SHARING = [{ key: 'perReport' }, { key: 'shared' }] as const

// `key` only tells the rows apart while they are edited.
interface MountRow { key: number; volume: string; path: string; read_only: boolean; sharing: 'perReport' | 'shared' }
let mountKey = 0

const form = reactive({ ...DEFAULTS, mounts: [] as MountRow[] })
// The server's refusal of one mount, by the row's key.
const mountErrors = ref<Record<number, string>>({})

function addMount() {
    form.mounts.push({ key: ++mountKey, volume: '', path: '', read_only: true, sharing: 'perReport' })
}
const filledMounts = () => form.mounts.filter(mount => mount.volume.trim() || mount.path.trim())
const mountsPayload = (): SandboxMount[] => filledMounts().map(mount => ({
    volume: mount.volume.trim(),
    path: mount.path.trim(),
    read_only: mount.read_only,
    // Only a writable mount can be shared.
    shared: !mount.read_only && mount.sharing === 'shared',
}))
const mountsKey = (mounts: SandboxMount[]) =>
    JSON.stringify(mounts.map(mount => [mount.volume, mount.path, !!mount.read_only, !!mount.shared]))

// Which computes have the volume a row names, or that none does yet.
function mountNote(mount: MountRow) {
    const name = mount.volume.trim()
    if (!name) return ''
    const found = props.volumes.find(volume => volume.name === name)
    return found
        ? t('sandbox.environments.mounts.onComputes', { computes: found.computes.join(', ') })
        : t('sandbox.environments.mounts.unknownVolume')
}

const confirmKey = computed(() => (CONFIRM_RUNS.includes(form.confirm_runs) ? form.confirm_runs : 'never'))
const confirmOptions = computed(() => CONFIRM_RUNS.map(value => ({ value, label: t(`sandbox.environments.confirm.${value}`) })))
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
        confirm_runs: env.confirm_runs || 'never',
        setup_script: env.setup_script || '', start_script: env.start_script || '', stop_script: env.stop_script || '',
        compute_labels: (env.compute_labels || []).join(', '),
    } : {})
    mountErrors.value = {}
    form.mounts = (env?.mounts || []).map(mount => ({
        key: ++mountKey, volume: mount.volume, path: mount.path, read_only: mount.read_only !== false,
        sharing: mount.shared ? 'shared' : 'perReport',
    }))
    for (const type of ['role', 'group', 'user'] as PrincipalType[]) {
        access[type] = (env?.access || []).filter(grant => grant.principal_type === type).map(grant => grant.principal_id)
    }
})

// A validation refusal (422) lists each field it refused. What it says about a
// mount goes under that mount's row; the rest is returned as one line, or ''
// when everything was about mounts. `null`: not a validation refusal.
function mountValidation(error: unknown, sent: MountRow[]): string | null {
    const detail = (error as any)?.data?.detail
    if (!Array.isArray(detail)) return null
    const clean = (message: unknown) => String(message || '').replace(/^Value error, /, '')
    const rows: Record<number, string> = {}
    const other: string[] = []
    for (const item of detail) {
        const loc: unknown[] = Array.isArray(item?.loc) ? item.loc.filter((part: unknown) => part !== 'body') : []
        const row = loc[0] === 'mounts' && typeof loc[1] === 'number' ? sent[loc[1]] : undefined
        if (row) rows[row.key] = [rows[row.key], clean(item.msg)].filter(Boolean).join(' ')
        else other.push(loc.length ? `${loc.join('.')}: ${clean(item.msg)}` : clean(item.msg))
    }
    mountErrors.value = rows
    return other.slice(0, 5).join('; ')
}

async function save() {
    if (saving.value) return
    errorMessage.value = ''
    mountErrors.value = {}
    if (filledMounts().some(mount => !mount.volume.trim() || !mount.path.trim())) {
        errorMessage.value = t('sandbox.environments.mounts.incomplete')
        return
    }
    saving.value = true
    const sentMounts = filledMounts()
    const env = props.environment
    // An emptied script is sent as null; one that was never set is left alone.
    const script = (key: typeof HOOKS[number]) => form[key].trim() ? form[key] : null
    const body: Record<string, any> = {
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
        confirm_runs: form.confirm_runs,
        compute_labels: form.compute_labels.split(',').map(label => label.trim()).filter(Boolean),
    }
    for (const hook of HOOKS) {
        if (script(hook) !== (env?.[hook] || null)) body[hook] = script(hook)
    }
    // The whole list, and only when it changed.
    const mounts = mountsPayload()
    if (mountsKey(mounts) !== mountsKey(env?.mounts || [])) body.mounts = mounts
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
        // Not errors: things the admin should know about it as saved. They stay
        // on its row in the list and at the top of this form.
        for (const warning of saved.warnings || []) {
            toast.add({ title: t('sandbox.environments.toast.warning'), description: warning, color: 'amber', icon: 'i-heroicons-exclamation-triangle', timeout: 12000 })
        }
        open.value = false
    } catch (error) {
        const rest = mountValidation(error, sentMounts)
        errorMessage.value = rest === null
            ? getErrorMessage(error, t('sandbox.environments.toast.saveFailed'))
            : rest || t('sandbox.environments.mounts.fix')
        // The environment itself was saved and only the access change failed:
        // keep the list in step with the server.
        if (saved) emit('saved', saved)
    } finally {
        saving.value = false
    }
}
</script>
