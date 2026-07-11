<template>
  <div class="flex items-start gap-2 pt-1">
    <UCheckbox :model-value="enabled" class="mt-0.5" @update:model-value="emit('update:enabled', $event)" />
    <div class="text-xs">
      <label class="font-medium text-gray-700 dark:text-gray-300 cursor-pointer" @click="emit('update:enabled', !enabled)">
        Create a public agent with this {{ noun || 'connection' }}
      </label>
      <div class="text-gray-500 dark:text-gray-400">
        We'll create an agent named "{{ name || title || 'Agent' }}" everyone in your org can use.
        Each user signs in individually before using it.
      </div>
      <input
        v-if="enabled"
        :value="name"
        type="text"
        :placeholder="title"
        class="mt-2 w-full border border-gray-300 dark:border-gray-600 rounded-md px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-blue-500"
        @input="emit('update:name', ($event.target as HTMLInputElement).value)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
// Shared "Create a public agent with this <noun>" toggle used by the connector
// forms (Integration, MCP, …). On save the parent calls createPublicAgent()
// with the resulting connection id. Two-way bound via v-model:enabled / v-model:name.
defineProps<{
  enabled: boolean
  name: string
  title?: string   // default agent name + placeholder (the connector title)
  noun?: string    // "integration" | "connection"
}>()
const emit = defineEmits<{
  (e: 'update:enabled', v: boolean): void
  (e: 'update:name', v: string): void
}>()
</script>
