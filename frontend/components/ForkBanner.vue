<template>
    <div class="flex items-center gap-2 px-4 py-2 bg-amber-50 dark:bg-amber-950 border-b border-amber-100 text-xs text-amber-700">
        <Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5 flex-shrink-0" />
        <span>
            Forked from
            <a
                v-if="forkedFromId || forkedFromDashboardId"
                :href="forkedFromDashboardId && !forkedFromId ? `/d/${forkedFromDashboardId}` : `/r/${forkedFromId}`"
                class="font-medium underline hover:text-amber-900 transition-colors"
                target="_blank"
            >{{ forkedFromTitle || 'Untitled' }}</a>
            <span v-if="forkedFromUserName"> by {{ forkedFromUserName }}</span>
        </span>
    </div>
</template>

<script setup lang="ts">
defineProps<{
    forkedFromId?: string | null
    // A fork from a published dashboard's page (phase 8): links to its /d page
    forkedFromDashboardId?: string | null
    forkedFromTitle?: string | null
    forkedFromUserName?: string | null
}>()
</script>
