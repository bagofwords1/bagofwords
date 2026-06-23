<template>
  <div
    class="group relative bg-white border border-gray-200 rounded-xl p-4 hover:border-gray-300 hover:shadow-sm transition cursor-pointer flex flex-col"
    @click="$emit('open')"
  >
    <div class="flex items-start justify-between">
      <DataSourceIcon :type="card.type" class="h-8" />
      <div @click.stop>
        <span
          v-if="card.connected"
          class="inline-flex items-center gap-1 text-xs text-green-700 bg-green-50 border border-green-100 px-2 py-0.5 rounded-full"
        >
          <span class="w-1.5 h-1.5 rounded-full bg-green-500"></span> Connected
        </span>
        <UButton
          v-else
          size="2xs"
          color="gray"
          variant="solid"
          @click="$emit('connect')"
        >Connect</UButton>
      </div>
    </div>

    <div class="mt-3">
      <div class="font-medium text-gray-900 text-sm">{{ card.title }}</div>
      <p class="mt-1 text-xs text-gray-500 line-clamp-2">{{ card.description }}</p>
    </div>

    <div v-if="card.connected" class="mt-3 pt-3 border-t border-gray-50 flex items-center gap-1 text-[11px] text-gray-400">
      <UIcon name="heroicons-wrench-screwdriver" class="w-3 h-3" />
      {{ card.tool_count }} {{ card.tool_count === 1 ? 'action' : 'actions' }}
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ card: any }>()
defineEmits<{ (e: 'open'): void; (e: 'connect'): void }>()
</script>
