<template>
    <!-- Manager: interactive dropdown for the agent lifecycle stage -->
    <UDropdown
        v-if="canManage"
        :items="items"
        :popper="{ placement: 'bottom-end' }"
        :ui="{ width: 'w-[26rem]', item: { padding: 'px-3 py-2' } }"
    >
        <button
            type="button"
            :disabled="saving"
            :class="[
                'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
                meta.badge,
                saving ? 'opacity-60 cursor-wait' : 'hover:brightness-95',
            ]"
        >
            <span :class="['w-1.5 h-1.5 rounded-full flex-shrink-0', meta.dot]" />
            {{ meta.label }}
            <UIcon name="heroicons-chevron-down" class="w-3 h-3 opacity-60" />
        </button>

        <template #item="{ item }">
            <div class="flex items-start gap-2 w-full text-left">
                <span :class="['mt-1 w-1.5 h-1.5 rounded-full flex-shrink-0', item.dot]" />
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-1.5">
                        <span class="text-sm text-gray-900 dark:text-white">{{ item.label }}</span>
                        <UIcon v-if="item.value === stage" name="heroicons-check" class="w-3.5 h-3.5 text-blue-600" />
                    </div>
                    <div class="text-[11px] text-gray-500 dark:text-gray-400 whitespace-normal leading-snug">{{ item.description }}</div>
                </div>
            </div>
        </template>
    </UDropdown>

    <!-- Non-manager: read-only badge (only meaningful when not plain Production) -->
    <span
        v-else-if="stage !== 'production'"
        :class="['inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium', meta.badge]"
    >
        <span :class="['w-1.5 h-1.5 rounded-full flex-shrink-0', meta.dot]" />
        {{ meta.label }}
    </span>
</template>

<script setup lang="ts">
import { useCan } from '~/composables/usePermissions'
import {
    deriveStage,
    stageWrite,
    stageMeta,
    STAGE_OPTIONS,
    type AgentStage,
} from '~/composables/useDataSourcePublishStatus'

const props = defineProps<{
    dataSourceId: string
    status: string                       // publish_status
    reliabilityStatus?: string           // ok | training | development
}>()

const emit = defineEmits<{ (e: 'updated', value: { publish_status: string; reliability_status?: string }): void }>()

const toast = useToast?.()
const saving = ref(false)

const canManage = computed(() => useCan('manage', { type: 'data_source', id: props.dataSourceId }))
const stage = computed<AgentStage>(() => deriveStage(props.status, props.reliabilityStatus))
const meta = computed(() => stageMeta(stage.value))

// UDropdown expects an array of groups (array of arrays).
const items = computed(() => [
    STAGE_OPTIONS.map((opt) => ({
        label: opt.label,
        description: opt.description,
        value: opt.value,
        dot: opt.dot,
        click: () => select(opt.value),
    })),
])

async function select(value: AgentStage) {
    if (saving.value || value === stage.value) return
    saving.value = true
    const body = stageWrite(value)
    const { error } = await useMyFetch(`/data_sources/${props.dataSourceId}`, { method: 'PUT', body })
    saving.value = false
    if (error?.value) {
        toast?.add?.({ title: 'Failed to update status', color: 'red' })
        return
    }
    toast?.add?.({ title: `Agent set to ${stageMeta(value).label}` })
    emit('updated', { publish_status: body.publish_status, reliability_status: body.reliability_status })
}
</script>
