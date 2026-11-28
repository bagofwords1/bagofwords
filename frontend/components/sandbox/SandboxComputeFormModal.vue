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

                <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    <div class="flex flex-col sm:col-span-2">
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
                    <div class="flex flex-col">
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
                            data-testid="sandbox-compute-capacity"
                        />
                        <p class="mt-1.5 text-xs text-ink-muted">{{ $t('sandbox.computes.form.capacityHint') }}</p>
                    </div>
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
import { sandboxFieldType, type SandboxCompute, type SandboxComputeType, type SandboxFormSchema } from '~/composables/useSandboxApi'
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
})

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
    saving.value = true
    errorMessage.value = ''
    try {
        const credentials = credentialsPayload()
        if (props.compute) {
            const body: Record<string, any> = {
                name: form.name.trim(),
                labels: labelsPayload(),
                capacity: Number(form.capacity),
            }
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
