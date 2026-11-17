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
      <template v-else-if="row.kind === 'folder'">
      <div class="group flex h-8 min-w-0 items-center gap-1.5 rounded-md pe-2 ps-1.5 text-sm"
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
      <FolderKnowledgeTree v-if="expanded.has(row.folder.id)" :folder="row.folder" :revision="knowledgeRevision" @pick="(kind, item) => emit('knowledge', row.folder.id, kind, item)" />
      </template>
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
          <UButton v-if="canMoveFolder" variant="ghost" color="gray" icon="i-heroicons-folder-arrow-down"
            @click="beginMove()">{{ $t('sharedFolders.moveFolder') }}</UButton>
        </div>
        <footer class="flex items-center justify-between border-t border-line px-5 py-3">
          <UButton color="blue" @click="editing = false">{{ $t('dashboardCollab.share.done') }}</UButton>
        </footer>
      </div>
    </UModal>
    <UModal v-model="moving" :prevent-close="busy" :ui="{ width: 'sm:max-w-md' }">
      <div v-if="item" class="space-y-4 p-5">
        <h2 class="break-words text-sm font-semibold">{{ $t('sharedFolders.moveNamed', {name: item.name}) }}</h2>
        <dl class="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-4 gap-y-2 text-sm">
          <dt class="text-ink-muted">{{ $t('sharedFolders.from') }}</dt>
          <dd class="break-words">{{ folderPath(originalDestination) }}</dd>
          <dt><label for="move-target" class="text-ink-muted">{{ $t('sharedFolders.to') }}</label></dt>
          <dd>
            <select id="move-target" v-model="destination" :disabled="busy" class="w-full rounded-md border border-line bg-surface px-2 py-1.5 text-sm">
              <option disabled :value="null">{{ $t('sharedFolders.chooseDestination') }}</option>
              <option v-for="target in destinations" :key="target.id" :value="target.id">{{ target.label }}</option>
            </select>
            <p v-if="destination !== null" class="mt-1 break-words text-xs text-ink-muted sm:hidden">{{ folderPath(destination) }}</p>
          </dd>
        </dl>
        <div v-if="destination !== null" aria-live="polite" class="rounded-md bg-surface-muted p-3 text-xs">
          <p class="font-medium">{{ $t('sharedFolders.parentAccess') }}</p>
          <p v-if="previewError" role="alert" class="mt-1 text-red-600">{{ previewError }}
            <button class="underline" @click="loadPreview">{{ $t('changelog.retry') }}</button>
          </p>
          <p v-else-if="!preview" class="mt-1 text-ink-muted">{{ $t('agentsPage.loading') }}</p>
          <template v-else>
            <p v-if="!preview.audience_hidden && !preview.changes.length" class="mt-1">{{ $t('sharedFolders.sameParentAccess') }}</p>
            <ul v-else-if="preview.changes.length" class="mt-1 max-h-40 space-y-1 overflow-y-auto">
              <li v-for="(change, i) in preview.changes" :key="i" class="break-words">{{ change.principal }} · {{ accessLabel(change.from) }} → {{ accessLabel(change.to) }}</li>
            </ul>
            <p class="mt-1 text-ink-muted">{{ $t('sharedFolders.directUnchanged') }}</p>
          </template>
        </div>
        <footer class="flex justify-end gap-2">
          <UButton color="gray" variant="ghost" :disabled="busy" @click="moving = false">{{ $t('common.cancel') }}</UButton>
          <UButton color="blue" :disabled="!preview || !destinationEligible" :loading="busy" @click="confirmMove">{{ $t(item.kind === 'agent' ? 'sharedFolders.moveAgent' : 'sharedFolders.moveFolder') }}</UButton>
        </footer>
      </div>
    </UModal>
    <UModal v-model="accessOpen" :ui="{ width: 'sm:max-w-md' }">
      <form v-if="accessAgent" class="space-y-4 p-5" @submit.prevent="saveAgentAccess">
        <h2 class="text-sm font-semibold">{{ accessAgent.name }}</h2>
        <label class="flex items-center justify-between text-sm">{{ $t('sharedFolders.inherit') }}<input v-model="inherit" type="checkbox" /></label>
        <footer class="flex justify-end gap-2">
          <UButton variant="ghost" color="gray" @click="accessOpen = false">{{ $t('common.cancel') }}</UButton>
          <UButton type="submit" color="blue" :loading="busy">{{ $t('common.save') }}</UButton>
        </footer>
      </form>
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
            <span v-if="entry.type === 'role' && entry.role" class="text-xs text-ink-muted">{{ $t(entry.role === 'Custom' ? 'rolesManager.levels.custom' : 'sharedFolders.' + entry.role.toLowerCase()) }}</span>
            <USelect v-if="entry.type !== 'role'" size="xs" color="gray" :aria-label="$t('sharedFolders.direct', {name:entry.name})" :model-value="entry.role || ''" :disabled="busy"
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

type Folder = { id: string, parent_id: string | null, name: string, limited_access: boolean, can_manage: boolean, can_move_from_folder: boolean, can_query: boolean, capabilities: string[] }
type Recipient = { id: string, type: string, name: string, role: string | null, inherited?: {role: string, source: string, source_id: string}[] }
const props = defineProps<{ agents: DragAgent[], knowledgeRevision: number, loading: boolean, showAll?: boolean, activeFolder?: string | null, activeAgent?: string | null }>()
const emit = defineEmits<{ knowledge: [folderId:string, kind:'instructions'|'evals'|'files', item?:{id:string,name:string}], select: [id: string | null], create: [parentId: string | null], changed: [] }>()
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
  const { data } = await useMyFetch<{ folders: Folder[], can_create: boolean }>('/folders', { query: { show_all: !!props.showAll } })
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
    accessRevision.value++
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
const editing = ref(false), editName = ref('')
const moving = ref(false)
const destination = ref<string | null>(null)
const previewError = ref('')
const item = ref<{kind: 'agent' | 'folder', id: string, name: string} | null>(null)
type MovePreview = {audience_hidden: boolean, audience_version: string, changes: {principal: string, from?:string, to?:string}[]}
const preview = ref<MovePreview | null>(null)
const originalDestination = ref('')
const foldersById = computed(() => new Map(folders.value.map(f => [f.id, f])))
const managedFolders = computed(() => folders.value.filter(f => f.can_manage))
function folderPath(id: string) {
  const names: string[] = []
  let folder = foldersById.value.get(id)
  while (folder) {
    names.unshift(folder.name)
    folder = foldersById.value.get(folder.parent_id || '')
  }
  return names.join(' / ') || t('sharedFolders.root')
}
function isDescendantOrSelf(id: string, ancestor: string) {
  let folder = foldersById.value.get(id)
  while (folder) {
    if (folder.id === ancestor) return true
    folder = foldersById.value.get(folder.parent_id || '')
  }
  return false
}
const destinations = computed(() => {
  const current = item.value
  if (!current) return []
  const options = managedFolders.value.filter(f => f.id !== originalDestination.value
    && (current.kind !== 'folder' || !isDescendantOrSelf(f.id, current.id)))
    .map(f => ({id: f.id, label: folderPath(f.id)}))
  if (originalDestination.value && (current.kind === 'agent' || canCreate.value)) options.unshift({id: '', label: t('sharedFolders.root')})
  return options
})
const destinationEligible = computed(() => destinations.value.some(target => target.id === destination.value))
const canMoveFolder = computed(() => item.value?.kind === 'folder' && foldersById.value.get(item.value.id)?.can_move_from_folder && destinations.value.length > 0)
function editItem(kind: 'folder', id: string, name: string) {
  item.value = {kind, id, name}
  editName.value = name
  originalDestination.value = foldersById.value.get(id)?.parent_id || ''
  editing.value = true
}
function beginMove(target: string | null = null) {
  editing.value = false
  destination.value = target
  moving.value = true
}
function moveAgent(id: string, target: string | null = null) {
  const agent = props.agents.find(a => a.id === id)
  if (!agent || !canDrag(agent)) return
  item.value = {kind: 'agent', id, name: agent.name}
  originalDestination.value = agent.folder_id || ''
  beginMove(target)
}
async function rename() {
  if (item.value && await save(`/folders/${item.value.id}`, 'PATCH', {name:editName.value})) editing.value = false
}
const accessRevision = ref(0)
async function openMemberAccess(id: string, name?: string) {
  search.value = name || ''
  await openShare(id, true)
}
async function afterDelete() {
  open(null)
  await load()
  emit('changed')
}
const { t } = useI18n()
const accessLabel = (role?: string) => role ? t('sharedFolders.' + role.toLowerCase()) : '—'
let previewRequest = 0
async function loadPreview() {
  const request = ++previewRequest
  preview.value = null
  previewError.value = ''
  if (!moving.value || !item.value || !destinationEligible.value) return
  const path = `/folders/items/${item.value.kind}/${item.value.id}/placement`
  const {data, error} = await useMyFetch<MovePreview>(path, {method:'PUT', body:{parent_id: destination.value || null, preview:true}})
  if (request !== previewRequest) return
  if (error.value) previewError.value = error.value.data?.detail || error.value.message
  else preview.value = data.value
}
watch([moving, destination, destinationEligible], loadPreview, {flush: 'sync'})
async function confirmMove() {
  if (!preview.value || !destinationEligible.value || !item.value || busy.value) return
  const path = `/folders/items/${item.value.kind}/${item.value.id}/placement`
  const parent_id = destination.value || null
  if (await save(path, 'PUT', {parent_id, audience_version: preview.value.audience_version})) {
    moving.value = false
    expandPath(parent_id)
    emit('changed')
  } else await loadPreview()
}
const accessOpen = ref(false), inherit = ref(false)
const accessAgent = ref<DragAgent | null>(null)
function openAgentAccess(id: string) {
  accessAgent.value = props.agents.find(agent => agent.id === id) || null
  inherit.value = !accessAgent.value?.limited_access
  accessOpen.value = !!accessAgent.value
}
async function saveAgentAccess() {
  if (!accessAgent.value) return
  if (await save(`/folders/agents/${accessAgent.value.id}/inheritance`, 'PUT', {limited_access: !inherit.value})) {
    accessOpen.value = false
    emit('changed')
  }
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
    if (parent && !entries.length && !props.loading && !foldersById.value.get(parent)?.can_query && !foldersById.value.get(parent)?.capabilities.length) visible.push({kind: 'empty', key: 'empty:' + parent, depth})
    for (const row of entries) {
      visible.push({...row, depth})
      if (row.kind === 'folder' && expanded.value.has(row.folder.id)) visit(row.folder.id, depth + 1)
    }
  }
  visit(null, 0)
  return visible
})
function canDrag(agent?: DragAgent) {
  return !!agent?.can_move_from_folder && (!!agent.folder_id || managedFolders.value.length > 0)
}
function startDrag(event: DragEvent, agent: DragAgent) {
  if (!event.dataTransfer || !canDrag(agent)) { event.preventDefault(); return }
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
  return dragged.value && (dragged.value.folder_id || null) !== id && canDrag(dragged.value) && (!id || foldersById.value.get(id)?.can_manage)
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
  moveAgent(agent.id, id || '')
}
onBeforeUnmount(endDrag)
defineExpose({afterDelete, openMemberAccess, accessRevision, open, folders, current, moveAgent, openAgentAccess, canDrag, startDrag, endDrag, dragged, folderCreated})
onMounted(load)
// Follows the agents "Show all" toggle: off lists only folders shared with the caller.
watch(() => props.showAll, load)
</script>
