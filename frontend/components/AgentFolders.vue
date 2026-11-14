<template>
  <section :aria-label="$t('sharedFolders.title')" v-if="folders.length || canCreate || editing" class="px-2 py-1 space-y-1">
    <div class="flex items-center gap-1 text-xs">
      <button v-if="selected" :aria-label="$t('sharedFolders.back')" @click="open(current?.parent_id ?? null)">
        <UIcon name="i-heroicons-chevron-left" class="rtl:rotate-180" />
      </button>
      <strong class="grow truncate">{{ current?.name || $t('sharedFolders.title') }}</strong>
      <UButton v-if="canEdit" size="2xs" color="gray" variant="ghost" icon="i-heroicons-folder-plus"
        :aria-label="$t('sharedFolders.create')" @click="creating = !creating" />
      <UButton v-if="current?.can_manage" size="2xs" color="gray" variant="ghost" icon="i-heroicons-pencil-square"
        :aria-label="$t('sharedFolders.edit')" @click="editItem('folder', current.id, current.name)" />
      <UButton v-if="current?.can_manage" size="2xs" color="gray" variant="ghost" icon="i-heroicons-user-plus"
        :aria-label="$t('sharedFolders.share')" @click="openShare()" />
    </div>
    <form v-if="creating" class="flex gap-1" @submit.prevent="create">
      <UInput v-model="name" autofocus required :placeholder="$t('sharedFolders.name')" />
      <UButton type="submit" size="xs" :loading="busy">{{ $t('sharedFolders.create') }}</UButton>
    </form>
    <div v-if="dragged" class="space-y-1 border-y border-line py-1" @dragend="endDrag">
      <button type="button" class="w-full rounded px-2 py-1 text-start text-xs" :class="dropClass(null)"
        @click="dragParent = null" @dragover="hoverDestination($event, null)" @dragleave="cancelBrowse" @drop="dropAgent($event, null)">
        {{ $t('sharedFolders.root') }}
      </button>
      <div v-if="dragParent" class="flex items-center gap-1">
        <UButton icon="i-heroicons-chevron-left" size="xs" variant="ghost" color="gray" :aria-label="$t('sharedFolders.back')"
          @dragover.prevent="scheduleBrowse(foldersById.get(dragParent)?.parent_id || null)" @dragleave="cancelBrowse" />
        <button class="truncate text-xs" :class="dropClass(dragParent)"
          @dragover="hoverDestination($event, dragParent)" @drop="dropAgent($event, dragParent)">{{ foldersById.get(dragParent)?.name }}</button>
      </div>
      <button v-for="folder in dragChildren" :key="folder.id" type="button"
        class="flex w-full items-center gap-2 rounded px-2 py-1 text-start text-sm" :class="dropClass(folder.id)"
        @dragenter="hoverDestination($event, folder.id)" @dragover="hoverDestination($event, folder.id)" @dragleave="leaveDestination" @drop="dropAgent($event, folder.id)">
        <UIcon name="i-heroicons-folder" /><span class="truncate">{{ folder.name }}</span>
      </button>
    </div>
    <button v-for="child in (dragged ? [] : children)" :key="child.id" class="flex w-full items-center gap-2 rounded px-2 py-1 hover:bg-surface-muted"
      @click="open(child.id)">
      <UIcon name="i-heroicons-folder" class="shrink-0" /><span class="min-w-0 truncate" :title="child.name">{{ child.name }}</span>
      <UIcon v-if="child.limited_access" name="i-heroicons-lock-closed" :aria-label="$t('sharedFolders.limit')" />
    </button>
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
              <UButton type="submit" color="gray" :disabled="!editName.trim() || editName.trim() === item.name">{{ $t('sharedFolders.rename') }}</UButton>
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
          <UButton class="ms-auto" color="black" :loading="busy" :disabled="destination === originalDestination || (destination ? !destinationFolder?.can_manage : item.kind === 'folder' && !canCreate)" @click="move">{{ $t(preview ? 'sharedFolders.move' : 'sharedFolders.preview') }}</UButton>
        </footer>
      </div>
    </UModal>
    <UModal v-model="sharing" :ui="{ width: 'sm:max-w-md' }">
      <div class="flex max-h-[85dvh] flex-col">
        <header class="flex items-center justify-between gap-3 px-5 pb-3 pt-4">
          <h2 class="min-w-0 truncate text-base font-semibold" :title="current?.name">{{ $t('dashboardCollab.share.shareNamed', {name: current?.name}) }}</h2>
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
          <label v-if="current?.parent_id" class="flex items-center gap-2 text-sm" :title="$t('sharedFolders.stops')">
            <input type="checkbox" :checked="!current.limited_access" :disabled="busy" @change="limit" />{{ $t('sharedFolders.inherit') }}
          </label>
          <UButton color="black" class="ms-auto" @click="sharing = false">{{ $t('dashboardCollab.share.done') }}</UButton>
        </footer>
      </div>
    </UModal>
  </section>
</template>

<script setup lang="ts">
type Folder = { id: string, parent_id: string | null, name: string, limited_access: boolean, can_manage: boolean, can_query: boolean, capabilities: string[] }
type Recipient = { id: string, type: string, name: string, role: string | null, inherited?: {role: string, source: string, source_id: string}[] }
const emit = defineEmits<{ select: [id: string | null], changed: [] }>()
const folders = ref<Folder[]>([])
const selected = ref<string | null>(null)
const current = computed(() => folders.value.find(f => f.id === selected.value))
const children = computed(() => folders.value.filter(f => f.parent_id === selected.value))
const canCreate = ref(false)
const canEdit = computed(() => selected.value ? current.value?.can_manage : canCreate.value)
const creating = ref(false), sharing = ref(false), busy = ref(false)
const name = ref(''), audience = ref<Recipient[]>([])
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
  creating.value = false
  emit('select', id)
}
async function load() {
  const { data } = await useMyFetch<{ folders: Folder[], can_create: boolean }>('/folders')
  folders.value = data.value?.folders || []
  canCreate.value = !!data.value?.can_create
  if (selected.value && !current.value) open(null)
}
async function inspectSource(id: string) {
  if (!foldersById.value.get(id)?.can_manage) return
  sharing.value = false
  open(id)
  await openShare()
}
async function openShare(keepSearch = false) {
  if (keepSearch !== true) search.value = ''
  const { data, error } = await useMyFetch<Recipient[]>(`/folders/${selected.value}/audience`)
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
async function create() {
  if (await save('/folders', 'POST', { name: name.value, parent_id: selected.value })) {
    name.value = ''
    creating.value = false
  }
}
async function setRole(entry: Recipient, role: string) {
  if (await save(`/folders/${selected.value}/share`, 'PUT', { principal_id: entry.id, principal_type: entry.type, role: role || null })) {
    if (current.value?.can_manage) await openShare(true)
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
  if (!await save(`/folders/${selected.value}/inheritance`, 'PUT', { limited_access: !input.checked })) {
    input.checked = !input.checked
    return
  }
  if (current.value?.can_manage) await openShare(true)
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
function editItem(kind: 'agent' | 'folder', id: string, name: string, limited = false) {
  item.value = {kind, id, name, limited}
  editName.value = name
  destination.value = kind === 'folder' ? current.value?.parent_id || '' : selected.value || ''
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
type DragAgent = {id: string, name: string, folder_id?: string | null, limited_access?: boolean}
const dragged = ref<DragAgent | null>(null)
const dragParent = ref<string | null>(null)
const dragChildren = computed(() => folders.value.filter(f => f.parent_id === dragParent.value))
let browseTimer: ReturnType<typeof setTimeout> | undefined
let pendingFolder: string | null | undefined
function cancelBrowse() {
  clearTimeout(browseTimer)
  pendingFolder = undefined
}
function leaveDestination(event: DragEvent) {
  if (!(event.relatedTarget instanceof Node) || !(event.currentTarget as Node).contains(event.relatedTarget)) cancelBrowse()
}
function scheduleBrowse(id: string | null) {
  if (pendingFolder === id) return
  cancelBrowse()
  pendingFolder = id
  browseTimer = setTimeout(() => { dragParent.value = id; cancelBrowse() }, 750)
}
function canDrag(source?: string | null) {
  return !source || !!foldersById.value.get(source)?.can_manage
}
function startDrag(event: DragEvent, agent: DragAgent) {
  if (!event.dataTransfer || !canDrag(agent.folder_id)) { event.preventDefault(); return }
  event.dataTransfer.effectAllowed = 'move'
  event.dataTransfer.setData('application/x-bow-agent', agent.id)
  dragged.value = agent
  dragParent.value = agent.folder_id || null
}
function endDrag() {
  cancelBrowse()
  dragged.value = null
}
function validDrop(id: string | null) {
  return dragged.value && (dragged.value.folder_id || null) !== id && canDrag(id)
}
function dropClass(id: string | null) {
  return validDrop(id) ? 'bg-surface-muted text-ink' : 'text-ink-muted'
}
function hoverDestination(event: DragEvent, id: string | null) {
  if (!dragged.value) return
  if (validDrop(id)) event.preventDefault()
  if (id !== dragParent.value && (!id || folders.value.some(f => f.parent_id === id))) scheduleBrowse(id)
}
async function dropAgent(event: DragEvent, id: string | null) {
  event.preventDefault()
  const agent = dragged.value
  if (!agent || !validDrop(id)) return
  endDrag()
  editItem('agent', agent.id, agent.name, agent.limited_access)
  originalDestination.value = agent.folder_id || ''
  browseDestination(id || '')
  await move()
}
onBeforeUnmount(endDrag)
defineExpose({open, folders, current, editItem, canDrag, startDrag, endDrag})
onMounted(load)
</script>
