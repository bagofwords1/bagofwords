<template>
    <!-- One list of the history modal with nothing in it yet. -->
    <div class="flex flex-col items-center justify-center text-center gap-2 py-10 px-4" data-testid="history-empty">
        <Icon :name="icon" class="w-6 h-6 text-gray-300 dark:text-gray-600" />
        <p class="text-sm text-gray-500 dark:text-gray-400 max-w-sm">{{ text }}</p>
    </div>
</template>

<script setup lang="ts">
defineProps<{ icon: string; text: string }>()
</script>
