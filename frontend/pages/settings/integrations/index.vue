<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900">
      {{ $t('settings.integrations.title') }}
      <p class="text-sm text-gray-500 font-normal mb-6">
        {{ $t('settings.integrations.subtitle') }}
      </p>
    </h2>
  </div>

  <!-- Two-pane layout: list on the left, detail / empty state on the right -->
  <div class="mt-2 flex gap-8 min-h-[26rem]">
    <!-- Left pane: integrations list -->
    <nav class="w-64 shrink-0 space-y-0.5">
      <button
        v-for="item in integrations"
        :key="item.key"
        type="button"
        class="group w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors"
        :class="selectedKey === item.key ? 'bg-gray-100' : 'hover:bg-gray-50'"
        @click="selectedKey = item.key"
      >
        <span class="w-6 h-6 shrink-0 flex items-center justify-center">
          <img v-if="item.iconType === 'img'" :src="item.icon" :alt="item.name" class="w-6 h-6" />
          <McpIcon v-else-if="item.iconType === 'mcp'" class="w-5 h-5 text-gray-600" />
          <UIcon v-else :name="item.icon" class="w-5 h-5 text-gray-500" />
        </span>
        <span
          class="flex-1 min-w-0 truncate text-sm"
          :class="selectedKey === item.key ? 'font-medium text-gray-900' : 'text-gray-600'"
        >
          {{ item.name }}
        </span>
        <span
          class="w-2 h-2 shrink-0 rounded-full"
          :class="item.connected ? 'bg-green-500' : 'bg-gray-300'"
          :title="item.connected ? $t('settings.integrations.connected') : $t('settings.integrations.notConnected')"
        />
      </button>
    </nav>

    <!-- Right pane: detail / empty state -->
    <div class="flex-1 min-w-0 border-l border-gray-100 pl-8">
      <!-- Empty state: illustration as backdrop with icon + copy centered on top -->
      <div
        v-if="!selectedItem"
        class="flex justify-center px-6 pt-2"
      >
        <!-- Fixed-height clipped box: image anchored to the bottom so the PNG's
             top whitespace is cropped and the artwork sits at the top. -->
        <div class="relative w-full max-w-lg h-72 overflow-hidden">
          <img
            src="/assets/empty-states/empty-integrations.png"
            alt=""
            class="absolute inset-x-0 bottom-0 w-full opacity-80 select-none pointer-events-none"
          />
          <div class="absolute inset-x-0 bottom-0 flex flex-col items-center justify-center text-center px-6 pb-2">
            <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white/70 backdrop-blur-sm ring-1 ring-gray-200/70 shadow-sm">
              <UIcon name="i-heroicons-squares-plus" class="w-5 h-5 text-gray-400" />
            </div>
            <h3 class="mt-3 text-[15px] font-medium text-gray-900">
              {{ $t('settings.integrations.emptyTitle') }}
            </h3>
            <p class="mt-1.5 max-w-xs text-sm leading-relaxed text-gray-500">
              {{ $t('settings.integrations.emptySubtitle') }}
            </p>
          </div>
        </div>
      </div>

      <!-- MCP toggle (no modal component) -->
      <div v-else-if="selectedItem.kind === 'toggle'" class="h-full flex flex-col">
        <div class="flex items-center gap-3">
          <McpIcon class="w-7 h-7 text-gray-700" />
          <div class="min-w-0">
            <h3 class="text-[15px] font-medium text-gray-900 leading-tight">{{ selectedItem.name }}</h3>
            <span class="inline-flex items-center gap-1.5 text-xs" :class="selectedItem.connected ? 'text-green-600' : 'text-gray-400'">
              <span class="w-1.5 h-1.5 rounded-full" :class="selectedItem.connected ? 'bg-green-500' : 'bg-gray-300'" />
              {{ selectedItem.connected ? $t('settings.integrations.connected') : $t('settings.integrations.notConnected') }}
            </span>
          </div>
        </div>
        <p class="mt-4 max-w-md text-sm leading-relaxed text-gray-500">{{ selectedItem.description }}</p>
        <div class="mt-6 flex items-center gap-3">
          <UToggle
            v-model="mcpEnabled"
            :loading="mcpUpdating"
            @update:model-value="toggleMcp"
          />
          <span class="text-sm text-gray-500">
            {{ mcpEnabled ? $t('settings.integrations.connected') : $t('settings.integrations.notConnected') }}
          </span>
        </div>
      </div>

      <!-- Selected integration: render its config inline (was a modal) -->
      <component
        v-else
        :is="selectedItem.component"
        :key="selectedItem.key"
        v-bind="selectedItem.props"
        @close="selectedKey = null"
        @updated="selectedItem.onUpdated && selectedItem.onUpdated()"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed, markRaw } from 'vue'
import { useI18n } from 'vue-i18n'
import SlackIntegrationModal from '~/components/SlackIntegrationModal.vue'
import TeamsIntegrationModal from '~/components/TeamsIntegrationModal.vue'
import WhatsAppIntegrationModal from '~/components/WhatsAppIntegrationModal.vue'
import EmailIntegrationModal from '~/components/EmailIntegrationModal.vue'
import ExcelAddinModal from '~/components/ExcelAddinModal.vue'
import OAuthClientsModal from '~/components/OAuthClientsModal.vue'
import McpIcon from '~/components/icons/McpIcon.vue'

definePageMeta({ auth: true, permissions: ['manage_settings'], layout: 'settings' })

const { t } = useI18n()

// Which integration is shown in the right pane (null = empty state)
const selectedKey = ref<string | null>(null)

const slackIntegrated = ref(false)
const slackConfig = ref<{ team_id?: string; team_name?: string } | null>(null)
const slackIntegrationData = ref<any>(null)

const teamsIntegrated = ref(false)
const teamsConfig = ref<{ tenant_id?: string; app_id?: string } | null>(null)
const teamsIntegrationData = ref<any>(null)

const excelAddinEnabled = ref(false)

const whatsappIntegrated = ref(false)
const whatsappConfig = ref<{ phone_number_id?: string; display_phone_number?: string; verified_name?: string; waba_id?: string } | null>(null)
const whatsappIntegrationData = ref<any>(null)

const emailIntegrated = ref(false)
const emailConfig = ref<{ from_address?: string; inbound_enabled?: boolean; capabilities?: string[] } | null>(null)
const emailIntegrationData = ref<any>(null)
// Prefill sources for the Email modal: the org's AI analyst name + signup domains.
const analystName = computed<string>(() => (settings.value as any)?.config?.general?.ai_analyst_name || '')
const signupDomains = ref<string[]>([])

async function fetchSignupDomains() {
  try {
    const res = await useMyFetch('/organization/signup-policy')
    const policy = res.data.value as any
    if (policy?.allowed_domains?.length) signupDomains.value = policy.allowed_domains
  } catch (e) {
    // best-effort prefill; ignore failures
  }
}

// MCP state
const mcpEnabled = ref(false)
const mcpUpdating = ref(false)

// OAuth clients
const oauthClientCount = ref(0)

const { settings, fetchSettings } = useOrgSettings()

// Raw component refs so Vue doesn't try to make them reactive.
const Slack = markRaw(SlackIntegrationModal)
const Teams = markRaw(TeamsIntegrationModal)
const WhatsApp = markRaw(WhatsAppIntegrationModal)
const Email = markRaw(EmailIntegrationModal)
const Excel = markRaw(ExcelAddinModal)
const OAuth = markRaw(OAuthClientsModal)

// Build the integrations list that drives both panes. Each entry carries the
// component to render inline in the right pane (the former modal contents).
const integrations = computed(() => {
  const items: any[] = [
    {
      key: 'slack',
      name: 'Slack',
      iconType: 'img',
      icon: '/icons/slack.png',
      connected: slackIntegrated.value,
      kind: 'component',
      component: Slack,
      props: { integrated: slackIntegrated.value, integrationData: slackIntegrationData.value },
      onUpdated: fetchIntegrations,
    },
    {
      key: 'teams',
      name: 'Microsoft Teams',
      iconType: 'img',
      icon: '/icons/teams.png',
      connected: teamsIntegrated.value,
      kind: 'component',
      component: Teams,
      props: { integrated: teamsIntegrated.value, integrationData: teamsIntegrationData.value },
      onUpdated: fetchIntegrations,
    },
    {
      key: 'whatsapp',
      name: 'WhatsApp',
      iconType: 'img',
      icon: '/icons/whatsapp.png',
      connected: whatsappIntegrated.value,
      kind: 'component',
      component: WhatsApp,
      props: { integrated: whatsappIntegrated.value, integrationData: whatsappIntegrationData.value },
      onUpdated: fetchIntegrations,
    },
    {
      key: 'email',
      name: t('settings.integrations.emailName'),
      iconType: 'uicon',
      icon: 'i-heroicons-sparkles',
      connected: emailIntegrated.value,
      kind: 'component',
      component: Email,
      props: {
        integrated: emailIntegrated.value,
        integrationData: emailIntegrationData.value,
        analystName: analystName.value,
        prefillDomains: signupDomains.value,
      },
      onUpdated: fetchIntegrations,
    },
  ]

  if (excelAddinEnabled.value) {
    items.push({
      key: 'excel',
      name: t('settings.integrations.excelAddinName'),
      iconType: 'img',
      icon: '/data_sources_icons/excel.png',
      connected: false,
      kind: 'component',
      component: Excel,
      props: {},
    })
  }

  items.push({
    key: 'mcp',
    name: t('settings.integrations.mcpName'),
    iconType: 'mcp',
    icon: '',
    description: t('settings.integrations.mcpDescription'),
    connected: mcpEnabled.value,
    kind: 'toggle',
  })

  items.push({
    key: 'oauth',
    name: t('settings.integrations.oauthName'),
    iconType: 'uicon',
    icon: 'i-heroicons-key',
    connected: oauthClientCount.value > 0,
    kind: 'component',
    component: OAuth,
    props: {},
    onUpdated: fetchOAuthClientCount,
  })

  return items
})

const selectedItem = computed(() => integrations.value.find(i => i.key === selectedKey.value) || null)

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

  const email = integrations.find((i: any) => i.platform_type === 'email' && i.is_active)
  emailIntegrated.value = !!email
  emailConfig.value = email?.platform_config || null
  emailIntegrationData.value = email || null
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

async function fetchOAuthClientCount() {
  try {
    const res = await useMyFetch('/api/oauth/clients')
    const clients = (res.data.value as any[]) || []
    oauthClientCount.value = clients.length
  } catch (e) {
    oauthClientCount.value = 0
  }
}

onMounted(() => {
  fetchIntegrations()
  loadMcpState()
  fetchOAuthClientCount()
  fetchSignupDomains()
})
</script>
