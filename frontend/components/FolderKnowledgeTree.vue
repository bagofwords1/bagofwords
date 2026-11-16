<template>
  <div v-for="section in sections" :key="section.kind" class="ps-3.5">
    <div class="flex h-8 items-center rounded-md px-1.5 text-sm text-ink-muted hover:bg-surface-muted">
      <button type="button" class="flex h-full shrink-0 items-center pe-1.5"
        :aria-label="$t('agentsPage.' + section.kind)" :aria-expanded="!collapsed.has(section.kind)" @click="toggle(section.kind)">
        <UIcon name="i-heroicons-chevron-right" class="size-3" :class="collapsed.has(section.kind) ? 'rtl:rotate-180' : 'rtl-own-transform rotate-90'" />
      </button>
      <button type="button" class="flex h-full min-w-0 grow items-center gap-1.5 text-start" @click="$emit('pick', section.kind)">
        <UIcon :name="section.icon" class="size-4" />
        <span class="grow">{{ $t('agentsPage.' + section.kind) }}</span>
        <span v-if="items[section.kind]" class="text-xs">{{ items[section.kind]?.length }}</span>
      </button>
    </div>
    <div v-if="!collapsed.has(section.kind)" class="ps-6">
      <button v-if="errors.has(section.kind)" class="text-xs text-ink-muted" @click="load(section.kind)">{{ $t('changelog.retry') }}</button>
      <Spinner v-else-if="!items[section.kind]" class="size-3 text-ink-muted" />
      <p v-else-if="!items[section.kind]?.length" class="py-1 text-2xs text-ink-muted">{{ $t(section.empty) }}</p>
      <button v-for="item in items[section.kind]" :key="item.id" type="button" :title="item.name"
        class="block h-7 w-full truncate rounded-md px-2 text-start text-xs hover:bg-surface-muted"
        @click="$emit('pick', section.kind, item)">{{ item.name }}</button>
    </div>
  </div>
</template>
<script setup lang="ts">
type Kind = 'instructions' | 'files' | 'evals'
type Item = {id: string, name: string}
const props = defineProps<{folder: {id: string, capabilities: string[], can_query: boolean}, revision: number}>()
defineEmits<{pick: [kind: Kind, item?: Item]}>()
const groups = [
  {kind:'instructions', icon:'i-heroicons-document-text', empty:'agentsPage.noInstructions'},
  {kind:'files', icon:'i-heroicons-paper-clip', empty:'agentsPage.noFiles'},
  {kind:'evals', icon:'i-heroicons-beaker', empty:'agentsPage.noSuites'},
] as const
const sections = computed(() => groups.filter(({kind}) => props.folder.capabilities.includes('manage_' + kind) || (kind !== 'evals' && props.folder.can_query)))
const items = reactive<Partial<Record<Kind, Item[]>>>({})
const errors = reactive(new Set<Kind>())
const collapsed = reactive(new Set<Kind>(groups.map(({kind}) => kind)))
const requests = {instructions:0, files:0, evals:0}
function toggle(kind: Kind) {
  if (collapsed.has(kind)) collapsed.delete(kind)
  else collapsed.add(kind)
}
async function load(kind: Kind) {
  const request = ++requests[kind]
  errors.delete(kind)
  const {data,error} = await useMyFetch<Item[]>(`/folders/${props.folder.id}/knowledge/${kind}`)
  if (request !== requests[kind]) return
  items[kind] = data.value || []
  if (error.value) errors.add(kind)
}
// Key on what is listed, not on the folder object: the tree replaces every
// folder object on each reload (a rename, share or move), and reloading three
// lists for every expanded folder each time queued the next request behind them.
// A change that moves knowledge between folders (deleting a folder and keeping
// its contents) must bump `revision` instead: KnowledgeExplorer does on delete.
const sectionKey = computed(() => props.folder.id + ':' + sections.value.map(({kind}) => kind).join(','))
watch([sectionKey, () => props.revision], () => Promise.all(sections.value.map(({kind}) => load(kind))), {immediate:true})
</script>
