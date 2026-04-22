<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm h-full">
        <div class="w-full max-w-7xl px-4 ps-0 py-2 h-full">
            <ConsoleInstructions show-header />
        </div>
    </div>
</template>

<script setup lang="ts">
import ConsoleInstructions from '~/components/ConsoleInstructions.vue'

definePageMeta({
    auth: true,
    layout: 'default'
})
</script>
