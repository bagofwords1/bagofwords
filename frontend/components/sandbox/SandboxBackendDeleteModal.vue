<template>
    <UModal v-model="open">
        <div class="p-6 relative" data-testid="sandbox-delete-backend-modal">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h3 class="text-lg font-semibold text-ink pe-8">{{ $t('sandbox.backends.delete.title', { name: backend?.name || '' }) }}</h3>
            <p class="mt-1 text-sm text-ink-muted leading-relaxed">{{ $t('sandbox.backends.delete.hint') }}</p>

            <div class="mt-4">
                <UCheckbox v-model="cleanup" :disabled="deleting" data-testid="sandbox-delete-cleanup">
                    <template #label>
                        <span class="text-sm text-ink">{{ $t('sandbox.backends.delete.cleanup') }}</span>
                    </template>
                    <template #help>
                        <span class="text-xs text-ink-muted">{{ $t('sandbox.backends.delete.cleanupHint') }}</span>
                    </template>
                </UCheckbox>
            </div>

            <!-- Refused: the backend still has sandboxes. Deleting them with it
                 is a second, explicit choice. -->
            <div
                v-if="inUse"
                class="mt-4 rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 flex items-start gap-3 text-sm text-amber-800 dark:text-amber-200"
                data-testid="sandbox-delete-in-use"
            >
                <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 shrink-0 mt-0.5" />
                <div class="min-w-0">
                    <div class="font-medium">{{ inUse }}</div>
                    <p class="text-xs mt-0.5 leading-relaxed">{{ $t('sandbox.backends.delete.inUseHint') }}</p>
                </div>
            </div>
            <p v-else-if="errorMessage" class="mt-4 text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-delete-error">
                {{ errorMessage }}
            </p>

            <div class="flex justify-end gap-2 pt-5">
                <UButton variant="ghost" color="gray" :disabled="deleting" @click="open = false">{{ $t('common.cancel') }}</UButton>
                <UButton
                    v-if="inUse"
                    color="red"
                    :loading="deleting"
                    data-testid="sandbox-delete-with-sandboxes"
                    @click="remove(true)"
                >
                    {{ $t('sandbox.backends.delete.withSandboxes') }}
                </UButton>
                <UButton v-else color="red" :loading="deleting" data-testid="sandbox-delete-confirm" @click="remove(false)">
                    {{ $t('sandbox.backends.delete.confirm') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import type { SandboxBackend } from '~/composables/useSandboxApi'

const props = defineProps<{ modelValue: boolean; backend: SandboxBackend | null }>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    deleted: [backend: SandboxBackend]
}>()

const { t } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage, getErrorCode } = useErrorMessage()

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})

const cleanup = ref(false)
const deleting = ref(false)
const inUse = ref('')
const errorMessage = ref('')

async function remove(withSandboxes: boolean) {
    if (!props.backend || deleting.value) return
    deleting.value = true
    errorMessage.value = ''
    try {
        const query: Record<string, string> = {}
        if (cleanup.value) query.cleanup = 'true'
        if (withSandboxes) query.delete_sandboxes = 'true'
        await call(`/backends/${props.backend.id}`, { method: 'DELETE', query })
        toast.add({ title: t('sandbox.backends.toast.deleted'), color: 'green' })
        emit('deleted', props.backend)
        open.value = false
    } catch (error) {
        const message = getErrorMessage(error, t('sandbox.backends.toast.deleteFailed'))
        if (getErrorCode(error) === 'sandbox.backend_in_use' && !withSandboxes) {
            inUse.value = message
        } else {
            inUse.value = ''
            errorMessage.value = message
        }
    } finally {
        deleting.value = false
    }
}

watch(() => props.modelValue, (isOpen) => {
    if (!isOpen) return
    cleanup.value = false
    inUse.value = ''
    errorMessage.value = ''
})
</script>
