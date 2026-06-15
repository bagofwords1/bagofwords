<template>
    <div class="text-sm h-full">
        <div class="w-full h-full">
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
