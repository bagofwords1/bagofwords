<template>
    <!-- A published dashboard: the /r page over the dashboard's endpoints.
         Remounted on `reload` (e.g. editor capabilities changed). -->
    <SharedDashboardView
        :key="`${$route.fullPath}#${reloads}`"
        source="dashboard"
        :id="String($route.params.ref)"
        @reload="reloads++"
    />
</template>

<script setup lang="ts">
import SharedDashboardView from '~/components/dashboard/SharedDashboardView.vue';

definePageMeta({
    layout: false,
    auth: false
});

const reloads = ref(0);
</script>
