<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900">
      Integrations
      <p class="text-sm text-gray-500 font-normal mb-8">
        Configure external platforms like Slack for your organization.
      </p>
    </h2>
  </div>
  <div class="mt-6">
    <!-- Slack Integration Row -->
    <div
      class="flex items-center justify-between p-4 border rounded-lg cursor-pointer hover:bg-gray-50"
      @click="showSlackModal = true"
    >
      <div class="flex items-center">
        <img src="/icons/slack.png" alt="Slack" class="w-8 h-8 mr-4" />
        <div>
          <div class="font-medium">Slack</div>
          <div class="text-sm text-gray-500">
            <span v-if="slackIntegrated">
                <span class="text-green-600">Connected</span>
            </span>
            <span v-else>
                <span class="text-gray-400">Not connected</span>
            </span>
          </div>
          <div v-if="slackConfig && slackIntegrated" class="text-xs text-gray-400 mt-1">
            <span>Workspace: {{ slackConfig.team_name }}</span>
            <span class="ml-2">ID: {{ slackConfig.team_id }}</span>
          </div>
        </div>
      </div>
      <button
        class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
        @click.stop="showSlackModal = true"
      >
        {{ slackIntegrated ? 'Settings' : 'Integrate' }}
      </button>
    </div>

    <!-- Slack Integration Modal -->
    <UModal v-model="showSlackModal" :ui="{ width: 'max-w-lg' }">
      <SlackIntegrationModal
        :integrated="slackIntegrated"
        :integration-data="slackIntegrationData"
        @close="showSlackModal = false"
        @updated="fetchIntegrations"
      />
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import SlackIntegrationModal from '~/components/SlackIntegrationModal.vue'

definePageMeta({ auth: true, permissions: ['manage_organization_external_platforms'], layout: 'settings' })

const showSlackModal = ref(false)
const slackIntegrated = ref(false)
const slackConfig = ref<{ team_id?: string; team_name?: string } | null>(null)
const slackIntegrationData = ref<any>(null)

async function fetchIntegrations() {
  // Replace with your actual API call
  const res = await useMyFetch('/api/settings/integrations')
  const slack = res.data.value?.find((i: any) => i.platform_type === 'slack' && i.is_active)
  slackIntegrated.value = !!slack
  slackConfig.value = slack?.platform_config || null
  slackIntegrationData.value = slack || null
}

onMounted(fetchIntegrations)
</script>
