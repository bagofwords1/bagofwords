<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900">
      {{ $t('settings.integrations.title') }}
      <p class="text-sm text-gray-500 font-normal mb-8">
        {{ $t('settings.integrations.subtitle') }}
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
        <img src="/icons/slack.png" alt="Slack" class="w-8 h-8 me-4" />
        <div>
          <div class="font-medium">Slack</div>
          <div class="text-sm text-gray-500">
            <span v-if="slackIntegrated">
                <span class="text-green-600">{{ $t('settings.integrations.connected') }}</span>
            </span>
            <span v-else>
                <span class="text-gray-400">{{ $t('settings.integrations.notConnected') }}</span>
            </span>
          </div>
          <div v-if="slackConfig && slackIntegrated" class="text-xs text-gray-400 mt-1">
            <span>{{ $t('settings.integrations.workspace', { name: slackConfig.team_name }) }}</span>
            <span class="ms-2">{{ $t('settings.integrations.id', { id: slackConfig.team_id }) }}</span>
          </div>
        </div>
      </div>
      <button
        class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
        @click.stop="showSlackModal = true"
      >
        {{ slackIntegrated ? $t('settings.integrations.settings') : $t('settings.integrations.integrate') }}
      </button>
    </div>

    <!-- Teams Integration Row -->
    <div
      class="flex items-center justify-between p-4 border rounded-lg cursor-pointer hover:bg-gray-50"
      @click="showTeamsModal = true"
    >
      <div class="flex items-center">
        <img src="/icons/teams.png" alt="Teams" class="w-8 h-8 me-4" />
        <div>
          <div class="font-medium">Microsoft Teams</div>
          <div class="text-sm text-gray-500">
            <span v-if="teamsIntegrated">
                <span class="text-green-600">{{ $t('settings.integrations.connected') }}</span>
            </span>
            <span v-else>
                <span class="text-gray-400">{{ $t('settings.integrations.notConnected') }}</span>
            </span>
          </div>
          <div v-if="teamsConfig && teamsIntegrated" class="text-xs text-gray-400 mt-1">
            <span>{{ $t('settings.integrations.tenant', { id: teamsConfig.tenant_id }) }}</span>
          </div>
        </div>
      </div>
      <button
        class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
        @click.stop="showTeamsModal = true"
      >
        {{ teamsIntegrated ? $t('settings.integrations.settings') : $t('settings.integrations.integrate') }}
      </button>
    </div>

    <!-- WhatsApp Integration Row -->
    <div
      class="flex items-center justify-between p-4 border rounded-lg cursor-pointer hover:bg-gray-50"
      @click="showWhatsAppModal = true"
    >
      <div class="flex items-center">
        <img src="/icons/whatsapp.png" alt="WhatsApp" class="w-8 h-8 me-4" />
        <div>
          <div class="font-medium">WhatsApp</div>
          <div class="text-sm text-gray-500">
            <span v-if="whatsappIntegrated">
                <span class="text-green-600">{{ $t('settings.integrations.connected') }}</span>
            </span>
            <span v-else>
                <span class="text-gray-400">{{ $t('settings.integrations.notConnected') }}</span>
            </span>
          </div>
          <div v-if="whatsappConfig && whatsappIntegrated" class="text-xs text-gray-400 mt-1">
            <span>{{ whatsappConfig.verified_name || $t('settings.integrations.business') }}</span>
            <span class="ms-2">{{ whatsappConfig.display_phone_number }}</span>
          </div>
        </div>
      </div>
      <button
        class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
        @click.stop="showWhatsAppModal = true"
      >
        {{ whatsappIntegrated ? $t('settings.integrations.settings') : $t('settings.integrations.integrate') }}
      </button>
    </div>

    <!-- Excel Add-in Integration Row -->
    <div
      v-if="excelAddinEnabled"
      class="flex items-center justify-between p-4 border rounded-lg cursor-pointer hover:bg-gray-50"
      @click="showExcelModal = true"
    >
      <div class="flex items-center">
        <img src="/data_sources_icons/excel.png" alt="Excel" class="w-8 h-8 me-4" />
        <div>
          <div class="font-medium">{{ $t('settings.integrations.excelAddinName') }}</div>
          <div class="text-sm text-gray-500">
            {{ $t('settings.integrations.excelAddinDescription') }}
          </div>
        </div>
      </div>
      <button
        class="bg-blue-500 text-white text-sm px-3 py-1.5 rounded-md"
        @click.stop="showExcelModal = true"
      >
        {{ $t('settings.integrations.setup') }}
      </button>
    </div>

    <!-- MCP Integration Row -->
    <div
      class="flex items-center justify-between p-4 border rounded-lg"
    >
      <div class="flex items-center">
        <McpIcon class="w-8 h-8 me-4 text-gray-700" />
        <div>
          <div class="font-medium">{{ $t('settings.integrations.mcpName') }}</div>
          <div class="text-sm text-gray-500">
            <span>
              {{ $t('settings.integrations.mcpDescription') }}
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

    <!-- WhatsApp Integration Modal -->
    <UModal v-model="showWhatsAppModal" :ui="{ width: 'max-w-lg' }">
      <WhatsAppIntegrationModal
        :integrated="whatsappIntegrated"
        :integration-data="whatsappIntegrationData"
        @close="showWhatsAppModal = false"
        @updated="fetchIntegrations"
      />
    </UModal>

    <!-- Excel Add-in Modal -->
    <UModal v-model="showExcelModal" :ui="{ width: 'sm:max-w-3xl' }">
      <ExcelAddinModal
        @close="showExcelModal = false"
      />
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import SlackIntegrationModal from '~/components/SlackIntegrationModal.vue'
import TeamsIntegrationModal from '~/components/TeamsIntegrationModal.vue'
import WhatsAppIntegrationModal from '~/components/WhatsAppIntegrationModal.vue'
import ExcelAddinModal from '~/components/ExcelAddinModal.vue'
import McpIcon from '~/components/icons/McpIcon.vue'

definePageMeta({ auth: true, permissions: ['manage_settings'], layout: 'settings' })

const showSlackModal = ref(false)
const slackIntegrated = ref(false)
const slackConfig = ref<{ team_id?: string; team_name?: string } | null>(null)
const slackIntegrationData = ref<any>(null)

const showTeamsModal = ref(false)
const teamsIntegrated = ref(false)
const teamsConfig = ref<{ tenant_id?: string; app_id?: string } | null>(null)
const teamsIntegrationData = ref<any>(null)

const showExcelModal = ref(false)
const excelAddinEnabled = ref(false)

const showWhatsAppModal = ref(false)
const whatsappIntegrated = ref(false)
const whatsappConfig = ref<{ phone_number_id?: string; display_phone_number?: string; verified_name?: string; waba_id?: string } | null>(null)
const whatsappIntegrationData = ref<any>(null)

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

  const whatsapp = integrations.find((i: any) => i.platform_type === 'whatsapp' && i.is_active)
  whatsappIntegrated.value = !!whatsapp
  whatsappConfig.value = whatsapp?.platform_config || null
  whatsappIntegrationData.value = whatsapp || null
}

async function loadMcpState() {
  await fetchSettings()
  const mcpFeature = settings.value?.config?.mcp_enabled
  if (mcpFeature) {
    mcpEnabled.value = mcpFeature.state === 'enabled' || mcpFeature.value === true
  }
  const excelFeature = settings.value?.config?.enable_excel_addin
  if (excelFeature) {
    excelAddinEnabled.value = excelFeature.state === 'enabled' || excelFeature.value === true
  } else {
    excelAddinEnabled.value = true // enabled by default
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
