<template>
  <main class="flex min-h-screen items-start justify-center bg-gray-50 px-5 py-16 dark:bg-gray-950 sm:py-24">
    <div class="w-full max-w-md">
      <div class="text-center">
        <img src="/assets/logo-128.png" alt="Bag of Words" class="mx-auto h-11 w-11" />
      </div>

      <div v-if="pageLoading || trustedAutoApprove" class="mt-8 rounded-2xl border border-gray-200 bg-white px-7 py-10 text-center shadow-sm dark:border-gray-800 dark:bg-gray-900">
        <Spinner class="mx-auto h-7 w-7 text-blue-500" />
        <h1 class="mt-4 text-base font-medium text-gray-900 dark:text-white">
          {{ trustedAutoApprove ? $t('settings.integrations.channels.oauth.trustedConnecting') : $t('settings.integrations.channels.oauth.authorizationLoading') }}
        </h1>
        <p v-if="trustedAutoApprove" class="mt-1 text-sm text-gray-500 dark:text-gray-400">{{ clientName }}</p>
      </div>

      <div v-else-if="errorMessage" class="mt-8 rounded-2xl border border-red-200 bg-white p-6 text-center shadow-sm dark:border-red-900 dark:bg-gray-900">
        <div class="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-red-50 dark:bg-red-950">
          <UIcon name="i-heroicons-exclamation-triangle" class="h-5 w-5 text-red-600 dark:text-red-400" />
        </div>
        <h1 class="mt-3 text-base font-semibold text-gray-900 dark:text-white">{{ $t('settings.integrations.channels.oauth.authorizationError') }}</h1>
        <p class="mt-2 text-sm leading-6 text-red-700 dark:text-red-300">{{ errorMessage }}</p>
        <button class="mt-5 text-sm text-gray-500 underline underline-offset-4 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200" @click="goBack">
          {{ $t('settings.integrations.channels.oauth.goBack') }}
        </button>
      </div>

      <div v-else class="mt-8 overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm dark:border-gray-800 dark:bg-gray-900">
        <div class="border-b border-gray-100 px-6 py-5 text-center dark:border-gray-800">
          <div class="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-blue-50 text-sm font-semibold text-blue-700 ring-1 ring-blue-100 dark:bg-blue-950/50 dark:text-blue-300 dark:ring-blue-900">
            {{ clientInitials }}
          </div>
          <h1 class="mt-3 text-xl font-semibold tracking-tight text-gray-900 dark:text-white">{{ $t('settings.integrations.channels.oauth.authorizationTitle') }}</h1>
          <p class="mt-1 text-sm leading-6 text-gray-500 dark:text-gray-400">
            {{ $t('settings.integrations.channels.oauth.wantsAccess', { name: clientName }) }}
          </p>
        </div>

        <div class="px-6 py-5">
          <div class="text-xs font-medium uppercase tracking-wide text-gray-400">{{ $t('settings.integrations.channels.oauth.permissionsRequested') }}</div>
          <div class="mt-3 space-y-3">
            <div v-for="requestedScope in requestedScopes" :key="requestedScope" class="flex gap-3 rounded-xl bg-gray-50 p-3 dark:bg-gray-950/60">
              <div class="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-green-100 dark:bg-green-950">
                <UIcon name="i-heroicons-check" class="h-3.5 w-3.5 text-green-700 dark:text-green-300" />
              </div>
              <div>
                <div class="text-sm font-medium text-gray-800 dark:text-gray-200">{{ scopeTitle(requestedScope) }}</div>
                <div class="mt-0.5 text-xs leading-5 text-gray-500 dark:text-gray-400">{{ scopeDescription(requestedScope) }}</div>
              </div>
            </div>
          </div>

          <div class="mt-6 flex gap-3">
            <UButton color="gray" variant="outline" size="lg" class="min-w-0 flex-1 justify-center" @click="deny">
              {{ $t('settings.integrations.channels.oauth.deny') }}
            </UButton>
            <UButton color="blue" size="lg" class="min-w-0 flex-1 justify-center" :loading="approving" @click="approve">
              {{ approving ? $t('settings.integrations.channels.oauth.authorizing') : $t('settings.integrations.channels.oauth.approve') }}
            </UButton>
          </div>
        </div>

        <p class="border-t border-gray-100 px-6 py-3 text-center text-xs text-gray-400 dark:border-gray-800">
          {{ $t('settings.integrations.channels.oauth.redirectNotice', { name: clientName }) }}
        </p>
      </div>
    </div>
  </main>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

definePageMeta({ layout: 'users' })

const route = useRoute()
const { status, getSession } = useAuth()
const { rawToken } = useAuthState()
const { t } = useI18n()
const config = useRuntimeConfig()

const pageLoading = ref(true)
const trustedAutoApprove = ref(false)
const requestValidated = ref(false)
const errorMessage = ref('')
const clientName = ref('')
const requestedScopes = ref<string[]>([])
const approving = ref(false)

const clientId = route.query.client_id as string
const redirectUri = route.query.redirect_uri as string
const state = route.query.state as string | undefined
const scope = (route.query.scope as string) || 'mcp'
const codeChallenge = route.query.code_challenge as string
const codeChallengeMethod = (route.query.code_challenge_method as string) || 'S256'
const responseType = (route.query.response_type as string) || 'code'

const clientInitials = computed(() => clientName.value.split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]?.toUpperCase()).join('') || 'OA')

function scopeTitle(value: string) {
  return value === 'app'
    ? t('settings.integrations.channels.oauth.appScope')
    : t('settings.integrations.channels.oauth.mcpScope')
}

function scopeDescription(value: string) {
  return value === 'app'
    ? t('settings.integrations.channels.oauth.appPermission')
    : t('settings.integrations.channels.oauth.mcpPermission')
}

onMounted(async () => {
  await getSession()
  if (status.value !== 'authenticated') {
    const returnTo = window.location.pathname + window.location.search
    await navigateTo(`/users/sign-in?redirect=${encodeURIComponent(returnTo)}`)
    return
  }

  if (!clientId || !redirectUri || !codeChallenge) {
    errorMessage.value = t('settings.integrations.channels.oauth.invalidAuthorizationRequest')
    pageLoading.value = false
    return
  }
  if (responseType !== 'code' || codeChallengeMethod !== 'S256') {
    errorMessage.value = t('settings.integrations.channels.oauth.unsupportedAuthorizationRequest')
    pageLoading.value = false
    return
  }

  try {
    const data = await $fetch(`/api/oauth/clients/${clientId}/info`, {
      baseURL: config.public.baseURL,
      params: { redirect_uri: redirectUri, scope },
    }) as any
    clientName.value = data.name
    requestedScopes.value = data.requested_scopes || scope.split(' ')
    requestValidated.value = true
    pageLoading.value = false
    if (data.trusted) {
      trustedAutoApprove.value = true
      await nextTick()
      await approve()
    }
  } catch {
    errorMessage.value = t('settings.integrations.channels.oauth.unknownAuthorizationClient')
    pageLoading.value = false
  }
})

async function approve() {
  if (!requestValidated.value || approving.value) return
  approving.value = true
  try {
    const { organization } = useOrganization()
    const headers: Record<string, string> = { Authorization: `Bearer ${rawToken.value}` }
    if (organization.value?.id) headers['X-Organization-Id'] = organization.value.id

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
    }) as any
    if (!data.redirect_url) throw new Error('missing_redirect')
    window.location.assign(data.redirect_url)
  } catch {
    trustedAutoApprove.value = false
    errorMessage.value = t('settings.integrations.channels.oauth.authorizationFailed')
    approving.value = false
  }
}

function deny() {
  if (!requestValidated.value) return goBack()
  const callback = new URL(redirectUri)
  callback.searchParams.set('error', 'access_denied')
  if (state) callback.searchParams.set('state', state)
  window.location.assign(callback.toString())
}

function goBack() {
  navigateTo('/')
}
</script>
