<template>
  <div class="mt-4">
    <div class="mb-4">
      <h2 class="text-sm font-medium text-gray-900">SCIM Provisioning</h2>
      <p class="text-xs text-gray-500 mt-0.5">Automatically provision and deprovision users from your identity provider</p>
    </div>

    <!-- Enterprise Gate -->
    <template v-if="!hasFeature('scim')">
      <div class="rounded border border-gray-200 p-4 bg-gray-50">
        <p class="text-xs text-gray-600 mb-2">
          SCIM provisioning requires an enterprise license. Automatically sync users from Okta, Azure AD, OneLogin, and other identity providers.
        </p>
        <a
          href="https://docs.bagofwords.com/enterprise"
          target="_blank"
          rel="noopener noreferrer"
          class="text-xs text-blue-600 hover:text-blue-700"
        >
          Learn more →
        </a>
      </div>
    </template>

    <!-- SCIM Content -->
    <template v-else>
      <!-- SCIM Endpoint URL -->
      <div class="mb-4 rounded border border-gray-200 p-3">
        <label class="block text-xs font-medium text-gray-700 mb-1">SCIM Base URL</label>
        <div class="flex items-center gap-2">
          <code class="flex-1 text-xs bg-gray-50 px-2 py-1.5 rounded border border-gray-200 text-gray-700 font-mono">
            {{ scimBaseUrl }}
          </code>
          <button
            class="px-2 py-1.5 text-xs text-gray-500 hover:text-gray-700 border border-gray-200 rounded hover:border-gray-300"
            @click="copyToClipboard(scimBaseUrl)"
          >
            {{ copied === 'url' ? 'Copied' : 'Copy' }}
          </button>
        </div>
        <p class="text-[11px] text-gray-400 mt-1">Paste this URL into your identity provider's SCIM configuration</p>
      </div>

      <!-- Token Management -->
      <div class="mb-3 flex items-center justify-between">
        <label class="text-xs font-medium text-gray-700">Bearer Tokens</label>
        <button
          class="px-2 py-1 text-xs text-white bg-blue-600 rounded hover:bg-blue-700"
          @click="showCreateModal = true"
        >
          Generate Token
        </button>
      </div>

      <!-- Loading State -->
      <div v-if="loading" class="py-8 text-center">
        <div class="inline-block w-4 h-4 border-2 border-gray-200 border-t-gray-500 rounded-full animate-spin"></div>
      </div>

      <!-- Error State -->
      <div v-else-if="error" class="py-6 text-center text-xs text-red-500">
        {{ error }}
      </div>

      <!-- Tokens List -->
      <div v-else class="border border-gray-200 rounded overflow-hidden">
        <template v-if="tokens.length > 0">
          <div
            v-for="(token, idx) in tokens"
            :key="token.id"
            class="flex items-center px-3 py-2.5 text-xs"
            :class="{ 'border-t border-gray-100': idx > 0 }"
          >
            <span class="w-36 flex-shrink-0 text-gray-700 font-medium truncate">{{ token.name }}</span>
            <span class="w-36 flex-shrink-0 text-gray-400 font-mono text-[11px]">{{ token.token_prefix }}...</span>
            <span class="flex-1 text-gray-400 text-[11px]">
              <template v-if="token.last_used_at">
                Last used {{ formatRelativeTime(token.last_used_at) }}
              </template>
              <template v-else>
                Never used
              </template>
            </span>
            <span class="w-24 flex-shrink-0 text-gray-400 text-[11px]">
              {{ formatRelativeTime(token.created_at) }}
            </span>
            <button
              class="text-[11px] text-red-500 hover:text-red-700 ml-2"
              @click="confirmRevoke(token)"
            >
              Revoke
            </button>
          </div>
        </template>

        <!-- Empty State -->
        <div v-else class="py-8 text-center">
          <p class="text-xs text-gray-400">No SCIM tokens yet</p>
          <p class="text-[11px] text-gray-400 mt-1">Generate a token to connect your identity provider</p>
        </div>
      </div>

    </template>

    <!-- Create Token Modal -->
    <div v-if="showCreateModal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="dismissCreateModal">
      <div class="bg-white rounded-lg shadow-lg w-full max-w-sm p-4">
        <h3 class="text-sm font-medium text-gray-900 mb-3">Generate SCIM Token</h3>

        <!-- Step 1: Name input -->
        <template v-if="!createdToken">
          <div class="mb-3">
            <label class="block text-xs text-gray-600 mb-1">Name</label>
            <input
              v-model="newTokenName"
              type="text"
              placeholder="e.g., Okta, Azure AD"
              class="w-full px-2 py-1.5 text-xs border border-gray-200 rounded focus:outline-none focus:border-gray-400"
              @keydown.enter="handleCreateToken"
            />
          </div>
          <div class="flex justify-end gap-2">
            <button
              class="px-3 py-1.5 text-xs text-gray-500 hover:text-gray-700"
              @click="dismissCreateModal"
            >
              Cancel
            </button>
            <button
              class="px-3 py-1.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50"
              :disabled="!newTokenName.trim() || creating"
              @click="handleCreateToken"
            >
              {{ creating ? 'Generating...' : 'Generate' }}
            </button>
          </div>
        </template>

        <!-- Step 2: Show token -->
        <template v-else>
          <div class="rounded border border-amber-200 bg-amber-50 p-3 mb-3">
            <div class="flex items-start gap-2">
              <svg class="w-4 h-4 text-amber-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
              </svg>
              <p class="text-xs font-medium text-amber-800">Copy this token now — you won't be able to see it again</p>
            </div>
          </div>
          <div class="flex items-center gap-2 mb-3">
            <code class="flex-1 text-[11px] bg-gray-50 px-2 py-1.5 rounded border border-gray-200 text-gray-700 font-mono truncate">
              {{ createdToken }}
            </code>
            <button
              class="px-2 py-1.5 text-xs text-gray-500 hover:text-gray-700 border border-gray-200 rounded hover:border-gray-300 flex-shrink-0"
              @click="copyToClipboard(createdToken!, 'token')"
            >
              {{ copied === 'token' ? 'Copied' : 'Copy' }}
            </button>
          </div>
          <div class="flex justify-end">
            <button
              class="px-3 py-1.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700"
              @click="dismissCreateModal"
            >
              Done
            </button>
          </div>
        </template>
      </div>
    </div>

    <!-- Revoke Confirmation Modal -->
    <div v-if="tokenToRevoke" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="tokenToRevoke = null">
      <div class="bg-white rounded-lg shadow-lg w-full max-w-sm p-4">
        <h3 class="text-sm font-medium text-gray-900 mb-2">Revoke Token</h3>
        <p class="text-xs text-gray-600 mb-3">
          Revoking <span class="font-medium">{{ tokenToRevoke.name }}</span> will immediately disconnect your identity provider's SCIM integration. This cannot be undone.
        </p>
        <div class="flex justify-end gap-2">
          <button
            class="px-3 py-1.5 text-xs text-gray-500 hover:text-gray-700"
            @click="tokenToRevoke = null"
          >
            Cancel
          </button>
          <button
            class="px-3 py-1.5 text-xs text-white bg-red-600 rounded hover:bg-red-700"
            @click="handleRevoke"
          >
            Revoke
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useScimTokens, type ScimToken } from '~/ee/composables/useScimTokens'

definePageMeta({
  auth: true,
  permissions: ['manage_scim'],
  layout: 'settings'
})

const { hasFeature, license } = useEnterprise()
const { tokens, loading, error, fetchTokens, createToken, revokeToken } = useScimTokens()

const showCreateModal = ref(false)
const newTokenName = ref('SCIM Token')
const creating = ref(false)
const createdToken = ref<string | null>(null)
const tokenToRevoke = ref<ScimToken | null>(null)
const copied = ref<string | null>(null)
const hasFetched = ref(false)

const scimBaseUrl = computed(() => {
  if (process.client) {
    return `${window.location.origin}/scim/v2`
  }
  return '/scim/v2'
})

const dismissCreateModal = () => {
  showCreateModal.value = false
  createdToken.value = null
  newTokenName.value = 'SCIM Token'
}

const handleCreateToken = async () => {
  if (!newTokenName.value.trim() || creating.value) return
  creating.value = true
  const result = await createToken(newTokenName.value.trim())
  creating.value = false

  if (result) {
    createdToken.value = result.token
  }
}

const confirmRevoke = (token: ScimToken) => {
  tokenToRevoke.value = token
}

const handleRevoke = async () => {
  if (!tokenToRevoke.value) return
  await revokeToken(tokenToRevoke.value.id)
  tokenToRevoke.value = null
  createdToken.value = null
}

const copyToClipboard = async (text: string, key: string = 'url') => {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = key
    setTimeout(() => { copied.value = null }, 2000)
  } catch {
    // Fallback
  }
}

const formatRelativeTime = (timestamp: string | null) => {
  if (!timestamp) return ''
  const isoTimestamp = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z'
  const date = new Date(isoTimestamp)
  const now = new Date()
  const diff = now.getTime() - date.getTime()
  const minutes = Math.floor(diff / 60000)
  const hours = Math.floor(diff / 3600000)
  const days = Math.floor(diff / 86400000)

  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  if (hours < 24) return `${hours}h ago`
  if (days < 7) return `${days}d ago`

  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

// Fetch tokens when license is loaded
watch(
  () => license.value,
  (newLicense) => {
    if (newLicense && hasFeature('scim') && !hasFetched.value) {
      hasFetched.value = true
      fetchTokens()
    }
  },
  { immediate: true }
)
</script>
