<template>
  <!-- Queries read left to right in every UI language, so the editor stays
       LTR even inside an RTL page. -->
  <div
    dir="ltr"
    class="relative rounded-md border border-line overflow-hidden bg-surface"
    :style="{ height }"
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
          <div class="flex items-center justify-center h-full">
            <Spinner class="w-4 h-4" />
          </div>
        </template>
      </Suspense>
      <template #fallback>
        <div class="flex items-center justify-center h-full">
          <Spinner class="w-4 h-4" />
        </div>
      </template>
    </ClientOnly>
  </div>
</template>

<script setup lang="ts">
// The one editor for queries a person writes against a source: the custom
// table editor and the table data preview both use it, so a query looks and
// behaves the same wherever it is typed.
//
// `language` is the source's query language. Monaco ships SQL; HogQL is SQL
// close enough for highlighting, and DAX has no grammar there, so it is shown
// as plain text rather than mis-highlighted as SQL.
const props = withDefaults(defineProps<{
  modelValue: string
  language?: 'sql' | 'hogql' | 'dax' | string
  readOnly?: boolean
  height?: string
  testid?: string
}>(), {
  language: 'sql',
  readOnly: false,
  height: '160px',
  testid: undefined,
})

const emit = defineEmits<{
  (e: 'update:modelValue', v: string): void
  // Ctrl/⌘ + Enter, the run shortcut every query editor in the app uses.
  (e: 'run'): void
}>()

const colorMode = useColorMode()
const isDark = computed(() => colorMode.value === 'dark')

const monacoLang = computed(() => (props.language === 'dax' ? 'plaintext' : 'sql'))

const options = computed(() => ({
  theme: isDark.value ? 'vs-dark' : 'vs',
  readOnly: props.readOnly,
  automaticLayout: true,
  minimap: { enabled: false },
  wordWrap: 'on',
  fontSize: 12,
  lineNumbers: 'off',
  glyphMargin: false,
  folding: false,
  lineDecorationsWidth: 8,
  scrollBeyondLastLine: false,
  renderLineHighlight: 'none',
  padding: { top: 6, bottom: 6 },
  scrollbar: { alwaysConsumeMouseWheel: false },
}))

let monacoApi: any = null

async function onLoad(editor: any) {
  try {
    monacoApi = await useMonaco()
  } catch {
    monacoApi = null
  }
  if (!monacoApi || !editor) return
  monacoApi.editor.setTheme(options.value.theme)
  editor.addCommand(
    monacoApi.KeyMod.CtrlCmd | monacoApi.KeyCode.Enter,
    () => emit('run'),
  )
}

// Monaco's theme is global, not a per-editor option, so a color-mode switch
// has to be applied explicitly.
watch(isDark, () => {
  monacoApi?.editor.setTheme(options.value.theme)
})
</script>
