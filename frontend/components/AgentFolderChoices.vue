<template>
  <UInput v-if="$slots.agent && search === undefined" v-model="localSearch" size="xs" icon="i-heroicons-magnifying-glass" :placeholder="$t('prompt.searchAgentFolders')" />
  <AgentFolderTree :folders="choices" :selected="selectedFolders ?? checked" :search="query" :disabled="disabled" :member-folders="$slots.agent ? [...direct.keys()] : []" @select="choose">
    <template #members="{folderId}">
      <slot v-for="agent in direct.get(folderId)" :key="agent.id" name="agent" :agent="agent" parent-path="" />
    </template>
  </AgentFolderTree>
  <template v-if="$slots.agent">
    <slot v-for="agent in visible" :key="agent.id" name="agent" :agent="agent" :parent-path="query ? pathFor(agent.id) : ''" />
  </template>
</template>

<script setup lang="ts" generic="T extends {id: string, name?: string}">
import { loadAgentFolderOptions } from '~/composables/useAgentFolderOptions'
const props = withDefaults(defineProps<{
  agents: T[], selectedFolders?: string[], selected?: string[], search?: string, disabled?: boolean
}>(), { selected: () => [] })
const emit = defineEmits<{select: [ids: string[], folder: { id: string, path: string }]}>()
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
  return folders.value.filter(f => members.has(f.id))
    .map(f => ({ ...f, ids: members.get(f.id)! }))
})
const localSearch = ref('')
const query = computed(() => (props.search ?? localSearch.value).trim())
const paths = computed(() => new Map(choices.value.map(f => [f.id, f.path])))
function pathFor(id: string) {
  return paths.value.get(placements.value.get(id) || '') || ''
}
const direct = computed(() => {
  const groups = new Map<string, T[]>()
  for (const agent of props.agents) {
    const folder = placements.value.get(agent.id)
    if (!folder || !paths.value.has(folder)) continue
    if (!groups.has(folder)) groups.set(folder, [])
    groups.get(folder)!.push(agent)
  }
  return groups
})
const visible = computed(() => {
  const term = query.value.toLowerCase()
  return props.agents.filter(a => term ? [a.name, pathFor(a.id)].some(s => s?.toLowerCase().includes(term)) : !pathFor(a.id))
})
const checked = computed(() => choices.value.filter(f => f.ids.every(id => props.selected.includes(id))).map(f => f.id))
function choose(id: string) {
  const folder = choices.value.find(f => f.id === id)
  if (folder) emit('select', folder.ids, folder)
}
</script>
