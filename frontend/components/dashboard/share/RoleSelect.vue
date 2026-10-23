<template>
    <!-- "Can view" / "Can edit" for one person or group on a dashboard
         (collab01). "Can view" is off before the first publish and while the
         dashboard is open to the whole organization or the link: viewers
         are then given by its visibility, not by a list. -->
    <USelectMenu
        :model-value="modelValue"
        :options="options"
        value-attribute="value"
        option-attribute="label"
        size="xs"
        :disabled="disabled"
        :aria-label="$t('dashboardCollab.share.role')"
        :popper="{ placement: 'bottom-end' }"
        :ui="{ rounded: 'rounded-lg', size: { xs: 'text-xs' }, padding: { xs: 'ps-2 pe-7 py-1' } }"
        :ui-menu="{ width: 'w-52' }"
        class="w-28 shrink-0"
        data-testid="share-role-select"
        @update:model-value="(v: Role) => emit('update:modelValue', v)"
    >
        <template #option="{ option }">
            <div class="flex flex-col py-0.5">
                <span class="text-xs">{{ option.label }}</span>
                <span v-if="option.hint" class="text-[11px] text-gray-400 whitespace-normal">{{ option.hint }}</span>
            </div>
        </template>
    </USelectMenu>
</template>

<script setup lang="ts">
type Role = 'view' | 'edit'

const props = defineProps<{
    modelValue: Role
    // Why "Can view" cannot be picked right now; none: it can.
    viewDisabledReason?: string | null
    disabled?: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', value: Role): void }>()

const { t } = useI18n()

const options = computed(() => [
    {
        value: 'view',
        label: t('dashboardCollab.share.canView'),
        hint: props.viewDisabledReason || '',
        disabled: !!props.viewDisabledReason,
    },
    { value: 'edit', label: t('dashboardCollab.share.canEdit'), hint: '', disabled: false },
])
</script>
