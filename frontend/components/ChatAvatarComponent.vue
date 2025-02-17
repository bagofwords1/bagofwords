<template>
    <div v-if="role == 'user'" class="h-7 w-7 flex items-center justify-center text-xs border border-blue-200 bg-blue-100 rounded-full inline-block">
        Y
    </div>
    <div v-else-if="role == 'system' || role == 'thinking'" class="h-7 w-7 flex font-bold items-center justify-center text-xs rounded-lg inline-block bg-contain bg-center bg-no-repeat" style="background-image: url('/assets/logo-128.png')">
    </div>
</template>

<script lang="ts" setup>
const props = defineProps<{
    role: string
    user?: Object
}>()

const role = props.role

</script>