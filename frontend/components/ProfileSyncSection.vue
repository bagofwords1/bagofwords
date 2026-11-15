<template>
  <SettingsDisclosure v-model:open="open" :title="pt('Title')" :description="pt('Subtitle')">
    <template #status>{{ enabled ? $t('settings.identityProvider.on') : $t('settings.identityProvider.off') }}</template>

    <SettingsRow :label="pt('Enable')" :description="pt('EnableHint')">
      <UToggle v-model="enabled" />
    </SettingsRow>

    <!-- Fields to include in context -->
    <div v-show="enabled" class="mt-6">
      <div class="flex items-center justify-between mb-2">
        <span class="text-sm font-medium text-ink">{{ pt('FieldsLabel') }}</span>
        <Spinner v-if="previewLoading" class="w-3.5 h-3.5 text-ink-muted" />
      </div>

      <p v-if="preview && !preview.connected" class="text-xs text-amber-700 dark:text-amber-400 mb-2">
        {{ previewHint }}
      </p>

      <!-- Field rows: checkbox (in context) | label | sample value | remove -->
      <div class="rounded-lg border border-line divide-y divide-line-subtle overflow-hidden">
        <div
          v-for="field in shownFields"
          :key="field"
          class="group/field flex items-center gap-3 px-4 py-2.5 text-sm hover:bg-ink/[0.03]"
        >
          <UCheckbox
            :id="`${i18nPrefix}-field-${field}`"
            :model-value="selected.has(field)"
            @update:model-value="toggleField(field)"
          />
          <label :for="`${i18nPrefix}-field-${field}`" class="w-44 shrink-0 text-ink-2 cursor-pointer">
            {{ fieldLabel(field) }}
          </label>
          <span class="flex-1 truncate font-mono text-xs" :class="sampleValue(field) ? 'text-ink-muted' : 'text-ink-subtle'">
            {{ sampleValue(field) || pt('NotSet') }}
          </span>
          <UButton
            color="gray"
            variant="ghost"
            size="2xs"
            icon="i-heroicons-x-mark"
            class="opacity-0 group-hover/field:opacity-100 focus:opacity-100"
            :aria-label="pt('Remove')"
            @click="removeField(field)"
          />
        </div>
        <div v-if="!shownFields.length" class="px-4 py-6 text-center text-sm text-ink-muted">
          {{ pt('NoFields') }}
        </div>
      </div>

      <!-- Add attribute -->
      <div v-if="addableFields.length" class="flex items-center gap-2 mt-3">
        <USelect
          v-model="fieldToAdd"
          size="sm"
          class="w-56"
          :options="[{ value: '', label: pt('AddPlaceholder') }, ...addableFields.map(f => ({ value: f, label: fieldLabel(f) }))]"
          option-attribute="label"
          value-attribute="value"
        />
        <UButton size="sm" color="gray" icon="i-heroicons-plus" :disabled="!fieldToAdd" @click="addField">
          {{ pt('Add') }}
        </UButton>
      </div>
    </div>

    <!-- Save -->
    <div class="flex items-center gap-3 mt-6">
      <UButton color="blue" :loading="saving" :disabled="saving" @click="handleSave">
        {{ saving ? pt('Saving') : $t('common.save') }}
      </UButton>
      <span v-if="savedFlash" class="inline-flex items-center gap-1 text-xs text-green-700 dark:text-green-400">
        <UIcon name="i-heroicons-check" class="w-3.5 h-3.5" />{{ pt('Saved') }}
      </span>
      <span v-if="error" class="text-xs text-red-600 dark:text-red-400">{{ error }}</span>
    </div>
  </SettingsDisclosure>
</template>

<script setup lang="ts">
import { useProfileSync } from '~/composables/useProfileSync'
import SettingsDisclosure from '~/components/settings/SettingsDisclosure.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import Spinner from '~/components/Spinner.vue'

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
