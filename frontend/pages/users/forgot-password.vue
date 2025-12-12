<template>
  <div class="flex h-screen justify-center py-20 px-5 sm:px-0" v-if="pageLoaded">
    <div class="w-full sm:w-1/4">
      <template v-if="!smtpEnabled">
        <div class="text-center">
          <Icon name="heroicons:exclamation-triangle" class="w-10 h-10 text-yellow-500 mx-auto mb-3" />
          <h1 class="font-bold text-lg">Password Reset Unavailable</h1>
          <p class="mt-3 text-sm text-gray-700">
            Email is not configured on this instance. Please contact your administrator to reset your password.
          </p>
          <div class="mt-5">
            <NuxtLink to="/users/sign-in" class="text-blue-400 hover:text-blue-600">
              Back to Sign in
            </NuxtLink>
          </div>
        </div>
      </template>
      <template v-else-if="!emailSent" class="bg-white">
        <h1 class="font-bold text-lg">Forgot Password</h1>
        <p class="mt-3 text-sm text-gray-700">
          Enter your email address and we'll send you a link to reset your password.
        </p>
        <form @submit.prevent="submit">
          <div class="field mt-3">
            <input 
              placeholder="Email" 
              id="email" 
              v-model="email" 
              type="email"
              class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"
              required
            />
          </div>
          <p v-if="error_message" class="mt-1 text-red-500 text-sm text-center">{{ error_message }}</p>
          <p v-if="success_message" class="mt-1 text-green-500 text-sm text-center">{{ success_message }}</p>
          <div class="field mt-3">
            <button 
              type="submit" 
              :disabled="isLoading"
              class="px-3 py-2 text-sm font-medium text-white bg-blue-700 hover:bg-blue-800 focus:ring-4 focus:outline-none focus:ring-blue-300 rounded-lg text-center disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {{ isLoading ? 'Sending...' : 'Send Reset Link' }}
            </button>
          </div>
        </form>
        <div class="mt-3 block text-sm text-center">
          Remember your password? 
          <NuxtLink to="/users/sign-in" class="text-blue-400">
            Sign in
          </NuxtLink>
        </div>
      </template>
      <template v-else>
        <div class="mt-8 text-center">
          <Icon name="heroicons:envelope" class="w-10 h-10 text-green-500 mx-auto mb-3" />
          <h2 class="font-bold text-lg">Check your email</h2>
          <p class="mt-3 text-sm text-gray-700">
            We've sent a password reset link to <strong>{{ email }}</strong><br /><br />
            Click the link in the email to reset your password.
          </p>
        </div>
      </template>
    </div>
  </div>
  <div v-else class="flex h-screen items-center justify-center"><Spinner class="h-6 w-6" /></div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { definePageMeta } from '#imports'
import Spinner from '~/components/Spinner.vue'

definePageMeta({
  auth: {
    unauthenticatedOnly: true,
    navigateAuthenticatedTo: '/'
  },
  layout: 'users'
})

const email = ref('')
const error_message = ref('')
const success_message = ref('')
const isLoading = ref(false)
const emailSent = ref(false)
const smtpEnabled = ref(false)
const pageLoaded = ref(false)

onMounted(async () => {
  try {
    const settings = await $fetch('/api/settings')
    smtpEnabled.value = settings?.smtp_enabled ?? false
  } catch (_) {}
  pageLoaded.value = true
})

async function submit() {
  if (!email.value) {
    error_message.value = 'Please enter your email address'
    return
  }

  isLoading.value = true
  error_message.value = ''
  success_message.value = ''

  try {
    const response = await $fetch('/api/auth/forgot-password', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        email: email.value
      }),
    })

    emailSent.value = true
    success_message.value = 'Password reset link sent successfully!'
  } catch (error: any) {
    console.error('Error requesting password reset:', error)
    
    if (error.data?.detail) {
      error_message.value = error.data.detail
    } else if (error.status === 404) {
      error_message.value = 'No account found with this email address'
    } else {
      error_message.value = 'An error occurred while sending the reset link. Please try again.'
    }
  } finally {
    isLoading.value = false
  }
}
</script>
