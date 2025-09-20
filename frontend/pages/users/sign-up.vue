<template>
  <div class="flex h-screen justify-center py-20 px-5 sm:px-0" v-if="pageLoaded">
    <div class="w-full text-center sm:w-[400px]">
      <div>
        <img src="/assets/logo-128.png" alt="Bag of Words" class="h-10 w-10 mx-auto" />
      </div>
      <h1 class="font-medium text-3xl mt-4 mb-5">Sign up</h1>
      <div class="px-10 py-6  border border-gray-200 rounded-xl shadow-sm bg-white">
        <form @submit.prevent='submit' v-if="authMode !== 'sso_only'">
          <div class="field block mt-3">
            <input placeholder="Name" id='name' v-model='name' class="border border-gray-300 rounded-lg px-4 py-2 w-full h-10 text-sm focus:outline-none focus:border-blue-500"/>
          </div>
          <div class="field mt-3">
            <input placeholder="Email" id='email' v-model='email' class="border border-gray-300 rounded-lg px-4 py-2 w-full h-10 text-sm focus:outline-none focus:border-blue-500"/>
          </div>
          <div class="field mt-3">
            <input type='password' placeholder="Password" id='password' v-model='password' class="border border-gray-300 rounded-lg px-4 py-2 w-full h-10 text-sm focus:outline-none focus:border-blue-500"/>
          </div>
          <p v-if="error_message" v-html="error_message" class="mt-1 text-red-500 text-sm whitespace-pre-line"></p>
          <div class="field mt-3">
            <button type='submit' class="px-3 py-2.5 mb-4 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 focus:ring-4 focus:outline-none focus:ring-blue-300 rounded-lg text-center dark:bg-blue-600 dark:hover:bg-blue-700 dark:focus:ring-blue-800 w-full">Sign up</button>
          </div>
        </form>

        <div class="mt-3" v-if="authMode !== 'local_only' && (googleSignIn || oidcProviders.length)">
          <div class="relative" v-if="authMode === 'hybrid'">
            <div class="absolute inset-0 flex items-center">
              <div class="w-full border-t border-gray-300"></div>
            </div>
            <div class="relative flex justify-center text-sm">
              <span class="px-2 bg-gray-50 text-gray-500">Or continue with</span>
            </div>
          </div>
          <div class="mt-3" v-if="googleSignIn">
            <button @click="signInWithGoogle" :disabled="loadingProvider !== null" class="w-full flex items-center justify-center px-4 py-2 border border-gray-300 rounded-lg shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed">
              <template v-if="loadingProvider === 'google'">
                <Spinner class="h-5 w-5 mr-2" />
                Redirecting...
              </template>
              <template v-else>
                <img src="/llm_providers_icons/google-icon.png" alt="Google logo" class="h-5 w-5 mr-2" />
                Sign up with Google
              </template>
            </button>
          </div>
          <div class="mt-3 space-y-2" v-if="oidcProviders.length">
            <button
              v-for="p in oidcProviders"
              :key="p.name"
              @click="() => signInWithProvider(p.name)"
              type="button"
              :disabled="loadingProvider !== null"
              class="w-full flex items-center justify-center px-4 py-2 border border-gray-300 rounded-lg shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <template v-if="loadingProvider === p.name">
                <Spinner class="h-5 w-5 mr-2" />
                Redirecting...
              </template>
              <template v-else>
                Continue with {{ p.name }}
              </template>
            </button>
          </div>
        </div>
      <div class="mt-3 block text-sm" v-if="authMode !== 'sso_only'">
        Already have an account?
        <NuxtLink to="/users/sign-in" class="text-blue-400 hover:text-blue-600">
          Login
        </NuxtLink>
      </div>
      </div>


      <div class="mt-3 block text-xs border-t border-gray-100 pt-3">
        By signing up, you agree to our 
        <a href="https://bagofwords.com/terms.html" target="_blank" class="text-blue-400">Terms of Service</a> and 
        <a href="https://bagofwords.com/privacy.html" target="_blank" class="text-blue-400">Privacy Policy</a>
      </div>
    </div>
  </div>
  <div v-else class="flex h-screen items-center justify-center"><Spinner class="h-6 w-6" /></div>
</template>

<script setup lang="ts">
import qs from 'qs'
import { ref, onMounted } from 'vue'
import Spinner from '~/components/Spinner.vue'
import { definePageMeta, useAuth, useRuntimeConfig, useRoute } from '#imports'
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
const error_message = ref('')

// Access runtime configuration
const config = useRuntimeConfig();
const googleSignIn = ref(config.public.googleSignIn);
const oidcProviders = ref<{ name: string; enabled: boolean }[]>([])
const loadingProvider = ref<string | null>(null)
const pageLoaded = ref(false)
const authMode = ref<'hybrid'|'local_only'|'sso_only'>('hybrid')

const { signIn, getSession } = useAuth();
const { ensureOrganization, fetchOrganization } = useOrganization()

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
  } catch (_) {}
  const inviteError = route.query.error as string
  if (inviteError) {
    error_message.value = inviteError
  }
  const emailFromQuery = route.query.email as string
  if (emailFromQuery) {
    email.value = emailFromQuery
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
    const session = await getSession()
    if (!session?.is_verified) {
      await verifyEmail(email)
      navigateTo('/users/verify');
    }
    else {
      navigateTo('/');
    }
    
  } catch (error) {
    console.error('Error during authentication:', error);
  }
}

async function submit() {
const payload = {
  name: name.value,
  email: email.value,
  password: password.value
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
    error_message.value = 'An error occurred during registration.'
    return
  }

  // Add automatic login after successful registration
  await signInWithCredentials(email.value, password.value)

} catch (error) {
  console.error('Error fetching data:', error);
  error_message.value = 'An error occurred during registration.'
}
}

async function signInWithGoogle() {
try {
  loadingProvider.value = 'google'
  const response = await $fetch('/api/auth/google/authorize', {
    method: 'GET',
  });
  
  if (response.authorization_url) {
    window.location.href = response.authorization_url;
  }
} catch (error) {
  error_message.value = 'Failed to initialize Google sign-in';
  loadingProvider.value = null
}
}

async function signInWithProvider(name: string) {
  try {
    loadingProvider.value = name
    const response = await $fetch(`/api/auth/${name}/authorize`, { method: 'GET' })
    if ((response as any)?.authorization_url) {
      window.location.href = (response as any).authorization_url
    }
  } catch (error) {
    error_message.value = `Failed to initialize ${name} sign-in`
    loadingProvider.value = null
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



