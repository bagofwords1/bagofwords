<template>
    <!-- A dashboard's draft, for its editors: where its unpublished /d sends them -->
    <SharedDashboardView source="draft" :id="String($route.params.ref)" />
</template>

<script setup lang="ts">
import SharedDashboardView from '~/components/dashboard/SharedDashboardView.vue';

definePageMeta({
    layout: false,
    auth: false
});
</script>
