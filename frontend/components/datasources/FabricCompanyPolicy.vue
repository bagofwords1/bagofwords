<template>
  <p v-if="!hasFeature('rls')" class="px-6 py-5 text-sm">{{ t('errors.license.enterprise_required') }}</p>
  <form v-else class="space-y-5 px-6 py-5 overflow-y-auto" @submit.prevent="save">
    <div>
      <h4 class="font-medium">{{ t('fabricCompany.mapping') }}</h4>
      <p class="text-sm text-ink-muted mt-1">{{ t('fabricCompany.scope') }}</p>
    </div>
    <fieldset :disabled="busy || loadFailed" class="space-y-4">
      <div class="grid gap-3 sm:grid-cols-2">
        <label v-for="field in fields" :key="field.key" class="text-sm">
          {{ t('fabricCompany.' + field.key) }}
          <input v-model.trim="draft[field.key]" required :placeholder="field.example" class="field">
        </label>
      </div>
      <h4 class="text-sm font-medium">{{ t('fabricCompany.tables') }}</h4>
      <div v-for="(rule, i) in draft.tables" :key="i" class="flex flex-wrap gap-2 items-end">
        <label v-for="field in ruleFields" :key="field.key" class="text-sm flex-1">
          {{ t('fabricCompany.' + field.key) }}
          <input v-model.trim="rule[field.key]" required :placeholder="field.example" class="field">
        </label>
        <UButton v-if="savedDraft === JSON.stringify(draft)" type="button" color="gray" @click="preview(rule.table)">{{ t('fabricCompany.preview') }}</UButton>
        <UButton v-if="draft.tables.length > 1" type="button" color="gray" icon="i-heroicons-trash"
          :aria-label="t('common.remove')" @click="draft.tables.splice(i, 1)" />
      </div>
      <UButton type="button" color="gray" variant="ghost" icon="i-heroicons-plus" @click="draft.tables.push(emptyRule())">{{ t('fabricCompany.add') }}</UButton>
    </fieldset>
    <section v-if="sample" class="overflow-x-auto text-sm" :aria-label="sample.table">
      <h4 class="mb-2 font-medium">{{ sample.table }}</h4>
      <table class="w-full text-start">
        <thead><tr><th v-for="(col, i) in sample.columns" :key="i" class="border-b p-2 text-start">{{ col }}</th></tr></thead>
        <tbody><tr v-for="(row, i) in sample.data" :key="i"><td v-for="(cell, j) in row" :key="j" class="border-b p-2">{{ cell }}</td></tr></tbody>
      </table>
      <p v-if="!sample.data.length">{{ t('queries.noRows') }}</p>
    </section>
    <div class="flex gap-3 items-center">
      <UButton type="submit" :disabled="busy || loadFailed">{{ t('fabricCompany.save') }}</UButton>
      <span role="status" class="text-sm">{{ message }}</span>
    </div>
  </form>
</template>

<script setup lang="ts">
import { useEnterprise } from '~/ee/composables/useEnterprise'
const { hasFeature } = useEnterprise()
const props = defineProps<{ connectionId: string }>()
const { t } = useI18n()
const fields = [
  { key: 'tenant_id', example: '00000000-0000-0000-0000-000000000001' },
  { key: 'grants_table', example: 'security.company_grants' },
  { key: 'employee_column', example: 'employee_email' },
  { key: 'company_column', example: 'company_permission' },
  { key: 'source_column', example: 'source_expression' },
] as const
const ruleFields = [
  { key: 'table', example: 'warehouse.sales' },
  { key: 'company_column', example: 'company_id' },
  { key: 'source', example: 'sales' },
] as const
const emptyRule = () => ({ table: '', company_column: '', source: '' })
const draft = reactive({ tenant_id: '', grants_table: '', employee_column: '', company_column: '', source_column: '', tables: [emptyRule()] })
const savedDraft = ref('')
const sample = ref<{ table: string, columns: string[], data: unknown[][] } | null>(null)
watch(draft, () => { sample.value = null }, { flush: 'sync' })
const busy = ref(true)
const loadFailed = ref(false)
const message = ref('')
const url = '/connections/' + encodeURIComponent(props.connectionId) + '/company-policy/draft'
onMounted(async () => {
  try {
    const { data, error } = await useMyFetch(url)
    if (error.value) throw error.value
    if (data.value) {
      Object.assign(draft, data.value)
      savedDraft.value = JSON.stringify(draft)
    }
  } catch {
    loadFailed.value = true
    message.value = t('fabricCompany.failed')
  } finally { busy.value = false }
})
async function save() {
  busy.value = true
  try {
    const { error } = await useMyFetch(url, { method: 'PUT', body: draft })
    if (error.value) throw error.value
    savedDraft.value = JSON.stringify(draft)
    message.value = t('fabricCompany.saved')
  } catch { message.value = t('fabricCompany.failed') }
  finally { busy.value = false }
}
async function preview(table: string) {
  busy.value = true
  message.value = ''
  sample.value = null
  try {
    const { data, error } = await useMyFetch(url + '/preview', { query: { table } })
    if (error.value || !data.value) throw error.value
    sample.value = { ...(data.value as { columns: string[], data: unknown[][] }), table }
  } catch { message.value = t('fabricCompany.previewFailed') }
  finally { busy.value = false }
}
</script>
<style scoped>
.field { @apply mt-1 block w-full rounded border border-gray-300 bg-transparent px-2 py-1.5 text-sm dark:border-gray-700; }
</style>
