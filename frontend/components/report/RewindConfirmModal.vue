<template>
	<UModal v-model="isOpen" :prevent-close="busy" :ui="{ width: 'sm:max-w-md' }">
		<form class="space-y-3 p-5" data-testid="rewind-confirm" @submit.prevent="emit('confirm')">
			<h2 class="text-base font-semibold text-ink">{{ $t('reportView.rewindConfirmTitle') }}</h2>
			<p class="text-sm text-ink-2">{{ $t('reportView.rewindConfirmBody', { n: count }, count) }}</p>
			<p class="text-xs text-ink-muted">{{ $t('reportView.rewindConfirmKeeps') }}</p>
			<p v-if="memoriesCount > 0" class="text-xs text-ink-muted" data-testid="rewind-confirm-memories">{{ $t('reportView.rewindConfirmMemories', { n: memoriesCount }, memoriesCount) }}</p>
			<p class="text-xs text-ink-muted">{{ $t('reportView.rewindConfirmPrefill') }}</p>
			<div class="flex justify-end gap-2 pt-2">
				<UButton color="gray" variant="ghost" :disabled="busy" @click="isOpen = false">{{ $t('common.cancel') }}</UButton>
				<UButton color="red" type="submit" :loading="busy" data-testid="rewind-confirm-action">{{ $t('reportView.rewindConfirmAction') }}</UButton>
			</div>
		</form>
	</UModal>
</template>

<script setup lang="ts">
// Confirmation for Rewind: the prompt and every later message leave the
// conversation and the agent's context.
// memoriesCount: memories saved from the removed turns. Rewind keeps them
// (memory belongs to the user, across reports); the dialog says so.
const props = defineProps<{ modelValue: boolean, count: number, memoriesCount: number, busy: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [boolean], confirm: [] }>()
const isOpen = computed({
	get: () => props.modelValue,
	set: (v: boolean) => emit('update:modelValue', v),
})
</script>
