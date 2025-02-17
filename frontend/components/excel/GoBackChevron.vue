<template>
    <div class="hover:bg-gray-50 hover:text-blue-500 cursor-pointer inline text-sm p-2"   @click="goBack" >
        <Icon name="heroicons-chevron-left" /> 
    </div>
</template>

<script setup lang="ts">
const router = useRouter()

function goBack() {
    router.back()
}
</script>