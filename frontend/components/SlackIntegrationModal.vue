<template>
    <div class="p-4">
      <div class="flex items-center gap-2 mb-2">
        <img src="/icons/slack.png" alt="Slack" class="w-5 h-5" />
        <h1 class="text-lg font-semibold">Slack Integration</h1>
      </div>
      <p class="text-sm text-gray-500">Configure and manage Slack integration for your organization</p>
      <hr class="my-4" />
      
      <div v-if="integrated" class="mb-4">
        <p class="text-green-600 mb-4">Slack is currently connected.</p>
        
        <!-- Integration Details -->
        <div class="bg-gray-50 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 mb-3">Integration Details</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between">
              <span class="text-gray-600">Workspace Name:</span>
              <span class="font-medium">{{ integrationData?.platform_config?.team_name || 'N/A' }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600">Workspace ID:</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.team_id || 'N/A' }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600">Base URL:</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.base_url || 'N/A' }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600">Connected:</span>
              <span class="font-medium">{{ formatDate(integrationData?.created_at) }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600">Last Updated:</span>
              <span class="font-medium">{{ formatDate(integrationData?.updated_at) }}</span>
            </div>
          </div>
        </div>
        
        <UButton 
          color="red" 
          variant="soft"
          @click="disconnect"
        >
          Disconnect
        </UButton>
      </div>
      <div v-else>
        <form @submit.prevent="connect">
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">Bot Token</label>
            <input v-model="botToken" type="text" class="w-full border rounded px-2 py-1" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">Signing Secret</label>
            <input v-model="signingSecret" type="text" class="w-full border rounded px-2 py-1" required />
          </div>
          <button type="submit" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md">Connect</button>
        </form>
      </div>
      <button class="absolute top-2 right-2 text-gray-400 hover:text-gray-600" @click="$emit('close')">✕</button>
    </div>
  </template>
  
  <script setup lang="ts">
  import { ref } from 'vue'
  const props = defineProps<{ 
    integrated: boolean
    integrationData?: any 
  }>()
  const emit = defineEmits(['close', 'updated'])
  const toast = useToast()
  
  const botToken = ref('')
  const signingSecret = ref('')
  
  function formatDate(dateString: string | undefined) {
    if (!dateString) return 'N/A'
    return new Date(dateString).toLocaleDateString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    })
  }
  
  async function connect() {
      const res = await useMyFetch('/api/settings/integrations/slack', {
        method: 'POST',
        body: { bot_token: botToken.value, signing_secret: signingSecret.value }
      })
      if (res.status.value === 'success') {
        toast.add({
          title: 'Slack connected',
          description: 'Slack integration successful',
          color: 'green'
        })
        emit('updated')
        emit('close')
      } else {
        toast.add({
        title: 'Failed to connect Slack',
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }
  
  async function disconnect() {
    const res = await useMyFetch('/api/settings/integrations/slack', {
      method: 'DELETE'
    })
    if (res.status.value === 'success') {
      toast.add({
        title: 'Slack disconnected',
        description: 'Slack integration disconnected',
        color: 'green'
      })
      emit('updated')
      emit('close')
    } else {
      toast.add({
        title: 'Failed to disconnect Slack',
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }
  </script>