<template>
    <SharedDashboardView source="report" :id="String($route.params.id)" />
</template>

<script setup lang="ts">
import SharedDashboardView from '~/components/dashboard/SharedDashboardView.vue';

definePageMeta({
    titleKey: 'pageTitles.report',
    layout: false,
    auth: false
});
</script>
