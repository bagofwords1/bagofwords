<template>
    <UModal v-model="open">
        <div class="p-6 relative" data-testid="sandbox-delete-sandbox-modal">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h3 class="text-lg font-semibold text-ink pe-8">{{ $t('sandbox.sandboxes.delete.title') }}</h3>
            <p class="mt-1 text-sm text-ink-2 break-words" dir="auto" data-testid="sandbox-delete-report">
                {{ sandbox?.report_title || $t('sandbox.sandboxes.untitledReport') }}
            </p>

            <ul class="mt-4 space-y-2 text-sm text-ink-muted leading-relaxed">
                <li class="flex items-start gap-2">
                    <UIcon name="i-heroicons-trash" class="w-4 h-4 shrink-0 mt-0.5 text-red-600 dark:text-red-400" />
                    <span>{{ $t('sandbox.sandboxes.delete.filesLost') }}</span>
                </li>
                <li class="flex items-start gap-2">
                    <UIcon name="i-heroicons-check-circle" class="w-4 h-4 shrink-0 mt-0.5 text-green-700 dark:text-green-400" />
                    <span>{{ $t('sandbox.sandboxes.delete.filesKept') }}</span>
                </li>
                <li v-if="sandbox?.state !== 'busy'" class="flex items-start gap-2">
                    <UIcon name="i-heroicons-stop-circle" class="w-4 h-4 shrink-0 mt-0.5" />
                    <span>{{ $t('sandbox.sandboxes.delete.runStopped') }}</span>
                </li>
            </ul>

            <!-- A run is in progress right now: deleting ends it. -->
            <div
                v-if="sandbox?.state === 'busy'"
                class="mt-4 rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 flex items-start gap-3 text-sm text-amber-800 dark:text-amber-200"
                data-testid="sandbox-delete-busy"
            >
                <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 shrink-0 mt-0.5" />
                <div class="min-w-0">{{ $t('sandbox.sandboxes.delete.runInProgress') }}</div>
            </div>

            <p v-if="errorMessage" class="mt-4 text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-delete-sandbox-error">
                {{ errorMessage }}
            </p>

            <div class="flex justify-end gap-2 pt-5">
                <UButton variant="ghost" color="gray" :disabled="deleting" @click="open = false">{{ $t('common.cancel') }}</UButton>
                <UButton color="red" :loading="deleting" data-testid="sandbox-delete-sandbox-confirm" @click="remove">
                    {{ $t('sandbox.sandboxes.delete.confirm') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import type { SandboxFleetRow } from '~/composables/useSandboxApi'

const props = defineProps<{ modelValue: boolean; sandbox: SandboxFleetRow | null }>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    deleted: [sandbox: SandboxFleetRow]
}>()

const { t } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage, getErrorCode } = useErrorMessage()

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})

const deleting = ref(false)
const errorMessage = ref('')

async function remove() {
    const sandbox = props.sandbox
    if (!sandbox || deleting.value) return
    deleting.value = true
    errorMessage.value = ''
    try {
        await call(`/sandboxes/${sandbox.id}`, { method: 'DELETE' })
        toast.add({ title: t('sandbox.sandboxes.toast.deleted'), color: 'green' })
        emit('deleted', sandbox)
        open.value = false
    } catch (error) {
        if (getErrorCode(error) === 'sandbox.not_found') {
            // Someone else deleted it first: the outcome asked for.
            emit('deleted', sandbox)
            open.value = false
        } else {
            errorMessage.value = getErrorMessage(error, t('sandbox.sandboxes.toast.deleteFailed'))
        }
    } finally {
        deleting.value = false
    }
}

watch(() => props.modelValue, (isOpen) => {
    if (isOpen) errorMessage.value = ''
})
</script>
