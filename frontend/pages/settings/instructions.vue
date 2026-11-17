<script setup lang="ts">
import ConsoleInstructions from '~/components/ConsoleInstructions.vue'

definePageMeta({
    titleKey: 'nav.instructions',
    permissions: ['manage_instructions'],
})
</script>

<template>
    <div>
        <ConsoleInstructions show-header />
    </div>
</template>