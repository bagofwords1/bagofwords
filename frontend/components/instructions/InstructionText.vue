<template>
  <span v-if="!markdown" class="whitespace-pre-wrap text-[13px] leading-relaxed text-gray-900">
    <template v-for="(segment, i) in segments" :key="i">
      <span
        v-if="segment.ref || segment.mention"
        class="inline-flex items-center gap-0.5 px-1 py-0.5 rounded bg-indigo-50 border border-indigo-100 text-[11px] font-sans font-medium text-indigo-700 align-baseline"
      >
        <template v-if="segment.ref">
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
        </template>
        <span v-else>@{{ segment.mention }}</span>
      </span>
      <span v-else>{{ segment.text }}</span>
    </template>
  </span>
  <div v-else class="instruction-prose" v-html="renderedHtml" />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface RawReference {
  id?: string
  type?: string
  object_type?: string
  name?: string | null
  display_text?: string | null
  data_source_type?: string | null
}

interface Reference {
  id: string
  type: string
  name: string | null
  data_source_type: string | null
}

const props = defineProps<{
  text: string
  references?: RawReference[]
  prose?: boolean  // kept for compatibility, no longer affects font
  markdown?: boolean
}>()

const normalizedRefs = computed((): Reference[] =>
  (props.references || []).map(r => ({
    id: r.id || '',
    type: r.type || r.object_type || '',
    name: r.name || r.display_text || null,
    data_source_type: r.data_source_type || null,
  }))
)

const refByName = computed(() => {
  const map = new Map<string, Reference>()
  for (const ref of normalizedRefs.value) {
    const key = (ref.name || '').toLowerCase()
    if (key) map.set(key, ref)
  }
  return map
})

interface Segment {
  text?: string
  ref?: Reference
  mention?: string
  raw?: string
}

const segments = computed((): Segment[] => {
  const result: Segment[] = []
  const parts = props.text.split(/(@[A-Za-z_][A-Za-z0-9_]*)/)
  for (const part of parts) {
    if (part.startsWith('@')) {
      const word = part.slice(1)
      const ref = refByName.value.get(word.toLowerCase())
      if (ref) {
        result.push({ ref, raw: word })
      } else {
        result.push({ mention: word })
      }
    } else {
      result.push({ text: part })
    }
  }
  return result
})

// ─── Markdown rendering ──────────────────────────────────────────────────────
// Mirrors InstructionEditor's pipeline so read-only and edit views render identically.

const md = new MarkdownIt({ html: true, breaks: false, linkify: false })

function preprocessMentions(text: string): string {
  return text.replace(
    /@([A-Za-z_][A-Za-z0-9_]*(?:[.\-][A-Za-z0-9_]+)*|"[^"]+")/g,
    (_, captured) => {
      const label = captured.startsWith('"') && captured.endsWith('"')
        ? captured.slice(1, -1)
        : captured
      const safe = label.replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      return `<span class="instruction-mention">@${safe}</span>`
    }
  )
}

const renderedHtml = computed(() => {
  if (!props.text?.trim()) return ''
  return md.render(preprocessMentions(props.text))
})
</script>

<style scoped>
.instruction-prose {
  font-size: 13px;
  line-height: 1.625;
  color: #111827;
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
}

.instruction-prose :deep(h1) { font-size: 1.25em; font-weight: 600; margin: 0.75em 0 0.25em; color: #111827; }
.instruction-prose :deep(h2) { font-size: 1.1em; font-weight: 600; margin: 0.6em 0 0.2em; color: #111827; }
.instruction-prose :deep(h3) { font-size: 1em; font-weight: 600; margin: 0.5em 0 0.15em; color: #111827; }

.instruction-prose :deep(p) { margin-bottom: 0.5em; }
.instruction-prose :deep(p:last-child) { margin-bottom: 0; }

.instruction-prose :deep(ul) { padding-left: 1.25em; list-style: disc; margin-bottom: 0.5em; }
.instruction-prose :deep(ol) { padding-left: 1.25em; list-style: decimal; margin-bottom: 0.5em; }
.instruction-prose :deep(li) { margin-bottom: 0.2em; }

.instruction-prose :deep(code) {
  background: #f3f4f6;
  padding: 1px 4px;
  border-radius: 3px;
  font-family: ui-monospace, monospace;
  font-size: 0.9em;
  color: #374151;
}

.instruction-prose :deep(pre) {
  background: #f9fafb;
  padding: 10px 12px;
  border-radius: 6px;
  margin-bottom: 0.5em;
  overflow-x: auto;
}
.instruction-prose :deep(pre code) {
  background: none;
  padding: 0;
  font-size: 11px;
  line-height: 1.5;
}

.instruction-prose :deep(blockquote) {
  border-left: 3px solid #e5e7eb;
  padding-left: 1em;
  margin: 0.5em 0;
  color: #6b7280;
}

.instruction-prose :deep(.instruction-mention) {
  background-color: rgba(99, 102, 241, 0.12);
  color: #4338ca;
  border-radius: 4px;
  padding: 1px 4px;
  font-weight: 500;
  font-size: 0.95em;
  white-space: nowrap;
}
</style>
