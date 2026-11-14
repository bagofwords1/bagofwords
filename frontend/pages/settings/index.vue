<script setup lang="ts">
definePageMeta({
    titleKey: 'settings.membersTab',
    middleware: () => navigateTo('/settings/members', { replace: true })
})
</script>
