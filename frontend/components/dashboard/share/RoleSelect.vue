<template>
    <!-- "Can view" / "Can edit" for one person or group on a dashboard
         (collab01), and, on a row of the access list, "Remove access" in the
         same menu as Google Docs has it. "Can view" is off before the first
         publish and while the dashboard is open to the whole organization or
         the link: viewers are then given by its general access, not a list. -->
    <USelectMenu
        :model-value="modelValue"
        :options="options"
        value-attribute="value"
        option-attribute="label"
        :variant="boxed ? 'outline' : 'none'"
        size="sm"
        :disabled="disabled"
        :aria-label="$t('dashboardCollab.share.role')"
        :popper="{ placement: 'bottom-end' }"
        :ui="{ rounded: 'rounded-md', padding: { sm: 'ps-2.5 pe-8 py-1.5' } }"
        :ui-menu="{ width: 'w-60' }"
        :class="boxed ? 'shrink-0' : 'shrink-0 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800'"
        data-testid="share-role-select"
        @update:model-value="onPick"
    >
        <template #label>
            <span class="text-sm text-gray-700 dark:text-gray-300 whitespace-nowrap">{{ current?.label }}</span>
        </template>
        <template #option="{ option }">
            <div class="flex flex-col py-0.5 w-full"
                :data-testid="option.value === REMOVE ? 'share-remove' : undefined">
                <span class="text-sm" :class="option.value === REMOVE ? 'text-red-600 dark:text-red-400' : ''">{{ option.label }}</span>
                <span v-if="option.hint" class="text-xs text-gray-500 whitespace-normal">{{ option.hint }}</span>
            </div>
        </template>
    </USelectMenu>
</template>

<script setup lang="ts">
type Role = 'view' | 'edit'
const REMOVE = '__remove__'

const props = defineProps<{
    modelValue: Role
    // Why "Can view" cannot be picked right now; none: it can.
    viewDisabledReason?: string | null
    disabled?: boolean
    // A row of the access list: the menu ends with "Remove access".
    removable?: boolean
    // Next to the add-people box: drawn as a field, not as text.
    boxed?: boolean
}>()
const emit = defineEmits<{
    (e: 'update:modelValue', value: Role): void
    (e: 'remove'): void
}>()

const { t } = useI18n()

const options = computed(() => [
    {
        value: 'view',
        label: t('dashboardCollab.share.canView'),
        hint: props.viewDisabledReason || '',
        disabled: !!props.viewDisabledReason,
    },
    { value: 'edit', label: t('dashboardCollab.share.canEdit'), hint: '', disabled: false },
    ...(props.removable ? [{ value: REMOVE, label: t('dashboardCollab.share.removeAccess'), hint: '', disabled: false }] : []),
])
const current = computed(() => options.value.find(o => o.value === props.modelValue))

function onPick(v: string) {
    if (v === REMOVE) emit('remove')
    else emit('update:modelValue', v as Role)
}
</script>
