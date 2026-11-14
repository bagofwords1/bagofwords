<template>
  <section :aria-label="$t('agentsPage.agentsSection')" class="space-y-0.5">
    <div class="flex min-h-7 items-center gap-2 rounded-md px-2" :class="dropTarget === null && treeDropTarget"
      @dragover="hoverDestination($event, null)" @dragleave="leaveDestination" @drop="dropAgent($event, null)">
      <span class="grow text-2xs font-semibold uppercase tracking-wider text-ink-muted">{{ $t('agentsPage.agentsSection') }}</span>
      <slot name="header" />
      <UButton v-if="canCreate" size="2xs" color="gray" variant="ghost" icon="i-heroicons-folder-plus"
        :title="$t('agentsPage.tipNewFolder')" :aria-label="$t('agentsPage.tipNewFolder')" @click="emit('create', null)" />
    </div>
    <div v-if="loading || !foldersLoaded" class="flex h-8 items-center gap-2 px-2 text-sm text-ink-muted">
      <Spinner class="size-3.5" />{{ $t('agentsPage.loading') }}
    </div>
    <div v-for="row in rows" :key="row.key" :style="{ paddingInlineStart: Math.min(row.depth, 8) * 14 + 'px' }">
      <slot v-if="row.kind === 'agent'" name="agent" :agent="row.agent" />
      <div v-else-if="row.kind === 'folder'" class="group flex h-8 min-w-0 items-center gap-1.5 rounded-md pe-2 ps-1.5 text-sm"
        :class="dropTarget === row.folder.id ? treeDropTarget : activeFolder === row.folder.id ? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800/70'"
        @dragover="hoverDestination($event, row.folder.id)" @dragleave="leaveDestination" @drop="dropAgent($event, row.folder.id)">
        <button type="button" class="flex h-full shrink-0 items-center" :aria-label="row.folder.name"
          :aria-expanded="expanded.has(row.folder.id)" @click="toggle(row.folder.id)">
          <UIcon name="i-heroicons-chevron-right" class="size-3 text-ink-muted transition-transform"
            :class="expanded.has(row.folder.id) ? 'rtl-own-transform rotate-90' : 'rtl:rotate-180'" />
        </button>
        <button type="button" class="flex h-full min-w-0 flex-1 items-center gap-1.5 text-start" :title="row.folder.name" @click="open(row.folder.id)">
          <UIcon :name="expanded.has(row.folder.id) ? 'i-heroicons-folder-open' : 'i-heroicons-folder'" class="size-4 shrink-0 text-ink-muted" />
          <span class="truncate bidi-label" dir="auto">{{ row.folder.name }}</span>
          <UIcon v-if="row.folder.limited_access" name="i-heroicons-lock-closed" class="size-3 shrink-0" :aria-label="$t('sharedFolders.limit')" />
        </button>
        <div v-if="row.folder.can_manage" class="flex shrink-0 items-center opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100">
          <UButton size="2xs" variant="ghost" color="gray" icon="i-heroicons-folder-plus" :aria-label="$t('agentsPage.tipNewSubfolder')" :title="$t('agentsPage.tipNewSubfolder')" @click="emit('create', row.folder.id)" />
          <UButton size="2xs" variant="ghost" color="gray" icon="i-heroicons-pencil" :aria-label="$t('sharedFolders.edit')" :title="$t('sharedFolders.edit')" @click="editItem('folder', row.folder.id, row.folder.name)" />
          <UButton size="2xs" variant="ghost" color="gray" icon="i-heroicons-user-plus" :aria-label="$t('sharedFolders.share')" :title="$t('sharedFolders.share')" @click="openShare(row.folder.id)" />
        </div>
      </div>
      <div v-else class="ps-5 py-1 text-2xs italic text-ink-muted">{{ $t('agentsPage.dirEmpty') }}</div>
    </div>
    <UModal v-model="editing" :ui="{ width: 'sm:max-w-md' }">
      <div v-if="item" class="flex max-h-[85dvh] flex-col">
        <header class="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div class="min-w-0">
            <h2 class="text-base font-semibold text-ink">{{ $t('sharedFolders.edit') }}</h2>
            <p class="truncate text-sm text-ink-muted" :title="item.name">{{ item.name }}</p>
          </div>
          <UButton icon="i-heroicons-x-mark" variant="ghost" color="gray" :aria-label="$t('common.close')" @click="editing = false" />
        </header>
        <div class="min-h-0 space-y-5 overflow-y-auto px-5 py-4">
          <form v-if="item.kind === 'folder'" class="space-y-2" @submit.prevent="rename">
            <label for="folder-name" class="text-sm font-medium">{{ $t('sharedFolders.name') }}</label>
            <div class="flex items-center gap-2">
              <UInput id="folder-name" v-model="editName" class="min-w-0 flex-1" required maxlength="160" />
              <UButton type="submit" color="blue" :disabled="!editName.trim() || editName.trim() === item.name">{{ $t('sharedFolders.rename') }}</UButton>
            </div>
          </form>
          <div>
            <h3 class="mb-2 text-sm font-medium">{{ $t('sharedFolders.move') }}</h3>
            <div class="overflow-hidden rounded-lg border border-line">
              <div class="flex items-center gap-2 bg-surface-subtle px-3 py-2 text-sm">
                <UButton v-if="destination" icon="i-heroicons-chevron-left" class="rtl:rotate-180" variant="ghost" color="gray" size="xs"
                  :aria-label="$t('sharedFolders.back')" @click="browseDestination(destinationFolder?.parent_id || '')" />
                <UIcon v-else name="i-heroicons-folder" class="shrink-0 text-ink-muted" />
                <span class="min-w-0 truncate font-medium" :title="destinationPath">{{ destinationFolder?.name || $t('sharedFolders.root') }}</span>
              </div>
              <div v-if="destinations.length" class="max-h-44 overflow-y-auto border-t border-line py-1">
                <button v-for="f in destinations" :key="f.id" class="flex w-full items-center gap-2 px-3 py-1.5 text-start text-sm hover:bg-surface-muted"
                  :title="f.name" @click="browseDestination(f.id)">
                  <UIcon name="i-heroicons-folder" class="shrink-0 text-ink-muted" />
                  <span class="min-w-0 flex-1 truncate">{{ f.name }}</span>
                  <UIcon name="i-heroicons-chevron-right" class="shrink-0 text-ink-muted rtl:rotate-180" />
                </button>
              </div>
            </div>
            <button v-if="destination" type="button" class="mt-1 block w-full text-start text-xs text-ink-muted"
              :class="showPath ? 'max-h-20 overflow-y-auto break-words' : 'truncate'" :aria-expanded="showPath"
              :title="destinationPath" @click="showPath = !showPath">{{ showPath ? destinationPath : compactPath }}</button>
            <div v-if="preview" class="mt-2 space-y-1 text-xs text-ink-muted" aria-live="polite">
              <p v-if="!preview.changes.length">{{ $t('sharedFolders.sameAccess') }}</p>
              <p v-for="c in preview.changes" :key="c.principal" class="break-words">{{ c.principal }} · {{ c.from ? $t('sharedFolders.' + c.from.toLowerCase()) : '—' }} → {{ c.to ? $t('sharedFolders.' + c.to.toLowerCase()) : '—' }}</p>
            </div>
          </div>
          <label v-if="item.kind === 'agent' && originalDestination" class="flex items-center justify-between text-sm">
            {{ $t('sharedFolders.inherit') }}<input type="checkbox" :disabled="busy" :checked="!item.limited" @change="agentLimit" />
          </label>
        </div>
        <footer class="flex shrink-0 items-center justify-between gap-2 border-t border-line px-5 py-3">
          <UButton v-if="item.kind === 'folder'" color="red" variant="ghost" size="xs" icon="i-heroicons-trash" @click="remove">{{ $t('sharedFolders.delete') }}</UButton>
          <UButton class="ms-auto" color="blue" :loading="busy" :disabled="destination === originalDestination || (destination ? !destinationFolder?.can_manage : item.kind === 'folder' && !canCreate)" @click="move">{{ $t(preview ? 'sharedFolders.move' : 'sharedFolders.preview') }}</UButton>
        </footer>
      </div>
    </UModal>
    <UModal v-model="sharing" :ui="{ width: 'sm:max-w-md' }">
      <div class="flex max-h-[85dvh] flex-col">
        <header class="flex items-center justify-between gap-3 px-5 pb-3 pt-4">
          <h2 class="min-w-0 truncate text-base font-semibold" :title="sharingFolder?.name">{{ $t('dashboardCollab.share.shareNamed', {name: sharingFolder?.name}) }}</h2>
          <UButton icon="i-heroicons-x-mark" variant="ghost" color="gray" :aria-label="$t('common.close')" @click="sharing = false" />
        </header>
        <div class="min-h-0 overflow-y-auto px-5 pb-4">
          <UInput v-model="search" icon="i-heroicons-magnifying-glass" :placeholder="$t('dashboardCollab.share.addPeopleAndGroups')" />
          <h3 class="mb-1 mt-4 text-sm font-medium">{{ $t(search.trim() ? 'common.search' : 'dashboardCollab.share.peopleWithAccess') }}</h3>
          <div v-for="entry in visibleAudience" :key="entry.type + entry.id" class="flex items-center gap-3 py-2">
            <UIcon :name="entry.type === 'role' ? 'i-heroicons-identification' : entry.type === 'group' ? 'i-heroicons-user-group' : 'i-heroicons-user-circle'" class="size-5 shrink-0 text-ink-muted" />
            <div class="min-w-0 flex-1 text-sm">
              <div class="truncate" :title="entry.name">{{ entry.name }}</div>
              <button v-for="grant in entry.inherited" :key="grant.source_id" type="button"
                class="block max-w-full truncate text-start text-xs text-ink-muted hover:underline" :title="grant.source"
                @click="inspectSource(grant.source_id)">{{ $t(grant.role === 'Custom' ? 'rolesManager.levels.custom' : 'sharedFolders.' + grant.role.toLowerCase()) }} · {{ $t('sharedFolders.via', {name: grant.source}) }}</button>
            </div>
            <USelect size="xs" color="gray"  :aria-label="$t('sharedFolders.direct', {name:entry.name})" :model-value="entry.role || ''" :disabled="busy || entry.type === 'role'"
              :options="roleOptions" @update:model-value="setRole(entry, $event)" />
          </div>
          <p class="mt-2 text-xs text-ink-muted">{{ $t('sharedFolders.hints') }}</p>
        </div>
        <footer class="flex shrink-0 items-center justify-between gap-2 border-t border-line px-5 py-3">
          <label v-if="sharingFolder?.parent_id" class="flex items-center gap-2 text-sm" :title="$t('sharedFolders.stops')">
            <input type="checkbox" :checked="!sharingFolder.limited_access" :disabled="busy" @change="limit" />{{ $t('sharedFolders.inherit') }}
          </label>
          <UButton color="blue" class="ms-auto" @click="sharing = false">{{ $t('dashboardCollab.share.done') }}</UButton>
        </footer>
      </div>
    </UModal>
  </section>
</template>

<script setup lang="ts">
import { treeDropTarget } from '~/utils/treeDragStyles'

type Folder = { id: string, parent_id: string | null, name: string, limited_access: boolean, can_manage: boolean, can_query: boolean, capabilities: string[] }
type Recipient = { id: string, type: string, name: string, role: string | null, inherited?: {role: string, source: string, source_id: string}[] }
const props = defineProps<{ agents: DragAgent[], loading: boolean, activeFolder?: string | null, activeAgent?: string | null }>()
const emit = defineEmits<{ select: [id: string | null], create: [parentId: string | null], changed: [] }>()
const foldersLoaded = ref(false)
const expanded = ref(new Set<string>())
const sharingId = ref<string | null>(null)
const sharingFolder = computed(() => folders.value.find(folder => folder.id === sharingId.value))
const folders = ref<Folder[]>([])
const selected = ref<string | null>(null)
const current = computed(() => folders.value.find(f => f.id === selected.value))
const canCreate = ref(false)
const sharing = ref(false), busy = ref(false)
const audience = ref<Recipient[]>([])
const search = ref('')
const visibleAudience = computed(() => audience.value.filter(entry => search.value.trim()
  ? entry.name.toLowerCase().includes(search.value.trim().toLowerCase())
  : entry.role || entry.inherited?.length))
const roleOptions = computed(() => ['', 'Viewer', 'Manager'].map(value => ({
  value, label: t('sharedFolders.' + (value ? value.toLowerCase() : 'noDirect')),
})))
const toast = useToast()

function open(id: string | null) {
  selected.value = id
  expandPath(id ? foldersById.value.get(id)?.parent_id : null)
  emit('select', id)
}
async function load() {
  const { data } = await useMyFetch<{ folders: Folder[], can_create: boolean }>('/folders')
  folders.value = data.value?.folders || []
  canCreate.value = !!data.value?.can_create
  foldersLoaded.value = true
  if (selected.value && !current.value) open(null)
}
async function inspectSource(id: string) {
  if (!foldersById.value.get(id)?.can_manage) return
  sharing.value = false
  await openShare(id)
}
async function openShare(id: string, keepSearch = false) {
  sharingId.value = id
  if (keepSearch !== true) search.value = ''
  const { data, error } = await useMyFetch<Recipient[]>(`/folders/${id}/audience`)
  if (sharingId.value !== id) return
  if (error.value) return toast.add({title: error.value.message, color:'red'})
  audience.value = data.value || []
  sharing.value = true
}
async function save(path: string, method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', body?: object) {
  busy.value = true
  try {
    const { error } = await useMyFetch(path, { method, body })
    if (error.value) {
      toast.add({ title: error.value.data?.detail || error.value.message, color: 'red' })
      return false
    }
    await load()
    return true
  } finally { busy.value = false }
}
async function folderCreated(parentId: string | null) {
  await load()
  expandPath(parentId)
}
async function setRole(entry: Recipient, role: string) {
  if (await save(`/folders/${sharingId.value}/share`, 'PUT', { principal_id: entry.id, principal_type: entry.type, role: role || null })) {
    if (sharingFolder.value?.can_manage) await openShare(sharingFolder.value.id, true)
    else sharing.value = false
  }
}
async function limit(event: Event) {
  const input = event.target as HTMLInputElement
  if (!input.checked) {
    const names = audience.value.filter(e => e.inherited?.length).map(e => e.name)
    if (names.length && !window.confirm(t('sharedFolders.stopConfirm', {names: names.join(', ')}))) {
      input.checked = true
      return
    }
  }
  if (!await save(`/folders/${sharingId.value}/inheritance`, 'PUT', { limited_access: !input.checked })) {
    input.checked = !input.checked
    return
  }
  if (sharingFolder.value?.can_manage) await openShare(sharingFolder.value.id, true)
  else sharing.value = false
}
const editing = ref(false), editName = ref(''), destination = ref('')
const item = ref<{kind: 'agent' | 'folder', id: string, name: string, limited: boolean} | null>(null)
const preview = ref<{audience_version: string, changes: {principal: string, from?:string, to?:string}[]} | null>(null)
const originalDestination = ref('')
const showPath = ref(false)
const foldersById = computed(() => new Map(folders.value.map(f => [f.id, f])))
const destinationFolder = computed(() => foldersById.value.get(destination.value))
const destinationSegments = computed(() => {
  const names: string[] = []
  let folder = destinationFolder.value
  while (folder) {
    names.push(folder.name)
    folder = foldersById.value.get(folder.parent_id || '')
  }
  return names.reverse()
})
const destinationPath = computed(() => destinationSegments.value.join(' / '))
const compactPath = computed(() => destinationSegments.value.length > 2
  ? '… / ' + destinationSegments.value.slice(-2).join(' / ')
  : destinationPath.value)
const destinations = computed(() => folders.value.filter(f =>
  (f.parent_id || '') === destination.value && !(item.value?.kind === 'folder' && f.id === item.value.id)))
function browseDestination(id: string) {
  destination.value = id
  showPath.value = false
  preview.value = null
}
function editItem(kind: 'agent' | 'folder', id: string, name: string, limited = false, source?: string | null) {
  item.value = {kind, id, name, limited}
  editName.value = name
  destination.value = kind === 'folder' ? foldersById.value.get(id)?.parent_id || '' : source || ''
  showPath.value = false
  originalDestination.value = destination.value
  preview.value = null
  editing.value = true
}
async function rename() {
  if (item.value && await save(`/folders/${item.value.id}`, 'PATCH', {name:editName.value})) editing.value = false
}
async function remove() {
  if (!item.value || !window.confirm(t('sharedFolders.delete') + ' · ' + item.value.name + '?')) return
  if (await save(`/folders/${item.value.id}`, 'DELETE')) editing.value = false
}
async function move() {
  if (!item.value) return
  const path = `/folders/items/${item.value.kind}/${item.value.id}/placement`
  const parent_id = destination.value || null
  if (!preview.value) {
    const {data, error} = await useMyFetch<any>(path, {method:'PUT', body:{parent_id,preview:true}})
    if (!editing.value || (destination.value || null) !== parent_id || path !== `/folders/items/${item.value?.kind}/${item.value?.id}/placement`) return
    if (error.value) return toast.add({title:error.value.data?.detail || error.value.message,color:'red'})
    preview.value = data.value
  } else if (await save(path,'PUT',{parent_id,audience_version:preview.value.audience_version})) {
    expandPath(parent_id)
    editing.value = false
    emit('changed')
  } else preview.value = null
}
const { t } = useI18n()
async function agentLimit(event: Event) {
  const input = event.target as HTMLInputElement
  if (!item.value) return
  if (await save(`/folders/agents/${item.value.id}/inheritance`, 'PUT', {limited_access:!input.checked})) {
    item.value.limited = !input.checked
    preview.value = null
    emit('changed')
  } else input.checked = !input.checked
}
type DragAgent = {[key: string]: any, id: string, name: string, folder_id?: string | null, limited_access?: boolean}
const dragged = ref<DragAgent | null>(null)
const dropTarget = ref<string | null>()
let browseTimer: ReturnType<typeof setTimeout> | undefined
function clearHover() {
  clearTimeout(browseTimer)
  dropTarget.value = undefined
}
function leaveDestination(event: DragEvent) {
  if (!(event.relatedTarget instanceof Node) || !(event.currentTarget as Node).contains(event.relatedTarget)) clearHover()
}
function toggle(id: string) {
  if (expanded.value.has(id)) expanded.value.delete(id)
  else expanded.value.add(id)
}
function expandPath(id?: string | null) {
  while (id) {
    expanded.value.add(id)
    id = foldersById.value.get(id)?.parent_id
  }
}
watch(
  () => [foldersLoaded.value, props.agents.find(agent => agent.id === props.activeAgent)?.folder_id] as const,
  ([loaded, folderId]) => { if (loaded) expandPath(folderId) },
  { immediate: true },
)
type TreeRow = { key: string, depth: number } & (
  { kind: 'folder', folder: Folder } | { kind: 'agent', agent: DragAgent } | { kind: 'empty' }
)
const rows = computed(() => {
  if (!foldersLoaded.value) return []
  const children = new Map<string | null, TreeRow[]>()
  function append(parent: string | null | undefined, row: TreeRow) {
    const id = parent && foldersById.value.has(parent) ? parent : null
    if (!children.has(id)) children.set(id, [])
    children.get(id)!.push(row)
  }
  for (const folder of folders.value) append(folder.parent_id, {kind: 'folder', folder, key: 'folder:' + folder.id, depth: 0})
  for (const agent of props.agents) append(agent.folder_id, {kind: 'agent', agent, key: 'agent:' + agent.id, depth: 0})
  const visible: TreeRow[] = []
  function visit(parent: string | null, depth: number) {
    const entries = children.get(parent) || []
    if (parent && !entries.length && !props.loading) visible.push({kind: 'empty', key: 'empty:' + parent, depth})
    for (const row of entries) {
      visible.push({...row, depth})
      if (row.kind === 'folder' && expanded.value.has(row.folder.id)) visit(row.folder.id, depth + 1)
    }
  }
  visit(null, 0)
  return visible
})
function canDrag(source?: string | null) {
  return !source || !!foldersById.value.get(source)?.can_manage
}
function startDrag(event: DragEvent, agent: DragAgent) {
  if (!event.dataTransfer || !canDrag(agent.folder_id)) { event.preventDefault(); return }
  event.dataTransfer.effectAllowed = 'move'
  event.dataTransfer.setData('application/x-bow-agent', agent.id)
  event.dataTransfer.setData('text/plain', agent.id)
  dragged.value = agent
}
function endDrag() {
  clearHover()
  dragged.value = null
}
function validDrop(id: string | null) {
  return dragged.value && (dragged.value.folder_id || null) !== id && canDrag(id)
}
function hoverDestination(event: DragEvent, id: string | null) {
  if (!validDrop(id)) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  if (dropTarget.value === id) return
  clearHover()
  dropTarget.value = id
  if (id && !expanded.value.has(id)) browseTimer = setTimeout(() => expanded.value.add(id), 650)
}
async function dropAgent(event: DragEvent, id: string | null) {
  const agent = dragged.value
  if (!agent || !validDrop(id)) return
  event.preventDefault()
  endDrag()
  editItem('agent', agent.id, agent.name, agent.limited_access, agent.folder_id)
  browseDestination(id || '')
  await move()
}
onBeforeUnmount(endDrag)
defineExpose({open, folders, current, editItem, canDrag, startDrag, endDrag, dragged, folderCreated})
onMounted(load)
</script>
