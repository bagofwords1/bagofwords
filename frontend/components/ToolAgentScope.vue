<template><slot /></template>

<script setup lang="ts">
import { computed, provide } from 'vue'
import { resolveToolAgent, toolAgentIconKey, type AgentToolExecution, type ToolAgent } from '~/utils/toolAgentIcon'

const props = defineProps<{
  executions: AgentToolExecution[]
  agents?: ToolAgent[]
  nested?: boolean
}>()
provide(toolAgentIconKey, computed(() => props.nested ? undefined : resolveToolAgent(props.executions, props.agents)))
</script>
