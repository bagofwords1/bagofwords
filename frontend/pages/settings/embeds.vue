<template>
  <div class="mt-6">
    <h2 class="text-lg font-medium text-gray-900 dark:text-white">
      {{ $t('settings.embeds.title') }}
      <p class="text-sm text-gray-500 dark:text-gray-400 font-normal mb-6">{{ $t('settings.embeds.subtitle') }}</p>
    </h2>

    <div v-if="loading" class="py-4"><ULoader /></div>

    <div v-else class="flex gap-8 min-h-[26rem]">
      <!-- Left: the list -->
      <nav class="w-60 shrink-0 space-y-0.5">
        <button v-for="e in embeds" :key="e.id" type="button"
          class="group w-full flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-start transition-colors"
          :class="selectedId === e.id ? 'bg-gray-100 dark:bg-gray-800' : 'hover:bg-gray-50 dark:hover:bg-gray-800'"
          :data-testid="`embed-item-${e.id}`" @click="select(e.id)">
          <span class="w-2 h-2 shrink-0 rounded-full" :class="e.enabled ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'" />
          <span class="flex-1 min-w-0 truncate text-sm" :class="selectedId === e.id ? 'font-medium text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-400'">{{ e.name }}</span>
        </button>
        <button type="button" class="mt-2 w-full px-2.5 py-2 text-sm rounded-lg border border-dashed border-gray-300 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
          data-testid="embed-create" @click="startCreate">
          + {{ $t('settings.embeds.create') }}
        </button>
      </nav>

      <!-- Right: detail -->
      <div class="flex-1 min-w-0 border-s border-gray-100 dark:border-gray-800 ps-8">
        <!-- Empty state -->
        <div v-if="!form && !creating" class="text-center py-16">
          <Icon name="heroicons:chat-bubble-left-right" class="w-10 h-10 text-gray-300 mx-auto mb-3" />
          <p class="text-sm font-medium text-gray-700 dark:text-gray-200">{{ $t('settings.embeds.none') }}</p>
          <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.noneHint') }}</p>
        </div>

        <!-- Create -->
        <form v-else-if="creating" class="md:w-2/3 space-y-4" data-testid="embed-create-form" @submit.prevent="create">
          <h3 class="text-base font-medium text-gray-900 dark:text-white">{{ $t('settings.embeds.create') }}</h3>
          <div>
            <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.name') }}</label>
            <input v-model="draft.name" type="text" :class="fieldClass" required data-testid="embed-name" placeholder="ERP assistant" />
          </div>
          <div>
            <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.origins') }}</label>
            <textarea v-model="draft.origins" rows="2" :class="fieldClass" data-testid="embed-origins" placeholder="https://erp.example.com" />
            <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.originsHint') }}</p>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.issuer') }}</label>
              <input v-model="draft.issuer" type="text" :class="fieldClass" required data-testid="embed-issuer" placeholder="erp" />
              <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.issuerHint') }}</p>
            </div>
            <div>
              <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.subjectAttribute') }}</label>
              <input v-model="draft.subject_attribute" type="text" :class="fieldClass" required data-testid="embed-subject-attribute" placeholder="employee_id" />
            </div>
          </div>
          <p class="text-xs text-ink-muted">{{ $t('settings.embeds.subjectAttributeHint') }}</p>
          <div class="flex items-center gap-2">
            <button type="submit" class="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50" :disabled="saving" data-testid="embed-create-submit">{{ $t('settings.embeds.create') }}</button>
            <button type="button" class="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300" @click="creating = false">{{ $t('common.cancel') }}</button>
            <span v-if="error" class="text-xs text-red-500" data-testid="embed-error">{{ error }}</span>
          </div>
        </form>

        <!-- Edit -->
        <div v-else class="space-y-5">
          <!-- Private key, shown once -->
          <div v-if="privateKey" class="rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950 dark:border-amber-800 p-3 md:w-2/3" data-testid="embed-private-key-box">
            <div class="flex items-center gap-2 text-sm font-medium text-amber-800 dark:text-amber-200">
              <Icon name="heroicons:key" class="w-4 h-4" /> {{ $t('settings.embeds.privateKeyOnce') }}
            </div>
            <pre class="mt-2 text-2xs font-mono whitespace-pre-wrap break-all max-h-40 overflow-auto bg-white/60 dark:bg-black/30 rounded p-2" data-testid="embed-private-key">{{ privateKey }}</pre>
            <div class="mt-2 flex items-center gap-2">
              <button type="button" class="px-2 py-1 text-xs rounded border border-amber-300 dark:border-amber-700" @click="copy(privateKey, 'key')">{{ copied === 'key' ? $t('settings.embeds.copied') : $t('settings.embeds.copy') }}</button>
              <button type="button" class="px-2 py-1 text-xs rounded border border-amber-300 dark:border-amber-700" @click="download(`${form.public_id}-private.pem`, privateKey)">{{ $t('settings.embeds.download') }}</button>
              <button type="button" class="ms-auto px-2 py-1 text-xs text-amber-800 dark:text-amber-200" @click="privateKey = ''">{{ $t('embed.close') }}</button>
            </div>
          </div>

          <div class="flex items-center justify-between md:w-2/3">
            <div class="flex items-center gap-3 min-w-0">
              <h3 class="text-base font-medium text-gray-900 dark:text-white truncate" data-testid="embed-heading">{{ form.name }}</h3>
              <span class="text-2xs font-mono text-ink-muted" data-testid="embed-public-id">{{ form.public_id }}</span>
            </div>
            <div class="flex items-center gap-3 text-xs text-ink-muted">
              <span :title="$t('settings.embeds.mappedMembers')"><Icon name="heroicons:users" class="w-3.5 h-3.5 inline me-0.5" />{{ form.mapped_members }}</span>
              <span :title="$t('settings.embeds.activeSessions')"><Icon name="heroicons:signal" class="w-3.5 h-3.5 inline me-0.5" />{{ form.active_sessions }}</span>
              <span :title="$t('settings.embeds.conversations')"><Icon name="heroicons:chat-bubble-left-ellipsis" class="w-3.5 h-3.5 inline me-0.5" />{{ form.conversations }}</span>
            </div>
          </div>

          <!-- Tabs -->
          <div class="border-b border-gray-200 dark:border-gray-700 md:w-2/3">
            <nav class="-mb-px flex gap-6">
              <button v-for="tab in tabs" :key="tab" type="button" class="py-2 text-sm border-b-2"
                :class="activeTab === tab ? 'border-blue-500 text-blue-600' : 'border-transparent text-gray-500 hover:text-gray-700'"
                :data-testid="`embed-tab-${tab}`" @click="activeTab = tab">{{ $t(`settings.embeds.tabs.${tab}`) }}</button>
            </nav>
          </div>

          <form class="md:w-2/3 space-y-4" @submit.prevent="save">
            <!-- Overview -->
            <template v-if="activeTab === 'overview'">
              <div class="flex items-start gap-3 border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2.5">
                <UToggle v-model="form.enabled" data-testid="embed-enabled" class="mt-0.5" />
                <div class="text-sm">
                  <div class="font-medium text-gray-900 dark:text-white">{{ $t('settings.embeds.enabled') }}</div>
                  <p class="text-xs text-ink-muted mt-0.5">{{ $t('settings.embeds.enabledHint') }}</p>
                </div>
              </div>
              <div>
                <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.name') }}</label>
                <input v-model="form.name" type="text" :class="fieldClass" required data-testid="embed-edit-name" />
              </div>
              <div>
                <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.origins') }}</label>
                <textarea v-model="form.originsText" rows="3" :class="fieldClass" data-testid="embed-edit-origins" />
                <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.originsHint') }}</p>
              </div>
            </template>

            <!-- Runtime -->
            <template v-else-if="activeTab === 'runtime'">
              <div>
                <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.agents') }}</label>
                <div class="border border-gray-200 dark:border-gray-700 rounded-lg divide-y divide-gray-100 dark:divide-gray-800 max-h-56 overflow-auto" data-testid="embed-agents">
                  <label v-for="a in agents" :key="a.id" class="flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer">
                    <input type="checkbox" :value="a.id" v-model="form.runtime.data_source_ids" class="rounded" />
                    <span class="truncate">{{ a.name }}</span>
                  </label>
                  <p v-if="!agents.length" class="px-3 py-2 text-xs text-ink-muted">—</p>
                </div>
                <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.agentsHint') }}</p>
                <label class="flex items-center gap-2 mt-2 text-sm">
                  <input type="checkbox" v-model="form.runtime.allow_agent_selection" class="rounded" data-testid="embed-allow-agent-selection" />
                  {{ $t('settings.embeds.allowAgentSelection') }}
                </label>
              </div>
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.model') }}</label>
                  <select v-model="form.runtime.model_id" :class="fieldClass" data-testid="embed-model">
                    <option :value="null">{{ $t('settings.embeds.orgDefault') }}</option>
                    <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}</option>
                  </select>
                </div>
                <div>
                  <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.effort') }}</label>
                  <select v-model="form.runtime.reasoning_effort" :class="fieldClass" data-testid="embed-effort">
                    <option :value="null">{{ $t('settings.embeds.effortDefault') }}</option>
                    <option v-for="e in efforts" :key="e" :value="e">{{ e }}</option>
                  </select>
                </div>
              </div>
              <div>
                <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.tools') }}</label>
                <div class="flex flex-wrap gap-2" data-testid="embed-tools">
                  <label v-for="key in options.tool_switches" :key="key" class="flex items-center gap-1.5 text-xs border border-gray-200 dark:border-gray-700 rounded-full px-2 py-1">
                    <input type="checkbox" :checked="form.runtime.tools?.[key] !== false" class="rounded" @change="toggleTool(key, ($event.target as HTMLInputElement).checked)" />
                    {{ key }}
                  </label>
                </div>
                <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.toolsHint') }} <span class="font-mono">{{ options.forced_off_tools.join(', ') }}</span></p>
              </div>
              <label class="flex items-center gap-2 text-sm">
                <input type="checkbox" :checked="form.runtime.follow_ups !== false" class="rounded" @change="form.runtime.follow_ups = ($event.target as HTMLInputElement).checked ? null : false" />
                {{ $t('settings.embeds.followUps') }}
              </label>
            </template>

            <!-- Authentication -->
            <template v-else-if="activeTab === 'auth'">
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.issuer') }}</label>
                  <input v-model="form.issuer" type="text" :class="fieldClass" required data-testid="embed-edit-issuer" />
                  <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.issuerHint') }}</p>
                </div>
                <div>
                  <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.subjectAttribute') }}</label>
                  <input v-model="form.subject_attribute" type="text" :class="fieldClass" required data-testid="embed-edit-subject" />
                </div>
              </div>
              <p class="text-xs text-ink-muted">{{ $t('settings.embeds.subjectAttributeHint') }}</p>
              <div class="border border-gray-200 dark:border-gray-700 rounded-lg p-3 space-y-2">
                <div class="text-sm font-medium">{{ $t('settings.embeds.keyTitle') }}</div>
                <p class="text-xs text-ink-muted">{{ $t('settings.embeds.keyHint') }}</p>
                <p class="text-xs"><span class="text-ink-muted">{{ $t('settings.embeds.keyFingerprint') }}:</span> <span class="font-mono" data-testid="embed-fingerprint">{{ form.key_fingerprint }}</span></p>
                <div class="flex flex-wrap gap-2">
                  <button type="button" class="px-2.5 py-1 text-xs rounded border border-gray-300 dark:border-gray-600" data-testid="embed-rotate-key" @click="rotateKey">{{ $t('settings.embeds.rotateKey') }}</button>
                  <button type="button" class="px-2.5 py-1 text-xs rounded border border-gray-300 dark:border-gray-600" data-testid="embed-revoke-sessions" @click="revokeSessions">{{ $t('settings.embeds.revokeSessions') }}</button>
                </div>
              </div>
              <div v-if="members.length" class="border border-gray-200 dark:border-gray-700 rounded-lg">
                <div class="px-3 py-2 text-sm font-medium border-b border-gray-100 dark:border-gray-800">{{ $t('settings.embeds.mappedMembers') }} ({{ members.length }})</div>
                <div class="max-h-48 overflow-auto divide-y divide-gray-100 dark:divide-gray-800" data-testid="embed-members">
                  <div v-for="m in members" :key="m.membership_id" class="px-3 py-1.5 text-xs flex items-center gap-2">
                    <span class="truncate flex-1">{{ m.name || m.email }}</span>
                    <span class="font-mono text-ink-muted">{{ m.subject }}</span>
                  </div>
                </div>
              </div>
            </template>

            <!-- Appearance -->
            <template v-else-if="activeTab === 'appearance'">
              <div class="grid grid-cols-2 gap-3">
                <div>
                  <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.displayName') }}</label>
                  <input v-model="form.branding.display_name" type="text" :class="fieldClass" data-testid="embed-display-name" :placeholder="form.name" />
                </div>
                <div>
                  <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.accentColor') }}</label>
                  <div class="flex items-center gap-2">
                    <input v-model="form.branding.accent_color" type="color" class="w-9 h-9 rounded border border-gray-200 dark:border-gray-700 p-0.5 bg-transparent" data-testid="embed-accent" />
                    <input v-model="form.branding.accent_color" type="text" :class="fieldClass" />
                  </div>
                </div>
                <div>
                  <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.theme') }}</label>
                  <select v-model="form.branding.theme" :class="fieldClass" data-testid="embed-theme">
                    <option value="light">{{ $t('settings.embeds.themeLight') }}</option>
                    <option value="dark">{{ $t('settings.embeds.themeDark') }}</option>
                    <option value="auto">{{ $t('settings.embeds.themeAuto') }}</option>
                  </select>
                </div>
                <div>
                  <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.locale') }}</label>
                  <select v-model="form.branding.locale" :class="fieldClass" data-testid="embed-locale">
                    <option :value="null">{{ $t('settings.embeds.localeAuto') }}</option>
                    <option v-for="l in options.locales" :key="l" :value="l">{{ l }}</option>
                  </select>
                </div>
              </div>
              <div>
                <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.greeting') }}</label>
                <textarea v-model="form.branding.greeting" rows="2" :class="fieldClass" data-testid="embed-greeting" />
              </div>
              <div>
                <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.suggestions') }}</label>
                <textarea v-model="form.suggestionsText" rows="3" :class="fieldClass" data-testid="embed-suggestions" />
                <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.suggestionsHint') }}</p>
              </div>
              <div>
                <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.launcherLabel') }}</label>
                <input v-model="form.branding.launcher_label" type="text" :class="fieldClass" />
              </div>
              <div>
                <label class="block text-sm font-medium mb-1">{{ $t('settings.embeds.logo') }}</label>
                <div class="flex items-center gap-3">
                  <img v-if="form.branding.logo_key" :src="`/api/embed/${form.public_id}/logo?v=${form.revision}`" alt="" class="h-10 w-auto rounded border border-gray-200 dark:border-gray-700 p-1" data-testid="embed-logo-preview" />
                  <label class="px-2.5 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 cursor-pointer">
                    {{ $t('settings.embeds.uploadLogo') }}
                    <input type="file" accept="image/png,image/jpeg" class="hidden" data-testid="embed-logo-input" @change="uploadLogo" />
                  </label>
                  <button v-if="form.branding.logo_key" type="button" class="text-xs text-red-500" @click="removeLogo">{{ $t('settings.embeds.removeLogo') }}</button>
                </div>
                <p class="text-xs text-ink-muted mt-1">{{ $t('settings.embeds.logoHint') }}</p>
              </div>
            </template>

            <!-- Install -->
            <template v-else-if="activeTab === 'install'">
              <p class="text-xs text-ink-muted">{{ $t('settings.embeds.installHint') }}</p>
              <div>
                <div class="flex items-center justify-between mb-1">
                  <label class="text-sm font-medium">{{ $t('settings.embeds.snippet') }}</label>
                  <button type="button" class="text-xs text-blue-600" @click="copy(snippet, 'snippet')">{{ copied === 'snippet' ? $t('settings.embeds.copied') : $t('settings.embeds.copy') }}</button>
                </div>
                <pre class="text-2xs font-mono bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-3 overflow-auto" data-testid="embed-snippet">{{ snippet }}</pre>
              </div>
              <div>
                <div class="flex items-center justify-between mb-1">
                  <label class="text-sm font-medium">{{ $t('settings.embeds.backendExample') }}</label>
                  <button type="button" class="text-xs text-blue-600" @click="copy(pythonExample, 'py')">{{ copied === 'py' ? $t('settings.embeds.copied') : $t('settings.embeds.copy') }}</button>
                </div>
                <pre class="text-2xs font-mono bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-3 overflow-auto">{{ pythonExample }}</pre>
              </div>
              <div>
                <div class="flex items-center justify-between mb-1">
                  <label class="text-sm font-medium">{{ $t('settings.embeds.backendExampleNode') }}</label>
                  <button type="button" class="text-xs text-blue-600" @click="copy(nodeExample, 'node')">{{ copied === 'node' ? $t('settings.embeds.copied') : $t('settings.embeds.copy') }}</button>
                </div>
                <pre class="text-2xs font-mono bg-gray-50 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-3 overflow-auto">{{ nodeExample }}</pre>
              </div>
              <p class="text-xs text-ink-muted">{{ $t('settings.embeds.assertionContract') }}</p>
              <p class="text-xs text-ink-muted">{{ $t('settings.embeds.cspHint') }}</p>
            </template>

            <!-- Actions -->
            <div v-if="activeTab !== 'install'" class="flex items-center gap-2 pt-2">
              <button type="submit" class="px-3 py-1.5 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50" :disabled="saving" data-testid="embed-save">{{ $t('settings.embeds.save') }}</button>
              <button type="button" class="px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-600" data-testid="embed-preview" @click="showPreview = !showPreview">{{ $t('settings.embeds.preview') }}</button>
              <button type="button" class="ms-auto px-3 py-1.5 text-sm text-red-600" data-testid="embed-delete" @click="remove">{{ $t('settings.embeds.delete') }}</button>
              <span v-if="savedFlash" class="text-xs text-green-600" data-testid="embed-saved">{{ $t('settings.embeds.saved') }}</span>
              <span v-if="error" class="text-xs text-red-500" data-testid="embed-error">{{ error }}</span>
            </div>
          </form>

          <!-- Preview: the real renderer, signed in as the admin -->
          <div v-if="showPreview" class="md:w-2/3">
            <p class="text-xs text-ink-muted mb-2">{{ $t('settings.embeds.previewHint') }}</p>
            <div class="h-[560px] w-[392px] max-w-full border border-dashed border-gray-300 dark:border-gray-700 rounded-xl overflow-hidden bg-gray-50 dark:bg-gray-950">
              <iframe :key="previewKey" :src="`/embed/${form.public_id}?preview=1&mode=inline`" class="w-full h-full border-0" :title="$t('settings.embeds.preview')" data-testid="embed-preview-frame" />
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'

definePageMeta({ layout: 'settings' })

const { t } = useI18n()
const fieldClass = 'w-full border border-gray-200 dark:border-gray-700 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500'
const tabs = ['overview', 'runtime', 'auth', 'appearance', 'install'] as const
const efforts = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']

const loading = ref(true)
const saving = ref(false)
const creating = ref(false)
const error = ref('')
const savedFlash = ref(false)
const copied = ref('')
const privateKey = ref('')
const showPreview = ref(false)
const previewKey = ref(0)
const activeTab = ref<typeof tabs[number]>('overview')

const embeds = ref<any[]>([])
const agents = ref<any[]>([])
const models = ref<any[]>([])
const members = ref<any[]>([])
const options = ref<{ tool_switches: string[]; forced_off_tools: string[]; locales: string[]; sdk_path: string }>({ tool_switches: [], forced_off_tools: [], locales: [], sdk_path: '/api/embed/sdk/v1/bow-embed.js' })
const selectedId = ref<string | null>(null)
const form = ref<any>(null)
const draft = reactive({ name: '', origins: '', issuer: '', subject_attribute: 'employee_id' })

const origin = computed(() => (typeof window !== 'undefined' ? window.location.origin : ''))

function toForm(e: any) {
  return {
    ...JSON.parse(JSON.stringify(e)),
    originsText: (e.allowed_origins || []).join('\n'),
    suggestionsText: (e.branding?.suggestions || []).join('\n'),
    runtime: { data_source_ids: [], model_id: null, reasoning_effort: null, tools: null, follow_ups: null, suggest_instructions: false, allow_agent_selection: false, ...(e.runtime || {}) },
    branding: { display_name: '', accent_color: '#2563eb', theme: 'light', greeting: '', suggestions: [], launcher_label: '', logo_key: null, locale: null, ...(e.branding || {}) },
  }
}

async function load() {
  loading.value = true
  try {
    const [{ data: list }, { data: opts }, { data: ds }, { data: ms }] = await Promise.all([
      useMyFetch('/embeds'),
      useMyFetch('/embeds/options'),
      useMyFetch('/data_sources?show_all=true'),
      useMyFetch('/llm/models?is_enabled=true'),
    ])
    embeds.value = (list.value as any[]) || []
    if (opts.value) options.value = opts.value as any
    agents.value = (ds.value as any[]) || []
    models.value = (ms.value as any[]) || []
    if (selectedId.value) {
      const current = embeds.value.find(e => e.id === selectedId.value)
      if (current) form.value = toForm(current)
    } else if (embeds.value.length) {
      select(embeds.value[0].id)
    }
  } catch (e: any) {
    error.value = e?.message || t('settings.embeds.saveFailed')
  } finally {
    loading.value = false
  }
}

async function select(id: string) {
  selectedId.value = id
  creating.value = false
  error.value = ''
  showPreview.value = false
  const current = embeds.value.find(e => e.id === id)
  form.value = current ? toForm(current) : null
  members.value = []
  try {
    const { data } = await useMyFetch(`/embeds/${id}/members`)
    members.value = (data.value as any[]) || []
  } catch { /* optional */ }
}

function startCreate() {
  creating.value = true
  form.value = null
  selectedId.value = null
  privateKey.value = ''
  error.value = ''
  Object.assign(draft, { name: '', origins: '', issuer: '', subject_attribute: 'employee_id' })
}

function splitLines(text: string) {
  return (text || '').split('\n').map(s => s.trim()).filter(Boolean)
}

function errorText(e: any): string {
  const code = e?.data?.error_code || e?.error_code
  if (code === 'embed.revision_conflict') return t('settings.embeds.conflict')
  const detail = e?.data?.detail || e?.detail || e?.message
  return typeof detail === 'string' ? detail : t('settings.embeds.saveFailed')
}

async function create() {
  saving.value = true
  error.value = ''
  try {
    const { data, error: err } = await useMyFetch('/embeds', {
      method: 'POST',
      body: { name: draft.name, allowed_origins: splitLines(draft.origins), issuer: draft.issuer, subject_attribute: draft.subject_attribute },
    })
    if (err.value) throw err.value
    const created = data.value as any
    privateKey.value = created.private_key
    creating.value = false
    selectedId.value = created.id
    await load()
    activeTab.value = 'auth'
  } catch (e: any) {
    error.value = errorText(e)
  } finally {
    saving.value = false
  }
}

function toggleTool(key: string, enabled: boolean) {
  const tools = { ...(form.value.runtime.tools || {}) }
  if (enabled) delete tools[key]
  else tools[key] = false
  form.value.runtime.tools = Object.keys(tools).length ? tools : null
}

async function save() {
  if (!form.value) return
  saving.value = true
  error.value = ''
  savedFlash.value = false
  const f = form.value
  const body = {
    revision: f.revision,
    name: f.name,
    enabled: f.enabled,
    allowed_origins: splitLines(f.originsText),
    issuer: f.issuer,
    subject_attribute: f.subject_attribute,
    runtime: f.runtime,
    branding: { ...f.branding, suggestions: splitLines(f.suggestionsText) },
  }
  try {
    const { data, error: err } = await useMyFetch(`/embeds/${f.id}`, { method: 'PUT', body })
    if (err.value) throw err.value
    const updated = data.value as any
    embeds.value = embeds.value.map(e => (e.id === updated.id ? updated : e))
    form.value = toForm(updated)
    savedFlash.value = true
    previewKey.value++
    setTimeout(() => { savedFlash.value = false }, 2500)
  } catch (e: any) {
    error.value = errorText(e)
  } finally {
    saving.value = false
  }
}

async function rotateKey() {
  if (!confirm(t('settings.embeds.rotateConfirm'))) return
  const { data, error: err } = await useMyFetch(`/embeds/${form.value.id}/rotate-key`, { method: 'POST' })
  if (err.value) { error.value = errorText(err.value); return }
  privateKey.value = (data.value as any).private_key
  await load()
}

async function revokeSessions() {
  if (!confirm(t('settings.embeds.revokeConfirm'))) return
  const { error: err } = await useMyFetch(`/embeds/${form.value.id}/revoke-sessions`, { method: 'POST' })
  if (err.value) { error.value = errorText(err.value); return }
  await load()
}

async function remove() {
  if (!confirm(t('settings.embeds.deleteConfirm'))) return
  const { error: err } = await useMyFetch(`/embeds/${form.value.id}`, { method: 'DELETE' })
  if (err.value) { error.value = errorText(err.value); return }
  selectedId.value = null
  form.value = null
  await load()
}

async function uploadLogo(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  const fd = new FormData()
  fd.append('logo', file)
  const { error: err } = await useMyFetch(`/embeds/${form.value.id}/logo`, { method: 'POST', body: fd })
  if (err.value) { error.value = errorText(err.value); return }
  await load()
}

async function removeLogo() {
  const { error: err } = await useMyFetch(`/embeds/${form.value.id}/logo`, { method: 'DELETE' })
  if (err.value) { error.value = errorText(err.value); return }
  await load()
}

async function copy(text: string, what: string) {
  try { await navigator.clipboard.writeText(text); copied.value = what; setTimeout(() => { copied.value = '' }, 1500) } catch { /* clipboard blocked */ }
}

function download(name: string, text: string) {
  const blob = new Blob([text], { type: 'application/x-pem-file' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = name
  a.click()
  URL.revokeObjectURL(a.href)
}

const snippet = computed(() => form.value ? `<script src="${origin.value}${options.value.sdk_path}"><\/script>
<script>
  const assistant = BOWEmbed.mount({
    baseUrl: "${origin.value}",
    embedId: "${form.value.public_id}",
    mode: "floating",                       // or "inline" with container: "#assistant"
    getAssertion: async () => {
      // Your backend verifies its own session and signs an assertion (see below).
      const res = await fetch("/internal/bow/assertion", { method: "POST" });
      if (!res.ok) throw new Error("Unable to authenticate assistant");
      return (await res.json()).assertion;
    },
    context:  { recordType: "incident", recordId: "INC-1042" },   // optional, visible to the assistant
    metadata: { workflow_id: "incident-review" }                  // optional, forwarded to MCP dynamic params only
  });
  // assistant.updateContext({...}); assistant.logout(); assistant.destroy();
<\/script>` : '')

const pythonExample = computed(() => form.value ? `# pip install pyjwt cryptography
import time, uuid, jwt

PRIVATE_KEY = open("bow-embed-private.pem").read()   # from BOW, shown once

def bow_assertion(employee_id: str) -> str:
    now = int(time.time())
    return jwt.encode({
        "iss": "${form.value.issuer}",
        "aud": "${form.value.public_id}",
        "sub": employee_id,            # the member's "${form.value.subject_attribute}" attribute in BOW
        "iat": now, "exp": now + 300, "jti": uuid.uuid4().hex,
    }, PRIVATE_KEY, algorithm="RS256")

# POST /internal/bow/assertion -> {"assertion": bow_assertion(current_user.employee_id)}` : '')

const nodeExample = computed(() => form.value ? `// npm install jsonwebtoken
const jwt = require("jsonwebtoken");
const crypto = require("crypto");
const PRIVATE_KEY = require("fs").readFileSync("bow-embed-private.pem");

function bowAssertion(employeeId) {
  return jwt.sign(
    { iss: "${form.value.issuer}", aud: "${form.value.public_id}", sub: employeeId, jti: crypto.randomUUID() },
    PRIVATE_KEY, { algorithm: "RS256", expiresIn: 300 }
  );
}
// app.post("/internal/bow/assertion", (req, res) => res.json({ assertion: bowAssertion(req.user.employeeId) }));` : '')

onMounted(load)
</script>
