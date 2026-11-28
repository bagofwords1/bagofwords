<template>
    <UModal v-model="open" :prevent-close="working">
        <div class="p-6 relative" data-testid="sandbox-evacuate-modal">
            <button
                type="button"
                class="absolute top-4 end-4 text-ink-muted hover:text-ink outline-none"
                :aria-label="$t('common.close')"
                :disabled="working"
                @click="open = false"
            >
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h3 class="text-lg font-semibold text-ink pe-8">{{ $t(result ? 'sandbox.computes.evacuate.doneTitle' : 'sandbox.computes.evacuate.title', { name: compute?.name || '' }) }}</h3>

            <!-- Before: what it does. -->
            <template v-if="!result">
                <p class="mt-1 text-sm text-ink-muted leading-relaxed">{{ $t('sandbox.computes.evacuate.hint') }}</p>
                <ul class="mt-3 space-y-1.5 text-sm text-ink-2 list-disc ps-5 leading-relaxed" data-testid="sandbox-evacuate-explain">
                    <li>{{ $t('sandbox.computes.evacuate.stepSnapshot') }}</li>
                    <li>{{ $t('sandbox.computes.evacuate.stepRecreate') }}</li>
                    <li>{{ $t('sandbox.computes.evacuate.stepLeft') }}</li>
                </ul>
                <p
                    v-if="compute?.enabled"
                    class="mt-4 rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 flex items-start gap-3 text-sm text-amber-800 dark:text-amber-200"
                    data-testid="sandbox-evacuate-needs-disable"
                >
                    <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 shrink-0 mt-0.5" />
                    <span>{{ $t('sandbox.computes.evacuate.disableFirst') }}</span>
                </p>
                <p v-else class="mt-3 text-sm text-ink-muted" data-testid="sandbox-evacuate-count">
                    {{ existing === null
                        ? $t('sandbox.computes.evacuate.countUnknown')
                        : $t('sandbox.computes.evacuate.count', { count: existing }, existing) }}
                </p>
                <p v-if="errorMessage" class="mt-4 text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-evacuate-error">
                    {{ errorMessage }}
                </p>
                <div class="flex justify-end gap-2 pt-5">
                    <UButton variant="ghost" color="gray" :disabled="working" @click="open = false">{{ $t('common.cancel') }}</UButton>
                    <UButton color="red" :loading="working" :disabled="!!compute?.enabled" data-testid="sandbox-evacuate-confirm" @click="evacuate">
                        {{ $t('sandbox.computes.evacuate.confirm') }}
                    </UButton>
                </div>
            </template>

            <!-- After: what moved, what stayed and why. -->
            <div v-else data-testid="sandbox-evacuate-result">
                <div
                    :class="['mt-3 rounded-lg border px-4 py-3 flex items-start gap-3 text-sm', result.left.length
                        ? 'border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 text-amber-800 dark:text-amber-200'
                        : 'border-green-200 dark:border-green-900/60 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-200']"
                >
                    <UIcon :name="result.left.length ? 'i-heroicons-exclamation-triangle' : 'i-heroicons-check-circle'" class="w-4 h-4 shrink-0 mt-0.5" />
                    <div class="min-w-0">
                        <div class="font-medium" data-testid="sandbox-evacuate-moved">
                            {{ result.moved.length || result.left.length
                                ? $t('sandbox.computes.evacuate.moved', { count: result.moved.length }, result.moved.length)
                                : $t('sandbox.computes.evacuate.nothing') }}
                        </div>
                        <div v-if="result.moved.length" class="mt-0.5 text-xs opacity-90">{{ $t('sandbox.computes.evacuate.movedHint') }}</div>
                    </div>
                </div>
                <template v-if="result.left.length">
                    <div class="mt-4 text-sm font-medium text-ink">
                        {{ $t('sandbox.computes.evacuate.left', { count: result.left.length }, result.left.length) }}
                    </div>
                    <ul class="mt-2 rounded-lg border border-line divide-y divide-line max-h-64 overflow-y-auto" data-testid="sandbox-evacuate-left">
                        <li v-for="item in result.left" :key="item.sandbox_id" class="px-3 py-2 text-sm">
                            <NuxtLink :to="`/reports/${item.report_id}`" class="font-medium text-blue-600 dark:text-blue-400 hover:underline break-words" dir="auto">
                                {{ titles[item.sandbox_id] || $t('sandbox.sandboxes.untitledReport') }}
                            </NuxtLink>
                            <!-- The server's own sentence. -->
                            <div class="mt-0.5 text-xs text-ink-muted break-words" dir="auto">{{ item.reason }}</div>
                        </li>
                    </ul>
                    <p class="mt-2 text-xs text-ink-muted leading-relaxed">{{ $t('sandbox.computes.evacuate.leftHint') }}</p>
                </template>
                <div class="flex justify-end pt-5">
                    <UButton color="gray" data-testid="sandbox-evacuate-done" @click="open = false">{{ $t('common.close') }}</UButton>
                </div>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import type { SandboxCompute, SandboxEvacuation, SandboxFleetRow } from '~/composables/useSandboxApi'

const props = defineProps<{ modelValue: boolean; compute: SandboxCompute | null }>()
const emit = defineEmits<{
    'update:modelValue': [value: boolean]
    // The compute's numbers changed: the list reads it again.
    done: [compute: SandboxCompute]
}>()

const { t } = useI18n()
const { call } = useSandboxApi()
const { getErrorMessage } = useErrorMessage()

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
})

const working = ref(false)
const errorMessage = ref('')
const result = ref<SandboxEvacuation | null>(null)
// Report titles of the sandboxes that were left, by sandbox id.
const titles = ref<Record<string, string>>({})

const existing = computed(() => {
    const count = props.compute?.allocated?.existing
    return typeof count === 'number' ? count : null
})

watch(() => props.modelValue, (isOpen) => {
    if (!isOpen) return
    result.value = null
    errorMessage.value = ''
    titles.value = {}
})

async function evacuate() {
    const compute = props.compute
    if (!compute || working.value) return
    working.value = true
    errorMessage.value = ''
    try {
        const outcome = await call<SandboxEvacuation>(`/computes/${compute.id}/evacuate`, { method: 'POST' })
        result.value = { moved: outcome?.moved || [], left: outcome?.left || [] }
        // Both are extras: the result stands without them.
        if (result.value.left.length) {
            try {
                const rows = (await call<SandboxFleetRow[]>('/sandboxes')) || []
                titles.value = Object.fromEntries(rows.filter(row => row.report_title).map(row => [row.id, row.report_title as string]))
            } catch { /* the links still work without titles */ }
        }
        try { emit('done', await call<SandboxCompute>(`/computes/${compute.id}`)) } catch { /* the list refreshes later */ }
    } catch (error) {
        errorMessage.value = getErrorMessage(error, t('sandbox.computes.evacuate.failed'))
    } finally {
        working.value = false
    }
}
</script>
