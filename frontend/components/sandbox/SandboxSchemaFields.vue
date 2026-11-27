<template>
    <!-- A backend type's form, rendered from the JSON schema the API hands out
         (GET /sandbox/backend-types): one input per property. Read-only
         properties are never inputs; secrets are never prefilled. -->
    <div class="space-y-4">
        <div v-for="field in fields" :key="field.key" class="flex flex-col" :data-testid="`sandbox-field-${field.key}`">
            <!-- Boolean: a switch beside its label. -->
            <div v-if="field.type === 'boolean'" class="flex items-start justify-between gap-6">
                <div class="min-w-0">
                    <label :for="inputId(field.key)" class="text-sm font-medium text-ink-2">{{ field.title }}</label>
                    <p v-if="field.description" class="mt-0.5 text-xs text-ink-muted leading-relaxed">{{ field.description }}</p>
                </div>
                <UToggle
                    :id="inputId(field.key)"
                    :model-value="!!modelValue[field.key]"
                    :data-testid="`sandbox-input-${field.key}`"
                    class="shrink-0 mt-0.5"
                    @update:model-value="set(field.key, $event)"
                />
            </div>

            <template v-else>
                <label :for="inputId(field.key)" class="flex flex-wrap items-center gap-1.5 text-sm font-medium text-ink-2 mb-1.5">
                    {{ field.title }}
                    <span v-if="field.required" class="text-red-600" aria-hidden="true">*</span>
                    <UBadge v-if="field.stored" size="xs" color="green" variant="subtle" :data-testid="`sandbox-stored-${field.key}`">
                        {{ $t('sandbox.backends.form.stored') }}
                    </UBadge>
                    <UBadge v-if="field.transient" size="xs" color="gray" variant="subtle">
                        {{ $t('sandbox.backends.form.transient') }}
                    </UBadge>
                </label>
                <UTextarea
                    v-if="field.secret && field.multiline"
                    :id="inputId(field.key)"
                    :model-value="modelValue[field.key] ?? ''"
                    :rows="4"
                    dir="ltr"
                    autocomplete="off"
                    spellcheck="false"
                    :placeholder="field.stored ? $t('sandbox.backends.form.storedPlaceholder') : ''"
                    :data-testid="`sandbox-input-${field.key}`"
                    :ui="{ base: 'font-mono text-xs' }"
                    @update:model-value="set(field.key, $event)"
                />
                <UInput
                    v-else-if="field.secret"
                    :id="inputId(field.key)"
                    :model-value="modelValue[field.key] ?? ''"
                    type="password"
                    dir="ltr"
                    autocomplete="new-password"
                    :placeholder="field.stored ? $t('sandbox.backends.form.storedPlaceholder') : ''"
                    :data-testid="`sandbox-input-${field.key}`"
                    @update:model-value="set(field.key, $event)"
                />
                <UInput
                    v-else-if="field.type === 'integer' || field.type === 'number'"
                    :id="inputId(field.key)"
                    :model-value="modelValue[field.key] ?? ''"
                    type="number"
                    dir="ltr"
                    :min="field.minimum"
                    :max="field.maximum"
                    class="sm:max-w-[12rem]"
                    :data-testid="`sandbox-input-${field.key}`"
                    @update:model-value="setNumber(field.key, $event)"
                />
                <UInput
                    v-else
                    :id="inputId(field.key)"
                    :model-value="modelValue[field.key] ?? ''"
                    dir="ltr"
                    autocomplete="off"
                    spellcheck="false"
                    :data-testid="`sandbox-input-${field.key}`"
                    @update:model-value="set(field.key, $event)"
                />
                <p v-if="field.description" class="mt-1.5 text-xs text-ink-muted leading-relaxed">{{ field.description }}</p>
            </template>
        </div>
    </div>
</template>

<script setup lang="ts">
import { sandboxFieldType, type SandboxFormSchema } from '~/composables/useSandboxApi'

const props = defineProps<{
    schema: SandboxFormSchema
    modelValue: Record<string, any>
    // Names of the secrets already stored on the backend being edited.
    storedSecrets?: string[]
    idPrefix?: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, any>] }>()

const fields = computed(() => {
    const required = new Set(props.schema?.required || [])
    return Object.entries(props.schema?.properties || {})
        .filter(([, prop]) => !prop.readOnly)
        .map(([key, prop]) => ({
            key,
            title: prop.title || key,
            description: prop.description || '',
            type: sandboxFieldType(prop),
            required: required.has(key),
            secret: !!prop.secret,
            multiline: !!prop.multiline,
            transient: !!prop.transient,
            minimum: prop.minimum,
            maximum: prop.maximum,
            stored: !!prop.secret && (props.storedSecrets || []).includes(key),
        }))
})

const inputId = (key: string) => `${props.idPrefix || 'sandbox'}-${key}`

function set(key: string, value: any) {
    emit('update:modelValue', { ...props.modelValue, [key]: value })
}
function setNumber(key: string, value: any) {
    set(key, value === '' || value === null ? '' : Number(value))
}
</script>
