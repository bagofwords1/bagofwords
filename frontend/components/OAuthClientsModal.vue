<template>
  <div class="p-4">
    <div class="flex items-center gap-2 mb-2">
      <UIcon name="heroicons-key" class="w-5 h-5 text-gray-700 dark:text-gray-300" />
      <h1 class="text-lg font-semibold">{{ $t('settings.integrations.channels.oauth.title') }}</h1>
    </div>
    <p class="text-sm text-gray-500 dark:text-gray-400">
      {{ $t('settings.integrations.channels.oauth.subtitle') }}
    </p>
    <hr class="my-4" />

    <!-- Loading -->
    <div v-if="loading" class="py-8 flex items-center justify-center">
      <Spinner class="w-6 h-6 text-gray-400" />
    </div>

    <div v-else>
      <!-- Clients list / empty state -->
      <div v-if="clients.length === 0" class="bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 border-dashed px-4 py-6 text-center mb-4">
        <p class="text-sm text-gray-500 dark:text-gray-400 mb-3">{{ $t('settings.integrations.channels.oauth.noneYet') }}</p>
      </div>

      <div v-else class="border border-gray-200 dark:border-gray-700 rounded-lg divide-y divide-gray-200 dark:divide-gray-700 mb-4">
        <div
          v-for="client in clients"
          :key="client.id"
          class="px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
        >
          <div class="flex items-center justify-between">
            <div class="min-w-0 flex-1">
              <div class="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{{ client.name }}</div>
              <div class="flex items-center gap-2 mt-0.5">
                <code class="font-mono text-[11px] text-gray-600 dark:text-gray-400 truncate">{{ client.client_id }}</code>
                <span class="text-[10px] text-gray-400">{{ formatDate(client.created_at) }}</span>
              </div>
            </div>
            <div class="flex items-center gap-1 flex-shrink-0 ms-3">
              <UButton
                size="xs"
                color="gray"
                variant="ghost"
                @click="copy(client.client_id)"
                :title="$t('settings.integrations.channels.oauth.copyClientId')"
              >
                <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
              </UButton>
              <UButton
                size="xs"
                color="gray"
                variant="ghost"
                @click="startEdit(client)"
                :title="$t('settings.integrations.channels.oauth.editRedirectUris')"
              >
                <UIcon name="heroicons-pencil-square" class="w-4 h-4" />
              </UButton>
              <UButton
                size="xs"
                color="gray"
                variant="ghost"
                @click="rotate(client)"
                :loading="rotatingId === client.id"
                :title="$t('settings.integrations.channels.oauth.rotateSecret')"
              >
                <UIcon name="heroicons-arrow-path" class="w-4 h-4" />
              </UButton>
              <UButton
                size="xs"
                color="red"
                variant="ghost"
                @click="remove(client)"
                :title="$t('settings.integrations.channels.oauth.delete')"
              >
                <UIcon name="heroicons-trash" class="w-4 h-4" />
              </UButton>
            </div>
          </div>

          <!-- Show freshly generated secret inline -->
          <div v-if="freshSecretByClientId[client.client_id]" class="mt-2 bg-amber-50 dark:bg-amber-950 border border-amber-200 rounded p-2">
            <div class="text-[10px] text-amber-700 uppercase tracking-wide mb-1">{{ $t('settings.integrations.channels.oauth.clientSecretOnce') }}</div>
            <div class="flex items-center justify-between gap-2">
              <code class="font-mono text-xs text-amber-900 break-all">{{ freshSecretByClientId[client.client_id] }}</code>
              <UButton
                size="xs"
                color="gray"
                variant="ghost"
                @click="copy(freshSecretByClientId[client.client_id])"
              >
                <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
              </UButton>
            </div>
          </div>

          <!-- Inline edit form for redirect URIs -->
          <div v-if="editingId === client.id" class="mt-2 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded p-2">
            <label class="block text-[11px] text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-1">{{ $t('settings.integrations.channels.oauth.redirectUris') }}</label>
            <textarea
              v-model="editRedirectUris"
              rows="3"
              class="w-full border rounded px-2 py-1 text-sm font-mono"
              :placeholder="$t('settings.integrations.channels.oauth.redirectUriPlaceholder')"
            />
            <p class="text-[11px] text-gray-400 mt-1">{{ $t('settings.integrations.channels.oauth.oneUriPerLine') }}</p>
            <div class="flex justify-end gap-2 mt-2">
              <UButton size="xs" color="gray" variant="ghost" @click="cancelEdit">{{ $t('settings.integrations.channels.common.cancel') }}</UButton>
              <UButton
                size="xs"
                color="blue"
                :loading="savingId === client.id"
                :disabled="!editRedirectUrisList.length"
                @click="saveEdit(client)"
              >{{ $t('settings.integrations.channels.common.save') }}</UButton>
            </div>
          </div>

          <!-- Registered redirect URIs -->
          <div v-else-if="client.redirect_uris?.length" class="mt-1">
            <details class="text-[11px] text-gray-500 dark:text-gray-400">
              <summary class="cursor-pointer hover:text-gray-700 dark:hover:text-gray-300">
                {{ $t('settings.integrations.channels.oauth.redirectUriCount', { count: client.redirect_uris.length }, client.redirect_uris.length) }}
              </summary>
              <ul class="mt-1 space-y-0.5">
                <li
                  v-for="uri in client.redirect_uris"
                  :key="uri"
                  class="font-mono break-all text-gray-600 dark:text-gray-400"
                >{{ uri }}</li>
              </ul>
            </details>
          </div>
        </div>
      </div>

      <!-- Add new client form -->
      <form @submit.prevent="submit" class="space-y-3">
        <div>
          <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.oauth.addClient') }}</label>
          <div class="flex gap-2">
            <input
              v-model="newName"
              type="text"
              class="flex-1 border rounded px-2 py-1 text-sm"
              :placeholder="$t('settings.integrations.channels.oauth.addClientPlaceholder')"
              required
            />
            <UButton
              type="submit"
              size="sm"
              color="blue"
              :loading="creating"
              :disabled="!newName.trim()"
            >
              <UIcon name="heroicons-plus" class="w-4 h-4 me-1" />
              {{ $t('settings.integrations.channels.oauth.add') }}
            </UButton>
          </div>
        </div>

        <details class="text-sm">
          <summary class="cursor-pointer text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
            {{ $t('settings.integrations.channels.oauth.customRedirectUris') }}
          </summary>
          <div class="mt-2">
            <textarea
              v-model="newRedirectUris"
              rows="3"
              class="w-full border rounded px-2 py-1 text-sm font-mono"
              :placeholder="$t('settings.integrations.channels.oauth.redirectUriPlaceholder')"
            />
            <p class="text-[11px] text-gray-400 mt-1">
              {{ $t('settings.integrations.channels.oauth.customRedirectUrisHint') }}
            </p>
          </div>
        </details>
      </form>

      <!-- Claude Web setup instructions -->
      <details class="mt-5 text-sm">
        <summary class="cursor-pointer text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300">
          {{ $t('settings.integrations.channels.oauth.setupInstructions') }}
        </summary>
        <div class="mt-3 bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-3 space-y-2">
          <div class="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <div class="w-1.5 h-1.5 rounded-full bg-green-500"></div>
            <code class="font-mono text-gray-700 dark:text-gray-300">{{ mcpServerUrl }}</code>
          </div>
          <ol class="text-sm text-gray-600 dark:text-gray-400 space-y-1.5 list-decimal list-inside">
            <li>{{ $t('settings.integrations.channels.oauth.setupStep1') }}</li>
            <li>{{ $t('settings.integrations.channels.oauth.setupStep2') }}</li>
            <li>{{ $t('settings.integrations.channels.oauth.setupStep3') }}</li>
            <li>{{ $t('settings.integrations.channels.oauth.setupStep4') }}</li>
            <li>{{ $t('settings.integrations.channels.oauth.setupStep5') }}</li>
          </ol>
        </div>
      </details>
    </div>

    <button class="absolute top-2 end-2 text-gray-400 hover:text-gray-600" :title="$t('settings.integrations.channels.common.close')" @click="$emit('close')">✕</button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import Spinner from '~/components/Spinner.vue'

const emit = defineEmits<{
  close: []
  updated: []
}>()

interface OAuthClient {
  id: string
  client_id: string
  client_secret?: string
  name: string
  redirect_uris: string[]
  created_at: string
}

const toast = useToast()
const { t } = useI18n()

const loading = ref(true)
const clients = ref<OAuthClient[]>([])
const creating = ref(false)
const rotatingId = ref<string | null>(null)
const editingId = ref<string | null>(null)
const editRedirectUris = ref('')
const savingId = ref<string | null>(null)
const newName = ref('')
const newRedirectUris = ref('')
const baseUrl = ref('')

const editRedirectUrisList = computed(() =>
  editRedirectUris.value.split('\n').map(s => s.trim()).filter(Boolean)
)

// Map of client_id → freshly revealed secret (shown until modal closes or user dismisses)
const freshSecretByClientId = ref<Record<string, string>>({})

const mcpServerUrl = computed(() => {
  const base = baseUrl.value || window.location.origin
  return `${base}/api/mcp`
})

const _df = useFormatDate()
function formatDate(dateStr: string) {
  return _df.format(dateStr, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  })
}

async function copy(text: string | undefined) {
  if (!text) return
  await navigator.clipboard.writeText(text)
  toast.add({ title: t('settings.integrations.channels.oauth.copied'), icon: 'i-heroicons-check-circle', color: 'green' })
}

async function loadBaseUrl() {
  try {
    const res = await useMyFetch('/settings')
    if (res.data.value) {
      baseUrl.value = (res.data.value as any).base_url || ''
    }
  } catch (e) {
    // fall back to window.location.origin
  }
}

async function loadClients() {
  try {
    const res = await useMyFetch('/api/oauth/clients')
    clients.value = (res.data.value as OAuthClient[]) || []
  } catch (e) {
    clients.value = []
  }
}

async function submit() {
  const name = newName.value.trim()
  if (!name) return
  const redirectUris = newRedirectUris.value
    .split('\n')
    .map(s => s.trim())
    .filter(Boolean)
  creating.value = true
  try {
    const body: { name: string; redirect_uris?: string[] } = { name }
    if (redirectUris.length) body.redirect_uris = redirectUris
    const res = await useMyFetch('/api/oauth/clients', {
      method: 'POST',
      body
    })
    if (res.data.value) {
      const created = res.data.value as OAuthClient
      clients.value = [created, ...clients.value]
      if (created.client_secret) {
        freshSecretByClientId.value[created.client_id] = created.client_secret
      }
      newName.value = ''
      newRedirectUris.value = ''
      toast.add({ title: t('settings.integrations.channels.oauth.createdToast'), icon: 'i-heroicons-check-circle', color: 'green' })
      emit('updated')
    }
  } catch (e) {
    toast.add({ title: t('settings.integrations.channels.oauth.failedCreate'), icon: 'i-heroicons-x-circle', color: 'red' })
  } finally {
    creating.value = false
  }
}

function startEdit(client: OAuthClient) {
  editingId.value = client.id
  editRedirectUris.value = (client.redirect_uris || []).join('\n')
}

function cancelEdit() {
  editingId.value = null
  editRedirectUris.value = ''
}

async function saveEdit(client: OAuthClient) {
  const uris = editRedirectUrisList.value
  if (!uris.length) return
  savingId.value = client.id
  try {
    const res = await useMyFetch(`/api/oauth/clients/${client.id}`, {
      method: 'PATCH',
      body: { redirect_uris: uris }
    })
    if (res.data.value) {
      const updated = res.data.value as OAuthClient
      const idx = clients.value.findIndex(c => c.id === client.id)
      if (idx !== -1) clients.value[idx].redirect_uris = updated.redirect_uris
      cancelEdit()
      toast.add({ title: t('settings.integrations.channels.oauth.redirectUpdatedToast'), icon: 'i-heroicons-check-circle', color: 'green' })
      emit('updated')
    }
  } catch (e) {
    toast.add({ title: t('settings.integrations.channels.oauth.failedUpdateRedirect'), icon: 'i-heroicons-x-circle', color: 'red' })
  } finally {
    savingId.value = null
  }
}

async function rotate(client: OAuthClient) {
  rotatingId.value = client.id
  try {
    const res = await useMyFetch(`/api/oauth/clients/${client.id}/rotate`, {
      method: 'POST'
    })
    if (res.data.value) {
      const updated = res.data.value as OAuthClient
      const idx = clients.value.findIndex(c => c.id === client.id)
      if (idx !== -1) {
        clients.value[idx].client_id = updated.client_id
      }
      if (updated.client_secret) {
        freshSecretByClientId.value[updated.client_id] = updated.client_secret
      }
      toast.add({ title: t('settings.integrations.channels.oauth.rotatedToast'), icon: 'i-heroicons-check-circle', color: 'green' })
      emit('updated')
    }
  } catch (e) {
    toast.add({ title: t('settings.integrations.channels.oauth.failedRotate'), icon: 'i-heroicons-x-circle', color: 'red' })
  } finally {
    rotatingId.value = null
  }
}

async function remove(client: OAuthClient) {
  if (!confirm(t('settings.integrations.channels.oauth.confirmDelete', { name: client.name }))) return
  try {
    await useMyFetch(`/api/oauth/clients/${client.id}`, { method: 'DELETE' })
    clients.value = clients.value.filter(c => c.id !== client.id)
    delete freshSecretByClientId.value[client.client_id]
    toast.add({ title: t('settings.integrations.channels.oauth.deletedToast'), icon: 'i-heroicons-check-circle', color: 'green' })
    emit('updated')
  } catch (e) {
    toast.add({ title: t('settings.integrations.channels.oauth.failedDelete'), icon: 'i-heroicons-x-circle', color: 'red' })
  }
}

onMounted(async () => {
  loading.value = true
  await Promise.all([loadBaseUrl(), loadClients()])
  loading.value = false
})
</script>
