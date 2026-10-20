<template>
  <div class="space-y-2" data-testid="member-attributes-editor">
    <div v-for="(row, idx) in rows" :key="row.id" class="flex items-start gap-2">
      <div class="flex-1 min-w-0">
        <UInput
          v-model="row.key"
          size="sm"
          :placeholder="$t('settings.members.attributeKeyPlaceholder')"
          :list="datalistId"
          :color="keyError(row) ? 'red' : 'white'"
          :aria-label="$t('settings.members.attributeKey')"
          :data-testid="`attr-key-${idx}`"
          @update:model-value="emitValue"
        />
        <p v-if="keyError(row)" class="text-[11px] text-red-500 mt-0.5">{{ keyError(row) }}</p>
      </div>
      <div class="flex-1 min-w-0">
        <UInput
          v-model="row.value"
          size="sm"
          maxlength="256"
          :placeholder="$t('settings.members.attributeValuePlaceholder')"
          :aria-label="$t('settings.members.attributeValue')"
          :data-testid="`attr-value-${idx}`"
          @update:model-value="emitValue"
        />
      </div>
      <button
        type="button"
        class="mt-1.5 text-gray-400 hover:text-red-500"
        :aria-label="$t('settings.members.attributeRemove')"
        @click="removeRow(idx)"
      >
        <UIcon name="heroicons-x-mark" class="w-4 h-4" />
      </button>
    </div>
    <datalist :id="datalistId">
      <option v-for="k in suggestions || []" :key="k" :value="k" />
    </datalist>
    <button
      type="button"
      class="inline-flex items-center gap-1 text-xs text-blue-600 hover:underline"
      data-testid="attr-add"
      @click="addRow"
    >
      <UIcon name="heroicons-plus" class="w-3.5 h-3.5" />
      {{ $t('settings.members.attributeAdd') }}
    </button>
  </div>
</template>

<script setup lang="ts">
// Key/value editor for admin-set member attributes. Mirrors the backend rules
// (services/member_attributes.py): identifier-like keys, single-line values;
// rows with an empty value are dropped on save (that is how one is removed).
const props = defineProps<{ modelValue: Record<string, string> | null | undefined; suggestions?: string[] }>()
const emit = defineEmits<{ (e: 'update:modelValue', value: Record<string, string>): void; (e: 'valid', ok: boolean): void }>()
const { t } = useI18n()

const KEY_RE = /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/
const datalistId = `member-attr-keys-${Math.random().toString(36).slice(2, 8)}`
let nextId = 0
const rows = ref<{ id: number; key: string; value: string }[]>([])

function load(value: Record<string, string> | null | undefined) {
  rows.value = Object.entries(value || {}).map(([key, v]) => ({ id: nextId++, key, value: String(v ?? '') }))
}
load(props.modelValue)

function keyError(row: { key: string; value: string }): string | null {
  const key = row.key.trim()
  if (!key) return row.value.trim() ? t('settings.members.attributeKeyRequired') : null
  if (!KEY_RE.test(key)) return t('settings.members.attributeKeyInvalid')
  const dupes = rows.value.filter(r => r.key.trim() === key).length
  return dupes > 1 ? t('settings.members.attributeKeyDuplicate') : null
}

function emitValue() {
  const out: Record<string, string> = {}
  for (const row of rows.value) {
    const key = row.key.trim()
    const value = row.value.trim()
    if (key && value) out[key] = value
  }
  emit('update:modelValue', out)
  emit('valid', rows.value.every(r => !keyError(r)))
}

function addRow() {
  rows.value.push({ id: nextId++, key: '', value: '' })
}

function removeRow(idx: number) {
  rows.value.splice(idx, 1)
  emitValue()
}
</script>
