<template>
  <img 
    src="/icons/mcp.png" 
    alt="MCP" 
    :class="props.class"
  />
</template>

<script setup lang="ts">
const props = defineProps<{
  class?: string
}>()
</script>




