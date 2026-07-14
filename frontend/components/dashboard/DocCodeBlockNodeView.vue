<template>
  <NodeViewWrapper class="doc-codeblock-node">
    <!-- Mermaid fence: rendered diagram, source only while the caret is inside -->
    <template v-if="isMermaid">
      <pre v-show="showSource" class="doc-codeblock-pre"><NodeViewContent as="code" class="language-mermaid" /></pre>
      <div
        contenteditable="false"
        class="doc-mermaid-preview"
        :class="{ 'doc-mermaid-preview--editing': showSource }"
        :title="$t('docEditor.editDiagramSource')"
        @mousedown.prevent="focusSource"
      >
        <DocMermaid :code="previewCode" :keep-last-good="true" />
      </div>
    </template>

    <!-- Any other language: the default code block rendering -->
    <pre v-else class="doc-codeblock-pre"><NodeViewContent as="code" :class="language ? `language-${language}` : undefined" /></pre>
  </NodeViewWrapper>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { NodeViewWrapper, NodeViewContent, nodeViewProps } from '@tiptap/vue-3'
import DocMermaid from '~/components/dashboard/DocMermaid.vue'

const props = defineProps(nodeViewProps)

const language = computed(() =>
  String(props.node.attrs.language || '').trim().split(/\s+/)[0].toLowerCase()
)
const isMermaid = computed(() => language.value === 'mermaid')

// Source is shown only while the selection sits inside this code block (or the
// node itself is selected), so an owner opening their doc sees the diagram —
// not the fence source — exactly like the read-only DocViewer.
const selectionInside = ref(false)
const showSource = computed(() => props.selected || selectionInside.value)

function updateSelection() {
  try {
    const pos = props.getPos()
    if (typeof pos !== 'number') { selectionInside.value = false; return }
    const { from, to } = props.editor.state.selection
    selectionInside.value = from >= pos && to <= pos + props.node.nodeSize
  } catch {
    selectionInside.value = false
  }
}
updateSelection()
props.editor.on('selectionUpdate', updateSelection)
onBeforeUnmount(() => props.editor.off('selectionUpdate', updateSelection))

// When keyboard navigation moves the caret into the (hidden) source, the DOM
// selection was placed while the <pre> had display:none — re-focus after the
// toggle so the caret is drawn where the state says it is.
watch(showSource, (visible) => {
  if (visible && props.editor.view.hasFocus()) {
    nextTick(() => props.editor.commands.focus())
  }
})

// Clicking the diagram puts the caret at the end of the source, revealing it.
function focusSource() {
  try {
    const pos = props.getPos()
    if (typeof pos !== 'number') return
    props.editor.chain().focus().setTextSelection(pos + props.node.nodeSize - 1).run()
  } catch { /* node was removed mid-click */ }
}

// Debounced preview: re-render the diagram shortly after typing pauses, and
// immediately when the source collapses (caret left the block).
const previewCode = ref(props.node.textContent)
let debounce: ReturnType<typeof setTimeout> | undefined
watch(() => props.node.textContent, (code) => {
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => { previewCode.value = code }, 400)
})
watch(showSource, (visible) => {
  if (!visible) {
    if (debounce) clearTimeout(debounce)
    previewCode.value = props.node.textContent
  }
})
onBeforeUnmount(() => { if (debounce) clearTimeout(debounce) })
</script>

<style scoped>
.doc-mermaid-preview { cursor: pointer; }
.doc-mermaid-preview--editing { opacity: 0.85; }
</style>
