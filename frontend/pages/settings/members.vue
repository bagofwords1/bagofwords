<template>
    <div class="mt-6">
        <MembersComponent :organization="organization" />
    </div>
</template>

<script setup lang="ts">
const { organization } = useOrganization()
definePageMeta({
    layout: 'settings'
})
</script>
