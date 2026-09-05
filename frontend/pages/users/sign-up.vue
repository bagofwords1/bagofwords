<template>
  <div class="min-h-screen flex items-center justify-center px-6 py-20" v-if="pageLoaded">
    <div class="w-full max-w-[360px]">
      <h1 class="text-xl font-semibold text-center text-gray-900 dark:text-white">
        {{ $t('auth.signUp') }}
      </h1>

      <p v-if="error_message" v-html="error_message"
         class="mt-5 text-sm text-red-500 text-center whitespace-pre-line"></p>

      <div v-if="showSso" class="mt-7">
        <AuthProviderButtons
          :providers="oidcProviders"
          :google-enabled="googleSignIn"
          mode="sign-up"
          @error="onProviderError"
        />
      </div>

      <div v-if="showSso && showCredentials" class="relative my-6">
        <div class="absolute inset-0 flex items-center" aria-hidden="true">
          <div class="w-full border-t border-gray-200 dark:border-gray-800"></div>
        </div>
        <div class="relative flex justify-center">
          <span class="px-3 text-xs text-gray-500 dark:text-gray-400 bg-white dark:bg-gray-950">
            {{ $t('auth.orContinueWithEmail') }}
          </span>
        </div>
      </div>

      <form v-if="showCredentials" @submit.prevent="submit" :class="showSso ? '' : 'mt-7'">
        <div>
          <label for="name" class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1.5">
            {{ $t('auth.name') }}
          </label>
          <input id="name" v-model="name" type="text" autocomplete="name" :class="inputClass" />
        </div>

        <div class="mt-4">
          <label for="email" class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1.5">
            {{ $t('auth.email') }}
          </label>
          <input id="email" v-model="email" type="email" autocomplete="email" :class="inputClass" />
        </div>

        <div class="mt-4">
          <label for="password" class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1.5">
            {{ $t('auth.password') }}
          </label>
          <input id="password" v-model="password" type="password" autocomplete="new-password" :class="inputClass" />
        </div>

        <button
          type="submit"
          :disabled="isSubmitting"
          class="mt-5 w-full h-10 inline-flex items-center justify-center rounded-lg text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500/40 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          <template v-if="isSubmitting">
            <Spinner class="h-4 w-4 me-2" />
            {{ $t('auth.signingUp') }}
          </template>
          <template v-else>{{ $t('auth.signUp') }}</template>
        </button>
      </form>

      <p v-if="authMode !== 'sso_only'" class="mt-6 text-sm text-center text-gray-500 dark:text-gray-400">
        {{ $t('auth.alreadyHaveAccount') }}
        <NuxtLink to="/users/sign-in" class="text-blue-600 hover:text-blue-700 dark:text-blue-400">
          {{ $t('auth.signIn') }}
        </NuxtLink>
      </p>

      <p class="mt-8 text-xs text-center text-gray-400 dark:text-gray-500">
        {{ $t('auth.termsPrefix') }}
        <a href="https://bagofwords.com/terms" target="_blank" class="underline hover:text-gray-600 dark:hover:text-gray-300">{{ $t('auth.termsOfService') }}</a>
        {{ $t('common.and') }}
        <a href="https://bagofwords.com/privacy" target="_blank" class="underline hover:text-gray-600 dark:hover:text-gray-300">{{ $t('auth.privacyPolicy') }}</a>
      </p>
    </div>
  </div>
  <div v-else class="min-h-screen flex items-center justify-center"><Spinner class="h-6 w-6" /></div>
</template>

<script setup lang="ts">
import qs from 'qs'
import { ref, computed, onMounted } from 'vue'
import Spinner from '~/components/Spinner.vue'
import { definePageMeta, useAuth, useRuntimeConfig, useRoute } from '#imports'
const { t } = useI18n()
const { rawToken } = useAuthState()
const toast = useToast()
const route = useRoute()

definePageMeta({
auth: {
  unauthenticatedOnly: true,
  navigateAuthenticatedTo: '/'
},
layout: 'users'
})

const name = ref('');
const email = ref('');
const password = ref('');
const inviteToken = ref('');
const error_message = ref('')

// Shape of an entry in /api/settings -> oidc_providers. `label` and `brand`
// are derived server-side so the button can show a real product name and logo
// instead of the routing slug.
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
const pageLoaded = ref(false)
const isSubmitting = ref(false)
const authMode = ref<'hybrid'|'local_only'|'sso_only'>('hybrid')

const showCredentials = computed(() => authMode.value !== 'sso_only')
const showSso = computed(() =>
  authMode.value !== 'local_only' && (googleSignIn.value || oidcProviders.value.length > 0))

const inputClass =
  'w-full h-10 px-3 rounded-lg border border-gray-300 dark:border-gray-700 ' +
  'bg-white dark:bg-gray-900 text-sm text-gray-900 dark:text-white ' +
  'focus:outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 transition-colors'

function onProviderError(message: string) {
  error_message.value = message
}

const { signIn, getSession } = useAuth();
const { ensureOrganization, fetchOrganization } = useOrganization()

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

// Pre-fill email from URL query parameter
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
  } catch (_) {}
  const inviteError = route.query.error as string
  if (inviteError) {
    error_message.value = inviteError
  }
  const emailFromQuery = route.query.email as string
  if (emailFromQuery) {
    email.value = emailFromQuery
  }
  const tokenFromQuery = route.query.token as string
  if (tokenFromQuery) {
    inviteToken.value = tokenFromQuery
  }
  // show spinner frame until mounted work finishes
  await nextTick()
  pageLoaded.value = true
})

async function signInWithCredentials(email: string, password: string) {
  const credentials = {
    username: email,
    password: password,
  };

  try {
    const response = await $fetch('/api/auth/jwt/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: qs.stringify(credentials),
    });

    if (!response) {
      throw new Error('Authentication failed');
    }
    rawToken.value = response.access_token
    await getSession({ force: true })

    // Check if the user has an organization (same as sign-in flow)
    const org = await fetchOrganization();
    if (!org || !org.id) {
      navigateTo('/organizations/new');
    } else {
      navigateTo('/');
    }

  } catch (error) {
    console.error('Error during authentication:', error);
  }
}

async function submit() {
isSubmitting.value = true
error_message.value = ''
const payload: Record<string, string> = {
  name: name.value,
  email: email.value,
  password: password.value
}
if (inviteToken.value) {
  payload.invite_token = inviteToken.value
}

try {
  const response = await $fetch('/api/auth/register', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  });

  if (!response) {
    error_message.value = t('auth.registrationError')
    isSubmitting.value = false
    return
  }

  // Add automatic login after successful registration
  await signInWithCredentials(email.value, password.value)

} catch (error: any) {
  console.error('Error fetching data:', error);
  error_message.value = extractErrorMessage(error, t('auth.registrationError'))
  isSubmitting.value = false
}
}

async function verifyEmail(email: string) {
const response = await $fetch('/api/auth/request-verify-token', {
  method: 'POST',
  body: {
    email: email
  }
});

if (response) {
  navigateTo('/users/verify');
}
}
</script>
