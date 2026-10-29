<template>
  <!-- Settings → Integrations → Host applications: the external systems
       (ERP, ServiceNow, portals) that may show artifacts and sign their users
       in. One per system; artifacts opt in from their own Embed tab. -->
  <section class="pb-10" data-testid="host-apps">
    <header class="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('hostApps.title') }}</h3>
        <p class="mt-1 max-w-2xl text-sm leading-6 text-gray-500 dark:text-gray-400">{{ $t('hostApps.subtitle') }}</p>
      </div>
      <UButton v-if="!editing" color="blue" size="sm" icon="i-heroicons-plus" data-testid="host-app-add" @click="editing = 'new'">
        {{ $t('hostApps.add') }}
      </UButton>
    </header>

    <div v-if="editing" class="mt-6 max-w-xl rounded-xl border border-gray-200 p-5 dark:border-gray-800">
      <h4 class="mb-4 text-sm font-semibold text-gray-900 dark:text-white">
        {{ editing === 'new' ? $t('hostApps.add') : $t('hostApps.edit', { name: editing.name }) }}
      </h4>
      <HostApplicationForm :host="editing === 'new' ? null : editing" @saved="onSaved" @cancel="editing = null" />
    </div>

    <div v-else-if="loading" class="flex items-center justify-center py-20">
      <Spinner class="h-6 w-6 text-ink-muted" />
    </div>

    <div v-else-if="hosts.length === 0" class="mt-6 rounded-xl border border-dashed border-gray-300 px-6 py-12 text-center dark:border-gray-700">
      <UIcon name="i-heroicons-window" class="mx-auto h-6 w-6 text-ink-muted" />
      <h4 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">{{ $t('hostApps.noneYet') }}</h4>
      <p class="mx-auto mt-1 max-w-sm text-xs leading-5 text-gray-500 dark:text-gray-400">{{ $t('hostApps.emptyHelp') }}</p>
    </div>

    <div v-else class="mt-6 space-y-4">
      <article v-for="host in hosts" :key="host.id" data-testid="host-app-row"
               class="rounded-xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
        <div class="flex items-start gap-3 px-4 py-3">
          <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-xs font-semibold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
            {{ host.name.slice(0, 2).toUpperCase() }}
          </div>
          <div class="min-w-0 flex-1">
            <div class="flex flex-wrap items-center gap-1.5">
              <h4 class="truncate text-sm font-medium text-gray-900 dark:text-white">{{ host.name }}</h4>
              <span class="rounded bg-blue-50 px-1.5 py-0.5 text-2xs font-medium text-blue-700 ring-1 ring-blue-200 dark:bg-blue-950/40 dark:text-blue-300 dark:ring-blue-900">
                {{ modeLabel(host.identity_mode) }}
              </span>
              <span v-if="!host.enabled" class="rounded px-1.5 py-0.5 text-2xs font-medium text-gray-500 ring-1 ring-gray-200 dark:ring-gray-700">
                {{ $t('hostApps.disabled') }}
              </span>
            </div>
            <p class="mt-0.5 font-mono text-2xs text-gray-500 dark:text-gray-400" dir="ltr">{{ host.public_id }}</p>
            <dl class="mt-2 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-gray-600 dark:text-gray-400 sm:grid-cols-2">
              <div><dt class="inline text-ink-muted">{{ $t('hostApps.origins') }}:</dt> <dd class="inline" dir="ltr">{{ host.allowed_origins.join(', ') || '—' }}</dd></div>
              <div v-if="host.identity_source"><dt class="inline text-ink-muted">{{ $t('hostApps.source') }}:</dt> <dd class="inline" dir="ltr">{{ host.identity_source }}{{ host.create_users ? ` · ${$t('hostApps.createsUsers')}` : '' }}</dd></div>
              <div v-if="host.key_fingerprint"><dt class="inline text-ink-muted">{{ $t('hostApps.key') }}:</dt> <dd class="inline font-mono" dir="ltr">{{ host.key_fingerprint.slice(0, 16) }}</dd></div>
              <div><dt class="inline text-ink-muted">{{ $t('hostApps.usage') }}:</dt> <dd class="inline">{{ $t('hostApps.usageValue', { sessions: host.active_sessions, artifacts: host.artifacts }) }}</dd></div>
            </dl>
          </div>
          <UToggle :model-value="host.enabled" :loading="busy === host.id" data-testid="host-app-enabled"
                   @update:model-value="setEnabled(host, $event)" />
          <UDropdown :items="actions(host)" :popper="{ placement: 'bottom-end' }">
            <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-ellipsis-horizontal" :aria-label="$t('hostApps.actions')" />
          </UDropdown>
        </div>
        <div v-if="rotatedKey[host.id]" class="border-t border-gray-100 px-4 py-3 dark:border-gray-800">
          <p class="mb-2 text-xs text-amber-700 dark:text-amber-300">{{ $t('hostApps.keyOnce') }}</p>
          <textarea readonly :value="rotatedKey[host.id]" rows="5"
                    class="w-full rounded-lg border border-gray-200 bg-gray-50 p-2 font-mono text-2xs dark:border-gray-800 dark:bg-gray-950" />
          <div class="mt-2 flex justify-end"><UButton size="xs" @click="delete rotatedKey[host.id]">{{ $t('hostApps.keySaved') }}</UButton></div>
        </div>
        <details v-if="host.identity_mode === 'assertion'" class="border-t border-gray-100 px-4 py-2.5 text-xs dark:border-gray-800">
          <summary class="cursor-pointer text-gray-600 dark:text-gray-400">{{ $t('hostApps.backendExample') }}</summary>
          <p class="mt-2 text-gray-500 dark:text-gray-400">{{ $t('hostApps.backendExampleHelp') }}</p>
          <pre class="mt-2 overflow-x-auto rounded-lg bg-gray-950 p-3 text-2xs leading-5 text-gray-100" dir="ltr">{{ assertionExample(host) }}</pre>
        </details>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import HostApplicationForm from '~/components/embed/HostApplicationForm.vue'

const emit = defineEmits<{ (e: 'updated'): void }>()
const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const hosts = ref<any[]>([])
const loading = ref(true)
const editing = ref<any>(null)
const busy = ref<string | null>(null)
const rotatedKey = reactive<Record<string, string>>({})

const modeLabel = (mode: string) => ({
  assertion: t('hostApps.modeAssertion'), idp_token: t('hostApps.modeIdp'), sso: t('hostApps.modeSso'),
} as Record<string, string>)[mode] || mode

async function load() {
  const { data } = await useMyFetch('/api/host_applications')
  hosts.value = (data.value as any[]) || []
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
  items.push({
    label: t('hostApps.delete'), icon: 'i-heroicons-trash',
    click: () => confirm(t('hostApps.deleteConfirm', { name: host.name })) && call(host, '', { method: 'DELETE' }),
  })
  return [items]
}

function assertionExample(host: any) {
  return `// Your backend, after checking its own session (Node, jsonwebtoken):
const assertion = jwt.sign(
  { sub: currentUser.${(host.identity_source || '').startsWith('member_attribute:') ? host.identity_source.split(':')[1] : 'id'} },
  process.env.BOW_HOST_PRIVATE_KEY,
  { algorithm: 'RS256', issuer: '${host.issuer || ''}', audience: '${host.public_id}',
    expiresIn: 60, jwtid: crypto.randomUUID() },
)
// Return { assertion } to identity.getAssertion() in the page.`
}

onMounted(load)
</script>
