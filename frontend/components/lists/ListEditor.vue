<!--
  Create / edit a list's schema: name, description and fields.

  A field's description is the agent's instruction for that value, so it gets
  a full-width line of its own. Names are normalized to snake_case as you type
  (they become column names in bow.<agent>.lists.<list> and in CSV exports).
  Field ids round-trip untouched, so a rename is not a delete + add.
-->
<template>
  <div class="flex flex-col h-full min-h-0" data-testid="list-editor">
    <div class="shrink-0 flex items-center gap-2 px-6 py-3 border-b border-gray-100 dark:border-gray-800">
      <button type="button" class="flex items-center gap-1.5 min-w-0 rounded px-1 -mx-1 hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="$emit('cancel')">
        <UIcon name="i-heroicons-arrow-left" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 shrink-0 rtl:rotate-180" />
        <span class="text-[13px] text-gray-500 dark:text-gray-400 truncate">{{ list ? list.name : $t('lists.back') }}</span>
      </button>
      <span class="text-[13px] font-medium text-gray-900 dark:text-white">{{ list ? $t('lists.editor.editTitle') : $t('lists.editor.newTitle') }}</span>
      <div class="ms-auto flex items-center gap-2">
        <button type="button" class="h-8 px-3 rounded-md text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="$emit('cancel')">{{ $t('lists.cancel') }}</button>
        <button
          type="button"
          data-testid="list-editor-save"
          :disabled="saving || !canSave"
          class="h-8 px-3 rounded-md bg-gray-900 dark:bg-white text-white dark:text-gray-900 text-xs font-medium hover:bg-gray-800 dark:hover:bg-gray-100 disabled:opacity-40 inline-flex items-center gap-1.5"
          @click="save"
        >
          <Spinner v-if="saving" class="w-3 h-3" />
          {{ list ? $t('lists.save') : $t('lists.create') }}
        </button>
      </div>
    </div>

    <div class="flex-1 min-h-0 overflow-y-auto px-6 py-5">
      <div class="max-w-3xl space-y-5">
        <div class="grid gap-4 sm:grid-cols-[1fr_2fr]">
          <label class="block">
            <span class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('lists.editor.name') }}</span>
            <input v-model="form.name" dir="auto" data-testid="list-name" type="text" :placeholder="$t('lists.editor.namePlaceholder')" :class="inputCls" />
          </label>
          <label class="block">
            <span class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('lists.editor.description') }}</span>
            <input v-model="form.description" dir="auto" data-testid="list-description" type="text" :placeholder="$t('lists.editor.descriptionPlaceholder')" :class="inputCls" />
          </label>
        </div>

        <div>
          <div class="flex items-center justify-between mb-2">
            <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('lists.editor.fields') }}</span>
            <span class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('lists.editor.nameRule') }}</span>
          </div>

          <div class="rounded-lg border border-gray-200 dark:border-gray-800 divide-y divide-gray-100 dark:divide-gray-800 bg-white dark:bg-gray-900">
            <div class="hidden sm:grid grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_auto_auto_28px] gap-2 px-3 py-1.5 text-[11px] text-gray-400 dark:text-gray-500">
              <span>{{ $t('lists.editor.name') }}</span>
              <span>{{ $t('lists.editor.type') }}</span>
              <span class="w-16 text-center">{{ $t('lists.editor.required') }}</span>
              <span class="w-12 text-center" :title="$t('lists.editor.keyHint')">{{ $t('lists.editor.key') }}</span>
              <span></span>
            </div>
            <div v-for="(f, i) in form.fields" :key="f._k" class="px-3 py-2.5 space-y-2" data-testid="list-field">
              <div class="grid grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_auto_auto_28px] gap-2 items-center">
                <input
                  :value="f.name"
                  data-testid="list-field-name"
                  type="text"
                  dir="ltr"
                  :placeholder="$t('lists.editor.fieldName')"
                  :class="[inputCls, 'font-mono']"
                  @input="f.name = normalizeName(($event.target as HTMLInputElement).value)"
                />
                <select v-model="f.type" data-testid="list-field-type" :class="inputCls">
                  <option v-for="t in TYPES" :key="t" :value="t">{{ $t('lists.editor.types.' + t) }}</option>
                </select>
                <label class="w-16 flex justify-center">
                  <input v-model="f.required" type="checkbox" data-testid="list-field-required" class="rounded border-gray-300 dark:border-gray-600 text-gray-900 focus:ring-0" />
                </label>
                <label class="w-12 flex justify-center" :title="$t('lists.editor.keyHint')">
                  <input
                    type="radio"
                    name="key-field"
                    data-testid="list-field-key"
                    :checked="form.key === f._k"
                    :disabled="!KEYABLE.includes(f.type)"
                    class="border-gray-300 dark:border-gray-600 text-gray-900 focus:ring-0 disabled:opacity-30"
                    @click="form.key = form.key === f._k ? null : f._k"
                  />
                </label>
                <button type="button" :title="$t('lists.editor.removeField')" :disabled="form.fields.length === 1" class="h-7 w-7 flex items-center justify-center rounded text-gray-400 hover:text-red-600 hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-30 disabled:hover:bg-transparent" @click="removeField(i)">
                  <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
                </button>
              </div>
              <input v-model="f.description" dir="auto" data-testid="list-field-description" type="text" :placeholder="$t('lists.editor.fieldDescription')" :class="[inputCls, 'text-gray-600 dark:text-gray-300']" />
              <input v-if="f.type === 'enum'" v-model="f.enumText" dir="auto" data-testid="list-field-enum" type="text" :placeholder="$t('lists.editor.enumValues')" :class="inputCls" />
            </div>
          </div>

          <button type="button" data-testid="list-add-field" class="mt-2 inline-flex items-center gap-1 h-7 px-2 rounded-md text-xs font-medium text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800/70" @click="addField">
            <UIcon name="i-heroicons-plus" class="w-3.5 h-3.5" />{{ $t('lists.editor.addField') }}
          </button>
        </div>

        <label class="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300">
          <input v-model="form.requireEvidence" type="checkbox" data-testid="list-require-evidence" class="rounded border-gray-300 dark:border-gray-600 text-gray-900 focus:ring-0" />
          {{ $t('lists.editor.requireEvidence') }}
        </label>
        <label class="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300 -mt-2">
          <input v-model="form.allowViewerSubmissions" type="checkbox" data-testid="list-allow-viewer-submissions" class="rounded border-gray-300 dark:border-gray-600 text-gray-900 focus:ring-0" />
          {{ $t('lists.editor.allowViewerSubmissions') }}
        </label>

        <div v-if="errors.length" class="rounded-md border border-red-200 dark:border-red-900/60 bg-red-50 dark:bg-red-950/30 px-3 py-2 text-xs text-red-700 dark:text-red-300 space-y-0.5" data-testid="list-editor-errors">
          <div v-for="(e, i) in errors" :key="i">{{ e }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import Spinner from '~/components/Spinner.vue'
import { useMyFetch } from '~/composables/useMyFetch'
import type { AgentList, ListField, ListFieldType } from '~/components/lists/types'

const props = defineProps<{ dsId: string; list: AgentList | null }>()
const emit = defineEmits<{ (e: 'cancel'): void; (e: 'saved', list: AgentList): void }>()

const { t } = useI18n()
const toast = useToast()
const TYPES: ListFieldType[] = ['string', 'number', 'integer', 'boolean', 'date', 'enum']
const KEYABLE: ListFieldType[] = ['string', 'integer', 'enum', 'date']
const inputCls = 'w-full h-8 px-2.5 text-xs bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400 placeholder:text-gray-400 dark:placeholder:text-gray-500'

type FormField = ListField & { _k: string; enumText: string }
let seq = 0
const toForm = (f: ListField): FormField => ({
  ...f, _k: f.id || `new-${++seq}`, description: f.description || '', required: !!f.required,
  enumText: (f.enum || []).join(', '),
})
const blank = (): FormField => toForm({ name: '', type: 'string', description: '', required: false })

const initialFields = props.list ? props.list.fields.map(toForm) : [blank()]
const form = reactive({
  name: props.list?.name || '',
  description: props.list?.description || '',
  fields: initialFields as FormField[],
  key: props.list?.key_field_id || null as string | null,
  requireEvidence: !!props.list?.require_evidence,
  allowViewerSubmissions: !!props.list?.allow_viewer_submissions,
})
const saving = ref(false)
const errors = ref<string[]>([])

const normalizeName = (v: string) => v.toLowerCase().replace(/[^a-z0-9_]+/g, '_').replace(/^[^a-z]+/, '').slice(0, 48)
const canSave = computed(() => form.name.trim() && form.fields.length && form.fields.every(f => f.name))

function addField() { form.fields.push(blank()) }
function removeField(i: number) {
  const [f] = form.fields.splice(i, 1)
  if (f && form.key === f._k) form.key = null
}

function payload() {
  const keyField = form.fields.find(f => f._k === form.key && KEYABLE.includes(f.type))
  return {
    name: form.name.trim(),
    description: form.description.trim(),
    require_evidence: form.requireEvidence,
    allow_viewer_submissions: form.allowViewerSubmissions,
    key_field: keyField ? keyField.name : null,
    fields: form.fields.map(f => ({
      ...(f.id ? { id: f.id } : {}),
      name: f.name,
      type: f.type,
      description: (f.description || '').trim(),
      required: !!f.required,
      enum: f.type === 'enum' ? f.enumText.split(',').map(s => s.trim()).filter(Boolean) : null,
      unit: f.unit || null,
      method: f.method || null,
    })),
  }
}

function extractErrors(err: any): string[] {
  const d = err?.data?.detail
  if (Array.isArray(d)) {
    return d.map((e: any) => {
      const loc = (e.loc || []).filter((p: any) => p !== 'body').join('.')
      const msg = String(e.msg || '').replace(/^Value error, /, '')
      return loc ? `${loc}: ${msg}` : msg
    })
  }
  if (d?.errors) return d.errors
  return [typeof d === 'string' ? d : t('lists.editor.saveFailed')]
}

async function save() {
  if (!canSave.value || saving.value) return
  saving.value = true
  errors.value = []
  try {
    const url = props.list
      ? `/api/data_sources/${props.dsId}/lists/${props.list.id}`
      : `/api/data_sources/${props.dsId}/lists`
    const { data, error } = await useMyFetch<AgentList>(url, { method: props.list ? 'PUT' : 'POST', body: payload() })
    if (error.value) { errors.value = extractErrors(error.value); return }
    const saved = data.value as AgentList
    if (saved.change === 'breaking') {
      toast.add({ title: t('lists.editor.saved'), description: t('lists.editor.breakingSaved', { v: saved.version }), color: 'amber' })
    } else {
      toast.add({ title: props.list ? t('lists.editor.saved') : t('lists.editor.created'), color: 'green' })
    }
    emit('saved', saved)
  } finally {
    saving.value = false
  }
}
</script>
