<template>
    <div class="flex items-center gap-2 px-4 py-2 bg-amber-50 border-b border-amber-100 text-xs text-amber-700">
        <Icon name="heroicons:arrow-path-rounded-square" class="w-3.5 h-3.5 flex-shrink-0" />
        <span>
            Forked from
            <a
                v-if="forkedFromId"
                :href="`/r/${forkedFromId}`"
                class="font-medium underline hover:text-amber-900 transition-colors"
                target="_blank"
            >{{ forkedFromTitle || 'Untitled' }}</a>
            <span v-if="forkedFromUserName"> by {{ forkedFromUserName }}</span>
        </span>
    </div>
</template>

<script setup lang="ts">
defineProps<{
    forkedFromId: string
    forkedFromTitle?: string | null
    forkedFromUserName?: string | null
}>()
</script>
