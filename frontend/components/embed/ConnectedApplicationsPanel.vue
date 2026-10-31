<template>
  <section class="pb-10 space-y-6" data-testid="connected-applications">
    <header class="flex items-start justify-between gap-4">
      <div>
        <h3 class="text-base font-semibold text-ink">{{ $t('connectedApps.title') }}</h3>
        <p class="mt-1 max-w-2xl text-sm text-ink-muted">{{ $t('connectedApps.subtitle') }}</p>
      </div>
      <UButton v-if="!selected" size="sm" icon="i-heroicons-plus" data-testid="application-add" @click="creating = true">{{ $t('connectedApps.add') }}</UButton>
      <UButton v-else size="sm" color="gray" variant="ghost" @click="selected = null">{{ $t('common.back') }}</UButton>
    </header>
    <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
    <Spinner v-if="loading" class="h-6 w-6 text-ink-muted" />
    <template v-else-if="selected">
      <div class="flex flex-wrap items-center gap-2">
        <h4 class="text-lg font-medium text-ink">{{ selected.name }}</h4>
        <UBadge color="gray" variant="soft">{{ selected.enabled ? $t('connectedApps.enabled') : $t('hostApps.disabled') }}</UBadge>
      </div>
      <nav class="flex flex-wrap gap-2 border-b border-line pb-3" :aria-label="$t('connectedApps.configuration')">
        <UButton v-for="item in tabs" :key="item.id" size="sm" :color="tab === item.id ? 'blue' : 'gray'" variant="soft" @click="tab = item.id">{{ item.label }}</UButton>
      </nav>
      <form v-if="tab === 'general'" class="max-w-xl space-y-5" @submit.prevent="save">
        <UFormGroup :label="$t('hostApps.name')"><UInput v-model="name" :maxlength="255" data-testid="application-name" /></UFormGroup>
        <label class="flex items-center gap-3 text-sm text-ink"><UToggle v-model="enabled" />{{ $t('connectedApps.enabled') }}</label>
        <p class="text-xs text-ink-muted">{{ $t('connectedApps.disableHelp') }}</p>
        <fieldset class="space-y-3">
          <legend class="mb-2 text-sm font-medium text-ink">{{ $t('connectedApps.agents') }}</legend>
          <p class="text-xs text-ink-muted">{{ $t('connectedApps.agentHelp') }}</p>
          <URadio v-model="allAgents" name="agent-access" :value="false" :label="$t('connectedApps.selectedAgents')" />
          <div v-if="!allAgents" class="max-h-60 overflow-y-auto rounded-lg border border-line p-3 space-y-2">
            <UCheckbox v-for="agent in agents" :key="agent.id" v-model="agentIds" :value="agent.id" :label="agent.name" />
            <p v-if="!agents.length" class="text-xs text-ink-muted">{{ $t('connectedApps.noAgents') }}</p>
          </div>
          <URadio v-model="allAgents" name="agent-access" :value="true" :label="$t('connectedApps.allAgents')" />
        </fieldset>
        <div class="flex justify-between gap-3">
          <UButton color="red" variant="ghost" :disabled="saving" @click="remove">{{ $t('common.delete') }}</UButton>
          <UButton type="submit" :loading="saving" data-testid="application-save">{{ $t('common.save') }}</UButton>
        </div>
      </form>
      <HostApplicationsPanel v-else-if="tab === 'embedding'" :key="selected.id" :application="selected" @updated="load" />
      <OAuthClientsModal v-else :key="selected.id" :application="selected" @updated="load" />
    </template>
    <div v-else-if="!applications.length" class="rounded-lg border border-dashed border-line-strong p-10 text-center text-sm text-ink-muted">{{ $t('connectedApps.empty') }}</div>
    <div v-else class="divide-y divide-line rounded-lg border border-line">
      <button v-for="app in applications" :key="app.id" class="flex w-full items-center justify-between gap-3 p-4 text-start hover:bg-surface-subtle" data-testid="application-row" @click="open(app)">
        <span class="text-sm font-medium text-ink">{{ app.name }}</span>
        <span class="flex flex-wrap gap-2">
          <UBadge v-if="app.embedding" color="gray" variant="soft">{{ $t('connectedApps.embedding') }}</UBadge>
          <UBadge v-if="app.oauth" color="gray" variant="soft">{{ $t('connectedApps.oauth') }}</UBadge>
          <UBadge v-if="!app.enabled" color="amber" variant="soft">{{ $t('hostApps.disabled') }}</UBadge>
        </span>
      </button>
    </div>
    <UModal v-model="creating">
      <UCard><template #header><h3 class="text-base font-medium text-ink">{{ $t('connectedApps.add') }}</h3></template>
        <form class="space-y-4" @submit.prevent="create">
          <UFormGroup :label="$t('hostApps.name')"><UInput v-model="newName" :maxlength="255" autofocus data-testid="application-new-name" /></UFormGroup>
          <p class="text-xs text-ink-muted">{{ $t('connectedApps.createHelp') }}</p>
          <p v-if="error" role="alert" class="text-sm text-red-600">{{ error }}</p>
          <div class="flex justify-end gap-2"><UButton color="gray" variant="ghost" @click="creating = false">{{ $t('common.cancel') }}</UButton><UButton type="submit" :disabled="!newName.trim()" :loading="saving" data-testid="application-create">{{ $t('common.create') }}</UButton></div>
        </form>
      </UCard>
    </UModal>
  </section>
</template>

<script setup lang="ts">
import HostApplicationsPanel from '~/components/embed/HostApplicationsPanel.vue'
import OAuthClientsModal from '~/components/OAuthClientsModal.vue'
import Spinner from '~/components/Spinner.vue'
const emit = defineEmits<{ (e: 'updated'): void }>()
const { t } = useI18n()
const { getErrorMessage } = useErrorMessage()
const toast = useToast()
const applications = ref<any[]>([])
const agents = ref<any[]>([])
const selected = ref<any>(null)
const tab = ref('general')
const loading = ref(true)
const saving = ref(false)
const creating = ref(false)
const error = ref('')
const newName = ref('')
const name = ref('')
const enabled = ref(true)
const allAgents = ref(false)
const agentIds = ref<string[]>([])
const tabs = computed(() => [
  { id: 'general', label: t('connectedApps.general') },
  { id: 'embedding', label: t('connectedApps.embedding') },
  { id: 'oauth', label: t('connectedApps.oauth') },
])
function open(app: any) {
  selected.value = app
  name.value = app.name
  enabled.value = app.enabled
  allAgents.value = app.agent_ids === null
  agentIds.value = [...(app.agent_ids || [])]
  tab.value = 'general'
  error.value = ''
}
async function load() {
  const res = await useMyFetch('/api/connected_applications')
  if (res.error.value) error.value = getErrorMessage(res.error.value)
  else {
    applications.value = (res.data.value as any[]) || []
    if (selected.value) selected.value = applications.value.find(a => a.id === selected.value.id) || null
    emit('updated')
  }
  loading.value = false
}
async function mutate(url: string, method: 'POST' | 'PATCH' | 'DELETE', body?: any) {
  saving.value = true
  error.value = ''
  try {
    const res = await useMyFetch(url, { method, body })
    if (res.error.value) { error.value = getErrorMessage(res.error.value); return null }
    await load()
    return res.data.value || true
  } finally { saving.value = false }
}
async function create() {
  const app = await mutate('/api/connected_applications', 'POST', { name: newName.value.trim() })
  if (app) { creating.value = false; newName.value = ''; open(app) }
}
async function save() {
  const app = await mutate(`/api/connected_applications/${selected.value.id}`, 'PATCH', { name: name.value.trim(), enabled: enabled.value, agent_ids: allAgents.value ? null : agentIds.value })
  if (app) { open(app); toast.add({ title: t('connectedApps.saved') }) }
}
async function remove() {
  if (!confirm(t('connectedApps.deleteConfirm', { name: selected.value.name }))) return
  if (await mutate(`/api/connected_applications/${selected.value.id}`, 'DELETE')) selected.value = null
}
onMounted(async () => {
  await load()
  const res = await useMyFetch('/api/data_sources', { query: { show_all: true } })
  if (res.error.value) error.value = getErrorMessage(res.error.value)
  else agents.value = (res.data.value as any[]) || []
  const id = useRoute().query.application
  const app = applications.value.find(a => a.id === id)
  if (app) open(app)
})
</script>
