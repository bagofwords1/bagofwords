<template>
    <div class="ps-2 md:ps-4 text-sm h-full">
        <div class="w-full px-4 ps-0 py-2 h-full">
            <KnowledgeExplorer />
        </div>
    </div>
</template>

<script setup lang="ts">
import KnowledgeExplorer from '~/components/KnowledgeExplorer.vue'

definePageMeta({
    auth: true,
    layout: 'default'
})
</script>
