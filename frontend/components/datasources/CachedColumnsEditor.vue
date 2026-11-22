<template>
  <!-- A cached table's columns with what the source catalog says about each:
       description, where it was copied from, and whether its values may be
       summed across rows. Both are editable; an edit is kept on every refresh
       (see utils/columnMetadata.ts and the backend's fast/column_metadata.py). -->
  <div data-testid="cq-columns" class="mt-4">
    <div class="flex items-baseline justify-between gap-3 mb-1.5">
      <h3 class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ t('cachedColumns.title') }}</h3>
      <p class="text-2xs text-ink-muted">{{ t('cachedColumns.hint') }}</p>
    </div>

    <div v-if="undecided.length" data-testid="cq-columns-undecided"
         class="mb-2 flex items-start gap-2 rounded-md bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 px-3 py-2">
      <UIcon name="heroicons-question-mark-circle" class="w-4 h-4 text-amber-600 mt-0.5 flex-shrink-0" />
      <p class="text-xs text-amber-800 dark:text-amber-200">
        {{ t('cachedColumns.undecided', { columns: undecided.join(', ') }) }}
      </p>
    </div>

    <div class="border border-gray-200 dark:border-gray-800 rounded-md divide-y divide-gray-100 dark:divide-gray-800">
      <div class="grid grid-cols-[minmax(0,11rem)_minmax(0,1fr)_10.5rem] gap-3 px-2.5 py-1.5 text-2xs font-medium text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-900 rounded-t-md">
        <div>{{ t('cachedColumns.column') }}</div>
        <div>{{ t('cachedColumns.description') }}</div>
        <div>{{ t('cachedColumns.aggregation') }}</div>
      </div>

      <div v-for="col in columns" :key="col.name" :data-testid="`cq-column-${col.name}`"
           class="grid grid-cols-[minmax(0,11rem)_minmax(0,1fr)_10.5rem] gap-3 px-2.5 py-2 items-start">
        <!-- name, type, flags -->
        <div class="min-w-0">
          <div class="text-xs font-mono text-gray-800 dark:text-gray-200 truncate" :title="col.name" dir="ltr">{{ col.name }}</div>
          <div class="text-2xs text-ink-muted truncate" dir="ltr">{{ col.dtype }}</div>
          <div class="mt-1 flex flex-wrap gap-1">
            <UTooltip v-if="hasEdit(col)" :text="t('cachedColumns.editedHint')">
              <span :data-testid="`cq-column-edited-${col.name}`"
                    class="text-2xs px-1 py-0.5 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300">{{ t('cachedColumns.edited') }}</span>
            </UTooltip>
            <UTooltip v-if="driftOf(col) && !reviewed.has(col.name)" :text="t('cachedColumns.driftHint')">
              <span :data-testid="`cq-column-drift-${col.name}`"
                    class="inline-flex items-center gap-0.5 text-2xs px-1 py-0.5 rounded bg-amber-50 dark:bg-amber-900/30 text-amber-700 dark:text-amber-300">
                <UIcon name="heroicons-exclamation-triangle" class="w-3 h-3" />{{ t('cachedColumns.drift') }}
              </span>
            </UTooltip>
          </div>
        </div>

        <!-- description + origin -->
        <div class="min-w-0">
          <textarea
            :value="shownDescription(edits, col)" :rows="shownDescription(edits, col).length > 90 ? 2 : 1" v-text-dir
            :data-testid="`cq-column-description-${col.name}`"
            :placeholder="t('cachedColumns.descriptionPlaceholder')"
            class="w-full text-xs border border-gray-300 dark:border-gray-700 rounded-md px-2 py-1 dark:bg-gray-900 dark:text-white resize-y min-h-[1.75rem]"
            @input="onDescription(col, ($event.target as HTMLTextAreaElement).value)"
          />
          <div v-if="showsSource(col, shownDescription(edits, col))" class="mt-0.5 text-2xs text-ink-muted break-words" :data-testid="`cq-column-source-${col.name}`">
            <template v-if="origin(col)!.kind === 'derived'">
              <i18n-t scope="global" keypath="cachedColumns.computedAs" tag="span">
                <template #expression><code class="font-mono" dir="ltr">{{ origin(col)!.text }}</code></template>
              </i18n-t>
            </template>
            <template v-else>
              <i18n-t scope="global" :keypath="origin(col)!.kind === 'measure' ? 'cachedColumns.measureFrom' : 'cachedColumns.copiedFrom'" tag="span">
                <template #source><code class="font-mono" dir="ltr">{{ origin(col)!.text }}</code></template>
              </i18n-t>
            </template>
          </div>
          <div v-if="driftOf(col)?.previousExpression && !reviewed.has(col.name)" class="mt-0.5 text-2xs text-amber-700 dark:text-amber-400 break-words">
            <i18n-t scope="global" keypath="cachedColumns.driftPrevious" tag="span">
              <template #expression><code class="font-mono" dir="ltr">{{ driftOf(col)!.previousExpression }}</code></template>
            </i18n-t>
          </div>
          <div class="mt-0.5 flex flex-wrap gap-x-3 text-2xs">
            <button v-if="hasEdit(col)" type="button" class="text-blue-600 dark:text-blue-400 hover:underline"
                    :data-testid="`cq-column-reset-${col.name}`" @click="onReset(col)">{{ t('cachedColumns.reset') }}</button>
            <button v-if="driftOf(col) && !reviewed.has(col.name)" type="button" class="text-amber-700 dark:text-amber-400 hover:underline"
                    :data-testid="`cq-column-review-${col.name}`" @click="emit('review', col.name)">{{ t('cachedColumns.markReviewed') }}</button>
          </div>
        </div>

        <!-- re-aggregation -->
        <div class="min-w-0">
          <select
            :value="stateValue(col)" :data-testid="`cq-column-additive-${col.name}`"
            class="w-full text-xs border rounded-md px-1.5 py-1 dark:bg-gray-900 dark:text-white"
            :class="stateValue(col) === 'unknown'
              ? 'border-amber-400 dark:border-amber-600'
              : 'border-gray-300 dark:border-gray-700'"
            @change="onAdditive(col, ($event.target as HTMLSelectElement).value)"
          >
            <option value="additive">{{ t('cachedColumns.additive') }}</option>
            <option value="notAdditive">{{ t('cachedColumns.notAdditive') }}</option>
            <option value="unknown">{{ t('cachedColumns.unknown') }}</option>
          </select>
          <div class="mt-0.5 text-2xs text-ink-muted" :data-testid="`cq-column-reason-${col.name}`">{{ reasonText(col) }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import {
  additiveReason, driftOf, shownAdditive, shownDescription, showsSource, sourceLabel, undecidedColumns, withEdit,
  type ColumnEdit,
} from '~/utils/columnMetadata'

const props = defineProps<{
  columns: any[]
  // The admin's edits, keyed by column name. Owned by the parent (v-model).
  edits: Record<string, ColumnEdit>
  // Columns whose drift flag the admin has marked reviewed in this session.
  reviewed: Set<string>
}>()
const emit = defineEmits<{
  (e: 'update:edits', value: Record<string, ColumnEdit>): void
  (e: 'review', name: string): void
}>()
const { t, te } = useI18n()

const undecided = computed(() => undecidedColumns(props.columns, props.edits))
const origin = (col: any) => sourceLabel(col)
const hasEdit = (col: any) => !!props.edits[col.name]

function stateValue(col: any): string {
  const value = shownAdditive(props.edits, col)
  return value === true ? 'additive' : value === false ? 'notAdditive' : 'unknown'
}

function reasonText(col: any): string {
  const edit = props.edits[col.name]
  const reason = edit && 'additive' in edit ? 'set_by_admin' : additiveReason(col)
  if (reason && te(`cachedColumns.reasons.${reason}`)) return t(`cachedColumns.reasons.${reason}`)
  const state = stateValue(col)
  return state === 'unknown' ? t('cachedColumns.unknownHint') : ''
}

function onDescription(col: any, value: string) {
  emit('update:edits', withEdit(props.edits, col, 'description', value))
}

function onAdditive(col: any, value: string) {
  const additive = value === 'additive' ? true : value === 'notAdditive' ? false : 'unknown'
  emit('update:edits', withEdit(props.edits, col, 'additive', additive))
}

function onReset(col: any) {
  const next = { ...props.edits }
  delete next[col.name]
  emit('update:edits', next)
}
</script>
