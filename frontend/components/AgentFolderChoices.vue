<template>
  <button v-for="folder in choices" :key="folder.id" type="button"
    class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-start text-xs hover:bg-surface-muted"
    :disabled="disabled" @click="$emit('select', folder.ids, folder)">
    <UIcon name="i-heroicons-folder" class="size-4 shrink-0 text-ink-muted" />
    <span class="flex-1 truncate">{{ folder.path }}</span>
    <UIcon v-if="folder.ids.every(id => selected.includes(id))" name="i-heroicons-check" class="size-4 text-blue-500" />
  </button>
</template>

<script setup lang="ts">
import { loadAgentFolderOptions } from '~/composables/useAgentFolderOptions'
const props = withDefaults(defineProps<{
  agents: {id: string}[], selected?: string[], search?: string, disabled?: boolean
}>(), { selected: () => [], search: '' })
defineEmits<{select: [ids: string[], folder: { id: string, path: string }]}>()
const folders = ref<Awaited<ReturnType<typeof loadAgentFolderOptions>>>([])
const placements = ref(new Map<string, string>())
onMounted(async () => {
  const [options, roster] = await Promise.all([
    loadAgentFolderOptions(),
    useMyFetch<{id: string, folder_id?: string}[]>('/data_sources', {query: {show_all: true}}),
  ])
  placements.value = new Map((roster.data.value || []).filter(a => a.folder_id).map(a => [a.id, a.folder_id!]))
  folders.value = options
})
const choices = computed(() => {
  const ancestors = new Map(folders.value.map(f => [f.id, f.ancestors]))
  const members = new Map<string, string[]>()
  for (const agent of props.agents) {
    for (const fid of ancestors.get(placements.value.get(agent.id) || '') || []) {
      if (!members.has(fid)) members.set(fid, [])
      members.get(fid)!.push(agent.id)
    }
  }
  return folders.value.filter(f => members.has(f.id) && f.path.toLowerCase().includes(props.search.toLowerCase()))
    .map(f => ({ ...f, ids: members.get(f.id)! }))
})
</script>
