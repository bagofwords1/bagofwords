<template>
  <div class="p-4">
    <div class="flex items-center gap-2 mb-2">
      <img src="/icons/whatsapp.png" alt="WhatsApp" class="w-5 h-5" />
      <h1 class="text-lg font-semibold">{{ $t('settings.integrations.channels.whatsapp.title') }}</h1>
    </div>
    <p class="text-sm text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.subtitle') }}</p>
    <hr class="my-4" />

    <!-- Usage Notes -->
    <div class="bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-lg p-4 mb-4">
      <h3 class="text-sm font-medium text-blue-800 mb-2">{{ $t('settings.integrations.channels.common.usageNotes') }}</h3>
      <ul class="text-sm text-blue-700 space-y-1 list-disc list-inside">
        <li>{{ $t('settings.integrations.channels.whatsapp.noteRegistered') }}</li>
        <li>{{ $t('settings.integrations.channels.whatsapp.noteDm') }}</li>
        <li>{{ $t('settings.integrations.channels.whatsapp.noteWindow') }}</li>
      </ul>
    </div>

    <!-- Connected numbers -->
    <div v-if="instances.length" class="mb-4">
      <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">{{ $t('settings.integrations.channels.whatsapp.numbersTitle') }}</h3>
      <div class="space-y-2">
        <div v-for="inst in instances" :key="inst.id" class="border border-gray-200 dark:border-gray-700 rounded-lg">
          <!-- Row header -->
          <button
            class="w-full flex items-center justify-between gap-2 px-3 py-2 text-start"
            @click="toggleExpanded(inst.id)"
          >
            <span class="flex items-center gap-2 min-w-0">
              <span class="w-2 h-2 rounded-full bg-green-500 shrink-0" />
              <span class="font-medium truncate">{{ instanceLabel(inst) }}</span>
              <span class="font-mono text-xs text-gray-500 dark:text-gray-400 truncate">{{ inst.platform_config?.display_phone_number || inst.platform_config?.phone_number_id }}</span>
            </span>
            <span class="text-ink-muted text-xs">{{ expandedId === inst.id ? '▲' : '▼' }}</span>
          </button>

          <!-- Row detail -->
          <div v-if="expandedId === inst.id" class="border-t border-gray-200 dark:border-gray-700 p-3 space-y-4">
            <!-- Details -->
            <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-3">
              <div class="space-y-2 text-sm">
                <div class="flex justify-between">
                  <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.businessName') }}</span>
                  <span class="font-medium">{{ inst.platform_config?.verified_name || $t('settings.integrations.channels.common.na') }}</span>
                </div>
                <div class="flex justify-between">
                  <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.phoneNumber') }}</span>
                  <span class="font-mono text-xs">{{ inst.platform_config?.display_phone_number || $t('settings.integrations.channels.common.na') }}</span>
                </div>
                <div class="flex justify-between">
                  <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.phoneNumberId') }}</span>
                  <span class="font-mono text-xs">{{ inst.platform_config?.phone_number_id || $t('settings.integrations.channels.common.na') }}</span>
                </div>
                <div class="flex justify-between">
                  <span class="text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.wabaId') }}</span>
                  <span class="font-mono text-xs">{{ inst.platform_config?.waba_id || $t('settings.integrations.channels.common.na') }}</span>
                </div>
              </div>
            </div>

            <!-- Agent scope -->
            <div>
              <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.integrations.channels.whatsapp.agentScopeTitle') }}</h4>
              <p class="text-xs text-gray-500 dark:text-gray-400 mb-2">{{ $t('settings.integrations.channels.whatsapp.agentScopeDesc') }}</p>
              <div v-if="agentsLoading" class="text-xs text-ink-muted">{{ $t('settings.integrations.channels.common.loading') }}</div>
              <div v-else-if="!allAgents.length" class="text-xs text-ink-muted">{{ $t('settings.integrations.channels.whatsapp.agentScopeNoAgents') }}</div>
              <template v-else>
                <div class="max-h-48 overflow-y-auto border border-gray-200 dark:border-gray-700 rounded-md divide-y divide-gray-100 dark:divide-gray-800">
                  <label v-for="agent in allAgents" :key="agent.id" class="flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer">
                    <input type="checkbox" :value="agent.id" v-model="scopeDraft" />
                    <span class="truncate">{{ agent.name }}</span>
                  </label>
                </div>
                <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">
                  {{ scopeDraft.length ? $t('settings.integrations.channels.whatsapp.agentScopeCount', { count: scopeDraft.length }) : $t('settings.integrations.channels.whatsapp.agentScopeAll') }}
                </p>
                <UButton class="mt-2" size="xs" color="gray" :loading="savingScope" @click="saveScope(inst.id)">
                  {{ $t('settings.integrations.channels.whatsapp.agentScopeSave') }}
                </UButton>
              </template>
            </div>

            <!-- Connected users -->
            <div>
              <div class="flex items-center justify-between mb-1">
                <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.integrations.channels.whatsapp.connectedUsersTitle') }}</h4>
                <span v-if="users.length" class="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-800 rounded-full px-2 py-0.5 shrink-0">{{ $t('settings.integrations.channels.whatsapp.connectedUsersCount', { count: users.length }) }}</span>
              </div>
              <div v-if="usersLoading" class="text-xs text-ink-muted">{{ $t('settings.integrations.channels.common.loading') }}</div>
              <div v-else-if="!users.length" class="text-xs text-ink-muted">{{ $t('settings.integrations.channels.whatsapp.connectedUsersEmpty') }}</div>
              <template v-else>
                <input
                  v-if="users.length > 5"
                  v-model="usersSearch"
                  type="text"
                  class="w-full border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 text-sm mb-2 bg-white dark:bg-gray-900"
                  :placeholder="$t('settings.integrations.channels.whatsapp.usersSearchPlaceholder')"
                />
                <div v-if="!filteredUsers.length" class="text-xs text-ink-muted">{{ $t('settings.integrations.channels.whatsapp.usersSearchEmpty') }}</div>
                <ul v-else class="max-h-56 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-800 border border-gray-200 dark:border-gray-700 rounded-md">
                  <li v-for="u in filteredUsers" :key="u.id" class="flex items-center justify-between gap-2 px-3 py-1.5 text-sm">
                    <span class="min-w-0 flex flex-col">
                      <span class="font-medium truncate">{{ u.external_name || u.external_email || $t('settings.integrations.channels.whatsapp.unknownUser') }}</span>
                      <span class="font-mono text-xs text-gray-500 dark:text-gray-400 truncate">{{ u.external_user_id }}</span>
                    </span>
                    <span
                      class="text-xs px-1.5 py-0.5 rounded-full shrink-0"
                      :class="u.is_verified ? 'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300' : 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'"
                    >
                      {{ u.is_verified ? $t('settings.integrations.channels.whatsapp.verified') : $t('settings.integrations.channels.whatsapp.unverified') }}
                    </span>
                  </li>
                </ul>
              </template>
            </div>

            <div class="flex justify-end pt-1">
              <UButton color="red" variant="ghost" size="xs" @click="disconnect(inst.id)">
                {{ $t('settings.integrations.channels.common.disconnect') }}
              </UButton>
            </div>
          </div>
        </div>
      </div>

      <!-- Webhook setup (one shared section for all numbers, always visible) -->
      <div class="bg-yellow-50 dark:bg-yellow-950 border border-yellow-200 dark:border-yellow-900 rounded-lg p-3 mt-4 space-y-1.5">
        <h4 class="text-sm font-medium text-yellow-800 dark:text-yellow-200">{{ $t('settings.integrations.channels.whatsapp.webhookSetup') }}</h4>
        <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.webhookSharedNote') }}</p>
        <p class="text-xs text-yellow-700 dark:text-yellow-300">{{ $t('settings.integrations.channels.whatsapp.webhookConfigure') }}</p>
        <code class="block bg-white dark:bg-gray-950 border border-yellow-200 dark:border-yellow-900 rounded px-2 py-1 text-xs break-all">{{ webhookUrl }}</code>
        <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.whatsapp.webhookVerifyHint', { verifyToken: $t('settings.integrations.channels.whatsapp.verifyTokenName'), messages: 'messages' }) }}</p>
      </div>

      <!-- Session staleness (org-wide) -->
      <div class="bg-gray-50 dark:bg-gray-900 rounded-lg p-4 mt-4">
        <h3 class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.integrations.channels.common.sessionStalenessTitle') }}</h3>
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">{{ $t('settings.integrations.channels.whatsapp.sessionStalenessDesc') }}</p>
        <div class="flex items-center gap-2">
          <input
            v-model.number="sessionMaxAgeHours"
            type="number"
            min="1"
            max="720"
            class="w-24 border rounded px-2 py-1 text-sm"
            :disabled="savingSessionMaxAge"
            @keyup.enter="saveSessionMaxAge"
          />
          <span class="text-sm text-gray-600 dark:text-gray-400">{{ $t('settings.integrations.channels.common.hoursSuffix') }}</span>
          <UButton size="xs" color="gray" :loading="savingSessionMaxAge" @click="saveSessionMaxAge">
            {{ $t('settings.integrations.channels.common.save') }}
          </UButton>
        </div>
      </div>
    </div>

    <!-- Connect a (new) number -->
    <div>
      <button
        v-if="instances.length && !showConnectForm"
        class="text-sm text-blue-600 hover:underline"
        @click="openConnectForm"
      >
        + {{ $t('settings.integrations.channels.whatsapp.addNumber') }}
      </button>

      <form v-if="!instances.length || showConnectForm" ref="connectFormRef" @submit.prevent="connect" class="mt-2">
        <h3 v-if="instances.length" class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">{{ $t('settings.integrations.channels.whatsapp.addNumberTitle') }}</h3>
        <div class="mb-4">
          <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.nameLabel') }}</label>
          <input v-model="name" type="text" class="w-full border rounded px-2 py-1" :placeholder="$t('settings.integrations.channels.whatsapp.namePlaceholder')" />
        </div>
        <div class="mb-4">
          <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.accessToken') }}</label>
          <input v-model="accessToken" type="password" class="w-full border rounded px-2 py-1" required />
          <i18n-t keypath="settings.integrations.channels.whatsapp.accessTokenHint" tag="p" class="text-xs text-gray-500 dark:text-gray-400 mt-1">
            <template #scope1><code>whatsapp_business_messaging</code></template>
            <template #scope2><code>whatsapp_business_management</code></template>
          </i18n-t>
        </div>
        <div class="mb-4">
          <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.phoneNumberIdLabel') }}</label>
          <input v-model="phoneNumberId" type="text" class="w-full border rounded px-2 py-1" required />
        </div>
        <div class="mb-4">
          <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.wabaIdLabel') }}</label>
          <input v-model="wabaId" type="text" class="w-full border rounded px-2 py-1" required />
        </div>
        <div class="mb-4">
          <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.appSecret') }}</label>
          <input v-model="appSecret" type="password" class="w-full border rounded px-2 py-1" required />
          <i18n-t keypath="settings.integrations.channels.whatsapp.appSecretHint" tag="p" class="text-xs text-gray-500 dark:text-gray-400 mt-1">
            <template #header><code>X-Hub-Signature-256</code></template>
          </i18n-t>
        </div>
        <div class="mb-4">
          <label class="block text-sm font-medium mb-1">{{ $t('settings.integrations.channels.whatsapp.verifyToken') }}</label>
          <input v-model="verifyToken" type="text" class="w-full border rounded px-2 py-1" required />
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ $t('settings.integrations.channels.whatsapp.verifyTokenHint') }}</p>
        </div>

        <div class="bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-3 mb-4">
          <p class="text-xs text-gray-600 dark:text-gray-400 mb-1">{{ $t('settings.integrations.channels.whatsapp.webhookAfterConnect') }}</p>
          <code class="block bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded px-2 py-1 text-xs break-all">{{ webhookUrl }}</code>
        </div>

        <div class="flex items-center gap-2">
          <button type="submit" class="bg-blue-600 text-white text-sm px-3 py-1.5 rounded-md">{{ $t('settings.integrations.channels.common.connect') }}</button>
          <button v-if="instances.length" type="button" class="text-sm text-ink-muted" @click="cancelConnect">{{ $t('settings.integrations.channels.whatsapp.cancel') }}</button>
        </div>
      </form>
    </div>

    <button class="absolute top-2 end-2 text-ink-muted hover:text-gray-600 dark:hover:text-gray-400" :title="$t('settings.integrations.channels.common.close')" @click="$emit('close')">✕</button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick } from 'vue'
const props = defineProps<{
  instances?: any[]
}>()
const emit = defineEmits(['close', 'updated'])
const toast = useToast()
const { t } = useI18n()

const instances = computed(() => props.instances || [])

// ---- Per-instance expansion + loaded detail ----
const expandedId = ref<string | null>(null)
const allAgents = ref<any[]>([])
const agentsLoading = ref(false)
const scopeDraft = ref<string[]>([])
const savingScope = ref(false)
const users = ref<any[]>([])
const usersLoading = ref(false)
const usersSearch = ref('')

const filteredUsers = computed(() => {
  const q = usersSearch.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter((u: any) =>
    (u.external_name || '').toLowerCase().includes(q) ||
    (u.external_email || '').toLowerCase().includes(q) ||
    (u.external_user_id || '').toLowerCase().includes(q),
  )
})

function instanceLabel(inst: any): string {
  return inst.platform_config?.name
    || inst.platform_config?.verified_name
    || inst.platform_config?.display_phone_number
    || inst.platform_config?.phone_number_id
    || t('settings.integrations.channels.whatsapp.unnamedNumber')
}

async function toggleExpanded(id: string) {
  if (expandedId.value === id) {
    expandedId.value = null
    return
  }
  expandedId.value = id
  await Promise.all([loadAgents(), loadScope(id), loadUsers(id)])
}

async function loadAgents() {
  if (allAgents.value.length) return
  agentsLoading.value = true
  const res = await useMyFetch('/api/data_sources', { query: { show_all: true } })
  agentsLoading.value = false
  if (res.status.value === 'success') {
    allAgents.value = ((res.data.value as any[]) || []).map((d: any) => ({ id: d.id, name: d.name }))
  }
}

async function loadScope(id: string) {
  scopeDraft.value = []
  const res = await useMyFetch(`/api/settings/integrations/${id}/agents`)
  if (res.status.value === 'success') {
    scopeDraft.value = ((res.data.value as any)?.agent_ids || []) as string[]
  }
}

async function saveScope(id: string) {
  savingScope.value = true
  const res = await useMyFetch(`/api/settings/integrations/${id}/agents`, {
    method: 'PUT',
    body: { agent_ids: scopeDraft.value },
  })
  savingScope.value = false
  if (res.status.value === 'success') {
    scopeDraft.value = ((res.data.value as any)?.agent_ids || []) as string[]
    toast.add({ title: t('settings.integrations.channels.whatsapp.agentScopeSaved'), color: 'green' })
  } else {
    toast.add({
      title: t('settings.integrations.channels.common.failedToUpdateSetting'),
      description: (res.error.value as any)?.data?.detail || (res.error.value as any)?.message,
      color: 'red',
    })
  }
}

async function loadUsers(id: string) {
  users.value = []
  usersSearch.value = ''
  usersLoading.value = true
  const res = await useMyFetch(`/api/settings/integrations/${id}/users`)
  usersLoading.value = false
  if (res.status.value === 'success') {
    users.value = (res.data.value as any[]) || []
  }
}

// ---- Session staleness (org-wide) ----
const sessionMaxAgeHours = ref<number>(24)
const savingSessionMaxAge = ref(false)

async function loadSessionMaxAge() {
  const res = await useMyFetch('/api/organization/settings')
  if (res.status.value === 'success') {
    const v = (res.data.value as any)?.config?.whatsapp_session_max_age_hours
    if (typeof v === 'number' && v > 0) sessionMaxAgeHours.value = v
  }
}

async function saveSessionMaxAge() {
  const v = sessionMaxAgeHours.value
  if (!Number.isInteger(v) || v < 1 || v > 720) {
    toast.add({ title: t('settings.integrations.channels.common.sessionStalenessInvalid'), color: 'amber' })
    return
  }
  savingSessionMaxAge.value = true
  const res = await useMyFetch('/api/organization/settings', {
    method: 'PUT',
    body: { config: { whatsapp_session_max_age_hours: v } },
  })
  savingSessionMaxAge.value = false
  if (res.status.value === 'success') {
    toast.add({ title: t('settings.integrations.channels.common.sessionStalenessSaved'), color: 'green' })
  } else {
    toast.add({
      title: t('settings.integrations.channels.common.failedToUpdateSetting'),
      description: (res.error.value as any)?.data?.detail || (res.error.value as any)?.message,
      color: 'red',
    })
  }
}

onMounted(() => {
  if (instances.value.length) loadSessionMaxAge()
})

// ---- Connect a new number ----
const showConnectForm = ref(false)
const name = ref('')
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

const connectFormRef = ref<HTMLElement | null>(null)

async function openConnectForm() {
  showConnectForm.value = true
  await nextTick()
  connectFormRef.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

function resetForm() {
  name.value = ''
  accessToken.value = ''
  phoneNumberId.value = ''
  wabaId.value = ''
  appSecret.value = ''
  verifyToken.value = ''
}

function cancelConnect() {
  showConnectForm.value = false
  resetForm()
}

async function connect() {
  const res = await useMyFetch('/api/settings/integrations/whatsapp', {
    method: 'POST',
    body: {
      name: name.value || null,
      access_token: accessToken.value,
      phone_number_id: phoneNumberId.value,
      waba_id: wabaId.value,
      app_secret: appSecret.value,
      verify_token: verifyToken.value,
    },
  })
  if (res.status.value === 'success') {
    toast.add({
      title: t('settings.integrations.channels.whatsapp.connectedToast'),
      description: t('settings.integrations.channels.whatsapp.connectedToastDesc'),
      color: 'green',
    })
    resetForm()
    showConnectForm.value = false
    emit('updated')
  } else {
    toast.add({
      title: t('settings.integrations.channels.whatsapp.failedConnect'),
      description: (res.error.value as any).data?.detail || (res.error.value as any).message,
      color: 'red',
    })
  }
}

async function disconnect(id: string) {
  const res = await useMyFetch(`/api/settings/integrations/${id}`, { method: 'DELETE' })
  if (res.status.value === 'success') {
    toast.add({
      title: t('settings.integrations.channels.whatsapp.disconnectedToast'),
      description: t('settings.integrations.channels.whatsapp.disconnectedToastDesc'),
      color: 'green',
    })
    if (expandedId.value === id) expandedId.value = null
    emit('updated')
  } else {
    toast.add({
      title: t('settings.integrations.channels.whatsapp.failedDisconnect'),
      description: (res.error.value as any).data?.detail || (res.error.value as any).message,
      color: 'red',
    })
  }
}
</script>
