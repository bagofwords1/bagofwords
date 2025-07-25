<template>
  <div class="flex h-screen justify-center py-20 px-5 sm:px-0">
<div class="w-full sm:w-1/4">
  <h1 class="font-bold text-lg">Sign up</h1>
  <form @submit.prevent='submit'>
      <div class="field block mt-3">
          <input placeholder="Name" id='name' v-model='name' class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"/>
      </div>

    <div class="field mt-3">
      <input placeholder="Email" id='email' v-model='email' class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"/>
    </div>
    <div class="field mt-3">
      <input type='password' placeholder="Password" id='password' v-model='password' class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"/>
    </div>
    <p v-if="error_message" v-html="error_message" class="mt-1 text-red-500 text-sm whitespace-pre-line"></p>
    <div class="field mt-3">
        <button type='submit' class="px-3 py-2 text-sm font-medium text-white bg-blue-700 hover:bg-blue-800 focus:ring-4 focus:outline-none focus:ring-blue-300 rounded-lg text-center dark:bg-blue-600 dark:hover:bg-blue-700 dark:focus:ring-blue-800">Sign up</button>
    </div>
  </form>
  <!-- Google sign in -->

  <div class="mt-3" v-if="googleSignIn">
    <div class="relative">
      <div class="absolute inset-0 flex items-center">
        <div class="w-full border-t border-gray-300"></div>
      </div>
      <div class="relative flex justify-center text-sm">
        <span class="px-2 bg-white text-gray-500">Or continue with</span>
      </div>
    </div>
    <div class="mt-3">  
      <button @click="signInWithGoogle" class="w-full flex items-center justify-center px-4 py-2 border border-gray-300 rounded-lg shadow-sm text-sm font-medium text-gray-700 bg-white hover:bg-gray-50">
        <img src="/llm_providers_icons/google.png" alt="Google logo" class="h-5 w-5 mr-2" />
        Sign up with Google
      </button>
    </div>
  </div>

  <div class="mt-3 block text-sm">
  Already have an account? 
   <NuxtLink to="/users/sign-in" class="text-blue-400">
    Login
  </NuxtLink>
</div>
<div class="mt-3 block text-xs border-t border-gray-100 pt-3">
  By signing up, you agree to our 
  <a href="https://bagofwords.com/terms.html" target="_blank" class="text-blue-400">Terms of Service</a> and 
  <a href="https://bagofwords.com/privacy.html" target="_blank" class="text-blue-400">Privacy Policy</a>
</div>
</div>
</div>
</template>

<script setup lang="ts">
import qs from 'qs'
import { ref, onMounted } from 'vue'
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

const { signIn, getSession } = useAuth();
const { ensureOrganization, fetchOrganization } = useOrganization()

// Pre-fill email from URL query parameter
onMounted(() => {
  const emailFromQuery = route.query.email as string
  if (emailFromQuery) {
    email.value = emailFromQuery
  }
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



