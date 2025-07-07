<template>
  <div class="flex h-screen justify-center py-20 px-5 sm:px-0">
    <div class="w-full sm:w-1/4">
      <template v-if="!passwordReset">
        <h1 class="font-bold text-lg">Reset Password</h1>
        <p class="mt-3 text-sm text-gray-700">
          Enter your new password below.
        </p>
        <form @submit.prevent="submit">
          <div class="field mt-3">
            <input 
              placeholder="New Password" 
              id="password" 
              v-model="password" 
              type="password"
              class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"
              required
              minlength="6"
            />
          </div>
          <div class="field mt-3">
            <input 
              placeholder="Confirm New Password" 
              id="confirmPassword" 
              v-model="confirmPassword" 
              type="password"
              class="border border-gray-300 rounded-lg px-4 py-2 w-full h-9 text-sm focus:outline-none focus:border-blue-500"
              required
              minlength="6"
            />
          </div>
          <p v-if="error_message" class="mt-1 text-red-500 text-sm text-center">{{ error_message }}</p>
          <div class="field mt-3">
            <button 
              type="submit" 
              :disabled="isLoading || !isValid"
              class="px-3 py-2 text-sm font-medium text-white bg-blue-700 hover:bg-blue-800 focus:ring-4 focus:outline-none focus:ring-blue-300 rounded-lg text-center disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {{ isLoading ? 'Resetting...' : 'Reset Password' }}
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
          <Icon name="heroicons:check-circle" class="w-10 h-10 text-green-500 mx-auto mb-3" />
          <h2 class="font-bold text-lg">Password Reset Successfully</h2>
          <p class="mt-3 text-sm text-gray-700">
            Your password has been updated. You can now sign in with your new password.
          </p>
          <NuxtLink to="/users/sign-in" class="text-blue-400 mt-4 block text-center text-sm hover:underline">
            Sign in
          </NuxtLink>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { definePageMeta, useRoute } from '#imports'

definePageMeta({
  auth: {
    unauthenticatedOnly: true,
    navigateAuthenticatedTo: '/'
  },
  layout: 'users'
})

const route = useRoute()
const password = ref('')
const confirmPassword = ref('')
const error_message = ref('')
const isLoading = ref(false)
const passwordReset = ref(false)
const token = ref('')

const isValid = computed(() => {
  return password.value.length >= 6 && password.value === confirmPassword.value
})

onMounted(() => {
  const tokenFromQuery = route.query.token as string
  if (!tokenFromQuery) {
    error_message.value = 'Invalid reset link. Please request a new password reset.'
    return
  }
  token.value = tokenFromQuery
})

async function submit() {
  if (!token.value) {
    error_message.value = 'Invalid reset link. Please request a new password reset.'
    return
  }

  if (password.value !== confirmPassword.value) {
    error_message.value = 'Passwords do not match'
    return
  }

  if (password.value.length < 6) {
    error_message.value = 'Password must be at least 6 characters long'
    return
  }

  isLoading.value = true
  error_message.value = ''

  try {
    const response = await $fetch('/api/auth/reset-password', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        token: token.value,
        password: password.value
      }),
    })

    passwordReset.value = true
  } catch (error: any) {
    console.error('Error resetting password:', error)
    
    if (error.data?.detail) {
      error_message.value = error.data.detail
    } else if (error.status === 400) {
      error_message.value = 'Invalid or expired reset token. Please request a new password reset.'
    } else {
      error_message.value = 'An error occurred while resetting your password. Please try again.'
    }
  } finally {
    isLoading.value = false
  }
}
</script> 