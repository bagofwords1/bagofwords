<template>
    <div class="flex h-screen justify-center py-20 px-5 sm:px-0" v-if="pageLoaded">
    <div class="w-full text-center sm:w-[400px]">
      <div>
        <img src="/assets/logo-128.png" alt="Bag of Words" class="h-10 w-10 mx-auto" />
      </div>
      <h1 class="font-medium text-3xl mt-4 mb-5">Login</h1>
      <div class="px-10 py-6  border border-gray-200 rounded-xl shadow-sm bg-white">
      <form @submit.prevent="signInWithCredentials()" v-if="authMode !== 'sso_only'">
        <div class="field block mt-3">
          <i class="i-heroicons-user"></i>
          <input type="text"
          placeholder="Email"
          id='email'
          v-model='email'
          class="border border-gray-300 rounded-lg px-4 py-2 w-full h-10 text-sm focus:outline-none focus:border-blue-500"
          />
        </div>
          <div class="field mt-4">
          <input type='password'
          placeholder="Password"
          id='password'
          v-model='password'
          class="border border-gray-300 rounded-lg px-4 py-2 w-full h-10 text-sm focus:outline-none focus:border-blue-500"
          />
          <p v-if="error_message" class="mt-1 text-red-500 text-sm">{{ error_message }}</p>
        </div>
        
        <div class="field mt-2 text-left">
          <NuxtLink to="/users/forgot-password" class="text-xs text-blue-400 hover:text-blue-600">
            Forgot Password?
          </NuxtLink>
        </div>
        
        <div class="field mt-3">
          <button type='submit' class="px-3 py-2.5 mb-4 text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 focus:ring-4 focus:outline-none focus:ring-blue-300 rounded-lg text-center dark:bg-blue-600 dark:hover:bg-blue-700 dark:focus:ring-blue-800 w-full">Login</button>
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
          <button
            @click="signInWithGoogle"
            type="button"
            :disabled="loadingProvider !== null"
            class="w-full flex items-center justify-center px-4 py-2 border border-gray-300 rounded-lg shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <template v-if="loadingProvider === 'google'">
              <Spinner class="h-5 w-5 mr-2" />
              Redirecting...
            </template>
            <template v-else>
              <img src="/llm_providers_icons/google-icon.png" alt="Google logo" class="h-5 w-5 mr-2" />
              Sign in with Google
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
              Sign in with {{ p.name }}
            </template>
          </button>
        </div>
      </div>


        <div class="mt-3 block text-sm" v-if="authMode !== 'sso_only'">
      New to Bag of words?
       <NuxtLink to="/users/sign-up" class="text-blue-400 hover:text-blue-600">
        Sign up
      </NuxtLink>
    </div>
    </div>
    </div>
  </div>
  <div v-else class="flex h-screen items-center justify-center"><Spinner class="h-6 w-6" /></div>
  </template>
  
  
  <script setup lang="ts">
  
  import qs from 'qs';
  
  import { ref, onMounted } from 'vue';
  import Spinner from '~/components/Spinner.vue';
  
  const { rawToken } = useAuthState()
  const { fetchOrganization } = useOrganization()
  const route = useRoute()
  const config = useRuntimeConfig();
  const googleSignIn = ref(config.public.googleSignIn);
  const oidcProviders = ref<{ name: string; enabled: boolean }[]>([])
  const loadingProvider = ref<string | null>(null)
  const authMode = ref<'hybrid'|'local_only'|'sso_only'>('hybrid')

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
    } catch (_) {}
    const inviteError = route.query.error as string
    if (inviteError) {
      error_message.value = inviteError
    }
    const access_token = route.query.access_token as string
    const userEmail = route.query.email as string
    if (access_token) {
      rawToken.value = access_token
      await getSession()
      navigateTo('/')
      return
    }
    pageLoaded.value = true
  })

  
  async function signInWithCredentials() {
    const route = useRoute();
    const redirectedFrom = route.query.redirect
    
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
        await getSession()
        
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
        error_message.value = 'Invalid credentials'
      }
    } catch (error) {
      error_message.value = 'Invalid credentials'
    }
  }

  // Add new function for Google sign-in
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
  </script>
