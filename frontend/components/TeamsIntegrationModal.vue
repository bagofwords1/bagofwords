<template>
    <div class="p-4">
      <div class="flex items-center gap-2 mb-2">
        <img src="/icons/teams.png" alt="Teams" class="w-5 h-5" />
        <h1 class="text-lg font-semibold">Teams Integration</h1>
      </div>
      <p class="text-sm text-gray-500">Configure and manage Microsoft Teams integration for your organization</p>
      <hr class="my-4" />

      <div v-if="integrated" class="mb-4">
        <p class="text-green-600 mb-4">Teams is currently connected.</p>

        <!-- Usage Notes -->
        <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-blue-800 mb-2">Usage Notes</h3>
          <ul class="text-sm text-blue-700 space-y-1 list-disc list-inside">
            <li>Only registered users can message or @mention the bot</li>
            <li>In channels/group chats, only <strong>public</strong> data sources are queried</li>
            <li>In private chats, <strong>public + private</strong> data sources (that the user has access to) are queried</li>
          </ul>
        </div>

        <!-- Integration Details -->
        <div class="bg-gray-50 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 mb-3">Integration Details</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between">
              <span class="text-gray-600">Tenant ID:</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.tenant_id || 'N/A' }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600">App ID:</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.app_id || 'N/A' }}</span>
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
            <label class="block text-sm font-medium mb-1">App ID</label>
            <input v-model="appId" type="text" class="w-full border rounded px-2 py-1" placeholder="Microsoft App ID" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">Client Secret</label>
            <input v-model="clientSecret" type="password" class="w-full border rounded px-2 py-1" placeholder="Microsoft App Password" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">Tenant ID</label>
            <input v-model="tenantId" type="text" class="w-full border rounded px-2 py-1" placeholder="Azure AD Tenant ID" required />
          </div>
          <button type="submit" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md">Connect</button>
        </form>
      </div>
      <button class="absolute top-2 right-2 text-gray-400 hover:text-gray-600" @click="$emit('close')">&#x2715;</button>
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

  const appId = ref('')
  const clientSecret = ref('')
  const tenantId = ref('')

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
      const res = await useMyFetch('/api/settings/integrations/teams', {
        method: 'POST',
        body: { app_id: appId.value, client_secret: clientSecret.value, tenant_id: tenantId.value }
      })
      if (res.status.value === 'success') {
        toast.add({
          title: 'Teams connected',
          description: 'Microsoft Teams integration successful',
          color: 'green'
        })
        emit('updated')
        emit('close')
      } else {
        toast.add({
        title: 'Failed to connect Teams',
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }

  async function disconnect() {
    const res = await useMyFetch(`/api/settings/integrations/${props.integrationData?.id}`, {
      method: 'DELETE'
    })
    if (res.status.value === 'success') {
      toast.add({
        title: 'Teams disconnected',
        description: 'Microsoft Teams integration disconnected',
        color: 'green'
      })
      emit('updated')
      emit('close')
    } else {
      toast.add({
        title: 'Failed to disconnect Teams',
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }
  </script>
