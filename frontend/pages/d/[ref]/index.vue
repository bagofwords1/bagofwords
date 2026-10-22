<template>
    <!-- A published dashboard: the /r page over the dashboard's endpoints -->
    <SharedDashboardView source="dashboard" :id="String($route.params.ref)" />
</template>

<script setup lang="ts">
import SharedDashboardView from '~/components/dashboard/SharedDashboardView.vue';

definePageMeta({
    layout: false,
    auth: false
});
</script>
