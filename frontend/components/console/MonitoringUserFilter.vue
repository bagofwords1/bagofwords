<template>
    <div class="flex items-center gap-2">
        <span class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('monitoring.diagnosis.filterUserLabel') }}:</span>
        <USelectMenu
            :model-value="selectedOption"
            :options="userOptions"
            :searchable="userOptions.length > 8"
            :search-attributes="['label']"
            size="sm"
            class="min-w-[160px]"
            @update:model-value="onChange"
        />
    </div>
</template>

<script setup lang="ts">
interface UserOption {
    label: string
    value: string
}

interface AgentExecutionUser {
    id: string
    name: string
    email?: string
    execution_count: number
}

const props = defineProps<{
    // Currently selected user id ('' means all users)
    modelValue: string
}>()

const emit = defineEmits<{
    'update:modelValue': [userId: string]
}>()

const { t } = useI18n()

const users = ref<AgentExecutionUser[]>([])

// First option clears the filter; the rest are the users who ran executions.
const userOptions = computed<UserOption[]>(() => [
    { label: t('monitoring.diagnosis.filterUserAll'), value: '' },
    ...users.value.map((u) => ({
        label: u.name || u.email || t('monitoring.diagnosis.unknownUser'),
        value: u.id,
    })),
])

const selectedOption = computed<UserOption>(() =>
    userOptions.value.find((o) => o.value === props.modelValue) || userOptions.value[0]
)

const onChange = (option: UserOption) => {
    emit('update:modelValue', option?.value ?? '')
}

onMounted(async () => {
    try {
        const res = await useMyFetch<{ items: AgentExecutionUser[] }>('/api/console/agent_executions/users')
        if (res.data.value?.items) {
            users.value = res.data.value.items
        }
    } catch (error) {
        console.error('Failed to load monitoring users:', error)
    }
})
</script>
