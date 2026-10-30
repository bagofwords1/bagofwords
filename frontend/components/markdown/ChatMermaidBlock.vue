<template>
  <MermaidBlockNode v-bind="$attrs" :node="node" @export="onExport" />
</template>

<script setup lang="ts">
// markstream's Mermaid block (chat replies, shared conversations, notes),
// unchanged except for its Export button: by default it downloads the
// on-screen SVG; here it downloads a PNG instead, same as doc diagrams.
// Registered for every <MarkdownRender> in plugins/markdown-mermaid-png.ts.
import { MermaidBlockNode, type MermaidBlockEvent } from 'markstream-vue'
import { downloadMermaidPng } from '~/utils/mermaidExport'

defineOptions({ inheritAttrs: false })
const props = defineProps<{ node: { code?: string; [key: string]: any } }>()
const { t } = useI18n()

async function onExport(ev: MermaidBlockEvent) {
  const code = props.node?.code || ''
  if (!code.trim()) return // nothing to export: keep markstream's SVG fallback
  ev.preventDefault()
  try {
    await downloadMermaidPng(code)
  } catch {
    useToast().add({ title: t('docViewer.diagramExportFailed'), color: 'red' })
  }
}
</script>
