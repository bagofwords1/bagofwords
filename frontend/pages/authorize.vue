<template>
  <div class="flex h-screen justify-center py-20 px-5 sm:px-0">
    <div class="w-full text-center sm:w-[420px]">
      <div>
        <img src="/assets/logo-128.png" alt="Bag of Words" class="h-10 w-10 mx-auto" />
      </div>

      <!-- Loading -->
      <div v-if="pageLoading" class="mt-8">
        <Spinner class="w-8 h-8 mx-auto text-gray-400" />
        <p class="text-sm text-gray-500 mt-4">Loading...</p>
      </div>

      <!-- Error -->
      <div v-else-if="errorMessage" class="mt-6">
        <div class="px-6 py-5 border border-red-200 rounded-xl bg-red-50">
          <p class="text-sm text-red-700">{{ errorMessage }}</p>
        </div>
        <button
          @click="deny"
          class="mt-4 text-sm text-gray-500 hover:text-gray-700 underline"
        >
          Go back
        </button>
      </div>

      <!-- Consent screen -->
      <div v-else class="mt-4">
        <h1 class="font-medium text-2xl mb-2">Authorize Access</h1>
        <p class="text-sm text-gray-500 mb-6">
          <strong>{{ clientName }}</strong> wants to access your Bag of Words account.
        </p>

        <div class="px-6 py-5 border border-gray-200 rounded-xl shadow-sm bg-white text-start">
          <!-- Scope info -->
          <div class="mb-5">
            <div class="text-xs uppercase tracking-wide text-gray-400 mb-2">Permissions requested</div>
            <div class="flex items-center gap-2 text-sm text-gray-700">
              <UIcon name="heroicons-check-circle" class="w-4 h-4 text-green-500 flex-shrink-0" />
              Access your MCP tools (query data, create reports)
            </div>
          </div>

          <!-- Action buttons -->
          <div class="flex gap-3">
            <button
              @click="deny"
              class="flex-1 px-4 py-2.5 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50 transition-colors"
            >
              Deny
            </button>
            <button
              @click="approve"
              :disabled="approving"
              class="flex-1 px-4 py-2.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:bg-gray-400 disabled:cursor-not-allowed transition-colors flex items-center justify-center"
            >
              <template v-if="approving">
                <Spinner class="h-4 w-4 me-2" />
                Authorizing...
              </template>
              <template v-else>Approve</template>
            </button>
          </div>
        </div>

        <p class="text-xs text-gray-400 mt-4">
          You'll be redirected back to {{ clientName }} after approving.
        </p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

definePageMeta({
  layout: 'users',
})

const route = useRoute()
const { status, getSession } = useAuth()
const { rawToken } = useAuthState()
const config = useRuntimeConfig()

const pageLoading = ref(true)
const errorMessage = ref('')
const clientName = ref('Unknown Application')
const approving = ref(false)

// Extract OAuth params from query string
const clientId = route.query.client_id as string
const redirectUri = route.query.redirect_uri as string
const state = route.query.state as string | undefined
const scope = (route.query.scope as string) || 'mcp'
const codeChallenge = route.query.code_challenge as string
const codeChallengeMethod = (route.query.code_challenge_method as string) || 'S256'
const responseType = (route.query.response_type as string) || 'code'

onMounted(async () => {
  // Check if user is authenticated
  await getSession()
  if (status.value !== 'authenticated') {
    // Redirect to sign-in with return URL
    const currentUrl = window.location.pathname + window.location.search
    navigateTo(`/users/sign-in?redirect=${encodeURIComponent(currentUrl)}`)
    return
  }

  // Validate required params
  if (!clientId || !redirectUri || !codeChallenge) {
    errorMessage.value = 'Invalid authorization request. Missing required parameters.'
    pageLoading.value = false
    return
  }

  if (responseType !== 'code') {
    errorMessage.value = 'Unsupported response type. Only "code" is supported.'
    pageLoading.value = false
    return
  }

  // Fetch client info
  try {
    const data = await $fetch(`/api/oauth/clients/${clientId}/info`, {
      baseURL: config.public.baseURL,
    })
    clientName.value = (data as any)?.name || 'Unknown Application'
  } catch (e) {
    errorMessage.value = 'Unknown client. The application requesting access was not found.'
    pageLoading.value = false
    return
  }

  pageLoading.value = false
})

async function approve() {
  approving.value = true
  try {
    const { organization } = useOrganization()
    const headers: Record<string, string> = {
      Authorization: `Bearer ${rawToken.value}`,
    }
    if (organization.value?.id) {
      headers['X-Organization-Id'] = organization.value.id
    }

    const data = await $fetch('/api/oauth/authorize', {
      baseURL: config.public.baseURL,
      method: 'POST',
      headers,
      body: {
        client_id: clientId,
        redirect_uri: redirectUri,
        state,
        scope,
        code_challenge: codeChallenge,
        code_challenge_method: codeChallengeMethod,
      },
    })

    const redirectUrl = (data as any)?.redirect_url
    if (redirectUrl) {
      window.location.href = redirectUrl
    } else {
      errorMessage.value = 'Failed to generate authorization code.'
      approving.value = false
    }
  } catch (e: any) {
    const detail = e?.data?.detail || e?.message || 'Authorization failed'
    errorMessage.value = typeof detail === 'string' ? detail : JSON.stringify(detail)
    approving.value = false
  }
}

function deny() {
  if (redirectUri) {
    const separator = redirectUri.includes('?') ? '&' : '?'
    const params = `error=access_denied`
    const url = state
      ? `${redirectUri}${separator}${params}&state=${encodeURIComponent(state)}`
      : `${redirectUri}${separator}${params}`
    window.location.href = url
  } else {
    navigateTo('/')
  }
}
</script>
