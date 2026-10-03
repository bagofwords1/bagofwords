<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }">
    <div class="p-5" data-testid="stream-form">
      <div class="flex items-center justify-between mb-4">
        <div class="flex items-center gap-2">
          <button
            v-if="!stream && destination"
            type="button"
            class="text-gray-400 hover:text-gray-600"
            :aria-label="$t('settings.audit.streams.form.back')"
            @click="destination = null"
          >
            <UIcon name="i-heroicons-arrow-left" class="w-4 h-4 rtl:-scale-x-100" />
          </button>
          <h3 class="text-base font-semibold">{{ stream ? $t('settings.audit.streams.form.titleEdit') : $t('settings.audit.streams.form.titleNew') }}</h3>
        </div>
        <button type="button" class="text-gray-400 hover:text-gray-600" :aria-label="$t('common.close')" @click="isOpen = false">
          <UIcon name="i-heroicons-x-mark" class="w-5 h-5" />
        </button>
      </div>

      <!-- Step 1: destination -->
      <div v-if="!destination">
        <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">{{ $t('settings.audit.streams.form.chooseDestination') }}</p>
        <div class="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
          <button
            v-for="d in specs"
            :key="d.type"
            type="button"
            :data-testid="`stream-destination-${d.type}`"
            class="flex items-start gap-2.5 p-3 rounded-lg border border-gray-100 dark:border-gray-800 hover:border-blue-200 hover:bg-gray-50 dark:hover:bg-gray-800 text-start"
            @click="chooseDestination(d.type)"
          >
            <AuditStreamIcon :destination="d.type" size="md" />
            <span class="min-w-0">
              <span class="block text-sm text-gray-800 dark:text-gray-100">{{ $t(`settings.audit.streams.destinations.${d.type}`) }}</span>
              <span class="block text-[11px] text-gray-400">{{ $t(`settings.audit.streams.destinationHints.${d.type}`) }}</span>
            </span>
          </button>
        </div>
      </div>

      <!-- Step 2: settings -->
      <form v-else class="space-y-4" @submit.prevent="save">
        <div class="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
          <AuditStreamIcon :destination="destination" />
          {{ $t(`settings.audit.streams.destinations.${destination}`) }}
          <span class="text-xs text-gray-400">· {{ $t(`settings.audit.streams.destinationHints.${destination}`) }}</span>
        </div>

        <div>
          <label class="form-label">{{ $t('settings.audit.streams.form.name') }}</label>
          <UInput v-model="name" size="sm" data-testid="stream-name" :placeholder="$t('settings.audit.streams.form.namePlaceholder')" />
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div v-for="f in basicFields" :key="f.key" :class="wide(f) ? 'sm:col-span-2' : ''">
            <FieldInput :f="f" />
          </div>
        </div>

        <div v-if="!stream" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="form-label">{{ $t('settings.audit.streams.form.startFrom') }}</label>
            <div class="flex gap-1.5">
              <button
                v-for="opt in (['now', 'beginning'] as const)"
                :key="opt"
                type="button"
                :data-testid="`stream-start-${opt}`"
                class="px-2.5 py-1 text-xs rounded border"
                :class="startFrom === opt ? 'bg-blue-50 dark:bg-blue-950 border-blue-300 text-blue-700 dark:text-blue-300' : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400'"
                @click="startFrom = opt"
              >{{ opt === 'now' ? $t('settings.audit.streams.form.startNow') : $t('settings.audit.streams.form.startBeginning') }}</button>
            </div>
          </div>
        </div>

        <div>
          <label class="form-label">{{ $t('settings.audit.streams.form.actionFilter') }}</label>
          <UInput v-model="actionFilter" size="sm" data-testid="stream-action-filter" placeholder="tool., member." />
          <p class="text-[11px] text-gray-400 mt-1">{{ $t('settings.audit.streams.form.actionFilterHint') }}</p>
        </div>

        <div v-if="advancedFields.length">
          <button type="button" class="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1" data-testid="stream-advanced-toggle" @click="showAdvanced = !showAdvanced">
            <UIcon name="i-heroicons-chevron-right" class="w-3 h-3 transition-transform rtl:-scale-x-100" :class="showAdvanced ? 'rotate-90' : ''" />
            {{ $t('settings.audit.streams.form.advanced') }}
          </button>
          <div v-if="showAdvanced" class="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-3">
            <div v-for="f in advancedFields" :key="f.key" :class="wide(f) ? 'sm:col-span-2' : ''">
              <FieldInput :f="f" />
            </div>
          </div>
        </div>

        <div v-if="externalId" class="rounded border border-blue-100 dark:border-blue-900 bg-blue-50/60 dark:bg-blue-950/40 p-2.5 text-xs">
          <div class="text-blue-800 dark:text-blue-200">{{ $t('settings.audit.streams.form.externalIdHint') }}</div>
          <code class="font-mono text-blue-900 dark:text-blue-100" dir="ltr">{{ externalId }}</code>
        </div>

        <div
          v-if="testResult"
          data-testid="stream-test-result"
          class="rounded px-2.5 py-2 text-xs flex items-start gap-1.5"
          :class="testResult.ok ? 'bg-green-50 dark:bg-green-950 text-green-700 dark:text-green-300' : 'bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-300'"
        >
          <UIcon :name="testResult.ok ? 'i-heroicons-check-circle' : 'i-heroicons-exclamation-triangle'" class="w-4 h-4 shrink-0" />
          <span class="break-all">{{ testResult.ok ? $t('settings.audit.streams.form.testOk') : $t('settings.audit.streams.form.testFailed', { error: testResult.error || testResult.kind }) }}</span>
        </div>
        <div v-if="formError" class="text-xs text-red-600" data-testid="stream-form-error">{{ formError }}</div>

        <div class="flex items-center justify-between pt-1">
          <UButton size="xs" color="gray" variant="soft" icon="i-heroicons-paper-airplane" :loading="testing" data-testid="stream-test" @click="runTest">
            {{ testing ? $t('settings.audit.streams.form.testing') : $t('settings.audit.streams.form.test') }}
          </UButton>
          <div class="flex gap-2">
            <UButton size="xs" color="gray" variant="ghost" @click="isOpen = false">{{ $t('settings.audit.streams.form.cancel') }}</UButton>
            <UButton size="xs" type="submit" :loading="saving" :disabled="!name.trim()" data-testid="stream-save">
              {{ saving ? $t('settings.audit.streams.form.saving') : $t('settings.audit.streams.form.save') }}
            </UButton>
          </div>
        </div>
      </form>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { defineComponent, h, resolveComponent } from 'vue'
import AuditStreamIcon from '~/components/audit/AuditStreamIcon.vue'
import { useAuditStreams, type AuditStream, type DestinationField, type DestinationSpec, type StreamDestination, type StreamTestResult } from '~/ee/composables/useAuditStreams'

const props = defineProps<{ modelValue: boolean; stream: AuditStream | null; specs: DestinationSpec[] }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void; (e: 'saved', s: AuditStream): void }>()

const { t } = useI18n({ useScope: 'global' })
const { createStream, updateStream, testStream, getErrorMessage } = useAuditStreams()

const isOpen = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })
const destination = ref<StreamDestination | null>(null)
const name = ref('')
const values = reactive<Record<string, any>>({})
const startFrom = ref<'now' | 'beginning'>('now')
const actionFilter = ref('')
const showAdvanced = ref(false)
const testing = ref(false)
const saving = ref(false)
const testResult = ref<StreamTestResult | null>(null)
const formError = ref<string | null>(null)

const spec = computed(() => props.specs.find((s) => s.type === destination.value))
const basicFields = computed(() => (spec.value?.fields || []).filter((f) => !f.advanced))
const advancedFields = computed(() => (spec.value?.fields || []).filter((f) => f.advanced))
const externalId = computed(() => (destination.value === 's3' && props.stream?.config?.role_arn ? props.stream.config.external_id : null))
const wide = (f: DestinationField) => ['textarea', 'headers', 'url'].includes(f.kind)

function reset() {
  for (const k of Object.keys(values)) delete values[k]
  testResult.value = null
  formError.value = null
  showAdvanced.value = false
}

function chooseDestination(d: StreamDestination) {
  reset()
  destination.value = d
  for (const f of spec.value?.fields || []) {
    values[f.key] = f.default ?? (f.kind === 'bool' ? false : '')
    if (f.kind === 'headers') values[f.key] = ''
  }
  if (!name.value) name.value = t(`settings.audit.streams.destinations.${d}`)
}

watch(() => [props.modelValue, props.stream] as const, ([open, s]) => {
  if (!open) return
  reset()
  if (s) {
    destination.value = s.destination
    name.value = s.name
    actionFilter.value = (s.action_filter || []).join(', ')
    for (const f of spec.value?.fields || []) {
      if (f.secret) values[f.key] = s.secrets?.[f.key] || ''
      else if (f.kind === 'headers') values[f.key] = s.config?.[f.key] ? JSON.stringify(s.config[f.key]) : ''
      else values[f.key] = s.config?.[f.key] ?? f.default ?? (f.kind === 'bool' ? false : '')
    }
  } else {
    destination.value = null
    name.value = ''
    actionFilter.value = ''
    startFrom.value = 'now'
  }
}, { immediate: true })

function split() {
  const config: Record<string, any> = {}
  const secrets: Record<string, any> = {}
  for (const f of spec.value?.fields || []) {
    let v = values[f.key]
    if (f.kind === 'headers') {
      if (!v) continue
      try { v = JSON.parse(v) } catch { throw new Error(t('errors.audit_stream.invalid_field', { field: t(`settings.audit.streams.fields.${f.key}`) })) }
    }
    if (f.kind === 'number' && v !== '' && v !== null) v = Number(v)
    ;(f.secret ? secrets : config)[f.key] = v
  }
  return { config, secrets }
}

const filterList = () => actionFilter.value.split(',').map((s) => s.trim()).filter(Boolean)

async function runTest() {
  formError.value = null
  testResult.value = null
  testing.value = true
  try {
    const { config, secrets } = split()
    testResult.value = await testStream({ destination: destination.value!, config, secrets, stream_id: props.stream?.id })
  } catch (e: any) {
    formError.value = e?.message && !e?.data ? e.message : getErrorMessage(e)
  } finally {
    testing.value = false
  }
}

async function save() {
  formError.value = null
  saving.value = true
  try {
    const { config, secrets } = split()
    const saved = props.stream
      ? await updateStream(props.stream.id, { name: name.value.trim(), config, secrets, action_filter: filterList() })
      : await createStream({ name: name.value.trim(), destination: destination.value!, config, secrets, action_filter: filterList(), start_from: startFrom.value, activate: true })
    emit('saved', saved)
    isOpen.value = false
  } catch (e: any) {
    formError.value = e?.message && !e?.data ? e.message : getErrorMessage(e)
  } finally {
    saving.value = false
  }
}

// One input per declared field; the backend's destination schema drives the form.
const FieldInput = defineComponent({
  props: { f: { type: Object as () => DestinationField, required: true } },
  setup(p) {
    return () => {
      const f = p.f
      const label = h('label', { class: 'form-label' }, [
        t(`settings.audit.streams.fields.${f.key}`),
        f.required && f.default == null ? h('span', { class: 'text-red-500 ms-0.5', title: t('settings.audit.streams.form.required') }, '*') : null,
      ])
      const testid = f.secret ? `stream-secret-${f.key}` : `stream-field-${f.key}`
      const common = 'w-full px-2.5 py-1.5 text-sm border border-gray-200 dark:border-gray-700 rounded-md bg-white dark:bg-gray-900 focus:outline-none focus:border-blue-400'
      let input
      if (f.kind === 'bool') {
        input = h(resolveComponent('UToggle') as any, { modelValue: !!values[f.key], 'onUpdate:modelValue': (v: boolean) => (values[f.key] = v), 'data-testid': testid })
      } else if (f.kind === 'select') {
        input = h('select', { class: common, value: values[f.key], 'data-testid': testid, onChange: (e: any) => (values[f.key] = e.target.value) },
          f.options.map((o) => h('option', { value: o }, o)))
      } else if (f.kind === 'textarea' || f.kind === 'headers') {
        input = h('textarea', {
          class: common + ' font-mono text-xs', rows: f.kind === 'headers' ? 2 : 4, dir: 'ltr', value: values[f.key], 'data-testid': testid,
          placeholder: f.kind === 'headers' ? '{"X-Env": "prod"}' : (f.secret && props.stream?.secrets?.[f.key] ? t('settings.audit.streams.form.secretStored') : ''),
          onInput: (e: any) => (values[f.key] = e.target.value),
        })
      } else {
        input = h('input', {
          class: common, dir: 'ltr', value: values[f.key], 'data-testid': testid,
          type: f.secret ? 'password' : f.kind === 'number' ? 'number' : 'text',
          autocomplete: f.secret ? 'new-password' : 'off',
          placeholder: f.secret && props.stream?.secrets?.[f.key] ? t('settings.audit.streams.form.secretStored') : (f.default != null && f.kind !== 'number' ? String(f.default) : ''),
          onInput: (e: any) => (values[f.key] = e.target.value),
        })
      }
      return h('div', [label, f.kind === 'bool' ? h('div', { class: 'pt-1' }, [input]) : input])
    }
  },
})
</script>

<style scoped>
:deep(.form-label) {
  @apply block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1;
}
.form-label {
  @apply block text-xs font-medium text-gray-600 dark:text-gray-300 mb-1;
}
</style>
