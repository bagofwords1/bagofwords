<template>
    <div class="flex flex-col gap-4" data-testid="ai-setting-tool_permission_mode">
        <div>
            <div class="text-sm font-medium text-ink flex items-center">
                <Icon name="heroicons:shield-check" class="me-2 w-4 h-4 text-ink-muted" />
                {{ $t('settings.aiSettingsPage.permissionMode.title') }}
            </div>
            <p class="mt-1 text-sm text-ink-2 leading-relaxed">{{ $t('settings.aiSettingsPage.permissionMode.description') }}</p>
        </div>

        <div class="grid gap-4 sm:grid-cols-2">
            <label class="flex flex-col gap-1">
                <span class="text-sm text-ink">{{ $t('settings.aiSettingsPage.permissionMode.defaultMode') }}</span>
                <USelect
                    :model-value="value.default_mode"
                    :options="defaultModeOptions"
                    :disabled="disabled || saving"
                    data-testid="permission-default-mode"
                    @update:model-value="(v: string) => save({ default_mode: v })"
                />
            </label>
            <label class="flex flex-col gap-1">
                <span class="text-sm text-ink">{{ $t('settings.aiSettingsPage.permissionMode.unattended') }}</span>
                <USelect
                    :model-value="value.unattended"
                    :options="unattendedOptions"
                    :disabled="disabled || saving"
                    data-testid="permission-unattended"
                    @update:model-value="(v: string) => save({ unattended: v })"
                />
                <span class="text-xs text-ink-muted">{{ $t(`settings.aiSettingsPage.permissionMode.unattendedHints.${value.unattended}`) }}</span>
            </label>
            <label class="flex flex-col gap-1">
                <span class="text-sm text-ink">{{ $t('settings.aiSettingsPage.permissionMode.judgeModel') }}</span>
                <USelect
                    :model-value="value.judge_decision_model_id || ''"
                    :options="judgeModelOptions"
                    :disabled="disabled || saving"
                    data-testid="permission-judge-model"
                    @update:model-value="(v: string) => save({ judge_decision_model_id: v || null })"
                />
                <span class="text-xs text-ink-muted">{{ $t('settings.aiSettingsPage.permissionMode.judgeModelHint') }}</span>
            </label>
        </div>

        <div class="flex items-start justify-between gap-4 p-3 rounded-lg border border-amber-200 bg-amber-50/70 dark:border-amber-900/60 dark:bg-amber-950/40">
            <div class="min-w-0">
                <div class="text-sm font-medium text-ink flex items-center">
                    <Icon name="heroicons:shield-exclamation" class="me-2 w-4 h-4 text-amber-600 dark:text-amber-400" />
                    {{ $t('settings.aiSettingsPage.permissionMode.allowAll') }}
                </div>
                <p class="mt-1 text-sm text-ink-2 leading-relaxed">{{ $t('settings.aiSettingsPage.permissionMode.allowAllDescription') }}</p>
            </div>
            <UToggle
                :model-value="!!value.allow_all_enabled"
                :disabled="disabled || saving"
                data-testid="permission-allow-all"
                @update:model-value="(v: boolean) => save({ allow_all_enabled: v })"
            />
        </div>
    </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

interface PermissionValue {
    default_mode: 'ask' | 'auto' | 'allow_all'
    allow_all_enabled: boolean
    unattended: 'refuse' | 'settings' | 'allow'
    // null = Automatic; 'small' = the small default model only; else a decision model id.
    judge_decision_model_id: string | null
}

// Stored value that makes the Auto judge skip every decision model.
const JUDGE_SMALL_MODEL = 'small'

const props = defineProps<{ feature: any, disabled?: boolean }>()
const emit = defineEmits<{ (e: 'updated', feature: any): void }>()
const { t } = useI18n()
const toast = useToast()
const saving = ref(false)

const value = computed<PermissionValue>(() => ({
    default_mode: 'ask',
    allow_all_enabled: false,
    unattended: 'refuse',
    judge_decision_model_id: null,
    ...(props.feature?.value || {}),
}))

// Allow all is offered as the default only while its switch is on.
const defaultModeOptions = computed(() => [
    { value: 'ask', label: t('prompt.permissionMode.modes.ask.label') },
    { value: 'auto', label: t('prompt.permissionMode.modes.auto.label') },
    ...(value.value.allow_all_enabled ? [{ value: 'allow_all', label: t('prompt.permissionMode.modes.allow_all.label') }] : []),
])

// The org's enabled decision models (the same list the LLM settings page reads).
const decisionModels = ref<{ id: string, name: string }[]>([])
const decisionModelsLoaded = ref(false)
onMounted(async () => {
    try {
        const { data } = await useMyFetch('/llm/models', { method: 'GET', query: { kind: 'decision' } })
        const list = Array.isArray(data.value) ? (data.value as any[]) : []
        decisionModels.value = list.filter(m => m?.is_enabled !== false).map(m => ({ id: String(m.id), name: m.name || m.model_id }))
    } catch {} finally {
        decisionModelsLoaded.value = true
    }
})

// Automatic ('' in the select, null when saved), each decision model, the small
// model. A stored model that is no longer enabled shows as unavailable rather
// than pretending to be Automatic.
const judgeModelOptions = computed(() => {
    const stored = value.value.judge_decision_model_id
    const options = [
        { value: '', label: t('settings.aiSettingsPage.permissionMode.judgeModelOptions.automatic') },
        ...decisionModels.value.map(m => ({ value: m.id, label: m.name })),
        { value: JUDGE_SMALL_MODEL, label: t('settings.aiSettingsPage.permissionMode.judgeModelOptions.small') },
    ]
    if (stored && stored !== JUDGE_SMALL_MODEL && !decisionModels.value.some(m => m.id === stored)) {
        // Until the list arrives the stored id is shown without a name.
        options.push({ value: stored, label: decisionModelsLoaded.value
            ? t('settings.aiSettingsPage.permissionMode.judgeModelOptions.unavailable')
            : '' })
    }
    return options
})
const unattendedOptions = computed(() => [
    { value: 'refuse', label: t('settings.aiSettingsPage.permissionMode.unattendedOptions.refuse') },
    { value: 'settings', label: t('settings.aiSettingsPage.permissionMode.unattendedOptions.settings') },
    { value: 'allow', label: t('settings.aiSettingsPage.permissionMode.unattendedOptions.allow') },
])

// Partial update: the server merges it over the stored value and validates it.
async function save(patch: Partial<PermissionValue>) {
    saving.value = true
    try {
        const response = await useMyFetch('/api/organization/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: { tool_permission_mode: { value: patch } } }),
        })
        if (response.status.value !== 'success') {
            const data: any = response.error?.value?.data
            throw new Error((typeof data?.detail === 'string' && data.detail) || t('settings.aiSettingsPage.updateError'))
        }
        const updated = (response.data?.value as any)?.config?.tool_permission_mode
        if (updated) emit('updated', updated)
    } catch (e: any) {
        toast.add({ title: e?.message || t('settings.aiSettingsPage.updateError'), color: 'red' })
    } finally {
        saving.value = false
    }
}
</script>
