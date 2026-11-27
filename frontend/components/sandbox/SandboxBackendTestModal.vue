<template>
    <UModal v-model="open" :ui="{ width: 'sm:max-w-lg' }">
        <div class="p-6 relative" data-testid="sandbox-test-modal">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h3 class="text-lg font-semibold text-ink pe-8">{{ $t('sandbox.backends.test.title', { name: backend?.name || '' }) }}</h3>
            <p class="text-sm text-ink-muted">{{ $t('sandbox.backends.test.hint') }}</p>
            <hr class="my-4 border-line" />

            <div v-if="testing && !result" class="flex items-center justify-center gap-2 py-8 text-sm text-ink-muted" role="status">
                <Spinner class="w-4 h-4" />
                <span>{{ $t('sandbox.backends.test.running') }}</span>
            </div>

            <p v-else-if="requestError" class="text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-test-error">
                {{ requestError }}
            </p>

            <div v-else-if="result" class="space-y-4">
                <div
                    :class="['rounded-lg border px-4 py-3 flex items-start gap-3 text-sm', summary.class]"
                    data-testid="sandbox-test-summary"
                    :data-healthy="String(result.healthy)"
                >
                    <UIcon :name="summary.icon" class="w-4 h-4 shrink-0 mt-0.5" />
                    <div class="min-w-0">
                        <div class="font-medium">{{ summary.title }}</div>
                        <div v-if="result.error" dir="auto" class="text-xs mt-0.5 opacity-90 break-words">{{ result.error }}</div>
                    </div>
                </div>

                <!-- The server's identity is not trusted yet: nothing is sent to
                     it until the admin confirms the fingerprint. -->
                <div
                    v-if="unpinnedFingerprint"
                    class="rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 space-y-3"
                    data-testid="sandbox-host-key"
                >
                    <div class="flex items-start gap-3 text-sm text-amber-800 dark:text-amber-200">
                        <UIcon name="i-heroicons-finger-print" class="w-4 h-4 shrink-0 mt-0.5" />
                        <div class="min-w-0">
                            <div class="font-medium">{{ $t('sandbox.backends.test.hostKeyTitle') }}</div>
                            <p class="text-xs mt-0.5 leading-relaxed">{{ $t('sandbox.backends.test.hostKeyHint') }}</p>
                        </div>
                    </div>
                    <SandboxCopyBox :value="unpinnedFingerprint" testid="sandbox-host-key-fingerprint" />
                    <div class="flex justify-end">
                        <UButton color="amber" :loading="confirming" data-testid="sandbox-host-key-trust" @click="trust">
                            {{ $t('sandbox.backends.test.trust') }}
                        </UButton>
                    </div>
                </div>

                <SandboxStepList v-if="result.checks?.length" :items="result.checks" data-testid="sandbox-test-checks" />
            </div>

            <div class="flex justify-end gap-2 pt-5">
                <UButton variant="ghost" color="gray" @click="open = false">{{ $t('common.close') }}</UButton>
                <UButton color="gray" :loading="testing" :disabled="confirming" icon="i-heroicons-arrow-path" data-testid="sandbox-test-again" @click="run">
                    {{ $t('sandbox.backends.test.again') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import type { SandboxBackend } from '~/composables/useSandboxApi'
import Spinner from '~/components/Spinner.vue'
import SandboxStepList from '~/components/sandbox/SandboxStepList.vue'
import SandboxCopyBox from '~/components/sandbox/SandboxCopyBox.vue'

interface TestResult {
    reachable: boolean
    prepared: boolean
    healthy: boolean
    error: string | null
    checks: Array<{ key: string; title: string; status: string; detail?: string | null }>
    backend: SandboxBackend
}

const props = defineProps<{ modelValue: boolean; backend: SandboxBackend | null }>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    updated: [backend: SandboxBackend]
}>()

const { t } = useI18n()
const { call } = useSandboxApi()
const { getErrorMessage } = useErrorMessage()

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})

const testing = ref(false)
const confirming = ref(false)
const result = ref<TestResult | null>(null)
const requestError = ref('')

const unpinnedFingerprint = computed(() => {
    const info = result.value?.backend?.info
    return info && info.host_key_pinned === false && info.host_key_seen_fingerprint
        ? String(info.host_key_seen_fingerprint) : ''
})

const summary = computed(() => {
    const r = result.value
    if (r?.healthy) {
        return {
            class: 'border-green-200 dark:border-green-900/60 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-200',
            icon: 'i-heroicons-check-circle',
            title: t('sandbox.backends.test.healthy'),
        }
    }
    if (r?.reachable) {
        return {
            class: 'border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 text-amber-800 dark:text-amber-200',
            icon: 'i-heroicons-exclamation-triangle',
            title: r.prepared ? t('sandbox.backends.test.reachableUnhealthy') : t('sandbox.backends.test.reachableNotPrepared'),
        }
    }
    return {
        class: 'border-red-200 dark:border-red-900/60 bg-red-50 dark:bg-red-950/40 text-red-800 dark:text-red-200',
        icon: 'i-heroicons-x-circle',
        title: t('sandbox.backends.test.unreachable'),
    }
})

async function run() {
    if (!props.backend || testing.value) return
    testing.value = true
    requestError.value = ''
    try {
        const response = await call<TestResult>(`/backends/${props.backend.id}/test`, { method: 'POST' })
        result.value = response
        if (response.backend) emit('updated', response.backend)
    } catch (error) {
        result.value = null
        requestError.value = getErrorMessage(error, t('sandbox.backends.test.failed'))
    } finally {
        testing.value = false
    }
}

async function trust() {
    if (!props.backend || !unpinnedFingerprint.value || confirming.value) return
    confirming.value = true
    try {
        const backend = await call<SandboxBackend>(`/backends/${props.backend.id}/confirm-identity`, {
            method: 'POST',
            body: { fingerprint: unpinnedFingerprint.value },
        })
        emit('updated', backend)
    } catch (error) {
        requestError.value = getErrorMessage(error, t('sandbox.backends.test.trustFailed'))
        result.value = null
        confirming.value = false
        return
    }
    confirming.value = false
    await run()
}

watch(() => props.modelValue, (isOpen) => {
    if (!isOpen) return
    result.value = null
    requestError.value = ''
    run()
})
</script>
