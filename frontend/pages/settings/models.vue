<template>
    <div>
        <LLMsComponent :organization="organization" />
    </div>
</template>

<script setup lang="ts">
const { organization } = useOrganization()
// Org LLM admins, or managers of at least one provider (the page then shows
// only their providers; the backend scopes every call).
definePageMeta({ auth: true, anyOf: ['manage_llm', { permission: 'manage_llm', resourceType: 'llm_provider' }] })
</script> 