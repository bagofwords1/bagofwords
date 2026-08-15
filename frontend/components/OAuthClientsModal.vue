<template>
  <section class="pb-10">
    <header class="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
      <div>
        <h3 class="text-base font-semibold text-gray-900 dark:text-white">
          {{ $t('settings.integrations.channels.oauth.title') }}
        </h3>
        <p class="mt-1 max-w-2xl text-sm leading-6 text-gray-500 dark:text-gray-400">
          {{ $t('settings.integrations.channels.oauth.subtitle') }}
        </p>
      </div>
      <div class="flex shrink-0 items-center gap-2">
        <UButton color="gray" variant="ghost" size="sm" @click="showEndpointsModal = true">
          {{ $t('settings.integrations.channels.oauth.endpointReference') }}
        </UButton>
        <UButton color="blue" size="sm" icon="i-heroicons-plus" @click="openCreate">
          {{ $t('settings.integrations.channels.oauth.registerApp') }}
        </UButton>
      </div>
    </header>

    <div v-if="loading" class="flex items-center justify-center py-20">
      <Spinner class="h-6 w-6 text-gray-400" />
    </div>

    <template v-else>
      <div v-if="clients.length > 5" class="mt-6 max-w-sm">
        <div class="relative">
          <UIcon name="i-heroicons-magnifying-glass" class="pointer-events-none absolute start-3 top-2.5 h-4 w-4 text-gray-400" />
          <input
            v-model="search"
            type="search"
            class="w-full rounded-lg border border-gray-200 bg-white py-2 pe-3 ps-9 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100 dark:border-gray-800 dark:bg-gray-900 dark:focus:ring-blue-950"
            :placeholder="$t('settings.integrations.channels.oauth.searchPlaceholder')"
          />
        </div>
      </div>

      <div v-if="clients.length === 0" class="mt-6 rounded-xl border border-dashed border-gray-300 px-6 py-12 text-center dark:border-gray-700">
        <UIcon name="i-heroicons-key" class="mx-auto h-6 w-6 text-gray-400" />
        <h4 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">
          {{ $t('settings.integrations.channels.oauth.noneYet') }}
        </h4>
        <p class="mx-auto mt-1 max-w-sm text-xs leading-5 text-gray-500 dark:text-gray-400">
          {{ $t('settings.integrations.channels.oauth.emptyHelp') }}
        </p>
        <UButton class="mt-4" color="blue" variant="soft" size="sm" icon="i-heroicons-plus" @click="openCreate">
          {{ $t('settings.integrations.channels.oauth.registerApp') }}
        </UButton>
      </div>

      <div v-else-if="filteredClients.length === 0" class="mt-6 rounded-xl border border-gray-200 py-10 text-center text-sm text-gray-500 dark:border-gray-800 dark:text-gray-400">
        {{ $t('settings.integrations.channels.oauth.noMatches') }}
      </div>

      <div v-else class="mt-6 overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
        <article
          v-for="client in filteredClients"
          :key="client.id"
          class="flex items-center gap-3 border-b border-gray-100 px-4 py-3 last:border-b-0 dark:border-gray-800"
        >
          <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-xs font-semibold text-gray-600 dark:bg-gray-800 dark:text-gray-300">
            {{ initials(client.name) }}
          </div>

          <div class="min-w-0 flex-1">
            <div class="flex min-w-0 flex-wrap items-center gap-1.5">
              <h4 class="truncate text-sm font-medium text-gray-900 dark:text-white">{{ client.name }}</h4>
              <span
                v-for="scopeName in client.scopes"
                :key="scopeName"
                class="rounded px-1.5 py-0.5 text-[10px] font-medium ring-1"
                :class="scopeName === 'app'
                  ? 'bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-950/40 dark:text-blue-300 dark:ring-blue-900'
                  : 'bg-violet-50 text-violet-700 ring-violet-200 dark:bg-violet-950/40 dark:text-violet-300 dark:ring-violet-900'"
              >
                {{ scopeLabel(scopeName) }}
              </span>
              <span v-if="client.trusted" class="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium text-amber-700 ring-1 ring-amber-200 dark:text-amber-300 dark:ring-amber-900">
                <UIcon name="i-heroicons-bolt" class="h-3 w-3" />
                {{ $t('settings.integrations.channels.oauth.trusted') }}
              </span>
            </div>

            <div class="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-gray-500 dark:text-gray-400">
              <button type="button" class="inline-flex min-w-0 items-center gap-1 hover:text-gray-800 dark:hover:text-gray-200" @click="copy(client.client_id)">
                <code class="max-w-48 truncate font-mono">{{ client.client_id }}</code>
                <UIcon name="i-heroicons-clipboard-document" class="h-3 w-3 shrink-0" />
              </button>
              <span aria-hidden="true" class="text-gray-300 dark:text-gray-700">·</span>
              <span>{{ $t('settings.integrations.channels.oauth.activeTokenCount', { count: client.active_token_count || 0 }, client.active_token_count || 0) }}</span>
              <span aria-hidden="true" class="hidden text-gray-300 sm:inline dark:text-gray-700">·</span>
              <span class="hidden sm:inline">{{ lastUsedLabel(client) }}</span>
            </div>
          </div>

          <UDropdown :items="clientActionItems[client.id]" :popper="{ placement: 'bottom-end', strategy: 'fixed' }">
            <UButton
              color="gray"
              variant="ghost"
              size="xs"
              :icon="rotatingId === client.id ? 'i-heroicons-arrow-path' : 'i-heroicons-ellipsis-horizontal'"
              :class="rotatingId === client.id ? '[&_svg]:animate-spin' : ''"
              :disabled="rotatingId === client.id"
              :aria-label="$t('settings.integrations.channels.oauth.actions')"
            />
            <template #item="{ item }">
              <span class="truncate" :class="item.danger ? 'text-red-600 dark:text-red-400' : ''">{{ item.label }}</span>
              <UIcon v-if="item.icon" :name="item.icon" class="ms-auto h-4 w-4 shrink-0" :class="item.danger ? 'text-red-500' : 'text-gray-400'" />
            </template>
          </UDropdown>
        </article>
      </div>
    </template>

    <UModal v-model="showAppModal" :prevent-close="submitting" :ui="{ width: 'sm:max-w-xl' }">
      <UCard :ui="{ body: { padding: 'px-5 py-5' }, header: { padding: 'px-5 py-4' }, footer: { padding: 'px-5 py-3' } }">
        <template #header>
          <div class="flex items-start justify-between gap-4">
            <div>
              <h3 class="text-base font-semibold text-gray-900 dark:text-white">
                {{ formMode === 'create' ? $t('settings.integrations.channels.oauth.createTitle') : $t('settings.integrations.channels.oauth.editApp') }}
              </h3>
              <p v-if="formMode === 'create'" class="mt-1 text-xs leading-5 text-gray-500 dark:text-gray-400">
                {{ $t('settings.integrations.channels.oauth.createHelp') }}
              </p>
            </div>
            <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" :aria-label="$t('settings.integrations.channels.common.close')" :disabled="submitting" @click="showAppModal = false" />
          </div>
        </template>

        <form id="oauth-app-form" class="space-y-5" @submit.prevent="submitApp">
          <div>
            <label class="mb-1.5 block text-sm font-medium text-gray-800 dark:text-gray-200">
              {{ $t('settings.integrations.channels.oauth.appName') }}
            </label>
            <input
              v-model="formName"
              type="text"
              required
              autofocus
              class="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm shadow-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100 dark:border-gray-700 dark:bg-gray-900 dark:focus:ring-blue-950"
              :placeholder="$t('settings.integrations.channels.oauth.appNamePlaceholder')"
            />
          </div>

          <fieldset>
            <legend class="text-sm font-medium text-gray-800 dark:text-gray-200">
              {{ $t('settings.integrations.channels.oauth.scopes') }}
            </legend>
            <div class="mt-2 grid gap-2 sm:grid-cols-2">
              <label
                v-for="item in scopeOptions"
                :key="item.value"
                class="flex cursor-pointer gap-3 rounded-lg border p-3 transition-colors"
                :class="formScopes.includes(item.value)
                  ? 'border-blue-400 bg-blue-50/50 ring-1 ring-blue-100 dark:border-blue-700 dark:bg-blue-950/20 dark:ring-blue-950'
                  : 'border-gray-200 dark:border-gray-700'"
              >
                <input v-model="formScopes" type="checkbox" :value="item.value" class="mt-0.5 h-4 w-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
                <span>
                  <span class="block text-sm font-medium text-gray-800 dark:text-gray-200">{{ item.label }}</span>
                  <span class="mt-0.5 block text-xs leading-4 text-gray-500 dark:text-gray-400">{{ item.description }}</span>
                </span>
              </label>
            </div>
            <p v-if="!formScopes.length" class="mt-1.5 text-xs text-red-600">
              {{ $t('settings.integrations.channels.oauth.scopesRequired') }}
            </p>
            <p v-else-if="formMode === 'edit' && scopesChanged" class="mt-1.5 text-xs leading-5 text-amber-700 dark:text-amber-300">
              {{ $t('settings.integrations.channels.oauth.scopeChangeRevokes') }}
            </p>
          </fieldset>

          <div>
            <label class="mb-1.5 block text-sm font-medium text-gray-800 dark:text-gray-200">
              {{ $t('settings.integrations.channels.oauth.redirectUris') }}
            </label>
            <textarea
              v-model="formRedirectUris"
              rows="3"
              required
              class="w-full rounded-lg border border-gray-300 bg-white px-3 py-2 font-mono text-xs shadow-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100 dark:border-gray-700 dark:bg-gray-900 dark:focus:ring-blue-950"
              :placeholder="$t('settings.integrations.channels.oauth.redirectUriPlaceholder')"
            />
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              {{ $t('settings.integrations.channels.oauth.oneUriPerLine') }}
            </p>
          </div>

          <label class="flex items-start justify-between gap-4 rounded-lg border border-gray-200 p-3 dark:border-gray-700">
            <span>
              <span class="flex items-center gap-2 text-sm font-medium text-gray-800 dark:text-gray-200">
                {{ $t('settings.integrations.channels.oauth.trusted') }}
                <span class="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
                  {{ $t('settings.integrations.channels.oauth.internalOnly') }}
                </span>
              </span>
              <span class="mt-1 block max-w-md text-xs leading-5 text-gray-500 dark:text-gray-400">
                {{ $t('settings.integrations.channels.oauth.trustedHelp') }}
              </span>
            </span>
            <UToggle v-model="formTrusted" class="mt-0.5 shrink-0" />
          </label>
        </form>

        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton color="gray" variant="ghost" size="sm" :disabled="submitting" @click="showAppModal = false">
              {{ $t('settings.integrations.channels.common.cancel') }}
            </UButton>
            <UButton form="oauth-app-form" type="submit" color="blue" size="sm" :loading="submitting" :disabled="!canSubmit">
              {{ formMode === 'create' ? $t('settings.integrations.channels.oauth.registerApp') : $t('settings.integrations.channels.common.save') }}
            </UButton>
          </div>
        </template>
      </UCard>
    </UModal>

    <UModal v-model="showSecretModal" :ui="{ width: 'sm:max-w-lg' }">
      <UCard :ui="{ body: { padding: 'px-5 py-5' }, header: { padding: 'px-5 py-4' }, footer: { padding: 'px-5 py-3' } }">
        <template #header>
          <div class="flex items-center justify-between gap-4">
            <h3 class="text-base font-semibold text-gray-900 dark:text-white">
              {{ $t('settings.integrations.channels.oauth.clientSecretOnce') }}
            </h3>
            <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" :aria-label="$t('settings.integrations.channels.common.close')" @click="showSecretModal = false" />
          </div>
        </template>

        <div class="space-y-4">
          <div>
            <div class="mb-1 text-xs font-medium text-gray-500 dark:text-gray-400">
              {{ $t('settings.integrations.channels.oauth.clientIdLabel') }}
            </div>
            <div class="flex items-center gap-2 rounded-lg bg-gray-50 px-3 py-2 dark:bg-gray-950/60">
              <code class="min-w-0 flex-1 break-all font-mono text-xs text-gray-700 dark:text-gray-300">{{ revealedClientId }}</code>
              <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-clipboard-document" :aria-label="$t('settings.integrations.channels.oauth.copyClientId')" @click="copy(revealedClientId)" />
            </div>
          </div>
          <div>
            <div class="mb-1 text-xs font-medium text-gray-500 dark:text-gray-400">
              {{ $t('settings.integrations.channels.oauth.clientSecretOnce') }}
            </div>
            <div class="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 dark:border-amber-900 dark:bg-amber-950/40">
              <code class="min-w-0 flex-1 break-all font-mono text-xs text-amber-950 dark:text-amber-100">{{ revealedSecret }}</code>
              <UButton color="amber" variant="ghost" size="xs" icon="i-heroicons-clipboard-document" :aria-label="$t('settings.integrations.channels.oauth.copy')" @click="copy(revealedSecret)" />
            </div>
            <p class="mt-2 text-xs text-amber-700 dark:text-amber-300">
              {{ $t('settings.integrations.channels.oauth.secretHelp') }}
            </p>
          </div>
        </div>

        <template #footer>
          <div class="flex justify-end">
            <UButton color="blue" size="sm" @click="showSecretModal = false">
              {{ $t('settings.integrations.channels.common.close') }}
            </UButton>
          </div>
        </template>
      </UCard>
    </UModal>

    <UModal v-model="showEndpointsModal" :ui="{ width: 'sm:max-w-xl' }">
      <UCard :ui="{ body: { padding: 'px-5 py-5' }, header: { padding: 'px-5 py-4' } }">
        <template #header>
          <div class="flex items-center justify-between gap-4">
            <h3 class="text-base font-semibold text-gray-900 dark:text-white">
              {{ $t('settings.integrations.channels.oauth.endpointReference') }}
            </h3>
            <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark" :aria-label="$t('settings.integrations.channels.common.close')" @click="showEndpointsModal = false" />
          </div>
        </template>
        <dl class="space-y-3">
          <div v-for="endpoint in endpoints" :key="endpoint.label">
            <dt class="mb-1 text-xs font-medium text-gray-500 dark:text-gray-400">{{ endpoint.label }}</dt>
            <dd class="flex items-center gap-2 rounded-lg bg-gray-50 px-3 py-2 dark:bg-gray-950/60">
              <code class="min-w-0 flex-1 truncate font-mono text-xs text-gray-700 dark:text-gray-300">{{ endpoint.value }}</code>
              <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-clipboard-document" :aria-label="$t('settings.integrations.channels.oauth.copy')" @click="copy(endpoint.value)" />
            </dd>
          </div>
        </dl>
      </UCard>
    </UModal>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'

const emit = defineEmits<{ updated: [] }>()

interface OAuthClient {
  id: string
  client_id: string
  client_secret?: string
  name: string
  redirect_uris: string[]
  scopes: string[]
  trusted: boolean
  active_token_count: number
  last_used_at: string | null
  last_issued_at: string | null
  created_at: string
}

const toast = useToast()
const { t } = useI18n()
const loading = ref(true)
const clients = ref<OAuthClient[]>([])
const search = ref('')
const showAppModal = ref(false)
const showSecretModal = ref(false)
const showEndpointsModal = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const editingClient = ref<OAuthClient | null>(null)
const submitting = ref(false)
const rotatingId = ref<string | null>(null)
const revealedClientId = ref('')
const revealedSecret = ref('')
const baseUrl = ref('')

const formName = ref('')
const formRedirectUris = ref('')
const formScopes = ref<string[]>(['app'])
const formTrusted = ref(false)

const scopeOptions = computed(() => [
  { value: 'app', label: t('settings.integrations.channels.oauth.appScope'), description: t('settings.integrations.channels.oauth.appScopeDesc') },
  { value: 'mcp', label: t('settings.integrations.channels.oauth.mcpScope'), description: t('settings.integrations.channels.oauth.mcpScopeDesc') },
])

const parseUris = (value: string) => value.split('\n').map(uri => uri.trim()).filter(Boolean)
const canSubmit = computed(() => Boolean(formName.value.trim() && parseUris(formRedirectUris.value).length && formScopes.value.length))
const scopesChanged = computed(() => {
  if (!editingClient.value) return false
  return [...formScopes.value].sort().join(' ') !== [...editingClient.value.scopes].sort().join(' ')
})

const filteredClients = computed(() => {
  const term = search.value.trim().toLowerCase()
  if (!term) return clients.value
  return clients.value.filter(client => [client.name, client.client_id, ...client.scopes, ...client.redirect_uris].some(value => value.toLowerCase().includes(term)))
})

const endpointRoot = computed(() => (baseUrl.value || (import.meta.client ? window.location.origin : '')).replace(/\/$/, ''))
const endpoints = computed(() => [
  { label: t('settings.integrations.channels.oauth.authorizationEndpoint'), value: `${endpointRoot.value}/api/oauth/authorize` },
  { label: t('settings.integrations.channels.oauth.tokenEndpoint'), value: `${endpointRoot.value}/api/oauth/token` },
  { label: t('settings.integrations.channels.oauth.discoveryEndpoint'), value: `${endpointRoot.value}/.well-known/oauth-authorization-server` },
])

const clientActionItems = computed<Record<string, any[][]>>(() => {
  const items: Record<string, any[][]> = {}
  for (const client of clients.value) {
    items[client.id] = [
      [
        { label: t('settings.integrations.channels.oauth.editApp'), icon: 'i-heroicons-pencil-square', click: () => openEdit(client) },
        { label: t('settings.integrations.channels.oauth.rotateSecret'), icon: 'i-heroicons-arrow-path', click: () => rotate(client) },
      ],
      [
        { label: t('settings.integrations.channels.oauth.delete'), icon: 'i-heroicons-trash', danger: true, click: () => remove(client) },
      ],
    ]
  }
  return items
})

const dateFormatter = useFormatDate()
function formatDate(value: string) {
  return dateFormatter.format(value, { month: 'short', day: 'numeric', year: 'numeric' })
}

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]?.toUpperCase()).join('') || 'OA'
}

function scopeLabel(scope: string) {
  return scope === 'app' ? t('settings.integrations.channels.oauth.appScope') : t('settings.integrations.channels.oauth.mcpScope')
}

function lastUsedLabel(client: OAuthClient) {
  if (client.last_used_at) return t('settings.integrations.channels.oauth.lastUsed', { date: formatDate(client.last_used_at) })
  if (client.last_issued_at) return t('settings.integrations.channels.oauth.issued', { date: formatDate(client.last_issued_at) })
  return t('settings.integrations.channels.oauth.neverUsed')
}

async function copy(value?: string) {
  if (!value) return
  await navigator.clipboard.writeText(value)
  toast.add({ title: t('settings.integrations.channels.oauth.copied'), icon: 'i-heroicons-check-circle', color: 'green' })
}

function openCreate() {
  formMode.value = 'create'
  editingClient.value = null
  formName.value = ''
  formRedirectUris.value = import.meta.client ? `${window.location.origin}/oauth/callback` : ''
  formScopes.value = ['app']
  formTrusted.value = false
  showAppModal.value = true
}

function openEdit(client: OAuthClient) {
  formMode.value = 'edit'
  editingClient.value = client
  formName.value = client.name
  formRedirectUris.value = client.redirect_uris.join('\n')
  formScopes.value = [...client.scopes]
  formTrusted.value = client.trusted
  showAppModal.value = true
}

function revealCredentials(clientId: string, secret?: string) {
  if (!secret) return
  revealedClientId.value = clientId
  revealedSecret.value = secret
  showSecretModal.value = true
}

async function loadBaseUrl() {
  const response = await useMyFetch('/settings')
  if (response.data.value) baseUrl.value = (response.data.value as any).base_url || ''
}

async function loadClients() {
  const response = await useMyFetch('/api/oauth/clients')
  clients.value = (response.data.value as OAuthClient[]) || []
}

async function submitApp() {
  if (!canSubmit.value) return
  submitting.value = true
  try {
    if (formMode.value === 'create') {
      const response = await useMyFetch('/api/oauth/clients', {
        method: 'POST',
        body: {
          name: formName.value.trim(),
          redirect_uris: parseUris(formRedirectUris.value),
          scopes: formScopes.value.join(' '),
          trusted: formTrusted.value,
        },
      })
      if (response.error.value) throw response.error.value
      const created = response.data.value as OAuthClient
      clients.value = [created, ...clients.value]
      showAppModal.value = false
      revealCredentials(created.client_id, created.client_secret)
      toast.add({ title: t('settings.integrations.channels.oauth.createdToast'), icon: 'i-heroicons-check-circle', color: 'green' })
    } else if (editingClient.value) {
      const response = await useMyFetch(`/api/oauth/clients/${editingClient.value.id}`, {
        method: 'PATCH',
        body: {
          name: formName.value.trim(),
          redirect_uris: parseUris(formRedirectUris.value),
          scopes: formScopes.value.join(' '),
          trusted: formTrusted.value,
        },
      })
      if (response.error.value) throw response.error.value
      const updated = response.data.value as OAuthClient
      const index = clients.value.findIndex(item => item.id === editingClient.value?.id)
      if (index !== -1) clients.value[index] = { ...clients.value[index], ...updated }
      showAppModal.value = false
      toast.add({ title: t('settings.integrations.channels.oauth.updatedToast'), icon: 'i-heroicons-check-circle', color: 'green' })
    }
    emit('updated')
  } catch {
    toast.add({
      title: t(formMode.value === 'create' ? 'settings.integrations.channels.oauth.failedCreate' : 'settings.integrations.channels.oauth.failedUpdate'),
      icon: 'i-heroicons-x-circle',
      color: 'red',
    })
  } finally {
    submitting.value = false
  }
}

async function rotate(client: OAuthClient) {
  rotatingId.value = client.id
  try {
    const response = await useMyFetch(`/api/oauth/clients/${client.id}/rotate`, { method: 'POST' })
    if (response.error.value) throw response.error.value
    const rotated = response.data.value as OAuthClient
    revealCredentials(client.client_id, rotated.client_secret)
    toast.add({ title: t('settings.integrations.channels.oauth.rotatedToast'), icon: 'i-heroicons-check-circle', color: 'green' })
  } catch {
    toast.add({ title: t('settings.integrations.channels.oauth.failedRotate'), icon: 'i-heroicons-x-circle', color: 'red' })
  } finally {
    rotatingId.value = null
  }
}

async function remove(client: OAuthClient) {
  if (!confirm(t('settings.integrations.channels.oauth.confirmDelete', { name: client.name }))) return
  const response = await useMyFetch(`/api/oauth/clients/${client.id}`, { method: 'DELETE' })
  if (response.error.value) {
    toast.add({ title: t('settings.integrations.channels.oauth.failedDelete'), icon: 'i-heroicons-x-circle', color: 'red' })
    return
  }
  clients.value = clients.value.filter(item => item.id !== client.id)
  toast.add({ title: t('settings.integrations.channels.oauth.deletedToast'), description: t('settings.integrations.channels.oauth.deleteImpact'), icon: 'i-heroicons-check-circle', color: 'green' })
  emit('updated')
}

onMounted(async () => {
  loading.value = true
  try {
    await Promise.all([loadBaseUrl(), loadClients()])
  } finally {
    loading.value = false
  }
})
</script>
