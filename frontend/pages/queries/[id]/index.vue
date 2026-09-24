<template>
  <!-- Deep-link shim. Queries live in the /agents knowledge tree now, but this
       route is linked from the report agent panel and the @-mention picker, so
       it stays and forwards into the tree instead of 404ing. -->
  <div />
</template>

<script setup lang="ts">
definePageMeta({ auth: true })

const route = useRoute()
onMounted(() => {
  const id = String(route.params.id || '')
  // Forward the agent the link was made for: a query shared with several
  // agents opens on that one.
  const agent = typeof route.query.agent === 'string' && route.query.agent
    ? `?agent=${encodeURIComponent(route.query.agent)}` : ''
  navigateTo(id ? `/agents/queries/${id}${agent}` : '/agents', { replace: true })
})
</script>
