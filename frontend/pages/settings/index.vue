<script setup lang="ts">
definePageMeta({
    layout: 'settings',
    middleware: () => navigateTo('/settings/members', { replace: true })
})
</script>
