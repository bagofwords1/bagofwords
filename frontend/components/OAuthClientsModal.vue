<template>
  <div class="p-4">
    <div class="flex items-center gap-2 mb-2">
      <UIcon name="heroicons-key" class="w-5 h-5 text-gray-700" />
      <h1 class="text-lg font-semibold">OAuth Clients</h1>
    </div>
    <p class="text-sm text-gray-500">
      Register external apps that connect to this workspace via OAuth 2.1 (e.g. Claude Web's MCP connector).
    </p>
    <hr class="my-4" />

    <!-- Loading -->
    <div v-if="loading" class="py-8 flex items-center justify-center">
      <Spinner class="w-6 h-6 text-gray-400" />
    </div>

    <div v-else>
      <!-- Clients list / empty state -->
      <div v-if="clients.length === 0" class="bg-gray-50 rounded-lg border border-gray-200 border-dashed px-4 py-6 text-center mb-4">
        <p class="text-sm text-gray-500 mb-3">No OAuth clients registered yet</p>
      </div>

      <div v-else class="border border-gray-200 rounded-lg divide-y divide-gray-200 mb-4">
        <div
          v-for="client in clients"
          :key="client.id"
          class="px-3 py-2 hover:bg-gray-50 transition-colors"
        >
          <div class="flex items-center justify-between">
            <div class="min-w-0 flex-1">
              <div class="text-sm font-medium text-gray-800 truncate">{{ client.name }}</div>
              <div class="flex items-center gap-2 mt-0.5">
                <code class="font-mono text-[11px] text-gray-600 truncate">{{ client.client_id }}</code>
                <span class="text-[10px] text-gray-400">{{ formatDate(client.created_at) }}</span>
              </div>
            </div>
            <div class="flex items-center gap-1 flex-shrink-0 ml-3">
              <UButton
                size="xs"
                color="gray"
                variant="ghost"
                @click="copy(client.client_id)"
                title="Copy Client ID"
              >
                <UIcon name="heroicons-clipboard-document" class="w-4 h-4" />
              </UButton>
              <UButton
                size="xs"
                color="gray"
                variant="ghost"
                @click="startEdit(client)"
                title="Edit redirect URIs"
              >
                <UIcon name="heroicons-pencil-square" class="w-4 h-4" />
              </UButton>
              <UButton
                size="xs"
                color="gray"
                variant="ghost"
                @click="rotate(client)"
                :loading="rotatingId === client.id"
                title="Rotate secret"
              >
                <UIcon name="heroicons-arrow-path" class="w-4 h-4" />
              </UButton>
              <UButton
                size="xs"
                color="red"
                variant="ghost"
                @click="remove(client)"
                title="Delete"
              >
                <UIcon name="heroicons-trash" class="w-4 h-4" />
              </UButton>
            </div>
          </div>

          <!-- Show freshly generated secret inline -->
          <div v-if="freshSecretByClientId[client.client_id]" class="mt-2 bg-amber-50 border border-amber-200 rounded p-2">
            <div class="text-[10px] text-amber-700 uppercase tracking-wide mb-1">Client Secret (shown once)</div>
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
          <div v-if="editingId === client.id" class="mt-2 bg-gray-50 border border-gray-200 rounded p-2">
            <label class="block text-[11px] text-gray-500 uppercase tracking-wide mb-1">Redirect URIs</label>
            <textarea
              v-model="editRedirectUris"
              rows="3"
              class="w-full border rounded px-2 py-1 text-sm font-mono"
              placeholder="https://your-app.example.com/oauth/callback"
            />
            <p class="text-[11px] text-gray-400 mt-1">One URI per line.</p>
            <div class="flex justify-end gap-2 mt-2">
              <UButton size="xs" color="gray" variant="ghost" @click="cancelEdit">Cancel</UButton>
              <UButton
                size="xs"
                color="blue"
                :loading="savingId === client.id"
                :disabled="!editRedirectUrisList.length"
                @click="saveEdit(client)"
              >Save</UButton>
            </div>
          </div>

          <!-- Registered redirect URIs -->
          <div v-else-if="client.redirect_uris?.length" class="mt-1">
            <details class="text-[11px] text-gray-500">
              <summary class="cursor-pointer hover:text-gray-700">
                {{ client.redirect_uris.length }} redirect URI{{ client.redirect_uris.length === 1 ? '' : 's' }}
              </summary>
              <ul class="mt-1 space-y-0.5">
                <li
                  v-for="uri in client.redirect_uris"
                  :key="uri"
                  class="font-mono break-all text-gray-600"
                >{{ uri }}</li>
              </ul>
            </details>
          </div>
        </div>
      </div>

      <!-- Add new client form -->
      <form @submit.prevent="submit" class="space-y-3">
        <div>
          <label class="block text-sm font-medium mb-1">Add a client</label>
          <div class="flex gap-2">
            <input
              v-model="newName"
              type="text"
              class="flex-1 border rounded px-2 py-1 text-sm"
              placeholder="e.g. Claude Web"
              required
            />
            <UButton
              type="submit"
              size="sm"
              color="blue"
              :loading="creating"
              :disabled="!newName.trim()"
            >
              <UIcon name="heroicons-plus" class="w-4 h-4 mr-1" />
              Add
            </UButton>
          </div>
        </div>

        <details class="text-sm">
          <summary class="cursor-pointer text-gray-500 hover:text-gray-700">
            Custom redirect URIs (optional)
          </summary>
          <div class="mt-2">
            <textarea
              v-model="newRedirectUris"
              rows="3"
              class="w-full border rounded px-2 py-1 text-sm font-mono"
              placeholder="https://your-app.example.com/oauth/callback"
            />
            <p class="text-[11px] text-gray-400 mt-1">
              One URI per line. Leave empty to use the defaults (Claude Web and local MCP inspector).
            </p>
          </div>
        </details>
      </form>

      <!-- Claude Web setup instructions -->
      <details class="mt-5 text-sm">
        <summary class="cursor-pointer text-gray-500 hover:text-gray-700">
          Claude Web setup instructions
        </summary>
        <div class="mt-3 bg-gray-50 border border-gray-200 rounded-lg p-3 space-y-2">
          <div class="flex items-center gap-2 text-xs text-gray-500">
            <div class="w-1.5 h-1.5 rounded-full bg-green-500"></div>
            <code class="font-mono text-gray-700">{{ mcpServerUrl }}</code>
          </div>
          <ol class="text-sm text-gray-600 space-y-1.5 list-decimal list-inside">
            <li>In Claude Web, go to <strong>Settings → Connectors → Add</strong></li>
            <li>Enter the MCP server URL above</li>
            <li>Click <strong>Advanced Settings</strong></li>
            <li>Enter the <strong>Client ID</strong> and <strong>Client Secret</strong> from a client above</li>
            <li>Click <strong>Connect</strong> — you'll be redirected to approve access</li>
          </ol>
        </div>
      </details>
    </div>

    <button class="absolute top-2 right-2 text-gray-400 hover:text-gray-600" @click="$emit('close')">✕</button>
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

function formatDate(dateStr: string) {
  return new Date(dateStr).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  })
}

async function copy(text: string | undefined) {
  if (!text) return
  await navigator.clipboard.writeText(text)
  toast.add({ title: 'Copied', icon: 'i-heroicons-check-circle', color: 'green' })
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
      toast.add({ title: 'OAuth client created', icon: 'i-heroicons-check-circle', color: 'green' })
      emit('updated')
    }
  } catch (e) {
    toast.add({ title: 'Failed to create client', icon: 'i-heroicons-x-circle', color: 'red' })
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
      toast.add({ title: 'Redirect URIs updated', icon: 'i-heroicons-check-circle', color: 'green' })
      emit('updated')
    }
  } catch (e) {
    toast.add({ title: 'Failed to update redirect URIs', icon: 'i-heroicons-x-circle', color: 'red' })
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
      toast.add({ title: 'Secret rotated', icon: 'i-heroicons-check-circle', color: 'green' })
      emit('updated')
    }
  } catch (e) {
    toast.add({ title: 'Failed to rotate secret', icon: 'i-heroicons-x-circle', color: 'red' })
  } finally {
    rotatingId.value = null
  }
}

async function remove(client: OAuthClient) {
  if (!confirm(`Delete OAuth client "${client.name}"?`)) return
  try {
    await useMyFetch(`/api/oauth/clients/${client.id}`, { method: 'DELETE' })
    clients.value = clients.value.filter(c => c.id !== client.id)
    delete freshSecretByClientId.value[client.client_id]
    toast.add({ title: 'OAuth client deleted', icon: 'i-heroicons-check-circle', color: 'green' })
    emit('updated')
  } catch (e) {
    toast.add({ title: 'Failed to delete client', icon: 'i-heroicons-x-circle', color: 'red' })
  }
}

onMounted(async () => {
  loading.value = true
  await Promise.all([loadBaseUrl(), loadClients()])
  loading.value = false
})
</script>
