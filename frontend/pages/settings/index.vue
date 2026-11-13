<script setup lang="ts">
definePageMeta({
    titleKey: 'settings.membersTab',
    layout: 'settings',
    middleware: () => navigateTo('/settings/members', { replace: true })
})
</script>
