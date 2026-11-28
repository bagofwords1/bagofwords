<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-xl' }">
        <div class="p-6 relative" data-testid="sandbox-compute-modal">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h3 class="text-lg font-semibold text-ink pe-8">{{ heading }}</h3>
            <p class="text-sm text-ink-muted">{{ subheading }}</p>
            <hr class="my-4 border-line" />

            <!-- Step 1: which kind of compute. -->
            <div v-if="step === 'type'" class="space-y-2" data-testid="sandbox-compute-types">
                <button
                    v-for="type in types"
                    :key="type.name"
                    type="button"
                    class="w-full text-start rounded-lg border border-line hover:border-line-strong hover:bg-surface-subtle px-4 py-3 transition-colors"
                    :data-testid="`sandbox-compute-type-${type.name}`"
                    @click="chooseType(type)"
                >
                    <div class="flex items-center gap-2">
                        <UIcon :name="typeIcon(type.name)" class="w-4 h-4 text-ink-muted shrink-0" />
                        <span class="text-sm font-medium text-ink">{{ type.title }}</span>
                    </div>
                    <p v-if="type.description" class="mt-1 text-xs text-ink-muted leading-relaxed">{{ type.description }}</p>
                </button>
                <p v-if="!types.length" class="text-sm text-ink-muted">{{ $t('sandbox.computes.form.noTypes') }}</p>
            </div>

            <!-- Step 2: the form. Common fields, then the type's own. -->
            <form v-else-if="step === 'form'" class="space-y-4" @submit.prevent="save">
                <div class="flex flex-col">
                    <label for="sandbox-compute-name" class="text-sm font-medium text-ink-2 mb-1.5">
                        {{ $t('sandbox.computes.form.name') }} <span class="text-red-600" aria-hidden="true">*</span>
                    </label>
                    <UInput
                        id="sandbox-compute-name"
                        v-model="form.name"
                        required
                        maxlength="120"
                        :placeholder="$t('sandbox.computes.form.namePlaceholder')"
                        data-testid="sandbox-compute-name"
                    />
                </div>

                <SandboxSchemaFields
                    v-if="selectedType"
                    v-model="form.config"
                    :schema="selectedType.config_schema"
                    id-prefix="sandbox-config"
                />
                <SandboxSchemaFields
                    v-if="selectedType && hasCredentialFields"
                    v-model="form.credentials"
                    :schema="selectedType.credentials_schema"
                    :stored-secrets="compute?.credentials_set || []"
                    id-prefix="sandbox-credentials"
                />

                <div class="flex flex-col">
                    <label for="sandbox-compute-labels" class="text-sm font-medium text-ink-2 mb-1.5">
                        {{ $t('sandbox.computes.form.labels') }}
                        <span class="font-normal text-ink-muted">{{ $t('common.optional') }}</span>
                    </label>
                    <UInput
                        id="sandbox-compute-labels"
                        v-model="form.labels"
                        dir="ltr"
                        placeholder="gpu, eu-west"
                        data-testid="sandbox-compute-labels"
                    />
                    <p class="mt-1.5 text-xs text-ink-muted">{{ $t('sandbox.computes.form.labelsHint') }}</p>
                </div>

                <!-- How much of the compute sandboxes may be given. -->
                <div data-testid="sandbox-compute-allocation">
                    <div class="text-sm font-semibold text-ink">{{ $t('sandbox.computes.form.allocation.title') }}</div>
                    <p class="mt-0.5 mb-3 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.computes.form.allocation.hint') }}</p>
                    <div class="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-4">
                        <div v-for="field in allocationFields" :key="field.key" class="flex flex-col">
                            <label :for="`sandbox-compute-${field.key}`" class="text-sm font-medium text-ink-2 mb-1.5">{{ field.label }}</label>
                            <UInput
                                :id="`sandbox-compute-${field.key}`"
                                v-model.number="form.allocation[field.key]"
                                type="number"
                                :min="field.min"
                                :max="field.max"
                                :step="field.step"
                                dir="ltr"
                                :data-testid="`sandbox-compute-${field.key}`"
                            />
                            <p class="mt-1.5 text-xs text-ink-muted leading-relaxed">
                                {{ field.hint }}
                                <span class="whitespace-nowrap">{{ $t('sandbox.computes.form.allocation.default', { value: field.default }) }}</span>
                            </p>
                        </div>
                        <div class="flex flex-col sm:col-span-2">
                            <label for="sandbox-compute-capacity" class="text-sm font-medium text-ink-2 mb-1.5">
                                {{ $t('sandbox.computes.form.capacity') }}
                            </label>
                            <UInput
                                id="sandbox-compute-capacity"
                                v-model.number="form.capacity"
                                type="number"
                                min="1"
                                max="1000"
                                dir="ltr"
                                class="sm:max-w-[10rem]"
                                data-testid="sandbox-compute-capacity"
                            />
                            <p class="mt-1.5 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.computes.form.capacityHint') }}</p>
                        </div>
                    </div>
                </div>

                <!-- Named storage on the compute that environments can mount. -->
                <div data-testid="sandbox-compute-volumes-form">
                    <div class="text-sm font-semibold text-ink">
                        {{ $t('sandbox.computes.volumes.title') }}
                        <span class="font-normal text-ink-muted">{{ $t('common.optional') }}</span>
                    </div>
                    <p class="mt-0.5 mb-3 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.computes.volumes.hint') }}</p>
                    <div v-if="form.volumes.length" class="space-y-3">
                        <div
                            v-for="(volume, index) in form.volumes"
                            :key="volume.key"
                            class="rounded-lg border border-line p-3"
                            data-testid="sandbox-compute-volume-row"
                        >
                            <div class="grid grid-cols-1 sm:grid-cols-[10rem_minmax(0,1fr)_auto] gap-3 items-start">
                                <div class="flex flex-col">
                                    <label :for="`sandbox-volume-name-${volume.key}`" class="text-xs font-medium text-ink-2 mb-1">{{ $t('sandbox.computes.volumes.name') }}</label>
                                    <UInput
                                        :id="`sandbox-volume-name-${volume.key}`"
                                        v-model="volume.name"
                                        dir="ltr"
                                        spellcheck="false"
                                        maxlength="40"
                                        placeholder="datasets"
                                        :color="volumeNameError(index) ? 'red' : undefined"
                                        :ui="{ base: 'font-mono' }"
                                        data-testid="sandbox-volume-name"
                                    />
                                </div>
                                <div class="flex flex-col min-w-0">
                                    <label :for="`sandbox-volume-source-${volume.key}`" class="text-xs font-medium text-ink-2 mb-1">{{ $t('sandbox.computes.volumes.source') }}</label>
                                    <UInput
                                        :id="`sandbox-volume-source-${volume.key}`"
                                        v-model="volume.source"
                                        dir="ltr"
                                        spellcheck="false"
                                        maxlength="500"
                                        placeholder="/mnt/datasets"
                                        :ui="{ base: 'font-mono' }"
                                        data-testid="sandbox-volume-source"
                                    />
                                </div>
                                <UButton
                                    type="button"
                                    color="gray"
                                    variant="ghost"
                                    size="xs"
                                    icon="i-heroicons-trash"
                                    class="sm:mt-6"
                                    :aria-label="$t('sandbox.computes.volumes.remove')"
                                    data-testid="sandbox-volume-remove"
                                    @click="form.volumes.splice(index, 1)"
                                />
                            </div>
                            <UInput
                                v-model="volume.description"
                                class="mt-3"
                                maxlength="300"
                                :placeholder="$t('sandbox.computes.volumes.descriptionPlaceholder')"
                                :aria-label="$t('sandbox.computes.volumes.description')"
                                data-testid="sandbox-volume-description"
                            />
                            <p v-if="volumeNameError(index)" class="mt-1.5 text-xs text-red-600 dark:text-red-400" data-testid="sandbox-volume-name-error">
                                {{ volumeNameError(index) }}
                            </p>
                        </div>
                    </div>
                    <UButton
                        type="button"
                        color="gray"
                        variant="soft"
                        size="xs"
                        icon="i-heroicons-plus"
                        :class="form.volumes.length ? 'mt-3' : ''"
                        :disabled="form.volumes.length >= 20"
                        data-testid="sandbox-volume-add"
                        @click="addVolume"
                    >
                        {{ $t('sandbox.computes.volumes.add') }}
                    </UButton>
                    <p v-if="form.volumes.length" class="mt-2 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.computes.volumes.sourceHint') }}</p>
                </div>

                <!-- What the compute type filled in itself (e.g. the generated
                     public key): shown, never editable. -->
                <SandboxReadonlyValues v-if="compute && selectedType" :schema="selectedType.config_schema" :config="compute.config" />

                <p v-if="errorMessage" class="text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-compute-error">
                    {{ errorMessage }}
                </p>

                <div class="flex justify-end gap-2 pt-2">
                    <UButton v-if="!compute" variant="ghost" color="gray" type="button" @click="step = 'type'">
                        {{ $t('common.back') }}
                    </UButton>
                    <UButton variant="ghost" color="gray" type="button" @click="open = false">
                        {{ $t('common.cancel') }}
                    </UButton>
                    <UButton type="submit" color="blue" :loading="saving" data-testid="sandbox-compute-save">
                        {{ compute ? $t('common.saveChanges') : $t('sandbox.computes.form.create') }}
                    </UButton>
                </div>
            </form>

            <!-- Step 3 (after creating): what the admin has to do next. -->
            <div v-else-if="step === 'created' && created" class="space-y-4" data-testid="sandbox-compute-created">
                <template v-if="created.config?.ssh_public_key">
                    <p class="text-sm text-ink-2 leading-relaxed">
                        {{ $t('sandbox.computes.form.publicKeyInstruction', { user: created.config.ssh_user || 'root' }) }}
                    </p>
                    <SandboxCopyBox :value="created.config.ssh_public_key" testid="sandbox-compute-public-key" />
                    <p class="text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.computes.form.publicKeyNext') }}</p>
                </template>
                <SandboxReadonlyValues v-else-if="selectedType" :schema="selectedType.config_schema" :config="created.config" />
                <div class="flex justify-end pt-2">
                    <UButton color="blue" data-testid="sandbox-compute-created-done" @click="open = false">
                        {{ $t('sandbox.computes.form.done') }}
                    </UButton>
                </div>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import {
    SANDBOX_ALLOCATION_DEFAULTS, SANDBOX_VOLUME_NAME, sandboxFieldType,
    type SandboxAllocation, type SandboxCompute, type SandboxComputeType, type SandboxFormSchema,
} from '~/composables/useSandboxApi'
import SandboxSchemaFields from '~/components/sandbox/SandboxSchemaFields.vue'
import SandboxReadonlyValues from '~/components/sandbox/SandboxReadonlyValues.vue'
import SandboxCopyBox from '~/components/sandbox/SandboxCopyBox.vue'

const props = defineProps<{
    modelValue: boolean
    types: SandboxComputeType[]
    // The compute being edited; null to add one.
    compute: SandboxCompute | null
}>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    saved: [compute: SandboxCompute]
}>()

const { t } = useI18n()
const { number } = useSandboxFormat()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage } = useErrorMessage()

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})

const step = ref<'type' | 'form' | 'created'>('type')
const selectedType = ref<SandboxComputeType | null>(null)
const created = ref<SandboxCompute | null>(null)
const saving = ref(false)
const errorMessage = ref('')
const form = reactive({
    name: '',
    labels: '',
    capacity: 20 as number | string,
    config: {} as Record<string, any>,
    credentials: {} as Record<string, any>,
    allocation: { ...SANDBOX_ALLOCATION_DEFAULTS } as Record<keyof SandboxAllocation, number | string>,
    volumes: [] as VolumeRow[],
})

// `key` only tells the rows apart while they are edited.
interface VolumeRow { key: number; name: string; source: string; description: string }
let volumeKey = 0
function addVolume() {
    form.volumes.push({ key: ++volumeKey, name: '', source: '', description: '' })
}

// A row left entirely empty is not sent.
const filledVolumes = () => form.volumes.filter(volume => volume.name.trim() || volume.source.trim() || volume.description.trim())

function volumeNameError(index: number) {
    const volume = form.volumes[index]
    const name = volume.name.trim()
    if (!name) return ''
    if (!SANDBOX_VOLUME_NAME.test(name)) return t('sandbox.computes.volumes.nameRule')
    if (form.volumes.some((other, at) => at !== index && other.name.trim() === name)) return t('sandbox.computes.volumes.nameTwice', { name })
    return ''
}

const volumesPayload = () => filledVolumes().map(volume => ({
    name: volume.name.trim(),
    source: volume.source.trim(),
    description: volume.description.trim() || null,
}))

// Limits as the server checks them (capacity.py: Allocation).
const allocationFields = computed(() => ([
    { key: 'cpu_overcommit', min: 1, max: 32, step: 'any' },
    { key: 'memory_overcommit', min: 1, max: 4, step: 'any' },
    { key: 'reserve_memory_mb', min: 0, max: 1048576, step: 1 },
    { key: 'reserve_disk_mb', min: 0, max: 104857600, step: 1 },
] as Array<{ key: keyof SandboxAllocation; min: number; max: number; step: number | 'any' }>).map(field => ({
    ...field,
    label: t(`sandbox.computes.form.allocation.${field.key}`),
    hint: t(`sandbox.computes.form.allocation.${field.key}Hint`),
    default: number(SANDBOX_ALLOCATION_DEFAULTS[field.key]),
})))

// What the policy was when the form opened: only the keys that differ are sent,
// so the server's default keeps applying to anything not touched.
const allocationAtOpen = (): SandboxAllocation => ({ ...SANDBOX_ALLOCATION_DEFAULTS, ...(props.compute?.allocation || {}) })
function allocationPayload() {
    const initial = allocationAtOpen()
    const out: Partial<SandboxAllocation> = {}
    for (const key of Object.keys(SANDBOX_ALLOCATION_DEFAULTS) as Array<keyof SandboxAllocation>) {
        const value = form.allocation[key]
        if (value === '' || value === null || value === undefined) continue
        if (Number(value) !== Number(initial[key])) out[key] = Number(value)
    }
    return out
}

const hasCredentialFields = computed(() =>
    Object.keys(selectedType.value?.credentials_schema?.properties || {}).length > 0
)

const heading = computed(() => {
    if (step.value === 'created') return t('sandbox.computes.form.createdTitle', { name: created.value?.name || '' })
    if (props.compute) return t('sandbox.computes.form.editTitle', { name: props.compute.name })
    return t('sandbox.computes.form.addTitle')
})
const subheading = computed(() => {
    if (step.value === 'type') return t('sandbox.computes.form.chooseType')
    if (step.value === 'created') return t('sandbox.computes.form.createdHint')
    return selectedType.value?.title || ''
})

function typeIcon(name: string) {
    return name === 'fake' ? 'i-heroicons-beaker' : 'i-heroicons-server'
}

// The form's starting values for a schema: what is stored when editing, the
// schema's defaults otherwise. Secrets always start empty.
function initialValues(schema: SandboxFormSchema, stored: Record<string, any> | null, secrets = false) {
    const values: Record<string, any> = {}
    for (const [key, prop] of Object.entries(schema?.properties || {})) {
        if (prop.readOnly) continue
        if (secrets || prop.secret) { values[key] = ''; continue }
        const value = stored && key in stored ? stored[key] : prop.default
        values[key] = sandboxFieldType(prop) === 'boolean' ? !!value : (value ?? '')
    }
    return values
}

function chooseType(type: SandboxComputeType) {
    selectedType.value = type
    form.config = initialValues(type.config_schema, props.compute?.config || null)
    form.credentials = initialValues(type.credentials_schema, null, true)
    errorMessage.value = ''
    step.value = 'form'
}

watch(() => props.modelValue, (isOpen) => {
    if (!isOpen) return
    created.value = null
    errorMessage.value = ''
    form.allocation = { ...allocationAtOpen() }
    form.volumes = (props.compute?.volumes || []).map(volume => ({
        key: ++volumeKey, name: volume.name, source: volume.source, description: volume.description || '',
    }))
    if (props.compute) {
        form.name = props.compute.name
        form.labels = (props.compute.labels || []).join(', ')
        form.capacity = props.compute.capacity
        const type = props.types.find(candidate => candidate.name === props.compute!.type)
        if (type) chooseType(type)
        else { selectedType.value = null; form.config = {}; form.credentials = {}; step.value = 'form' }
    } else {
        form.name = ''
        form.labels = ''
        form.capacity = 20
        selectedType.value = null
        step.value = 'type'
        // Nothing to choose between: go straight to the form.
        if (props.types.length === 1) chooseType(props.types[0])
    }
})

// The config to send: every input of the schema. An emptied optional field is
// sent as null (the stored value is replaced, not kept); an emptied number is
// left out so the type's default applies.
function configPayload() {
    const out: Record<string, any> = {}
    const schema = selectedType.value?.config_schema
    const required = new Set(schema?.required || [])
    for (const [key, prop] of Object.entries(schema?.properties || {})) {
        if (prop.readOnly) continue
        const type = sandboxFieldType(prop)
        let value = form.config[key]
        if (typeof value === 'string') value = value.trim()
        if (type === 'boolean') out[key] = !!value
        else if (value === '' || value === null || value === undefined) {
            if (type === 'string') out[key] = required.has(key) ? '' : null
        } else out[key] = value
    }
    return out
}

// Only the secrets that were typed: an empty input keeps what is stored.
function credentialsPayload() {
    const out: Record<string, any> = {}
    for (const [key, value] of Object.entries(form.credentials)) {
        if (typeof value === 'string' ? value.trim() : value) out[key] = value
    }
    return out
}

function labelsPayload() {
    return form.labels.split(',').map(label => label.trim()).filter(Boolean)
}

async function save() {
    if (saving.value) return
    errorMessage.value = ''
    // A volume needs both a valid name and where it is on the compute.
    const incomplete = filledVolumes().some(volume => !volume.name.trim() || !volume.source.trim())
    if (incomplete || form.volumes.some((_, index) => volumeNameError(index))) {
        errorMessage.value = t(incomplete ? 'sandbox.computes.volumes.incomplete' : 'sandbox.computes.volumes.fixNames')
        return
    }
    saving.value = true
    try {
        const credentials = credentialsPayload()
        const allocation = allocationPayload()
        if (props.compute) {
            const body: Record<string, any> = {
                name: form.name.trim(),
                labels: labelsPayload(),
                capacity: Number(form.capacity),
                // The whole list: a volume taken out here is removed.
                volumes: volumesPayload(),
            }
            if (Object.keys(allocation).length) body.allocation = allocation
            if (selectedType.value) body.config = configPayload()
            if (Object.keys(credentials).length) body.credentials = credentials
            const saved = await call<SandboxCompute>(`/computes/${props.compute.id}`, { method: 'PUT', body })
            toast.add({ title: t('sandbox.computes.toast.saved'), color: 'green' })
            emit('saved', saved)
            open.value = false
        } else {
            const saved = await call<SandboxCompute>('/computes', {
                method: 'POST',
                body: {
                    name: form.name.trim(),
                    type: selectedType.value!.name,
                    config: configPayload(),
                    credentials,
                    labels: labelsPayload(),
                    capacity: Number(form.capacity),
                    allocation,
                    volumes: volumesPayload(),
                },
            })
            emit('saved', saved)
            toast.add({ title: t('sandbox.computes.toast.created'), color: 'green' })
            // A generated public key has to be installed by hand, unless a
            // one-time password already let BoW install it.
            const transientUsed = Object.entries(selectedType.value?.credentials_schema?.properties || {})
                .some(([key, prop]) => prop.transient && credentials[key])
            if (saved.config?.ssh_public_key && !transientUsed) {
                created.value = saved
                step.value = 'created'
            } else {
                open.value = false
            }
        }
    } catch (error) {
        errorMessage.value = getErrorMessage(error, t('sandbox.computes.toast.saveFailed'))
    } finally {
        saving.value = false
    }
}
</script>
