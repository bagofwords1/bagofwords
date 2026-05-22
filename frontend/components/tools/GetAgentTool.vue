<template>
  <div class="mt-1">
    <Transition name="fade" appear>
      <div class="mb-2 flex items-center text-xs text-gray-500">
        <span v-if="status === 'running'" class="tool-shimmer flex items-center">
          <Icon name="heroicons-magnifying-glass" class="w-3 h-3 me-1 text-gray-400" />
          <span>Loading <span class="font-medium">{{ requestedName }}</span>…</span>
        </span>
        <span v-else-if="!agent" class="text-gray-600">
          <Icon name="heroicons-x-circle" class="w-3 h-3 me-1 text-gray-400 inline" />
          {{ errorMessage || `Agent '${requestedName}' not found` }}
        </span>
        <span v-else class="text-gray-700 flex items-center">
          <DataSourceIcon :type="primaryType" class="h-2 me-1" />
          <span class="font-medium">{{ agent.name }}</span>
          <span
            v-if="!agent.is_public"
            class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 ms-2 flex-shrink-0"
          >private</span>
        </span>
      </div>
    </Transition>

    <div v-if="agent" class="text-xs text-gray-600 ms-1 space-y-2">
      <p v-if="agent.description" class="text-gray-500 leading-snug">{{ agent.description }}</p>

      <!-- Connections -->
      <section v-if="agent.connections && agent.connections.length">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('connections')"
        >
          <Icon :name="isOpen('connections') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-link" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Connections</span>
          <span class="text-gray-400">{{ agent.connections.length }}</span>
        </button>
        <ul v-if="isOpen('connections')" class="ps-5 mt-1 space-y-0.5">
          <li v-for="c in agent.connections" :key="c.id" class="flex items-center gap-2">
            <DataSourceIcon :type="c.type" class="h-2" />
            <code class="text-[11px] text-gray-700">{{ c.name }}</code>
            <span class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 uppercase tracking-wide">{{ c.type }}</span>
            <span v-if="!c.is_indexed" class="text-[9px] px-1 py-0.5 rounded bg-yellow-50 text-yellow-700">indexing</span>
            <span class="text-[10px] text-gray-400 ms-auto">{{ c.table_count }} tables<span v-if="c.tool_count"> · {{ c.tool_count }} tools</span></span>
          </li>
        </ul>
      </section>

      <!-- Example questions -->
      <section v-if="agent.conversation_starters && agent.conversation_starters.length">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('starters')"
        >
          <Icon :name="isOpen('starters') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-chat-bubble-left-right" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Example questions</span>
          <span class="text-gray-400">{{ agent.conversation_starters.length }}</span>
        </button>
        <ul v-if="isOpen('starters')" class="ps-5 mt-1 space-y-0.5">
          <li v-for="(q, i) in agent.conversation_starters" :key="i" class="text-gray-600 truncate">
            <Icon name="heroicons-question-mark-circle" class="w-3 h-3 text-gray-400 me-1 inline" />
            {{ q }}
          </li>
        </ul>
      </section>

      <!-- Tables (top-N) -->
      <section v-if="agent.tables && agent.tables.length">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('tables')"
        >
          <Icon :name="isOpen('tables') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-table-cells" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Tables</span>
          <span class="text-gray-400">
            {{ agent.tables.length }}<template v-if="agent.tables_truncated"> of {{ agent.tables_total }}</template>
          </span>
        </button>
        <ul v-if="isOpen('tables')" class="ps-5 mt-1 space-y-0.5">
          <li v-for="(t, i) in agent.tables" :key="i" class="flex items-center gap-2">
            <span v-if="t.connection_name" class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500 flex-shrink-0 truncate max-w-[100px]">{{ t.connection_name }}</span>
            <code class="text-[11px] text-gray-700 truncate">{{ t.name }}</code>
            <span v-if="t.columns_preview && t.columns_preview.length" class="text-[10px] text-gray-400 truncate">
              ({{ t.columns_preview.slice(0, 4).map((c: any) => c.name).join(', ') }}<span v-if="t.columns_preview.length > 4">, …</span>)
            </span>
          </li>
          <li v-if="agent.tables_truncated" class="text-[10px] text-gray-400">…</li>
        </ul>
      </section>

      <!-- Tools overlay -->
      <section v-if="agent.tools && agent.tools.length">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('tools')"
        >
          <Icon :name="isOpen('tools') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-wrench-screwdriver" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Tools</span>
          <span class="text-gray-400">{{ agent.tools.length }}</span>
        </button>
        <ul v-if="isOpen('tools')" class="ps-5 mt-1 space-y-0.5">
          <li v-for="(t, i) in agent.tools" :key="i" class="flex items-center gap-2">
            <span class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500">{{ t.connection_name }}</span>
            <code class="text-[11px] text-gray-700">{{ t.tool_name }}</code>
            <span
              class="text-[9px] px-1 py-0.5 rounded"
              :class="policyClass(t.policy, t.is_enabled)"
            >{{ t.is_enabled ? t.policy : 'disabled' }}</span>
            <span v-if="t.has_overlay" class="text-[9px] text-blue-500" title="Per-agent override">override</span>
          </li>
        </ul>
      </section>

      <!-- Members -->
      <section v-if="agent.members && agent.members.length">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('members')"
        >
          <Icon :name="isOpen('members') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-user-group" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Members</span>
          <span class="text-gray-400">{{ agent.members.length }}</span>
        </button>
        <ul v-if="isOpen('members')" class="ps-5 mt-1 space-y-0.5">
          <li v-for="(m, i) in agent.members" :key="i" class="flex items-center gap-2">
            <Icon
              :name="m.principal_type === 'group' ? 'heroicons-user-group' : 'heroicons-user'"
              class="w-3 h-3 text-gray-400"
            />
            <span class="text-gray-700">{{ m.name_or_email }}</span>
            <span v-for="p in (m.permissions || [])" :key="p" class="text-[9px] px-1 py-0.5 rounded bg-gray-100 text-gray-500">{{ p }}</span>
          </li>
        </ul>
      </section>

      <!-- Context (collapsed by default if long) -->
      <section v-if="agent.context">
        <button
          type="button"
          class="flex items-center gap-1 hover:text-gray-900"
          @click="toggleSection('context')"
        >
          <Icon :name="isOpen('context') ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3 h-3 text-gray-400 rtl-flip" />
          <Icon name="heroicons-document-text" class="w-3 h-3 text-gray-400" />
          <span class="font-medium text-gray-700">Context</span>
        </button>
        <p v-if="isOpen('context')" class="ps-5 mt-1 text-gray-500 whitespace-pre-wrap leading-snug">{{ agent.context }}</p>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'

interface ToolExecution {
  status: string
  result_json?: any
  arguments_json?: any
}
const props = defineProps<{ toolExecution: ToolExecution }>()

const status = computed<string>(() => props.toolExecution?.status || '')
const result = computed<any>(() => props.toolExecution?.result_json || {})
const args = computed<any>(() => props.toolExecution?.arguments_json || {})
const agent = computed<any>(() => result.value?.agent || null)
const errorMessage = computed<string>(() => result.value?.error_message || '')
const requestedName = computed<string>(() => args.value?.name || agent.value?.name || 'agent')
const primaryType = computed<string>(() => {
  const c = (agent.value?.connections || [])[0]
  return c?.type || 'resource'
})

// Default sections open: connections + starters + tables
const openSections = ref<Set<string>>(new Set(['connections', 'starters', 'tables']))
function toggleSection(key: string) {
  if (openSections.value.has(key)) openSections.value.delete(key)
  else openSections.value.add(key)
}
function isOpen(key: string): boolean { return openSections.value.has(key) }

function policyClass(policy: string, isEnabled: boolean): string {
  if (!isEnabled) return 'bg-gray-100 text-gray-400'
  if (policy === 'deny') return 'bg-red-50 text-red-600'
  if (policy === 'confirm') return 'bg-amber-50 text-amber-700'
  return 'bg-green-50 text-green-700'
}
</script>

<style scoped>
.tool-shimmer {
  animation: shimmer 1.6s linear infinite;
  background: linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(160,160,160,0.15) 50%, rgba(0,0,0,0) 100%);
  background-size: 300% 100%;
  background-clip: text;
}
@keyframes shimmer { 0% { background-position: 0% 0; } 100% { background-position: 100% 0; } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s ease, transform 0.25s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; transform: translateY(2px); }
</style>
