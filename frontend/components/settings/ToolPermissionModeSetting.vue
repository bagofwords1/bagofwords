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
import { computed, ref } from 'vue'

interface PermissionValue {
    default_mode: 'ask' | 'auto'
    allow_all_enabled: boolean
    unattended: 'refuse' | 'settings' | 'allow'
}

const props = defineProps<{ feature: any, disabled?: boolean }>()
const emit = defineEmits<{ (e: 'updated', feature: any): void }>()
const { t } = useI18n()
const toast = useToast()
const saving = ref(false)

const value = computed<PermissionValue>(() => ({
    default_mode: 'ask',
    allow_all_enabled: false,
    unattended: 'refuse',
    ...(props.feature?.value || {}),
}))

const defaultModeOptions = computed(() => [
    { value: 'ask', label: t('prompt.permissionMode.modes.ask.label') },
    { value: 'auto', label: t('prompt.permissionMode.modes.auto.label') },
])
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
