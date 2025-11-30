<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-6xl' }" :prevent-close="showCreateSuiteModal">
        <UCard>
            <template #header>
                <div class="flex items-center justify-between">
                    <h3 class="text-lg font-semibold text-gray-900">{{ isEditing ? 'Edit Test Case' : 'Add Test Case' }}</h3>
                    <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="close" />
                </div>
                <div class="mt-2">
                    <div class="flex flex-col md:flex-row md:items-center gap-2">
                        <div class="text-xs text-gray-600 md:w-20">Suite</div>
                        <div class="flex-1">
                            <USelectMenu
                                v-model="selectedSuiteIdLocal"
                                :options="suiteOptions"
                                option-attribute="label"
                                value-attribute="value"
                                size="xs"
                                class="text-xs w-full md:w-64"
                                @change="onSuiteMenuChanged"
                            >
                                <template #option="{ option }">
                                    <div class="text-xs truncate">{{ option.label }}</div>
                                </template>
                            </USelectMenu>
                        </div>
                    </div>
                    <div class="text-[11px] text-gray-400 mt-1">
                        Select an existing suite{{ isEditing ? '' : ' or choose "Create New Suite…" to add one' }}.
                    </div>
                </div>
            </template>

            <div class="max-h-[62vh] overflow-hidden pr-1">
                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 min-h-[420px]">
                <!-- Left: Prompt -->
                <div class="border border-gray-200 rounded-lg overflow-hidden">
                    <div class="px-3 py-2 border-b border-gray-200 text-xs text-gray-600">Prompt</div>
                    <div class="p-2">
                        <TestPromptBox
                            :textareaContent="promptText"
                            :selectedDataSources="testSelectedDataSources"
                            @update:modelValue="(v:string) => promptText = v"
                            @update:selectedDataSources="(v:any[]) => testSelectedDataSources = v"
                            @update:selectedModelId="(v:string) => testSelectedModelId = v"
                            @update:uploadedFiles="(v:any[]) => testUploadedFiles = v"
                            @update:mentions="(v:any[]) => testMentions = v"
                        />
                    </div>
                </div>

                <!-- Right: Expectations Builder -->
                <div class="border border-gray-100 rounded-lg overflow-hidden flex flex-col max-h-[58vh]">
                    <div class="px-3 py-2 border-b border-gray-100 text-xs text-gray-700">Expectations</div>
                    <div class="p-3 flex-1 flex flex-col space-y-3 overflow-y-auto">
                        <div v-if="categoryRules.length === 0" class="p-6 text-center">
                          <div class="text-sm font-medium text-gray-800 mb-1">No rules yet</div>
                          <div class="text-[11px] text-gray-500 mb-3">Define expectations for your test. Add your first rule to get started.</div>
                          <div class="flex items-center justify-center">
                            <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-plus" @click="addCategory">Add rule</UButton>
                          </div>
                          <div class="text-[11px] text-gray-500 mt-2" v-if="catalogLoading">Loading catalog…</div>
                        </div>
                        <template v-else>
                          <div class="flex items-center gap-2">
                              <UButton color="blue" size="xs" variant="soft" icon="i-heroicons-plus" @click="addCategory">Add rule</UButton>
                              <div class="text-[11px] text-gray-500 ml-auto" v-if="catalogLoading">Loading catalog…</div>
                          </div>
                          <!-- Category list -->
                          <div class="space-y-3">
                            <div v-for="cat in categoryRules" :key="cat.key" class="rounded-md border border-blue-200">
                            <!-- Header: Category anchor + remove -->
                            <div class="flex items-center gap-2 px-3 py-2">
                              <div class="w-56">
                                <USelectMenu
                                  v-model="cat.categoryId"
                                  :options="categoryOptions"
                                  option-attribute="label"
                                  value-attribute="id"
                                  size="xs"
                                  class="text-xs w-32"
                                  :ui="{ content: 'w-56' }"
                                  :uiMenu="{
                                    base: 'w-56',
                                  }"
                                  @change="() => onChangeCategory(cat)"
                                >
                                  <template #option="{ option }">
                                    <div class="text-xs truncate">{{ option.label }}</div>
                                  </template>
                                </USelectMenu>
                              </div>
                              <div class="ml-auto">
                                <UButton color="gray" variant="ghost" icon="i-heroicons-trash" @click="removeCategory(cat.key)" />
                              </div>
                            </div>

                            <!-- Category helper text -->
                            <div class="px-3 pb-1 text-[11px] text-gray-500">
                              {{ categoryShortHelp(cat.categoryId) }}
                            </div>

                            <!-- Field rows -->
                            <div v-if="cat.categoryId === 'judge'" class="px-3 pb-3 space-y-3">
                              <!-- Judge prompt textarea -->
                              <div class="space-y-1">
                                <div class="text-[11px] text-gray-500 mb-1">Prompt</div>
                                <textarea
                                  v-model="(getJudgeRule(cat, 'prompt').matcher as any).value"
                                  rows="4"
                                  class="border border-gray-300 rounded px-2 py-1 text-xs w-full"
                                  placeholder="Write the evaluation prompt..."
                                />
                              </div>
                              <div class="space-y-1">
                                <div class="text-[11px] text-gray-500 mb-1">Output</div>
                                <div class="flex items-center gap-2">
                                  <span class="bg-green-100 text-green-800 text-xs px-2 py-1 rounded-md">Pass</span>
                                  <span class="bg-red-100 text-red-800 text-xs px-2 py-1 rounded-md">Fail</span>
                                </div>
                              </div>
                              <!-- Judge model selector (popover UI like PromptBoxV2) -->
                              <div class="space-y-1">
                                <div class="text-[11px] text-gray-500 mb-1">Model</div>
                                <UPopover>
                                  <UTooltip :text="judgeSelectedModelLabel(cat)" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
                                    <button class="text-gray-600 hover:text-gray-900 hover:bg-gray-50 rounded-md px-2 py-1 text-xs flex items-center border border-gray-200">
                                      <Icon name="heroicons-cpu-chip" class="w-4 h-4" />
                                      <span class="ml-1 truncate max-w-[240px] text-left">{{ judgeSelectedModelLabel(cat) }}</span>
                                    </button>
                                  </UTooltip>
                                  <template #panel="{ close }">
                                    <div class="p-2 text-xs max-h-64 overflow-y-auto w-[260px]">
                                      <div v-for="m in judgeModels" :key="m.id || m.model_id" class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center" @click="() => { setJudgeModel(cat, m); close(); }">
                                        <div class="mr-2">
                                          <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :icon="true" class="w-4 h-4" />
                                        </div>
                                        <div class="flex flex-col flex-1 text-left min-w-0">
                                          <span class="font-medium truncate">{{ m.name || m.model_id }}</span>
                                          <span class="text-gray-500 text-[10px] truncate">{{ m.provider?.name || m.provider_name || '' }}</span>
                                        </div>
                                        <Icon v-if="(getJudgeRule(cat, 'model_id').matcher as any).value === (m.model_id || m.value)" name="heroicons-check" class="w-4 h-4 text-blue-500 ml-2 flex-shrink-0" />
                                      </div>
                                    </div>
                                  </template>
                                </UPopover>
                              </div>
                            </div>
                            <div v-else class="px-3 pb-3 space-y-2">
                              <div v-for="fr in cat.fieldRules" :key="fr.key" class="grid grid-cols-1 md:grid-cols-8 gap-2 items-center">
                                <!-- Field -->
                                <div class="md:col-span-2">
                                  <div class="text-[11px] text-gray-500 mb-1">Field</div>
                                  <USelectMenu
                                    v-model="fr.target.field"
                                    :options="fieldOptionsForCategory(cat.categoryId)"
                                    option-attribute="label"
                                    value-attribute="key"
                                    size="xs"
                                    class="text-xs"
                                    :ui="{ width: 'w-72' }"
                                    :uiMenu="{
                                      base: 'w-56',
                                    }"
                                    @change="() => onChangeField(cat, fr)"
                                  >
                                    <template #option="{ option }">
                                      <div class="text-xs truncate">{{ option.label }}</div>
                                    </template>
                                  </USelectMenu>
                                </div>
                                <!-- Op -->
                                <div class="md:col-span-2">
                                  <div class="text-[11px] text-gray-500 mb-1">Operator</div>
                                  <USelectMenu
                                    v-model="(fr.matcher as any).type"
                                    :options="opOptionsFor(cat, fr)"
                                    option-attribute="label"
                                    value-attribute="value"
                                    size="xs"
                                    class="text-xs"
                                    :ui="{ width: 'w-72' }"
                                    :uiMenu="{
                                      base: 'w-56',
                                    }"
                                    @change="() => onChangeOp(fr)"
                                  >
                                    <template #option="{ option }">
                                      <div class="text-xs truncate">{{ option.label }}</div>
                                    </template>
                                  </USelectMenu>
                                </div>
                                <!-- Value editor -->
                                <div class="md:col-span-3">
                                  <div class="text-[11px] text-gray-500 mb-1">Value</div>
                                  <div v-if="(fr.matcher as any).type === 'number.cmp' || (fr.matcher as any).type === 'length.cmp'" class="flex items-center gap-2">
                                    <USelectMenu
                                      v-model="(fr.matcher as any).op"
                                      :options="cmpOps"
                                      option-attribute="label"
                                      value-attribute="value"
                                      size="xs"
                                      class="text-xs"
                                    >
                                      <template #option="{ option }">
                                        <div class="text-xs truncate">{{ option.label }}</div>
                                      </template>
                                    </USelectMenu>
                                    <input type="number" v-model.number="(fr.matcher as any).value" class="border border-gray-300 rounded px-2 py-1 text-xs w-full" />
                                  </div>
                                  <USelectMenu
                                    v-else-if="getFieldMeta(cat.categoryId, fr.target.field)?.options?.length && (fr.matcher as any).type !== 'text.regex' && (fr.matcher as any).type !== 'list.contains_any' && (fr.matcher as any).type !== 'list.contains_all'"
                                    v-model="(fr.matcher as any).value"
                                    :options="getFieldMeta(cat.categoryId, fr.target.field)?.options || []"
                                    option-attribute="label"
                                    value-attribute="value"
                                    size="xs"
                                    class="text-xs w-full"
                                   >
                                     <template #option="{ option }">
                                       <div class="text-xs truncate">{{ option.label }}</div>
                                     </template>
                                   </USelectMenu>
                                  <input v-else-if="(fr.matcher as any).type === 'text.regex'" type="text" v-model="(fr.matcher as any).pattern" class="border border-gray-300 rounded px-2 py-1 text-xs w-full" placeholder="/pattern/" />
                                  <input v-else-if="(fr.matcher as any).type === 'list.contains_any' || (fr.matcher as any).type === 'list.contains_all'" type="text" v-model="fr.valuesComma" @change="onValuesCommaChange(fr)" class="border border-gray-300 rounded px-2 py-1 text-xs w-full" placeholder="apple, banana" />
                                  <input v-else type="text" v-model="(fr.matcher as any).value" class="border border-gray-300 rounded px-2 py-1 text-xs w-full" />
                                </div>
                                <div class="md:col-span-1 flex items-end justify-end h-full">
                                  <UButton color="gray" size="xs" variant="ghost" icon="i-heroicons-trash" @click="removeField(cat, fr.key)" />
                                </div>
                              </div>
                              <div class="pt-1" v-if="cat.categoryId !== 'judge'">
                                <UButton color="gray" variant="soft" size="xs" icon="i-heroicons-plus" @click="addField(cat)">Add condition</UButton>
                              </div>
                            </div>
                          </div>
                          </div>
                        </template>
                    </div>
                </div>
                </div>
            </div>

            <template #footer>
                <div class="flex items-center justify-end space-x-2">
                    <UButton color="gray" variant="soft" @click="close">Cancel</UButton>
                    <UButton :loading="isSaving" color="blue" @click="save">Save</UButton>
                    <UButton :loading="isRunning" color="blue" variant="soft" @click="runNow">Save and Run</UButton>
                </div>
            </template>
        </UCard>
    </UModal>
    <!-- Create Suite small modal (teleported to body to avoid nested modal close issues) -->
    <Teleport to="body">
        <UModal v-model="showCreateSuiteModal" :ui="{ width: 'sm:max-w-md' }" :prevent-close="true">
            <UCard>
                <template #header>
                    <div class="flex items-center justify-between">
                        <h3 class="text-base font-semibold text-gray-900">Create Suite</h3>
                        <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="showCreateSuiteModal = false" />
                    </div>
                </template>
                <div class="space-y-2">
                    <label class="text-xs text-gray-600">Name</label>
                    <input
                        v-model="createSuiteName"
                        type="text"
                        placeholder="Suite name"
                        class="border border-gray-300 rounded px-2 py-1 text-xs w-full"
                    />
                </div>
                <template #footer>
                    <div class="flex items-center justify-end gap-2">
                        <UButton color="gray" variant="soft" @click="showCreateSuiteModal = false">Cancel</UButton>
                        <UButton :loading="createSuiteLoading" class="!bg-blue-500 !text-white" @click="createSuite">Create</UButton>
                    </div>
                </template>
            </UCard>
        </UModal>
    </Teleport>
</template>

<script setup lang="ts">
import TestPromptBox from '~/components/monitoring/TestPromptBox.vue'
import LLMProviderIcon from '~/components/LLMProviderIcon.vue'

const props = defineProps<{ modelValue: boolean, suiteId: string, caseId?: string }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void; (e: 'created', payload: any): void }>()

const isOpen = computed({ get: () => props.modelValue, set: (v) => emit('update:modelValue', v) })
const isEditing = computed(() => !!props.caseId)
const promptText = ref('')
const isSaving = ref(false)
const isRunning = ref(false)
const router = useRouter()
// Suites
const suitesLoading = ref(false)
const suites = ref<Array<{ id: string, name: string }>>([])
const selectedSuiteIdLocal = ref<string>(props.suiteId || '')
const suiteOptions = computed(() => {
  const base = (suites.value || []).map(s => ({ label: s.name, value: s.id }))
  return [...base, { label: 'Create New Suite…', value: '__create__' }]
})
const showCreateSuiteModal = ref(false)
const createSuiteName = ref('')
const createSuiteLoading = ref(false)
// Test prompt context
const testSelectedDataSources = ref<any[]>([])
const testSelectedModelId = ref<string>('')
const testUploadedFiles = ref<any[]>([])
const testMentions = ref<any[]>([])
// Catalog and targets (Category → Field)
type AllowedOp = 'text.contains' | 'text.not_contains' | 'text.equals' | 'text.regex' | 'number.cmp' | 'list.contains' | 'list.contains_any' | 'list.contains_all' | 'length.cmp'
type ValueType = 'text' | 'number' | 'list<string>' | 'list<object>' | 'object'
type SelectOption = { label: string, value: any }
type FieldDescriptor = { key: string, label: string, value_type: ValueType, allowed_ops: AllowedOp[], io?: 'input'|'output', examples?: any[], options?: SelectOption[] }
type CategoryDescriptor = { id: string, label: string, kind: 'tool'|'metadata'|'completion', tool_name?: string, fields: FieldDescriptor[] }
type TestCatalog = { categories: CategoryDescriptor[] }

const catalogLoading = ref(false)
const categories = ref<CategoryDescriptor[]>([])
const categoryById = computed(() => Object.fromEntries(categories.value.map(c => [c.id, c])))

// Field rules state
type Matcher = any
type FieldRuleUI = {
  key: string
  categoryKind: CategoryDescriptor['kind']
  target: { category: string, field: string, occurrence?: number }
  allowedOps: AllowedOp[]
  matcher: Matcher
  valuesComma?: string // for contains_any/all editing convenience
}

type CategoryRuleUI = {
  key: string
  categoryId: string
  categoryKind: CategoryDescriptor['kind']
  fieldRules: FieldRuleUI[]
}

const categoryRules = ref<CategoryRuleUI[]>([])
const judgeModels = ref<any[]>([])

const cmpOps = [
  { label: '>', value: 'gt' },
  { label: '≥', value: 'gte' },
  { label: '<', value: 'lt' },
  { label: '≤', value: 'lte' },
  { label: '=', value: 'eq' },
  { label: '≠', value: 'ne' },
]

const categoryOptions = computed(() => (categories.value || []).map(c => ({ id: c.id, label: c.label })))
const categoryShortHelp = (categoryId: string) => {
  if (categoryId === 'judge') return 'Will use the full trace, and output pass/fail'
  return 'The following rules will pass if any of the generated widgets/data will pass'
}

const fieldOptionsForCategory = (categoryId: string) => {
  const cat = categoryById.value[categoryId]
  if (!cat) return [] as Array<{ key: string; label: string }>
  return cat.fields.map(f => ({ key: f.key, label: f.label }))
}

const getFieldMeta = (categoryId: string, fieldKey: string): FieldDescriptor | undefined => {
  const cat = categoryById.value[categoryId]
  return cat?.fields.find(f => f.key === fieldKey)
}

const opOptionsFor = (cat: CategoryRuleUI, r: FieldRuleUI) => {
  const catMeta = categoryById.value[cat.categoryId]
  const field = catMeta?.fields.find(f => f.key === r.target.field)
  const ops = field?.allowed_ops || []
  const labelFor = (op: AllowedOp) => {
    if (op === 'text.contains') return 'text contains'
    if (op === 'text.not_contains') return 'text not contains'
    if (op === 'text.equals') return 'text equals'
    if (op === 'text.regex') return 'text matches regex'
    if (op === 'number.cmp') return 'number compare'
    if (op === 'length.cmp') return 'length compare'
    if (op === 'list.contains') return 'list contains value'
    if (op === 'list.contains_any') return 'list contains any'
    if (op === 'list.contains_all') return 'list contains all'
    return op
  }
  return ops.map(op => ({ label: labelFor(op), value: op }))
}

// Create missing categories/fields dynamically for legacy or custom rules so edit works
function ensureDynamicCategoryAndField(categoryId: string, fieldKey: string, matcherType?: string) {
  // If category exists and field exists, nothing to do
  const existingCat = (categories.value || []).find(c => c.id === categoryId)
  if (existingCat) {
    const fieldExists = (existingCat.fields || []).some(f => f.key === fieldKey)
    if (fieldExists) return
  }
  // Build a new or augmented category descriptor
  const deriveKind = (catId: string): CategoryDescriptor['kind'] => {
    if (catId === 'completion') return 'completion'
    if (catId === 'metadata' || catId === 'judge') return 'metadata'
    return catId.startsWith('tool:') ? 'tool' as const : 'metadata'
  }
  const humanize = (s: string) => {
    try {
      return s.replace(/_/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())
    } catch {
      return s
    }
  }
  const valueTypeForMatcher = (m?: string): ValueType => {
    if (!m) return 'text'
    if (m === 'number.cmp' || m === 'length.cmp') return 'number'
    if (m === 'list.contains_any' || m === 'list.contains_all') return 'list<string>'
    if (m === 'list.contains') return 'text'
    if (m.startsWith('text.')) return 'text'
    return 'text'
  }
  const allowedOpsForMatcher = (m?: string): AllowedOp[] => {
    if (!m) return ['text.contains']
    if (m === 'number.cmp') return ['number.cmp']
    if (m === 'length.cmp') return ['length.cmp']
    if (m === 'list.contains_any') return ['list.contains_any']
    if (m === 'list.contains_all') return ['list.contains_all']
    if (m === 'list.contains') return ['list.contains']
    if (m === 'text.regex') return ['text.regex']
    if (m === 'text.equals') return ['text.equals']
    if (m === 'text.not_contains') return ['text.not_contains']
    return ['text.contains']
  }
  const newField: FieldDescriptor = {
    key: fieldKey,
    label: humanize(fieldKey),
    value_type: valueTypeForMatcher(matcherType),
    allowed_ops: allowedOpsForMatcher(matcherType),
    io: undefined,
    examples: [],
    options: [],
  }
  if (existingCat) {
    existingCat.fields = [...existingCat.fields, newField]
    // trigger reactivity by replacing array
    categories.value = categories.value.map(c => (c.id === existingCat.id ? existingCat : c))
  } else {
    const newCat: CategoryDescriptor = {
      id: categoryId,
      label: categoryId.startsWith('tool:') ? humanize(categoryId.split(':')[1] || categoryId) : humanize(categoryId),
      kind: deriveKind(categoryId),
      tool_name: categoryId.startsWith('tool:') ? (categoryId.split(':')[1] || undefined) : undefined,
      fields: [newField],
    }
    categories.value = [...categories.value, newCat]
  }
}

const loadCatalog = async () => {
  catalogLoading.value = true
  try {
    const res: any = await useMyFetch('/api/tests/catalog')
    if (res?.error?.value) throw res.error.value
    const data = (res?.data?.value || {}) as TestCatalog
    categories.value = data.categories || []
  } catch (e) {
    console.error('Failed to load test catalog', e)
  } finally {
    catalogLoading.value = false
  }
}

onMounted(async () => {
  await loadSuites()
  await loadCatalog()
  await loadJudgeModels()
  // Prepopulate when editing
  if (isEditing.value && props.caseId) {
    await loadCaseForEdit(props.caseId)
  }
})

watch(() => props.suiteId, (v) => {
  if (v && !selectedSuiteIdLocal.value) selectedSuiteIdLocal.value = v
})

// Ensure we fetch latest case data when the modal opens or caseId changes
watch([() => props.caseId, isOpen], async ([cid, open]) => {
  if (open && cid) {
    // Ensure dependencies are loaded
    if ((categories.value || []).length === 0) await loadCatalog()
    if ((suites.value || []).length === 0) await loadSuites()
    await loadCaseForEdit(String(cid))
  } else if (open && !cid) {
    // Opening fresh create modal: reset state to avoid leaking previous edit values
    resetFormForCreate()
  }
})

async function loadSuites() {
  suitesLoading.value = true
  try {
    const res: any = await useMyFetch('/api/tests/suites?limit=100')
    suites.value = (res?.data?.value || []) as Array<{ id: string, name: string }>
  } catch (e) {
    suites.value = []
  } finally {
    suitesLoading.value = false
  }
}

async function ensureSuiteId(): Promise<string> {
  // Prefer explicit selection
  if (selectedSuiteIdLocal.value) return selectedSuiteIdLocal.value
  if (props.suiteId) return props.suiteId
  throw new Error('Please select an existing suite or provide a name for a new suite.')
}

function onSuiteMenuChanged() {
  if (selectedSuiteIdLocal.value === '__create__') {
    // reset selection and open create modal
    selectedSuiteIdLocal.value = props.suiteId || ''
    createSuiteName.value = ''
    showCreateSuiteModal.value = true
  }
}

async function createSuite() {
  if (createSuiteLoading.value) return
  const name = (createSuiteName.value || '').trim()
  if (name.length === 0) return
  createSuiteLoading.value = true
  try {
    const res: any = await useMyFetch('/api/tests/suites', {
      method: 'POST',
      body: { name }
    })
    if (res?.error?.value) throw res.error.value
    const suite = res?.data?.value as any
    if (suite?.id) {
      // update list and select
      const exists = (suites.value || []).some(s => s.id === suite.id)
      if (!exists) suites.value = [...suites.value, { id: suite.id, name: suite.name }]
      selectedSuiteIdLocal.value = suite.id
      showCreateSuiteModal.value = false
      createSuiteName.value = ''
    }
  } catch (e) {
    // silent fail; could add toast later
  } finally {
    createSuiteLoading.value = false
  }
}

const defaultMatcherFor = (field: FieldDescriptor): Matcher => {
  const op = field.allowed_ops[0]
  if (op === 'number.cmp') return { type: 'number.cmp', op: 'gt', value: 0 }
  if (op === 'length.cmp') return { type: 'length.cmp', op: 'gt', value: 0 }
  if (op === 'text.regex') return { type: 'text.regex', pattern: '' }
  if (op === 'list.contains_any') return { type: 'list.contains_any', values: [] }
  if (op === 'list.contains_all') return { type: 'list.contains_all', values: [] }
  if (op === 'list.contains') return { type: 'list.contains', value: '' }
  // text.contains / text.equals / text.not_contains
  return { type: op, value: '' }
}

const removeCategory = (key: string) => {
  categoryRules.value = categoryRules.value.filter(c => c.key !== key)
}

const onValuesCommaChange = (r: FieldRuleUI) => {
  const raw = (r.valuesComma || '').split(',').map(s => s.trim()).filter(Boolean)
  ;(r.matcher as any).values = raw
}

const defaultMatcherForOp = (op: AllowedOp) => {
  if (op === 'number.cmp') return { type: 'number.cmp', op: 'gt', value: 0 }
  if (op === 'length.cmp') return { type: 'length.cmp', op: 'gt', value: 0 }
  if (op === 'text.regex') return { type: 'text.regex', pattern: '' }
  if (op === 'list.contains_any') return { type: 'list.contains_any', values: [] }
  if (op === 'list.contains_all') return { type: 'list.contains_all', values: [] }
  if (op === 'list.contains') return { type: 'list.contains', value: '' }
  // text.contains / text.equals / text.not_contains
  return { type: op, value: '' }
}

function makeFieldRuleFor(cat: CategoryDescriptor, field: FieldDescriptor): FieldRuleUI {
  return {
    key: `${cat.id}:${field.key}:${Date.now()}:${Math.random().toString(36).slice(2, 6)}`,
    categoryKind: cat.kind,
    target: { category: cat.id, field: field.key },
    allowedOps: field.allowed_ops,
    matcher: defaultMatcherFor(field),
    valuesComma: '',
  }
}

const addCategory = async () => {
  let firstCat = categories.value[0]
  if (!firstCat) {
    await loadCatalog()
    firstCat = categories.value[0]
  }
  if (!firstCat) return
  const firstField = firstCat.fields[0]
  if (!firstField) return
  const fieldRule = makeFieldRuleFor(firstCat, firstField)
  categoryRules.value.push({
    key: `${firstCat.id}:${Date.now()}:${Math.random().toString(36).slice(2, 6)}`,
    categoryId: firstCat.id,
    categoryKind: firstCat.kind,
    fieldRules: [fieldRule],
  })
}

const addField = (cat: CategoryRuleUI) => {
  const meta = categoryById.value[cat.categoryId]
  if (!meta) return
  const firstField = meta.fields[0]
  if (!firstField) return
  cat.fieldRules.push(makeFieldRuleFor(meta, firstField))
}

const removeField = (cat: CategoryRuleUI, fieldKey: string) => {
  cat.fieldRules = cat.fieldRules.filter(fr => fr.key !== fieldKey)
}

// Ensure a judge field rule exists and return it
const getJudgeRule = (cat: CategoryRuleUI, fieldKey: 'prompt' | 'model_id'): FieldRuleUI => {
  let found = cat.fieldRules.find(fr => fr.target.field === fieldKey)
  if (found) return found
  const meta = categoryById.value[cat.categoryId]
  const field = meta?.fields.find(f => f.key === fieldKey)
  if (meta && field) {
    const created = makeFieldRuleFor(meta, field)
    cat.fieldRules.push(created)
    return created
  }
  // Fallback placeholder rule
  const placeholder: FieldRuleUI = {
    key: `${cat.categoryId}:${fieldKey}:${Date.now()}`,
    categoryKind: cat.categoryKind,
    target: { category: cat.categoryId, field: fieldKey },
    allowedOps: ['text.equals'] as any,
    matcher: { type: 'text.equals', value: '' },
  }
  cat.fieldRules.push(placeholder)
  return placeholder
}

async function loadJudgeModels() {
  try {
    const { data } = await useMyFetch('/api/llm/models?is_enabled=true')
    judgeModels.value = (data as any)?.value || []
    // Auto-select default model for any existing judge categories without a selection
    for (const cat of categoryRules.value) {
      if (cat.categoryId === 'judge') ensureDefaultJudgeModel(cat)
    }
  } catch (e) {
    judgeModels.value = []
  }
}

function judgeSelectedModelLabel(cat: CategoryRuleUI): string {
  const val = (getJudgeRule(cat, 'model_id').matcher as any).value
  const m = (judgeModels.value || []).find((x: any) => (x.model_id || x.value) === val)
  return m?.name || m?.model_id || ((judgeModels.value || [])[0]?.name || (judgeModels.value || [])[0]?.model_id || 'Select Model')
}

function setJudgeModel(cat: CategoryRuleUI, m: any) {
  (getJudgeRule(cat, 'model_id').matcher as any).value = m?.model_id || m?.value || ''
}

function ensureDefaultJudgeModel(cat: CategoryRuleUI) {
  const currentVal = (getJudgeRule(cat, 'model_id').matcher as any).value
  if (currentVal) return
  const small = (judgeModels.value || []).find((m: any) => m.is_small_default)
  const reg = (judgeModels.value || []).find((m: any) => m.is_default)
  const pick = small || reg || (judgeModels.value || [])[0]
  if (pick) {
    (getJudgeRule(cat, 'model_id').matcher as any).value = pick.model_id || pick.value || ''
  }
}

const onChangeCategory = (cat: CategoryRuleUI) => {
  const meta = categoryById.value[cat.categoryId]
  if (!meta) return
  // Special-case judge: create prompt + model_id rules and hide operators in UI
  if (meta.id === 'judge') {
    const promptField = meta.fields.find(f => f.key === 'prompt') || meta.fields[0]
    const modelField = meta.fields.find(f => f.key === 'model_id') || meta.fields[1] || meta.fields[0]
    const rules: FieldRuleUI[] = []
    if (promptField) rules.push(makeFieldRuleFor(meta, promptField))
    if (modelField && modelField !== promptField) rules.push(makeFieldRuleFor(meta, modelField))
    cat.categoryKind = meta.kind
    cat.fieldRules = rules
    ensureDefaultJudgeModel(cat)
    return
  }
  const firstField = meta.fields[0]
  if (!firstField) return
  // Reset fields for simplicity when changing category
  cat.categoryKind = meta.kind
  cat.fieldRules = [makeFieldRuleFor(meta, firstField)]
}

const onChangeField = (cat: CategoryRuleUI, r: FieldRuleUI) => {
  const meta = categoryById.value[cat.categoryId]
  const field = meta?.fields.find(f => f.key === r.target.field)
  if (!field) return
  r.allowedOps = field.allowed_ops
  r.matcher = defaultMatcherFor(field)
  r.valuesComma = ''
  // If judge model field, ensure default model applied
  if (cat.categoryId === 'judge' && r.target.field === 'model_id') ensureDefaultJudgeModel(cat)
}

const onChangeOp = (r: FieldRuleUI) => {
  const op = (r.matcher as any)?.type as AllowedOp
  r.matcher = defaultMatcherForOp(op)
  r.valuesComma = ''
}

const normalizeMatcher = (m: any) => {
  // Ensure numeric values are numbers
  if (m?.type === 'number.cmp') return { type: m.type, op: m.op, value: Number(m.value ?? 0) }
  if (m?.type === 'length.cmp') return { type: m.type, op: m.op, value: Number(m.value ?? 0) }
  if (m?.type === 'text.regex') return { type: m.type, pattern: String(m.pattern ?? '') }
  if (m?.type === 'list.contains_any' || m?.type === 'list.contains_all') return { type: m.type, values: Array.isArray(m.values) ? m.values : [] }
  if (m?.type === 'list.contains') return { type: m.type, value: m.value }
  if (m?.type?.startsWith('text.')) return { type: m.type, value: String(m.value ?? '') }
  return m
}

const close = () => emit('update:modelValue', false)

function resetFormForCreate() {
  promptText.value = ''
  testSelectedDataSources.value = []
  testSelectedModelId.value = ''
  testUploadedFiles.value = []
  testMentions.value = []
  selectedSuiteIdLocal.value = props.suiteId || ''
  // Reset rules to empty for new case
  categoryRules.value = []
}

const save = async () => {
  isSaving.value = true
  try {
    if (isEditing.value && props.caseId) {
      const updated = await updateCase(props.caseId)
      const res = updated?.raw
      if (!updated?.case) throw new Error('Failed to update case')
      // Editing existing case: emit only updated for in-place list updates
      // @ts-ignore - extended event (extended event type)
      emit('updated' as any, (res as any)?.data?.value)
      if ((res as any)?.error?.value) throw (res as any).error.value
    } else {
      const created = await createCase()
      const res = created?.raw
      if (!created?.case) throw new Error('Failed to create case')
      emit('created', (res as any)?.data?.value)
      if ((res as any)?.error?.value) throw (res as any).error.value
    }
    close()
  } catch (e) {
    console.error('Failed to create test case', e)
  } finally {
    isSaving.value = false
  }
}

// Helper used by both save() and runNow()
const createCase = async (): Promise<{ case: any | null, raw: any } | null> => {
  const flatRules: any[] = []
  for (const cat of categoryRules.value) {
    for (const r of cat.fieldRules) {
      flatRules.push({ type: 'field', target: r.target, matcher: normalizeMatcher(r.matcher) })
    }
  }
  const expectations = { spec_version: 1, rules: flatRules }
  const trimmed = promptText.value.trim()
  const name = (trimmed.length > 0 ? trimmed : 'Untitled test').slice(0, 60)
  // Build mentions grouped like PromptBoxV2
  const mentionsByType = {
    data_sources: (testMentions.value || []).filter((m: any) => m.type === 'data_source'),
    tables: (testMentions.value || []).filter((m: any) => m.type === 'datasource_table'),
    files: (testMentions.value || []).filter((m: any) => m.type === 'file'),
    entities: (testMentions.value || []).filter((m: any) => m.type === 'entity')
  }
  const mentions = [
    { name: 'DATA SOURCES', items: mentionsByType.data_sources },
    { name: 'TABLES', items: mentionsByType.tables },
    { name: 'FILES', items: mentionsByType.files },
    { name: 'ENTITIES', items: mentionsByType.entities }
  ]
  const fileIds = (testUploadedFiles.value || []).map((f: any) => f.id).filter(Boolean)
  const suiteId = await ensureSuiteId()
  const res = await useMyFetch(`/api/tests/suites/${suiteId}/cases`, {
    method: 'POST',
    body: {
      name,
      prompt_json: { content: promptText.value, model_id: testSelectedModelId.value || undefined, mentions, files: fileIds },
      expectations_json: expectations,
      data_source_ids_json: (testSelectedDataSources.value || []).map((ds: any) => ds.id)
    }
  })
  const created = (res as any)?.data?.value
  return { case: created || null, raw: res }
}

const updateCase = async (caseId: string): Promise<{ case: any | null, raw: any } | null> => {
  const flatRules: any[] = []
  for (const cat of categoryRules.value) {
    for (const r of cat.fieldRules) {
      flatRules.push({ type: 'field', target: r.target, matcher: normalizeMatcher(r.matcher) })
    }
  }
  const expectations = { spec_version: 1, rules: flatRules }
  const trimmed = promptText.value.trim()
  const name = (trimmed.length > 0 ? trimmed : 'Untitled test').slice(0, 60)
  // Reuse mentions/files/data sources captured from PromptBox session (best-effort)
  const mentions = testMentions.value || []
  const fileIds = (testUploadedFiles.value || []).map((f: any) => f.id).filter(Boolean)
  const suiteId = await ensureSuiteId()
  const res = await useMyFetch(`/api/tests/cases/${caseId}`, {
    method: 'PATCH',
    body: {
      name,
      prompt_json: { content: promptText.value, model_id: testSelectedModelId.value || undefined, mentions, files: fileIds },
      expectations_json: expectations,
      data_source_ids_json: (testSelectedDataSources.value || []).map((ds: any) => ds.id),
    }
  })
  const updated = (res as any)?.data?.value
  return { case: updated || null, raw: res }
}

const runNow = async () => {
  if (isRunning.value) return
  isRunning.value = true
  try {
    let caseId: string | null = null
    if (isEditing.value && props.caseId) {
      // Update then run
      const updated = await updateCase(props.caseId)
      if (!updated?.case?.id) throw new Error('Failed to update case')
      // Editing existing case: emit only updated for in-place list updates
      // @ts-ignore - extended event (extended event type)
      emit('updated' as any, updated.case)
      caseId = updated.case.id
    } else {
      const created = await createCase()
      if (!created?.case?.id) throw new Error('Failed to create case')
      emit('created', created.case)
      caseId = created.case.id
    }
    // Create the run for this single case
      const runRes: any = await useMyFetch('/api/tests/runs', {
      method: 'POST',
      body: { case_ids: [caseId], trigger_reason: 'manual' }
    })
    if (runRes?.error?.value) throw runRes.error.value
    const run = runRes?.data?.value
    // 3) Navigate to run details
    if (run?.id) {
      close()
      router.push(`/evals/runs/${run.id}`)
    }
  } catch (e) {
    console.error('Failed to run test now', e)
  } finally {
    isRunning.value = false
  }
}

async function loadCaseForEdit(caseId: string) {
  try {
    const res: any = await useMyFetch(`/api/tests/cases/${caseId}`)
    const c = res?.data?.value
    if (!c) return
    // Suite
    selectedSuiteIdLocal.value = c.suite_id || selectedSuiteIdLocal.value
    // Prompt
    promptText.value = (c.prompt_json?.content || '').trim()
    testSelectedModelId.value = c.prompt_json?.model_id || ''
    // Data sources (best-effort; PromptBox does not accept initial props)
    testSelectedDataSources.value = Array.isArray(c.data_source_ids_json) ? c.data_source_ids_json.map((id: string) => ({ id })) : []
    // Rules → UI
    const rules = Array.isArray(c.expectations_json?.rules) ? c.expectations_json.rules : []
    const groupedByCategory: Record<string, CategoryRuleUI[]> = {}
    const ensureGroup = (catId: string, catMeta: CategoryDescriptor, forceNew: boolean = false) => {
      if (!groupedByCategory[catId]) groupedByCategory[catId] = []
      if (!forceNew && groupedByCategory[catId].length > 0) return groupedByCategory[catId][groupedByCategory[catId].length - 1]
      const group: CategoryRuleUI = {
        key: `${catId}:${Date.now()}:${Math.random().toString(36).slice(2,6)}`,
        categoryId: catId,
        categoryKind: catMeta.kind,
        fieldRules: []
      }
      groupedByCategory[catId].push(group)
      return group
    }

    const findJudgeGroupForModel = () => {
      const arr = groupedByCategory['judge'] || []
      return arr.find(g => !g.fieldRules.some(fr => fr.target.field === 'model_id'))
    }

    for (const r of rules) {
      if (r?.type !== 'field') continue
      const catId = r?.target?.category
      const fieldKey = r?.target?.field
      if (!catId || !fieldKey) continue
      // Ensure catalog has this category/field even if backend catalog doesn't include it (legacy/custom)
      ensureDynamicCategoryAndField(String(catId), String(fieldKey), r?.matcher?.type)
      const catMeta = categoryById.value[catId]
      const field = catMeta?.fields.find((f: any) => f.key === fieldKey)
      if (!catMeta || !field) continue

      let group: CategoryRuleUI
      if (catId === 'judge') {
        if (fieldKey === 'prompt') {
          group = ensureGroup(catId, catMeta, true)
        } else if (fieldKey === 'model_id') {
          group = findJudgeGroupForModel() || ensureGroup(catId, catMeta, true)
        } else {
          group = ensureGroup(catId, catMeta, true)
        }
      } else {
        group = ensureGroup(catId, catMeta, false)
      }

      const fr = makeFieldRuleFor(catMeta, field)
      // Overwrite matcher and target occurrence if present
      fr.matcher = r.matcher || fr.matcher
      // Prefill comma input for list.contains_any/all for proper display
      if (fr.matcher?.type === 'list.contains_any' || fr.matcher?.type === 'list.contains_all') {
        const vals = Array.isArray((fr.matcher as any).values) ? (fr.matcher as any).values : []
        fr.valuesComma = vals.join(', ')
      }
      if (typeof r?.target?.occurrence === 'number') fr.target.occurrence = r.target.occurrence
      group.fieldRules.push(fr)
    }
    const flattened = Object.values(groupedByCategory).flat()
    if (flattened.length) {
      categoryRules.value = flattened
    }
  } catch (e) {
    console.error('Failed to load case for edit', e)
  }
}
</script>


