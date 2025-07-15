<template>
    <div class="flex h-screen justify-center py-20 px-5 sm:px-0" v-if="pageLoaded">
    <div class="w-full sm:w-1/4">
      <h1 class="font-bold text-lg">Login</h1>
      <form @submit.prevent="signInWithCredentials()">
        <div class="field block mt-3">
          <i class="i-heroicons-user"></i>
          <input type="text"
          placeholder="Email"
          id='email'
          v-model='email'
          class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"
          />
        </div>
          <div class="field mt-3">
          <input type='password'
          placeholder="Password"
          id='password'
          v-model='password'
          class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"
          />
          <p v-if="error_message" class="mt-1 text-red-500 text-sm">{{ error_message }}</p>
        </div>
        
        <div class="field mt-2 text-right">
          <NuxtLink to="/users/forgot-password" class="text-sm text-blue-400 hover:text-blue-600">
            Forgot Password?
          </NuxtLink>
        </div>
        
        <div class="field mt-3">
          <button type='submit' class="px-3 py-2 text-sm font-medium text-white bg-blue-700 hover:bg-blue-800 focus:ring-4 focus:outline-none focus:ring-blue-300 rounded-lg text-center dark:bg-blue-600 dark:hover:bg-blue-700 dark:focus:ring-blue-800">Login</button>
        </div>
      </form>

        <div class="mt-3" v-if="googleSignIn">
        <div class="relative">
          <div class="absolute inset-0 flex items-center">
            <div class="w-full border-t border-gray-300"></div>
          </div>
          <div class="relative flex justify-center text-sm">
            <span class="px-2 bg-white text-gray-500">Or continue with</span>
          </div>
        </div>
        <div class="mt-3" v-if="googleSignIn">
          <button
            @click="signInWithGoogle"
            type="button"
            class="w-full flex items-center justify-center px-4 py-2 border border-gray-300 rounded-lg shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50"
          >
            <img src="/llm_providers_icons/google.png" alt="Google logo" class="h-5 w-5 mr-2" />
            Sign in with Google
          </button>
        </div>
      </div>


        <div class="mt-3 block text-sm">
      New to Bag of words?
       <NuxtLink to="/users/sign-up" class="text-blue-400">
        Create an account
      </NuxtLink>
    </div>
    </div>
  </div>
  </template>
  
  
  <script setup lang="ts">
  
  import qs from 'qs';
  
  import { ref, onMounted } from 'vue';
  
  const { rawToken } = useAuthState()
  const { fetchOrganization } = useOrganization()
  const route = useRoute()
  const config = useRuntimeConfig();
  const googleSignIn = ref(config.public.googleSignIn);

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
    const access_token = route.query.access_token as string
    const userEmail = route.query.email as string
    if (access_token) {
      rawToken.value = access_token
      await getSession()
      navigateTo('/')
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
      const response = await $fetch('/api/auth/google/authorize', {
        method: 'GET',
      });
      
      if (response.authorization_url) {
        window.location.href = response.authorization_url;
      }
    } catch (error) {
      error_message.value = 'Failed to initialize Google sign-in';
    }
  }
  </script>
