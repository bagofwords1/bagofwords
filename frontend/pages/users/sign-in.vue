<template>
  <div class="min-h-screen flex items-center justify-center px-6 py-24" v-if="pageLoaded">
    <div class="w-full max-w-[360px]">
      <h1 class="text-[22px] leading-tight font-semibold tracking-tight text-center text-gray-900 dark:text-white">
        {{ $t('auth.welcomeBack') }}
      </h1>
      <p class="mt-1.5 text-sm text-center text-gray-500 dark:text-gray-400">
        {{ $t('auth.signInSubtitle') }}
      </p>

      <p v-if="error_message" v-html="error_message"
         class="mt-5 text-sm text-red-500 text-center whitespace-pre-line"></p>

      <div v-if="showSso" class="mt-7">
        <AuthProviderButtons
          :providers="oidcProviders"
          :google-enabled="googleSignIn"
          mode="sign-in"
          @error="onProviderError"
        />
      </div>

      <div v-if="showSso && showCredentials" class="relative my-5">
        <div class="absolute inset-0 flex items-center" aria-hidden="true">
          <div class="w-full border-t border-gray-200 dark:border-gray-800"></div>
        </div>
        <div class="relative flex justify-center">
          <span class="px-3 text-[11px] uppercase tracking-widest text-gray-400 dark:text-gray-500 bg-white dark:bg-gray-950">
            {{ $t('auth.or') }}
          </span>
        </div>
      </div>

      <form v-if="showCredentials" @submit.prevent="signInWithCredentials()" :class="showSso ? '' : 'mt-7'">
        <div>
          <label for="email" :class="labelClass">{{ $t('auth.email') }}</label>
          <input id="email" v-model="email" type="email" autocomplete="email" :class="inputClass" />
        </div>

        <div class="mt-4">
          <div class="flex items-baseline justify-between">
            <label for="password" :class="labelClass">{{ $t('auth.password') }}</label>
            <NuxtLink
              v-if="smtpEnabled"
              to="/users/forgot-password"
              class="mb-1.5 text-[13px] text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white transition-colors"
            >
              {{ $t('auth.forgotPassword') }}
            </NuxtLink>
          </div>
          <input id="password" v-model="password" type="password" autocomplete="current-password" :class="inputClass" />
        </div>

        <button type="submit" :disabled="isSubmitting" :class="primaryButtonClass">
          <template v-if="isSubmitting">
            <Spinner class="h-3.5 w-3.5 me-2" />
            {{ $t('auth.loggingIn') }}
          </template>
          <template v-else>{{ $t('auth.signIn') }}</template>
        </button>
      </form>

      <p v-if="authMode !== 'sso_only'" class="mt-6 text-sm text-center text-gray-500 dark:text-gray-400">
        {{ $t('auth.newToBow') }}
        <NuxtLink to="/users/sign-up" class="font-medium text-gray-900 dark:text-white hover:underline underline-offset-4">
          {{ $t('auth.signUp') }}
        </NuxtLink>
      </p>
    </div>
  </div>
  <div v-else class="min-h-screen flex items-center justify-center"><Spinner class="h-6 w-6" /></div>
</template>

<script setup lang="ts">

  import qs from 'qs';

  import { ref, computed, onMounted } from 'vue';
  import Spinner from '~/components/Spinner.vue';

  const { t } = useI18n()
  const { rawToken } = useAuthState()
  const { fetchOrganization } = useOrganization()
  const route = useRoute()
  // Shape of an entry in /api/settings -> oidc_providers. `label` and `brand`
  // are derived server-side so the sign-in button can show a real product name
  // and logo instead of the routing slug.
  interface AuthProvider {
    name: string
    enabled?: boolean
    label?: string
    brand?: 'microsoft' | 'google' | 'custom'
    icon?: string | null
  }

  // Google availability comes from /api/settings, the same place the OIDC list
  // and auth mode come from — runtimeConfig never defined a googleSignIn key.
  const googleSignIn = ref(false)
  const oidcProviders = ref<AuthProvider[]>([])
  const authMode = ref<'hybrid'|'local_only'|'sso_only'>('hybrid')
  const smtpEnabled = ref(false)
  const isSubmitting = ref(false)
  const localOverride = computed(() => route.query.local === 'true')

  // `?local=true` is the escape hatch that lets an admin reach the password
  // form on an sso_only instance.
  const showCredentials = computed(() => authMode.value !== 'sso_only' || localOverride.value)
  const showSso = computed(() =>
    authMode.value !== 'local_only' && (googleSignIn.value || oidcProviders.value.length > 0))

  const labelClass = 'block text-[13px] font-medium text-gray-900 dark:text-gray-100 mb-1.5'

  // Monochrome controls: the only color on the page is the provider logos and
  // the one primary action.
  const inputClass =
    'w-full h-10 px-3 rounded-lg border border-gray-300 dark:border-gray-700 ' +
    'bg-white dark:bg-gray-900 text-sm text-gray-900 dark:text-white ' +
    'focus:outline-none focus:border-gray-900 dark:focus:border-white ' +
    'focus:ring-1 focus:ring-gray-900 dark:focus:ring-white transition-colors'

  const primaryButtonClass =
    'mt-5 w-full h-10 inline-flex items-center justify-center rounded-lg text-sm font-medium ' +
    'text-white bg-gray-900 hover:bg-gray-800 dark:text-gray-900 dark:bg-white dark:hover:bg-gray-100 ' +
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-900 ' +
    'dark:focus-visible:ring-white focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-950 ' +
    'disabled:opacity-40 disabled:cursor-not-allowed transition-colors'

  function onProviderError(message: string) {
    error_message.value = message
  }

  definePageMeta({
  auth: {
    unauthenticatedOnly: true,
  },
    layout: 'users'
})

  // Define reactive references for email and password
  const email = ref('');
  const password = ref('');

  const error_message = ref('')
  // Extract the signIn function from useAuth
  const { signIn, getSession } = useAuth();

  // Only honor redirects to same-origin paths to avoid open-redirect bugs
  function safeRedirectTarget(value: unknown): string | null {
    if (typeof value !== 'string' || !value) return null
    if (!value.startsWith('/') || value.startsWith('//')) return null
    return value
  }

  const OAUTH_REDIRECT_STORAGE_NAME = 'bow:postSignInRedirect'

  // Helper to extract error message from server response
  function extractErrorMessage(error: any, fallback: string): string {
    const data = error?.data
    if (!data) return fallback

    // Handle FastAPI validation errors (detail array)
    if (Array.isArray(data.detail)) {
      return data.detail.map((d: any) => d.msg || d.message || JSON.stringify(d)).join('\n')
    }
    // Handle simple detail string
    if (typeof data.detail === 'string') {
      return data.detail
    }
    // Handle message field
    if (data.message) {
      return data.message
    }
    return fallback
  }
  const pageLoaded = ref(false)

  // Add this code to handle URL parameters
  onMounted(async () => {
    try {
      const settings = await $fetch('/api/settings')
      if (settings?.oidc_providers?.length) {
        oidcProviders.value = settings.oidc_providers.filter((p: any) => p.enabled)
      }
      if (settings?.auth?.mode) {
        authMode.value = settings.auth.mode
      }
      googleSignIn.value = settings?.google_oauth?.enabled ?? false
      smtpEnabled.value = settings?.smtp_enabled ?? false

      // Nothing to sign in to on an unclaimed instance — the first account is
      // created through sign-up, so send visitors straight there.
      if (settings?.setup_required) {
        return navigateTo('/users/sign-up')
      }
    } catch (_) {}
    const inviteError = route.query.error as string
    if (inviteError) {
      error_message.value = inviteError
    }
    const access_token = route.query.access_token as string
    const userEmail = route.query.email as string
    if (access_token) {
      rawToken.value = access_token
      await getSession({ force: true })
      // Check if the user has an organization (same as credentials login)
      const org = await fetchOrganization()
      if (!org || !org.id) {
        navigateTo('/organizations/new')
      } else {
        let pendingRedirect: string | null = null
        try {
          pendingRedirect = safeRedirectTarget(sessionStorage.getItem(OAUTH_REDIRECT_STORAGE_NAME))
          sessionStorage.removeItem(OAUTH_REDIRECT_STORAGE_NAME)
        } catch (_) {}
        navigateTo(pendingRedirect || '/')
      }
      return
    }
    pageLoaded.value = true
  })


  async function signInWithCredentials() {
    isSubmitting.value = true
    error_message.value = ''
    const route = useRoute();
    const redirectedFrom = safeRedirectTarget(route.query.redirect)

    const credentials = {
      username: email.value,
      password: password.value,
    };

    try {
      const response = await $fetch('/api/auth/jwt/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: qs.stringify(credentials),
      });


      if (response) {
        rawToken.value = response.access_token
        await getSession({ force: true })

        // Check if the user has an organization
        const org = await fetchOrganization();
        if (!org || !org.id) {
          navigateTo('/organizations/new');
        } else {
          if (redirectedFrom) {
            navigateTo(redirectedFrom);
          } else {
            navigateTo('/');
          }
        }
      }
      else {
        error_message.value = t('auth.invalidCredentials')
        isSubmitting.value = false
      }
    } catch (error: any) {
      error_message.value = extractErrorMessage(error, t('auth.invalidCredentials'))
      isSubmitting.value = false
    }
  }

  </script>
