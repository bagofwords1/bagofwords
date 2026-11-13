<template>
    <div class="mt-6">
        <LLMsComponent :organization="organization" />
    </div>
</template>

<script setup lang="ts">
const { organization } = useOrganization()
definePageMeta({ titleKey: 'settings.llm', auth: true, permissions: ['manage_llm'], layout: 'settings' })
</script> 