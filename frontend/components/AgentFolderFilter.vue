<template>
  <UPopover>
    <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-folder" :aria-label="$t('sharedFolders.title')" :disabled="disabled" />
    <template #panel="{ close }">
      <div class="max-h-64 w-64 overflow-auto p-2">
        <UInput v-model="query" size="xs" icon="i-heroicons-magnifying-glass" :placeholder="$t('common.search')" />
        <AgentFolderChoices :agents="agents" :selected="selected" :search="query" @select="ids => { $emit('select', ids); close() }" />
      </div>
    </template>
  </UPopover>
</template>
<script setup lang="ts">
defineProps<{ agents: { id: string }[], selected?: string[], disabled?: boolean }>()
defineEmits<{select: [ids: string[]]}>()
const query = ref('')
</script>
