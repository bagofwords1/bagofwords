<template>
  <div v-if="rows.length" class="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
    <table class="w-full text-[11px] border-collapse">
      <thead>
        <tr class="bg-gray-50 dark:bg-gray-900/60">
          <th
            v-for="(h, i) in header"
            :key="i"
            class="px-2 py-1 text-start font-medium text-gray-600 dark:text-gray-300 whitespace-nowrap border-b border-gray-200 dark:border-gray-700"
            dir="auto"
          >
            {{ h }}
          </th>
          <th v-if="hiddenCols" class="px-2 py-1 text-start font-normal text-gray-400 whitespace-nowrap border-b border-gray-200 dark:border-gray-700">
            +{{ hiddenCols }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(row, r) in rows"
          :key="r"
          class="odd:bg-white even:bg-gray-50/50 dark:odd:bg-transparent dark:even:bg-gray-900/30"
        >
          <td
            v-for="(cell, c) in row"
            :key="c"
            class="px-2 py-1 text-start text-gray-700 dark:text-gray-300 whitespace-nowrap max-w-[220px] truncate"
            :title="cell"
            dir="auto"
          >
            {{ cell }}
          </td>
          <td v-if="hiddenCols" class="px-2 py-1 text-gray-300 dark:text-gray-600">…</td>
        </tr>
      </tbody>
    </table>
  </div>
  <div v-if="footer" class="mt-1 text-[10px] text-gray-400 dark:text-gray-500">{{ footer }}</div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = withDefaults(defineProps<{
  csv?: string | null
  /** The file's REAL row count, which differs from what `csv` holds when the
   *  read was row-capped — the footer must not imply we are showing it all. */
  rowCount?: number | null
  colCount?: number | null
  maxRows?: number
  maxCols?: number
}>(), {
  csv: '',
  rowCount: null,
  colCount: null,
  maxRows: 10,
  maxCols: 8,
})

const { t } = useI18n()

/**
 * Minimal RFC-4180 parse: quoted fields may contain commas, newlines, and ""
 * escapes. A naive split(',') mangles exactly the files people care about
 * (addresses, descriptions, anything with a comma in it).
 */
function parseCsv(text: string, rowLimit: number): string[][] {
  const out: string[][] = []
  let row: string[] = []
  let field = ''
  let quoted = false

  for (let i = 0; i < text.length; i++) {
    const ch = text[i]
    if (quoted) {
      if (ch === '"') {
        if (text[i + 1] === '"') { field += '"'; i++ } else { quoted = false }
      } else {
        field += ch
      }
      continue
    }
    if (ch === '"') { quoted = true; continue }
    if (ch === ',') { row.push(field); field = ''; continue }
    if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++
      row.push(field)
      field = ''
      out.push(row)
      row = []
      if (out.length > rowLimit) return out
      continue
    }
    field += ch
  }
  if (field.length || row.length) { row.push(field); out.push(row) }
  return out
}

// +1 for the header line.
const parsed = computed(() => parseCsv(String(props.csv || ''), props.maxRows + 1))
const header = computed(() => (parsed.value[0] || []).slice(0, props.maxCols))
const rows = computed(() =>
  parsed.value.slice(1, props.maxRows + 1).map((r) => r.slice(0, props.maxCols)),
)

const totalCols = computed(() => props.colCount ?? (parsed.value[0]?.length || 0))
const hiddenCols = computed(() => Math.max(0, totalCols.value - props.maxCols))

const footer = computed(() => {
  if (!rows.value.length) return ''
  const total = props.rowCount
  if (total != null && total > rows.value.length) {
    return t('filePreview.showingRows', { n: rows.value.length, total })
  }
  return ''
})
</script>
