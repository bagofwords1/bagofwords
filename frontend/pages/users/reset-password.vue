<template>
  <div class="flex h-screen justify-center py-20 px-5 sm:px-0">
    <div class="w-full sm:w-1/4">
      <template v-if="!passwordReset">
        <h1 class="font-bold text-lg">{{ $t('auth.resetPasswordTitle') }}</h1>
        <p class="mt-3 text-sm text-gray-700">
          {{ $t('auth.resetPasswordDescription') }}
        </p>
        <form @submit.prevent="submit">
          <div class="field mt-3">
            <input
              :placeholder="$t('auth.newPassword')"
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
              :placeholder="$t('auth.confirmPassword')"
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
              {{ isLoading ? $t('auth.resetting') : $t('auth.resetPasswordButton') }}
            </button>
          </div>
        </form>
        <div class="mt-3 block text-sm text-center">
          {{ $t('auth.rememberPassword') }}
          <NuxtLink to="/users/sign-in" class="text-blue-400">
            {{ $t('auth.signIn') }}
          </NuxtLink>
        </div>
      </template>
      <template v-else>
        <div class="mt-8 text-center">
          <Icon name="heroicons:check-circle" class="w-10 h-10 text-green-500 mx-auto mb-3" />
          <h2 class="font-bold text-lg">{{ $t('auth.resetSuccess') }}</h2>
          <p class="mt-3 text-sm text-gray-700">
            {{ $t('auth.resetSuccessMessage') }}
          </p>
          <NuxtLink to="/users/sign-in" class="text-blue-400 mt-4 block text-center text-sm hover:underline">
            {{ $t('auth.signIn') }}
          </NuxtLink>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { definePageMeta, useRoute } from '#imports'

const { t } = useI18n()

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
    error_message.value = t('auth.invalidResetLink')
    return
  }
  token.value = tokenFromQuery
})

async function submit() {
  if (!token.value) {
    error_message.value = t('auth.invalidResetLink')
    return
  }

  if (password.value !== confirmPassword.value) {
    error_message.value = t('auth.passwordsDoNotMatch')
    return
  }

  if (password.value.length < 6) {
    error_message.value = t('auth.passwordTooShort')
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
      error_message.value = t('auth.invalidResetToken')
    } else {
      error_message.value = t('auth.resetFailed')
    }
  } finally {
    isLoading.value = false
  }
}
</script>
