<template>
    <!-- A dashboard's history (collab01): the conversations that edited it,
         its versions (preview any), the releases kept for Roll back, its
         notes and its owner. Contract used by the dashboard page and the
         in-conversation frame:
           v-model (open), dashboard-id, capabilities (from the page meta),
           @rolled-back (readers now see another release), @transferred. -->
    <UModal v-model="open" :ui="{ width: 'sm:max-w-3xl' }">
        <div class="p-5" data-testid="dashboard-history">
            <slot />
        </div>
    </UModal>
</template>

<script setup lang="ts">
const props = defineProps<{ modelValue: boolean; dashboardId: string; capabilities: string[] }>();
const emit = defineEmits<{
    (e: 'update:modelValue', value: boolean): void;
    (e: 'rolled-back'): void;
    (e: 'transferred'): void;
}>();

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
});
</script>
