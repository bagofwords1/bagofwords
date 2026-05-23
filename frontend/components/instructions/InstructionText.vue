<template>
  <span :class="prose ? 'whitespace-pre-wrap text-sm leading-relaxed text-gray-900' : 'whitespace-pre-wrap text-xs leading-relaxed font-mono text-gray-800'">
    <template v-for="(segment, i) in segments" :key="i">
      <span
        v-if="segment.ref"
        class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-indigo-50 border border-indigo-100 text-[11px] font-sans font-medium text-indigo-700 align-baseline"
      >
        <DataSourceIcon
          v-if="segment.ref.data_source_type"
          :type="segment.ref.data_source_type"
          class="h-3 flex-shrink-0"
        />
        <Icon
          v-else-if="segment.ref.type === 'instruction'"
          name="heroicons:document-text"
          class="w-3 h-3 flex-shrink-0 text-indigo-400"
        />
        <Icon
          v-else
          name="heroicons:table-cells"
          class="w-3 h-3 flex-shrink-0 text-blue-400"
        />
        <Icon
          v-if="segment.ref.type === 'connection_tool'"
          name="heroicons:wrench-screwdriver"
          class="w-2.5 h-2.5 flex-shrink-0 text-indigo-300"
        />
        <span>@{{ segment.ref.name || segment.raw }}</span>
      </span>
      <span v-else>{{ segment.text }}</span>
    </template>
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface Reference {
  id: string
  type: 'metadata_resource' | 'datasource_table' | 'instruction' | 'connection_tool'
  name?: string | null
  data_source_type?: string | null
  display_text?: string | null
}

const props = defineProps<{
  text: string
  references?: Reference[]
  prose?: boolean  // true = chat bubble style (sans-serif, normal size)
}>()

// Build a lookup: lowercased name → reference
const refByName = computed(() => {
  const map = new Map<string, Reference>()
  for (const ref of props.references || []) {
    const key = (ref.name || ref.display_text || '').toLowerCase()
    if (key) map.set(key, ref)
  }
  return map
})

interface Segment {
  text?: string
  ref?: Reference
  raw?: string  // the word after @, used as fallback label
}

const segments = computed((): Segment[] => {
  const result: Segment[] = []
  // Split on @word boundaries — word chars + underscore
  const parts = props.text.split(/(@[A-Za-z_][A-Za-z0-9_]*)/)
  for (const part of parts) {
    if (part.startsWith('@')) {
      const word = part.slice(1)
      const ref = refByName.value.get(word.toLowerCase())
      if (ref) {
        result.push({ ref, raw: word })
      } else {
        result.push({ text: part })
      }
    } else {
      result.push({ text: part })
    }
  }
  return result
})
</script>
