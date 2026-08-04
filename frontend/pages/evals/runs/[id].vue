<template>
  <EvalRunDetail :run-id="runId" :back-to="backTo" />
</template>

<script setup lang="ts">
definePageMeta({
  auth: true,
  layout: 'default',
  permissions: ['manage_evals']
})

import EvalRunDetail from '~/components/EvalRunDetail.vue'

const route = useRoute()
const runId = computed(() => String(route.params.id || ''))
// Honors ?back=<internal path> so deep links opened from an agent's Evals
// panel return there. Only same-app absolute paths — a "//host" or external
// value must not win.
const backTo = computed(() => {
  const b = route.query.back
  return (typeof b === 'string' && b.startsWith('/') && !b.startsWith('//')) ? b : '/evals'
})
</script>
