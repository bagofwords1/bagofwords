<script setup lang="ts">
import ConsoleInstructions from '~/components/ConsoleInstructions.vue'

definePageMeta({
    permissions: ['manage_instructions'],
})
</script>

<template>
    <div>
        <ConsoleInstructions show-header />
    </div>
</template>