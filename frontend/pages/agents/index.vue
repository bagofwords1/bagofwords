<template>
    <div class="text-sm h-full">
        <div class="w-full h-full">
            <KnowledgeExplorer />
        </div>
    </div>
</template>

<script setup lang="ts">
import KnowledgeExplorer from '~/components/KnowledgeExplorer.vue'

definePageMeta({
    auth: true,
    layout: 'default'
})

// Handle the OAuth callback redirect (the backend sends the browser back to
// /agents?oauth=success|error after token exchange). Surface the result as a
// toast and strip the query params so a refresh doesn't re-fire the toast.
const { t } = useI18n()
onMounted(() => {
    const route = useRoute()
    if (route.query.oauth === 'success') {
        useToast().add({ title: t('data.connectedSuccess'), color: 'green', icon: 'i-heroicons-check-circle' })
        navigateTo('/agents', { replace: true })
    } else if (route.query.oauth === 'error') {
        useToast().add({
            title: t('data.connectionFailed'),
            description: (route.query.message as string) || '',
            color: 'red',
            icon: 'i-heroicons-x-circle',
        })
        navigateTo('/agents', { replace: true })
    }
})
</script>
