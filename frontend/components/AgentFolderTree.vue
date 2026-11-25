<template>
  <div v-for="row in rows" :key="row.folder.id">
    <div class="flex items-center" :style="{paddingInlineStart: Math.min(row.depth, 8) * 12 + 'px'}">
      <button v-if="!query && row.hasChildren" type="button" class="grid size-6 shrink-0 place-items-center rounded hover:bg-surface-muted"
        :aria-expanded="expanded.has(row.folder.id)" :aria-label="$t(expanded.has(row.folder.id) ? 'prompt.collapseDirectory' : 'prompt.expandDirectory', {name: row.folder.name})"
        @click="toggle(row.folder.id)">
        <UIcon :name="expanded.has(row.folder.id) ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="size-3 text-ink-muted" />
      </button>
      <span v-else-if="!query" class="w-6 shrink-0" />
      <button type="button" class="flex min-w-0 flex-1 items-center gap-2 rounded px-1.5 py-1.5 text-start text-sm hover:bg-surface-muted"
        :disabled="disabled || disabledIds.includes(row.folder.id)" :aria-pressed="selected.includes(row.folder.id)" :title="row.folder.path" data-kbd-option @click="$emit('select', row.folder.id)">
        <UIcon name="i-heroicons-folder" class="size-4 shrink-0 text-ink-muted" />
        <span class="min-w-0 flex-1">
          <span class="block truncate">{{ row.folder.name }}</span>
          <span v-if="query && row.folder.parentPath" class="block truncate text-2xs text-ink-muted">{{ row.folder.parentPath }}</span>
        </span>
        <UIcon v-if="selected.includes(row.folder.id)" name="i-heroicons-check" class="size-4 shrink-0 text-blue-500" />
      </button>
    </div>
    <div v-if="!query && expanded.has(row.folder.id) && memberFolders.includes(row.folder.id)" :style="{paddingInlineStart: Math.min(row.depth + 1, 9) * 12 + 'px'}">
      <slot name="members" :folder-id="row.folder.id" />
    </div>
  </div>
</template>

<script setup lang="ts">
import type { AgentFolderOption } from '~/composables/useAgentFolderOptions'
const props = withDefaults(defineProps<{
  folders: AgentFolderOption[], selected?: string[], search?: string, disabled?: boolean, disabledIds?: string[], memberFolders?: string[]
}>(), {selected: () => [], search: '', disabledIds: () => [], memberFolders: () => []})
defineEmits<{select: [id: string]}>()
const query = computed(() => props.search.trim().toLowerCase())
const expanded = ref(new Set<string>())
function toggle(id: string) {
  if (expanded.value.has(id)) expanded.value.delete(id)
  else expanded.value.add(id)
}
const rows = computed(() => {
  const ids = new Set(props.folders.map(f => f.id))
  const children = new Map<string | undefined, AgentFolderOption[]>()
  for (const folder of props.folders) {
    // A selectable descendant can have a browse-only parent.
    const parent = folder.ancestors.slice(1).find(id => ids.has(id))
    if (!children.has(parent)) children.set(parent, [])
    children.get(parent)!.push(folder)
  }
  for (const siblings of children.values()) siblings.sort((a, b) => a.name.localeCompare(b.name))
  if (query.value) return props.folders.filter(f => f.path.toLowerCase().includes(query.value))
    .sort((a,b) => a.path.localeCompare(b.path)).map(folder => ({folder, depth: 0, hasChildren: false}))
  const members = new Set(props.memberFolders)
  const result: { folder: AgentFolderOption, depth: number, hasChildren: boolean }[] = []
  function visit(folder: AgentFolderOption, depth: number) {
    const nested = children.get(folder.id) || []
    result.push({folder, depth, hasChildren: !!nested.length || members.has(folder.id)})
    if (expanded.value.has(folder.id)) for (const child of nested) visit(child, depth + 1)
  }
  for (const root of children.get(undefined) || []) visit(root, 0)
  return result
})
</script>
