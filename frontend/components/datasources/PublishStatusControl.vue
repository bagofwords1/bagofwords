<template>
    <!-- Manager: interactive dropdown to change the publishing lifecycle -->
    <UDropdown
        v-if="canManage"
        :items="items"
        :popper="{ placement: 'bottom-end' }"
    >
        <button
            type="button"
            :disabled="saving"
            :class="[
                'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
                publishStatusBadgeClass(status),
                saving ? 'opacity-60 cursor-wait' : 'hover:brightness-95',
            ]"
        >
            <span :class="['w-1.5 h-1.5 rounded-full flex-shrink-0', publishStatusDotClass(status)]" />
            {{ publishStatusLabel(status) }}
            <UIcon name="heroicons-chevron-down" class="w-3 h-3 opacity-60" />
        </button>

        <template #item="{ item }">
            <div class="flex items-start gap-2 w-full text-left">
                <span :class="['mt-1 w-1.5 h-1.5 rounded-full flex-shrink-0', publishStatusDotClass(item.value)]" />
                <div class="min-w-0">
                    <div class="flex items-center gap-1.5">
                        <span class="text-sm text-gray-900">{{ item.label }}</span>
                        <UIcon
                            v-if="item.value === status"
                            name="heroicons-check"
                            class="w-3.5 h-3.5 text-blue-600"
                        />
                    </div>
                    <div class="text-[11px] text-gray-500 whitespace-normal">{{ item.description }}</div>
                </div>
            </div>
        </template>
    </UDropdown>

    <!-- Non-manager: read-only badge, only meaningful when not the default published -->
    <span
        v-else-if="status && status !== 'published'"
        :class="['inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium', publishStatusBadgeClass(status)]"
    >
        <span :class="['w-1.5 h-1.5 rounded-full flex-shrink-0', publishStatusDotClass(status)]" />
        {{ publishStatusLabel(status) }}
    </span>
</template>

<script setup lang="ts">
import { useCan } from '~/composables/usePermissions'
import {
    publishStatusBadgeClass,
    publishStatusDotClass,
    publishStatusLabel,
    publishStatusOptions,
    type PublishStatus,
} from '~/composables/useDataSourcePublishStatus'

const props = defineProps<{
    dataSourceId: string
    status: string
}>()

const emit = defineEmits<{ (e: 'updated', value: PublishStatus): void }>()

const toast = useToast?.()
const saving = ref(false)

const canManage = computed(() =>
    useCan('manage', { type: 'data_source', id: props.dataSourceId })
)

// UDropdown expects an array of groups (array of arrays).
const items = computed(() => [
    publishStatusOptions().map((opt) => ({
        label: opt.label,
        description: opt.description,
        value: opt.value,
        click: () => select(opt.value),
    })),
])

async function select(value: PublishStatus) {
    if (saving.value || value === props.status) return
    saving.value = true
    const { error } = await useMyFetch(`/data_sources/${props.dataSourceId}`, {
        method: 'PUT',
        body: { publish_status: value },
    })
    saving.value = false
    if (error?.value) {
        toast?.add?.({ title: 'Failed to update status', color: 'red' })
        return
    }
    toast?.add?.({ title: `Agent set to ${publishStatusLabel(value)}` })
    emit('updated', value)
}
</script>
