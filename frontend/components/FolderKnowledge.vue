<template>
  <section class="mx-auto w-full max-w-3xl overflow-y-auto px-5 py-6 sm:px-8" data-testid="folder-knowledge">
    <header class="mb-6 flex items-center gap-3">
      <UIcon name="i-heroicons-folder-open" class="size-6 shrink-0 text-ink-muted" />
      <h2 class="break-words text-lg font-semibold">{{ folder.name }}</h2>
    </header>
    <section v-for="kind in sections" :key="kind" class="mb-6 border-t border-gray-200 pt-4 dark:border-gray-800">
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="me-auto text-sm font-medium">{{ $t('agentsPage.' + kind) }}</h3>
        <template v-if="canEdit(kind)">
          <UButton size="xs" variant="ghost" color="gray" icon="i-heroicons-link" @click="start(kind, 'attach')">{{ $t('sharedFolders.attach') }}</UButton>
          <UButton size="xs" color="gray" :icon="kind === 'files' ? 'i-heroicons-arrow-up-tray' : 'i-heroicons-plus'" @click="start(kind, 'create')">{{ $t(kind === 'files' ? 'files.sourceUpload' : 'common.create') }}</UButton>
        </template>
      </div>
      <p v-if="errors[kind]" class="py-2 text-sm text-red-600">{{ errors[kind] }}</p>
      <ul v-else class="mt-2 divide-y divide-gray-100 dark:divide-gray-800">
        <li v-for="item in items[kind]" :key="item.id" class="flex min-h-9 items-center gap-2 text-sm">
          <button v-if="kind === 'instructions'" class="me-auto text-start hover:underline" @click="$emit('instruction', item.id)">{{ item.name }}</button>
          <button v-else-if="kind === 'files'" class="me-auto text-start hover:underline" @click="$emit('file', {id:item.id, filename:item.name})">{{ item.name }}</button>
          <button v-else class="me-auto text-start hover:underline" @click="inspect(item.id)">{{ item.name }}</button>
          <template v-if="kind === 'evals' && canEdit(kind)">
            <UButton v-if="item.owned" size="xs" color="gray" variant="ghost" @click="start(kind, 'case', item.id)">{{ $t('sharedFolders.addCase') }}</UButton>
            <UButton v-if="folder.can_query" size="xs" color="gray" variant="ghost" icon="i-heroicons-play" :loading="busy" @click="run(item.id)">{{ $t('agentsPage.tipRunSuite') }}</UButton>
          </template>
          <UButton v-if="canEdit(kind)" color="gray" variant="ghost" size="xs" icon="i-heroicons-minus" :aria-label="$t('common.remove')" :disabled="busy" @click="change(kind, item.id, false)" />
        </li>
      </ul>
    </section>
    <section v-if="expanded && canEdit('evals')" class="mb-6 border-t border-line pt-3">
      <h3 class="text-sm font-medium">{{ $t('agentsPage.casesLabel') }}</h3>
      <button v-for="test in cases" :key="test.id" class="block py-2 text-start text-sm hover:underline" @click="editCase(test.id)">{{ test.name }}</button>
      <p v-if="!cases.length" class="py-2 text-xs text-ink-muted">{{ $t('agentsPage.noTestsInSuite') }}</p>
    </section>
    <p v-if="feedback && !show" role="status" class="text-sm text-ink-muted">{{ feedback }}</p>
    <UModal v-model="show" :ui="{width:action === 'case' ? 'sm:max-w-4xl' : 'sm:max-w-md'}">
      <div v-if="action === 'case'" class="space-y-4 p-5">
        <TestCaseEditor ref="caseEditor" :folder-id="folder.id" :suite-id="suite" :case-id="caseId || undefined" @close="show = false" @created="caseSaved" @updated="caseSaved" />
        <div class="flex justify-end"><UButton color="black" :loading="caseEditor?.isSaving" @click="caseEditor?.save()">{{ $t('common.save') }}</UButton></div>
      </div>
      <form v-else class="space-y-4 p-5" @submit.prevent="save">
        <h3 class="font-medium">{{ dialogTitle }}</h3>
        <USelect v-if="action === 'attach'" v-model="pick" :options="candidates" option-attribute="name" value-attribute="id" :aria-label="dialogTitle" />
        <input v-else-if="active === 'files'" type="file" required :aria-label="dialogTitle" @change="file = ($event.target as HTMLInputElement).files?.[0]" />
        <template v-else>
          <UInput v-model="name" required autofocus :placeholder="$t('agentsPage.starterTitlePlaceholder')" :aria-label="$t('agentsPage.starterTitlePlaceholder')" />
          <UTextarea v-if="active === 'instructions'" v-model="body" required :placeholder="$t('agentsPage.instructions')" />
          <USelect v-if="active === 'instructions'" v-model="mode" :options="modes" />
        </template>
        <p v-if="feedback" role="alert" class="text-sm text-red-600">{{ feedback }}</p>
        <div class="flex justify-end gap-2">
          <UButton color="gray" variant="ghost" @click="show = false">{{ $t('common.cancel') }}</UButton>
          <UButton type="submit" color="black" :loading="busy" :disabled="action === 'attach' && !pick">{{ $t('common.save') }}</UButton>
        </div>
      </form>
    </UModal>
  </section>
</template>
<script setup lang="ts">
import TestCaseEditor from "~/components/monitoring/TestCaseEditor.vue"
type Kind = 'instructions' | 'files' | 'evals'
type Item = { id: string, name: string, owned?: boolean }
const props = defineProps<{folder: {id: string, name: string, capabilities: string[], can_query: boolean}}>()
defineEmits<{instruction: [id: string], file:[item:{id:string, filename:string}]}>()
const { t } = useI18n()
const kinds: Kind[] = ['instructions', 'files', 'evals']
const canEdit = (kind: Kind) => props.folder.capabilities.includes('manage_' + kind)
const sections = computed(() => kinds.filter(k => canEdit(k) || (props.folder.can_query && k !== 'evals')))
const items = reactive<Record<Kind, Item[]>>({instructions: [], files: [], evals: []})
const errors = reactive<Record<Kind, string>>({instructions: '', files: '', evals: ''})
const url = (k: Kind) => '/folders/' + props.folder.id + '/knowledge/' + k
const active = ref<Kind>('instructions'), action = ref<'attach' | 'create' | 'case'>('create')
const show = ref(false), busy = ref(false), feedback = ref(''), name = ref(''), body = ref(''), mode = ref('always')
const file = ref<File>(), pick = ref(''), suite = ref(''), candidates = ref<Item[]>([])
const expanded = ref(''), cases = ref<Item[]>([]), caseId = ref(''), caseEditor = ref<InstanceType<typeof TestCaseEditor>>()
async function inspect(id: string) {
  expanded.value = id
  const result = await useMyFetch<Item[]>(url('evals') + '/' + id + '/cases')
  if (expanded.value === id) cases.value = result.data.value || []
}
function editCase(id: string) {
  if (!items.evals.some(item => item.id === expanded.value && item.owned)) return
  suite.value = expanded.value; caseId.value = id; action.value = 'case'; show.value = true
}
async function caseSaved() {
  show.value = false
  await inspect(suite.value)
}
const modes = computed(() => ['always', 'intelligent'].map(value => ({value, label:t(value === 'always' ? 'agentsPage.optLoadAlways' : 'agentsPage.optLoadSmart')})))
const dialogTitle = computed(() => t(action.value === 'attach' ? 'sharedFolders.attach' : action.value === 'case' ? 'sharedFolders.addCase' : {instructions:'agentsPage.newInstructionHeader',files:'agentPage.tables.addFiles',evals:'agentsPage.suiteNewTitle'}[active.value]))
async function load(kind: Kind) {
  const {data, error} = await useMyFetch<Item[]>(url(kind))
  items[kind] = data.value || []
  errors[kind] = error.value ? t('common.error') : ''
}
watch(sections, list => Promise.all(list.map(load)), {immediate:true})
async function start(kind: Kind, operation: typeof action.value, id = '') {
  active.value = kind; action.value = operation; suite.value = id; caseId.value = ''
  name.value = ''; body.value = ''; file.value = undefined; pick.value = ''; feedback.value = ''; mode.value = 'always'
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
    show.value = false; await load(kind)
  } catch { feedback.value = t('common.error') } finally { busy.value = false }
}
async function save() {
  const kind = active.value
  if (action.value === 'attach') return change(kind, pick.value, true)
  const path = url(kind)
  let payload: FormData | Record<string, unknown>
  if (kind === 'files') {
    if (!file.value) return
    payload = new FormData(); payload.append('file', file.value)

  } else payload = kind === 'evals' ? {name:name.value} : {title:name.value, text:body.value, load_mode:mode.value}
  busy.value = true
  try {
    const {error} = await useMyFetch(path, {method:'POST', body:payload})
    if (error.value) throw error.value
    show.value = false; await load(kind)
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
