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
    <div class="min-h-0 flex-1 overflow-auto" :class="{'p-3': !section}">
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
    <AgentFilesPanel v-else-if="section === 'files' && sections.includes('files')" class="px-6 py-4"
      :folder-id="folder.id" :can-update="canEdit('files')" @open="emit('file', $event)" @changed="emit('changed')" />
    <ScopedInstructionsPanel v-else-if="section === 'instructions' && sections.includes('instructions')"
      :search="search" :loading="!loaded.has('instructions')" :count="filteredInstructions.length" :can-add="canEdit('instructions')"
      @search="search = $event" @add="emit('createInstruction', folder.id)">
      <template #actions><UButton v-if="canEdit('instructions')" size="xs" color="gray" variant="ghost" icon="i-heroicons-link" :title="$t('sharedFolders.attach')" :aria-label="$t('sharedFolders.attach')" @click="start('instructions', 'attach')" /></template>
      <div v-for="item in filteredInstructions" :key="item.id" class="group flex items-center">
        <slot name="instruction" :item="item" />
        <UButton v-if="canEdit('instructions')" class="opacity-0 group-hover:opacity-100 focus:opacity-100" size="xs" color="gray" variant="ghost" icon="i-heroicons-x-mark" :aria-label="$t('common.remove')" @click="change('instructions', item.id, false)" />
      </div>
      <slot v-if="!filteredInstructions.length" name="empty" :search="search" :can-add="canEdit('instructions')" />
    </ScopedInstructionsPanel>
    <AgentEvalsPanel v-else-if="section === 'evals' && canEdit('evals') && loaded.has('evals')" :key="suiteId"
      :folder-id="folder.id" :folder-can-query="folder.can_query" :folder-suites="items.evals" :initial-suite-id="suiteId"
      @create-suite="start('evals', 'create')">
      <template #actions>
        <UDropdown :items="suiteActions" :popper="{placement:'bottom-end'}">
          <UButton size="xs" variant="ghost" color="gray" icon="i-heroicons-ellipsis-horizontal" :aria-label="$t('agentsPage.evals')" />
        </UDropdown>
      </template>
    </AgentEvalsPanel>
    <p v-if="section && errors[section]" role="alert" class="px-3 text-sm text-red-600">{{errors[section]}}</p>
    <p v-if="feedback && !show" role="status" class="text-sm text-ink-muted">{{ feedback }}</p>
    </div>
    <UModal v-model="show" :ui="{width:'sm:max-w-md'}">
      <form class="space-y-4 p-5" @submit.prevent="save">
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
import AgentFilesPanel from "~/components/datasources/AgentFilesPanel.vue"
type Kind = 'instructions' | 'files' | 'evals'
type Item = { id: string, name: string, owned?: boolean, text?: string }
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
const active = ref<'instructions' | 'evals'>('instructions'), action = ref<'attach' | 'create'>('attach')
const show = ref(false), busy = ref(false), feedback = ref(''), name = ref('')
const pick = ref(''), candidates = ref<Item[]>([])
const search = ref('')
const filteredInstructions = computed(() => items.instructions.filter(item =>
  [item.name, item.text].some(value => value?.toLocaleLowerCase().includes(search.value.trim().toLocaleLowerCase()))))
const suiteActions = computed(() => [
  [{label:t('agentsPage.suiteNewTitle'), click:() => start('evals','create')},
   {label:t('sharedFolders.attach'), click:() => start('evals','attach')}],
  items.evals.map(suite => ({label:t('common.remove') + ' · ' + suite.name, click:() => change('evals',suite.id,false)})),
].filter(group => group.length))
const dialogTitle = computed(() => t(action.value === 'attach' ? 'sharedFolders.attach' : 'agentsPage.suiteNewTitle'))
async function load(kind: Kind) {
  const {data, error} = await useMyFetch<Item[]>(url(kind) + (props.section === 'instructions' ? '?details=true' : ''))
  loaded.add(kind)
  items[kind] = data.value || []
  errors[kind] = error.value ? t('common.error') : ''
}
watch(sections, list => Promise.all(list.filter(k => !(props.section === 'files' && k === 'files')).map(load)), {immediate:true})
async function start(kind: 'instructions' | 'evals', operation: typeof action.value) {
  if (kind === 'instructions' && operation === 'create') { emit('createInstruction', props.folder.id); return }
  active.value = kind; action.value = operation
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
    show.value = false; await load(kind); emit('changed')
  } catch { feedback.value = t('common.error') } finally { busy.value = false }
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
</script>
