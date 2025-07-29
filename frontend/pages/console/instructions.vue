<template>
    <div class="mt-6">
        <ConsoleInstructions />
    </div>
</template>

<script setup lang="ts">
import ConsoleInstructions from '~/components/ConsoleInstructions.vue'

definePageMeta({
    auth: true,
    permissions: ['view_console'],
    layout: 'console'
})
</script> 