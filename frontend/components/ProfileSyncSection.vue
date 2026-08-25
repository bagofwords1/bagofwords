<template>
  <div class="border border-gray-200 dark:border-gray-700 rounded-lg">
    <button
      class="w-full flex items-center justify-between px-4 py-3 text-start"
      @click="open = !open"
    >
      <div>
        <h2 class="text-sm font-medium text-gray-900 dark:text-white">{{ pt('Title') }}</h2>
        <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ pt('Subtitle') }}</p>
      </div>
      <svg class="w-4 h-4 text-gray-400 transition-transform flex-shrink-0" :class="open ? 'rotate-180' : ''" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
        <path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
      </svg>
    </button>

    <div v-show="open" class="px-4 pb-4 border-t border-gray-100 dark:border-gray-800 pt-4">
      <!-- Enable toggle -->
      <div class="flex items-start justify-between gap-4 mb-4">
        <div class="flex-1">
          <div class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ pt('Enable') }}</div>
          <p class="text-[11px] text-gray-400 dark:text-gray-400 mt-0.5">{{ pt('EnableHint') }}</p>
        </div>
        <button
          type="button"
          role="switch"
          :aria-checked="enabled"
          class="relative inline-flex h-5 w-9 flex-shrink-0 rounded-full transition-colors focus:outline-none"
          :class="enabled ? 'bg-blue-600' : 'bg-gray-200 dark:bg-gray-700'"
          @click="enabled = !enabled"
        >
          <span
            class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform mt-0.5"
            :class="enabled ? 'translate-x-4' : 'translate-x-0.5'"
          ></span>
        </button>
      </div>

      <!-- Fields to include in context -->
      <div v-show="enabled">
        <div class="flex items-center justify-between mb-2">
          <label class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ pt('FieldsLabel') }}</label>
          <span v-if="previewLoading" class="text-[11px] text-gray-400">
            <span class="inline-block w-3 h-3 border-2 border-gray-200 dark:border-gray-700 border-t-gray-500 rounded-full animate-spin align-middle"></span>
          </span>
        </div>

        <!-- Preview connection hint -->
        <p v-if="preview && !preview.connected" class="text-[11px] text-amber-600 dark:text-amber-500 mb-2">
          {{ previewHint }}
        </p>

        <!-- Field rows: checkbox (in context) | label | sample value | remove -->
        <div class="border border-gray-200 dark:border-gray-700 rounded overflow-hidden">
          <div
            v-for="(field, idx) in shownFields"
            :key="field"
            class="flex items-center gap-2 px-3 py-2 text-xs"
            :class="{ 'border-t border-gray-100 dark:border-gray-800': idx > 0 }"
          >
            <input
              :id="`${i18nPrefix}-field-${field}`"
              type="checkbox"
              :checked="selected.has(field)"
              class="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              @change="toggleField(field)"
            />
            <label :for="`${i18nPrefix}-field-${field}`" class="w-40 flex-shrink-0 text-gray-700 dark:text-gray-300 cursor-pointer">
              {{ fieldLabel(field) }}
            </label>
            <span class="flex-1 truncate font-mono text-[11px]" :class="sampleValue(field) ? 'text-gray-500 dark:text-gray-400' : 'text-gray-300 dark:text-gray-600 italic'">
              {{ sampleValue(field) || pt('NotSet') }}
            </span>
            <button
              class="text-gray-300 hover:text-red-500 flex-shrink-0"
              :title="pt('Remove')"
              @click="removeField(field)"
            >
              <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>
            </button>
          </div>
          <div v-if="!shownFields.length" class="px-3 py-4 text-center text-[11px] text-gray-400">
            {{ pt('NoFields') }}
          </div>
        </div>

        <!-- Add attribute -->
        <div v-if="addableFields.length" class="flex items-center gap-2 mt-2">
          <select
            v-model="fieldToAdd"
            class="text-xs border border-gray-200 dark:border-gray-700 dark:bg-gray-900 rounded px-2 py-1.5 focus:outline-none focus:border-gray-400"
          >
            <option value="">{{ pt('AddPlaceholder') }}</option>
            <option v-for="f in addableFields" :key="f" :value="f">{{ fieldLabel(f) }}</option>
          </select>
          <button
            class="px-2 py-1.5 text-xs text-gray-600 dark:text-gray-300 border border-gray-200 dark:border-gray-700 rounded hover:border-gray-300 disabled:opacity-40"
            :disabled="!fieldToAdd"
            @click="addField"
          >
            {{ pt('Add') }}
          </button>
        </div>
      </div>

      <!-- Save -->
      <div class="flex items-center gap-3 mt-4">
        <button
          class="px-3 py-1.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50"
          :disabled="saving"
          @click="handleSave"
        >
          {{ saving ? pt('Saving') : $t('common.save') }}
        </button>
        <span v-if="savedFlash" class="text-[11px] text-green-600">{{ pt('Saved') }}</span>
        <span v-if="error" class="text-[11px] text-red-500">{{ error }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useProfileSync } from '~/composables/useProfileSync'

const props = defineProps<{
  // i18n key prefix under settings.identityProvider, e.g. 'entra' | 'google'
  i18nPrefix: string
  // API endpoint base, e.g. '/api/organization/identity/entra-profile-sync'
  endpoint: string
  // Allowlist fallback when the preview hasn't loaded yet
  allowedFields: string[]
  // Fields shown as default rows when nothing is configured yet
  defaultVisible: string[]
}>()

const { t } = useI18n()

const open = ref(false)
const enabled = ref(false)
const shownFields = ref<string[]>([...props.defaultVisible])
const selected = ref<Set<string>>(new Set(props.defaultVisible))
const fieldToAdd = ref('')
const savedFlash = ref(false)

const {
  config,
  preview,
  previewLoading,
  saving,
  error,
  fetchConfig,
  save,
  fetchPreview,
} = useProfileSync(props.endpoint, props.allowedFields)

const pt = (suffix: string) => t(`settings.identityProvider.${props.i18nPrefix}${suffix}`)

const previewHint = computed(() => {
  const err = preview.value?.error
  if (err === `no_${props.i18nPrefix}_login`) return pt('NoLogin')
  if (err === 'reauth_required') return pt('Reauth')
  return pt('PreviewError')
})

const fieldLabel = (f: string) => {
  const key = `settings.identityProvider.field_${f}`
  const translated = t(key)
  return translated === key ? f : translated
}

const sampleValue = (f: string): string => {
  const v = preview.value?.samples?.[f]
  if (v === null || v === undefined || v === '') return ''
  if (typeof v === 'object') {
    return Object.entries(v)
      .filter(([, val]) => val !== null && val !== '')
      .map(([k, val]) => `${k}=${val}`)
      .join(', ')
  }
  return String(v)
}

// Allowlisted fields not currently shown → available to add.
const addableFields = computed(() =>
  (preview.value?.allowed_fields || props.allowedFields).filter(f => !shownFields.value.includes(f))
)

const toggleField = (f: string) => {
  const next = new Set(selected.value)
  if (next.has(f)) next.delete(f)
  else next.add(f)
  selected.value = next
}

const removeField = (f: string) => {
  shownFields.value = shownFields.value.filter(x => x !== f)
  const next = new Set(selected.value)
  next.delete(f)
  selected.value = next
}

const addField = () => {
  const f = fieldToAdd.value
  if (!f || shownFields.value.includes(f)) return
  shownFields.value = [...shownFields.value, f]
  selected.value = new Set([...selected.value, f])
  fieldToAdd.value = ''
}

const applyConfig = () => {
  if (!config.value) return
  enabled.value = !!config.value.enabled
  const configured = config.value.fields || []
  // Show configured fields plus the default set, so the admin always sees the
  // common attributes even before selecting any.
  const union = Array.from(new Set([...props.defaultVisible, ...configured]))
  shownFields.value = union
  selected.value = new Set(configured.length ? configured : props.defaultVisible)
}

const handleSave = async () => {
  savedFlash.value = false
  const fields = shownFields.value.filter(f => selected.value.has(f))
  const ok = await save({ enabled: enabled.value, fields })
  if (ok) {
    applyConfig()
    savedFlash.value = true
    setTimeout(() => { savedFlash.value = false }, 3000)
  }
}

onMounted(async () => {
  await fetchConfig()
  applyConfig()
  // Live sample values from the admin's own profile (best-effort).
  fetchPreview()
})
</script>
