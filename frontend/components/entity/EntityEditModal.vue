<!--
  One form for a saved query, in two modes — the report's "Save Query" layout
  (title, description, agents, status) plus the code, since there is no chat
  step to take it from:

  - create (`entityId` null, `dsId` set): the agent panel's "New query". Run
    previews through the stateless POST /entities/preview; Save mints the row
    with POST /entities and runs it once so it lands with data. Same two
    tiers as the report: entity managers publish, anyone with access to the
    agents suggests (the backend decides; the form only says which).
  - edit (`entityId` + `detail`): the query's Edit button. Run previews
    through POST /entities/{id}/preview; Save is PUT /entities/{id}, plus a
    run when the code changed so the snapshot follows it.
  - save from a report (`stepId`): the chat result's "Save query". Same
    form, prefilled from the step; Save is POST /entities/from_step/{id}
    carrying any edited code/declarations, and a run when they changed.
-->
<template>
  <!-- The panel's size follows the tab (Details compact, Code roomy) through
       CSS variables set on the root element: changing the `ui` prop instead
       re-mounts the dialog and makes it flash on every switch. -->
  <UModal v-model="open" :ui="{ width: 'sm:max-w-[var(--bow-query-modal-w,42rem)] transition-[max-width,height] duration-200', height: 'sm:h-[var(--bow-query-modal-h,80vh)]' }">
    <div class="h-full flex flex-col bg-gray-50 dark:bg-gray-900" data-testid="entity-edit-modal">
      <!-- Header -->
      <div class="px-4 py-3 bg-white dark:bg-gray-900 border-b flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800 dark:text-gray-200" data-testid="entity-edit-title">
          {{ isCreate ? (canCreateEntities ? (isFromStep ? $t('entityCreate.saveQuery') : $t('queries.newQuery')) : $t('entityCreate.suggestQuery')) : $t('queries.editQuery') }}
        </div>
        <button class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="open = false">{{ $t('entityCreate.close') }}</button>
      </div>

      <!-- Tabs: details / code / parameters. One thing per screen instead of
           one long scroll; the footer's Save applies to all three. -->
      <div class="px-4 pt-2 bg-white dark:bg-gray-900 border-b flex items-center gap-1 text-xs flex-shrink-0">
        <button
          v-for="t in tabs"
          :key="t.key"
          type="button"
          :data-testid="'entity-edit-tab-' + t.key"
          class="h-8 px-2.5 -mb-px border-b-2 transition-colors inline-flex items-center gap-1.5"
          :class="tab === t.key ? 'border-blue-500 text-gray-900 dark:text-white font-medium' : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'"
          @click="tab = t.key"
        >
          {{ t.label }}
          <span v-if="t.key === 'code' && runError" class="w-1.5 h-1.5 rounded-full bg-red-500" />
        </button>
      </div>

      <div class="flex-1 flex overflow-hidden min-h-0">
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div class="flex-1 overflow-auto">
            <div class="bg-white dark:bg-gray-900 rounded-lg p-3">
              <!-- Save failure: surface the backend's reason instead of failing silently -->
              <div v-if="errorMsg" class="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 rounded-lg text-xs text-red-800" data-testid="entity-edit-error">
                <div class="font-medium mb-1">{{ $t('entityCreate.saveFailed') }}</div>
                <div>{{ errorMsg }}</div>
              </div>

              <!-- ── Details ─────────────────────────────────────────── -->
              <div v-if="tab === 'details'">
                <!-- Tier banners (create only): same wording as the report's Save Query -->
                <template v-if="isCreate">
                  <div v-if="canCreateEntities" class="mb-4 p-3 bg-green-50 dark:bg-green-950 border border-green-200 rounded-lg text-xs text-green-800" data-testid="entity-edit-banner-publish">
                    <div class="font-medium mb-1">{{ $t('entityCreate.adminHeading') }}</div>
                    <div>{{ $t('entityCreate.adminBody') }}</div>
                  </div>
                  <div v-else class="mb-4 p-3 bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-lg text-xs text-blue-800" data-testid="entity-edit-banner-suggest">
                    <div class="font-medium mb-1">{{ $t('entityCreate.suggestHeading') }}</div>
                    <div>{{ $t('entityCreate.suggestBody') }}</div>
                  </div>
                </template>
                <EntityForm v-model="form" :show-status="canCreateEntities" />
              </div>

              <!-- ── Code ────────────────────────────────────────────── -->
              <div v-else-if="tab === 'code'" @keydown.capture="onCodeKeydown">
                <ClientOnly>
                  <div class="h-[380px] rounded-lg overflow-hidden border border-gray-300 dark:border-gray-600">
                    <MonacoEditor
                      v-model="code"
                      :lang="editorLang || 'python'"
                      :options="{ theme: 'vs-dark', automaticLayout: true, minimap: { enabled: false }, wordWrap: 'off', lineNumbers: 'on', fontSize: 13, scrollBeyondLastLine: false, padding: { top: 8 } }"
                      style="height: 100%"
                    />
                  </div>
                </ClientOnly>
                <!-- ── Parameters ──────────────────────────────────────── -->
                <div class="mt-4" data-testid="entity-edit-params">
                  <div class="flex items-start justify-between gap-3 mb-2">
                    <button type="button" class="text-start group" data-testid="entity-edit-params-toggle" @click="paramsCollapsed = !paramsCollapsed">
                      <div class="text-xs font-medium text-gray-700 dark:text-gray-300 inline-flex items-center gap-1">
                        <Icon name="heroicons-chevron-down" class="w-3 h-3 text-gray-400 transition-transform" :class="{ '-rotate-90': paramsCollapsed }" />
                        {{ $t('queries.paramsLabel') }}<span v-if="paramSpecs.length" class="ms-1 text-gray-400 font-normal">({{ paramSpecs.length }})</span>
                        <!-- Folded: the test values in one line, so the run's inputs stay readable. -->
                        <span v-if="paramsCollapsed && paramSummary" class="ms-2 font-normal text-gray-400 dark:text-gray-500 font-mono text-[11px]">{{ paramSummary }}</span>
                      </div>
                      <div v-if="!paramsCollapsed" class="text-[11px] leading-relaxed text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('queries.paramsHint') }}</div>
                    </button>
                    <button v-if="!paramsCollapsed" type="button" data-testid="entity-edit-add-param" class="shrink-0 h-7 px-2.5 rounded-md border border-gray-200 dark:border-gray-700 text-xs font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 inline-flex items-center gap-1" @click="addParam">
                      <Icon name="heroicons-plus" class="w-3 h-3" />{{ $t('queries.addParam') }}
                    </button>
                  </div>

                  <div v-if="paramsCollapsed" />
                  <div v-else-if="!paramSpecs.length" class="py-4 text-center text-xs text-gray-400 dark:text-gray-500 border border-dashed border-gray-200 dark:border-gray-700 rounded-lg">
                    {{ $t('queries.noParams') }}
                  </div>

                  <div v-else class="space-y-2">
                    <div v-for="(spec, idx) in paramSpecs" :key="idx" class="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900" :data-testid="`entity-param-row-${idx}`">
                      <!-- Basics: one line. -->
                      <div class="p-2.5 grid grid-cols-[1fr_auto] gap-2 items-center">
                        <div class="grid grid-cols-4 gap-1.5">
                          <div>
                            <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-0.5">{{ $t('queries.paramName') }}</div>
                            <input v-model="spec.name" placeholder="country" :class="[fieldClass, 'font-mono']" :data-testid="`entity-param-name-${idx}`" @change="onParamNamed(spec)" />
                          </div>
                          <div>
                            <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-0.5">{{ $t('queries.paramType') }}</div>
                            <select v-model="spec.type" :class="fieldClass">
                              <option v-for="t in ['string','number','date','date_range','id','list']" :key="t" :value="t">{{ t }}</option>
                            </select>
                          </div>
                          <div>
                            <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-0.5">{{ $t('queries.paramLabel') }}</div>
                            <input v-model="spec.label" :placeholder="spec.name || $t('queries.paramLabel')" :class="fieldClass" />
                          </div>
                          <div>
                            <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-0.5">{{ $t('queries.paramTestValue') }}</div>
                            <input v-if="spec.source !== 'identity'" v-model="paramTestValues[spec.name]" :placeholder="spec.default || $t('queries.paramAll')" :class="fieldClass" :data-testid="`entity-param-test-${idx}`" />
                            <div v-else :class="[fieldClass, 'text-gray-400 dark:text-gray-500 bg-gray-50 dark:bg-gray-800/60']">{{ $t('queries.paramFromViewer') }}</div>
                          </div>
                        </div>
                        <div class="flex items-center gap-1 self-end pb-0.5">
                          <button type="button" class="h-7 px-2 rounded-md text-[11px] text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800 inline-flex items-center gap-1" :data-testid="`entity-param-more-${idx}`" @click="spec._open = !spec._open">
                            {{ $t('queries.paramMore') }}
                            <Icon name="heroicons-chevron-down" class="w-3 h-3 transition-transform" :class="{ 'rotate-180': spec._open }" />
                          </button>
                          <button type="button" class="h-7 w-7 rounded-md text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40 inline-flex items-center justify-center" :title="$t('queries.paramRemove')" @click="removeParam(idx)"><Icon name="heroicons-trash" class="w-3.5 h-3.5" /></button>
                        </div>
                      </div>

                      <!-- Advanced: who sets the value, choices, default. Two even
                           columns; every cell is label / control / one helper line. -->
                      <div v-if="spec._open" class="px-2.5 pb-2.5 pt-2.5 border-t border-gray-100 dark:border-gray-800 grid grid-cols-2 gap-x-4 gap-y-3">
                        <div>
                          <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('queries.paramSource') }}</div>
                          <USelectMenu
                            v-model="spec.source"
                            :options="sourceOptions"
                            value-attribute="value"
                            option-attribute="label"
                            size="xs"
                            :ui-menu="{ width: 'w-full', option: { size: 'text-xs' } }"
                            :data-testid="`entity-param-source-${idx}`"
                          />
                          <div class="mt-1 text-[11px] leading-snug text-gray-400 dark:text-gray-500">{{ $t('queries.paramSourceDesc.' + spec.source) }}</div>
                        </div>
                        <div v-if="spec.source !== 'input'">
                          <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('queries.paramBinding') }}</div>
                          <div class="flex gap-1.5">
                            <select v-model="spec.identity_binding" :class="fieldClass">
                              <option value="viewer.email">viewer.email</option>
                              <option value="viewer.user_id">viewer.user_id</option>
                              <option value="viewer.groups">viewer.groups</option>
                              <option v-if="spec.identity_binding && spec.identity_binding.startsWith('viewer.profile_attributes.')" :value="spec.identity_binding">{{ spec.identity_binding }}</option>
                            </select>
                            <input :placeholder="$t('queries.paramAttrPlaceholder')" :class="fieldClass" @change="spec.identity_binding = ($event.target as HTMLInputElement).value ? ('viewer.profile_attributes.' + ($event.target as HTMLInputElement).value.trim()) : spec.identity_binding" />
                          </div>
                          <div class="mt-1 text-[11px] leading-snug text-gray-400 dark:text-gray-500">{{ $t('queries.paramBindingDesc') }}</div>
                        </div>
                        <div v-else>
                          <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('queries.paramDefault') }}</div>
                          <input v-model="spec.default" :placeholder="$t('queries.paramAll')" :class="fieldClass" />
                          <div class="mt-1 text-[11px] leading-snug text-gray-400 dark:text-gray-500">{{ $t('queries.paramDefaultDesc') }}</div>
                        </div>
                        <template v-if="spec.source !== 'identity'">
                          <div>
                            <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('queries.paramChoices') }}</div>
                            <USelectMenu
                              :model-value="choicesMode(spec)"
                              :options="choicesOptions"
                              value-attribute="value"
                              option-attribute="label"
                              size="xs"
                              :ui-menu="{ width: 'w-full', option: { size: 'text-xs' } }"
                              :data-testid="`entity-param-choices-${idx}`"
                              @update:model-value="setChoicesMode(spec, $event)"
                            />
                            <div class="mt-1 text-[11px] leading-snug text-gray-400 dark:text-gray-500">{{ $t('queries.paramChoicesDesc.' + choicesMode(spec)) }}</div>
                          </div>
                          <div v-if="choicesMode(spec) === 'static'">
                            <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('queries.paramOptionsLabel') }}</div>
                            <input :value="(spec.options || []).join(', ')" placeholder="IL, FR, DE" :class="fieldClass" @input="spec.options = parseOptions(($event.target as HTMLInputElement).value)" />
                            <div class="mt-1 text-[11px] leading-snug text-gray-400 dark:text-gray-500">{{ $t('queries.paramOptionsPlaceholder') }}</div>
                          </div>
                          <div v-else-if="choicesMode(spec) === 'entity'">
                            <div class="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-1">{{ $t('queries.paramChoicesEntity') }}</div>
                            <div class="grid grid-cols-1 gap-1.5">
                              <USelectMenu
                                v-model="spec.options_source.entity_id"
                                :options="optionEntityChoices"
                                value-attribute="value"
                                option-attribute="label"
                                size="xs"
                                searchable
                                :placeholder="$t('queries.paramPickEntity')"
                                :ui-menu="{ width: 'w-full', option: { size: 'text-xs' } }"
                                @update:model-value="loadEntityColumns($event)"
                              />
                              <div class="grid grid-cols-2 gap-1.5">
                                <USelectMenu
                                  v-model="spec.options_source.value_column"
                                  :options="entityColumns[spec.options_source.entity_id] || []"
                                  size="xs"
                                  :placeholder="$t('queries.paramValueColumn')"
                                  :ui-menu="{ width: 'w-full', option: { size: 'text-xs' } }"
                                />
                                <USelectMenu
                                  v-model="spec.options_source.label_column"
                                  :options="labelColumnChoices(spec.options_source.entity_id)"
                                  value-attribute="value"
                                  option-attribute="label"
                                  size="xs"
                                  :placeholder="$t('queries.paramLabelColumn')"
                                  :ui-menu="{ width: 'w-full', option: { size: 'text-xs' } }"
                                />
                              </div>
                            </div>
                          </div>
                          <div v-else />
                          <label class="col-span-2 inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400 cursor-pointer"><input type="checkbox" v-model="spec.required" />{{ $t('queries.paramRequiredLong') }}</label>
                        </template>
                      </div>
                    </div>
                  </div>
                </div>

                <div class="mt-3 flex items-center gap-2">
                  <button
                    type="button"
                    data-testid="entity-edit-run"
                    class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center gap-1.5 disabled:opacity-50"
                    :disabled="running || !code.trim()"
                    @click="run"
                  >
                    <Icon v-if="running" name="heroicons-arrow-path" class="w-3 h-3 animate-spin" />
                    <Icon v-else name="heroicons-play" class="w-3 h-3" />
                    <span>{{ running ? $t('queries.running') : $t('queries.run') }}</span>
                    <kbd class="ms-1 text-[10px] text-gray-400 dark:text-gray-500 font-sans">{{ runShortcut }}</kbd>
                  </button>
                  <span v-if="runError" class="text-xs text-red-600 dark:text-red-400" data-testid="entity-edit-run-error">{{ runError }}</span>
                </div>

                <div v-if="result" ref="resultEl" class="mt-3 rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden" data-testid="entity-edit-result">
                  <div class="px-3 py-2 text-xs text-gray-600 dark:text-gray-400 border-b bg-gray-50 dark:bg-gray-900 flex items-center justify-between">
                    <span class="font-medium">{{ $t('queries.resultsHeading') }}</span>
                    <span class="tabular-nums">{{ $t('queries.previewRows', { n: resultTotal }) }}<template v-if="resultShown < resultTotal"> · {{ $t('queries.showingFirst', { n: resultShown }) }}</template></span>
                  </div>
                  <div class="overflow-auto max-h-[40vh]">
                    <table class="min-w-full text-xs border-separate border-spacing-0">
                      <thead>
                        <tr>
                          <th
                            v-for="col in result.columns"
                            :key="col.field"
                            class="sticky top-0 z-10 bg-gray-50 dark:bg-gray-900 px-3 py-2 text-xs font-medium text-gray-600 dark:text-gray-300 border-b border-gray-200 dark:border-gray-700 whitespace-nowrap text-start"
                          >{{ col.headerName || col.field }}</th>
                        </tr>
                      </thead>
                      <tbody>
                        <tr v-for="(row, i) in resultRows" :key="i" class="odd:bg-white even:bg-gray-50/60 dark:odd:bg-gray-900 dark:even:bg-gray-800/40 hover:bg-blue-50/40 dark:hover:bg-blue-500/10">
                          <td
                            v-for="col in result.columns"
                            :key="col.field"
                            class="px-3 py-1.5 text-gray-800 dark:text-gray-200 whitespace-nowrap border-b border-gray-100 dark:border-gray-800 text-start tabular-nums"
                          >{{ formatCell(row[col.field]) }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>

            </div>
          </div>

          <!-- Footer Actions -->
          <div class="px-4 py-3 bg-white dark:bg-gray-900 border-t flex items-center justify-end gap-2 flex-shrink-0">
            <button class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800" @click="open = false">{{ $t('entityCreate.cancel') }}</button>
            <button
              data-testid="entity-edit-save"
              class="text-white text-xs font-medium py-1.5 px-3 rounded-lg disabled:opacity-50"
              :class="(!isCreate || canCreateEntities) ? 'bg-blue-500 hover:bg-blue-600' : 'bg-amber-500 hover:bg-amber-600'"
              :disabled="saving || !canSave"
              @click="onSave"
            >
              <span v-if="saving">{{ (!isCreate || canCreateEntities) ? $t('entityCreate.saving') : $t('entityCreate.submitting') }}</span>
              <span v-else-if="!isCreate">{{ $t('queries.save') }}</span>
              <span v-else>{{ canCreateEntities ? $t('entityCreate.saveEntity') : $t('entityCreate.suggestEntity') }}</span>
            </button>
          </div>
        </section>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCanAll, useCan } from '~/composables/usePermissions'
import EntityForm from './EntityForm.vue'

const { t } = useI18n()
const toast = useToast()

type MinimalDS = { id: string; name?: string; type?: string }
type EntityDetail = {
  id: string
  type: string
  title: string
  slug: string
  description?: string | null
  data?: any
  status?: string
  data_sources?: MinimalDS[]
  code?: string
  private_status?: string | null
  global_status?: string | null
  owner_id?: string
  [key: string]: any
}

const props = defineProps<{
  modelValue: boolean
  detail?: EntityDetail | null
  entityId?: string | null
  editorLang?: string
  // create mode: the agent whose Queries panel opened the form
  dsId?: string | null
  // save-from-report mode: the step to promote, and what to prefill from it
  stepId?: string | null
  initialTitle?: string
  initialType?: string
  initialCode?: string
  initialParameters?: any[] | null
  initialData?: any
  initialDataSourceIds?: string[]
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved', entity?: any): void
}>()

const open = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})
const isCreate = computed(() => !props.entityId)
const isFromStep = computed(() => isCreate.value && !!props.stepId)

const tab = ref<'details' | 'code'>('details')
// Details is a form: keep the compact dialog. Code wants room for the editor
// and the result table. Applied on the root so the teleported panel sees it.
const MODAL_SIZES = { details: ['42rem', '80vh'], code: ['1100px', '90vh'] } as const
function applyModalSize(which: 'details' | 'code' | null) {
  if (typeof document === 'undefined') return
  const st = document.documentElement.style
  if (!which) { st.removeProperty('--bow-query-modal-w'); st.removeProperty('--bow-query-modal-h'); return }
  const [w, h] = MODAL_SIZES[which]
  st.setProperty('--bow-query-modal-w', w)
  st.setProperty('--bow-query-modal-h', h)
}
watch([tab, () => props.modelValue], ([t, isOpen]) => applyModalSize(isOpen ? t : null), { immediate: true })
const tabs = computed(() => ([
  { key: 'details' as const, label: t('queries.tabDetails') },
  { key: 'code' as const, label: t('queries.tabCode') },
]))
const fieldClass = 'w-full h-7 px-2 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 dark:text-gray-100 outline-none focus:border-gray-400'
// Styled menus for the two "mode" pickers: a native <select> opens as a wide
// OS popup that ignores the control's width.
const sourceOptions = computed(() => ([
  { value: 'input', label: t('queries.paramSourceInput') },
  { value: 'identity', label: t('queries.paramSourceIdentity') },
  { value: 'input_identity_default', label: t('queries.paramSourceInputIdentity') },
]))
const optionEntityChoices = computed(() => optionEntities.value.map(e => ({ value: e.id, label: e.title || e.slug })))
const labelColumnChoices = (entityId: string) => ([
  { value: null, label: t('queries.paramLabelColumn') },
  ...((entityColumns.value[entityId] || []).map(c => ({ value: c, label: c }))),
])
const choicesOptions = computed(() => ([
  { value: 'none', label: t('queries.paramChoicesNone') },
  { value: 'static', label: t('queries.paramChoicesStatic') },
  { value: 'entity', label: t('queries.paramChoicesEntity') },
]))

const form = ref<{
  type: string
  title: string
  description: string | null
  status: string
  data_source_ids?: string[]
  global_status?: string | null
}>({ type: 'model', title: '', description: null, status: 'published', data_source_ids: [], global_status: null })

const code = ref('')
const savedCode = ref('')
const savedSpecs = ref<any[]>([])

// ── Parameters ─────────────────────────────────────────────
// Declarations (ParamSpec) edited alongside the code, plus test values the
// Run button uses. Identity params never take a client value.
const PARAM_NAME_RE = /^[a-zA-Z_]\w{0,63}$/
const paramSpecs = ref<any[]>([])
const paramTestValues = ref<Record<string, any>>({})
const optionEntities = ref<any[]>([])
const entityColumns = ref<Record<string, string[]>>({})

// Per-row "More" (source, choices, default, required) stays folded so the
// common case reads as one line. The flag lives on the row (`_open`);
// specsPayload rebuilds each spec from named fields, so it never leaves the form.
function removeParam(idx: number) {
  const name = (paramSpecs.value[idx]?.name || '').trim()
  paramSpecs.value.splice(idx, 1)
  if (name) {
    const next = unscaffoldParam(code.value, name)
    if (next !== code.value) code.value = next
    delete paramTestValues.value[name]
  }
}

function addParam() {
  paramSpecs.value.push({ name: '', type: 'string', label: '', default: null, required: false, source: 'input', options: null, options_source: null, identity_binding: null, _open: false, _scaffoldedAs: null })
  // The code only receives params when generate_df declares the argument.
  const m = code.value.match(/def\s+generate_df\s*\(([^)]*)\)/)
  if (m && !/\bparams\b/.test(m[1])) {
    code.value = code.value.replace(m[0], `def generate_df(${m[1].trim() ? m[1].trim() + ', ' : ''}params)`)
  }
}

// ── Scaffold the parameter into the code ─────────────────
// Naming a parameter wires it into a simple single-query body: the SQL gets
// `WHERE (:name IS NULL OR name = :name)` (the column is assumed to share the
// parameter's name — one word to fix when it doesn't), and the execute_query
// call gets `params={"name": params.get("name")}`. Empty stays "All". Code the
// scaffold cannot read (no literal SQL, several queries) is left alone.
// The call's own closing paren is the one that ends the line — the lazy tail
// otherwise stops at the first `)` inside `params.get("x")`.
// The tail may hold one level of parens (`params.get("x")`) but never the
// call's own `)`, so a chained `.head(10)` after it is left untouched.
const EXEC_RE = /(\.execute_query\(\s*)("""|'''|"|')([\s\S]*?)\2((?:[^()]|\([^()]*\))*?)(\))/
const SCALAR_TYPES = new Set(['string', 'number', 'date', 'id'])

function scaffoldParam(src: string, name: string, type: string): { code: string; sqlTouched: boolean } | null {
  if (!PARAM_NAME_RE.test(name)) return null
  const alreadyRead = new RegExp(`params(\\.get\\(\\s*|\\[\\s*)["']${name}["']`).test(src)
  if (alreadyRead) return null
  const m = src.match(EXEC_RE)
  if (!m) return null
  const [whole, head, quote, sql, tail, close] = m
  let newSql = sql
  let sqlTouched = false
  if (SCALAR_TYPES.has(type) && !new RegExp(`:${name}\\b`).test(sql)) {
    const lines = sql.split('\n')
    const lastLine = [...lines].reverse().find(l => l.trim())
    const indent = lastLine ? (lastLine.match(/^\s*/)?.[0] || '') : ''
    const hasWhere = /\bwhere\b/i.test(sql)
    const clause = `${hasWhere ? 'AND' : 'WHERE'} (:${name} IS NULL OR ${name} = :${name})`
    const tailKw = sql.search(/\b(group\s+by|order\s+by|limit|having)\b/i)
    const multiline = quote.length === 3
    if (!multiline) {
      // A one-line string cannot take newlines: splice the clause with spaces.
      newSql = tailKw >= 0
        ? `${sql.slice(0, tailKw).replace(/\s*$/, '')} ${clause} ${sql.slice(tailKw)}`
        : `${sql.replace(/\s*$/, '')} ${clause}`
    } else if (tailKw >= 0) {
      newSql = sql.slice(0, tailKw).replace(/\s*$/, '') + `\n${indent}${clause}\n${indent}` + sql.slice(tailKw)
    } else {
      const closingIndent = sql.match(/\n([ \t]*)$/)?.[1] || ''
      newSql = sql.replace(/\s*$/, '') + `\n${indent}${clause}\n${closingIndent}`
    }
    sqlTouched = true
  }
  const entry = `"${name}": params.get("${name}")`
  let newTail: string
  const pm = tail.match(/(params\s*=\s*\{)([\s\S]*?)(\})/)
  if (pm) {
    const inner = pm[2].trim()
    newTail = tail.replace(pm[0], `${pm[1]}${inner ? inner.replace(/,?\s*$/, '') + ', ' : ''}${entry}${pm[3]}`)
  } else {
    newTail = `${tail.replace(/\s*$/, '')}, params={${entry}}`
  }
  const rebuilt = `${head}${quote}${newSql}${quote}${newTail}${close}`
  return { code: src.replace(whole, rebuilt), sqlTouched }
}

// Removing a parameter takes the scaffold back out: the `WHERE/AND (:name IS
// NULL OR col = :name)` predicate and the `"name": params.get("name")` entry.
// A following `AND` is promoted to `WHERE` when the removed clause carried it.
// Hand-written uses of the parameter are left alone (the backend's
// declarations-vs-code check will point at them on save).
function unscaffoldParam(src: string, name: string): string {
  if (!PARAM_NAME_RE.test(name)) return src
  let out = src
  const pred = `\\(\\s*:${name}\\s+IS\\s+NULL\\s+OR\\s+[\\w.]+\\s*=\\s*:${name}\\s*\\)`
  // Multi-line form: the clause on its own line.
  const lineRe = new RegExp(`^([ \\t]*)(WHERE|AND)\\s+${pred}[ \\t]*\\n`, 'im')
  const lm = out.match(lineRe)
  if (lm) {
    const removedWhere = lm[2].toUpperCase() === 'WHERE'
    const idx = out.indexOf(lm[0])
    let rest = out.slice(idx + lm[0].length)
    if (removedWhere) rest = rest.replace(/^([ \t]*)AND\b/i, '$1WHERE')
    out = out.slice(0, idx) + rest
  } else {
    // One-line form: inline clause.
    const inlineRe = new RegExp(`\\s+(WHERE|AND)\\s+${pred}`, 'i')
    const im = out.match(inlineRe)
    if (im) {
      const removedWhere = im[1].toUpperCase() === 'WHERE'
      const idx = out.indexOf(im[0])
      let rest = out.slice(idx + im[0].length)
      if (removedWhere) rest = rest.replace(/^\s+AND\b/i, ' WHERE')
      out = out.slice(0, idx) + rest
    }
  }
  // The params entry, then an emptied params={} kwarg.
  const entry = new RegExp(`(,\\s*)?"${name}"\\s*:\\s*params(\\.get\\(\\s*"${name}"\\s*\\)|\\[\\s*"${name}"\\s*\\])`, 'g')
  out = out.replace(entry, '')
  out = out.replace(/\{\s*,\s*/g, '{')            // "{, "b": ...}" after removing a first entry
  out = out.replace(/,\s*params\s*=\s*\{\s*\}/g, '')
  return out
}

function onParamNamed(spec: any) {
  const name = (spec.name || '').trim()
  if (!name) return
  spec.name = name
  // A rename takes the previous name's scaffold out before adding the new one.
  if (spec._scaffoldedAs && spec._scaffoldedAs !== name) {
    code.value = unscaffoldParam(code.value, spec._scaffoldedAs)
    delete paramTestValues.value[spec._scaffoldedAs]
    spec._scaffoldedAs = null
  }
  const out = scaffoldParam(code.value, name, spec.type || 'string')
  if (!out) return
  code.value = out.code
  spec._scaffoldedAs = name
  toast.add({
    title: t('queries.paramScaffoldedTitle', { name }),
    description: out.sqlTouched ? t('queries.paramScaffoldedBody', { name }) : t('queries.paramScaffoldedBodyNoSql', { name }),
    color: 'blue',
  })
}

function parseOptions(raw: string): string[] {
  return raw.split(',').map(x => x.trim()).filter(Boolean)
}

// An empty array still means "fixed list" — the mode the user just picked —
// so the options input shows before anything is typed. specsPayload drops an
// empty list, so nothing meaningless is saved.
function choicesMode(spec: any): 'none' | 'static' | 'entity' {
  if (spec.options_source) return 'entity'
  if (Array.isArray(spec.options)) return 'static'
  return 'none'
}

async function setChoicesMode(spec: any, mode: string) {
  if (mode === 'entity') {
    spec.options = null
    spec.options_source = { entity_id: '', value_column: '', label_column: null }
    await loadOptionEntities()
  } else if (mode === 'static') {
    spec.options_source = null
    spec.options = spec.options || []
  } else {
    spec.options_source = null
    spec.options = null
  }
}

// Saved queries on the selected agents may feed a choice list (never this one).
async function loadOptionEntities() {
  try {
    const q = selectedIds.value.length ? `?data_source_ids=${encodeURIComponent(selectedIds.value.join(','))}&limit=200` : '?limit=200'
    const { data } = await useMyFetch<any>(`/api/entities${q}`, { method: 'GET' })
    optionEntities.value = ((data.value || []) as any[]).filter(e => e.status === 'published' && e.id !== props.entityId)
  } catch { optionEntities.value = [] }
}

async function loadEntityColumns(id: string) {
  if (!id || entityColumns.value[id]) return
  try {
    const { data } = await useMyFetch<any>(`/api/entities/${id}`, { method: 'GET' })
    const cols = ((data.value?.data?.columns || []) as any[]).map(c => String(c.field || c.headerName || '')).filter(Boolean)
    entityColumns.value = { ...entityColumns.value, [id]: cols }
  } catch { /* the selects stay empty; the backend rejects a bad column on save */ }
}

// The declarations as the backend expects them: empty names dropped, empty
// strings as null, choice sources only when complete.
function specsPayload(): any[] {
  return paramSpecs.value
    .filter(sp => (sp.name || '').trim())
    .map(sp => {
      const out: any = { name: sp.name.trim(), type: sp.type || 'string', source: sp.source || 'input', required: !!sp.required }
      if (sp.label) out.label = sp.label
      if (sp.source !== 'identity' && sp.default !== null && sp.default !== undefined && String(sp.default) !== '') out.default = sp.default
      if (sp.source !== 'identity') {
        if (sp.options_source?.entity_id && sp.options_source?.value_column) {
          out.options_source = { entity_id: sp.options_source.entity_id, value_column: sp.options_source.value_column, label_column: sp.options_source.label_column || null }
        } else if (sp.options_source?.query_id) {
          // Promoted from a dashboard query: its report-side source is kept as is.
          out.options_source = sp.options_source
        } else if (Array.isArray(sp.options) && sp.options.length) {
          out.options = sp.options
        }
      }
      if (sp.source !== 'input') out.identity_binding = sp.identity_binding || 'viewer.email'
      return out
    })
}

function testParamsPayload(): Record<string, any> | undefined {
  const out: Record<string, any> = {}
  for (const sp of paramSpecs.value) {
    if (!sp.name || sp.source === 'identity') continue
    const v = paramTestValues.value[sp.name]
    if (v !== undefined && v !== null && String(v) !== '') out[sp.name] = v
  }
  return Object.keys(out).length ? out : undefined
}

const paramsValid = computed(() => paramSpecs.value.every(sp => !sp.name || PARAM_NAME_RE.test(sp.name)))
const result = ref<any | null>(null)
const resultEl = ref<HTMLElement | null>(null)
// The table shows the first 100 rows; numbers align on the right.
const resultRows = computed<any[]>(() => ((result.value?.rows || []) as any[]).slice(0, 100))
const resultShown = computed(() => resultRows.value.length)
const resultTotal = computed<number>(() => Number(result.value?.info?.total_rows ?? result.value?.rows?.length ?? 0))
function formatCell(v: any): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 })
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// Parameters fold to a one-line summary once a result is on screen, so the
// editor and the table get the room; the header reopens them.
const paramsCollapsed = ref(false)
const paramSummary = computed(() => paramSpecs.value
  .filter(sp => sp.name)
  .map(sp => `${sp.name} = ${sp.source === 'identity' ? t('queries.paramFromViewer') : (paramTestValues.value[sp.name] ?? '') === '' ? t('queries.paramAll') : paramTestValues.value[sp.name]}`)
  .join(' · '))

const runShortcut = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘↵' : 'Ctrl+↵'
function onCodeKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !running.value && code.value.trim()) {
    e.preventDefault()
    run()
  }
}
// The result renders under the editor, often below the fold of the modal's
// scroll area; bring it into view so a run visibly produced something.
const revealResult = async () => {
  await nextTick()
  resultEl.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}
const runError = ref('')
const errorMsg = ref('')
const running = ref(false)
const saving = ref(false)

// Same rule as the report's Save Query: publishing / full editing needs
// per-agent `create_entities` on EVERY selected agent (org `manage_entities`
// and full admins via implication). Agent-less queries are org-wide and stay
// an org-admin capability.
const selectedIds = computed(() => form.value.data_source_ids || [])
const canCreateEntities = computed(() =>
  selectedIds.value.length ? useCanAll('create_entities', 'data_source', selectedIds.value) : useCan('manage_entities'))
const canSave = computed(() => !!form.value.title.trim() && !!code.value.trim() && paramsValid.value)

// Entity code is `generate_df(ds_clients, excel_files)` reaching an agent
// through `ds_clients["<agent name>:<connection name>"]`. Seed a new query
// with the opening agent's real key so the user only has to replace the SQL.
function template(key?: string): string {
  const client = key ? `ds_clients[${JSON.stringify(key)}]` : 'ds_clients["<agent>:<connection>"]'
  return `def generate_df(ds_clients, excel_files):\n    return ${client}.execute_query("""\n        SELECT 1 AS example\n    """)\n`
}

async function reset() {
  tab.value = 'details'
  paramsCollapsed.value = false
  result.value = null
  runError.value = ''
  errorMsg.value = ''
  running.value = false
  saving.value = false
  if (!isCreate.value && props.detail) {
    const d = props.detail
    form.value = {
      type: d.type || 'model',
      title: d.title || '',
      description: d.description || null,
      status: d.status || 'draft',
      data_source_ids: (d.data_sources || []).map(ds => ds.id),
      global_status: d.global_status || null,
    }
    code.value = d.code || ''
    savedCode.value = code.value
    result.value = d.data || null
    paramSpecs.value = JSON.parse(JSON.stringify(d.parameters || []))
    savedSpecs.value = specsPayload()
    paramTestValues.value = { ...(d.applied_params || {}) }
    entityColumns.value = {}
    if (paramSpecs.value.some(sp => sp.options_source?.entity_id)) {
      await loadOptionEntities()
      for (const sp of paramSpecs.value) if (sp.options_source?.entity_id) loadEntityColumns(sp.options_source.entity_id)
    }
    return
  }
  form.value = {
    type: props.initialType || 'model', title: props.initialTitle || '', description: null, status: 'published',
    data_source_ids: props.initialDataSourceIds?.length ? [...props.initialDataSourceIds] : (props.dsId ? [props.dsId] : []),
    global_status: null,
  }
  if (isFromStep.value) {
    // Prefilled from the chat step: its code, declarations and rows.
    code.value = props.initialCode || ''
    savedCode.value = code.value
    paramSpecs.value = JSON.parse(JSON.stringify(props.initialParameters || []))
    savedSpecs.value = specsPayload()
    result.value = props.initialData || null
    return
  }
  code.value = template()
  savedCode.value = ''
  paramSpecs.value = []
  savedSpecs.value = []
  paramTestValues.value = {}
  entityColumns.value = {}
  if (!props.dsId) return
  try {
    const { data } = await useMyFetch<any>(`/api/data_sources/${props.dsId}`, { method: 'GET' })
    const ds: any = data.value
    const conn = (ds?.connections || []).find((c: any) => c?.is_active !== false)
    if (ds?.name && conn?.name && code.value === template()) code.value = template(`${ds.name}:${conn.name}`)
  } catch { /* the placeholder key is still a valid starting point */ }
}

watch(() => props.modelValue, (v) => { if (v) reset() })

async function run() {
  running.value = true
  runError.value = ''
  try {
    const { data, error } = isCreate.value
      ? await useMyFetch<any>('/api/entities/preview', { method: 'POST', body: { code: code.value, data_source_ids: selectedIds.value, parameters: specsPayload(), params: testParamsPayload() } })
      : await useMyFetch<any>(`/api/entities/${props.entityId}/preview`, { method: 'POST', body: { code: code.value, parameters: specsPayload(), params: testParamsPayload() } })
    if (error.value) throw error.value
    const payload: any = data.value
    if (payload?.error) { runError.value = payload.error; result.value = null; return }
    result.value = payload?.data || null
    if (result.value) { paramsCollapsed.value = paramSpecs.value.length > 0; revealResult() }
  } catch (e: any) {
    runError.value = e?.data?.detail || e?.message || t('queries.runFailed')
    result.value = null
  } finally {
    running.value = false
  }
}

// useMyFetch never throws on HTTP errors, so a failed run has to be read off
// the response; the query stays saved either way.
async function runAfterSave(id: string) {
  try {
    const { data, error } = await useMyFetch<any>(`/api/entities/${id}/run`, { method: 'POST', body: { code: code.value } })
    const failure = error.value?.data?.detail || error.value?.message || (data.value as any)?.error
    if (failure) toast.add({ title: t('queries.savedButRunFailed'), description: String(failure), color: 'amber' })
  } catch (e: any) {
    toast.add({ title: t('queries.savedButRunFailed'), description: e?.message || '', color: 'amber' })
  }
}

async function onSave() {
  saving.value = true
  errorMsg.value = ''
  try {
    const status = form.value.status || 'draft'
    if (isFromStep.value) {
      const specs = specsPayload()
      const body = {
        type: form.value.type || 'model',
        title: form.value.title.trim(),
        description: form.value.description || null,
        publish: canCreateEntities.value && status === 'published',
        data_source_ids: selectedIds.value,
        code: code.value,
        parameters: specs,
      }
      const { data, error } = await useMyFetch<any>(`/api/entities/from_step/${props.stepId}`, { method: 'POST', body })
      if (error.value) throw error.value
      const saved: any = data.value
      // Edited code or declarations: the step's rows no longer apply, run once.
      if (code.value !== savedCode.value || JSON.stringify(specs) !== JSON.stringify(savedSpecs.value)) {
        await runAfterSave(String(saved.id))
      }
      toast.add({
        title: saved?.global_status === 'approved' && saved?.status === 'published'
          ? t('entityCreate.publishedToast') : t('entityCreate.suggestedToast'),
        color: 'green',
      })
      emit('saved', saved)
    } else if (isCreate.value) {
      const body = {
        type: form.value.type || 'model',
        title: form.value.title.trim(),
        description: form.value.description || null,
        code: code.value,
        data: {},
        status,
        data_source_ids: selectedIds.value,
        parameters: specsPayload(),
      }
      const { data, error } = await useMyFetch<any>('/api/entities', { method: 'POST', body })
      if (error.value) throw error.value
      const saved: any = data.value
      // Fill the snapshot so the row opens with data; a failed run still
      // leaves a saved query the user can fix here — but say so.
      await runAfterSave(String(saved.id))
      toast.add({
        title: saved?.global_status === 'approved' && saved?.status === 'published'
          ? t('entityCreate.publishedToast') : t('entityCreate.suggestedToast'),
        color: 'green',
      })
      emit('saved', saved)
    } else {
      const body: any = {
        type: form.value.type || 'model',
        title: form.value.title.trim(),
        description: form.value.description || null,
        code: code.value,
        data_source_ids: selectedIds.value,
      }
      // Declarations travel only when there are (or were) any: a row saved
      // before declarations existed keeps working untouched.
      const specs = specsPayload()
      const hadSpecs = (props.detail?.parameters || []).length > 0
      if (specs.length || hadSpecs) body.parameters = specs
      // Status is only offered to entity managers; leaving it out keeps the
      // owner tier from being refused for a field they never saw.
      if (canCreateEntities.value) body.status = status
      const { data, error } = await useMyFetch<any>(`/api/entities/${props.entityId}`, { method: 'PUT', body })
      if (error.value) throw error.value
      // The snapshot follows the code AND the declarations (a default or a
      // source change alters what a run produces).
      const declsChanged = JSON.stringify(specs) !== JSON.stringify(savedSpecs.value)
      if (code.value !== savedCode.value || declsChanged) {
        await runAfterSave(String(props.entityId))
      }
      emit('saved', data.value)
    }
    open.value = false
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || t('entityCreate.saveFailed')
    toast.add({ title: t('entityCreate.saveFailed'), description: errorMsg.value, color: 'red' })
  } finally {
    saving.value = false
  }
}
</script>
