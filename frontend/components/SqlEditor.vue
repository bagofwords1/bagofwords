<template>
  <!-- Queries read left to right in every UI language, so the editor stays
       LTR even inside an RTL page. -->
  <div
    dir="ltr"
    class="relative rounded-md border border-gray-200 dark:border-gray-700 overflow-hidden bg-white dark:bg-gray-950 focus-within:border-blue-500 dark:focus-within:border-blue-500 transition-colors"
    :style="{ height: `${height}px` }"
    :data-testid="testid"
  >
    <!-- MonacoEditor awaits the monaco chunk in its setup, so it suspends on
         mount; the Suspense fallback covers that beat and ClientOnly covers
         SSR. -->
    <ClientOnly>
      <Suspense>
        <MonacoEditor
          :model-value="modelValue"
          :lang="monacoLang"
          :options="options"
          class="h-full"
          @update:model-value="(v: string) => emit('update:modelValue', v)"
          @load="onLoad"
        />
        <template #fallback>
          <pre class="h-full m-0 px-3 py-2 text-xs font-mono text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{{ modelValue }}</pre>
        </template>
      </Suspense>
      <template #fallback>
        <pre class="h-full m-0 px-3 py-2 text-xs font-mono text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{{ modelValue }}</pre>
      </template>
    </ClientOnly>
  </div>
</template>

<script setup lang="ts">
// The one editor for queries written against a source: the table data preview
// and the custom table editor both use it, so a query looks and behaves the
// same wherever it is typed.
//
// Compact by default: no gutter, no minimap, and the box grows with the query
// between `minHeight` and `maxHeight` instead of reserving space. Ctrl/⌘ +
// Enter emits `run`. Given a `connectionId`, it suggests that connection's
// tables and columns (see utils/sqlCompletion.ts).
//
// `language` is the source's query language. Monaco ships SQL; HogQL is SQL
// close enough for highlighting and suggestions, and DAX has no grammar there,
// so it is shown as plain text rather than mis-highlighted as SQL.
import type { SqlCatalog } from '~/utils/sqlCompletion'
import { completionRegistry as registry, registerSqlCompletions } from '~/utils/sqlCompletionProvider'

const props = withDefaults(defineProps<{
  modelValue: string
  language?: 'sql' | 'hogql' | 'dax' | string
  readOnly?: boolean
  minHeight?: number
  maxHeight?: number
  // Connection whose tables and columns are suggested. Omit for none.
  connectionId?: string
  testid?: string
}>(), {
  language: 'sql',
  readOnly: false,
  minHeight: 64,
  maxHeight: 280,
  connectionId: undefined,
  testid: undefined,
})

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void
  // Ctrl/⌘ + Enter, the run shortcut every query editor in the app uses.
  (e: 'run'): void
}>()

const colorMode = useColorMode()
const isDark = computed(() => colorMode.value === 'dark')
const completes = computed(() => !!props.connectionId && !['dax', 'json', 'text'].includes(props.language))

const monacoLang = computed(() => (['dax', 'text'].includes(props.language) ? 'plaintext' : props.language === 'json' ? 'json' : 'sql'))

const options = computed(() => ({
  theme: isDark.value ? 'vs-dark' : 'vs',
  readOnly: props.readOnly,
  automaticLayout: true,
  minimap: { enabled: false },
  wordWrap: 'on',
  fontSize: 12,
  lineHeight: 18,
  lineNumbers: 'off',
  glyphMargin: false,
  folding: false,
  lineDecorationsWidth: 10,
  lineNumbersMinChars: 0,
  scrollBeyondLastLine: false,
  renderLineHighlight: 'none',
  overviewRulerLanes: 0,
  hideCursorInOverviewRuler: true,
  overviewRulerBorder: false,
  padding: { top: 8, bottom: 8 },
  scrollbar: { alwaysConsumeMouseWheel: false, verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
  // Suggestions come from the connection's catalog, not from words already
  // in the buffer, which would only repeat what was typed.
  wordBasedSuggestions: completes.value ? 'off' : 'currentDocument',
  quickSuggestions: completes.value ? { other: true, comments: false, strings: false } : false,
  suggestOnTriggerCharacters: true,
  suggest: { showWords: !completes.value, preview: true },
  fixedOverflowWidgets: true,
}))

// Grow with the query, within bounds.
const height = ref(props.minHeight)

// ---------------------------------------------------------------------------
// Suggestions. Monaco registers a completion provider per language for every
// editor on the page, so one provider is registered once and looks up which
// connection the asking editor belongs to by its model URI.
// ---------------------------------------------------------------------------
const catalogs = useState<Record<string, SqlCatalog | null>>('sqlEditorCatalogs', () => ({}))

let monacoApi: any = null
let modelUri = ''

async function loadCatalog(connectionId: string) {
  if (connectionId in catalogs.value) return
  catalogs.value[connectionId] = null
  try {
    const { data, error } = await useMyFetch(`/connections/${connectionId}/completions`, { method: 'GET' })
    if (!error.value && data.value) catalogs.value[connectionId] = data.value as SqlCatalog
    else delete catalogs.value[connectionId]
  } catch {
    delete catalogs.value[connectionId]
  }
}

watch(() => [props.connectionId, completes.value] as const, ([id, on]) => {
  if (id && on) loadCatalog(id)
  if (modelUri) registry.models.set(modelUri, on && id ? id : '')
}, { immediate: true })

async function onLoad(editor: any) {
  try {
    monacoApi = await useMonaco()
  } catch {
    monacoApi = null
  }
  if (!monacoApi || !editor) return
  monacoApi.editor.setTheme(options.value.theme)
  editor.addCommand(monacoApi.KeyMod.CtrlCmd | monacoApi.KeyCode.Enter, () => emit('run'))

  const fit = () => {
    const h = Math.min(props.maxHeight, Math.max(props.minHeight, editor.getContentHeight() + 2))
    if (h !== height.value) height.value = h
  }
  editor.onDidContentSizeChange(fit)
  fit()

  modelUri = editor.getModel()?.uri?.toString() || ''
  if (modelUri) registry.models.set(modelUri, completes.value && props.connectionId ? props.connectionId : '')
  registerSqlCompletions(monacoApi, () => catalogs.value)
}

onBeforeUnmount(() => {
  if (modelUri) registry.models.delete(modelUri)
})

// Monaco's theme is global, not a per-editor option, so a color-mode switch
// has to be applied explicitly.
watch(isDark, () => {
  monacoApi?.editor.setTheme(options.value.theme)
})
</script>

