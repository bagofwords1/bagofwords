<template>
    <!-- A value to copy somewhere else (a public key, a fingerprint): always
         left-to-right, selectable, with a copy button. -->
    <div class="flex items-start gap-2 rounded-lg border border-line bg-surface-subtle px-3 py-2">
        <code dir="ltr" class="flex-1 min-w-0 font-mono text-xs text-ink-2 break-all select-all text-left" :data-testid="testid">{{ value }}</code>
        <UButton
            color="gray"
            variant="ghost"
            size="xs"
            :icon="copied ? 'i-heroicons-check' : 'i-heroicons-clipboard-document'"
            :aria-label="copied ? $t('common.copied') : $t('common.copy')"
            :data-testid="testid ? `${testid}-copy` : undefined"
            @click="copy"
        >
            {{ copied ? $t('common.copied') : $t('common.copy') }}
        </UButton>
    </div>
</template>

<script setup lang="ts">
const props = defineProps<{ value: string; testid?: string }>()

const copied = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined

async function copy() {
    try {
        await navigator.clipboard?.writeText(props.value)
        copied.value = true
        clearTimeout(timer)
        timer = setTimeout(() => { copied.value = false }, 2000)
    } catch { /* clipboard unavailable: the value is selectable */ }
}
onBeforeUnmount(() => clearTimeout(timer))
</script>
