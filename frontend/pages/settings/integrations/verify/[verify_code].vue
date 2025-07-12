<template>
  <div class="flex justify-center py-40 px-5 sm:px-0">
    <div class="w-full sm:w-1/3 bg-white rounded-lg shadow p-8 flex flex-col items-center">
      <div class="mb-4">
        <!-- Slack icon, you can use an SVG or an <img> if you have one -->
        <img src="/icons/slack.png" alt="Slack" class="w-12 h-12" />
      </div>
      <h1 class="font-bold text-lg mb-2">Verify your Slack account</h1>
      <p class="mt-3 text-sm text-gray-700 text-center mb-6">
        Please follow the instructions below to activate your Slack integration.
      </p>
      <div v-if="loading" class="text-blue-500 font-medium">Verifying...</div>
      <div v-else-if="success && alreadyVerified" class="text-blue-600 font-medium text-center">
        <Icon name="heroicons:information-circle" class="inline w-6 h-6 mr-1" />
        {{ message || 'Your account is already verified.' }}
      </div>
      <div v-else-if="success" class="text-green-600 font-medium text-center">
        <Icon name="heroicons:check-circle" class="inline w-6 h-6 mr-1" />
        Your account has been verified! You can now use Slack integration.
      </div>
      <div v-else-if="error" class="text-red-600 font-medium text-center">
        <Icon name="heroicons:x-circle" class="inline w-6 h-6 mr-1" />
        Verification failed: {{ error }}
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'

definePageMeta({ auth: true, permissions: ['create_reports'], layout: 'default' })

const route = useRoute()
const verifyCode = route.params.verify_code

const loading = ref(true)
const success = ref(false)
const alreadyVerified = ref(false)
const message = ref('')
const error = ref('')

const verify = async () => {
  loading.value = true
  error.value = ''
  success.value = false
  alreadyVerified.value = false
  message.value = ''
  try {
    const res = await useMyFetch(`/api/settings/integrations/verify/${verifyCode}/complete`, {
      method: 'POST'
    })
    if (res.data.value && res.data.value.success) {
      success.value = true
      alreadyVerified.value = !!res.data.value.already_verified
      message.value = res.data.value.message || ''
    } else {
      error.value = res.data.value?.error || 'Unknown error'
    }
  } catch (e) {
    error.value = e?.message || 'Unknown error'
  } finally {
    loading.value = false
  }
}

onMounted(verify)
</script>

<style scoped>
.text-green-600 { color: #16a34a; }
.text-red-600 { color: #dc2626; }
.text-blue-600 { color: #2563eb; }
</style>
