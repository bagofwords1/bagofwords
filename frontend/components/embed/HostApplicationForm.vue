<template>
  <!-- Create or edit one host application. After a create (or switching to
       assertion mode, or a key rotation) the private key is shown once. -->
  <div class="space-y-4" data-testid="host-app-form">
    <template v-if="revealedKey">
      <p class="flex gap-2 text-xs leading-5 text-amber-700 dark:text-amber-300">
        <UIcon name="i-heroicons-key" class="mt-0.5 h-4 w-4 shrink-0" />
        <span>{{ $t('hostApps.keyOnce') }}</span>
      </p>
      <div>
        <div class="mb-1 flex items-center justify-between">
          <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('hostApps.privateKey') }}</span>
          <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-clipboard-document" @click="copy(revealedKey)">
            {{ $t('hostApps.copy') }}
          </UButton>
        </div>
        <textarea readonly :value="revealedKey" rows="5" data-testid="host-app-private-key" dir="ltr"
                  class="w-full rounded-lg border border-gray-200 bg-gray-50/60 p-2 font-mono text-2xs text-gray-700 dark:border-gray-800 dark:bg-gray-900/60 dark:text-gray-300" />
      </div>
      <div v-if="saved?.identity_mode === 'assertion'">
        <div class="mb-1 text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('hostApps.backendExample') }}</div>
        <EmbedCode :code="backendExample(saved, lang)" :langs="EXAMPLE_LANGS" v-model="lang" testid="host-app-new-backend-example" />
      </div>
      <div class="flex justify-end">
        <UButton size="sm" color="gray" variant="solid" data-testid="host-app-key-done" @click="finish">{{ $t('hostApps.keySaved') }}</UButton>
      </div>
    </template>

    <template v-else>
      <UFormGroup :label="$t('hostApps.name')" :hint="$t('hostApps.nameHint')">
        <UInput v-model="form.name" data-testid="host-app-name" :placeholder="$t('hostApps.namePlaceholder')" />
      </UFormGroup>

      <UFormGroup :label="$t('hostApps.origins')" :help="$t('hostApps.originsHelp')">
        <UTextarea v-model="originsText" :rows="2" data-testid="host-app-origins" placeholder="https://erp.example.com" />
      </UFormGroup>

      <UFormGroup :label="$t('hostApps.signIn')">
        <div class="space-y-2">
          <label v-for="option in modes" :key="option.value"
                 class="flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2.5"
                 :class="form.identity_mode === option.value ? 'border-blue-300 bg-blue-50/60 dark:border-blue-800 dark:bg-blue-950/30' : 'border-gray-200 dark:border-gray-800'">
            <input v-model="form.identity_mode" type="radio" :value="option.value" class="mt-1" :data-testid="`host-app-mode-${option.value}`" />
            <span>
              <span class="block text-sm font-medium text-gray-900 dark:text-white">{{ option.label }}</span>
              <span class="block text-xs leading-5 text-gray-500 dark:text-gray-400">{{ option.help }}</span>
            </span>
          </label>
        </div>
      </UFormGroup>

      <template v-if="form.identity_mode !== 'sso'">
        <UFormGroup :label="$t('hostApps.source')" :help="$t('hostApps.sourceHelp')">
          <USelect v-model="sourceKind" :options="sourceOptions" option-attribute="label" value-attribute="value"
                   data-testid="host-app-source" />
        </UFormGroup>
        <UFormGroup v-if="sourceKind === 'member_attribute:'" :label="$t('hostApps.attribute')" :help="$t('hostApps.attributeHelp')">
          <UInput v-model="attributeKey" placeholder="employee_id" data-testid="host-app-attribute" />
        </UFormGroup>
        <UFormGroup v-if="form.identity_mode === 'assertion'" :label="$t('hostApps.issuer')" :help="$t('hostApps.issuerHelp')">
          <UInput v-model="form.issuer" placeholder="erp" data-testid="host-app-issuer" />
        </UFormGroup>
        <UFormGroup v-if="form.identity_mode === 'idp_token'" :label="$t('hostApps.audience')" :help="$t('hostApps.audienceHelp')">
          <UInput v-model="form.audience" placeholder="api://erp" data-testid="host-app-audience" />
        </UFormGroup>
        <label v-if="selectedSource?.supports_create_users" class="flex items-start gap-2.5">
          <UToggle v-model="form.create_users" data-testid="host-app-create-users" />
          <span>
            <span class="block text-sm text-gray-900 dark:text-white">{{ $t('hostApps.createUsers') }}</span>
            <span class="block text-xs leading-5 text-gray-500 dark:text-gray-400">{{ $t('hostApps.createUsersHelp') }}</span>
          </span>
        </label>
      </template>

      <p v-if="error" class="text-sm text-red-600 dark:text-red-400" data-testid="host-app-error">{{ error }}</p>
      <div class="flex justify-end gap-2">
        <UButton size="sm" color="gray" variant="ghost" @click="emit('cancel')">{{ $t('common.cancel') }}</UButton>
        <UButton size="sm" color="blue" :loading="saving" data-testid="host-app-save" @click="save">
          {{ host ? $t('common.save') : $t('hostApps.create') }}
        </UButton>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import EmbedCode from '~/components/embed/EmbedCode.vue'
import { EXAMPLE_LANGS, backendExample, type ExampleLang } from '~/utils/embedExamples'

type Host = Record<string, any>
const props = defineProps<{ host?: Host | null }>()
const emit = defineEmits<{ (e: 'saved', host: Host): void; (e: 'cancel'): void }>()

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const form = reactive({
  name: props.host?.name || '',
  identity_mode: (props.host?.identity_mode || 'assertion') as 'assertion' | 'idp_token' | 'sso',
  issuer: props.host?.issuer || '',
  audience: props.host?.audience || '',
  create_users: !!props.host?.create_users,
})
const originsText = ref((props.host?.allowed_origins || []).join('\n'))
const currentSource: string = props.host?.identity_source || ''
const sourceKind = ref(currentSource.startsWith('member_attribute:') ? 'member_attribute:' : currentSource)
const attributeKey = ref(currentSource.startsWith('member_attribute:') ? currentSource.slice('member_attribute:'.length) : '')
const sources = ref<any[]>([])
const saving = ref(false)
const error = ref('')
const revealedKey = ref('')
const saved = ref<Host | null>(null)
const lang = ref<ExampleLang>('python')

const modes = computed(() => [
  { value: 'assertion', label: t('hostApps.modeAssertion'), help: t('hostApps.modeAssertionHelp') },
  { value: 'idp_token', label: t('hostApps.modeIdp'), help: t('hostApps.modeIdpHelp') },
  { value: 'sso', label: t('hostApps.modeSso'), help: t('hostApps.modeSsoHelp') },
])
const sourceOptions = computed(() => sources.value
  .filter(s => form.identity_mode !== 'idp_token' || s.supports_idp_token)
  .map(s => ({ value: s.value, label: s.label })))
const selectedSource = computed(() => sources.value.find(s => s.value === sourceKind.value) || null)
watch(selectedSource, (s) => { if (!s?.supports_create_users) form.create_users = false })
watch(sourceOptions, (opts) => {
  if (!opts.some(o => o.value === sourceKind.value)) sourceKind.value = opts[0]?.value || ''
})

onMounted(async () => {
  const { data } = await useMyFetch('/api/host_applications/identity_sources')
  sources.value = (data.value as any[]) || []
  if (!sourceKind.value) sourceKind.value = sourceOptions.value[0]?.value || ''
})

function body() {
  const source = form.identity_mode === 'sso' ? null
    : sourceKind.value === 'member_attribute:' ? `member_attribute:${attributeKey.value.trim()}` : sourceKind.value
  return {
    name: form.name.trim(),
    allowed_origins: originsText.value.split(/[\s,]+/).map(s => s.trim()).filter(Boolean),
    identity_mode: form.identity_mode,
    identity_source: source,
    create_users: form.identity_mode !== 'sso' && form.create_users,
    issuer: form.identity_mode === 'assertion' ? form.issuer.trim() : null,
    audience: form.identity_mode === 'idp_token' ? form.audience.trim() : null,
  }
}

async function save() {
  saving.value = true
  error.value = ''
  try {
    const { data, error: fetchError } = props.host
      ? await useMyFetch(`/api/host_applications/${props.host.id}`, { method: 'PATCH', body: { ...body(), revision: props.host.revision } })
      : await useMyFetch('/api/host_applications', { method: 'POST', body: body() })
    if (fetchError.value) { error.value = getErrorMessage(fetchError.value); return }
    saved.value = data.value as Host
    if (saved.value?.private_key) revealedKey.value = saved.value.private_key
    else finish()
  } finally {
    saving.value = false
  }
}

function finish() {
  revealedKey.value = ''
  if (saved.value) emit('saved', saved.value)
}

async function copy(value: string) {
  try { await navigator.clipboard.writeText(value); toast.add({ title: t('hostApps.copied') }) } catch { /* no clipboard */ }
}
</script>
