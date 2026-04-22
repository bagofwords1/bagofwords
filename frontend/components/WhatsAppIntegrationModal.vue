<template>
    <div class="p-4">
      <div class="flex items-center gap-2 mb-2">
        <img src="/icons/whatsapp.png" alt="WhatsApp" class="w-5 h-5" />
        <h1 class="text-lg font-semibold">WhatsApp Integration</h1>
      </div>
      <p class="text-sm text-gray-500">Configure and manage WhatsApp Cloud API integration for your organization</p>
      <hr class="my-4" />

      <div v-if="integrated" class="mb-4">
        <p class="text-green-600 mb-4">WhatsApp is currently connected.</p>

        <!-- Usage Notes -->
        <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-blue-800 mb-2">Usage Notes</h3>
          <ul class="text-sm text-blue-700 space-y-1 list-disc list-inside">
            <li>Only registered users can message the bot</li>
            <li>WhatsApp is always a 1:1 DM — <strong>public + private</strong> data sources (that the user has access to) are queried</li>
            <li>The bot can only reply within 24h of a user's last message (WhatsApp's customer service window)</li>
          </ul>
        </div>

        <!-- Integration Details -->
        <div class="bg-gray-50 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-gray-700 mb-3">Integration Details</h3>
          <div class="space-y-2 text-sm">
            <div class="flex justify-between">
              <span class="text-gray-600">Business Name:</span>
              <span class="font-medium">{{ integrationData?.platform_config?.verified_name || 'N/A' }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600">Phone Number:</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.display_phone_number || 'N/A' }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600">Phone Number ID:</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.phone_number_id || 'N/A' }}</span>
            </div>
            <div class="flex justify-between">
              <span class="text-gray-600">WABA ID:</span>
              <span class="font-mono text-xs">{{ integrationData?.platform_config?.waba_id || 'N/A' }}</span>
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

        <!-- Webhook Setup Info -->
        <div class="bg-yellow-50 border border-yellow-200 rounded-lg p-4 mb-4">
          <h3 class="text-sm font-medium text-yellow-800 mb-2">Webhook Setup</h3>
          <p class="text-xs text-yellow-700 mb-1">
            Configure your Meta app's webhook URL to:
          </p>
          <code class="block bg-white border border-yellow-200 rounded px-2 py-1 text-xs break-all">
            {{ webhookUrl }}
          </code>
          <p class="text-xs text-yellow-700 mt-2">
            Use the same <strong>Verify Token</strong> you entered during setup, and subscribe to the <code>messages</code> field on the WABA.
          </p>
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
            <label class="block text-sm font-medium mb-1">Access Token</label>
            <input v-model="accessToken" type="password" class="w-full border rounded px-2 py-1" required />
            <p class="text-xs text-gray-500 mt-1">System User access token with <code>whatsapp_business_messaging</code> and <code>whatsapp_business_management</code>.</p>
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">Phone Number ID</label>
            <input v-model="phoneNumberId" type="text" class="w-full border rounded px-2 py-1" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">WhatsApp Business Account ID (WABA ID)</label>
            <input v-model="wabaId" type="text" class="w-full border rounded px-2 py-1" required />
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">App Secret</label>
            <input v-model="appSecret" type="password" class="w-full border rounded px-2 py-1" required />
            <p class="text-xs text-gray-500 mt-1">Used to verify <code>X-Hub-Signature-256</code> on inbound webhooks.</p>
          </div>
          <div class="mb-4">
            <label class="block text-sm font-medium mb-1">Verify Token</label>
            <input v-model="verifyToken" type="text" class="w-full border rounded px-2 py-1" required />
            <p class="text-xs text-gray-500 mt-1">A string you pick — Meta sends it back during webhook verification.</p>
          </div>

          <div class="bg-gray-50 border border-gray-200 rounded-lg p-3 mb-4">
            <p class="text-xs text-gray-600 mb-1">After connecting, set your Meta app's webhook URL to:</p>
            <code class="block bg-white border border-gray-200 rounded px-2 py-1 text-xs break-all">
              {{ webhookUrl }}
            </code>
          </div>

          <button type="submit" class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md">Connect</button>
        </form>
      </div>
      <button class="absolute top-2 end-2 text-gray-400 hover:text-gray-600" @click="$emit('close')">✕</button>
    </div>
  </template>

  <script setup lang="ts">
  import { ref, computed } from 'vue'
  const props = defineProps<{
    integrated: boolean
    integrationData?: any
  }>()
  const emit = defineEmits(['close', 'updated'])
  const toast = useToast()

  const accessToken = ref('')
  const phoneNumberId = ref('')
  const wabaId = ref('')
  const appSecret = ref('')
  const verifyToken = ref('')

  const webhookUrl = computed(() => {
    if (typeof window !== 'undefined') {
      return `${window.location.origin}/api/settings/integrations/whatsapp/webhook`
    }
    return '/api/settings/integrations/whatsapp/webhook'
  })

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
      const res = await useMyFetch('/api/settings/integrations/whatsapp', {
        method: 'POST',
        body: {
          access_token: accessToken.value,
          phone_number_id: phoneNumberId.value,
          waba_id: wabaId.value,
          app_secret: appSecret.value,
          verify_token: verifyToken.value,
        }
      })
      if (res.status.value === 'success') {
        toast.add({
          title: 'WhatsApp connected',
          description: 'WhatsApp integration successful',
          color: 'green'
        })
        emit('updated')
        emit('close')
      } else {
        toast.add({
          title: 'Failed to connect WhatsApp',
          description: (res.error.value as any).data?.detail || (res.error.value as any).message,
          color: 'red'
        })
    }
  }

  async function disconnect() {
    if (!props.integrationData?.id) return
    const res = await useMyFetch(`/api/settings/integrations/${props.integrationData.id}`, {
      method: 'DELETE'
    })
    if (res.status.value === 'success') {
      toast.add({
        title: 'WhatsApp disconnected',
        description: 'WhatsApp integration disconnected',
        color: 'green'
      })
      emit('updated')
      emit('close')
    } else {
      toast.add({
        title: 'Failed to disconnect WhatsApp',
        description: (res.error.value as any).data?.detail || (res.error.value as any).message,
        color: 'red'
      })
    }
  }
  </script>
