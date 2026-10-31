<template>
  <!-- Settings → Integrations → Host applications: the external systems
       (ERP, ServiceNow, portals) that may show artifacts and sign their users
       in. One per system; artifacts opt in from their own Embed tab. -->
  <section class="pb-10" data-testid="host-apps">
    <header class="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t(application ? 'connectedApps.embedding' : 'hostApps.title') }}</h3>
        <p class="mt-1 max-w-2xl text-sm leading-6 text-gray-500 dark:text-gray-400">{{ $t(application ? 'connectedApps.embeddingHelp' : 'hostApps.subtitle') }}</p>
      </div>
      <UButton v-if="!editing && (!application || !hosts.length)" color="gray" variant="soft" size="sm" icon="i-heroicons-plus" data-testid="host-app-add" @click="editing = 'new'">
        {{ $t(application ? 'connectedApps.configureEmbedding' : 'hostApps.add') }}
      </UButton>
    </header>

    <div v-if="editing" class="mt-6 max-w-xl">
      <h4 class="mb-4 text-sm font-semibold text-gray-900 dark:text-white">
        {{ editing === 'new' ? $t(application ? 'connectedApps.configureEmbedding' : 'hostApps.add') : $t('hostApps.edit', { name: editing.name }) }}
      </h4>
      <HostApplicationForm :application="application" :host="editing === 'new' ? null : editing" @saved="onSaved" @cancel="editing = null" />
    </div>

    <div v-else-if="loading" class="flex items-center justify-center py-20">
      <Spinner class="h-6 w-6 text-ink-muted" />
    </div>

    <div v-else-if="hosts.length === 0" class="mt-6 rounded-xl border border-dashed border-gray-300 px-6 py-12 text-center dark:border-gray-700">
      <UIcon name="i-heroicons-window" class="mx-auto h-6 w-6 text-ink-muted" />
      <h4 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">{{ $t(application ? 'connectedApps.embeddingEmpty' : 'hostApps.noneYet') }}</h4>
      <p class="mx-auto mt-1 max-w-sm text-xs leading-5 text-gray-500 dark:text-gray-400">{{ $t('hostApps.emptyHelp') }}</p>
    </div>

    <div v-else class="mt-6 divide-y divide-gray-100 border-y border-gray-100 dark:divide-gray-800 dark:border-gray-800">
      <article v-for="host in hosts" :key="host.id" data-testid="host-app-row" class="py-4">
        <div class="flex items-start gap-3">
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-2">
              <h4 class="truncate text-sm font-medium text-gray-900 dark:text-white">{{ host.name }}</h4>
              <span class="text-2xs text-gray-500 dark:text-gray-400">{{ modeLabel(host.identity_mode) }}</span>
              <span v-if="!host.enabled" class="text-2xs text-amber-600 dark:text-amber-400">· {{ $t('hostApps.disabled') }}</span>
            </div>
            <p class="mt-1 truncate text-xs text-gray-500 dark:text-gray-400" dir="ltr">
              <span class="font-mono">{{ host.public_id }}</span>
              <span class="mx-1.5 text-gray-300 dark:text-gray-600">·</span>{{ host.allowed_origins.join(', ') || '—' }}
            </p>
            <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
              <template v-if="host.identity_source">
                <span dir="ltr">{{ host.identity_source }}</span>{{ host.create_users ? ` · ${$t('hostApps.createsUsers')}` : '' }}
                <span class="mx-1.5 text-gray-300 dark:text-gray-600">·</span>
              </template>
              {{ $t('hostApps.usageValue', { sessions: host.active_sessions, artifacts: host.artifacts }) }}
            </p>
          </div>
          <UToggle v-if="!application" :model-value="host.enabled" :loading="busy === host.id" size="sm" data-testid="host-app-enabled"
                   @update:model-value="setEnabled(host, $event)" />
          <UDropdown :items="actions(host)" :popper="{ placement: 'bottom-end' }">
            <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-ellipsis-horizontal" :aria-label="$t('hostApps.actions')" />
          </UDropdown>
        </div>
        <div v-if="rotatedKey[host.id]" class="mt-3">
          <p class="mb-2 text-xs text-amber-700 dark:text-amber-300">{{ $t('hostApps.keyOnce') }}</p>
          <EmbedCode :code="rotatedKey[host.id]" />
          <div class="mt-2 flex justify-end"><UButton size="xs" color="gray" variant="soft" @click="delete rotatedKey[host.id]">{{ $t('hostApps.keySaved') }}</UButton></div>
        </div>
        <details v-if="host.identity_mode === 'assertion'" class="group mt-2 text-xs">
          <summary class="cursor-pointer list-none text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200">
            <UIcon name="i-heroicons-chevron-right" class="me-0.5 h-3 w-3 align-[-2px] transition-transform group-open:rotate-90 rtl:rotate-180 rtl:group-open:rotate-90" />
            {{ $t('hostApps.backendExample') }}
          </summary>
          <p class="mb-2 mt-2 text-gray-500 dark:text-gray-400">{{ $t('hostApps.backendExampleHelp') }}</p>
          <EmbedCode :code="backendExample(host, lang)" :langs="EXAMPLE_LANGS" v-model="lang" testid="host-app-backend-example" />
        </details>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import HostApplicationForm from '~/components/embed/HostApplicationForm.vue'
import EmbedCode from '~/components/embed/EmbedCode.vue'
import { EXAMPLE_LANGS, backendExample, type ExampleLang } from '~/utils/embedExamples'

const props = defineProps<{ application?: Record<string, any> }>()
const emit = defineEmits<{ (e: 'updated'): void }>()
const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const hosts = ref<any[]>([])
const loading = ref(true)
const editing = ref<any>(null)
const busy = ref<string | null>(null)
const rotatedKey = reactive<Record<string, string>>({})
const lang = ref<ExampleLang>('python')

const modeLabel = (mode: string) => ({
  assertion: t('hostApps.modeAssertion'), idp_token: t('hostApps.modeIdp'), sso: t('hostApps.modeSso'),
} as Record<string, string>)[mode] || mode

async function load() {
  const { data } = await useMyFetch('/api/host_applications')
  hosts.value = ((data.value as any[]) || []).filter(h => !props.application || h.id === props.application.id)
  loading.value = false
}

function onSaved() {
  editing.value = null
  load()
  emit('updated')
}

async function call(host: any, path: string, opts: any, done?: (value: any) => void) {
  busy.value = host.id
  try {
    const { data, error } = await useMyFetch(`/api/host_applications/${host.id}${path}`, opts)
    if (error.value) { toast.add({ title: getErrorMessage(error.value), color: 'red' }); return }
    done?.(data.value)
    await load()
    emit('updated')
  } finally {
    busy.value = null
  }
}

const setEnabled = (host: any, enabled: boolean) =>
  call(host, '', { method: 'PATCH', body: { revision: host.revision, enabled } })

function actions(host: any) {
  const items: any[] = [{ label: t('hostApps.editAction'), icon: 'i-heroicons-pencil-square', click: () => { editing.value = host } }]
  if (host.identity_mode === 'assertion') {
    items.push({
      label: t('hostApps.rotateKey'), icon: 'i-heroicons-arrow-path',
      click: () => confirm(t('hostApps.rotateConfirm')) && call(host, '/rotate_key', { method: 'POST' },
        (v) => { if (v?.private_key) rotatedKey[host.id] = v.private_key }),
    })
  }
  items.push({
    label: t('hostApps.revokeSessions'), icon: 'i-heroicons-no-symbol',
    click: () => confirm(t('hostApps.revokeConfirm')) && call(host, '/revoke_sessions', { method: 'POST' },
      () => toast.add({ title: t('hostApps.revoked') })),
  })
  if (!props.application) items.push({
    label: t('hostApps.delete'), icon: 'i-heroicons-trash',
    click: () => confirm(t('hostApps.deleteConfirm', { name: host.name })) && call(host, '', { method: 'DELETE' }),
  })
  return [items]
}

onMounted(load)
</script>
