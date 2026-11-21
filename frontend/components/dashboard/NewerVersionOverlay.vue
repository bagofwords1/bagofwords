<template>
    <!-- A newer version of the open dashboard was saved (by the agent, in
         another tab or conversation): the stale one is veiled, not swapped
         under the reader, and one click shows the new one. "Keep viewing"
         lifts the veil for this version only. -->
    <div class="absolute inset-0 z-30 flex items-center justify-center bg-white/60 dark:bg-gray-900/60 backdrop-blur-[2px]"
        data-testid="newer-version-overlay" role="status" aria-live="polite">
        <div class="mx-4 max-w-sm rounded-lg border border-line bg-surface-elevated shadow-lg px-5 py-4 text-center">
            <div class="flex items-center justify-center gap-1.5 text-sm font-medium text-ink">
                <Icon name="heroicons:sparkles" class="w-4 h-4 text-blue-500" />
                {{ $t('dashboardCollab.versions.newerTitle', { n: version }) }}
            </div>
            <p class="mt-1 text-xs text-ink-muted">{{ $t('dashboardCollab.versions.newerBody') }}</p>
            <div class="mt-3 flex items-center justify-center gap-2">
                <button type="button" data-testid="newer-version-refresh"
                    class="flex items-center gap-1.5 rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700"
                    @click="$emit('refresh')">
                    <Icon name="heroicons:arrow-path" class="w-3.5 h-3.5" />
                    {{ $t('dashboardCollab.versions.newerRefresh', { n: version }) }}
                </button>
                <button v-if="current" type="button" data-testid="newer-version-dismiss"
                    class="rounded-md px-2 py-1.5 text-xs text-ink-muted hover:text-ink-2 hover:bg-surface-muted"
                    @click="$emit('dismiss')">
                    {{ $t('dashboardCollab.versions.newerKeep', { n: current }) }}
                </button>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
defineProps<{
    // The newer version on offer, and the one on screen.
    version: number;
    current?: number | null;
}>();
defineEmits<{
    (e: 'refresh'): void;
    (e: 'dismiss'): void;
}>();
</script>
