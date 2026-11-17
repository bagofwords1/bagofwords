<template>
  <UPopover :popper="{placement:'bottom-start'}">
    <button v-bind="$attrs" type="button" class="inline-flex max-w-full items-center gap-1.5 rounded-md border border-line px-2 py-1 text-xs text-ink">
      <DataSourceIcon v-if="triggerType" :type="triggerType" class="size-4 shrink-0" />
      <UIcon v-else :name="triggerIcon" class="size-4 shrink-0 text-ink-muted" />
      <span class="truncate">{{ summary }}</span><UIcon name="i-heroicons-chevron-down" class="size-3 shrink-0" />
    </button>
    <template #panel="{ close }">
      <div class="w-64 rounded-lg bg-surface p-2">
        <UInput v-model="query" size="xs" icon="i-heroicons-magnifying-glass" :placeholder="searchPlaceholder || $t('prompt.searchAgentFolders')" @vue:unmounted="query = ''" />
        <div class="mt-1 max-h-64 overflow-auto">
          <button v-if="firstLabel" type="button" class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-start text-sm hover:bg-surface-muted"
            :aria-pressed="firstSelected" @click="$emit('first')">
            <UIcon :name="firstIcon || 'i-heroicons-bolt'" class="size-4 text-ink-muted" />{{ firstLabel }}
            <UIcon v-if="firstSelected" name="i-heroicons-check" class="ms-auto size-4 text-blue-500" />
          </button>
          <AgentFolderTree :folders="folders" :selected="selectedFolders" :search="query" :member-folders="[...members.keys()]" @select="id => pick('folder:' + id, close)">
            <template #members="{folderId}">
              <button v-for="option in members.get(folderId)" :key="option.value" type="button" class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-start text-sm hover:bg-surface-muted"
                :aria-pressed="selected.includes(option.value)" @click="pick(option.value, close)">
                <DataSourceIcon v-if="option.type" :type="option.type" class="size-4" />
                <UIcon v-else :name="option.heroicon || 'i-heroicons-cube'" class="size-4 text-ink-muted" />
                <span class="truncate">{{ option.label }}</span><UIcon v-if="selected.includes(option.value)" name="i-heroicons-check" class="ms-auto size-4 text-blue-500" />
              </button>
            </template>
          </AgentFolderTree>
          <button v-for="opt in matches" :key="opt.value" type="button" class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-start text-sm hover:bg-surface-muted"
            :aria-pressed="selected.includes(opt.value)" @click="pick(opt.value, close)">
            <DataSourceIcon v-if="opt.type" :type="opt.type" class="size-4 shrink-0" />
            <UIcon v-else :name="opt.heroicon || 'i-heroicons-cube'" class="size-4 shrink-0 text-ink-muted" />
            <span class="truncate">{{ opt.label }}<span v-if="query && folderPaths.has(opt.folderId || '')" class="block truncate text-2xs text-ink-muted">{{ folderPaths.get(opt.folderId || '') }}</span></span>
            <UIcon v-if="selected.includes(opt.value)" name="i-heroicons-check" class="ms-auto size-4 shrink-0 text-blue-500" />
          </button>
        </div>
      </div>
    </template>
  </UPopover>
</template>
<script setup lang="ts">
import type { AgentFolderOption } from '~/composables/useAgentFolderOptions'
defineOptions({ inheritAttrs: false })
const props = defineProps<{
  folders: AgentFolderOption[], options: {value: string, label: string, type?: string, heroicon?: string, parentPath?: string, folderId?: string}[],
  selected: string[], placeholder: string, searchPlaceholder?: string, single?: boolean, firstLabel?: string, firstIcon?: string, firstSelected?: boolean
}>()
const emit = defineEmits<{select: [value: string], first: []}>()
const query = ref('')
const selectedFolders = computed(() => props.selected.filter(v => v.startsWith('folder:')).map(v => v.slice(7)))
const folderPaths = computed(() => new Map(props.folders.map(f => [f.id, f.path])))
const members = computed(() => {
  const byFolder = new Map<string, typeof props.options>()
  for (const option of props.options) {
    if (!option.folderId || !folderPaths.value.has(option.folderId)) continue
    if (!byFolder.has(option.folderId)) byFolder.set(option.folderId, [])
    byFolder.get(option.folderId)!.push(option)
  }
  return byFolder
})
const matches = computed(() => {
  const term = query.value.trim().toLowerCase()
  if (!term) return props.options.filter(o => !members.value.has(o.folderId || ''))
  return props.options.filter(o => [o.label, folderPaths.value.get(o.folderId || '')].some(text => text?.toLowerCase().includes(term)))
})
const summary = computed(() => {
  const [first, ...others] = props.selected
  if (!first || props.single) return props.placeholder
  const label = first.startsWith('folder:') ? props.folders.find(f => f.id === first.slice(7))?.name : props.options.find(o => o.value === first)?.label
  return (label || (props.firstSelected && props.firstLabel) || props.placeholder) + (others.length ? ' +' + others.length : '')
})
// The trigger icon mirrors the dropdown items (DataSourceIcon for an agent, a
// folder icon for a folder, the resource's own heroicon, the first-option icon
// for a global/Auto scope) instead of always showing a folder — which made an
// agent- or org-scoped selection read as if it lived in a folder.
const firstSelection = computed(() => (props.single ? undefined : props.selected[0]))
const firstSelectedOption = computed(() => {
  const value = firstSelection.value
  if (!value || value.startsWith('folder:')) return undefined
  return props.options.find(o => o.value === value)
})
const triggerType = computed(() => firstSelectedOption.value?.type)
const triggerIcon = computed(() => {
  const value = firstSelection.value
  if (value?.startsWith('folder:')) return 'i-heroicons-folder'
  if (firstSelectedOption.value?.heroicon) return firstSelectedOption.value.heroicon
  if (!value && props.firstSelected) return props.firstIcon || 'i-heroicons-globe-alt'
  return 'i-heroicons-cube'
})
function pick(id: string, close: () => void) {
  emit('select', id)
  if (props.single) close()
}
</script>
