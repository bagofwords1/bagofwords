<script setup lang="ts">
import ConsoleInstructions from '~/components/ConsoleInstructions.vue'

definePageMeta({
    permissions: ['manage_instructions'],
})
</script>

<template>
    <div class="mt-6">
        <ConsoleInstructions show-header />
    </div>
</template>