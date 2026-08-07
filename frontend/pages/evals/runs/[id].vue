<template>
  <EvalRunDetail :run-id="runId" :back-to="backTo" />
</template>

<script setup lang="ts">
definePageMeta({
  auth: true,
  layout: 'default',
  // Org-level eval admins get the org-wide view; a per-agent manager reaches
  // the same pages scoped to their agents, because the eval routes filter by
  // agent authority server-side. Guarding on the org perm alone bounced an
  // agent manager off the run page right after they launched a run — the run
  // never executed, since this page is what drives it.
  anyOf: ['manage_evals', { permission: 'manage_evals', resourceType: 'data_source' }]
})

import EvalRunDetail from '~/components/EvalRunDetail.vue'

const route = useRoute()
const runId = computed(() => String(route.params.id || ''))
// Honors ?back=<internal path> so deep links opened from an agent's Evals
// panel return there. Only same-app absolute paths — a "//host" or external
// value must not win.
const backTo = computed(() => {
  const b = route.query.back
  return (typeof b === 'string' && b.startsWith('/') && !b.startsWith('//')) ? b : '/agents'
})
</script>
