<template>
  <div ref="containerRef" class="monaco-diff-container" :style="{ height: height }"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'

interface Props {
  original: string
  modified: string
  height?: string
  language?: string
}

const props = withDefaults(defineProps<Props>(), {
  height: '200px',
  language: 'plaintext'
})

const containerRef = ref<HTMLElement | null>(null)
let diffEditor: any = null

onMounted(async () => {
  if (!containerRef.value) return

  // Dynamic import monaco
  const monaco = await import('monaco-editor')

  // Create diff editor with inline view (not side-by-side)
  diffEditor = monaco.editor.createDiffEditor(containerRef.value, {
    readOnly: true,
    renderSideBySide: false, // Inline diff view
    automaticLayout: true,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    wordWrap: 'on',
    lineNumbers: 'off',
    glyphMargin: false,
    folding: false,
    lineDecorationsWidth: 0,
    lineNumbersMinChars: 0,
    renderIndicators: true,
    originalEditable: false,
    fontSize: 12,
    scrollbar: {
      vertical: 'auto',
      horizontal: 'auto',
      verticalScrollbarSize: 8,
      horizontalScrollbarSize: 8
    }
  })

  // Set the models
  const originalModel = monaco.editor.createModel(props.original, props.language)
  const modifiedModel = monaco.editor.createModel(props.modified, props.language)

  diffEditor.setModel({
    original: originalModel,
    modified: modifiedModel
  })
})

// Watch for content changes
watch(() => [props.original, props.modified], async ([newOriginal, newModified]) => {
  if (!diffEditor) return

  const monaco = await import('monaco-editor')
  const model = diffEditor.getModel()

  if (model) {
    model.original.setValue(newOriginal)
    model.modified.setValue(newModified)
  }
})

onBeforeUnmount(() => {
  if (diffEditor) {
    const model = diffEditor.getModel()
    if (model) {
      model.original?.dispose()
      model.modified?.dispose()
    }
    diffEditor.dispose()
    diffEditor = null
  }
})
</script>

<style scoped>
.monaco-diff-container {
  width: 100%;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  overflow: hidden;
}

/* Override Monaco diff editor styles for a cleaner look */
.monaco-diff-container :deep(.monaco-editor) {
  padding: 4px 0;
}

.monaco-diff-container :deep(.view-overlays .current-line) {
  display: none;
}
</style>
