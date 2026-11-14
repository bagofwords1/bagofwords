<template>
  <section class="flex h-full min-h-0 w-full flex-col" data-testid="folder-knowledge">
    <header class="flex h-11 shrink-0 items-center gap-2 border-b border-line px-4 text-sm">
      <button type="button" class="flex min-w-0 items-center gap-1.5 rounded text-start font-medium hover:bg-surface-muted" @click="emit('section')">
        <UIcon name="i-heroicons-folder-open" class="size-4 shrink-0 text-ink-muted" />
        <span class="truncate">{{ folder.name }}</span>
      </button>
      <template v-if="section">
        <UIcon name="i-heroicons-chevron-right" class="size-3 shrink-0 text-ink-muted rtl:rotate-180" />
        <span class="text-ink-muted">{{ $t('agentsPage.' + section) }}</span>
      </template>
      <UButton v-if="!section && canEdit('instructions') && isTrainingModeEnabled" class="ms-auto" size="xs" variant="ghost" color="gray"
               icon="i-heroicons-academic-cap" :to="{path:'/reports/new', query:{mode:'training', folder:folder.id}}">
        {{ $t('prompt.training') }}
      </UButton>
    </header>
    <div class="min-h-0 flex-1 overflow-auto p-3">
    <template v-if="!section">
      <section class="mb-4">
        <h3 class="mb-1 px-2 text-xs font-medium text-ink-muted">{{ $t('nav.agents') }}</h3>
        <p v-if="!agents.length" class="py-2 text-sm text-ink-muted">{{ $t('nav.noAgents') }}</p>
        <button v-for="agent in agents" :key="agent.id" type="button"
          class="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-start hover:bg-surface-muted" @click="emit('agent', agent.id)">
          <DataSourceIcon :type="agent.type" :connector-key="agent.connector_key" :icon="agent.icon" :icon-token="agent.icon_token" class="mt-0.5 size-4 shrink-0" />
          <span class="min-w-0">
            <span class="block break-words text-sm font-medium">{{ agent.name }}</span>
            <span v-if="agent.description" class="mt-0.5 block break-words text-xs text-ink-muted">{{ agent.description }}</span>
          </span>
        </button>
      </section>
      <div class="border-t border-line pt-2">
        <button v-for="kind in sections" :key="kind" type="button" class="flex w-full items-center gap-2 rounded-md px-2 py-2 text-start text-sm hover:bg-surface-muted"
          @click="emit('section', kind)">
          <span class="grow">{{ $t('agentsPage.' + kind) }}</span>
          <span v-if="loaded.has(kind) && !errors[kind]" class="nums text-xs text-ink-muted">{{ items[kind].length }}</span>
          <UIcon name="i-heroicons-chevron-right" class="size-3 text-ink-muted rtl:rotate-180" />
        </button>
      </div>
    </template>
    <section v-for="kind in (section ? sections : [])" :key="kind" class="space-y-2">
      <div class="flex flex-wrap items-center gap-2">
        <UInput v-model="search" class="min-w-32 flex-1" size="sm" icon="i-heroicons-magnifying-glass" :placeholder="$t('common.search')" :aria-label="$t('common.search')" />
        <span v-if="loaded.has(kind)" class="nums text-xs text-ink-muted">{{ visibleItems(kind).length }}</span>
        <template v-if="canEdit(kind)">
          <UButton size="xs" variant="ghost" color="gray" icon="i-heroicons-link" @click="start(kind, 'attach')">{{ $t('sharedFolders.attach') }}</UButton>
          <UButton v-if="kind !== 'files'" size="xs" color="gray" variant="outline" icon="i-heroicons-plus" @click="start(kind, 'create')">{{ $t('agentsPage.addShort') }}</UButton>
        </template>
      </div>
      <template v-if="kind === 'files' && canEdit(kind)">
        <input ref="uploadInput" type="file" multiple hidden @change="chooseFiles" />
        <button type="button" class="flex w-full items-center justify-center gap-2 rounded-md border border-dashed px-3 py-5 text-sm"
          :class="dragging ? 'border-blue-500 bg-blue-50 text-blue-600 dark:bg-blue-950' : 'border-line text-ink-muted hover:border-blue-400'"
          :disabled="busy" @click="uploadInput?.[0]?.click()" @dragover.prevent="dragging = !busy"
          @dragleave.prevent="dragging = false" @drop.prevent="dropFiles">
          <Spinner v-if="uploading" class="size-4" /><UIcon v-else name="i-heroicons-paper-clip" class="size-4" />
          {{ $t(uploading ? 'projects.overview.uploading' : 'projects.overview.dropFiles') }}
        </button>
      </template>
      <p v-if="errors[kind]" class="py-2 text-sm text-red-600">{{ errors[kind] }}</p>
      <p v-else-if="loaded.has(kind) && !visibleItems(kind).length" class="px-2 py-2 text-xs text-ink-muted">
        {{ $t(search ? 'agentsPage.noResults' : emptyLabels[kind]) }}
      </p>
      <ul v-else>
        <li v-for="item in visibleItems(kind)" :key="item.id" class="group flex min-h-8 items-center gap-2 rounded-md px-2 text-sm hover:bg-surface-muted">
          <UIcon :name="icons[kind]" class="size-4 shrink-0 text-ink-muted" />
          <button v-if="kind === 'instructions'" class="min-w-0 flex-1 truncate py-1 text-start hover:underline" :title="item.name" @click="$emit('instruction', item.id)">{{ item.name }}</button>
          <button v-else-if="kind === 'files'" class="min-w-0 flex-1 truncate py-1 text-start hover:underline" :title="item.name" @click="$emit('file', {id:item.id, filename:item.name})">{{ item.name }}</button>
          <button v-else class="min-w-0 flex-1 truncate py-1 text-start hover:underline" :title="item.name" @click="inspect(item.id)">{{ item.name }}</button>
          <template v-if="kind === 'evals' && canEdit(kind)">
            <UButton v-if="item.owned" size="xs" color="gray" variant="ghost" @click="start(kind, 'case', item.id)">{{ $t('sharedFolders.addCase') }}</UButton>
            <UButton v-if="folder.can_query" size="xs" color="gray" variant="ghost" icon="i-heroicons-play" :loading="busy" @click="run(item.id)">{{ $t('agentsPage.tipRunSuite') }}</UButton>
          </template>
          <UButton v-if="canEdit(kind)" color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" :aria-label="$t('common.remove')" :title="$t('common.remove')" class="sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100" :disabled="busy" @click="change(kind, item.id, false)" />
        </li>
      </ul>
    </section>
    <section v-if="expanded && canEdit('evals')" class="mb-6 border-t border-line pt-3">
      <h3 class="text-sm font-medium">{{ $t('agentsPage.casesLabel') }}</h3>
      <button v-for="test in cases" :key="test.id" class="block py-2 text-start text-sm hover:underline" @click="editCase(test.id)">{{ test.name }}</button>
      <p v-if="!cases.length" class="py-2 text-xs text-ink-muted">{{ $t('agentsPage.noTestsInSuite') }}</p>
    </section>
    <p v-if="feedback && !show" role="status" class="text-sm text-ink-muted">{{ feedback }}</p>
    </div>
    <UModal v-model="show" :ui="{width:action === 'case' ? 'sm:max-w-4xl' : 'sm:max-w-md'}">
      <div v-if="action === 'case'" class="space-y-4 p-5">
        <TestCaseEditor ref="caseEditor" :folder-id="folder.id" :suite-id="suite" :case-id="caseId || undefined" @close="show = false" @created="caseSaved" @updated="caseSaved" />
        <div class="flex justify-end"><UButton color="blue" :loading="caseEditor?.isSaving" @click="caseEditor?.save()">{{ $t('common.save') }}</UButton></div>
      </div>
      <form v-else class="space-y-4 p-5" @submit.prevent="save">
        <h3 class="font-medium">{{ dialogTitle }}</h3>
        <USelect v-if="action === 'attach'" v-model="pick" :options="candidates" option-attribute="name" value-attribute="id" :aria-label="dialogTitle" />
        <UInput v-else v-model="name" required autofocus :placeholder="$t('agentsPage.starterTitlePlaceholder')" :aria-label="$t('agentsPage.starterTitlePlaceholder')" />
        <p v-if="feedback" role="alert" class="text-sm text-red-600">{{ feedback }}</p>
        <div class="flex justify-end gap-2">
          <UButton color="gray" variant="ghost" @click="show = false">{{ $t('common.cancel') }}</UButton>
          <UButton type="submit" color="blue" :loading="busy" :disabled="action === 'attach' && !pick">{{ $t('common.save') }}</UButton>
        </div>
      </form>
    </UModal>
  </section>
</template>
<script setup lang="ts">
import TestCaseEditor from "~/components/monitoring/TestCaseEditor.vue"
type Kind = 'instructions' | 'files' | 'evals'
type Item = { id: string, name: string, owned?: boolean }
const props = defineProps<{suiteId?: string, section?: Kind,
  agents: { id: string, name: string, description?: string, type?: string, connector_key?: string, icon?: string, icon_token?: string }[],
  folder: {id: string, name: string, capabilities: string[], can_query: boolean}}>()
const emit = defineEmits<{agent: [id: string], section: [kind?: Kind], changed: [], createInstruction: [folderId: string], instruction: [id: string], file:[item:{id:string, filename:string}]}>()
const { t } = useI18n()
const { isTrainingModeEnabled } = useOrgSettings()
const kinds: Kind[] = ['instructions', 'evals', 'files']
const canEdit = (kind: Kind) => props.folder.capabilities.includes('manage_' + kind)
const sections = computed(() => kinds.filter(k => (!props.section || props.section === k) && (canEdit(k) || (props.folder.can_query && k !== 'evals'))))
const items = reactive<Record<Kind, Item[]>>({instructions: [], files: [], evals: []})
const loaded = reactive(new Set<Kind>())
const errors = reactive<Record<Kind, string>>({instructions: '', files: '', evals: ''})
const url = (k: Kind) => '/folders/' + props.folder.id + '/knowledge/' + k
const active = ref<Kind>('instructions'), action = ref<'attach' | 'create' | 'case'>('create')
const show = ref(false), busy = ref(false), feedback = ref(''), name = ref('')
const pick = ref(''), suite = ref(''), candidates = ref<Item[]>([])
const expanded = ref(''), cases = ref<Item[]>([]), caseId = ref(''), caseEditor = ref<InstanceType<typeof TestCaseEditor>>()
async function inspect(id: string) {
  expanded.value = id
  const result = await useMyFetch<Item[]>(url('evals') + '/' + id + '/cases')
  if (expanded.value === id) cases.value = result.data.value || []
}
watch(() => props.suiteId, id => { if (id && canEdit('evals')) inspect(id) }, {immediate:true})
function editCase(id: string) {
  if (!items.evals.some(item => item.id === expanded.value && item.owned)) return
  suite.value = expanded.value; caseId.value = id; action.value = 'case'; show.value = true
}
async function caseSaved() {
  show.value = false
  await inspect(suite.value)
}
const dialogTitle = computed(() => t(action.value === 'attach' ? 'sharedFolders.attach' : action.value === 'case' ? 'sharedFolders.addCase' : {instructions:'agentsPage.newInstructionHeader',files:'agentPage.tables.addFiles',evals:'agentsPage.suiteNewTitle'}[active.value]))
async function load(kind: Kind) {
  const {data, error} = await useMyFetch<Item[]>(url(kind))
  loaded.add(kind)
  items[kind] = data.value || []
  errors[kind] = error.value ? t('common.error') : ''
}
watch(sections, list => Promise.all(list.map(load)), {immediate:true})
async function start(kind: Kind, operation: typeof action.value, id = '') {
  if (kind === 'instructions' && operation === 'create') { emit('createInstruction', props.folder.id); return }
  active.value = kind; action.value = operation; suite.value = id; caseId.value = ''
  name.value = ''; pick.value = ''; feedback.value = ''
  candidates.value = []; show.value = true
  if (operation === 'attach') {
    const {data, error} = await useMyFetch<Item[]>(url(kind) + '?candidates=true')
    candidates.value = data.value || []; pick.value = candidates.value[0]?.id || ''
    if (error.value) feedback.value = t('common.error')
  }
}
async function change(kind: Kind, id: string, attach: boolean) {
  busy.value = true
  try {
    const {error} = await useMyFetch(url(kind) + '/' + id, {method:attach ? 'PUT' : 'DELETE'})
    if (error.value) throw error.value
    if (!attach && kind === 'evals' && expanded.value === id) expanded.value = ''
    show.value = false; await load(kind); emit('changed')
  } catch { feedback.value = t('common.error') } finally { busy.value = false }
}
const search = ref('')
const visibleItems = (kind: Kind) => items[kind].filter(item => item.name.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()))
const icons = {instructions: 'i-heroicons-document-text', files: 'i-heroicons-paper-clip', evals: 'i-heroicons-check-circle'}
const emptyLabels = {instructions: 'agentsPage.noInstructions', files: 'agentsPage.noFiles', evals: 'agentsPage.noSuites'}
const uploadInput = ref<HTMLInputElement[]>()
const uploading = ref(false)
const dragging = ref(false)
function chooseFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  input.value = ''
  uploadFiles(files)
}
function dropFiles(event: DragEvent) {
  dragging.value = false
  uploadFiles(Array.from(event.dataTransfer?.files || []))
}
async function uploadFiles(files: File[]) {
  if (busy.value || !canEdit('files') || !files.length) return
  uploading.value = busy.value = true
  feedback.value = ''
  const failed: string[] = []
  try {
    for (const file of files) {
      const body = new FormData()
      body.append('file', file)
      try {
        const {error} = await useMyFetch(url('files'), {method: 'POST', body})
        if (error.value) failed.push(file.name)
      } catch { failed.push(file.name) }
    }
    if (failed.length) feedback.value = t('common.error') + ' · ' + failed.join(', ')
    if (failed.length < files.length) {
      await load('files')
      emit('changed')
    }
  } finally {
    uploading.value = busy.value = false
  }
}
async function save() {
  const kind = active.value
  if (action.value === 'attach') return change(kind, pick.value, true)
  const path = url(kind)
  const payload = {name: name.value}
  busy.value = true
  try {
    const {error} = await useMyFetch(path, {method:'POST', body:payload})
    if (error.value) throw error.value
    show.value = false; await load(kind); emit('changed')
  } catch { feedback.value = t('common.error') } finally { busy.value = false }
}
async function run(id: string) {
  busy.value = true
  try {
    const {data,error} = await useMyFetch<{id:string}>(url('evals') + '/' + id + '/runs', {method:'POST'})
    if (error.value || !data.value) throw error.value
    await navigateTo('/evals/runs/' + data.value.id)
  } catch { feedback.value = t('agentsPage.runSuiteFailed') } finally { busy.value = false }
}
</script>
