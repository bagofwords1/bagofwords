<template>
  <nav class="w-full mb-4">
    <ol class="flex justify-center items-center gap-4 text-xs">
      <li v-for="(step, idx) in steps" :key="step.key" class="flex items-center gap-2">
        <span @click="go(step.key)" :class="['flex items-center gap-2', canClick(step.key) ? 'cursor-pointer' : 'cursor-default']">
          <span :class="circleClass(step.key)" class="w-5 h-5 rounded-full flex items-center justify-center">
            <UIcon v-if="isDone(step.key)" name="heroicons-check" class="w-3.5 h-3.5" />
            <span v-else>{{ idx + 1 }}</span>
          </span>
          <span :class="labelClass(step.key)">{{ step.label }}</span>
        </span>
        <span v-if="idx < steps.length - 1" class="mx-2 w-6 h-px bg-gray-200"></span>
      </li>
    </ol>
  </nav>
  
</template>

<script setup lang="ts">
const props = defineProps<{ current: 'connect' | 'schema' | 'context', dsId?: string }>()
const router = useRouter()

const steps = [
  { key: 'connect', label: 'Connect Data Source' },
  { key: 'schema', label: 'Select Tables' },
  { key: 'context', label: 'Set Context' },
] as const

function circleClass(key: string) {
  if (isDone(key)) return 'bg-green-100 text-green-600'
  if (key === props.current) return 'bg-gray-900 text-white'
  return 'bg-gray-100 text-gray-500'
}

function labelClass(key: string) {
  if (key === props.current) return 'text-gray-900'
  return 'text-gray-500'
}

const order = ['connect','schema','context'] as const
function isDone(key: string) {
  const curr = order.indexOf(props.current as any)
  const idx = order.indexOf(key as any)
  return idx > -1 && idx < curr
}

function canClick(key: string) {
  // Only allow clicking schema/context (not connect)
  if (key === 'connect') return false
  return true
}

function go(key: string) {
  if (!canClick(key)) return
  if (key === 'schema' && props.dsId) return router.push(`/integrations/new/${props.dsId}/schema`)
  if (key === 'context' && props.dsId) return router.push(`/integrations/new/${props.dsId}/context`)
}
</script>

<style scoped>
</style>


