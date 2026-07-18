<template>
  <!-- Silent session event: a lightweight, gray, ambient line — deliberately
       quieter than a message bubble or a tool card. Dark-mode aware. -->
  <div class="flex items-center justify-center my-1.5 select-none">
    <div
      class="flex items-center gap-1.5 text-[11px] leading-none text-gray-400 dark:text-gray-500 max-w-xl px-2"
      :title="label"
    >
      <Icon :name="icon" class="w-3 h-3 flex-shrink-0 opacity-60" />
      <span class="truncate" dir="auto">{{ label }}</span>
      <span v-if="ts" class="opacity-50 flex-shrink-0">· {{ ts }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ m: any }>()

// Per-kind icon "registry" — keyed by message_type, same idea as the tool
// component registry but intentionally lighter (one component, an icon map).
const ICONS: Record<string, string> = {
  run_stopped: 'heroicons-stop-circle',
  llm_changed: 'heroicons-cpu-chip',
  file_uploaded: 'heroicons-paper-clip',
  file_removed: 'heroicons-x-mark',
  agent_scope_changed: 'heroicons-adjustments-horizontal',
  report_shared: 'heroicons-user-plus',
  report_published: 'heroicons-globe-alt',
  report_unpublished: 'heroicons-lock-closed',
  artifact_shared: 'heroicons-share',
  artifact_unshared: 'heroicons-lock-closed',
  artifact_schedule_set: 'heroicons-clock',
  artifact_schedule_changed: 'heroicons-clock',
  artifact_schedule_removed: 'heroicons-clock',
}

const icon = computed(() => ICONS[props.m?.message_type as string] || 'heroicons-information-circle')

const label = computed(() => {
  const p = props.m?.prompt || {}
  return p.content || p.summary || ''
})

const ts = computed(() => {
  const raw = props.m?.created_at
  if (!raw) return ''
  try {
    return new Date(raw).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch {
    return ''
  }
})
</script>
