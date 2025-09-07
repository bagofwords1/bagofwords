<template>
    <div class="flex justify-center pl-2 md:pl-4 text-sm">
        <div class="w-full max-w-7xl px-4 pl-0 py-2">
                <div>
                    <h1 class="text-lg font-semibold">
                        <GoBackChevron v-if="isExcel" />
                        Instructions
                    </h1>
                    <p class="mt-2 text-gray-500">Create and manage your instructions</p>
                </div>
                <div>
                    <ConsoleInstructions />
                </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import ConsoleInstructions from '~/components/ConsoleInstructions.vue'
import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'

definePageMeta({
    auth: true,
    permissions: ['view_console'],
    layout: 'default'
})

const permissionsLoaded = usePermissionsLoaded()
const canCreate = computed(() => permissionsLoaded.value && useCan('create_instructions'))
const isExcel = false
</script>
