<template>
  <NodeViewWrapper class="doc-viz-node" contenteditable="false">
    <DocVizEmbed :viz="viz" />
  </NodeViewWrapper>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NodeViewWrapper, nodeViewProps } from '@tiptap/vue-3'
import DocVizEmbed from '~/components/dashboard/DocVizEmbed.vue'

const props = defineProps(nodeViewProps)

// The editor stores a computed Map of viz data under storage.docVizMap
const viz = computed(() => {
  const id = String(props.node.attrs.vizId || '').toLowerCase()
  const mapRef: any = (props.editor.storage as any).docVizMap
  const map = mapRef?.value ?? mapRef
  return map?.get?.(id) || null
})
</script>
