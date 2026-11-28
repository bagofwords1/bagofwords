<template>
    <!-- Keep warm for some hours, or push a deletion later by some days: one
         number, a few usual choices, and what it means. -->
    <UModal v-model="open">
        <form class="p-6 relative" :data-testid="`sandbox-${mode}-modal`" @submit.prevent="submit">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h3 class="text-lg font-semibold text-ink pe-8">{{ $t(`sandbox.sandboxes.${mode}.title`) }}</h3>
            <p class="mt-0.5 text-sm text-ink-muted break-words" dir="auto">{{ sandbox?.report_title || $t('sandbox.sandboxes.untitledReport') }}</p>
            <p class="mt-3 text-sm text-ink-2 leading-relaxed">{{ $t(`sandbox.sandboxes.${mode}.hint`) }}</p>

            <div class="mt-4 flex flex-wrap items-center gap-2">
                <UButton
                    v-for="preset in PRESETS[mode]"
                    :key="preset"
                    type="button"
                    size="xs"
                    :color="Number(amount) === preset ? 'blue' : 'gray'"
                    :variant="Number(amount) === preset ? 'soft' : 'ghost'"
                    :data-testid="`sandbox-amount-preset-${preset}`"
                    @click="amount = preset"
                >
                    {{ $t(`sandbox.sandboxes.${mode}.preset`, { count: preset }, preset) }}
                </UButton>
            </div>
            <div class="mt-3 flex flex-col">
                <label for="sandbox-amount" class="text-sm font-medium text-ink-2 mb-1.5">{{ $t(`sandbox.sandboxes.${mode}.amount`) }}</label>
                <UInput
                    id="sandbox-amount"
                    v-model.number="amount"
                    type="number"
                    :min="LIMITS[mode].min"
                    :max="LIMITS[mode].max"
                    step="any"
                    required
                    dir="ltr"
                    class="sm:max-w-[10rem]"
                    data-testid="sandbox-amount"
                />
            </div>
            <p v-if="errorMessage" class="mt-3 text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-amount-error">
                {{ errorMessage }}
            </p>
            <div class="flex justify-end gap-2 pt-5">
                <UButton variant="ghost" color="gray" type="button" :disabled="working" @click="open = false">{{ $t('common.cancel') }}</UButton>
                <UButton type="submit" color="blue" :loading="working" data-testid="sandbox-amount-confirm">
                    {{ $t(`sandbox.sandboxes.${mode}.confirm`) }}
                </UButton>
            </div>
        </form>
    </UModal>
</template>

<script setup lang="ts">
import type { SandboxFleetRow } from '~/composables/useSandboxApi'

type Mode = 'keepWarm' | 'extend'

const props = defineProps<{ modelValue: boolean; sandbox: SandboxFleetRow | null; mode: Mode }>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    done: [row: SandboxFleetRow]
    gone: [sandbox: SandboxFleetRow]
}>()

const { t } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage, getErrorCode } = useErrorMessage()

// Hours to keep warm (the API takes up to a week), days to extend by (up to a year).
const PRESETS: Record<Mode, number[]> = { keepWarm: [1, 4, 8, 24], extend: [1, 7, 30] }
const LIMITS: Record<Mode, { min: number; max: number }> = { keepWarm: { min: 0.1, max: 168 }, extend: { min: 0.1, max: 365 } }
const DEFAULTS: Record<Mode, number> = { keepWarm: 4, extend: 7 }

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})
const amount = ref<number | string>(4)
const working = ref(false)
const errorMessage = ref('')

watch(() => props.modelValue, (isOpen) => {
    if (!isOpen) return
    amount.value = DEFAULTS[props.mode]
    errorMessage.value = ''
})

async function submit() {
    const sandbox = props.sandbox
    if (!sandbox || working.value) return
    working.value = true
    errorMessage.value = ''
    try {
        const row = props.mode === 'keepWarm'
            ? await call<SandboxFleetRow>(`/sandboxes/${sandbox.id}/keep-warm`, { method: 'POST', body: { hours: Number(amount.value) } })
            : await call<SandboxFleetRow>(`/sandboxes/${sandbox.id}/extend`, { method: 'POST', body: { days: Number(amount.value) } })
        toast.add({ title: t(`sandbox.sandboxes.${props.mode}.done`), color: 'green' })
        emit('done', row)
        open.value = false
    } catch (error) {
        if (getErrorCode(error) === 'sandbox.not_found') {
            emit('gone', sandbox)
            open.value = false
            toast.add({ title: getErrorMessage(error), color: 'amber' })
        } else {
            errorMessage.value = getErrorMessage(error, t(`sandbox.sandboxes.${props.mode}.failed`))
        }
    } finally {
        working.value = false
    }
}
</script>
