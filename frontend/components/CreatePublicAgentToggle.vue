<template>
  <div class="flex items-start gap-2 pt-1">
    <UCheckbox :model-value="enabled" class="mt-0.5" @update:model-value="emit('update:enabled', $event)" />
    <div class="text-xs">
      <label class="font-medium text-gray-700 dark:text-gray-300 cursor-pointer" @click="emit('update:enabled', !enabled)">
        {{ $t('connectors.shareToggleLabel') }}
      </label>
      <div class="text-gray-500 dark:text-gray-400">
        {{ $t('connectors.shareToggleHint', { name: name || title || $t('connectors.title') }) }}
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
// Shared "Make available to everyone in your org" toggle used by the connector
// forms (Integration, MCP, …). On save the parent calls createPublicAgent(),
// which creates an org-wide CONNECTOR (kind: 'connector') for the connection —
// each person then signs in with their own account. Two-way bound via v-model:enabled / v-model:name.
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
