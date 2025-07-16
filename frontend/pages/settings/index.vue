<script setup lang="ts">
definePageMeta({
    layout: 'settings'
})

onMounted(() => {
    navigateTo('/settings/members')
})
</script>