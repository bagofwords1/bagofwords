<template>
  <!-- Silent session event: a lightweight, gray, ambient line — deliberately
       quieter than a message bubble or a tool card. Right-aligned to the
       user-bubble edge, since these are the user's own out-of-band actions.
       Dark-mode aware. -->
  <div class="flex items-center justify-end my-1.5 select-none me-2 md:me-[36px]">
    <div
      class="flex items-center gap-1.5 text-[11px] leading-none text-gray-400 dark:text-gray-500 max-w-xl px-1"
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

const { t, te } = useI18n()

// Resolve a localized label from the event's kind + structured meta, falling
// back to the backend's English prompt.content for any kind/variant without a
// translation key (so the strip never renders blank or a raw key). Mirrors the
// machine-event strips (events.evalRunFinished) — same events data, localized.
function resolveKey(): { key: string | null; params: Record<string, any> } {
  const kind = props.m?.message_type as string
  const meta = (props.m?.prompt?.meta || {}) as Record<string, any>
  const list = (a: any) => (Array.isArray(a) ? a.join(', ') : (a || ''))

  switch (kind) {
    case 'run_stopped': return { key: 'run_stopped', params: {} }
    case 'llm_changed':
      return (meta.to_name || meta.to)
        ? { key: 'llm_changed', params: { to: meta.to_name || meta.to } }
        : { key: 'llm_reset', params: {} }
    case 'file_uploaded': return { key: 'file_uploaded', params: { name: meta.filename || meta.file_id || '' } }
    case 'file_removed': return { key: 'file_removed', params: { name: meta.filename || meta.file_id || '' } }
    case 'agent_scope_changed': {
      const added = list(meta.added), removed = list(meta.removed)
      if (added && removed) return { key: 'scope_changed', params: { added, removed } }
      if (added) return { key: 'scope_added', params: { added } }
      if (removed) return { key: 'scope_removed', params: { removed } }
      return { key: 'scope_generic', params: {} }
    }
    case 'report_shared': {
      const who = list(meta.shared_with)
      return who ? { key: 'report_shared', params: { who } } : { key: 'report_shared_generic', params: {} }
    }
    case 'report_published': return { key: 'report_published', params: {} }
    case 'report_unpublished':
      return meta.share_type === 'conversation'
        ? { key: 'conversation_share_off', params: {} }
        : { key: 'report_unpublished', params: {} }
    case 'artifact_shared': {
      const who = list(meta.shared_with)
      const title = meta.title || ''
      return who
        ? { key: 'artifact_shared', params: { title, who } }
        : { key: 'artifact_shared_generic', params: { title } }
    }
    case 'artifact_unshared': return { key: 'artifact_unshared', params: { title: meta.title || '' } }
    case 'artifact_schedule_set': return { key: 'artifact_schedule_set', params: { title: meta.title || '' } }
    case 'artifact_schedule_changed': return { key: 'artifact_schedule_changed', params: { title: meta.title || '' } }
    case 'artifact_schedule_removed': return { key: 'artifact_schedule_removed', params: { title: meta.title || '' } }
    default: return { key: null, params: {} }
  }
}

const label = computed(() => {
  const p = props.m?.prompt || {}
  const fallback = p.content || p.summary || ''
  const { key, params } = resolveKey()
  if (key) {
    const full = `sessionEvents.${key}`
    if (te(full)) return t(full, params)
  }
  return fallback
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
