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
  navigateTo(id ? `/agents/queries/${id}` : '/agents', { replace: true })
})
</script>
