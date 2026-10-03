<template>
    <!-- Shown in place of a fork's dashboard and query panel while its queries
         are still being run under the forker's credentials (fork hydration),
         or — if none of them could run — instead of an empty dashboard. -->
    <div class="flex items-start gap-2 rounded-lg border px-3 py-2 text-xs"
         :class="nothingRan
            ? 'border-red-100 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-300'
            : 'border-amber-100 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300'">
        <Icon v-if="nothingRan" name="heroicons:exclamation-triangle" class="w-4 h-4 flex-shrink-0 mt-0.5" />
        <Spinner v-else class="w-4 h-4 flex-shrink-0 mt-0.5" />
        <div v-if="nothingRan">{{ $t('fork.nothingRan') }}</div>
        <div v-else>
            <div class="font-medium">{{ $t('fork.preparing') }}</div>
            <div class="opacity-80">{{ $t('fork.preparingDetail') }}</div>
        </div>
    </div>
</template>

<script setup lang="ts">
defineProps<{ nothingRan?: boolean }>()
</script>
