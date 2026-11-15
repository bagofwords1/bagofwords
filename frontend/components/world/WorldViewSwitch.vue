<template>
  <nav class="inline-flex shrink-0 gap-0.5 rounded-md border border-line p-0.5" :aria-label="$t('agentsPage.title')">
    <NuxtLink v-for="item in views" :key="item.view" :to="item.to"
      :data-testid="'open-' + item.view" :aria-current="active === item.view ? 'page' : undefined"
      :title="$t(item.label)" :aria-label="$t(item.label)"
      class="grid h-6 w-6 place-items-center rounded focus-visible:outline-blue-500"
      :class="active === item.view ? 'bg-surface-muted text-ink' : 'text-ink-muted hover:bg-surface-subtle'">
      <UIcon :name="item.icon" class="h-3.5 w-3.5" />
    </NuxtLink>
  </nav>
</template>

<script setup lang="ts">
defineProps<{ active: 'tree' | 'world' }>()
const views = [
  { view: 'tree', to: '/agents', label: 'world.tree', icon: 'i-heroicons-list-bullet' },
  { view: 'world', to: '/agents/world', label: 'world.title', icon: 'i-heroicons-globe-alt' },
]
</script>
