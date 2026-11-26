<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-lg' }">
        <div class="p-6 relative" data-testid="sandbox-bootstrap-modal" :data-status="status">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h3 class="text-lg font-semibold text-ink pe-8">
                {{ $t(wasReady ? 'sandbox.backends.bootstrap.repairTitle' : 'sandbox.backends.bootstrap.title', { name: current?.name || '' }) }}
            </h3>
            <p class="text-sm text-ink-muted">{{ $t('sandbox.backends.bootstrap.hint') }}</p>
            <hr class="my-4 border-line" />

            <div class="space-y-4">
                <div
                    v-if="banner"
                    :class="['rounded-lg border px-4 py-3 flex items-start gap-3 text-sm', banner.class]"
                    data-testid="sandbox-bootstrap-banner"
                >
                    <Spinner v-if="status === 'running'" class="w-4 h-4 shrink-0 mt-0.5" />
                    <UIcon v-else :name="banner.icon" class="w-4 h-4 shrink-0 mt-0.5" />
                    <div class="min-w-0">
                        <div class="font-medium">{{ banner.title }}</div>
                        <div v-if="banner.detail" dir="auto" class="text-xs mt-0.5 opacity-90 break-words" data-testid="sandbox-bootstrap-failure">{{ banner.detail }}</div>
                    </div>
                </div>

                <p v-if="requestError" class="text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-bootstrap-error">
                    {{ requestError }}
                </p>

                <SandboxStepList v-if="steps.length" :items="steps" data-testid="sandbox-bootstrap-steps" />
                <p v-else-if="status !== 'running'" class="text-sm text-ink-muted">{{ $t('sandbox.backends.bootstrap.notRun') }}</p>

                <!-- The server belongs to another BoW instance: taking it over
                     removes that instance's sandboxes, so it is asked twice. -->
                <div
                    v-if="ownedByOther && status === 'failed'"
                    class="rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 space-y-3"
                    data-testid="sandbox-takeover"
                >
                    <div class="flex items-start gap-3 text-sm text-amber-800 dark:text-amber-200">
                        <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 shrink-0 mt-0.5" />
                        <div class="min-w-0">
                            <div class="font-medium">{{ $t('sandbox.backends.bootstrap.takeOverTitle') }}</div>
                            <p class="text-xs mt-0.5 leading-relaxed">{{ $t('sandbox.backends.bootstrap.takeOverHint') }}</p>
                        </div>
                    </div>
                    <div class="flex justify-end gap-2">
                        <template v-if="confirmingTakeOver">
                            <UButton variant="ghost" color="gray" @click="confirmingTakeOver = false">{{ $t('common.cancel') }}</UButton>
                            <UButton color="red" :loading="starting" data-testid="sandbox-takeover-confirm" @click="start(true)">
                                {{ $t('sandbox.backends.bootstrap.takeOverConfirm') }}
                            </UButton>
                        </template>
                        <UButton v-else color="amber" data-testid="sandbox-takeover-button" @click="confirmingTakeOver = true">
                            {{ $t('sandbox.backends.bootstrap.takeOver') }}
                        </UButton>
                    </div>
                </div>
            </div>

            <div class="flex justify-end gap-2 pt-5">
                <UButton variant="ghost" color="gray" @click="open = false">{{ $t('common.close') }}</UButton>
                <UButton
                    v-if="status !== 'running'"
                    color="blue"
                    :loading="starting"
                    data-testid="sandbox-bootstrap-start"
                    @click="start(false)"
                >
                    {{ startLabel }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import type { SandboxBackend } from '~/composables/useSandboxApi'
import Spinner from '~/components/Spinner.vue'
import SandboxStepList from '~/components/sandbox/SandboxStepList.vue'

const POLL_MS = 1500

const props = defineProps<{
    modelValue: boolean
    backend: SandboxBackend | null
    // Start a bootstrap as the dialog opens (the row's Bootstrap button), rather
    // than only showing the last run (the row's status, or Repair).
    autoStart?: boolean
}>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    updated: [backend: SandboxBackend]
}>()

const { t } = useI18n()
const { call } = useSandboxApi()
const { getErrorMessage, getErrorCode } = useErrorMessage()

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})

const current = ref<SandboxBackend | null>(null)
const starting = ref(false)
const requestError = ref('')
const confirmingTakeOver = ref(false)
// Whether the backend had been bootstrapped before this dialog opened: the
// action is then a repair.
const wasReady = ref(false)

const status = computed(() => current.value?.bootstrap_status || 'none')
const steps = computed(() => current.value?.bootstrap_steps || [])
const failedStep = computed(() => steps.value.find(step => step.status === 'failed') || null)

// A failed step carries a code saying why. "owned_by_other" means the server
// belongs to another BoW instance, which is what offers Take over.
const ownedByOther = computed(() => failedStep.value?.code === 'owned_by_other')

const banner = computed(() => {
    if (status.value === 'running') {
        return {
            class: 'border-blue-200 dark:border-blue-900/60 bg-blue-50 dark:bg-blue-950/40 text-blue-800 dark:text-blue-200',
            icon: '',
            title: t('sandbox.backends.bootstrap.running'),
            detail: '',
        }
    }
    if (status.value === 'ready') {
        return {
            class: 'border-green-200 dark:border-green-900/60 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-200',
            icon: 'i-heroicons-check-circle',
            title: t('sandbox.backends.bootstrap.ready'),
            detail: current.value?.enabled ? '' : t('sandbox.backends.bootstrap.readyHint'),
        }
    }
    if (status.value === 'failed') {
        return {
            class: 'border-red-200 dark:border-red-900/60 bg-red-50 dark:bg-red-950/40 text-red-800 dark:text-red-200',
            icon: 'i-heroicons-x-circle',
            title: failedStep.value
                ? t('sandbox.backends.bootstrap.failedAt', { step: failedStep.value.title })
                : t('sandbox.backends.bootstrap.failed'),
            detail: failedStep.value?.detail || current.value?.last_error || '',
        }
    }
    return null
})

const startLabel = computed(() => {
    if (status.value === 'failed') return t('sandbox.backends.bootstrap.retry')
    if (wasReady.value || status.value === 'ready') return t('sandbox.backends.bootstrap.runRepair')
    return t('sandbox.backends.bootstrap.start')
})

let timer: ReturnType<typeof setTimeout> | undefined
function stopPolling() {
    clearTimeout(timer)
    timer = undefined
}

async function poll() {
    stopPolling()
    if (!open.value || !current.value) return
    try {
        const backend = await call<SandboxBackend>(`/backends/${current.value.id}`)
        if (!open.value) return
        current.value = backend
        requestError.value = ''
        if (backend.bootstrap_status !== 'running') {
            emit('updated', backend)
            return
        }
    } catch (error) {
        // A dropped poll is not the bootstrap failing: say so and keep asking.
        requestError.value = getErrorMessage(error, t('sandbox.backends.bootstrap.pollFailed'))
    }
    timer = setTimeout(poll, POLL_MS)
}

async function start(takeOver: boolean) {
    if (!current.value || starting.value) return
    starting.value = true
    requestError.value = ''
    confirmingTakeOver.value = false
    try {
        const backend = await call<SandboxBackend>(`/backends/${current.value.id}/bootstrap`, {
            method: 'POST',
            body: takeOver ? { take_over: true } : {},
        })
        current.value = backend
        emit('updated', backend)
    } catch (error) {
        // One is running already (another tab, another admin): follow that one.
        if (getErrorCode(error) !== 'sandbox.bootstrap_running') {
            requestError.value = getErrorMessage(error, t('sandbox.backends.bootstrap.startFailed'))
            starting.value = false
            return
        }
        current.value = { ...current.value, bootstrap_status: 'running' }
    }
    starting.value = false
    if (current.value?.bootstrap_status === 'running') timer = setTimeout(poll, POLL_MS)
}

watch(() => props.modelValue, (isOpen) => {
    stopPolling()
    if (!isOpen || !props.backend) return
    current.value = { ...props.backend }
    wasReady.value = props.backend.bootstrap_status === 'ready' || !!props.backend.bootstrapped_at
    requestError.value = ''
    confirmingTakeOver.value = false
    if (props.backend.bootstrap_status === 'running') poll()
    else if (props.autoStart) start(false)
})
onBeforeUnmount(stopPolling)
</script>
