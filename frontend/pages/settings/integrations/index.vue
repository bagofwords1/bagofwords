<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900">
      Integrations
      <p class="text-sm text-gray-500 font-normal mb-8">
        Configure external platforms like Slack and Microsoft Teams for your organization.
      </p>
    </h2>
  </div>
  <div class="mt-6 space-y-4">
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

    <!-- Teams Integration Row -->
    <div
      class="flex items-center justify-between p-4 border rounded-lg cursor-pointer hover:bg-gray-50"
      @click="showTeamsModal = true"
    >
      <div class="flex items-center">
        <img src="/icons/teams.png" alt="Teams" class="w-8 h-8 mr-4" />
        <div>
          <div class="font-medium">Microsoft Teams</div>
          <div class="text-sm text-gray-500">
            <span v-if="teamsIntegrated">
                <span class="text-green-600">Connected</span>
            </span>
            <span v-else>
                <span class="text-gray-400">Not connected</span>
            </span>
          </div>
          <div v-if="teamsConfig && teamsIntegrated" class="text-xs text-gray-400 mt-1">
            <span>Tenant: {{ teamsConfig.tenant_id }}</span>
          </div>
        </div>
      </div>
      <button
        class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
        @click.stop="showTeamsModal = true"
      >
        {{ teamsIntegrated ? 'Settings' : 'Integrate' }}
      </button>
    </div>

    <!-- MCP Integration Row -->
    <div
      class="flex items-center justify-between p-4 border rounded-lg"
    >
      <div class="flex items-center">
        <McpIcon class="w-8 h-8 mr-4 text-gray-700" />
        <div>
          <div class="font-medium">BOW MCP Server</div>
          <div class="text-sm text-gray-500">
            <span>
              Enable MCP endpoint for integration with AI assistants like Cursor, Claude, or others
            </span>
          </div>
        </div>
      </div>
      <UToggle
        v-model="mcpEnabled"
        :loading="mcpUpdating"
        @update:model-value="toggleMcp"
      />
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

    <!-- Teams Integration Modal -->
    <UModal v-model="showTeamsModal" :ui="{ width: 'max-w-lg' }">
      <TeamsIntegrationModal
        :integrated="teamsIntegrated"
        :integration-data="teamsIntegrationData"
        @close="showTeamsModal = false"
        @updated="fetchIntegrations"
      />
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import SlackIntegrationModal from '~/components/SlackIntegrationModal.vue'
import TeamsIntegrationModal from '~/components/TeamsIntegrationModal.vue'
import McpIcon from '~/components/icons/McpIcon.vue'

definePageMeta({ auth: true, permissions: ['manage_organization_external_platforms'], layout: 'settings' })

const showSlackModal = ref(false)
const slackIntegrated = ref(false)
const slackConfig = ref<{ team_id?: string; team_name?: string } | null>(null)
const slackIntegrationData = ref<any>(null)

const showTeamsModal = ref(false)
const teamsIntegrated = ref(false)
const teamsConfig = ref<{ tenant_id?: string; app_id?: string } | null>(null)
const teamsIntegrationData = ref<any>(null)

// MCP state
const mcpEnabled = ref(false)
const mcpUpdating = ref(false)

const { settings, fetchSettings } = useOrgSettings()

async function fetchIntegrations() {
  const res = await useMyFetch('/api/settings/integrations')
  const integrations = res.data.value || []

  const slack = integrations.find((i: any) => i.platform_type === 'slack' && i.is_active)
  slackIntegrated.value = !!slack
  slackConfig.value = slack?.platform_config || null
  slackIntegrationData.value = slack || null

  const teams = integrations.find((i: any) => i.platform_type === 'teams' && i.is_active)
  teamsIntegrated.value = !!teams
  teamsConfig.value = teams?.platform_config || null
  teamsIntegrationData.value = teams || null
}

async function loadMcpState() {
  await fetchSettings()
  const mcpFeature = settings.value?.config?.mcp_enabled
  if (mcpFeature) {
    mcpEnabled.value = mcpFeature.state === 'enabled' || mcpFeature.value === true
  }
}

async function toggleMcp(value: boolean) {
  mcpUpdating.value = true
  try {
    await useMyFetch('/api/organization/settings', {
      method: 'PUT',
      body: JSON.stringify({
        config: {
          mcp_enabled: {
            value: value,
            state: value ? 'enabled' : 'disabled'
          }
        }
      })
    })
    await fetchSettings()
  } catch (e) {
    // Revert on error
    mcpEnabled.value = !value
  } finally {
    mcpUpdating.value = false
  }
}

onMounted(() => {
  fetchIntegrations()
  loadMcpState()
})
</script>
