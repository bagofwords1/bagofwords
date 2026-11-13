<script setup lang="ts">
import ConsoleInstructions from '~/components/ConsoleInstructions.vue'

definePageMeta({
    titleKey: 'nav.instructions',
    layout: 'settings',
    permissions: ['manage_instructions'],
})
</script>

<template>
    <div class="mt-6">
        <ConsoleInstructions show-header />
    </div>
</template>