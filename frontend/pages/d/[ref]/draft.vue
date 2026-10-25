<template>
    <!-- A dashboard's draft, for its editors: where its unpublished /d sends
         them. Any version (?version=N) or named draft (?draft=slug) is a page
         of its own (collab01): keyed on the full path, and on `reload` after a
         publish or a roll back. -->
    <SharedDashboardView
        :key="`${$route.fullPath}#${reloads}`"
        source="draft"
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
